"""
Roadent API — Day 2 complete
Run: uvicorn api:app --reload --port 8000
"""

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from typing import Optional
import sqlite3, os, json, re, requests, time, traceback
from collections import defaultdict, deque
from math import radians, sin, cos, sqrt, atan2

# ── Config ─────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_RENDER_DB = '/data/project_data.db'
_LOCAL_DB  = os.path.join(BASE_DIR, 'project_data.db')
DB_PATH    = _RENDER_DB if os.path.isdir('/data') else _LOCAL_DB
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")   # set in terminal: export MISTRAL_API_KEY=your_key
MISTRAL_MODEL   = "mistral-small-latest"
MISTRAL_TIMEOUT = 8          # seconds — tight for emergency context
MAX_RADIUS_KM   = 50         # local DB search radius
OVERPASS_URL    = "https://overpass-api.de/api/interpreter"
ADMIN_TOKEN     = os.environ.get("ADMIN_TOKEN", "")   # required to call POST /api/services
RATE_LIMIT      = 30         # requests
RATE_WINDOW_SEC = 60         # per this many seconds, per client IP

# ── App ─────────────────────────────────────────────────
app = FastAPI(title="Roadent API", version="2.0")

@app.on_event("startup")
def copy_db_to_disk():
    """
    Refresh the persistent disk from the repo's bundled DB on every boot.
    Without Render shell access there's no other way to update /data on
    redeploy, so the repo's project_data.db is treated as the source of
    truth — this intentionally overwrites any rows added at runtime via
    POST /api/services.
    """
    if os.path.isdir('/data'):
        if os.path.exists(_LOCAL_DB):
            import shutil
            shutil.copy(_LOCAL_DB, _RENDER_DB)
            print(f"[STARTUP] Refreshed persistent DB from repo: {_RENDER_DB}")
        else:
            print(f"[STARTUP] No bundled DB found at {_LOCAL_DB} — keeping existing: {_RENDER_DB}")
    else:
        print(f"[STARTUP] Local dev, using: {_LOCAL_DB}")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════
#  HARDENING — rate limiting + exception handling
# ═══════════════════════════════════════════════════════

# In-memory sliding-window log, per client IP. Single-process deployment
# (Render runs one uvicorn worker here), so a plain dict is safe — no
# cross-worker sharing needed. Resets on restart, which is fine for this
# app's threat model (basic abuse/scraping protection, not a hard SLA).
_rate_buckets: dict[str, deque] = defaultdict(deque)

