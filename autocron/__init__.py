"""
AutoCron Agent — Autonomous cron job creation via dual-LLM feedback loop.

Public API:
    from autocron import AutoCron, Judge, KnowledgeStore, Router, Creator
"""

__version__ = "0.1.0"

from .main import AutoCron, Config
from .judge import Judge, ExecutionTrace
from .knowledge import KnowledgeStore
from .convergence import ConvergenceDetector, ConvergenceConfig, RoundSignal
from .router import Router, RouteDecision
from .creator import TaskGenerator, SessionCapture
from .git_manager import GitManager

__all__ = [
    "AutoCron",
    "Config",
    "Judge",
    "ExecutionTrace",
    "KnowledgeStore",
    "ConvergenceDetector",
    "ConvergenceConfig",
    "RoundSignal",
    "Router",
    "RouteDecision",
    "TaskGenerator",
    "SessionCapture",
    "GitManager",
]
