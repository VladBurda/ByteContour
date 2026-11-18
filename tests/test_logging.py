import json
import logging

from bytecontour.logging_config import JsonFormatter


def test_structured_diagnostics() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "stopped %s", ("normally",), None)
    record.context = {"exit_code": 0}

    data = json.loads(JsonFormatter().format(record))

    assert data["timestamp"].endswith("+00:00")
    assert data["level"] == "INFO"
    assert data["logger"] == "test"
    assert data["message"] == "stopped normally"
    assert data["exit_code"] == 0
