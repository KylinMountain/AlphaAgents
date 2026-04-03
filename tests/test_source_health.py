import time
from alpha_agents.pipeline.source_health import SourceHealthTracker


def test_healthy_by_default():
    tracker = SourceHealthTracker()
    tracker.register("test", "Test Source")
    assert not tracker.should_skip("test")


def test_record_success():
    tracker = SourceHealthTracker()
    tracker.register("test", "Test")
    tracker.record_success("test", item_count=10)
    status = tracker.get_status()
    assert status[0]["total_success"] == 1
    assert status[0]["total_items"] == 10
    assert status[0]["success_rate"] == 100.0


def test_mark_unhealthy_after_threshold():
    tracker = SourceHealthTracker()
    tracker.register("test", "Test")
    for _ in range(3):
        tracker.record_failure("test", "connection error")
    status = tracker.get_status()
    assert status[0]["healthy"] is False
    assert status[0]["consecutive_failures"] == 3
    assert tracker.should_skip("test")


def test_recovery_on_success():
    tracker = SourceHealthTracker()
    tracker.register("test", "Test")
    for _ in range(3):
        tracker.record_failure("test", "error")
    assert tracker.should_skip("test")
    tracker.record_success("test", 5)
    assert not tracker.should_skip("test")
    assert tracker.get_status()[0]["healthy"] is True


def test_retry_after_timeout():
    tracker = SourceHealthTracker(retry_after=0.01)  # 10ms for test
    tracker.register("test", "Test")
    for _ in range(3):
        tracker.record_failure("test", "error")
    assert tracker.should_skip("test")
    time.sleep(0.02)
    # After retry_after, should_skip returns False to allow retry
    assert not tracker.should_skip("test")


def test_success_rate_calculation():
    tracker = SourceHealthTracker()
    tracker.register("test", "Test")
    tracker.record_success("test", 10)
    tracker.record_success("test", 5)
    tracker.record_failure("test", "err")
    status = tracker.get_status()
    assert status[0]["success_rate"] == 66.7  # 2/3


def test_unregistered_source_noop():
    tracker = SourceHealthTracker()
    tracker.record_success("unknown", 10)  # should not raise
    tracker.record_failure("unknown", "err")
    assert not tracker.should_skip("unknown")