def _client_ip(request: Request) -> str:
    """Prefer the original client IP from Render's proxy, else the socket peer."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        ip = _client_ip(request)
        now = time.time()
        bucket = _rate_buckets[ip]
        while bucket and now - bucket[0] > RATE_WINDOW_SEC:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": "You're sending requests a little too fast — please wait a moment and try again.",
                },
            )
        bucket.append(now)
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Malformed JSON bodies and failed field validation both land here —
    always a clean 400, never FastAPI's default verbose 422."""
    return JSONResponse(
        status_code=400,
        content={
            "error": "invalid_request",
            "message": "Your request couldn't be processed — please check the data you sent and try again.",
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all so an unexpected bug in any endpoint degrades to a clean
    500 instead of leaking a raw stack trace to the client. This only fires
    for exceptions with no more specific handler (HTTPException and
    RequestValidationError are handled above/by FastAPI's defaults first)."""
    print(f"[UNHANDLED] {request.method} {request.url.path}: {exc!r}")
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "Something went wrong on our end. Please try again in a moment.",
        },
    )


# Serve the frontend
from fastapi.responses import FileResponse

@app.get("/", include_in_schema=False)
def root():
    """Serve index.html at root — works on Render and locally."""
    index = os.path.join(BASE_DIR, "static", "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "Roadent API is running. Place index.html in static/ folder."}

@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Serve the service worker from root so its scope covers the whole app, not just /static/."""
    sw = os.path.join(BASE_DIR, "static", "sw.js")
    return FileResponse(sw, media_type="application/javascript")

# Serve static assets (CSS, JS etc)
if os.path.isdir(os.path.join(BASE_DIR, "static")):
    app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Input sanitization ───────────────────────────────────
_HTML_TAG_RE = re.compile(r'<[^>]*>')

def strip_html(text: str) -> str:
    """Strip HTML tags from free-text user input before it reaches Mistral
    or gets echoed back — a lightweight defense, not a full sanitizer, but
    enough to stop tags/scripts riding along in the prompt or UI."""
    return _HTML_TAG_RE.sub('', text)


# ── Models ──────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    history: list[dict] = []       # conversation history for multi-turn
    offline: bool = False

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message cannot be empty")
        if len(v) > 1000:
            raise ValueError("message must be 1000 characters or fewer")
        cleaned = strip_html(v).strip()
        if not cleaned:
            raise ValueError("message cannot be empty")
        return cleaned

class EmergencyRequest(BaseModel):
    lat: float
    lng: float
    mode: Optional[str] = "emergency"   # "emergency" (default) or "breakdown"

class AddServiceRequest(BaseModel):
    name: str
    type: str
    lat: float
    lng: float
    phone: Optional[str] = "N/A"
    address: Optional[str] = "N/A"
    city: Optional[str] = ""
    state: Optional[str] = ""


# ── Haversine ───────────────────────────────────────────
def haversine(lat1, lng1, lat2, lng2) -> float:
    R = 6371
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


# ── DB helpers ──────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def find_nearest(lat, lng, stype, limit=5, radius_km=MAX_RADIUS_KM) -> list[dict]:
    """Return nearest services of a given type within radius_km."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM services WHERE type = ?", (stype,)).fetchall()
    conn.close()
    results = []
    for row in rows:
        if row["lat"] and row["lng"]:
            d = haversine(lat, lng, row["lat"], row["lng"])
            if d <= radius_km:
                r = dict(row)
                r["distance_km"] = round(d, 2)
                results.append(r)
    results.sort(key=lambda x: x["distance_km"])
    return results[:limit]


def find_nearest_adaptive(lat, lng, stype, target=5) -> tuple[list[dict], float]:
    """
    Adaptive radius search — expands 10km → 25km → 50km → 100km
    until we have enough results. Fixes sparse rural areas returning
    results from 40km+ away when closer ones exist within 25km.
    """
    conn = get_conn()
    rows = conn.execute("SELECT * FROM services WHERE type = ?", (stype,)).fetchall()
    conn.close()

    scored = []
    for row in rows:
        if row["lat"] and row["lng"]:
            d = haversine(lat, lng, row["lat"], row["lng"])
            r = dict(row)
            r["distance_km"] = round(d, 2)
            scored.append(r)
    scored.sort(key=lambda x: x["distance_km"])

    for radius in [10, 25, 50, 100]:
        within = [r for r in scored if r["distance_km"] <= radius]
        if len(within) >= target or radius == 100:
            return within[:target], radius

    return [], 100


def get_all_nearby(lat, lng) -> dict:
    """
    Smart search — always queries OSM first for the user's immediate area
    (5km radius) to get truly local results, then fills gaps from local DB.
    Results are strictly sorted by distance, no city-jumping.
    """
    # Step 1: Always try OSM for a tight 5km radius around the user
    # This gets real nearby clinics, hospitals, police that OSM has mapped
    osm_results = osm_fallback(lat, lng, radius_m=5000)

    # Step 2: Also get local DB results within 15km max
    config = {
        "hospital":      5,
        "ambulance":     3,
        "police":        3,
        "towing":        2,
        "puncture_shop": 2,
    }
    db_results = []
    for stype, target in config.items():
        svcs, _ = find_nearest_adaptive(lat, lng, stype, target=target)
        # Hard cap: only include DB results within 15km
        # so we never show a hospital 40km away if OSM has one at 2km
        close_svcs = [s for s in svcs if s["distance_km"] <= 15]
        db_results.extend(close_svcs)

    # Step 3: Merge — OSM results take priority, DB fills gaps
    # Deduplicate by proximity (same place mapped in both sources)
    all_results = []
    seen_coords = set()

    # Add OSM first (higher quality for immediate area)
    for s in osm_results:
        coord_key = (round(s["lat"], 3), round(s["lng"], 3))
        if coord_key not in seen_coords:
            seen_coords.add(coord_key)
            all_results.append(s)

    # Add DB results that aren't already covered by OSM
    for s in db_results:
        coord_key = (round(s["lat"], 3), round(s["lng"], 3))
        if coord_key not in seen_coords:
            seen_coords.add(coord_key)
            all_results.append(s)

    # Step 4: If we still have very few results, widen OSM to 10km
    if len(all_results) < 3:
        wider = osm_fallback(lat, lng, radius_m=10000)
        for s in wider:
            coord_key = (round(s["lat"], 3), round(s["lng"], 3))
            if coord_key not in seen_coords:
                seen_coords.add(coord_key)
                all_results.append(s)

    # Step 5: Sort strictly by distance — closest first, always
    all_results.sort(key=lambda x: x["distance_km"])

    # Step 6: Group by type for the frontend cards
    grouped = {}
    type_limits = {
        "hospital": 5, "ambulance": 3, "police": 3,
        "towing": 2, "puncture_shop": 2
    }
    type_counts = {}
    final_flat = []

    for s in all_results:
        stype = s["type"]
        limit = type_limits.get(stype, 3)
        type_counts[stype] = type_counts.get(stype, 0) + 1
        if type_counts[stype] <= limit:
            grouped.setdefault(stype, []).append(s)
            final_flat.append(s)

    print(f"[SEARCH] {len(osm_results)} OSM + {len(db_results)} DB → {len(final_flat)} final results")
    return {"grouped": grouped, "flat": final_flat}


def get_breakdown_nearby(lat, lng) -> dict:
    """
    Vehicle Breakdown search — petrol pumps, mechanics, towing, and puncture
    shops from the local DB. Local-DB only for now (no live OSM fallback like
    get_all_nearby() has for emergency mode) since the seeded regions already
    cover Delhi NCR, Jaipur, Rohtak, Gurugram, and Chennai.
    """
    config = {
        "petrol_pump":   5,
        "mechanic":      3,
        "towing":        2,
        "puncture_shop": 3,
    }
    db_results = []
    for stype, target in config.items():
        svcs, _ = find_nearest_adaptive(lat, lng, stype, target=target)
        close_svcs = [s for s in svcs if s["distance_km"] <= 15]
        db_results.extend(close_svcs)

    db_results.sort(key=lambda x: x["distance_km"])

    grouped = {}
    type_limits = {"petrol_pump": 5, "mechanic": 3, "towing": 2, "puncture_shop": 3}
    type_counts = {}
    final_flat = []

    for s in db_results:
        stype = s["type"]
        limit = type_limits.get(stype, 3)
        type_counts[stype] = type_counts.get(stype, 0) + 1
        if type_counts[stype] <= limit:
            grouped.setdefault(stype, []).append(s)
            final_flat.append(s)

    print(f"[BREAKDOWN] {len(final_flat)} final results")
    return {"grouped": grouped, "flat": final_flat}


# ── OSM Fallback (for locations outside seeded area) ────
def osm_fallback(lat, lng, radius_m=10000) -> list[dict]:
    """
    Query OpenStreetMap Overpass API for emergency services globally.
    Uses 'nwr' (node + way + relation) so building-mapped hospitals are found.
    'out center' gives a centroid lat/lng even for polygon ways.
    Works for any country — this is the global applicability feature.
    """
    query = f"""
[out:json][timeout:20];
(
  nwr["amenity"="hospital"](around:{radius_m},{lat},{lng});
  nwr["amenity"="clinic"](around:{radius_m},{lat},{lng});
  nwr["amenity"="police"](around:{radius_m},{lat},{lng});
  nwr["emergency"="ambulance_station"](around:{radius_m},{lat},{lng});
  nwr["amenity"="fire_station"](around:{radius_m},{lat},{lng});
);
out center;
"""
    TYPE_MAP = {
        "hospital":          "hospital",
        "clinic":            "hospital",
        "police":            "police",
        "ambulance_station": "ambulance",
        "fire_station":      "towing",   # closest proxy for roadside help
    }
    try:
        r = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": "Roadent/2.0 (student hackathon project)"},
            timeout=22,
        )
        r.raise_for_status()
        results = []
        seen: dict[str, int] = {}

        for el in r.json().get("elements", []):
            tags = el.get("tags", {})
            amenity = tags.get("amenity") or tags.get("emergency", "")
            stype = TYPE_MAP.get(amenity)
            if not stype:
                continue

            # Nodes have lat/lon directly; ways/relations expose a 'center' object
            el_lat = el.get("lat") or el.get("center", {}).get("lat")
            el_lng = el.get("lon") or el.get("center", {}).get("lon")
            if not el_lat or not el_lng:
                continue

            seen[stype] = seen.get(stype, 0) + 1
            if seen[stype] > 6:   # max 6 per type from OSM
                continue

            # Best-effort name — OSM data quality varies internationally
            name = (tags.get("name")
                    or tags.get("name:en")
                    or tags.get("operator")
                    or f"Nearby {stype.replace('_', ' ').title()}")

            results.append({
                "name":        name,
                "type":        stype,
                "lat":         el_lat,
                "lng":         el_lng,
                "phone":       (tags.get("phone")
                                or tags.get("contact:phone")
                                or tags.get("contact:mobile")
                                or "N/A"),
                "address":     (tags.get("addr:full")
                                or tags.get("addr:street")
                                or ""),
                "city":        tags.get("addr:city") or tags.get("addr:district") or "",
                "state":       tags.get("addr:state") or tags.get("addr:province") or "",
                "distance_km": round(haversine(lat, lng, el_lat, el_lng), 2),
                "source":      "openstreetmap",
            })

        results.sort(key=lambda x: x["distance_km"])
        print(f"[OSM] found {len(results)} results near ({lat:.3f},{lng:.3f})")
        return results

    except Exception as e:
        print(f"[OSM] fallback failed: {e}")
        return []


