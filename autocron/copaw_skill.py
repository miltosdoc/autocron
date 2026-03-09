"""
AutoCron Skill for CoPaw — Self-Correcting Platform Integration.

This skill does three things:
  1. Exposes AutoCron's full pipeline (Creator → Router → Loop → Deploy)
     as a CoPaw skill invocable from any messaging channel.
  2. Observes CoPaw's own behavior (intent routing, tool calls, cron execution,
     channel delivery) and feeds failures into AutoCron's knowledge store.
  3. Uses accumulated platform knowledge to self-correct CoPaw via:
     - Prompt patches (injected into CoPaw's agent system prompt)
     - Config patches (applied to CoPaw's settings)
     - Skill generation (new skills auto-created to fill capability gaps)

The skill treats CoPaw the same way AutoCron treats bash scripts:
something to be observed, diagnosed, and iteratively improved.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# AutoCron core imports (these live alongside this file)
from .knowledge import KnowledgeStore, Lesson
from .judge import Judge
from .convergence import ConvergenceDetector, ConvergenceConfig, RoundSignal
from .router import Router

logger = logging.getLogger("autocron.skill")


# ═══════════════════════════════════════════════════════════════════
# 1. PLATFORM OBSERVER — watches CoPaw's behavior
# ═══════════════════════════════════════════════════════════════════

@dataclass
class PlatformEvent:
    """An observable event from the host platform (CoPaw or any other)."""
    timestamp: str
    event_type: str         # "intent_route" | "tool_call" | "cron_exec" |
                            # "channel_send" | "agent_response" | "error"
    success: bool
    input_text: str = ""    # what the user said
    output_text: str = ""   # what the platform produced
    error_detail: str = ""  # stack trace or error message
    metadata: dict = field(default_factory=dict)  # platform-specific context


class PlatformObserver:
    """
    Observes and records platform behavior for pattern analysis.

    Works with any platform — CoPaw, OpenClaw, or a custom adapter.
    The observer doesn't import the platform; it receives events via
    a push API. The platform's hooks/middleware call observer.record().
    """

    def __init__(
        self,
        knowledge: KnowledgeStore,
        event_log_dir: str = "~/.autocron/platform_events",
        analysis_buffer_size: int = 20,
    ):
        self.knowledge = knowledge
        self.event_log_dir = Path(os.path.expanduser(event_log_dir))
        self.event_log_dir.mkdir(parents=True, exist_ok=True)
        self.buffer: list[PlatformEvent] = []
        self.buffer_size = analysis_buffer_size
        self._event_log = self.event_log_dir / "events.jsonl"

    def record(self, event: PlatformEvent):
        """Record a platform event. Called by platform hooks."""
        # Persist raw event
        with open(self._event_log, "a") as f:
            f.write(json.dumps({
                "timestamp": event.timestamp,
                "type": event.event_type,
                "success": event.success,
                "input": event.input_text[:500],
                "output": event.output_text[:500],
                "error": event.error_detail[:1000],
                "meta": event.metadata,
            }) + "\n")

        self.buffer.append(event)

        # When buffer is full, analyze for patterns
        if len(self.buffer) >= self.buffer_size:
            patterns = self._analyze_buffer()
            self.buffer.clear()
            return patterns
        return []

    def _analyze_buffer(self) -> list[dict]:
        """
        Analyze buffered events for recurring failure patterns.
        Returns list of detected patterns for the Manager to review.
        """
        patterns = []

        # Group failures by type
        failures = [e for e in self.buffer if not e.success]
        if not failures:
            return patterns

        failure_rate = len(failures) / len(self.buffer)
        if failure_rate > 0.3:
            patterns.append({
                "pattern": "high_failure_rate",
                "rate": failure_rate,
                "sample_errors": [f.error_detail[:200] for f in failures[:5]],
            })

        # Detect repeated identical errors
        error_counts: dict[str, int] = {}
        for f in failures:
            # Normalize error: take first line, strip variable content
            key = f.error_detail.split("\n")[0][:100] if f.error_detail else f.event_type
            error_counts[key] = error_counts.get(key, 0) + 1

        for error_key, count in error_counts.items():
            if count >= 3:
                patterns.append({
                    "pattern": "repeated_error",
                    "error": error_key,
                    "count": count,
                    "event_type": failures[0].event_type,
                })

        # Detect intent misrouting (user asked for X, got unrelated Y)
        intent_failures = [
            e for e in failures if e.event_type == "intent_route"
        ]
        if len(intent_failures) >= 2:
            patterns.append({
                "pattern": "intent_misroute",
                "examples": [
                    {"input": e.input_text[:100], "output": e.output_text[:100]}
                    for e in intent_failures[:3]
                ],
            })

        # Detect tool call failures
        tool_failures = [
            e for e in failures if e.event_type == "tool_call"
        ]
        if tool_failures:
            tool_names = [
                e.metadata.get("tool_name", "unknown") for e in tool_failures
            ]
            patterns.append({
                "pattern": "tool_failure",
                "tools": list(set(tool_names)),
                "count": len(tool_failures),
            })

        return patterns

    def get_failure_summary(self, last_n: int = 100) -> str:
        """Get a summary of recent platform failures for Manager review."""
        if not self._event_log.exists():
            return "No platform events recorded yet."

        lines = self._event_log.read_text().strip().split("\n")
        recent = lines[-last_n:] if len(lines) >= last_n else lines

        failures = []
        total = 0
        for line in recent:
            try:
                event = json.loads(line)
                total += 1
                if not event.get("success"):
                    failures.append(event)
            except json.JSONDecodeError:
                continue

        if not failures:
            return f"No failures in last {total} events."

        summary_lines = [
            f"Platform failure summary: {len(failures)} failures in {total} events "
            f"({len(failures)/total*100:.0f}% failure rate)",
            "",
        ]

        # Group by type
        by_type: dict[str, list] = {}
        for f in failures:
            t = f.get("type", "unknown")
            by_type.setdefault(t, []).append(f)

        for event_type, events in by_type.items():
            summary_lines.append(f"  [{event_type}] {len(events)} failures:")
            for e in events[:3]:
                summary_lines.append(
                    f"    Input: {e.get('input', '')[:80]}"
                )
                summary_lines.append(
                    f"    Error: {e.get('error', '')[:120]}"
                )
            if len(events) > 3:
                summary_lines.append(f"    ... and {len(events)-3} more")

        return "\n".join(summary_lines)


# ═══════════════════════════════════════════════════════════════════
# 2. PLATFORM CORRECTOR — applies accumulated knowledge to fix the host
# ═══════════════════════════════════════════════════════════════════

@dataclass
class CorrectionAction:
    """A proposed correction to the host platform."""
    action_type: str      # "prompt_patch" | "config_patch" | "new_skill" | "advisory"
    target: str           # what to modify (e.g., "system_prompt", "config.yaml", skill name)
    content: str          # the patch content
    explanation: str      # why this correction is needed
    confidence: float     # 0.0-1.0, based on evidence frequency
    auto_apply: bool      # safe to apply without user confirmation?


class PlatformCorrector:
    """
    Generates corrections for the host platform based on knowledge store patterns.

    Three correction types:
      1. Prompt patches: modifications to the platform agent's system prompt
      2. Config patches: changes to platform configuration
      3. New skills: generated Python files for capability gaps
    """

    # Patterns that map to prompt corrections
    PROMPT_CORRECTABLE = {
        "intent_misroute", "hallucinated_tool", "wrong_format",
        "missing_confirmation", "unsafe_assumption", "context_loss",
    }

    # Patterns that map to config corrections
    CONFIG_CORRECTABLE = {
        "timeout_too_short", "rate_limit_hit", "timezone_mismatch",
        "memory_overflow", "model_mismatch", "channel_config_error",
    }

    def __init__(
        self,
        knowledge: KnowledgeStore,
        copaw_dir: str = "~/.copaw",
    ):
        self.knowledge = knowledge
        self.copaw_dir = Path(os.path.expanduser(copaw_dir))
        self.corrections_log = (
            self.copaw_dir / "autocron_corrections.jsonl"
        )

    def generate_corrections(self) -> list[CorrectionAction]:
        """
        Scan the knowledge store for platform-related patterns and
        generate correction actions.
        """
        corrections = []

        for pattern, lesson in self.knowledge._lessons.items():
            # Only consider lessons with enough evidence
            if lesson.frequency < 2:
                continue

            if pattern in self.PROMPT_CORRECTABLE:
                corrections.append(self._make_prompt_patch(lesson))
            elif pattern in self.CONFIG_CORRECTABLE:
                corrections.append(self._make_config_patch(lesson))
            elif pattern.startswith("capability_gap_"):
                corrections.append(self._make_skill_proposal(lesson))

        return corrections

    def get_prompt_injection(self) -> str:
        """
        Build a prompt injection block for the host platform's agent.

        This is analogous to the PITFALLS + TOOLKIT blocks injected into
        AutoCron's Worker, but targeted at the platform's agent behavior.

        The platform should include this in its system prompt via a hook.
        """
        # Select platform-relevant lessons
        platform_lessons = [
            l for l in self.knowledge._lessons.values()
            if l.pattern in self.PROMPT_CORRECTABLE
            or l.pattern.startswith("platform_")
            or l.pattern.startswith("copaw_")
            or l.pattern.startswith("agent_")
        ]

        if not platform_lessons:
            return ""

        # Sort by frequency
        platform_lessons.sort(key=lambda l: l.frequency, reverse=True)

        lines = [
            "PLATFORM CORRECTIONS (learned from observed failures):"
        ]

        for lesson in platform_lessons[:8]:
            freq = f" [seen {lesson.frequency}x]" if lesson.frequency > 1 else ""
            if lesson.type == "prose":
                lines.append(f"  - {lesson.lesson}{freq}")
            elif lesson.type in ("command", "snippet"):
                lines.append(f"  - {lesson.explanation}{freq}")
                for code_line in lesson.lesson.strip().split("\n"):
                    lines.append(f"    {code_line}")

        return "\n".join(lines)

    def apply_correction(
        self, correction: CorrectionAction, dry_run: bool = True,
    ) -> dict:
        """
        Apply a correction to the platform.

        Returns {"applied": bool, "detail": str}
        """
        result = {"applied": False, "detail": "", "correction": correction.action_type}

        if dry_run:
            result["detail"] = f"[DRY RUN] Would apply: {correction.explanation}"
            self._log_correction(correction, applied=False)
            return result

        if correction.action_type == "prompt_patch":
            result = self._apply_prompt_patch(correction)
        elif correction.action_type == "config_patch":
            result = self._apply_config_patch(correction)
        elif correction.action_type == "new_skill":
            result = self._apply_new_skill(correction)
        elif correction.action_type == "advisory":
            result["detail"] = f"Advisory: {correction.explanation}"

        self._log_correction(correction, applied=result.get("applied", False))
        return result

    # ------------------------------------------------------------------
    # Correction generators
    # ------------------------------------------------------------------

    def _make_prompt_patch(self, lesson: Lesson) -> CorrectionAction:
        return CorrectionAction(
            action_type="prompt_patch",
            target="agent_system_prompt",
            content=lesson.lesson,
            explanation=f"[{lesson.pattern}] {lesson.explanation or lesson.lesson}",
            confidence=min(1.0, lesson.frequency / 5),
            auto_apply=lesson.frequency >= 3,  # auto-apply if seen 3+ times
        )

    def _make_config_patch(self, lesson: Lesson) -> CorrectionAction:
        return CorrectionAction(
            action_type="config_patch",
            target="copaw_config",
            content=lesson.lesson,
            explanation=f"[{lesson.pattern}] {lesson.explanation or lesson.lesson}",
            confidence=min(1.0, lesson.frequency / 5),
            auto_apply=False,  # config changes always need user confirmation
        )

    def _make_skill_proposal(self, lesson: Lesson) -> CorrectionAction:
        return CorrectionAction(
            action_type="new_skill",
            target=lesson.pattern.replace("capability_gap_", ""),
            content=lesson.lesson,  # may be a code snippet
            explanation=f"Detected capability gap: {lesson.explanation}",
            confidence=min(1.0, lesson.frequency / 5),
            auto_apply=False,  # new skills always need user confirmation
        )

    # ------------------------------------------------------------------
    # Correction appliers
    # ------------------------------------------------------------------

    def _apply_prompt_patch(self, correction: CorrectionAction) -> dict:
        """Inject correction into CoPaw's prompt via the hook system."""
        self.copaw_dir.mkdir(parents=True, exist_ok=True)
        patch_file = self.copaw_dir / "autocron_prompt_patches.json"
        existing = []
        if patch_file.exists():
            try:
                existing = json.loads(patch_file.read_text())
            except json.JSONDecodeError:
                existing = []

        # Deduplicate
        if correction.content not in [p.get("content") for p in existing]:
            existing.append({
                "content": correction.content,
                "pattern": correction.target,
                "added": datetime.now().isoformat(),
                "explanation": correction.explanation,
            })
            patch_file.write_text(json.dumps(existing, indent=2))

        return {
            "applied": True,
            "detail": f"Prompt patch added. {len(existing)} total patches.",
        }

    def _apply_config_patch(self, correction: CorrectionAction) -> dict:
        """Propose a config change (never auto-applied)."""
        self.copaw_dir.mkdir(parents=True, exist_ok=True)
        proposals_file = self.copaw_dir / "autocron_config_proposals.json"
        existing = []
        if proposals_file.exists():
            try:
                existing = json.loads(proposals_file.read_text())
            except json.JSONDecodeError:
                existing = []

        existing.append({
            "proposed": correction.content,
            "explanation": correction.explanation,
            "timestamp": datetime.now().isoformat(),
            "status": "pending",
        })
        proposals_file.write_text(json.dumps(existing, indent=2))

        return {
            "applied": False,
            "detail": f"Config proposal saved for user review. {len(existing)} pending.",
        }

    def _apply_new_skill(self, correction: CorrectionAction) -> dict:
        """Generate a new CoPaw skill file from the correction."""
        skills_dir = self.copaw_dir / "customized_skills" / correction.target
        skills_dir.mkdir(parents=True, exist_ok=True)

        # Write SKILL.md
        skill_md = (
            f"---\n"
            f"name: {correction.target}\n"
            f"description: {correction.explanation}\n"
            f"---\n\n"
            f"# {correction.target}\n\n"
            f"Auto-generated by AutoCron to address a detected capability gap.\n\n"
            f"## Purpose\n\n{correction.explanation}\n"
        )
        (skills_dir / "SKILL.md").write_text(skill_md)

        # Write the skill implementation if we have code
        if correction.content and (
            correction.content.strip().startswith("def ")
            or correction.content.strip().startswith("import ")
            or correction.content.strip().startswith("#!")
        ):
            (skills_dir / f"{correction.target}.py").write_text(correction.content)

        return {
            "applied": True,
            "detail": f"New skill created at {skills_dir}. Restart CoPaw to activate.",
        }

    def _log_correction(self, correction: CorrectionAction, applied: bool):
        """Append correction to the audit log."""
        self.corrections_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.corrections_log, "a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now().isoformat(),
                "action": correction.action_type,
                "target": correction.target,
                "explanation": correction.explanation,
                "confidence": correction.confidence,
                "applied": applied,
            }) + "\n")


