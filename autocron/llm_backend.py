"""
LLM Backend v3 — Multi-provider dual-role agent team for AutoCron.

Two roles, each independently configurable with any provider + model:
  - Worker: Generates, fixes, and hardens bash scripts.
  - Manager: Reviews EVERY execution round. Diagnoses, reviews, extracts lessons.

Two modes:
  1. CoPaw mode: Uses CoPaw's ProviderManager (any registered provider).
  2. Standalone mode: Any OpenAI-compatible API via httpx (Ollama, LMStudio,
     llama.cpp, vLLM, litellm, cloud APIs — anything at /v1/chat/completions).

The interface is the same in both modes — AgentTeam with worker_* and manager_*.
"""

import json
import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger("autocron.llm")


# ---------------------------------------------------------------------------
# Worker Prompts
# ---------------------------------------------------------------------------

WORKER_SYSTEM_TEMPLATE = """\
You are a senior Linux system administrator. Your job is to write a bash script
that accomplishes the user's task, and optionally a cron schedule.

RULES FOR CRON-SAFE SCRIPTS:
1. Scripts run in a CRON-LIKE environment: minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin),
   no interactive shell, no tty, no user environment variables beyond HOME.
2. ALWAYS use absolute paths for every command and every file.
3. NEVER use sudo (cron has no tty for password prompts).
4. NEVER rely on ~ expansion. Use absolute paths or $HOME.
5. First line: #!/bin/bash
6. Second line: set -euo pipefail
7. Add comments explaining each step.
8. Use mkdir -p for directories that may not exist.
9. Log output with timestamps for debugging.
10. Validate inputs/preconditions before destructive operations.

{pitfalls_block}

{toolkit_block}

{examples_block}

RESPONSE FORMAT — reply with ONLY a JSON object, no markdown fences, no preamble:
{{
  "script": "#!/bin/bash\\nset -euo pipefail\\n...",
  "cron_schedule": "0 3 * * *",
  "reasoning": "Brief explanation of approach"
}}

If no cron schedule is needed, set cron_schedule to null.
"""

WORKER_INITIAL = """\
TASK:
{task}

Write a bash script to accomplish this task.
Reply with ONLY the JSON object.
"""

WORKER_FIX = """\
Your previous script FAILED. The supervisor has reviewed the full execution trace
(including bash xtrace output, environment, command resolution) and provided
detailed analysis below.

TASK:
{task}

SUPERVISOR ANALYSIS:
{manager_analysis}

You MUST address every issue the supervisor identified.
Reply with ONLY the JSON object.
"""

WORKER_HARDEN = """\
Your previous script PASSED (exit code 0), but the supervisor found issues
during code review that must be fixed before deployment.

TASK:
{task}

SUPERVISOR REVIEW:
{manager_analysis}

Fix all issues identified. The script must still pass execution.
Reply with ONLY the JSON object.
"""


# ---------------------------------------------------------------------------
# Manager Prompts
# ---------------------------------------------------------------------------

MANAGER_SYSTEM = """\
You are a principal systems engineer reviewing automated script generation.
You receive COMPLETE execution traces including bash xtrace output (with
timestamps and line numbers via PS4), environment variables, command resolution
maps, and file permissions.

You have three responsibilities on EVERY round:

1. DIAGNOSE failures: identify root cause from the xtrace, not guessing.
   The xtrace shows you exactly which command failed and what state the
   variables were in at that point. Quote specific xtrace lines.

2. REVIEW successes: a passing exit code is necessary but not sufficient.
   Look for:
   - Silent failures (command succeeds but does the wrong thing)
   - Race conditions, TOCTOU bugs
   - Missing error handling on critical operations
   - Security issues (world-writable files, unvalidated inputs)
   - Fragility (hardcoded values, assumptions about disk space, etc.)
   - Temp file leaks, missing cleanup
   - Unnecessary complexity

3. EXTRACT a lesson: every review produces exactly one lesson for the
   knowledge base. The lesson must be REUSABLE, not task-specific.

   CRITICAL: Choose the right lesson type. If the lesson is best expressed
   as a command or code pattern, express it as CODE, not prose.

   Lesson types:
   - "prose": A principle or rule in natural language.
     Example: "Always validate that backup files are non-empty before deleting old backups."
   - "command": A single command or one-liner that solves a recurring problem.
     Example: "mktemp -d /tmp/autocron.XXXXXX"
   - "snippet": A multi-line code pattern (2-6 lines) for a recurring need.
     Example: "exec 200>/var/lock/myscript.lock\\nflock -n 200 || { echo 'Already running'; exit 1; }"

   Prefer "command" or "snippet" when the lesson IS a technique or pattern.
   Use "prose" only when the lesson is truly about judgment or principles.

RESPONSE FORMAT — reply with ONLY a JSON object, no markdown:
{
  "verdict": "fail" | "pass_with_issues" | "approved",
  "analysis": "Detailed analysis referencing specific xtrace lines, variable values, and error messages. Be precise enough that the Worker can fix the issue without guessing.",
  "lesson_pattern": "short_snake_case_tag",
  "lesson_type": "prose" | "command" | "snippet",
  "lesson_content": "The lesson itself: a sentence, a command, or a code block.",
  "lesson_explanation": "One sentence explaining when/why to use this. Always prose."
}

verdict meanings:
  - "fail": script crashed, wrong exit code, or did not accomplish the task
  - "pass_with_issues": script ran successfully but has robustness/security issues
  - "approved": script is production-ready, no changes needed
"""

