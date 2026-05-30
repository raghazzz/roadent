# Roadent — AI Road Emergency Assistant

Road Safety Hackathon 2026 | CoERS, IIT Madras  
Theme: AI in Road Safety

## Live Demo

📹 [Watch the demo video](https://your-youtube-link-here)

---

## What it does
Roadent is an emergency chatbot that instantly surfaces the nearest hospitals, police stations, ambulances, and towing services when a road accident occurs.

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

---

## Running Locally

You need two terminals running at the same time.

### Prerequisites
- Python 3.11 (required — pydantic-core does not support Python 3.14 yet)
- A Mistral AI API key — get one free at [console.mistral.ai](https://console.mistral.ai)

---

### Mac

**First-time setup**

```bash
brew install python@3.11
cd ~/Desktop/roadent
/opt/homebrew/bin/python3.11 -m venv venv --without-pip
curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py
./venv/bin/python get-pip.py
./venv/bin/pip install fastapi==0.115.0 "uvicorn[standard]==0.30.6" pydantic==2.7.4 requests==2.32.3 python-multipart==0.0.9
```

**Every time you want to run the app**

Terminal 1 — Backend:

```bash
cd ~/Desktop/roadent
source venv/bin/activate
export MISTRAL_API_KEY="your_mistral_api_key_here"
python3 -m uvicorn api:app --reload --port 8000
```

Terminal 2 — Frontend (open a new window with Cmd+N):

```bash
cd ~/Desktop/roadent
python3 -m http.server 3000
```

Open Chrome and go to `http://localhost:3000/` then click `static/index.html`.

---

### Windows

**First-time setup**

Download and install Python 3.11 from [python.org/downloads](https://www.python.org/downloads/release/python-3119/) — make sure to check **"Add Python to PATH"** during install.

Then open Command Prompt and run:

```cmd
cd %USERPROFILE%\Desktop\roadent
py -3.11 -m venv venv
venv\Scripts\python.exe -m pip install fastapi==0.115.0 "uvicorn[standard]==0.30.6" pydantic==2.7.4 requests==2.32.3 python-multipart==0.0.9
```

**Every time you want to run the app**

Terminal 1 — Backend (Command Prompt):

```cmd
cd %USERPROFILE%\Desktop\roadent
venv\Scripts\activate
set MISTRAL_API_KEY=your_mistral_api_key_here
python -m uvicorn api:app --reload --port 8000
```

Terminal 2 — Frontend (open a new Command Prompt window):

```cmd
cd %USERPROFILE%\Desktop\roadent
python -m http.server 3000
```

Open Chrome and go to `http://localhost:3000/` then click `static/index.html`.

---

## Project Structure

```
roadent/
├── api.py              # FastAPI backend — all endpoints and AI logic
├── project_data.db     # SQLite database — 900+ emergency services
├── requirements.txt    # Python dependencies
├── README.md
└── static/
    └── index.html      # Frontend — chatbot UI with map and voice
```

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/chat` | POST | Main chatbot — AI reply + nearby services |
| `/api/emergency` | POST | Fast SOS lookup — no LLM, <300ms |
| `/api/nearby` | GET | Quick URL param search |
| `/api/report` | POST | Generate incident report |
| `/api/stats` | GET | Database statistics |
| `/health` | GET | Health check |