# ── Mistral call ─────────────────────────────────────────
def _trim_history(history: list[dict], max_turns: int = 6) -> list[dict]:
    """
    Keep only the last N user/assistant pairs to stay within token limits.
    Always keeps the first turn (initial SOS context) + most recent turns.
    Rough budget: system ~400 tokens, context ~300, history ~150/turn → safe at 6 turns.
    """
    if len(history) <= max_turns:
        return history
    # Always keep the first exchange (has the original emergency context)
    first_pair = history[:2]
    recent = history[-(max_turns - 2):]
    return first_pair + recent


def call_mistral(user_message: str, history: list[dict],
                 services_context: str, offline: bool, intent: str = "emergency") -> str:
    """
    Call Mistral with smart history trimming + service context.
    Falls back to a clean offline structured reply on any failure.
    """
    if offline or not MISTRAL_API_KEY:
        return _offline_reply(services_context, intent)

    system = (
        "You are Roadent, a calm and clear road-safety and emergency assistant for India. "
        "You help with active road accidents AND general road-safety questions — insurance "
        "claims, FIR filing, first aid, and what to do after a minor accident.\n\n"
        "Rules:\n"
        "- Always be brief and action-oriented. People may be stressed.\n"
        "- If nearby services are listed below, lead with the single most critical one "
        "(hospital or ambulance first) and always include its phone number.\n"
        "- If no services are listed, this is a general question — answer directly from "
        "your own knowledge without inventing nearby places.\n"
        "- FIR filing: go to the nearest police station (or the state e-FIR portal if "
        "available for minor/no-injury cases), note vehicle numbers and witnesses, and get "
        "a copy of the FIR for insurance.\n"
        "- Insurance claims: inform the insurer promptly (many require notice within "
        "24-48 hours), photograph the damage, keep the FIR and any medical reports, and "
        "don't admit fault in writing before the claim is assessed.\n"
        "- First aid: check responsiveness and breathing, don't move a seriously injured "
        "person unless there's immediate danger (fire, oncoming traffic), apply firm "
        "pressure to control heavy bleeding, and call 108 for anything beyond minor "
        "cuts/bruises.\n"
        "- The context below always tells you whether the user's location is known. "
        "Never say you can't track or access their location if the context says it's "
        "known — only ask them to enable location or share their city/area if the "
        "context explicitly says location is unavailable.\n"
        "- Never make up phone numbers. If unknown, say 'call 108'.\n"
        "- Keep every response under 150 words.\n\n"
        f"Services near the user right now:\n{services_context}"
    )

    trimmed_history = _trim_history(history)
    messages = (
        [{"role": "system", "content": system}]
        + trimmed_history
        + [{"role": "user", "content": user_message}]
    )

    try:
        r = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model":      MISTRAL_MODEL,
                "messages":   messages,
                "max_tokens": 300,
                "temperature": 0.3,   # low temp = more reliable, less creative in emergencies
            },
            timeout=MISTRAL_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    except requests.exceptions.Timeout:
        print("[Mistral] timeout — using offline fallback")
        return _offline_reply(services_context, intent)
    except Exception as e:
        print(f"[Mistral] error: {e}")
        return _offline_reply(services_context, intent)


