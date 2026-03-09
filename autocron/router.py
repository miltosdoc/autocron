"""
AutoCron Router — Dispatch incoming requests to the right execution path.

Three paths:
  1. Exact match:   Deploy existing approved script directly.
  2. Partial match:  Start the loop with an existing script as template.
  3. No match:      Full AutoCron loop from scratch.

The Router queries the knowledge store's examples.jsonl and the
approved scripts directory to find prior solutions.
"""

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .knowledge import KnowledgeStore


@dataclass
class RouteDecision:
    path: str                          # "deploy" | "adapt" | "full_loop"
    confidence: float                  # 0.0 - 1.0
    matched_example: Optional[dict]    # the matched SolvedExample if any
    matched_script: Optional[str]      # path to existing script if any
    explanation: str                   # human-readable routing reason


class Router:
    """
    Routes incoming task requests to the optimal execution path.

    Searches:
      1. Approved scripts directory (~/.autocron/scripts/) for deployed solutions
      2. Knowledge store examples (examples.jsonl) for solved task strategies
      3. Knowledge store lessons for relevant operational patterns
    """

    # Thresholds for routing decisions
    EXACT_THRESHOLD = 0.75    # above this → deploy existing script
    PARTIAL_THRESHOLD = 0.35  # above this → adapt existing script
    # Below partial → full loop from scratch

    def __init__(
        self,
        knowledge_store: Optional[KnowledgeStore] = None,
        scripts_dir: str = "~/.autocron/scripts",
        runs_dir: str = "runs",
    ):
        self.knowledge = knowledge_store or KnowledgeStore()
        self.scripts_dir = Path(os.path.expanduser(scripts_dir))
        self.runs_dir = Path(runs_dir)
        self._script_index = self._build_script_index()

    def route(self, task_text: str) -> RouteDecision:
        """
        Analyze a task description and decide the execution path.

        Args:
            task_text: The full task.md content.

        Returns:
            RouteDecision with path, confidence, and matched resources.
        """
        task_words = self._extract_keywords(task_text)
        if not task_words:
            return RouteDecision(
                path="full_loop", confidence=0.0,
                matched_example=None, matched_script=None,
                explanation="Could not extract keywords from task. Starting from scratch.",
            )

        # ── 1. Check against solved examples ──────────────────────
        best_example, example_score = self._match_examples(task_words)

        # ── 2. Check against deployed scripts ─────────────────────
        best_script, script_score, script_path = self._match_scripts(task_words)

        # ── 3. Combine scores ─────────────────────────────────────
        # Use the higher of the two matches
        if script_score >= example_score:
            best_score = script_score
            match_source = "script"
        else:
            best_score = example_score
            match_source = "example"

        # ── 4. Route decision ─────────────────────────────────────
        if best_score >= self.EXACT_THRESHOLD:
            if match_source == "script" and script_path:
                return RouteDecision(
                    path="deploy",
                    confidence=best_score,
                    matched_example=best_example,
                    matched_script=str(script_path),
                    explanation=(
                        f"High-confidence match ({best_score:.0%}) with existing "
                        f"deployed script: {script_path.name}. "
                        f"Can deploy directly without running the loop."
                    ),
                )
            elif best_example:
                return RouteDecision(
                    path="adapt",
                    confidence=best_score,
                    matched_example=best_example,
                    matched_script=str(script_path) if script_path else None,
                    explanation=(
                        f"High-confidence match ({best_score:.0%}) with solved "
                        f"example [{best_example.get('task_type', '?')}]. "
                        f"Strategy: {best_example.get('key_decisions', '?')}. "
                        f"Will adapt rather than generate from scratch."
                    ),
                )

        if best_score >= self.PARTIAL_THRESHOLD:
            return RouteDecision(
                path="adapt",
                confidence=best_score,
                matched_example=best_example,
                matched_script=str(script_path) if script_path else None,
                explanation=(
                    f"Partial match ({best_score:.0%}). Similar task found but "
                    f"not identical. Will use as starting template."
                ),
            )

        return RouteDecision(
            path="full_loop",
            confidence=best_score,
            matched_example=best_example if best_score > 0 else None,
            matched_script=None,
            explanation=(
                f"No strong match found (best: {best_score:.0%}). "
                f"Running full AutoCron loop from scratch."
            ),
        )

    def list_available_solutions(self) -> list[dict]:
        """List all deployed scripts and solved examples for inspection."""
        solutions = []

        # Deployed scripts
        if self.scripts_dir.exists():
            for script in sorted(self.scripts_dir.glob("*.sh")):
                content = script.read_text()
                # Extract description from comments
                desc = ""
                for line in content.split("\n"):
                    if line.startswith("# ") and not line.startswith("#!"):
                        desc = line[2:].strip()
                        break
                solutions.append({
                    "type": "deployed_script",
                    "name": script.name,
                    "path": str(script),
                    "description": desc,
                    "modified": datetime.fromtimestamp(
                        script.stat().st_mtime
                    ).isoformat(),
                })

        # Solved examples from knowledge store
        for ex in self.knowledge._examples:
            solutions.append({
                "type": "solved_example",
                "name": ex.task_type,
                "description": ex.task_summary,
                "strategy": ex.key_decisions,
                "schedule": ex.cron_schedule,
                "timestamp": ex.timestamp,
            })

        return solutions

    # ------------------------------------------------------------------
    # Matching logic
    # ------------------------------------------------------------------

    def _match_examples(self, task_words: set[str]) -> tuple[Optional[dict], float]:
        """Find the best matching solved example."""
        best_example = None
        best_score = 0.0

        for ex in self.knowledge._examples:
            ex_words = self._extract_keywords(
                f"{ex.task_type} {ex.task_summary} {ex.key_decisions}"
            )
            if not ex_words:
                continue

            score = self._jaccard(task_words, ex_words)
            if score > best_score:
                best_score = score
                best_example = ex.to_dict()

        return best_example, best_score

    def _match_scripts(
        self, task_words: set[str]
    ) -> tuple[Optional[dict], float, Optional[Path]]:
        """Find the best matching deployed script."""
        best_info = None
        best_score = 0.0
        best_path = None

        for path, keywords in self._script_index.items():
            if not keywords:
                continue

            score = self._jaccard(task_words, keywords)
            if score > best_score:
                best_score = score
                best_path = path
                best_info = {"path": str(path), "keywords": list(keywords)[:10]}

        return best_info, best_score, best_path

    def _build_script_index(self) -> dict[Path, set[str]]:
        """Index deployed scripts by their content keywords."""
        index = {}
        if not self.scripts_dir.exists():
            return index

        for script in self.scripts_dir.glob("*.sh"):
            try:
                content = script.read_text()
                keywords = self._extract_keywords(content)
                index[script] = keywords
            except Exception:
                continue

        return index

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        """
        Overlap coefficient: intersection / min(|a|, |b|).
        Better than Jaccard for asymmetric matches where one set
        is much larger (e.g., script content vs task description).
        """
        if not a or not b:
            return 0.0
        intersection = len(a & b)
        minimum = min(len(a), len(b))
        return intersection / minimum if minimum > 0 else 0.0

    @staticmethod
    def _extract_keywords(text: str) -> set[str]:
        """Same keyword extraction as KnowledgeStore for consistency."""
        STOP_WORDS = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "being", "have", "has", "had", "do", "does", "did", "will",
            "would", "could", "should", "may", "might", "must", "shall",
            "can", "to", "of", "in", "for", "on", "with", "at", "by",
            "from", "as", "into", "through", "during", "before", "after",
            "and", "but", "or", "nor", "not", "no", "so", "yet", "both",
            "each", "every", "all", "any", "few", "more", "most", "other",
            "some", "such", "than", "too", "very", "just", "also", "if",
            "then", "else", "when", "while", "where", "how", "what",
            "which", "who", "this", "that", "these", "those", "it", "its",
            "use", "using", "used", "run", "make", "file", "script",
            "echo", "set", "exit", "true", "false", "null", "bin",
            "usr", "var", "tmp", "home", "etc", "dev", "bash",
        }
        words = set(re.findall(r"[a-z0-9_]+", text.lower()))
        return words - STOP_WORDS


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="AutoCron Router")
    sub = parser.add_subparsers(dest="command", required=True)

    # Route a task
    p_route = sub.add_parser("route", help="Route a task to the best execution path")
    p_route.add_argument("task_file", help="Path to task.md")
    p_route.add_argument("--knowledge-dir", default="~/.autocron/knowledge")
    p_route.add_argument("--scripts-dir", default="~/.autocron/scripts")

    # List available solutions
    p_list = sub.add_parser("list", help="List all available solutions")
    p_list.add_argument("--knowledge-dir", default="~/.autocron/knowledge")
    p_list.add_argument("--scripts-dir", default="~/.autocron/scripts")

    args = parser.parse_args()

    ks = KnowledgeStore(store_dir=args.knowledge_dir)
    router = Router(knowledge_store=ks, scripts_dir=args.scripts_dir)

    if args.command == "route":
        task_text = Path(args.task_file).read_text()
        decision = router.route(task_text)

        print(f"Path:       {decision.path}")
        print(f"Confidence: {decision.confidence:.0%}")
        print(f"Explanation: {decision.explanation}")
        if decision.matched_script:
            print(f"Script:     {decision.matched_script}")
        if decision.matched_example:
            print(f"Example:    {decision.matched_example}")

    elif args.command == "list":
        solutions = router.list_available_solutions()
        if not solutions:
            print("No solutions found.")
        else:
            for s in solutions:
                print(f"  [{s['type']}] {s['name']}: {s.get('description', '')}")


if __name__ == "__main__":
    main()
