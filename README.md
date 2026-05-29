# Roadent — AI Road Emergency Assistant

Road Safety Hackathon 2026 | CoERS, IIT Madras  
Theme: AI in Road Safety

## What it does
Roadent is an emergency chatbot that instantly surfaces the nearest hospitals, police stations, ambulances, and towing services when a road accident occurs. Features:

- 🆘 One-tap SOS button — results in under 1 second
- 🎤 Voice input + text-to-speech readout (no typing needed in emergencies)  
- 🗺️ Live map with colour-coded service markers
- 📵 Full offline mode — works with zero internet using local SQLite DB
- 🌐 Global fallback via OpenStreetMap for any location worldwide
- 📋 Instant incident report generation
- 💬 Multi-turn AI chat powered by Mistral AI

## Stack
- **Backend:** FastAPI + Python + SQLite
- **AI:** Mistral AI (`mistral-small-latest`)
- **Data:** 900+ emergency services across Rajasthan, Haryana, Delhi NCR
- **Maps:** Leaflet.js + OpenStreetMap
- **Voice:** Web Speech API (built-in browser, works offline)

## Run locally
```bash
pip install -r requirements.txt
export MISTRAL_API_KEY=your_key_here
uvicorn api:app --reload --port 8000
# Open static/index.html in browser
```

## API Endpoints
| Endpoint | Description |
|---|---|
| `POST /api/chat` | Main chatbot — AI reply + nearby services |
| `POST /api/emergency` | Fast SOS lookup — no LLM, <300ms |
| `GET /api/nearby` | Quick URL param search |
| `POST /api/report` | Generate incident report |
| `GET /api/stats` | Database statistics |
| `GET /health` | Health check |
