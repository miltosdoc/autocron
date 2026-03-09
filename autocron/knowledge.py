"""
Knowledge Store v2 — Typed lessons for AutoCron.

Lessons come in three types:
  - "prose":   Natural language principle (e.g., "Always use absolute paths")
  - "command": A single command or one-liner (e.g., "mktemp -d /tmp/ac.XXXXXX")
  - "snippet": Multi-line code pattern (e.g., flock guard, logging setup)

Prose lessons → KNOWN_PITFALLS block (advice)
Command/snippet lessons → TOOLKIT block (code the Worker can copy)

No RAG. No embeddings. Flat JSONL + frequency ranking + keyword matching.
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Lesson:
    pattern: str            # category tag: "temp_dir_creation", "file_locking", etc.
    type: str               # "prose" | "command" | "snippet"
    lesson: str             # the content: a sentence, a command, or a code block
    explanation: str        # brief context (always prose, even for code lessons)
    frequency: int = 1
    first_seen: str = ""
    last_seen: str = ""
    source_task: str = ""

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "type": self.type,
            "lesson": self.lesson,
            "explanation": self.explanation,
            "frequency": self.frequency,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "source_task": self.source_task,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Lesson":
        # Backward compat: old lessons without "type" default to prose
        if "type" not in d:
            d["type"] = "prose"
        if "explanation" not in d:
            d["explanation"] = ""
        # Migrate old format: "lesson_text" → "lesson"
        if "lesson_text" in d and "lesson" not in d:
            d["lesson"] = d.pop("lesson_text")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class SolvedExample:
    task_type: str
    task_summary: str
    key_decisions: str
    cron_schedule: str
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "task_type": self.task_type,
            "task_summary": self.task_summary,
            "key_decisions": self.key_decisions,
            "cron_schedule": self.cron_schedule,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SolvedExample":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class KnowledgeStore:
    """
    Persistent knowledge accumulator with typed lessons.

    Files:
      knowledge.jsonl  — one lesson per line, deduped by pattern
      examples.jsonl   — one solved task per line
    """

    def __init__(self, store_dir: str = "~/.autocron/knowledge"):
        self.store_dir = Path(os.path.expanduser(store_dir))
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.lessons_file = self.store_dir / "knowledge.jsonl"
        self.examples_file = self.store_dir / "examples.jsonl"
        self._lessons: dict[str, Lesson] = {}
        self._examples: list[SolvedExample] = []
        self._load()

    def _load(self):
        if self.lessons_file.exists():
            for line in self.lessons_file.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    lesson = Lesson.from_dict(d)
                    self._lessons[lesson.pattern] = lesson
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue

        if self.examples_file.exists():
            for line in self.examples_file.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    self._examples.append(SolvedExample.from_dict(d))
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue

    def _save_lessons(self):
        lines = [json.dumps(l.to_dict()) for l in self._lessons.values()]
        self.lessons_file.write_text("\n".join(lines) + "\n" if lines else "")

    def _save_examples(self):
        lines = [json.dumps(e.to_dict()) for e in self._examples]
        self.examples_file.write_text("\n".join(lines) + "\n" if lines else "")

    # -----------------------------------------------------------------
    # Ingestion
    # -----------------------------------------------------------------

    def add_lesson(
        self,
        pattern: str,
        lesson_type: str,
        lesson: str,
        explanation: str = "",
        source_task: str = "",
    ):
        """
        Add or update a lesson.

        Args:
            pattern: category tag (snake_case)
            lesson_type: "prose" | "command" | "snippet"
            lesson: the content — a sentence, a command, or a code block
            explanation: brief context (always prose)
            source_task: which task.md triggered this
        """
        now = datetime.now().isoformat()
        pattern = pattern.strip().lower().replace(" ", "_")
        lesson_type = lesson_type.strip().lower()
        if lesson_type not in ("prose", "command", "snippet"):
            lesson_type = "prose"

        if pattern in self._lessons:
            existing = self._lessons[pattern]
            existing.frequency += 1
            existing.last_seen = now
            # Upgrade type if we got a more specific version
            # (prose → command → snippet is an upgrade)
            type_rank = {"prose": 0, "command": 1, "snippet": 2}
            if type_rank.get(lesson_type, 0) > type_rank.get(existing.type, 0):
                existing.type = lesson_type
                existing.lesson = lesson
                existing.explanation = explanation or existing.explanation
            elif len(lesson) > len(existing.lesson) and lesson_type == existing.type:
                existing.lesson = lesson
                if explanation:
                    existing.explanation = explanation
        else:
            self._lessons[pattern] = Lesson(
                pattern=pattern,
                type=lesson_type,
                lesson=lesson,
                explanation=explanation,
                frequency=1,
                first_seen=now,
                last_seen=now,
                source_task=source_task,
            )
        self._save_lessons()

    def add_solved_example(
        self, task_type: str, task_summary: str,
        key_decisions: str, cron_schedule: str = "",
    ):
        self._examples.append(SolvedExample(
            task_type=task_type.strip().lower().replace(" ", "_"),
            task_summary=task_summary,
            key_decisions=key_decisions,
            cron_schedule=cron_schedule or "N/A",
            timestamp=datetime.now().isoformat(),
        ))
        self._save_examples()

    # -----------------------------------------------------------------
    # Retrieval — builds TWO prompt blocks
    # -----------------------------------------------------------------

    def get_injection_blocks(
        self,
        task_text: str,
        max_frequency_items: int = 5,
        max_relevant_items: int = 5,
        max_total: int = 12,
    ) -> tuple[str, str]:
        """
        Build both prompt injection blocks:
          1. KNOWN_PITFALLS — prose lessons (advice)
          2. TOOLKIT — command/snippet lessons (code to copy)

        Returns (pitfalls_block, toolkit_block). Either may be empty string.
        """
        if not self._lessons:
            return "", ""

        selected = self._select_lessons(
            task_text, max_frequency_items, max_relevant_items, max_total
        )
        if not selected:
            return "", ""

        # Split by type
        prose_lessons = [l for l in selected if l.type == "prose"]
        code_lessons = [l for l in selected if l.type in ("command", "snippet")]

        pitfalls_block = self._format_pitfalls(prose_lessons)
        toolkit_block = self._format_toolkit(code_lessons)

        return pitfalls_block, toolkit_block

    # Keep backward compat
    def get_pitfalls_block(self, task_text: str, **kwargs) -> str:
        pitfalls, _ = self.get_injection_blocks(task_text, **kwargs)
        return pitfalls

    def get_examples_block(self, task_text: str, max_examples: int = 3) -> str:
        if not self._examples:
            return ""

        task_words = self._extract_keywords(task_text)
        if not task_words:
            return ""

        scored = []
        for ex in self._examples:
            ex_words = self._extract_keywords(
                f"{ex.task_type} {ex.task_summary} {ex.key_decisions}"
            )
            overlap = len(task_words & ex_words)
            if overlap > 0:
                scored.append((overlap, ex))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [ex for _, ex in scored[:max_examples]]
        if not top:
            return ""

        lines = ["SIMILAR TASKS SOLVED PREVIOUSLY:"]
        for ex in top:
            lines.append(
                f"  - [{ex.task_type}] {ex.task_summary} | "
                f"Strategy: {ex.key_decisions} | Cron: {ex.cron_schedule}"
            )
        return "\n".join(lines)

    # -----------------------------------------------------------------
    # Selection logic
    # -----------------------------------------------------------------

    def _select_lessons(
        self, task_text: str,
        max_frequency: int, max_relevant: int, max_total: int,
    ) -> list[Lesson]:
        selected: dict[str, Lesson] = {}

        # Top by frequency (chronic blind spots)
        by_freq = sorted(
            self._lessons.values(), key=lambda l: l.frequency, reverse=True
        )
        for lesson in by_freq[:max_frequency]:
            selected[lesson.pattern] = lesson

        # Top by keyword relevance
        task_words = self._extract_keywords(task_text)
        if task_words:
            scored = []
            for lesson in self._lessons.values():
                if lesson.pattern in selected:
                    continue
                lesson_words = self._extract_keywords(
                    f"{lesson.pattern} {lesson.lesson} {lesson.explanation}"
                )
                overlap = len(task_words & lesson_words)
                if overlap > 0:
                    scored.append((overlap, lesson))

            scored.sort(key=lambda x: x[0], reverse=True)
            for _, lesson in scored[:max_relevant]:
                if len(selected) >= max_total:
                    break
                selected[lesson.pattern] = lesson

        return sorted(selected.values(), key=lambda l: l.frequency, reverse=True)

    # -----------------------------------------------------------------
    # Formatting
    # -----------------------------------------------------------------

    @staticmethod
    def _format_pitfalls(lessons: list[Lesson]) -> str:
        if not lessons:
            return ""
        lines = ["KNOWN PITFALLS (from previous runs on this system):"]
        for l in lessons:
            freq = f" [seen {l.frequency}x]" if l.frequency > 1 else ""
            lines.append(f"  - {l.lesson}{freq}")
        return "\n".join(lines)

    @staticmethod
    def _format_toolkit(lessons: list[Lesson]) -> str:
        if not lessons:
            return ""
        lines = ["TOOLKIT (proven patterns from this system — use these):"]
        for l in lessons:
            freq = f"  # seen {l.frequency}x" if l.frequency > 1 else ""
            if l.type == "command":
                lines.append(f"  # {l.explanation}{freq}")
                lines.append(f"  {l.lesson}")
                lines.append("")
            elif l.type == "snippet":
                lines.append(f"  # {l.explanation}{freq}")
                # Indent snippet lines
                for snippet_line in l.lesson.strip().split("\n"):
                    lines.append(f"  {snippet_line}")
                lines.append("")
        return "\n".join(lines).rstrip()

    # -----------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------

    @property
    def lesson_count(self) -> int:
        return len(self._lessons)

    @property
    def example_count(self) -> int:
        return len(self._examples)

    def top_patterns(self, n: int = 10) -> list[tuple[str, int, str]]:
        """Return top patterns: (pattern, frequency, type)."""
        return [
            (l.pattern, l.frequency, l.type)
            for l in sorted(
                self._lessons.values(), key=lambda l: l.frequency, reverse=True
            )[:n]
        ]

    def stats(self) -> dict:
        by_type = {"prose": 0, "command": 0, "snippet": 0}
        for l in self._lessons.values():
            by_type[l.type] = by_type.get(l.type, 0) + 1
        return {
            "total_lessons": len(self._lessons),
            "total_examples": len(self._examples),
            "by_type": by_type,
            "total_observations": sum(l.frequency for l in self._lessons.values()),
        }

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    @staticmethod
    def _extract_keywords(text: str) -> set[str]:
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
        }
        words = set(re.findall(r"[a-z0-9_]+", text.lower()))
        return words - STOP_WORDS
