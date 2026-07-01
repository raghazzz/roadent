"""
Roadent API — Day 2 complete
Run: uvicorn api:app --reload --port 8000
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3, os, json, requests
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

# ── App ─────────────────────────────────────────────────
app = FastAPI(title="Roadent API", version="2.0")

@app.on_event("startup")
def copy_db_to_disk():
    """Copy bundled DB to Render persistent disk on first boot."""
    if os.path.isdir('/data') and not os.path.exists(_RENDER_DB):
        import shutil
        if os.path.exists(_LOCAL_DB):
            shutil.copy(_LOCAL_DB, _RENDER_DB)
            print(f"[STARTUP] Copied DB to persistent disk: {_RENDER_DB}")
    elif os.path.isdir('/data'):
        print(f"[STARTUP] Using existing persistent DB: {_RENDER_DB}")
    else:
        print(f"[STARTUP] Local dev, using: {_LOCAL_DB}")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
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

# Serve static assets (CSS, JS etc)
if os.path.isdir(os.path.join(BASE_DIR, "static")):
    app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Models ──────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    history: list[dict] = []       # conversation history for multi-turn
    offline: bool = False

class EmergencyRequest(BaseModel):
    lat: float
    lng: float

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
                 services_context: str, offline: bool) -> str:
    """
    Call Mistral with smart history trimming + service context.
    Falls back to a clean offline structured reply on any failure.
    """
    if offline or not MISTRAL_API_KEY:
        return _offline_reply(services_context)

    system = (
        "You are Roadent, a calm and clear emergency road-safety assistant. "
        "You help accident victims and bystanders find help fast.\n\n"
        "Rules:\n"
        "- Always be brief and action-oriented. People are stressed.\n"
        "- Lead with the single most critical service (hospital or ambulance first).\n"
        "- Always include the phone number so the user can call immediately.\n"
        "- For follow-up questions, answer from the services data provided.\n"
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
        return _offline_reply(services_context)
    except Exception as e:
        print(f"[Mistral] error: {e}")
        return _offline_reply(services_context)


def _offline_reply(context: str) -> str:
    """Direct structured reply from DB data — no internet needed."""
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


# ═══════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════

@app.get("/health")
def health():
    """Always returns OK. Frontend uses this to detect online/offline."""
    return {"status": "ok", "mistral_ready": bool(MISTRAL_API_KEY), "db": DB_PATH}


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
    """
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
    - Queries local DB; falls back to OSM if sparse
    - Calls Mistral; falls back to offline structured reply if API fails
    - Multi-turn: pass history[] from frontend for follow-up questions
    """
    services = []
    source = "none"

    if req.lat is not None and req.lng is not None:
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

    context = format_context(services)
    is_offline = req.offline or not MISTRAL_API_KEY

    reply = call_mistral(
        user_message=req.message,
        history=req.history,
        services_context=context,
        offline=is_offline,
    )

    return {
        "reply":         reply,
        "services":      services,
        "source":        source,
        "offline":       is_offline,
        "location_used": req.lat is not None,
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
def add_service(req: AddServiceRequest):
    """Add a new service to the local DB (judges can test this too)."""
    valid = {"hospital","police","ambulance","towing","puncture_shop","showroom"}
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