# ═══════════════════════════════════════════════════════════════════
# 3. COPAW SKILL INTERFACE — the entry point CoPaw loads
# ═══════════════════════════════════════════════════════════════════

class AutoCronSkill:
    """
    The main skill class that CoPaw loads.

    Handles three categories of user requests:
      1. Task automation: "back up my database nightly"
         → Creator → Router → AutoCron loop → deploy
      2. Status queries: "what cron jobs are running?"
         → Router.list_available_solutions()
      3. Platform health: "how is the system performing?"
         → Observer.get_failure_summary() + Corrector.generate_corrections()

    Also runs passively via CoPaw hooks to observe and self-correct.
    """

    def __init__(
        self,
        knowledge_dir: str = "~/.autocron/knowledge",
        copaw_dir: str = "~/.copaw",
        worker_url: str = "http://localhost:11434",
        worker_model: str = "qwen3:27b",
        manager_provider: str = "anthropic",
        manager_model: str = "claude-sonnet-4-20250514",
    ):
        self.knowledge = KnowledgeStore(store_dir=knowledge_dir)
        self.observer = PlatformObserver(knowledge=self.knowledge)
        self.corrector = PlatformCorrector(
            knowledge=self.knowledge, copaw_dir=copaw_dir,
        )
        self.router = Router(knowledge_store=self.knowledge)

        # Store config for AutoCron loop invocation
        self._worker_url = worker_url
        self._worker_model = worker_model
        self._manager_provider = manager_provider
        self._manager_model = manager_model

    # ------------------------------------------------------------------
    # CoPaw hook: called on every agent response (passive observation)
    # ------------------------------------------------------------------

    def on_agent_response(
        self,
        user_input: str,
        agent_output: str,
        success: bool = True,
        error: str = "",
        metadata: Optional[dict] = None,
    ):
        """
        Hook for CoPaw to call after every agent response.
        Records the event and checks for self-correction opportunities.

        Integration in CoPaw:
          agent.register_instance_hook("post_reply", "autocron_observe",
              lambda msg: autocron_skill.on_agent_response(...))
        """
        event = PlatformEvent(
            timestamp=datetime.now().isoformat(),
            event_type="agent_response",
            success=success,
            input_text=user_input,
            output_text=agent_output,
            error_detail=error,
            metadata=metadata or {},
        )

        patterns = self.observer.record(event)

        # If patterns detected, feed them to the knowledge store
        if patterns:
            for p in patterns:
                pattern_name = f"platform_{p['pattern']}"
                self.knowledge.add_lesson(
                    pattern=pattern_name,
                    lesson_type="prose",
                    lesson=json.dumps(p, default=str)[:300],
                    explanation=f"Platform pattern detected: {p['pattern']}",
                    source_task="platform_observation",
                )

    def on_tool_call(
        self,
        tool_name: str,
        tool_input: dict,
        tool_output: Any,
        success: bool,
        error: str = "",
    ):
        """Hook for CoPaw tool call observation."""
        self.observer.record(PlatformEvent(
            timestamp=datetime.now().isoformat(),
            event_type="tool_call",
            success=success,
            input_text=json.dumps(tool_input, default=str)[:300],
            output_text=str(tool_output)[:300],
            error_detail=error,
            metadata={"tool_name": tool_name},
        ))

    def on_cron_execution(
        self,
        job_name: str,
        success: bool,
        output: str = "",
        error: str = "",
    ):
        """Hook for CoPaw cron job execution observation."""
        self.observer.record(PlatformEvent(
            timestamp=datetime.now().isoformat(),
            event_type="cron_exec",
            success=success,
            input_text=job_name,
            output_text=output[:500],
            error_detail=error,
            metadata={"job_name": job_name},
        ))

    # ------------------------------------------------------------------
    # User-facing commands
    # ------------------------------------------------------------------

    def handle_request(self, user_message: str) -> str:
        """
        Main entry point for user messages routed to this skill.

        Returns a response string to send back via the messaging channel.
        """
        msg = user_message.lower().strip()

        # Status queries
        if any(w in msg for w in ["status", "jobs", "list", "what's running"]):
            return self._handle_status()

        # Health / diagnostics
        if any(w in msg for w in ["health", "diagnose", "platform", "self-check"]):
            return self._handle_health()

        # Corrections
        if any(w in msg for w in ["fix yourself", "self-correct", "improve"]):
            return self._handle_self_correct()

        # Default: treat as a new automation task
        return self._handle_new_task(user_message)

    def _handle_new_task(self, task_description: str) -> str:
        """Route and potentially execute a new automation task."""
        route = self.router.route(task_description)

        if route.path == "deploy" and route.matched_script:
            return (
                f"I found an existing solution (confidence: {route.confidence:.0%}).\n"
                f"Script: {route.matched_script}\n"
                f"Shall I deploy it directly, or would you like me to "
                f"run the full optimization loop?"
            )

        elif route.path == "adapt":
            return (
                f"I found a similar solved task (confidence: {route.confidence:.0%}).\n"
                f"Strategy: {route.matched_example.get('key_decisions', 'N/A')}\n"
                f"I'll use this as a starting point and optimize from there.\n"
                f"Starting AutoCron loop..."
            )
            # In production, this would trigger:
            # from main import AutoCron, Config
            # config = Config(task_file=..., ...)
            # state = AutoCron(config).run()

        else:
            return (
                f"This is a new task type (no prior solutions found).\n"
                f"I'll run the full AutoCron optimization loop.\n"
                f"Starting from scratch..."
            )

    def _handle_status(self) -> str:
        """List current solutions and knowledge stats."""
        solutions = self.router.list_available_solutions()
        stats = self.knowledge.stats()

        lines = [f"AutoCron Knowledge: {stats['total_lessons']} lessons "
                 f"({stats['by_type'].get('prose', 0)} prose, "
                 f"{stats['by_type'].get('command', 0)} commands, "
                 f"{stats['by_type'].get('snippet', 0)} snippets)"]
        lines.append(f"Solved examples: {stats['total_examples']}")
        lines.append(f"Total observations: {stats['total_observations']}")

        if solutions:
            lines.append(f"\nAvailable solutions ({len(solutions)}):")
            for s in solutions[:10]:
                lines.append(f"  [{s['type']}] {s['name']}: {s.get('description', '')[:60]}")

        return "\n".join(lines)

    def _handle_health(self) -> str:
        """Report platform health and detected issues."""
        summary = self.observer.get_failure_summary()
        corrections = self.corrector.generate_corrections()

        lines = [summary]

        if corrections:
            lines.append(f"\nProposed corrections ({len(corrections)}):")
            for c in corrections:
                auto_tag = " [auto-applicable]" if c.auto_apply else " [needs confirmation]"
                lines.append(
                    f"  [{c.action_type}] {c.explanation[:80]}"
                    f" (confidence: {c.confidence:.0%}){auto_tag}"
                )
        else:
            lines.append("\nNo corrections needed at this time.")

        return "\n".join(lines)

    def _handle_self_correct(self) -> str:
        """Apply accumulated corrections to the platform."""
        corrections = self.corrector.generate_corrections()

        if not corrections:
            return "No corrections available. The platform is operating within observed parameters."

        applied = []
        pending = []

        for c in corrections:
            if c.auto_apply and c.confidence >= 0.6:
                result = self.corrector.apply_correction(c, dry_run=False)
                if result.get("applied"):
                    applied.append(c)
                else:
                    pending.append(c)
            else:
                pending.append(c)

        lines = []
        if applied:
            lines.append(f"Applied {len(applied)} corrections:")
            for c in applied:
                lines.append(f"  ✅ [{c.action_type}] {c.explanation[:80]}")

        if pending:
            lines.append(f"\n{len(pending)} corrections need your approval:")
            for c in pending:
                lines.append(
                    f"  ⏳ [{c.action_type}] {c.explanation[:80]}"
                    f" (confidence: {c.confidence:.0%})"
                )

        prompt_block = self.corrector.get_prompt_injection()
        if prompt_block:
            lines.append(f"\nActive prompt corrections: {prompt_block.count(chr(10))} lines")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # CoPaw prompt hook: inject platform corrections
    # ------------------------------------------------------------------

    def get_system_prompt_additions(self) -> str:
        """
        Called by CoPaw's prompt hook to get AutoCron's corrections.

        Integration:
          agent.register_instance_hook("pre_reasoning", "autocron_prompt",
              lambda: autocron_skill.get_system_prompt_additions())
        """
        return self.corrector.get_prompt_injection()