def _offline_reply(context: str, intent: str = "emergency") -> str:
    """Direct structured reply from DB data — no internet needed."""
    if intent == "general":
        return (
            "⚡ Offline mode — I can't reach the AI assistant right now, so I can't answer "
            "general questions in detail. For anything urgent, call 108 (ambulance) or "
            "100 (police)."
        )
    lines = [l for l in context.split("\n") if l.strip() and not l.startswith("Nearby services")]
    body = "\n".join(lines[:20])
    return (
        "⚡ Offline mode — showing local database results.\n\n"
        "🚨 Call 108 (ambulance) or 100 (police) immediately.\n\n"
        + body
    )

def format_context(services: list[dict]) -> str:
    if not services:
        return "No services found in local DB within 50km."
    by_type: dict[str, list] = {}
    for s in services:
        by_type.setdefault(s["type"], []).append(s)
    lines = []
    for t, svcs in by_type.items():
        lines.append(f"\n{t.upper()}:")
        for s in svcs:
            lines.append(f"  - {s['name']} | {s['distance_km']}km | Phone: {s['phone']} | {s.get('address','')}")
    return "\n".join(lines)


# ── Intent detection ─────────────────────────────────────
# Simple keyword match, checked in priority order: a specific-service mention
# (emergency or breakdown) wins over a generic proximity phrase, so "nearest
# petrol pump" is breakdown, not emergency, even though "nearest" alone would
# default to emergency.
EMERGENCY_KEYWORDS = [
    "accident", "injury", "injured", "emergency", "hurt", "bleeding",
    "crash", "collision", "sos", "hospital", "police station", "police", "clinic",
]
BREAKDOWN_KEYWORDS = [
    "puncture", "fuel", "petrol", "diesel", "engine", "breakdown", "broke down",
    "broken down", "battery", "won't start", "wont start", "flat tire", "flat tyre",
    "mechanic", "tow",
]
# Pure "where am I" questions — answered by reverse-geocoding the coordinates
# directly, not by fetching services. Checked after emergency/breakdown so
# "hospitals near my location" still searches instead of just naming the area.
LOCATION_INFO_KEYWORDS = [
    "my location", "where am i", "what is my location", "current location",
]
# Generic proximity phrases with no specific service word — default to an
# emergency search (the app's core safety use case) rather than falling
# through to a no-services "general" answer.
PROXIMITY_KEYWORDS = ["near me", "nearby", "nearest", "close to me", "around me"]


