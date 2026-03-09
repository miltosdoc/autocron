---
name: autocron
description: >
  Autonomous cron job creation, testing, and deployment from natural language.
  Uses a dual-LLM architecture (any provider): one model writes scripts, another
  reviews every execution in a sandboxed cron environment with full xtrace
  instrumentation. Knowledge accumulates across runs — the system gets better
  over time. Say "automate [task]" to create a production-tested cron job.
metadata:
  copaw:
    emoji: "🤖"
    requires: {}
---

# AutoCron — Autonomous Cron Job Agent

AutoCron turns natural language into **production-tested cron jobs**. It uses a dual-LLM feedback loop inspired by [autoresearch](https://github.com/karpathy/autoresearch): one model writes scripts, another reviews every execution with full bash xtrace analysis.

## Commands

- **"automate [description]"** — Generate, test, and deploy a cron job from a natural language description. AutoCron writes the script, tests it in a sandboxed cron environment, gets it reviewed, and deploys when approved.

- **"autocron status"** — Show deployed scripts, knowledge store stats, and recent git history.

- **"autocron health"** — Run platform diagnostics: failure rates, detected patterns, proposed corrections.

- **"autocron self-correct"** — Apply accumulated corrections to improve platform behavior.

- **"autocron history"** — Show git log of all deployments with diffs.

## Examples

> "Automate checking my clinic schedule at regionhalland.se every weekday at 7am and message me"

> "Automate monitoring PubMed for MS research weekly and maintain a library of relevant articles"

> "Automate a nightly database backup with 30-day rotation"

> "Create cron jobs for our quarterly board meetings, annual financial audit, and monthly team reviews"

## How It Works

1. You describe what you want automated
2. AutoCron's **Router** checks if a similar task was solved before (deploy/adapt/full loop)
3. The **Worker** model generates a bash script
4. The **Judge** executes it in a sandboxed cron environment (minimal PATH, no HOME, full xtrace)
5. The **Manager** model reviews the complete execution trace
6. If issues found → Worker fixes → repeat. If approved → deploy to crontab
7. A **Knowledge Store** accumulates lessons across all runs
8. Everything is **git-versioned** for history and rollback

## Configuration

AutoCron uses two LLM roles that can be configured independently:

| Role | Purpose | Configure in CoPaw |
|------|---------|-------------------|
| **Worker** | Writes scripts | Any local or cloud model |
| **Manager** | Reviews executions | Recommended: Claude, GPT-4, or Qwen3 |

Models are configured through CoPaw's provider system — use the Console UI to set up providers (Ollama, OpenAI, Anthropic, llama.cpp, MLX, etc.) and AutoCron will use whichever models you select.

## Files

```
SKILL.md            — This file (skill manifest)
autocron/           — Python package with all components
  main.py           — Orchestration loop
  llm_backend.py    — Multi-provider LLM calls
  judge.py          — Sandboxed cron execution
  knowledge.py      — Typed knowledge store
  convergence.py    — Stopping conditions
  router.py         — Task routing
  creator.py        — Session capture → task.md
  copaw_skill.py    — CoPaw integration hooks
  git_manager.py    — Git version control
scripts/            — Helper scripts
examples/           — Example task.md files
```
