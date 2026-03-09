"""Tests for the Router — task routing decisions."""
import tempfile
import pytest
from autocron.knowledge import KnowledgeStore
from autocron.router import Router


@pytest.fixture
def store():
    d = tempfile.mkdtemp()
    return KnowledgeStore(store_dir=d)


@pytest.fixture
def router(store):
    return Router(knowledge_store=store)


def test_new_task_full_loop(router):
    """A completely new task should route to full_loop."""
    decision = router.route("something never seen before")
    assert decision.path == "full_loop"


def test_route_returns_decision(router):
    """Route should always return a RouteDecision."""
    decision = router.route("backup my database")
    assert hasattr(decision, "path")
    assert hasattr(decision, "explanation")
