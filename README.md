# Roadent — AI Road Emergency Assistant

Road Safety Hackathon 2026 | CoERS, IIT Madras
Theme: AI in Road Safety

## Live

🌐 **[roadent.in](https://roadent.in)** — try it right now, on your phone or laptop
📹 [Watch the demo video](https://youtu.be/ase0gJIejrA?si=v7IAuoNb6WaYo741)
💻 [github.com/raghazzz/roadent](https://github.com/raghazzz/roadent)

---

## What it does

Roadent is an AI-powered road emergency assistant. It uses your GPS location to instantly surface the nearest hospitals, police stations, ambulances, and towing services — with a single tap, a spoken command, or automatically if you can't respond at all.

- 🆘 **One-tap SOS** — nearest hospitals, ambulances, and police in under 1 second, distance-ranked, tap-to-call
- 🔧 **Vehicle Breakdown mode** — petrol pumps, mechanics, and towing for non-accident distress
- 🚗 **Crash Detection** — Drive Mode monitors the phone's accelerometer and detects impact automatically. A 30-second countdown gives the driver a chance to cancel; if not, a bystander screen takes over — alarm sounding, location announced on a loop, and giant one-tap **Call 108** / **Alert Family** buttons for whoever reaches the phone first
- 🎤 **Voice in, voice out** — speak your emergency, hear the response, no typing or reading required
- 📵 **Installable, offline-capable PWA** — a service worker caches the app and your last-known nearby services, so core features still work with zero internet
- 🌐 **Global fallback via OpenStreetMap** — works in any country automatically when local data is sparse
- 💬 **AI assistant** — multi-turn chat (Mistral AI) for FIR filing, insurance claims, first aid, and general road-safety questions, alongside the emergency lookup
- 🩹 **Offline first-aid cards** — Bleeding, Unconscious, Burns, Fracture — hardcoded, always available
- 📤 **One-tap sharing** — WhatsApp location alert to family, copyable incident report, Google Maps navigation handoff
- 🌙 **Night Drive theme** — dark mode UI designed for low-light, real driving conditions

## Stack

- **Backend:** FastAPI + Python + SQLite
- **AI:** Mistral AI (`mistral-small-latest`)
- **Data:** 2,639 verified emergency services across Tamil Nadu, Rajasthan, Delhi NCR, and Haryana
- **Maps:** Leaflet.js + OpenStreetMap (Overpass API for live global fallback)
- **Voice:** Web Speech API — SpeechRecognition + speechSynthesis, built into the browser, works offline
- **Motion:** DeviceMotion API for crash detection
- **Offline:** Service Worker (PWA) — network-first for API calls, cache-first for the app shell
- **Hosting:** Render, custom domain via GoDaddy, auto-issued HTTPS

## Design philosophy — fail-safe by default

Every dependency has a fallback, so the user always gets *something*:

| If this fails | Roadent falls back to |
|---|---|
| GPS permission denied | Manual location entry, geocoded via OSM Nominatim |
| GPS times out | Manual location entry after 10s |
| Internet drops | Service worker cache of last-known nearby services |
| Backend reachable, AI down | Structured local-database response, no LLM needed |
| Everything down | National emergency numbers (108/100/101/1073) + offline first-aid cards, always on screen |
| Browser can't send a message/call without a tap | A bystander screen puts one-tap **Call 108** and **Alert Family** buttons in front of whoever finds the phone |

There is no scenario where the user sees a blank page.

---

## Running Locally

> Clone or download this repo first, then open a terminal and navigate into the project folder before running any commands below.

You need two terminals running at the same time.

### Prerequisites

- Python 3.11 (required — pydantic-core does not support Python 3.14 yet)
- A Mistral AI API key — get one free at [console.mistral.ai](https://console.mistral.ai)

---

### Mac

**First-time setup**

```bash
# Install Python 3.11 if you don't have it
brew install python@3.11

# Navigate into the project folder (wherever you downloaded/cloned it)
cd path/to/roadent

# Create and set up the virtual environment
/opt/homebrew/bin/python3.11 -m venv venv --without-pip
curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py
./venv/bin/python get-pip.py
./venv/bin/pip install fastapi==0.115.0 "uvicorn[standard]==0.30.6" pydantic==2.7.4 requests==2.32.3 python-multipart==0.0.9
```

**Every time you want to run the app**

Terminal 1 — Backend:

```bash
cd path/to/roadent
source venv/bin/activate
export MISTRAL_API_KEY="your_mistral_api_key_here"
python3 -m uvicorn api:app --reload --port 8000
```

Terminal 2 — Frontend (open a new window with Cmd+N):

```bash
cd path/to/roadent
python3 -m http.server 3000
```

Open Chrome and go to `http://localhost:3000/` then click `static/index.html`.

---

### Windows

**First-time setup**

Download and install Python 3.11 from [python.org/downloads](https://www.python.org/downloads/release/python-3119/) — make sure to check **"Add Python to PATH"** during install.

Then open Command Prompt and run:

```cmd
:: Navigate into the project folder (wherever you downloaded/cloned it)
cd path\to\roadent

py -3.11 -m venv venv
venv\Scripts\python.exe -m pip install fastapi==0.115.0 "uvicorn[standard]==0.30.6" pydantic==2.7.4 requests==2.32.3 python-multipart==0.0.9
```

**Every time you want to run the app**

Terminal 1 — Backend (Command Prompt):

```cmd
cd path\to\roadent
venv\Scripts\activate
set MISTRAL_API_KEY=your_mistral_api_key_here
python -m uvicorn api:app --reload --port 8000
```

Terminal 2 — Frontend (open a new Command Prompt window):

```cmd
cd path\to\roadent
python -m http.server 3000
```

Open Chrome and go to `http://localhost:3000/` then click `static/index.html`.

---

> **Tip:** Replace `path/to/roadent` with the actual path to wherever you put the folder.
> For example: `cd Downloads/roadent` or `cd Documents/roadent`
> If you cloned with git it'll be wherever you ran `git clone`.

Voice input requires Chrome, Edge, or Safari — Firefox does not support the Web Speech API and the mic button hides automatically there. Crash detection requires a device with a motion sensor (a phone); on iOS it will ask for motion-sensor permission the first time Drive Mode is enabled.

---

## Project Structure

```
roadent/
├── api.py              # FastAPI backend — endpoints, search, hardening, Mistral integration
├── project_data.db     # SQLite database — 2,639 emergency services, 4 states
├── requirements.txt    # Python dependencies
├── README.md
└── static/
    ├── index.html      # Frontend — chatbot UI, map, voice, crash detection
    ├── sw.js            # Service worker — offline caching, PWA support
    ├── manifest.json    # PWA manifest
    └── icon.svg         # App icon
```

## Data quality

Hospitals are sourced from the government health facility registry, filtered to only Community Health Centres, District Hospitals, and Sub-district Hospitals — facilities with real emergency capability. Sub-centres and PHCs (no doctor, no 24/7 care) were deliberately excluded. Coordinates were deduplicated on a ~111m grid to remove facilities incorrectly mapped to a shared town centroid. Police, ambulance, petrol pump, and mechanic data is sourced from OpenStreetMap and manually spot-verified for the Chennai/IIT Madras area.

## Security

- Rate limiting (30 req/min per IP) on all `/api/*` endpoints
- Input validation and HTML stripping before any text reaches the AI model
- `POST /api/services` requires an admin token — the database is not publicly writable
- No secrets are hardcoded; `MISTRAL_API_KEY` is read from the environment only
- Global exception handling — no endpoint can leak a raw stack trace

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Serves the app |
| `/api/chat` | POST | Main chatbot — AI reply + nearby services, intent-aware (emergency / breakdown / general) |
| `/api/emergency` | POST | Fast SOS/breakdown lookup — no LLM, near-instant on seeded regions |
| `/api/nearby` | GET | Quick URL param search |
| `/api/report` | POST | Generate a shareable incident report |
| `/api/stats` | GET | Database statistics by type and state |
| `/health` | GET | Health check |

---

**Team:** Raghav Malhotra (Team Developer) · Siddharth Bajaj (Team Member)
Road Safety Hackathon 2026 | CoERS, IIT Madras