def detect_intent(message: str) -> str:
    """Returns 'emergency', 'breakdown', 'location_info', or 'general'."""
    m = message.lower()
    if any(kw in m for kw in EMERGENCY_KEYWORDS):
        return "emergency"
    if any(kw in m for kw in BREAKDOWN_KEYWORDS):
        return "breakdown"
    if any(kw in m for kw in LOCATION_INFO_KEYWORDS):
        return "location_info"
    if any(kw in m for kw in PROXIMITY_KEYWORDS):
        return "emergency"
    return "general"


def reverse_geocode(lat: float, lng: float) -> Optional[str]:
    """
    Reverse-geocode coordinates to a human-readable area name via OSM
    Nominatim. Returns None on any failure so the caller can degrade
    gracefully instead of erroring out.
    """
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lng, "format": "json", "zoom": 14},
            headers={"User-Agent": "Roadent/2.0 (Road Safety Hackathon 2026)"},
            timeout=6,
        )
        r.raise_for_status()
        addr = r.json().get("address", {})
        area = (addr.get("suburb") or addr.get("neighbourhood")
                or addr.get("city_district") or addr.get("town") or addr.get("village"))
        # Nominatim sometimes prefixes the locality with an admin zone label
        # (e.g. "Zone 13 Adyar") — strip that for a cleaner user-facing name.
        if area:
            area = re.sub(r'^Zone\s+\d+\s+', '', area)

        # For big Indian metros, `city` is often the formal municipal
        # corporation name (e.g. "Chennai Corporation") — state_district is
        # usually the plain, commonly-used city name (e.g. "Chennai").
        city = addr.get("state_district") or addr.get("city") or addr.get("town") or addr.get("county")
        state = addr.get("state")

        seen = set()
        ordered = []
        for part in (area, city, state):
            if part and part not in seen:
                seen.add(part)
                ordered.append(part)
        return ", ".join(ordered) if ordered else None
    except Exception as e:
        print(f"[reverse_geocode] failed: {e}")
        return None


