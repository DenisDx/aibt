# aibt — Multi-Agent AI Platform

A modular, extensible platform for running multi-agent AI systems built on LangChain and LangGraph.

## Features

- **Multi-agent orchestration** via LangGraph with routing, retries, and fallback
- **Multiple interfaces** — Web UI, Telegram bot, scheduled cron tasks
- **Extensible adapter architecture** — add Slack, Discord, webhooks, etc. in one file
- **Agent memory** — short-term (LangGraph checkpointer), long-term namespaces (LangGraph Store), background memory management (LangMem)
- **Hybrid RAG** — document store + dense/sparse retrieval (planned)
- **External tools** via MCP servers (file system, shell sandbox, web search, etc.)
- **Web UI** — dashboard, live log stream, agent chat, LangGraph management

## Requirements

- Python 3.11+
- Docker (for PostgreSQL)
- A systemd-based Linux system (for service management)
- An OpenAI-compatible LLM API endpoint and key

## Installation

```bash
# 1. Clone the repository
git clone <repo-url> aibt
cd aibt

# 2. Install system prerequisites (Python, Docker, etc.)
sudo ./install_prerequisites.sh

# 3. Install the application (creates venv, .env, config, systemd service, cron)
./install.sh
```

`install.sh` on first run:
- Creates `.env` from `.env.example`
- Sets `AIBT_ROOT` to the current directory
- Creates a Python virtualenv and installs all dependencies from `requirements.txt`
- Copies `config.json5.example` → `config.json5`
- Sets up a systemd user service (`aibt`) that auto-restarts on failure
- Registers `src/core/cron.py` in the user's crontab (runs every minute)

## Configuration

Edit `.env` for secrets and machine-specific values (never committed to git):

| Variable | Description |
|---|---|
| `LLM_BASE_URL` | OpenAI-compatible API base URL |
| `LLM_API_KEY` | API key for the LLM provider |
| `LLM_MODEL` | Model name (e.g. `gpt-4o-mini`) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `ROOT_USER` / `ROOT_PASSWORD` | Web UI admin credentials |
| `WEBUI_PORT` | Web UI port (default: `50080`) |

See `.env.example` for the full list.

Edit `config.json5` for application settings (agents, adapters, logging, etc.).  
See `config.json5.example` for all available options with comments.

### Enable Telegram bot

In `config.json5`:

```json5
"adapters": {
  "items": {
    "telegram": {
      "enabled": true,
      "token": "${TELEGRAM_BOT_TOKEN}",
      "default_agent": "chat_group_helper",
      "polling": true
    }
  }
}
```

### Schedule cron tasks

In `config.json5`:

```json5
"cron": {
  "enabled": true,
  "tasks": [
    {
      "name": "daily_task",
      "enabled": true,
      "agent": "math",
      "schedule": "0 9 * * *",
      "message": "Run daily summary"
    }
  ]
}
```

## Project Structure

```
src/
  core/          # App entry point, cron runner, config loader, doctor
  agents/        # Per-agent implementations (echo, math, graph_echo, ...)
  orchestrator/  # LangGraph-based multi-agent orchestrator
  adapters/      # Integration adapters (webui, telegram, cron, ...)
webui/
  backend/       # FastAPI server
  frontend/      # Web UI (vanilla JS SPA)
```

## Running

```bash
# Check service status
systemctl --user status aibt

# Start / restart
systemctl --user restart aibt

# Web UI (default port 50080)
# Open http://localhost:50080 in your browser

# Run doctor to validate setup
./venv/bin/python src/core/doctor.py
./venv/bin/python src/core/doctor.py --fix   # auto-fix issues

# Run memoryd smoke test through the memoryd envid
./venv/bin/python scripts/memoryd_smoke.py --envid envid-telegram-bot2-memoryd --muid smoke
```

## Uninstall

```bash
./uninstall.sh
```
