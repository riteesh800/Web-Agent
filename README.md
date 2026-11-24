# 🌐 Web Agent — Autonomous Browser & AI Assistant

A full-stack web application that combines **autonomous browser automation** (via Microsoft Edge) with a **multi-provider AI chatbot**. The browser agent navigates websites, performs research tasks, and returns structured results — which can then be discussed with the built-in AI assistant.

## Features

- **Browser Automation** — Uses the `browser-use` library to control Microsoft Edge for web research, data extraction, and task execution.
- **Multi-Provider AI Failover** — Automatically falls back between OpenRouter → Gemini → Groq if any provider hits rate limits or errors.
- **AI Chatbot** — Conversational assistant powered by OpenRouter with Gemini & Groq fallback, capable of discussing browser research results.
- **Live Streaming Logs** — Real-time SSE-based terminal showing browser agent activity.
- **API Key Management** — Add/swap provider keys directly from the UI without restarting.
- **Dark-Themed UI** — Clean, responsive frontend with quick-task presets.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, Starlette, Uvicorn |
| Browser Engine | browser-use, Microsoft Edge |
| AI Providers | OpenRouter, Google Gemini, Groq, OpenAI, Anthropic |
| Frontend | HTML, CSS, JavaScript |
| Deployment | Docker, Docker Compose |

## Project Structure

```
├── bootstrap.py              # Entry point
├── dependencies.txt           # Python dependencies
├── .env                      # API keys (gitignored)
├── agent_core/
│   ├── navigation_driver.py   # Browser session management
│   ├── provider_failover.py   # Multi-LLM failover logic
│   └── stream_collector.py    # Log streaming handler
├── backend/
│   └── gateway.py             # Starlette API server & routes
├── core_config/
│   └── environment_loader.py  # Config & secret management
├── frontend/
│   ├── index.html             # Main UI
│   ├── styles/theme.css       # Dark theme stylesheet
│   └── scripts/orchestrator_ui.js  # Frontend logic
├── deployment/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── launch_orchestrator.bat
└── qa_suite/                  # Unit tests
    ├── test_settings.py
    ├── test_fault_detection.py
    ├── test_failover_engine.py
    └── test_http_gateway.py
```

## Quick Start

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   playwright install
   ```

2. **Configure API keys** — Add your keys to `agent_secrets.env`:
   ```env
   OPENROUTER_API_KEY=sk-or-v1-...
   GOOGLE_API_KEY=your-gemini-key
   GROQ_API_KEY=gsk_...
   ```

3. **Run the app**
   ```bash
   python bootstrap.py
   ```
   Opens automatically at `http://127.0.0.1:5000`

## Docker

```bash
cd deployment
docker-compose up --build
```

## Running Tests

```bash
python -m qa_suite.run_verification
```

## License

This project is for educational and personal use.
