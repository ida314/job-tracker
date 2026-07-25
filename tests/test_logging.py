"""Structured logging. The deployed service emits one JSON object per line so an
aggregator can parse it; interactive runs keep the human format. Only the JSON
formatter has logic worth asserting — the format switch is a TTY check.
"""

import json
import logging
import sys

from jobtracker.cli import _JsonLogFormatter


def _record(msg, *args, name="jobtracker.test", level=logging.INFO, exc_info=None):
    return logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)


def test_emits_valid_json_with_core_fields():
    out = _JsonLogFormatter().format(_record("hello %s", "world"))
    obj = json.loads(out)  # raises if not one valid JSON object
    assert obj["level"] == "INFO"
    assert obj["logger"] == "jobtracker.test"
    assert obj["msg"] == "hello world"  # args are interpolated, not left as a template
    assert "ts" in obj


def test_extra_fields_become_top_level_keys():
    rec = _record("degraded", level=logging.WARNING)
    rec.board = "stripe"      # what logging's extra={...} does under the hood
    rec.retries = 2
    obj = json.loads(_JsonLogFormatter().format(rec))
    assert obj["board"] == "stripe"
    assert obj["retries"] == 2


def test_exception_is_captured():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = _record("failed", level=logging.ERROR, exc_info=sys.exc_info())
    obj = json.loads(_JsonLogFormatter().format(rec))
    assert "ValueError: boom" in obj["exc"]
