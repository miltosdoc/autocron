# 🤖 AutoCron Agent

**Autonomous cron job creation, testing, and deployment from natural language.**

AutoCron turns natural language into production-tested cron jobs using a dual-LLM feedback loop. Inspired by [autoresearch](https://github.com/karpathy/autoresearch), one model writes scripts while another reviews every execution with full bash xtrace analysis. Knowledge accumulates across runs — the system gets better over time.

Works as a [CoPaw](https://github.com/copaw/copaw) skill (Telegram, Discord, etc.) or standalone CLI.

## Architecture

```
User → "automate nightly database backup with 30-day rotation"
                    ↓
              ┌─────────────┐
              │   Router     │ ← Checks knowledge store for similar solved tasks
              └──────┬───────┘
                     ↓
              ┌─────────────┐
              │   Worker     │ ← LLM #1 writes bash script (local or cloud)
              └──────┬──────┘
                     ↓
              ┌─────────────┐
              │   Judge      │ ← Executes in cron sandbox (minimal PATH, xtrace)
              └──────┬──────┘
                     ↓
              ┌─────────────┐
              │   Manager    │ ← LLM #2 reviews trace, diagnoses, extracts lesson
              └──────┬──────┘
                     ↓
              ┌─────────────────┐
              │ Convergence     │ ← Stop if approved / cosmetic only / saturated
              └──────┬──────────┘
                     ↓
         Deploy to crontab + git commit
```

## Quick Start

### As CoPaw Skill (recommended)

**One-liner:**
```bash
pip install copaw git+https://github.com/miltosdoc/autocron.git && autocron install
```

That's it. `autocron install` copies the skill into CoPaw's `~/.copaw/customized_skills/autocron/` automatically.

Then start CoPaw and chat:
```bash
copaw app
```

### Standalone CLI

```bash
pip install .
autocron examples/backup_task.md
```

## LLM Configuration

Each role (Worker, Manager) uses a different LLM, independently configurable:

| Role | Purpose | Typical choice |
|------|---------|---------------|
| **Worker** | Generates/fixes scripts | Local: Ollama, llama.cpp, MLX, LMStudio |
| **Manager** | Reviews every execution | Cloud: Anthropic, OpenAI, DashScope |

### CoPaw Mode

Configure models in CoPaw's web console — AutoCron uses CoPaw's provider system
which supports OpenAI, Anthropic, Ollama, llama.cpp, MLX, DashScope, Azure OpenAI,
and custom providers.

### Standalone Mode

```bash
# Environment variables
export AUTOCRON_WORKER_URL=http://localhost:11434
export AUTOCRON_WORKER_MODEL=qwen3:27b
export AUTOCRON_MANAGER_URL=https://api.anthropic.com
export AUTOCRON_MANAGER_MODEL=claude-sonnet-4-20250514
export AUTOCRON_MANAGER_API_KEY=sk-ant-...

# Or CLI flags
autocron examples/backup_task.md \
  --worker-url http://localhost:11434 \
  --worker-model qwen3:27b \
  --manager-url https://api.openai.com \
  --manager-model gpt-4.1 \
  --manager-api-key sk-...
```

**Example combos:**
```
Worker: ollama / qwen3:27b     + Manager: anthropic / claude-sonnet    (hybrid)
Worker: llamacpp / deepseek-r1 + Manager: openai / gpt-4.1            (hybrid)
Worker: openai / gpt-4o-mini   + Manager: openai / o3                 (cloud-only)
Worker: ollama / llama3:8b     + Manager: ollama / qwen3:72b          (fully local)
```

## Example Tasks

| Task | File | Schedule |
|------|------|----------|
| Database backup with rotation | `examples/backup_task.md` | Daily 3 AM |
| Clinic schedule check | `examples/clinic_schedule_task.md` | Weekdays 6:30 AM |
| MS research monitoring | `examples/research_monitor_task.md` | Weekly Monday 8 AM |
| Board meeting reminders | `examples/board_meeting_task.md` | Weekdays 8 AM |
| Disk usage alerts | `examples/disk_monitor_task.md` | Every 6 hours |

## Key Features

- **Dual-LLM feedback loop** — Worker writes, Manager reviews, knowledge accumulates
- **Sandboxed cron execution** — Scripts tested in real cron environment (minimal PATH, no HOME, full xtrace)
- **Knowledge store** — Typed lessons (prose, command, snippet) persist across runs
- **Git version control** — Every deployment auto-committed, tagged, and optionally pushed
- **Smart routing** — Similar tasks reuse existing solutions, adapted when needed
- **Convergence detection** — Stops when approved, cosmetic-only, or knowledge-saturated
- **Multi-provider LLM** — Any provider, any model, per-role configuration
- **CoPaw integration** — Telegram, Discord, and more via CoPaw's channel system

## Project Structure

```
autocron-agent/
├── SKILL.md                    # CoPaw skill manifest
├── pyproject.toml              # Python package config
├── autocron/                   # Core package
│   ├── main.py                 # Orchestration loop
│   ├── llm_backend.py          # Multi-provider LLM calls
│   ├── judge.py                # Sandboxed cron execution
│   ├── knowledge.py            # Typed knowledge store
│   ├── convergence.py          # Stopping conditions
│   ├── router.py               # Task routing
│   ├── creator.py              # Session capture → task.md
│   ├── copaw_skill.py          # CoPaw integration
│   └── git_manager.py          # Git version control
├── examples/                   # Example task files
├── scripts/                    # Helper scripts
└── tests/                      # Unit tests
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## License

MIT