def forward_geocode(query: str) -> Optional[dict]:
    """
    Forward-geocode a free-text place name (city, area, landmark) to
    coordinates via OSM Nominatim, restricted to India. Returns None on no
    match or failure so the caller can fall back to treating the text as a
    normal chat message instead of a location.
    """
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "in"},
            headers={"User-Agent": "Roadent/2.0 (Road Safety Hackathon 2026)"},
            timeout=6,
        )
        r.raise_for_status()
        results = r.json()
        if not results:
            return None
        top = results[0]
        return {
            "lat": float(top["lat"]),
            "lng": float(top["lon"]),
            "display_name": top.get("display_name"),
        }
    except Exception as e:
        print(f"[forward_geocode] failed: {e}")
        return None


# ═══════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════

@app.get("/health")
def health():
    """Always returns OK. Frontend uses this to detect online/offline."""
    return {"status": "ok", "mistral_ready": bool(MISTRAL_API_KEY), "db": DB_PATH}


@app.get("/api/geocode")
def geocode(query: str):
    """
    Forward-geocode a typed place name to coordinates — used when GPS is
    denied/unavailable and the user types their city or area instead.
    """
    result = forward_geocode(query)
    if not result:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return result


@app.get("/api/services")
def list_services(type: Optional[str] = None, state: Optional[str] = None, limit: int = 50):
    """List all services. Filter by type or state. Useful for judges."""
    conn = get_conn()
    q = "SELECT * FROM services WHERE 1=1"
    params = []
    if type:  q += " AND type=?";  params.append(type)
    if state: q += " AND state LIKE ?"; params.append(f"%{state}%")
    q += f" LIMIT {min(limit, 500)}"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    return {"total": len(rows), "data": rows}


