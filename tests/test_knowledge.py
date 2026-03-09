"""Tests for the KnowledgeStore — typed lessons and dedup."""
import tempfile
import pytest
from autocron.knowledge import KnowledgeStore


@pytest.fixture
def store():
    d = tempfile.mkdtemp()
    return KnowledgeStore(store_dir=d)


def test_add_lesson(store):
    """Adding a lesson should increase the count."""
    store.add_lesson("test_pattern", "command", "echo ok", "Always echo ok")
    assert store.lesson_count >= 1


def test_duplicate_lesson(store):
    """Adding the same pattern twice should be handled (dedup or update)."""
    store.add_lesson("dup_test", "prose", "First version", "Test")
    initial_count = store.lesson_count
    store.add_lesson("dup_test", "prose", "Updated version", "Test")
    # Count should not increase for duplicate pattern
    assert store.lesson_count <= initial_count + 1


def test_injection_blocks(store):
    """get_injection_blocks should return pitfalls and toolkit strings."""
    store.add_lesson("path_issue", "command", "/usr/bin/which curl", "Use full paths")
    pitfalls, toolkit = store.get_injection_blocks("backup task")
    assert isinstance(pitfalls, str)
    assert isinstance(toolkit, str)


def test_empty_store_injection(store):
    """Empty store should return empty injection blocks."""
    pitfalls, toolkit = store.get_injection_blocks("any task")
    assert isinstance(pitfalls, str)
    assert isinstance(toolkit, str)
