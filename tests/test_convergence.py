"""Tests for the ConvergenceDetector — stopping conditions."""
import pytest
from autocron.convergence import (
    ConvergenceDetector, ConvergenceConfig, RoundSignal,
)


@pytest.fixture
def detector():
    return ConvergenceDetector(ConvergenceConfig(
        max_rounds=5,
        cosmetic_patience=2,
        saturation_window=20,
        saturation_threshold=1,
    ))


def test_approved_stops(detector):
    """Manager approval should stop immediately."""
    decision = detector.check(RoundSignal(1, "approved"))
    assert decision.should_stop
    assert decision.reason == "approved"


def test_max_rounds_stops(detector):
    """Reaching max rounds should stop."""
    for i in range(1, 5):
        d = detector.check(RoundSignal(i, "fail"))
        assert not d.should_stop
    d = detector.check(RoundSignal(5, "fail"))
    assert d.should_stop
    assert d.reason == "max_rounds"


def test_cosmetic_patience(detector):
    """Consecutive cosmetic-only rounds should trigger soft stop."""
    detector.check(RoundSignal(1, "pass_with_issues", issue_severity="cosmetic"))
    d = detector.check(RoundSignal(2, "pass_with_issues", issue_severity="cosmetic"))
    assert d.should_stop
    assert d.reason == "cosmetic_only"


def test_cosmetic_reset_on_failure(detector):
    """Non-cosmetic issues should reset the cosmetic counter."""
    detector.check(RoundSignal(1, "pass_with_issues", issue_severity="cosmetic"))
    detector.check(RoundSignal(2, "fail"))
    d = detector.check(RoundSignal(3, "pass_with_issues", issue_severity="cosmetic"))
    assert not d.should_stop


def test_continue_on_failure(detector):
    """Simple failures should continue."""
    decision = detector.check(RoundSignal(1, "fail"))
    assert not decision.should_stop
    assert decision.reason == "continue"


def test_metrics_tracked(detector):
    """Metrics should be populated in decisions."""
    detector.check(RoundSignal(1, "fail"))
    d = detector.check(RoundSignal(2, "approved"))
    assert "total_rounds" in d.metrics
    assert d.metrics["total_rounds"] == 2


def test_reset(detector):
    """Reset should clear state."""
    detector.check(RoundSignal(1, "fail"))
    detector.reset()
    assert len(detector.signals) == 0
