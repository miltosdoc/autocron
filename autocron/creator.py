#!/usr/bin/env python3
"""
AutoCron Creator — From observed manual task to automated loop.

Captures a manual shell session (commands, files touched, env, timing)
and generates a structured task.md via a single Manager-class LLM call.

Usage:
  # Start capture
  autocron-creator start [--session-name backup_db]

  # ... do your manual work in the spawned shell ...
  pg_dump meditalks_prod | gzip > /home/deploy/backup.sql.gz
  find /home/deploy/backups -mtime +14 -delete

  # Stop capture and generate task.md
  exit   # or: autocron-creator stop

  # Or capture from an existing script
  autocron-creator from-script /path/to/manual_backup.sh

  # Or capture from shell history
  autocron-creator from-history --last 10
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


# ---------------------------------------------------------------------------
# Session Capture
# ---------------------------------------------------------------------------

@dataclass
class CapturedCommand:
    command: str
    timestamp: float
    exit_code: int = 0
    duration: float = 0.0

@dataclass
class CapturedSession:
    session_name: str
    commands: list[CapturedCommand] = field(default_factory=list)
    env_snapshot: dict = field(default_factory=dict)
    working_dir: str = ""
    user: str = ""
    hostname: str = ""
    start_time: str = ""
    end_time: str = ""
    files_modified: list[str] = field(default_factory=list)
    script_source: Optional[str] = None  # if from-script mode

    def to_analysis_text(self) -> str:
        """Format session for LLM analysis."""
        lines = [
            f"Session: {self.session_name}",
            f"User: {self.user}@{self.hostname}",
            f"Working directory: {self.working_dir}",
            f"Started: {self.start_time}",
            f"Ended: {self.end_time}",
        ]

        if self.script_source:
            lines.append(f"\nSource script:\n```bash\n{self.script_source}\n```")
        else:
            lines.append(f"\nCommands executed ({len(self.commands)}):")
            for cmd in self.commands:
                exit_tag = "" if cmd.exit_code == 0 else f" [EXIT {cmd.exit_code}]"
                dur_tag = f" ({cmd.duration:.1f}s)" if cmd.duration > 0.5 else ""
                lines.append(f"  $ {cmd.command}{exit_tag}{dur_tag}")

        if self.files_modified:
            lines.append(f"\nFiles modified ({len(self.files_modified)}):")
            for f in self.files_modified[:20]:
                lines.append(f"  {f}")

        # Relevant env vars (filter noise)
        relevant_env = {
            k: v for k, v in self.env_snapshot.items()
            if k in ("PATH", "HOME", "USER", "SHELL", "PGHOST", "PGPORT",
                      "PGUSER", "PGDATABASE", "DOCKER_HOST", "REDIS_URL",
                      "DATABASE_URL", "BACKUP_DIR", "LOG_DIR")
            or k.startswith(("AUTOCRON_", "APP_", "DB_"))
        }
        if relevant_env:
            lines.append("\nRelevant environment:")
            for k, v in sorted(relevant_env.items()):
                lines.append(f"  {k}={v}")

        return "\n".join(lines)


class SessionCapture:
    """Capture shell sessions using script(1) + bash PROMPT_COMMAND."""

    CAPTURE_DIR = Path.home() / ".autocron" / "captures"

    def __init__(self, session_name: str = ""):
        self.session_name = session_name or datetime.now().strftime("session_%Y%m%d_%H%M%S")
        self.capture_dir = self.CAPTURE_DIR / self.session_name
        self.capture_dir.mkdir(parents=True, exist_ok=True)

    def start_interactive(self) -> CapturedSession:
        """
        Start an interactive capture session.
        Spawns a new bash shell with instrumentation.
        Returns CapturedSession when the shell exits.
        """
        typescript_file = self.capture_dir / "typescript"
        timing_file = self.capture_dir / "timing"
        history_file = self.capture_dir / "commands.log"
        snapshot_before = self._snapshot_files()

        # Bash init that logs every command with timestamp and exit code
        bashrc_content = f"""\
