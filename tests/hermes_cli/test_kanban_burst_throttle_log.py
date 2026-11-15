"""The burst-throttle log line must be formattable, and must actually throttle.

Measured on srv1 (2026-11-15): ``_tick_spawn_budget`` logged

    "kanban dispatch: burst throttle limiting to %d workers this tick
     (spawn_budget=%d, spawn_per_tick=%d)", spawn_per_tick, spawn_budget, spawn_per_tick

passing the *parameter* ``spawn_per_tick`` (default ``None``, no caller ever
supplied it) instead of the resolved config value. That produced 2,350
``--- Logging error ---`` records in one day, and the very next statement,
``spawn_budget = spawn_per_tick``, set the budget to ``None`` — silently
disabling the throttle the line was announcing.
"""

import logging

import pytest

from hermes_cli import kanban_db_dispatch as dispatch


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def conn(monkeypatch):
    """A tick with ten spawnable slots and a resolved burst throttle of three."""
    monkeypatch.setattr(dispatch, "count_running_tasks", lambda _conn: 0)
    monkeypatch.setattr(
        dispatch, "count_running_tasks_other_boards", lambda board=None: 0
    )
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda sample=None: "unknown")
    monkeypatch.setattr(dispatch, "_resolve_spawn_per_tick", lambda: 3)
    return object()


@pytest.fixture
def capture():
    sentinel = _Capture()
    logger = dispatch._kb._log
    logger.addHandler(sentinel)
    try:
        yield sentinel
    finally:
        logger.removeHandler(sentinel)


def _tick(conn):
    return dispatch._tick_spawn_budget(
        conn, dispatch.DispatchResult(), max_spawn=10, max_in_progress=None, board=None
    )


def test_burst_throttle_log_record_is_formattable(conn, capture):
    _tick(conn)

    throttled = [r for r in capture.records if "burst throttle" in str(r.msg)]
    assert throttled, "the tick that limits the burst must say so"
    # getMessage() is what raised TypeError in the gateway; it must not now.
    assert "limiting to 3 workers" in throttled[0].getMessage()


def test_burst_throttle_clamps_the_budget_to_the_resolved_config(conn):
    may_spawn, spawn_budget = _tick(conn)

    assert may_spawn is True
    assert spawn_budget == 3
