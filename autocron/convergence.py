"""
Convergence Detector for AutoCron.

Three stopping conditions:
  1. Hard stop:  Manager issues "approved" verdict.
  2. Soft stop:  Only cosmetic issues remain after N rounds.
  3. Research stop: Knowledge store is saturated (no new patterns).

The detector is queried each round by main.py to decide whether to continue.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class ConvergenceConfig:
    # Soft stop: stop if only cosmetic issues for this many consecutive rounds
    cosmetic_patience: int = 3

    # Research stop: new patterns per window
    saturation_window: int = 10       # look at last N rounds
    saturation_threshold: int = 1     # stop if fewer than this many new patterns

    # First-round success rate for convergence detection
    # (across recent runs, not within a single run)
    first_round_approval_threshold: float = 0.7

    # Maximum total rounds (hard ceiling)
    max_rounds: int = 30


@dataclass
class RoundSignal:
    """Signal from a single round, fed to the detector."""
    round_num: int
    verdict: str                    # "fail" | "pass_with_issues" | "approved"
    issue_severity: str = "unknown" # "critical" | "moderate" | "cosmetic"
    new_lesson_pattern: Optional[str] = None  # None if no new pattern was learned
    is_duplicate_pattern: bool = False         # True if pattern already existed


class ConvergenceDetector:
    """
    Tracks round signals and determines when to stop.

    Usage:
        detector = ConvergenceDetector(config)
        for round in loop:
            ... execute round ...
            signal = RoundSignal(...)
            decision = detector.check(signal)
            if decision.should_stop:
                break
    """

    def __init__(self, config: Optional[ConvergenceConfig] = None):
        self.config = config or ConvergenceConfig()
        self.signals: list[RoundSignal] = []
        self._consecutive_cosmetic = 0
        self._new_patterns_in_window: list[bool] = []

    def check(self, signal: RoundSignal) -> "ConvergenceDecision":
        """
        Process a round signal and return a decision.

        Returns ConvergenceDecision with should_stop, reason, and metrics.
        """
        self.signals.append(signal)

        # ── 1. Hard stop: approved ────────────────────────────────
        if signal.verdict == "approved":
            return ConvergenceDecision(
                should_stop=True,
                reason="approved",
                message=f"Manager approved the script on round {signal.round_num}.",
                metrics=self._compute_metrics(),
            )

        # ── 2. Hard ceiling ───────────────────────────────────────
        if signal.round_num >= self.config.max_rounds:
            return ConvergenceDecision(
                should_stop=True,
                reason="max_rounds",
                message=f"Reached maximum rounds ({self.config.max_rounds}).",
                metrics=self._compute_metrics(),
            )

        # ── 3. Soft stop: cosmetic patience ───────────────────────
        if signal.verdict == "pass_with_issues":
            if signal.issue_severity == "cosmetic":
                self._consecutive_cosmetic += 1
            else:
                self._consecutive_cosmetic = 0

            if self._consecutive_cosmetic >= self.config.cosmetic_patience:
                return ConvergenceDecision(
                    should_stop=True,
                    reason="cosmetic_only",
                    message=(
                        f"Only cosmetic issues for {self._consecutive_cosmetic} "
                        f"consecutive rounds. Script is good enough."
                    ),
                    metrics=self._compute_metrics(),
                )
        else:
            self._consecutive_cosmetic = 0

        # ── 4. Research stop: knowledge saturation ────────────────
        is_new = (
            signal.new_lesson_pattern is not None
            and not signal.is_duplicate_pattern
        )
        self._new_patterns_in_window.append(is_new)

        if len(self._new_patterns_in_window) >= self.config.saturation_window:
            window = self._new_patterns_in_window[-self.config.saturation_window:]
            new_count = sum(window)
            if new_count < self.config.saturation_threshold:
                return ConvergenceDecision(
                    should_stop=True,
                    reason="knowledge_saturated",
                    message=(
                        f"Only {new_count} new patterns in last "
                        f"{self.config.saturation_window} rounds. "
                        f"Knowledge store is saturated."
                    ),
                    metrics=self._compute_metrics(),
                )

        # ── Continue ──────────────────────────────────────────────
        return ConvergenceDecision(
            should_stop=False,
            reason="continue",
            message=f"Round {signal.round_num}: continuing.",
            metrics=self._compute_metrics(),
        )

    def _compute_metrics(self) -> dict:
        """Compute convergence metrics for logging/reporting."""
        if not self.signals:
            return {}

        total = len(self.signals)
        approvals = sum(1 for s in self.signals if s.verdict == "approved")
        failures = sum(1 for s in self.signals if s.verdict == "fail")
        pass_issues = sum(1 for s in self.signals if s.verdict == "pass_with_issues")
        new_patterns = sum(1 for s in self.signals
                         if s.new_lesson_pattern and not s.is_duplicate_pattern)
        dup_patterns = sum(1 for s in self.signals if s.is_duplicate_pattern)

        # Verdict trajectory (last 5)
        recent = self.signals[-5:]
        trajectory = [s.verdict for s in recent]

        # Rounds to first pass
        first_pass = None
        for s in self.signals:
            if s.verdict in ("pass_with_issues", "approved"):
                first_pass = s.round_num
                break

        return {
            "total_rounds": total,
            "approvals": approvals,
            "failures": failures,
            "pass_with_issues": pass_issues,
            "new_patterns_learned": new_patterns,
            "duplicate_patterns": dup_patterns,
            "consecutive_cosmetic": self._consecutive_cosmetic,
            "trajectory": trajectory,
            "rounds_to_first_pass": first_pass,
        }

    def reset(self):
        """Reset for a new run."""
        self.signals.clear()
        self._consecutive_cosmetic = 0
        self._new_patterns_in_window.clear()


@dataclass
class ConvergenceDecision:
    should_stop: bool
    reason: str       # "approved" | "max_rounds" | "cosmetic_only" | "knowledge_saturated" | "continue"
    message: str
    metrics: dict


# ---------------------------------------------------------------------------
# Cross-run convergence analysis
# ---------------------------------------------------------------------------

class CrossRunAnalyzer:
    """
    Analyze convergence trends across multiple AutoCron runs.
    Used to determine if the knowledge store has matured enough
    that further runs provide diminishing returns.
    """

    def __init__(self, results_dir: str = "runs"):
        self.results_dir = results_dir

    def analyze(self) -> dict:
        """
        Analyze all completed runs and return convergence metrics.

        Returns:
            {
                "total_runs": int,
                "success_rate": float,
                "avg_rounds_to_approval": float,
                "first_round_approval_rate": float,
                "new_patterns_per_run": float,  # trending metric
                "recommendation": str,
            }
        """
        import json
        from pathlib import Path

        results_path = Path(self.results_dir)
        if not results_path.exists():
            return {"error": "No runs directory found"}

        runs = []
        for run_dir in sorted(results_path.iterdir()):
            results_file = run_dir / "results.json"
            if results_file.exists():
                try:
                    runs.append(json.loads(results_file.read_text()))
                except json.JSONDecodeError:
                    continue

        if not runs:
            return {"error": "No completed runs found"}

        total = len(runs)
        solved = sum(1 for r in runs if r.get("solved"))
        rounds_to_approval = [
            r["total_rounds"] for r in runs if r.get("solved")
        ]
        first_round_approvals = sum(
            1 for r in runs
            if r.get("solved") and r.get("total_rounds", 99) <= 1
        )

        # Count new patterns per run (from lessons in each run's rounds)
        patterns_per_run = []
        seen_patterns = set()
        for r in runs:
            new_in_run = 0
            for round_data in r.get("rounds", []):
                lesson = round_data.get("lesson", "")
                # Extract pattern from "[pattern] text" format
                match = __import__("re").match(r"\[(\w+)\]", lesson)
                if match:
                    pattern = match.group(1)
                    if pattern not in seen_patterns:
                        seen_patterns.add(pattern)
                        new_in_run += 1
            patterns_per_run.append(new_in_run)

        avg_new_patterns = (
            sum(patterns_per_run[-10:]) / min(10, len(patterns_per_run))
            if patterns_per_run else 0
        )

        result = {
            "total_runs": total,
            "success_rate": solved / total if total else 0,
            "avg_rounds_to_approval": (
                sum(rounds_to_approval) / len(rounds_to_approval)
                if rounds_to_approval else None
            ),
            "first_round_approval_rate": (
                first_round_approvals / total if total else 0
            ),
            "new_patterns_per_run_recent": round(avg_new_patterns, 2),
            "total_unique_patterns": len(seen_patterns),
        }

        # Recommendation
        if avg_new_patterns < 0.5 and result["first_round_approval_rate"] > 0.7:
            result["recommendation"] = (
                "Knowledge store is mature. Consider fine-tuning the Worker "
                "model on accumulated successful scripts."
            )
        elif avg_new_patterns < 1.0:
            result["recommendation"] = (
                "Knowledge store is stabilizing. Continue running for "
                "broader task coverage."
            )
        else:
            result["recommendation"] = (
                "Knowledge store is still growing. Continue accumulating "
                "lessons across diverse tasks."
            )

        return result
