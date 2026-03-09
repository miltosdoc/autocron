"""
Judge — Full-instrumentation sandbox for AutoCron.

Executes scripts in a cron-like environment and captures EVERYTHING:
- Full stdout/stderr (never truncated for Manager consumption)
- bash xtrace via BASH_XTRACEFD (separated from stderr)
- Environment variables as seen by the script
- Filesystem context (df, permissions on touched paths)
- Command resolution (which binaries exist on PATH)
- Timing data

The Judge's job is observability. It makes no decisions about quality —
that's the Manager's role.
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ExecutionTrace:
    """Complete execution record. Nothing is truncated — the Manager sees all."""

    # Identity
    round_num: int = 0

    # The script
    script_content: str = ""
    script_hash: str = ""

    # Execution environment
    env_vars: dict = field(default_factory=dict)
    working_dir: str = ""
    shell: str = "/bin/bash"
    effective_user: str = ""

    # Raw output — NEVER truncate
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1

    # Bash xtrace — the killer diagnostic
    xtrace: str = ""

    # System context
    disk_free: str = ""
    path_resolution: dict = field(default_factory=dict)
    touched_paths_permissions: dict = field(default_factory=dict)

    # Timing
    duration_seconds: float = 0.0
    timed_out: bool = False

    # Verdict (simple boolean — did the process exit 0?)
    process_succeeded: bool = False

    def summary_for_log(self) -> str:
        """Short summary for the console log."""
        status = "PASS" if self.process_succeeded else "FAIL"
        return (
            f"[{status}] exit={self.exit_code} "
            f"duration={self.duration_seconds:.1f}s "
            f"stderr_lines={len(self.stderr.splitlines())}"
        )

    def full_report(self) -> str:
        """Full structured report for the Manager. Holds nothing back."""
        sections = []

        sections.append("=" * 60)
        sections.append("EXECUTION TRACE")
        sections.append("=" * 60)

        sections.append(f"\n-- SCRIPT ({len(self.script_content)} bytes, hash={self.script_hash}) --")
        sections.append(self.script_content)

        sections.append(f"\n-- ENVIRONMENT --")
        for k, v in sorted(self.env_vars.items()):
            sections.append(f"  {k}={v}")

        sections.append(f"\n-- RESULT --")
        sections.append(f"  Exit code: {self.exit_code}")
        sections.append(f"  Duration: {self.duration_seconds:.2f}s")
        sections.append(f"  Timed out: {self.timed_out}")

        if self.stdout:
            sections.append(f"\n-- STDOUT ({len(self.stdout)} bytes) --")
            sections.append(self.stdout)
        else:
            sections.append(f"\n-- STDOUT: (empty) --")

        if self.stderr:
            sections.append(f"\n-- STDERR ({len(self.stderr)} bytes) --")
            sections.append(self.stderr)
        else:
            sections.append(f"\n-- STDERR: (empty) --")

        if self.xtrace:
            sections.append(f"\n-- BASH XTRACE ({len(self.xtrace)} bytes) --")
            sections.append(self.xtrace)
        else:
            sections.append(f"\n-- BASH XTRACE: (not captured) --")

        if self.disk_free:
            sections.append(f"\n-- DISK (df -h) --")
            sections.append(self.disk_free)

        if self.path_resolution:
            sections.append(f"\n-- COMMAND RESOLUTION --")
            for cmd, path in sorted(self.path_resolution.items()):
                marker = " *** MISSING ***" if path == "NOT FOUND" else ""
                sections.append(f"  {cmd} -> {path}{marker}")

        if self.touched_paths_permissions:
            sections.append(f"\n-- FILE PERMISSIONS --")
            for path, perms in sorted(self.touched_paths_permissions.items()):
                sections.append(f"  {path}: {perms}")

        sections.append("\n" + "=" * 60)
        return "\n".join(sections)


class Judge:
    """Execute scripts in a sandboxed cron-like environment with full tracing."""

    CRON_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    CRON_ENV = {
        "PATH": CRON_PATH,
        "SHELL": "/bin/sh",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }

    COMMON_COMMANDS = [
        "cp", "mv", "rm", "mkdir", "tar", "gzip", "gunzip", "rsync", "find",
        "grep", "awk", "sed", "curl", "wget", "logger", "df", "du",
        "chmod", "chown", "date", "cat", "tee", "head", "tail", "wc",
        "sort", "uniq", "xargs", "basename", "dirname", "mktemp",
        "systemctl", "journalctl", "crontab", "mail", "sendmail",
        "pg_dump", "pg_restore", "mysqldump", "redis-cli",
        "docker", "docker-compose",
    ]

    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    def execute(self, script_content: str, round_num: int = 0) -> ExecutionTrace:
        """
        Execute a script with full instrumentation.
        Returns a complete ExecutionTrace — the Manager sees everything.
        """
        trace = ExecutionTrace(
            round_num=round_num,
            script_content=script_content,
            script_hash=self._hash(script_content),
        )

        work_dir = tempfile.mkdtemp(prefix="autocron_")
        trace_file = os.path.join(work_dir, "xtrace.log")
        script_path = os.path.join(work_dir, "task.sh")

        try:
            # Build environment
            env = dict(self.CRON_ENV)
            env["HOME"] = work_dir
            trace.env_vars = dict(env)
            trace.working_dir = work_dir
            trace.effective_user = os.environ.get("USER", "unknown")

            # Instrument the script with xtrace capture
            instrumented = self._instrument_script(script_content, trace_file)
            with open(script_path, "w") as f:
                f.write(instrumented)
            os.chmod(script_path, 0o755)

            # Pre-execution system context
            trace.disk_free = self._capture_disk()
            trace.path_resolution = self._resolve_commands(env["PATH"], script_content)

            # Execute
            t0 = time.time()
            try:
                result = subprocess.run(
                    ["/bin/bash", script_path],
                    env=env,
                    cwd=work_dir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                trace.duration_seconds = round(time.time() - t0, 3)
                trace.exit_code = result.returncode
                trace.stdout = result.stdout
                trace.stderr = result.stderr
                trace.process_succeeded = result.returncode == 0
                trace.timed_out = False

            except subprocess.TimeoutExpired as e:
                trace.duration_seconds = round(time.time() - t0, 3)
                trace.exit_code = 124
                trace.stdout = (e.stdout or "") if hasattr(e, "stdout") and e.stdout else ""
                trace.stderr = f"TIMEOUT: Script exceeded {self.timeout}s limit"
                trace.process_succeeded = False
                trace.timed_out = True

            # Read xtrace output
            if os.path.exists(trace_file):
                try:
                    with open(trace_file, "r") as f:
                        trace.xtrace = f.read()
                except Exception:
                    trace.xtrace = "[could not read xtrace file]"

            # Post-execution file permissions
            trace.touched_paths_permissions = self._check_touched_paths(
                script_content, work_dir
            )

        except Exception as e:
            trace.exit_code = 126
            trace.stderr = f"Judge internal error: {e}"
            trace.process_succeeded = False
        finally:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass

        return trace

    def _instrument_script(self, script: str, trace_file: str) -> str:
        """
        Inject xtrace capture via BASH_XTRACEFD.
        Sends set -x output to fd 7 → file, keeping stderr clean.
        """
        lines = script.split("\n")

        if lines and lines[0].startswith("#!"):
            shebang = lines[0]
            body = "\n".join(lines[1:])
        else:
            shebang = "#!/bin/bash"
            body = script

        # Use PS4 to add timestamps and line numbers to xtrace
        instrumentation = f"""\