# AutoCron capture instrumentation
export AUTOCRON_CAPTURE=1
export AUTOCRON_HISTORY="{history_file}"
export PROMPT_COMMAND='echo "$(date +%s.%N) $? $(history 1 | sed "s/^[ ]*[0-9]\\+[ ]*//")" >> "{history_file}"'
echo "🔴 AutoCron capture active: {self.session_name}"
echo "   Commands logged to: {history_file}"
echo "   Type 'exit' when done."
"""
        bashrc_path = self.capture_dir / ".bashrc_capture"
        bashrc_path.write_text(bashrc_content)

        # Capture env before
        env_snapshot = dict(os.environ)

        start_time = datetime.now().isoformat()
        print(f"Starting capture session: {self.session_name}")
        print(f"Capture dir: {self.capture_dir}")

        # Use script(1) for full terminal recording + custom bash
        subprocess.run([
            "script",
            "--timing=" + str(timing_file),
            "-q",  # quiet
            str(typescript_file),
            "-c", f"bash --rcfile {bashrc_path} -i",
        ])

        end_time = datetime.now().isoformat()
        snapshot_after = self._snapshot_files()

        # Parse captured commands
        commands = self._parse_history(history_file)

        # Detect modified files
        files_modified = self._diff_snapshots(snapshot_before, snapshot_after)

        return CapturedSession(
            session_name=self.session_name,
            commands=commands,
            env_snapshot=env_snapshot,
            working_dir=os.getcwd(),
            user=os.environ.get("USER", "unknown"),
            hostname=os.uname().nodename,
            start_time=start_time,
            end_time=end_time,
            files_modified=files_modified,
        )

    def from_script(self, script_path: str) -> CapturedSession:
        """Generate a session from an existing bash script."""
        path = Path(script_path)
        if not path.exists():
            print(f"Error: {script_path} not found", file=sys.stderr)
            sys.exit(1)

        content = path.read_text()

        # Extract commands (strip comments, shebangs, empty lines)
        commands = []
        for line in content.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("set "):
                continue
            commands.append(CapturedCommand(
                command=stripped,
                timestamp=time.time(),
            ))

        return CapturedSession(
            session_name=path.stem,
            commands=commands,
            env_snapshot=dict(os.environ),
            working_dir=os.getcwd(),
            user=os.environ.get("USER", "unknown"),
            hostname=os.uname().nodename,
            start_time=datetime.now().isoformat(),
            end_time=datetime.now().isoformat(),
            script_source=content,
        )

    def from_history(self, last_n: int = 10) -> CapturedSession:
        """Generate a session from recent shell history."""
        # Try to read bash history
        histfile = os.environ.get("HISTFILE", str(Path.home() / ".bash_history"))
        try:
            lines = Path(histfile).read_text().strip().split("\n")
            recent = lines[-last_n:] if len(lines) >= last_n else lines
        except FileNotFoundError:
            print(f"History file not found: {histfile}", file=sys.stderr)
            sys.exit(1)

        commands = [
            CapturedCommand(command=cmd.strip(), timestamp=time.time())
            for cmd in recent if cmd.strip() and not cmd.startswith("#")
        ]

        return CapturedSession(
            session_name=f"history_last_{last_n}",
            commands=commands,
            env_snapshot=dict(os.environ),
            working_dir=os.getcwd(),
            user=os.environ.get("USER", "unknown"),
            hostname=os.uname().nodename,
            start_time=datetime.now().isoformat(),
            end_time=datetime.now().isoformat(),
        )

    def _parse_history(self, history_file: Path) -> list[CapturedCommand]:
        """Parse the instrumented command log."""
        commands = []
        if not history_file.exists():
            return commands

        prev_timestamp = None
        for line in history_file.read_text().strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            try:
                ts = float(parts[0])
                exit_code = int(parts[1])
                cmd = parts[2]
            except (ValueError, IndexError):
                continue

            duration = (ts - prev_timestamp) if prev_timestamp else 0.0
            prev_timestamp = ts

            commands.append(CapturedCommand(
                command=cmd,
                timestamp=ts,
                exit_code=exit_code,
                duration=max(0, duration),
            ))

        return commands

    def _snapshot_files(self) -> dict[str, float]:
        """Snapshot mtime of common directories for change detection."""
        snapshot = {}
        watch_dirs = ["/tmp", "/var/log", str(Path.home())]
        for d in watch_dirs:
            try:
                for root, dirs, files in os.walk(d):
                    # Limit depth
                    depth = root[len(d):].count(os.sep)
                    if depth > 2:
                        dirs.clear()
                        continue
                    for f in files[:100]:
                        fp = os.path.join(root, f)
                        try:
                            snapshot[fp] = os.path.getmtime(fp)
                        except OSError:
                            pass
            except OSError:
                pass
        return snapshot

    def _diff_snapshots(
        self, before: dict[str, float], after: dict[str, float]
    ) -> list[str]:
        """Find files that were created or modified."""
        modified = []
        for path, mtime in after.items():
            if path not in before or before[path] < mtime:
                modified.append(path)
        return sorted(modified)[:50]


# ---------------------------------------------------------------------------
# Task Generator — LLM call to produce task.md
# ---------------------------------------------------------------------------

GENERATOR_SYSTEM = """\
You are a senior systems engineer. You are given a captured shell session
showing manual work done by an operator. Your job is to produce a structured
task.md file that an autonomous system (AutoCron) can use to recreate this
work as an automated cron job.

