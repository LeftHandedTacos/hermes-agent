"""A malformed log record must never take the process down.

Measured on srv1 (2026-11-15): a dispatch log statement passed ``None`` into a
``%d`` slot, ``record.getMessage()`` raised ``TypeError``, and logging's
``handleError`` printed 2,350 ``--- Logging error ---`` tracebacks in one day.
The record is malformed at the emitting statement (fixed in
``tests/hermes_cli/test_kanban_burst_throttle_log.py``), but the formatter is
the last line of defence: one bad log line must not cost a worker batch.
"""

import logging

from agent.redact import RedactingFormatter

_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_BURST = (
    "kanban dispatch: burst throttle limiting to %d workers this tick "
    "(spawn_budget=%d, spawn_per_tick=%d)"
)


def _malformed_record() -> logging.LogRecord:
    return logging.LogRecord(
        "kanban.dispatch", logging.WARNING, __file__, 1, _BURST, (None, 6, None), None
    )


def test_none_in_a_percent_d_slot_still_formats():
    line = RedactingFormatter(_FMT).format(_malformed_record())

    assert "burst throttle" in line
    # The fallback keeps the arguments visible instead of dropping the line.
    assert "(None, 6, None)" in line


def test_missing_argument_for_percent_d_still_formats():
    record = logging.LogRecord(
        "kanban.dispatch", logging.WARNING, __file__, 1, _BURST, ("only-one",), None
    )

    line = RedactingFormatter(_FMT).format(record)

    assert "burst throttle" in line


def test_broken_record_still_reaches_the_file(tmp_path, capsys):
    path = tmp_path / "agent.log"
    handler = logging.FileHandler(path, encoding="utf-8", delay=False)
    handler.setFormatter(RedactingFormatter(_FMT))
    logger = logging.getLogger("tests.malformed_record_sink")
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        logger.warning(_BURST, None, 6, None)
    finally:
        logger.removeHandler(handler)
        handler.close()

    written = path.read_text(encoding="utf-8")
    assert "burst throttle" in written
    assert "Logging error" not in capsys.readouterr().err


def test_redaction_survives_the_fallback():
    record = logging.LogRecord(
        "kanban.dispatch", logging.WARNING, __file__, 1,
        "auxiliary failed for key %d: %s", (None, "token ghp_" + "a" * 32), None,
    )

    line = RedactingFormatter(_FMT).format(record)

    assert "ghp_" + "a" * 32 not in line