@app.get("/api/stats")
def db_stats():
    """DB summary stats — great for the demo slide."""
    conn = get_conn()
    total  = conn.execute("SELECT COUNT(*) FROM services").fetchone()[0]
    by_type  = {r[0]: r[1] for r in conn.execute("SELECT type, COUNT(*) FROM services GROUP BY type")}
    by_state = {r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM services GROUP BY state ORDER BY 2 DESC")}
    conn.close()
    return {"total_services": total, "by_type": by_type, "by_state": by_state}


@app.post("/api/emergency")
def emergency_lookup(req: EmergencyRequest):
    """
    Fast raw lookup — returns structured services JSON without LLM.
    Perfect for the SOS button that needs results in <1 second.

    mode="breakdown" switches to Vehicle Breakdown results (petrol pumps,
    mechanics, towing, puncture shops) instead of hospitals/police/ambulance.
    """
    if req.mode == "breakdown":
        data = get_breakdown_nearby(req.lat, req.lng)
        flat = data["flat"]
        return {
            "services":         flat,
            "by_type":          data["grouped"],
            "total":            len(flat),
            "source":           "local_db" if flat else "none",
            "mode":             "breakdown",
        }

    data = get_all_nearby(req.lat, req.lng)
    flat = data["flat"]

    # OSM fallback if local DB has fewer than 2 results
    if len(flat) < 2:
        osm = osm_fallback(req.lat, req.lng)
        if osm:
            flat = flat + osm
            flat.sort(key=lambda x: x["distance_km"])
            data["flat"] = flat

    return {
        "services":         flat,
        "by_type":          data["grouped"],
        "total":            len(flat),
        "source":           "local_db" if len(flat) >= 2 else "openstreetmap",
        "emergency_numbers": {"ambulance": "108", "police": "100", "fire": "101", "highway": "1073"},
    }


@app.post("/api/chat")
def chat(req: ChatRequest):
    """
    Main chatbot endpoint.
    - Accepts message + lat/lng + conversation history
    - Detects intent from the message: "emergency" (accident/injury/hospital/
      police/near-me wording) fetches hospitals/police/ambulance, "breakdown"
      (puncture/fuel/engine/mechanic wording) fetches petrol pumps/mechanics/
      towing/repair shops, "location_info" ("where am I") reverse-geocodes the
      coordinates directly, anything else is answered conversationally with no
      service lookup
    - Calls Mistral; falls back to offline structured reply if API fails
    - Multi-turn: pass history[] from frontend for follow-up questions
    """
    intent = detect_intent(req.message)
    has_location = req.lat is not None and req.lng is not None

    # "Where am I" is answered directly by reverse-geocoding — deterministic,
    # no LLM round-trip, no risk of the model guessing/hallucinating a place.
    if intent == "location_info":
        if has_location:
            place = reverse_geocode(req.lat, req.lng)
            reply = (f"You are near {place}." if place
                      else f"I have your coordinates ({req.lat:.4f}, {req.lng:.4f}) but couldn't resolve them to a place name right now.")
        else:
            reply = "I don't have your location yet — please enable GPS access, or tell me your city or area."

        return {
            "reply":         reply,
            "services":      [],
            "source":        "reverse_geocode" if has_location else "none",
            "offline":       req.offline,
            "location_used": has_location,
            "intent":        intent,
        }

    services = []
    source = "none"

    if has_location and intent in ("emergency", "breakdown"):
        if intent == "breakdown":
            data = get_breakdown_nearby(req.lat, req.lng)
            flat = data["flat"]
            source = "local_db" if flat else "none"
        else:
            data = get_all_nearby(req.lat, req.lng)
            flat = data["flat"]

            # OSM fallback if local sparse
            if len(flat) < 2:
                osm = osm_fallback(req.lat, req.lng)
                flat = flat + osm
                flat.sort(key=lambda x: x["distance_km"])
                source = "openstreetmap" if osm else "local_db"
            else:
                source = "local_db"

        services = flat

    if services:
        context = format_context(services)
    elif has_location:
        # No service search was triggered for this message, but the user's
        # location IS known — the system prompt is instructed not to claim
        # otherwise.
        context = (
            "The user's location IS known (lat/lng provided), but no specific "
            "nearby-service search was triggered for this message. Do not say "
            "you can't track their location — just answer their question directly."
        )
    else:
        context = (
            "The user's location is NOT available for this message. If relevant, "
            "politely ask them to enable GPS/location access, or tell you their "
            "city or area."
        )

    is_offline = req.offline or not MISTRAL_API_KEY

    reply = call_mistral(
        user_message=req.message,
        history=req.history,
        services_context=context,
        offline=is_offline,
        intent=intent,
    )

    return {
        "reply":         reply,
        "services":      services,
        "source":        source,
        "offline":       is_offline,
        "location_used": has_location,
        "intent":        intent,
    }


@app.get("/api/nearby")
def nearby(lat: float, lng: float, radius_km: float = 30):
    """Quick URL-param lookup. e.g. /api/nearby?lat=28.89&lng=76.58"""
    if radius_km > 200:
        raise HTTPException(400, "radius_km must be ≤ 200")
    types = ["hospital", "police", "ambulance", "towing", "puncture_shop"]
    all_svcs = []
    for t in types:
        all_svcs.extend(find_nearest(lat, lng, t, limit=3, radius_km=radius_km))
    all_svcs.sort(key=lambda x: x["distance_km"])
    return {"lat": lat, "lng": lng, "radius_km": radius_km,
            "services": all_svcs, "count": len(all_svcs)}


@app.post("/api/services")
def add_service(req: AddServiceRequest, x_admin_token: Optional[str] = Header(default=None)):
    """
    Add a new service to the local DB. Admin-only: requires the ADMIN_TOKEN
    env var to be set and matched via the X-Admin-Token header — without
    this, anyone could inject fake hospitals/services into the live DB.
    If ADMIN_TOKEN isn't set on the server, this endpoint is disabled outright.
    """
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")

    valid = {"hospital","police","ambulance","towing","puncture_shop","showroom","petrol_pump","mechanic"}
    if req.type not in valid:
        raise HTTPException(400, f"type must be one of: {', '.join(valid)}")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO services (name,type,lat,lng,phone,address,city,state) VALUES (?,?,?,?,?,?,?,?)",
        (req.name, req.type, req.lat, req.lng, req.phone, req.address, req.city, req.state)
    )
    new_id = cur.lastrowid
    conn.commit(); conn.close()
    return {"success": True, "id": new_id}