Analyze the session and infer:
1. The HIGH-LEVEL GOAL (not just "run these commands")
2. Requirements and constraints
3. Edge cases the operator may not have considered
4. A suggested cron schedule based on the nature of the task
5. The execution environment

RESPONSE FORMAT — reply with ONLY a JSON object:
{
  "title": "Short descriptive title",
  "goal": "One paragraph describing the high-level goal",
  "requirements": ["Requirement 1", "Requirement 2", ...],
  "edge_cases": ["Edge case 1", "Edge case 2", ...],
  "suggested_schedule": "cron expression (e.g., 0 3 * * *)",
  "schedule_reasoning": "Why this schedule",
  "environment": {
    "os": "Ubuntu 24.04",
    "user": "deploy",
    "dependencies": ["pg_dump", "gzip", "find"],
    "notes": "Any environment-specific notes"
  }
}
"""

GENERATOR_PROMPT = """\
Here is the captured session:

{session_text}

Analyze this and produce a task.md specification for autonomous automation.
Reply with ONLY the JSON object.
"""


class TaskGenerator:
    """Generate task.md from a captured session using a Manager-class LLM."""

    def __init__(
        self,
        provider: str = "anthropic",
        model: str = "claude-sonnet-4-20250514",
    ):
        self.provider = provider
        self.model = model
        self.client = httpx.Client(timeout=120)

    def generate(self, session: CapturedSession) -> str:
        """
        Analyze a captured session and produce task.md content.
        Returns the full markdown text ready to write to a file.
        """
        session_text = session.to_analysis_text()
        prompt = GENERATOR_PROMPT.format(session_text=session_text)

        raw = self._call_llm(GENERATOR_SYSTEM, prompt)
        spec = self._parse_response(raw)

        return self._format_task_md(spec, session)

    def _call_llm(self, system: str, prompt: str) -> str:
        if self.provider == "anthropic":
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                return json.dumps({"error": "ANTHROPIC_API_KEY not set"})
            try:
                resp = self.client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model, "max_tokens": 2000,
                        "system": system,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                return resp.json()["content"][0]["text"]
            except Exception as e:
                return json.dumps({"error": str(e)})
        elif self.provider == "openai":
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                return json.dumps({"error": "OPENAI_API_KEY not set"})
            try:
                resp = self.client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model, "max_tokens": 2000,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except Exception as e:
                return json.dumps({"error": str(e)})
        return json.dumps({"error": f"Unknown provider: {self.provider}"})

    def _parse_response(self, raw: str) -> dict:
        cleaned = re.sub(r"```(?:json)?\s*", "", raw)
        cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\"goal\".*\}", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return {"error": f"Failed to parse LLM response", "raw": raw[:500]}

    def _format_task_md(self, spec: dict, session: CapturedSession) -> str:
        """Format the LLM output as a clean task.md."""
        if "error" in spec:
            return f"# AutoCron Task\n\n## Error\n\n{spec['error']}\n"

        lines = [f"# AutoCron Task: {spec.get('title', session.session_name)}"]
        lines.append("")

        lines.append("## Goal")
        lines.append(spec.get("goal", "No goal inferred."))
        lines.append("")

        reqs = spec.get("requirements", [])
        if reqs:
            lines.append("## Requirements")
            for r in reqs:
                lines.append(f"- {r}")
            lines.append("")

        edges = spec.get("edge_cases", [])
        if edges:
            lines.append("## Edge Cases to Handle")
            for e in edges:
                lines.append(f"- {e}")
            lines.append("")

        schedule = spec.get("suggested_schedule", "")
        if schedule:
            lines.append("## Schedule")
            lines.append(f"Cron expression: `{schedule}`")
            reasoning = spec.get("schedule_reasoning", "")
            if reasoning:
                lines.append(f"Reasoning: {reasoning}")
            lines.append("")

        env = spec.get("environment", {})
        if env:
            lines.append("## Environment")
            if env.get("os"):
                lines.append(f"- OS: {env['os']}")
            if env.get("user"):
                lines.append(f"- User: {env['user']}")
            deps = env.get("dependencies", [])
            if deps:
                lines.append(f"- Dependencies: {', '.join(deps)}")
            notes = env.get("notes")
            if notes:
                lines.append(f"- Notes: {notes}")
            lines.append("")

        # Append source provenance
        lines.append("## Provenance")
        lines.append(f"Generated by AutoCron Creator from session: {session.session_name}")
        lines.append(f"Captured: {session.start_time}")
        lines.append(f"Host: {session.user}@{session.hostname}")
        lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="AutoCron Creator — capture manual work and generate task.md"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # start: interactive capture
    p_start = sub.add_parser("start", help="Start interactive capture session")
    p_start.add_argument("--session-name", default="", help="Name for this session")
    p_start.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"])
    p_start.add_argument("--model", default="claude-sonnet-4-20250514")
    p_start.add_argument("--output", "-o", default="", help="Output path for task.md")

    # from-script: generate from existing script
    p_script = sub.add_parser("from-script", help="Generate task.md from existing script")
    p_script.add_argument("script_path", help="Path to bash script")
    p_script.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"])
    p_script.add_argument("--model", default="claude-sonnet-4-20250514")
    p_script.add_argument("--output", "-o", default="", help="Output path for task.md")

    # from-history: generate from shell history
    p_hist = sub.add_parser("from-history", help="Generate task.md from shell history")
    p_hist.add_argument("--last", type=int, default=10, help="Number of recent commands")
    p_hist.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"])
    p_hist.add_argument("--model", default="claude-sonnet-4-20250514")
    p_hist.add_argument("--output", "-o", default="", help="Output path for task.md")

    args = parser.parse_args()

    capture = SessionCapture(session_name=getattr(args, "session_name", ""))

    if args.command == "start":
        session = capture.start_interactive()
    elif args.command == "from-script":
        session = capture.from_script(args.script_path)
    elif args.command == "from-history":
        session = capture.from_history(last_n=args.last)
    else:
        parser.print_help()
        sys.exit(1)

    print(f"\nCaptured {len(session.commands)} commands")
    print("Generating task.md...")

    generator = TaskGenerator(provider=args.provider, model=args.model)
    task_md = generator.generate(session)

    # Output
    output_path = args.output or f"{session.session_name}_task.md"
    Path(output_path).write_text(task_md)
    print(f"Task specification written to: {output_path}")
    print("\nGenerated task.md:")
    print("-" * 40)
    print(task_md)


if __name__ == "__main__":
    main()
