"""
Seed project_data.db with petrol pumps and mechanics for Vehicle Breakdown mode.

Pulls amenity=fuel -> petrol_pump and shop=car_repair -> mechanic from OSM
Overpass, one region at a time (each region is its own try/except so a slow
or failing Overpass query for one city doesn't kill the whole run), across:
Delhi NCR, Jaipur, Rohtak, Gurugram, and Chennai.

Dedupes by rounded coordinate — same (round(lat,3), round(lng,3)) ~111m
convention used everywhere else in this repo (api.py's get_all_nearby(),
scripts/seed_chennai_tn.py) — against the full existing table. This means
in Chennai specifically, shop=car_repair nodes already seeded as
"puncture_shop" by seed_chennai_tn.py will mostly dedupe out here rather
than appear twice under a different type name; that's expected, not a bug —
Vehicle Breakdown mode queries both "mechanic" and "puncture_shop", so those
shops still show up either way.

Run:  python3 scripts/seed_breakdown_services.py
"""
import os
import sqlite3
import time

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "project_data.db")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
RADIUS_M = 30_000

# name, lat, lng, state (for rows where OSM doesn't supply addr:state)
REGIONS = [
    ("Delhi NCR", 28.6139, 77.2090, "Delhi"),
    ("Jaipur", 26.9124, 75.7873, "Rajasthan"),
    ("Rohtak", 28.8955, 76.6066, "Haryana"),
    ("Gurugram", 28.4595, 77.0266, "Haryana"),
    ("Chennai", 12.9916, 80.2337, "Tamil Nadu"),
]

TYPE_MAP = {
    "fuel": "petrol_pump",
    "car_repair": "mechanic",
}


def query_region(name, lat, lng, state):
    query = f"""
[out:json][timeout:90];
(
  nwr["amenity"="fuel"](around:{RADIUS_M},{lat},{lng});
  nwr["shop"="car_repair"](around:{RADIUS_M},{lat},{lng});
);
out center tags;
"""
    print(f"[OSM] {name}: querying a {RADIUS_M / 1000:.0f}km radius…")
    r = requests.post(
        OVERPASS_URL,
        data={"data": query},
        headers={"User-Agent": "Roadent/2.0 (Road Safety Hackathon 2026 seed script)"},
        timeout=120,
    )
    r.raise_for_status()
    elements = r.json().get("elements", [])
    print(f"[OSM] {name}: {len(elements)} raw elements")

    rows = []
    for el in elements:
        tags = el.get("tags", {})
        tag_value = tags.get("amenity") or tags.get("shop", "")
        stype = TYPE_MAP.get(tag_value)
        if not stype:
            continue

        lat_ = el.get("lat") or el.get("center", {}).get("lat")
        lng_ = el.get("lon") or el.get("center", {}).get("lon")
        if not lat_ or not lng_:
            continue

        default_name = "Petrol Pump" if stype == "petrol_pump" else "Mechanic / Car Repair"
        svc_name = (tags.get("name") or tags.get("brand") or tags.get("operator")
                    or f"Unnamed {default_name}")
        phone = (tags.get("phone") or tags.get("contact:phone")
                 or tags.get("contact:mobile") or "N/A")
        addr_parts = [tags.get("addr:housenumber"), tags.get("addr:street")]
        address = ", ".join(p for p in addr_parts if p) or tags.get("addr:full") or "N/A"
        city = tags.get("addr:city") or tags.get("addr:district") or name

        rows.append((svc_name, stype, lat_, lng_, phone, address, city, state))

    return rows


def dedupe(rows, seen):
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
    seen = {
        (round(lat, 3), round(lng, 3))
        for lat, lng in cur.execute(
            "SELECT lat, lng FROM services WHERE lat IS NOT NULL AND lng IS NOT NULL"
        )
    }
    print(f"[DB] {before} existing rows, {len(seen)} unique coordinate buckets")

    all_new_rows = []
    for name, lat, lng, state in REGIONS:
        try:
            region_rows = query_region(name, lat, lng, state)
        except Exception as e:
            print(f"[OSM] {name}: FAILED — {e}")
            continue

        survivors, seen = dedupe(region_rows, seen)
        # Also drop junk nodes with no name, no phone, no address — a card
        # with nothing to call/find is worse than not showing one.
        survivors = [r for r in survivors if not (r[0].startswith("Unnamed") and r[4] == "N/A" and r[5] == "N/A")]
        print(f"[DEDUPE] {name}: {len(survivors)} rows survive")
        all_new_rows.extend(survivors)
        time.sleep(1)  # be polite to the public Overpass instance between regions

    cur.executemany(
        "INSERT INTO services (name, type, lat, lng, phone, address, city, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        all_new_rows,
    )
    conn.commit()

    after = cur.execute("SELECT COUNT(*) FROM services").fetchone()[0]
    by_type_state = cur.execute(
        "SELECT state, type, COUNT(*) FROM services WHERE type IN ('petrol_pump','mechanic') "
        "GROUP BY state, type ORDER BY state, type"
    ).fetchall()

    print(f"\n[DONE] {before} -> {after} rows ({after - before} inserted)")
    print("petrol_pump / mechanic rows by state:")
    for state, t, n in by_type_state:
        print(f"  {state:12s} {t:12s} {n}")

    conn.close()


if __name__ == "__main__":
    main()