# ── Incident Report endpoint ─────────────────────────────
class ReportRequest(BaseModel):
    lat: float
    lng: float
    description: str = "Road accident reported via Roadent"
    services_shown: list[dict] = []   # the services that were surfaced to the user

@app.post("/api/report")
def generate_report(req: ReportRequest):
    """
    Generate a structured incident report from a user's emergency session.
    Useful for:
      - Judges: proves the system worked end-to-end
      - Demo slide: show a real output
      - Real use: user can copy/share with family or insurance
    """
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Build services summary
    svc_lines = []
    for s in req.services_shown[:10]:   # cap at 10 for report length
        svc_lines.append(
            f"  • [{s.get('type','').upper()}] {s.get('name','—')} "
            f"| {s.get('distance_km','?')} km "
            f"| {s.get('phone','N/A')}"
        )

    services_text = "\n".join(svc_lines) if svc_lines else "  No services recorded."

    # Nearest hospital and police for the summary header
    hospitals = [s for s in req.services_shown if s.get("type") == "hospital"]
    police    = [s for s in req.services_shown if s.get("type") == "police"]
    ambulance = [s for s in req.services_shown if s.get("type") == "ambulance"]

    nearest_hospital  = hospitals[0]["name"]  if hospitals  else "N/A"
    nearest_police    = police[0]["name"]     if police     else "N/A"
    nearest_ambulance = ambulance[0]["name"]  if ambulance  else "N/A"

    report_text = f"""
╔══════════════════════════════════════════════════════╗
           ROADENT — INCIDENT REPORT
╚══════════════════════════════════════════════════════╝

Timestamp      : {timestamp}
Location (GPS) : {req.lat:.6f}, {req.lng:.6f}
Google Maps    : https://maps.google.com/?q={req.lat},{req.lng}

Incident       : {req.description}

─── NEAREST CRITICAL SERVICES ────────────────────────
  Hospital     : {nearest_hospital}
  Ambulance    : {nearest_ambulance}
  Police       : {nearest_police}

─── ALL SERVICES SURFACED ────────────────────────────
{services_text}

─── NATIONAL EMERGENCY NUMBERS ───────────────────────
  Ambulance    : 108
  Police       : 100
  Fire Brigade : 101
  Road Helpline: 1073

─── GENERATED BY ─────────────────────────────────────
  Roadent Emergency Assistant
  Road Safety Hackathon 2026 | CoERS, IIT Madras
  AI in Road Safety — Team submission
══════════════════════════════════════════════════════
""".strip()

    return {
        "report":    report_text,
        "timestamp": timestamp,
        "location":  {"lat": req.lat, "lng": req.lng},
        "maps_url":  f"https://maps.google.com/?q={req.lat},{req.lng}",
        "summary": {
            "nearest_hospital":  nearest_hospital,
            "nearest_ambulance": nearest_ambulance,
            "nearest_police":    nearest_police,
            "total_services":    len(req.services_shown),
        },
    }