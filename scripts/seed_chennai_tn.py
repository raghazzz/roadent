"""
Seed project_data.db with Chennai / Tamil Nadu emergency services.

1. Pulls hospital / police / ambulance_station / car_repair from OSM Overpass
   within a 30km radius of IIT Madras (12.9916, 80.2337).
2. Adds a short list of manually verified landmark entries (5 hospitals,
   3 police stations, and 4 representative 108-ambulance coverage points).
3. Dedupes by rounded coordinate — same (round(lat,3), round(lng,3)) ~111m
   convention api.py's own get_all_nearby() uses to merge OSM + DB results —
   so nothing here can silently collide with an existing row at query time.

Manual entries take priority over the OSM pull: if Overpass independently
surfaces the same physical hospital (e.g. Fortis Malar) as a raw, lower-detail
node, the verified manual row reserves that coordinate first and the OSM
duplicate is dropped.

Sources for manual entries: apollohospitals.com, tn.gov.in, miotinternational.com,
hospital directory cross-checks (bajajfinservhealth, hexahealth, justdial), OSM
Nominatim geocoding (2026-07-03), and IIT Madras's own published emergency
numbers (ccw.iitm.ac.in emergency-numbers PDF). See project chat for the full
source list per entry.

NOTE on scope: amenity=fuel was intentionally NOT pulled. api.py's search
config and the frontend's TYPE_META only recognize hospital / ambulance /
police / towing / puncture_shop — a "fuel" row would never be queried or
displayed. shop=car_repair is folded into "puncture_shop", the app's existing
bucket for roadside repair/tire shops, so Chennai data blends into the
existing taxonomy without further code changes.

NOTE on the "108 Ambulance" entries: Tamil Nadu's 108 service (GVK EMRI) is a
single centrally-dispatched number with GPS-routed ambulances — there is no
public directory of fixed branch addresses. The 4 rows below are
representative coverage points near named localities (IIT Madras/Adyar,
Guindy, Adyar proper, Kotturpuram), each offset a few hundred metres from its
paired landmark so it doesn't collide in the coordinate dedup. Phone "108" is
the one thing that's exactly correct regardless of location.

Run:  python3 scripts/seed_chennai_tn.py
"""
import os
import sqlite3

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "project_data.db")

CENTER_LAT, CENTER_LNG = 12.9916, 80.2337
RADIUS_M = 30_000
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# OSM amenity/emergency/shop tag -> the app's `type` column
TYPE_MAP = {
    "hospital": "hospital",
    "police": "police",
    "ambulance_station": "ambulance",
    "car_repair": "puncture_shop",
}

QUERY = f"""
[out:json][timeout:90];
(
  nwr["amenity"="hospital"](around:{RADIUS_M},{CENTER_LAT},{CENTER_LNG});
  nwr["amenity"="police"](around:{RADIUS_M},{CENTER_LAT},{CENTER_LNG});
  nwr["emergency"="ambulance_station"](around:{RADIUS_M},{CENTER_LAT},{CENTER_LNG});
  nwr["shop"="car_repair"](around:{RADIUS_M},{CENTER_LAT},{CENTER_LNG});
);
out center tags;
"""

# name, type, lat, lng, phone, address, city, state
MANUAL_ENTRIES = [
    ("Apollo Hospital, Greams Road", "hospital", 13.0632248, 80.2515817,
     "04428290200", "21 Greams Lane, Off Greams Road, Thousand Lights", "Chennai", "Tamil Nadu"),
    ("Rajiv Gandhi Government General Hospital", "hospital", 13.0806077, 80.2773314,
     "04425305000", "EVR Periyar Salai, Park Town", "Chennai", "Tamil Nadu"),
    ("MIOT International", "hospital", 13.0211808, 80.1858412,
     "105710", "Miot International Hospital Road, Manapakkam", "Chennai", "Tamil Nadu"),
    ("Fortis Malar Hospital", "hospital", 13.0101759, 80.2586997,
     "04442892222", "No. 52, 1st Main Road, Gandhi Nagar, Adyar", "Chennai", "Tamil Nadu"),
    ("IIT Madras Institute Hospital", "hospital", 12.9916, 80.2337,
     "04422578333", "IIT Madras Campus (Hospital Ambulance ext. 8333/8888)", "Chennai", "Tamil Nadu"),

    ("Kotturpuram Police Station (J4)", "police", 13.0241585, 80.2431357,
     "04424473472", "4th Main Road, Gandhi Mandapam Road, Kotturpuram", "Chennai", "Tamil Nadu"),
    ("Guindy Police Station (J3)", "police", 13.0093642, 80.2106940,
     "04422501539", "Thiru-Vi-Ka Industrial Estate, Guindy", "Chennai", "Tamil Nadu"),
    ("Adyar Police Station (J2)", "police", 12.9963113, 80.2546950,
     "04423452583", "No. 56, L.B. Road, Adyar", "Chennai", "Tamil Nadu"),

    ("108 Ambulance — IIT Madras / Adyar", "ambulance", 12.9950, 80.2310,
     "108", "State-wide GVK EMRI dispatch — nearest available unit", "Chennai", "Tamil Nadu"),
    ("108 Ambulance — Guindy", "ambulance", 13.0060, 80.2140,
     "108", "State-wide GVK EMRI dispatch — nearest available unit", "Chennai", "Tamil Nadu"),
    ("108 Ambulance — Adyar", "ambulance", 13.0000, 80.2590,
     "108", "State-wide GVK EMRI dispatch — nearest available unit", "Chennai", "Tamil Nadu"),
    ("108 Ambulance — Kotturpuram", "ambulance", 13.0270, 80.2460,
     "108", "State-wide GVK EMRI dispatch — nearest available unit", "Chennai", "Tamil Nadu"),
]