# --- AutoCron instrumentation (injected by Judge) ---
exec 7>{trace_file}
export BASH_XTRACEFD=7
export PS4='+[${{SECONDS}}s] ${{BASH_SOURCE}}:${{LINENO}}: '
set -x
# --- End instrumentation ---
"""
        return f"{shebang}\n{instrumentation}\n{body}"

    def _resolve_commands(self, path: str, script_content: str) -> dict:
        """Check which commands the script uses and whether they exist on PATH."""
        resolution = {}
        path_dirs = path.split(":")

        # Commands from known list that appear in script
        script_commands = set()
        for cmd in self.COMMON_COMMANDS:
            if re.search(rf"\b{cmd}\b", script_content):
                script_commands.add(cmd)

        # Also extract leading commands from lines
        for line in script_content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Skip control flow, assignments, redirections
            tokens = line.split()
            if not tokens:
                continue
            first = tokens[0].rstrip(";")
            if first in ("if", "then", "else", "elif", "fi", "for", "while",
                         "do", "done", "case", "esac", "{", "}", "[[", "]]",
                         "set", "export", "local", "declare", "readonly",
                         "source", ".", "eval", "exec", "return", "exit"):
                continue
            if "=" in first:
                continue
            if first.startswith(("$", "(", "[", "!", ">", "<", "|", "&")):
                continue
            clean = os.path.basename(first)
            if clean:
                script_commands.add(clean)

        for cmd in sorted(script_commands):
            found = False
            for d in path_dirs:
                full = os.path.join(d, cmd)
                if os.path.isfile(full) and os.access(full, os.X_OK):
                    resolution[cmd] = full
                    found = True
                    break
            if not found:
                resolution[cmd] = "NOT FOUND"

        return resolution

    def _capture_disk(self) -> str:
        try:
            result = subprocess.run(
                ["df", "-h"], capture_output=True, text=True, timeout=5
            )
            return result.stdout
        except Exception:
            return "[could not capture df output]"

    def _check_touched_paths(self, script: str, work_dir: str) -> dict:
        """Extract absolute paths from script and check permissions."""
        perms = {}
        paths = re.findall(r'(?:^|[\s"\'=>])(/[\w/._-]+)', script)
        paths.append(work_dir)

        for p in set(paths):
            try:
                if os.path.exists(p):
                    result = subprocess.run(
                        ["ls", "-la", p], capture_output=True, text=True, timeout=3
                    )
                    perms[p] = result.stdout.strip()
                else:
                    perms[p] = "DOES NOT EXIST"
            except Exception:
                perms[p] = "[check failed]"

        return perms

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:12]
