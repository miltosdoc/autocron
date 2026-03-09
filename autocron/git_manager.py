"""
Git Manager — Version control for AutoCron scripts and knowledge.

Tracks deployed scripts, knowledge store changes, and task history
using local git. Optionally syncs to a remote (GitHub, etc).

No Python git libraries — just subprocess calls to `git`.
"""

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("autocron.git")


class GitManager:
    """
    Manage a git repository for AutoCron's persistent state.

    Tracks:
      - Deployed cron scripts
      - Knowledge store (JSONL lessons and examples)
      - Task history and convergence reports

    Usage:
        gm = GitManager(repo_dir="~/.autocron")
        gm.init()
        gm.commit_script("backup.sh", "Deploy backup script (5 rounds)")
        gm.tag_deployment("backup", round_count=5)
    """

    def __init__(self, repo_dir: str = "~/.autocron"):
        self.repo_dir = str(Path(repo_dir).expanduser().resolve())

    def init(self) -> bool:
        """
        Initialize a git repo in repo_dir if not already initialized.
        Returns True if a new repo was created, False if one already existed.
        """
        repo = Path(self.repo_dir)
        repo.mkdir(parents=True, exist_ok=True)

        if (repo / ".git").exists():
            logger.debug("Git repo already exists at %s", self.repo_dir)
            return False

        self._run("init")

        # Create .gitignore for runtime files
        gitignore = repo / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "# AutoCron runtime\n"
                "*.log\n"
                "*.tmp\n"
                "__pycache__/\n"
                "platform_events/\n"
            )

        # Initial commit
        self._run("add", "-A")
        self._run("commit", "-m", "Initialize AutoCron repository",
                   "--allow-empty")
        logger.info("Initialized git repo at %s", self.repo_dir)
        return True

    def commit_script(self, script_path: str, message: str) -> Optional[str]:
        """
        Stage and commit a deployed script.
        Returns the commit hash, or None if nothing to commit.
        """
        # Resolve relative to repo_dir
        full_path = Path(self.repo_dir) / script_path
        if not full_path.exists():
            logger.warning("Script not found: %s", full_path)
            return None

        self._run("add", str(full_path))
        return self._commit(message)

    def commit_knowledge(self, message: str = "Update knowledge store") -> Optional[str]:
        """
        Commit all changes in the knowledge directory.
        Returns the commit hash, or None if nothing to commit.
        """
        knowledge_dir = Path(self.repo_dir) / "knowledge"
        if knowledge_dir.exists():
            self._run("add", str(knowledge_dir))
        return self._commit(message)

    def commit_all(self, message: str) -> Optional[str]:
        """
        Stage all changes and commit.
        Returns the commit hash, or None if nothing to commit.
        """
        self._run("add", "-A")
        return self._commit(message)

    def tag_deployment(self, task_name: str, round_count: int) -> str:
        """
        Tag the current HEAD with deployment metadata.
        Returns the tag name.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Sanitize task_name for use as git tag
        safe_name = "".join(
            c if c.isalnum() or c in "-_" else "_"
            for c in task_name
        ).strip("_")[:50]
        tag = f"deploy/{safe_name}_{timestamp}"

        self._run("tag", "-a", tag, "-m",
                   f"Deploy {task_name} ({round_count} rounds)")
        logger.info("Tagged deployment: %s", tag)
        return tag

    def log(self, n: int = 10) -> list[dict]:
        """
        Return the last N commits as dicts with hash, date, message.
        """
        result = self._run(
            "log", f"-{n}",
            "--format=%H|%aI|%s",
            "--no-walk=sorted",
            check=False,
        )
        if not result or not result.strip():
            return []

        entries = []
        for line in result.strip().split("\n"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                entries.append({
                    "hash": parts[0][:7],
                    "date": parts[1],
                    "message": parts[2],
                })
        return entries

    def diff_last(self) -> str:
        """Show the diff of the last commit."""
        result = self._run("diff", "HEAD~1", "HEAD", check=False)
        return result or ""

    def push(self, remote: str = "origin", branch: str = "main") -> bool:
        """
        Push to remote. Returns True if successful.
        Fails silently if no remote is configured.
        """
        try:
            self._run("push", remote, branch, check=True)
            return True
        except subprocess.CalledProcessError:
            logger.debug("Push failed (remote '%s' may not be configured)", remote)
            return False

    def has_remote(self) -> bool:
        """Check if any remote is configured."""
        result = self._run("remote", check=False)
        return bool(result and result.strip())

    def status_summary(self) -> dict:
        """
        Return a summary of the repo for status reports.
        """
        log = self.log(n=5)
        tags = self._run("tag", "-l", "deploy/*", check=False) or ""
        tag_count = len([t for t in tags.strip().split("\n") if t])

        return {
            "repo_dir": self.repo_dir,
            "has_remote": self.has_remote(),
            "total_deployments": tag_count,
            "recent_commits": log,
        }

    # ── Internal ──────────────────────────────────────────────────

    def _commit(self, message: str) -> Optional[str]:
        """Commit staged changes. Returns hash or None if nothing staged."""
        # Check if there's anything to commit
        status = self._run("status", "--porcelain", check=False)
        if not status or not status.strip():
            logger.debug("Nothing to commit")
            return None

        self._run("commit", "-m", message)
        result = self._run("rev-parse", "--short", "HEAD")
        commit_hash = result.strip() if result else None
        logger.info("Committed: %s — %s", commit_hash, message)
        return commit_hash

    def _run(self, *args: str, check: bool = True) -> Optional[str]:
        """Run a git command in the repo directory."""
        cmd = ["git", "-C", self.repo_dir] + list(args)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=check,
                timeout=30,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            if check:
                raise
            logger.debug("Git command failed: %s\n%s", " ".join(cmd), e.stderr)
            return None
        except FileNotFoundError:
            logger.error("git is not installed or not on PATH")
            return None