def fetch_osm():
    print(f"[OSM] querying Overpass for a {RADIUS_M / 1000:.0f}km radius around IIT Madras…")
    r = requests.post(
        OVERPASS_URL,
        data={"data": QUERY},
        headers={"User-Agent": "Roadent/2.0 (Road Safety Hackathon 2026 seed script)"},
        timeout=120,
    )
    r.raise_for_status()
    elements = r.json().get("elements", [])
    print(f"[OSM] {len(elements)} raw elements returned")

    rows = []
    for el in elements:
        tags = el.get("tags", {})
        amenity = tags.get("amenity") or tags.get("emergency") or tags.get("shop", "")
        stype = TYPE_MAP.get(amenity)
        if not stype:
            continue

        lat = el.get("lat") or el.get("center", {}).get("lat")
        lng = el.get("lon") or el.get("center", {}).get("lon")
        if not lat or not lng:
            continue

        name = (tags.get("name") or tags.get("name:en") or tags.get("operator")
                or f"Unnamed {stype.replace('_', ' ').title()}")
        phone = (tags.get("phone") or tags.get("contact:phone")
                 or tags.get("contact:mobile") or "N/A")
        addr_parts = [tags.get("addr:housenumber"), tags.get("addr:street")]
        address = ", ".join(p for p in addr_parts if p) or tags.get("addr:full") or "N/A"
        city = tags.get("addr:city") or tags.get("addr:district") or "Chennai"

        rows.append((name, stype, lat, lng, phone, address, city, "Tamil Nadu"))

    return rows


def dedupe(rows, seen):
    """Drop rows whose rounded coordinate is already in `seen`. Mutates
    nothing — returns (surviving_rows, updated_seen_set)."""
    seen = set(seen)
    out = []
    for row in rows:
        lat, lng = row[2], row[3]
        key = (round(lat, 3), round(lng, 3))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out, seen


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    before = cur.execute("SELECT COUNT(*) FROM services").fetchone()[0]
    existing_coords = {
        (round(lat, 3), round(lng, 3))
        for lat, lng in cur.execute(
            "SELECT lat, lng FROM services WHERE lat IS NOT NULL AND lng IS NOT NULL"
        )
    }
    print(f"[DB] {before} existing rows, {len(existing_coords)} unique coordinate buckets")

    # Manual entries first — verified data wins over any raw OSM duplicate of
    # the same physical place.
    manual_rows, seen = dedupe(MANUAL_ENTRIES, existing_coords)
    dropped_manual = len(MANUAL_ENTRIES) - len(manual_rows)
    if dropped_manual:
        print(f"[DEDUPE] {dropped_manual} manual entries collided with an existing row — skipped")

    osm_rows = fetch_osm()
    osm_rows, seen = dedupe(osm_rows, seen)
    print(f"[DEDUPE] {len(osm_rows)} OSM rows survive after dropping duplicates")

    all_rows = manual_rows + osm_rows
    cur.executemany(
        "INSERT INTO services (name, type, lat, lng, phone, address, city, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        all_rows,
    )
    conn.commit()

    after = cur.execute("SELECT COUNT(*) FROM services").fetchone()[0]
    by_type = cur.execute(
        "SELECT type, COUNT(*) FROM services WHERE state='Tamil Nadu' GROUP BY type"
    ).fetchall()

    print(f"\n[DONE] {before} -> {after} rows ({after - before} inserted)")
    print("Tamil Nadu rows by type:")
    for t, n in by_type:
        print(f"  {t:15s} {n}")

    conn.close()


if __name__ == "__main__":
    main()
