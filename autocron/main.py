#!/usr/bin/env python3
"""
AutoCron v2 — Autonomous System Administration via LLM Loop

Architecture:
  - Worker (Local LLM): Writes/fixes/hardens scripts. Free inference.
  - Manager (Cloud LLM): Reviews EVERY round. Diagnoses failures, reviews
    successes, extracts lessons. Always in the loop, not just on error.
  - Judge (Python harness): Executes in sandboxed cron-like env with full
    instrumentation (xtrace, env capture, command resolution, permissions).
  - Knowledge Store: Accumulates distilled lessons across runs. Injected
    into Worker's system prompt as <500 token KNOWN_PITFALLS block.

The Manager sees everything. The Worker sees only what it needs.
Information flows: raw trace → Manager diagnosis → distilled lesson → Worker hint.

Inspired by karpathy/autoresearch.

Usage:
  python main.py task.md --dry-run
  python main.py task.md --worker-model qwen3:27b --manager-model claude-sonnet-4-20250514
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .llm_backend import AgentTeam
from .judge import Judge, ExecutionTrace
from .knowledge import KnowledgeStore
from .convergence import ConvergenceDetector, ConvergenceConfig, RoundSignal
from .router import Router, RouteDecision


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    task_file: str
    max_rounds: int = 30
    dry_run: bool = False
    output_dir: str = "runs"
    knowledge_dir: str = "~/.autocron/knowledge"
    scripts_dir: str = "~/.autocron/scripts"
    worker_model: str = "qwen3:27b"
    worker_url: str = "http://localhost:11434"
    manager_provider: str = "anthropic"
    manager_model: str = "claude-sonnet-4-20250514"
    sandbox_timeout: int = 60
    log_level: str = "INFO"
    # Convergence
    cosmetic_patience: int = 3
    skip_routing: bool = False  # bypass router, always run full loop


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class RoundRecord:
    round_num: int
    timestamp: str
    worker_mode: str          # "generate" | "fix" | "harden"
    script_hash: str
    exit_code: int
    process_succeeded: bool
    manager_verdict: str      # "fail" | "pass_with_issues" | "approved"
    manager_analysis: str
    lesson_pattern: str
    lesson_text: str
    duration_worker: float    # seconds for Worker inference
    duration_judge: float     # seconds for script execution
    duration_manager: float   # seconds for Manager review

@dataclass
class RunState:
    task: str
    rounds: list = field(default_factory=list)
    solved: bool = False
    final_script: Optional[str] = None
    final_cron: Optional[str] = None
    total_manager_tokens: int = 0  # rough estimate


# ---------------------------------------------------------------------------
# Main Engine
# ---------------------------------------------------------------------------

class AutoCron:
    def __init__(self, config: Config):
        self.config = config
        self.team = AgentTeam(
            worker_url=config.worker_url,
            worker_model=config.worker_model,
            manager_provider=config.manager_provider,
            manager_model=config.manager_model,
        )
        self.judge = Judge(timeout=config.sandbox_timeout)
        self.knowledge = KnowledgeStore(store_dir=config.knowledge_dir)
        self.run_dir = self._init_run_dir()
        self.logger = self._init_logger()

    def _init_run_dir(self) -> Path:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = Path(self.config.output_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _init_logger(self) -> logging.Logger:
        logger = logging.getLogger("autocron")
        logger.setLevel(self.config.log_level)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        fh = logging.FileHandler(self.run_dir / "autocron.log")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
        return logger

    def run(self) -> RunState:
        task = Path(self.config.task_file).read_text().strip()
        state = RunState(task=task)

        self.logger.info("=" * 60)
        self.logger.info("AutoCron v3")
        self.logger.info(f"Task: {self.config.task_file}")
        self.logger.info(f"Worker: {self.config.worker_model} @ {self.config.worker_url}")
        self.logger.info(f"Manager: {self.config.manager_model} ({self.config.manager_provider})")
        self.logger.info(f"Knowledge: {self.knowledge.lesson_count} lessons, {self.knowledge.example_count} examples")
        self.logger.info(f"Max rounds: {self.config.max_rounds}")
        self.logger.info("=" * 60)

        # ── Route: check if we already have a solution ────────────
        if not self.config.skip_routing:
            router = Router(
                knowledge_store=self.knowledge,
                scripts_dir=self.config.scripts_dir,
            )
            route = router.route(task)
            self.logger.info(f"🧭 Router: {route.path} (confidence={route.confidence:.0%})")
            self.logger.info(f"   {route.explanation}")

            if route.path == "deploy" and route.matched_script:
                self.logger.info(f"   Deploying existing script: {route.matched_script}")
                script = Path(route.matched_script).read_text()
                state.solved = True
                state.final_script = script
                # Still run through Judge once to verify it still works
                self.logger.info("   Verifying existing script in sandbox...")
                trace = self.judge.execute(script, round_num=0)
                if trace.process_succeeded:
                    self.logger.info("   ✅ Existing script still passes. Deploying.")
                    self._save_report(state)
                    self._print_summary(state)
                    return state
                else:
                    self.logger.info("   ⚠️ Existing script failed verification. Falling through to loop.")
                    state.solved = False

        # ── Initialize convergence detector ───────────────────────
        conv_config = ConvergenceConfig(
            cosmetic_patience=self.config.cosmetic_patience,
            max_rounds=self.config.max_rounds,
        )
        detector = ConvergenceDetector(conv_config)

        # Get knowledge blocks (refreshed when new lessons added)
        pitfalls, toolkit = self.knowledge.get_injection_blocks(task)
        examples = self.knowledge.get_examples_block(task)

        if pitfalls:
            self.logger.info(f"Injecting pitfalls block ({pitfalls.count(chr(10))} lines)")
        if toolkit:
            self.logger.info(f"Injecting toolkit block ({toolkit.count(chr(10))} lines)")
        if examples:
            self.logger.info(f"Injecting {examples.count(chr(10))} example lines into Worker prompt")

        last_manager_analysis: Optional[str] = None
        last_verdict: Optional[str] = None

        for round_num in range(1, self.config.max_rounds + 1):
            self.logger.info(f"\n{'─'*50}")
            self.logger.info(f"ROUND {round_num}")
            self.logger.info(f"{'─'*50}")

            # ── 1. Worker generates/fixes/hardens ─────────────────────
            t0 = time.time()
            if last_manager_analysis is None:
                # First round
                worker_mode = "generate"
                self.logger.info("🤖 Worker: generating initial script...")
                worker_output = self.team.worker_generate(
                    task, pitfalls, toolkit, examples
                )
            elif last_verdict == "fail":
                worker_mode = "fix"
                self.logger.info("🤖 Worker: fixing failed script...")
                worker_output = self.team.worker_fix(
                    task, last_manager_analysis, pitfalls, toolkit, examples
                )
            else:  # pass_with_issues
                worker_mode = "harden"
                self.logger.info("🤖 Worker: hardening script per review...")
                worker_output = self.team.worker_harden(
                    task, last_manager_analysis, pitfalls, toolkit, examples
                )
            t_worker = time.time() - t0

            script = worker_output.get("script", "")
            cron_schedule = worker_output.get("cron_schedule")
            reasoning = worker_output.get("reasoning", "")

            self.logger.info(f"   Worker responded in {t_worker:.1f}s")
            self.logger.info(f"   Reasoning: {reasoning[:150]}")

            if not script.strip():
                self.logger.warning("   Worker returned empty script. Retrying...")
                continue

            # Save script
            script_path = self.run_dir / f"round_{round_num:03d}.sh"
            script_path.write_text(script)

            # ── 2. Judge executes with full instrumentation ───────────
            self.logger.info("⚖️  Judge: executing in cron sandbox...")
            t0 = time.time()
            trace: ExecutionTrace = self.judge.execute(script, round_num=round_num)
            t_judge = time.time() - t0

            self.logger.info(f"   {trace.summary_for_log()}")

            # Save full trace
            trace_path = self.run_dir / f"round_{round_num:03d}_trace.txt"
            trace_report = trace.full_report()
            trace_path.write_text(trace_report)

            # ── 3. Manager reviews (EVERY round) ─────────────────────
            history_summary = self._build_history_summary(state.rounds)

            self.logger.info("🧠 Manager: reviewing execution trace...")
            t0 = time.time()
            review = self.team.manager_review(
                task=task,
                execution_trace_report=trace_report,
                round_num=round_num,
                history_summary=history_summary,
            )
            t_manager = time.time() - t0

            verdict = review.get("verdict", "fail")
            analysis = review.get("analysis", "No analysis provided.")
            lesson_pattern = review.get("lesson_pattern", "unknown")
            lesson_type = review.get("lesson_type", "prose")
            lesson_content = review.get("lesson_content", "")
            lesson_explanation = review.get("lesson_explanation", "")
            # Backward compat: old Manager responses may use "lesson_text"
            if not lesson_content:
                lesson_content = review.get("lesson_text", "")

            self.logger.info(f"   Manager responded in {t_manager:.1f}s")
            self.logger.info(f"   Verdict: {verdict}")
            self.logger.info(f"   Analysis: {analysis[:200]}")
            self.logger.info(f"   Lesson: [{lesson_pattern}] ({lesson_type}) {lesson_content[:120]}")

            # Save Manager review
            review_path = self.run_dir / f"round_{round_num:03d}_review.json"
            review_path.write_text(json.dumps(review, indent=2))

            # ── 4. Extract lesson (EVERY round) ──────────────────────
            is_new_pattern = False
            is_dup = False
            if lesson_pattern and lesson_content:
                existing = self.knowledge._lessons.get(
                    lesson_pattern.strip().lower().replace(" ", "_")
                )
                is_dup = existing is not None
                is_new_pattern = not is_dup

                self.knowledge.add_lesson(
                    pattern=lesson_pattern,
                    lesson_type=lesson_type,
                    lesson=lesson_content,
                    explanation=lesson_explanation,
                    source_task=self.config.task_file,
                )
                # Refresh both injection blocks with new knowledge
                pitfalls, toolkit = self.knowledge.get_injection_blocks(task)

            # ── 5. Record round ───────────────────────────────────────
            record = RoundRecord(
                round_num=round_num,
                timestamp=datetime.now().isoformat(),
                worker_mode=worker_mode,
                script_hash=trace.script_hash,
                exit_code=trace.exit_code,
                process_succeeded=trace.process_succeeded,
                manager_verdict=verdict,
                manager_analysis=analysis,
                lesson_pattern=lesson_pattern,
                lesson_text=f"({lesson_type}) {lesson_content}",
                duration_worker=round(t_worker, 2),
                duration_judge=round(t_judge, 2),
                duration_manager=round(t_manager, 2),
            )
            state.rounds.append(record)

            # ── 6. Convergence check ──────────────────────────────────
            # Infer severity from Manager's analysis
            severity = "unknown"
            analysis_lower = analysis.lower()
            if verdict == "fail":
                severity = "critical"
            elif verdict == "pass_with_issues":
                if any(w in analysis_lower for w in
                       ("security", "race condition", "data loss", "permission",
                        "injection", "overflow", "corrupt")):
                    severity = "critical"
                elif any(w in analysis_lower for w in
                         ("hardcode", "fragile", "edge case", "validate",
                          "error handling", "cleanup")):
                    severity = "moderate"
                else:
                    severity = "cosmetic"

            signal = RoundSignal(
                round_num=round_num,
                verdict=verdict,
                issue_severity=severity,
                new_lesson_pattern=lesson_pattern if is_new_pattern else None,
                is_duplicate_pattern=is_dup,
            )
            decision = detector.check(signal)

            if decision.should_stop:
                if verdict == "approved":
                    self.logger.info(f"\n✅ APPROVED on round {round_num}!")
                    state.solved = True
                    state.final_script = script
                    state.final_cron = cron_schedule

                    self.knowledge.add_solved_example(
                        task_type=lesson_pattern,
                        task_summary=task[:100],
                        key_decisions=reasoning[:200],
                        cron_schedule=cron_schedule or "",
                    )

                    if cron_schedule and not self.config.dry_run:
                        self._install_cron(script, cron_schedule)
                    elif cron_schedule:
                        self.logger.info(f"   [DRY RUN] Would install: {cron_schedule}")

                elif decision.reason == "cosmetic_only":
                    self.logger.info(f"\n✅ GOOD ENOUGH on round {round_num} (cosmetic issues only)")
                    state.solved = True
                    state.final_script = script
                    state.final_cron = cron_schedule

                    self.knowledge.add_solved_example(
                        task_type=lesson_pattern,
                        task_summary=task[:100],
                        key_decisions=reasoning[:200],
                        cron_schedule=cron_schedule or "",
                    )

                    if cron_schedule and not self.config.dry_run:
                        self._install_cron(script, cron_schedule)
                    elif cron_schedule:
                        self.logger.info(f"   [DRY RUN] Would install: {cron_schedule}")

                else:
                    self.logger.info(f"\n⛔ STOPPED: {decision.message}")

                self.logger.info(f"   Convergence: {json.dumps(decision.metrics, indent=2)}")
                break

            else:
                # Continue loop
                last_manager_analysis = analysis
                last_verdict = verdict
                if verdict == "fail":
                    self.logger.info("   → Worker will FIX next round")
                elif verdict == "pass_with_issues":
                    self.logger.info(f"   → Worker will HARDEN next round (severity: {severity})")

        # ── Final report ──────────────────────────────────────────────
        self._save_report(state)
        self._print_summary(state)
        return state

    def _build_history_summary(self, rounds: list[RoundRecord]) -> str:
        """Compact history for Manager context. Shows trajectory, not full traces."""
        if not rounds:
            return "This is the first round. No previous attempts."

        lines = [f"Previous {len(rounds)} rounds:"]
        for r in rounds:
            lines.append(
                f"  Round {r.round_num}: {r.worker_mode} → "
                f"exit={r.exit_code}, verdict={r.manager_verdict}, "
                f"lesson=[{r.lesson_pattern}]"
            )
        return "\n".join(lines)

    def _install_cron(self, script: str, schedule: str):
        install_dir = Path.home() / ".autocron" / "scripts"
        install_dir.mkdir(parents=True, exist_ok=True)

        script_name = f"autocron_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sh"
        script_path = install_dir / script_name
        script_path.write_text(script)
        script_path.chmod(0o755)

        try:
            existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            current = existing.stdout if existing.returncode == 0 else ""
            new_entry = f"{schedule} {script_path}  # AutoCron {datetime.now().isoformat()}\n"
            new_crontab = current.rstrip("\n") + "\n" + new_entry

            proc = subprocess.run(
                ["crontab", "-"], input=new_crontab, capture_output=True, text=True
            )
            if proc.returncode == 0:
                self.logger.info(f"📅 Installed: {schedule} {script_path}")
            else:
                self.logger.error(f"Crontab install failed: {proc.stderr}")
        except Exception as e:
            self.logger.error(f"Crontab error: {e}")

    def _save_report(self, state: RunState):
        report = {
            "task": state.task,
            "task_file": self.config.task_file,
            "solved": state.solved,
            "total_rounds": len(state.rounds),
            "final_cron": state.final_cron,
            "knowledge_lessons_total": self.knowledge.lesson_count,
            "knowledge_examples_total": self.knowledge.example_count,
            "config": {
                "worker_model": self.config.worker_model,
                "manager_model": self.config.manager_model,
                "sandbox_timeout": self.config.sandbox_timeout,
            },
            "rounds": [
                {
                    "round": r.round_num,
                    "mode": r.worker_mode,
                    "exit_code": r.exit_code,
                    "verdict": r.manager_verdict,
                    "lesson": f"[{r.lesson_pattern}] {r.lesson_text}",
                    "timing": {
                        "worker": r.duration_worker,
                        "judge": r.duration_judge,
                        "manager": r.duration_manager,
                    },
                }
                for r in state.rounds
            ],
        }
        path = self.run_dir / "results.json"
        path.write_text(json.dumps(report, indent=2))
        self.logger.info(f"📄 Report: {path}")

    def _print_summary(self, state: RunState):
        total_worker = sum(r.duration_worker for r in state.rounds)
        total_judge = sum(r.duration_judge for r in state.rounds)
        total_manager = sum(r.duration_manager for r in state.rounds)
        kstats = self.knowledge.stats()

        self.logger.info(f"\n{'='*60}")
        self.logger.info("AUTOCRON SUMMARY")
        self.logger.info(f"{'='*60}")
        self.logger.info(f"Task:             {state.task[:80]}")
        self.logger.info(f"Solved:           {'YES' if state.solved else 'NO'}")
        self.logger.info(f"Total rounds:     {len(state.rounds)}")
        if state.final_cron:
            self.logger.info(f"Cron schedule:    {state.final_cron}")
        self.logger.info(f"Time (Worker):    {total_worker:.1f}s")
        self.logger.info(f"Time (Judge):     {total_judge:.1f}s")
        self.logger.info(f"Time (Manager):   {total_manager:.1f}s")
        self.logger.info(
            f"Knowledge:        {kstats['total_lessons']} lessons "
            f"({kstats['by_type'].get('prose', 0)} prose, "
            f"{kstats['by_type'].get('command', 0)} commands, "
            f"{kstats['by_type'].get('snippet', 0)} snippets) | "
            f"{kstats['total_examples']} examples | "
            f"{kstats['total_observations']} total observations"
        )
        self.logger.info(f"Run dir:          {self.run_dir}")
        self.logger.info(f"{'='*60}")

        # Show top patterns with type
        top = self.knowledge.top_patterns(5)
        if top:
            self.logger.info("\nTop learned patterns:")
            for pattern, freq, ltype in top:
                self.logger.info(f"  [{freq}x] ({ltype}) {pattern}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AutoCron v3")
    parser.add_argument("task_file", help="Path to task.md")
    parser.add_argument("--max-rounds", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", default="runs")
    parser.add_argument("--knowledge-dir", default="~/.autocron/knowledge")
    parser.add_argument("--scripts-dir", default="~/.autocron/scripts")
    parser.add_argument("--worker-model", default="qwen3:27b")
    parser.add_argument("--worker-url", default="http://localhost:11434")
    parser.add_argument("--manager-provider", default="anthropic",
                        choices=["anthropic", "openai"])
    parser.add_argument("--manager-model", default="claude-sonnet-4-20250514")
    parser.add_argument("--sandbox-timeout", type=int, default=60)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--cosmetic-patience", type=int, default=3,
                        help="Rounds of cosmetic-only issues before soft stop")
    parser.add_argument("--skip-routing", action="store_true",
                        help="Skip router, always run full loop")

    args = parser.parse_args()
    config = Config(**vars(args))

    engine = AutoCron(config)
    state = engine.run()
    sys.exit(0 if state.solved else 1)


if __name__ == "__main__":
    main()