MANAGER_REVIEW_TEMPLATE = """\
TASK GOAL:
{task}

ROUND: {round_num} of this run
HISTORY OF PREVIOUS ROUNDS:
{history_summary}

{execution_trace}

Analyze this execution thoroughly. Provide your verdict, detailed analysis,
and one extracted lesson.
Reply with ONLY the JSON object.
"""


# ---------------------------------------------------------------------------
# LLM Call Interface (abstracts provider differences)
# ---------------------------------------------------------------------------

class LLMEndpoint:
    """
    A single LLM endpoint — wraps either a CoPaw provider or an
    OpenAI-compatible API URL.
    """

    def __init__(
        self,
        *,
        # Standalone mode (OpenAI-compatible)
        url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        # CoPaw mode
        copaw_provider_id: Optional[str] = None,
        copaw_model_id: Optional[str] = None,
        # Shared
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: int = 180,
    ):
        self.url = url.rstrip("/") if url else None
        self.model = model
        self.api_key = api_key
        self.copaw_provider_id = copaw_provider_id
        self.copaw_model_id = copaw_model_id
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

        # Will be lazily resolved if using CoPaw
        self._copaw_model_instance = None

        # Shared httpx client for standalone mode
        self._client = httpx.Client(timeout=timeout) if self.url else None

    @property
    def description(self) -> str:
        """Human-readable description of this endpoint."""
        if self.copaw_provider_id:
            return f"{self.copaw_provider_id}/{self.copaw_model_id}"
        return f"{self.url} / {self.model}"

    def call(self, system: str, prompt: str) -> str:
        """
        Send a system+user prompt to this LLM and return the raw response text.
        Tries CoPaw provider first, then falls back to OpenAI-compatible API.
        """
        if self.copaw_provider_id:
            return self._call_copaw(system, prompt)

        if self.url:
            # Try OpenAI-compatible API first
            result = self._call_openai_compat(system, prompt)
            if result is not None:
                return result

            # Fallback: try Ollama native API
            result = self._call_ollama_native(system, prompt)
            if result is not None:
                return result

        return json.dumps({
            "error": "No LLM endpoint configured or reachable",
        })

    def _call_copaw(self, system: str, prompt: str) -> str:
        """Call via CoPaw's ProviderManager."""
        try:
            if self._copaw_model_instance is None:
                from copaw.providers import ProviderManager
                pm = ProviderManager.get_instance()
                provider = pm.get_provider(self.copaw_provider_id)
                self._copaw_model_instance = provider.get_chat_model_instance(
                    self.copaw_model_id
                )

            # AgentScope ChatModelBase expects Msg objects, but we can
            # use the underlying client directly for simpler integration.
            # For now, use the OpenAI-compatible interface that all
            # CoPaw providers expose.
            model = self._copaw_model_instance

            # Most CoPaw models wrap OpenAI-compatible chat, so we can
            # construct messages directly.
            from agentscope.msg import Msg
            sys_msg = Msg(name="system", content=system, role="system")
            user_msg = Msg(name="user", content=prompt, role="user")
            response = model([sys_msg, user_msg])
            return response.content if hasattr(response, 'content') else str(response)

        except ImportError:
            logger.warning(
                "CoPaw not installed. Install with: pip install copaw"
            )
            return json.dumps({"error": "CoPaw not installed"})
        except Exception as e:
            logger.error("CoPaw provider call failed: %s", e)
            return json.dumps({"error": f"CoPaw call failed: {e}"})

    def _call_openai_compat(self, system: str, prompt: str) -> Optional[str]:
        """Call any OpenAI-compatible /v1/chat/completions endpoint."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }

        # Determine the URL — add /v1/chat/completions if not already there
        url = self.url
        if not url.endswith("/chat/completions"):
            if url.endswith("/v1"):
                url = f"{url}/chat/completions"
            else:
                url = f"{url}/v1/chat/completions"

        try:
            resp = self._client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.debug("OpenAI-compat call to %s failed: %s", url, e)
            return None

    def _call_ollama_native(self, system: str, prompt: str) -> Optional[str]:
        """Fallback: try Ollama's native /api/chat endpoint."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens},
        }

        try:
            resp = self._client.post(f"{self.url}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "")
        except Exception as e:
            logger.debug("Ollama native call failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Agent Team
# ---------------------------------------------------------------------------

class AgentTeam:
    """
    Dual-role LLM team for AutoCron.

    Each role (Worker, Manager) can use a different provider and model.

    Standalone mode:
        team = AgentTeam(
            worker_url="http://localhost:11434",
            worker_model="qwen3:27b",
            manager_url="https://api.anthropic.com",
            manager_model="claude-sonnet-4-20250514",
            manager_api_key=os.environ["ANTHROPIC_API_KEY"],
        )

    CoPaw mode:
        team = AgentTeam(
            worker_copaw_provider="ollama",
            worker_copaw_model="qwen3:27b",
            manager_copaw_provider="anthropic",
            manager_copaw_model="claude-sonnet-4-20250514",
        )
    """

    def __init__(
        self,
        # --- Worker (standalone) ---
        worker_url: str = "http://localhost:11434",
        worker_model: str = "qwen3:27b",
        worker_api_key: Optional[str] = None,
        # --- Manager (standalone) ---
        manager_url: Optional[str] = None,
        manager_model: str = "claude-sonnet-4-20250514",
        manager_api_key: Optional[str] = None,
        manager_provider: Optional[str] = None,  # Legacy compat: "anthropic" | "openai"
        # --- CoPaw mode ---
        worker_copaw_provider: Optional[str] = None,
        worker_copaw_model: Optional[str] = None,
        manager_copaw_provider: Optional[str] = None,
        manager_copaw_model: Optional[str] = None,
    ):
        # Resolve legacy manager_provider to URL
        if manager_url is None and manager_provider:
            if manager_provider == "anthropic":
                manager_url = "https://api.anthropic.com"
                manager_api_key = manager_api_key or os.environ.get("ANTHROPIC_API_KEY")
            elif manager_provider == "openai":
                manager_url = "https://api.openai.com"
                manager_api_key = manager_api_key or os.environ.get("OPENAI_API_KEY")

        # Also check env vars for standalone mode
        worker_url = os.environ.get("AUTOCRON_WORKER_URL", worker_url)
        worker_model = os.environ.get("AUTOCRON_WORKER_MODEL", worker_model)
        worker_api_key = worker_api_key or os.environ.get("AUTOCRON_WORKER_API_KEY")
        if manager_url is None:
            manager_url = os.environ.get("AUTOCRON_MANAGER_URL")
        manager_model = os.environ.get("AUTOCRON_MANAGER_MODEL", manager_model)
        manager_api_key = manager_api_key or os.environ.get("AUTOCRON_MANAGER_API_KEY")

        # Build endpoints
        if worker_copaw_provider:
            self.worker = LLMEndpoint(
                copaw_provider_id=worker_copaw_provider,
                copaw_model_id=worker_copaw_model,
                temperature=0.3,
                max_tokens=4096,
            )
        else:
            self.worker = LLMEndpoint(
                url=worker_url,
                model=worker_model,
                api_key=worker_api_key,
                temperature=0.3,
                max_tokens=4096,
            )

        if manager_copaw_provider:
            self.manager = LLMEndpoint(
                copaw_provider_id=manager_copaw_provider,
                copaw_model_id=manager_copaw_model,
                temperature=0.2,
                max_tokens=2000,
            )
        elif manager_url:
            self.manager = LLMEndpoint(
                url=manager_url,
                model=manager_model,
                api_key=manager_api_key,
                temperature=0.2,
                max_tokens=2000,
            )
        else:
            # Fallback: use worker endpoint for manager too
            logger.warning(
                "No manager endpoint configured. Using worker endpoint for both roles."
            )
            self.manager = LLMEndpoint(
                url=worker_url,
                model=manager_model or worker_model,
                api_key=worker_api_key,
                temperature=0.2,
                max_tokens=2000,
            )

        logger.info(
            "AgentTeam initialized — Worker: %s | Manager: %s",
            self.worker.description, self.manager.description,
        )

    # ------------------------------------------------------------------
    # Worker (three modes: generate, fix, harden)
    # ------------------------------------------------------------------

    def worker_generate(self, task: str, pitfalls_block: str = "",
                        toolkit_block: str = "", examples_block: str = "") -> dict:
        system = WORKER_SYSTEM_TEMPLATE.format(
            pitfalls_block=pitfalls_block, toolkit_block=toolkit_block,
            examples_block=examples_block,
        )
        prompt = WORKER_INITIAL.format(task=task)
        raw = self.worker.call(system, prompt)
        return self._parse_worker_response(raw)

    def worker_fix(self, task: str, manager_analysis: str,
                   pitfalls_block: str = "", toolkit_block: str = "",
                   examples_block: str = "") -> dict:
        system = WORKER_SYSTEM_TEMPLATE.format(
            pitfalls_block=pitfalls_block, toolkit_block=toolkit_block,
            examples_block=examples_block,
        )
        prompt = WORKER_FIX.format(task=task, manager_analysis=manager_analysis)
        raw = self.worker.call(system, prompt)
        return self._parse_worker_response(raw)

    def worker_harden(self, task: str, manager_analysis: str,
                      pitfalls_block: str = "", toolkit_block: str = "",
                      examples_block: str = "") -> dict:
        system = WORKER_SYSTEM_TEMPLATE.format(
            pitfalls_block=pitfalls_block, toolkit_block=toolkit_block,
            examples_block=examples_block,
        )
        prompt = WORKER_HARDEN.format(task=task, manager_analysis=manager_analysis)
        raw = self.worker.call(system, prompt)
        return self._parse_worker_response(raw)

    # ------------------------------------------------------------------
    # Manager (reviews every round)
    # ------------------------------------------------------------------

    def manager_review(self, task: str, execution_trace_report: str,
                       round_num: int, history_summary: str = "First round.") -> dict:
        prompt = MANAGER_REVIEW_TEMPLATE.format(
            task=task, round_num=round_num,
            history_summary=history_summary,
            execution_trace=execution_trace_report,
        )
        raw = self.manager.call(MANAGER_SYSTEM, prompt)
        return self._parse_manager_response(raw)

    # ------------------------------------------------------------------
    # Parsing (robust JSON extraction from LLM output)
    # ------------------------------------------------------------------

    def _parse_worker_response(self, raw: str) -> dict:
        cleaned = re.sub(r"```(?:json)?\s*", "", raw)
        cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL)
        cleaned = cleaned.strip()

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and "script" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

        json_match = re.search(r'\{.*"script".*\}', raw, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        script_match = re.search(r"```(?:bash|sh)?\s*\n(.*?)```", raw, re.DOTALL)
        if script_match:
            return {
                "script": script_match.group(1).strip(),
                "cron_schedule": None,
                "reasoning": "Extracted from code block (JSON parse failed)",
            }

        return {
            "script": "", "cron_schedule": None,
            "reasoning": f"Parse failed. Raw length: {len(raw)}",
        }

    def _parse_manager_response(self, raw: str) -> dict:
        cleaned = re.sub(r"```(?:json)?\s*", "", raw)
        cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE)
        cleaned = cleaned.strip()

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and "verdict" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

        json_match = re.search(r'\{.*"verdict".*\}', raw, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return {
            "verdict": "fail", "analysis": raw[:3000],
            "lesson_pattern": "parse_error",
            "lesson_text": "Manager response was not valid JSON.",
        }
