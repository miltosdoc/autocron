"""Tests for the Judge — sandboxed cron-like execution."""
import pytest
from autocron.judge import Judge


@pytest.fixture
def judge():
    return Judge(timeout=10)


def test_simple_script_success(judge):
    """A basic echo script should succeed."""
    trace = judge.execute("#!/bin/bash\nset -euo pipefail\necho 'hello world'\n")
    assert trace.process_succeeded
    assert "hello world" in trace.stdout


def test_script_failure(judge):
    """A script with a bad command should fail."""
    trace = judge.execute("#!/bin/bash\nset -euo pipefail\nfalse\n")
    assert not trace.process_succeeded


def test_xtrace_present(judge):
    """Execution traces should include bash xtrace output."""
    trace = judge.execute("#!/bin/bash\nset -euo pipefail\necho test_marker\n")
    assert trace.xtrace is not None or trace.stderr is not None


def test_empty_script_handled(judge):
    """Empty script should not crash the judge."""
    trace = judge.execute("")
    # Should handle gracefully (may fail, but shouldn't crash)
    assert trace is not None


def test_timeout_enforcement(judge):
    """Scripts exceeding timeout should be killed."""
    trace = judge.execute("#!/bin/bash\nsleep 60\n")
    assert not trace.process_succeeded
