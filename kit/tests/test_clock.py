import datetime as dt

import pytest

from kit.clock import EPOCH, ScheduledEvent, SimClock


def test_time_only_moves_when_the_episode_moves_it(clock: SimClock) -> None:
    first = clock.now()
    second = clock.now()
    assert first == second == EPOCH
    clock.advance(60)
    assert clock.now() - first == dt.timedelta(minutes=1)
    assert clock.elapsed == dt.timedelta(minutes=1)


def test_time_never_moves_backwards(clock: SimClock) -> None:
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)


def test_start_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SimClock(dt.datetime(2026, 1, 1))


def test_events_fire_in_time_order_then_scheduling_order(clock: SimClock) -> None:
    clock.schedule_in(120, "later")
    clock.schedule_in(60, "first")
    clock.schedule_in(60, "second")
    assert clock.due() == []
    clock.advance(60)
    assert [event.name for event in clock.due()] == ["first", "second"]
    assert [event.name for event in clock.pending] == ["later"]
    clock.advance(60)
    assert [event.name for event in clock.due()] == ["later"]
    assert clock.pending == []


def test_due_drains_only_once(clock: SimClock) -> None:
    clock.schedule_in(0, "now")
    clock.advance(1)
    assert len(clock.due()) == 1
    assert clock.due() == []


def test_events_carry_a_payload(clock: SimClock) -> None:
    clock.schedule_in(5, "limit_change", {"daily_cap": 500})
    clock.advance(5)
    (event,) = clock.due()
    assert event.payload == {"daily_cap": 500}


def test_events_cannot_be_scheduled_before_the_episode_started(clock: SimClock) -> None:
    with pytest.raises(ValueError, match="before the episode started"):
        clock.schedule(ScheduledEvent(at=EPOCH - dt.timedelta(seconds=1), name="past"))


def test_next_event_at_reports_the_queue_head(clock: SimClock) -> None:
    assert clock.next_event_at() is None
    clock.schedule_in(30, "soon")
    assert clock.next_event_at() == EPOCH + dt.timedelta(seconds=30)


def test_scheduled_events_are_immutable(clock: SimClock) -> None:
    event = clock.schedule_in(1, "x")
    with pytest.raises(Exception, match=r"frozen"):
        event.name = "y"  # type: ignore[misc]
