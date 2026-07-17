"""
Tests for DynatraceOTLPProfileExporter — retry, circuit breaker, 4xx no-retry.
"""
import time
import pytest
from unittest.mock import MagicMock, patch, call
from requests import Timeout, ConnectionError as ReqConnectionError
import requests

from dt_profiler.otlp_exporter import DynatraceOTLPProfileExporter, _RETRY_DELAYS


SIMPLE_SAMPLES = {((("app.py", 1, "main"),), "", ""): 3}
START_NS       = 1_000_000_000
DURATION_NS    = 30_000_000_000


def _make_exporter(**kwargs):
    defaults = dict(
        endpoint="http://localhost:9999",
        api_token="test-token",
        service_name="test-service",
    )
    defaults.update(kwargs)
    return DynatraceOTLPProfileExporter(**defaults)


def _mock_response(status_code, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


# ── success ──────────────────────────────────────────────────────────────────

def test_export_success_202():
    exp = _make_exporter()
    with patch.object(exp._session, "post", return_value=_mock_response(202)):
        result = exp.export(SIMPLE_SAMPLES, START_NS, DURATION_NS)
    assert result is True
    assert exp._consecutive_failures == 0


def test_export_empty_samples_returns_true_without_posting():
    exp = _make_exporter()
    with patch.object(exp._session, "post") as mock_post:
        result = exp.export({}, START_NS, DURATION_NS)
    assert result is True
    mock_post.assert_not_called()


# ── 4xx no-retry ──────────────────────────────────────────────────────────────

def test_4xx_does_not_retry():
    exp = _make_exporter()
    with patch.object(exp._session, "post", return_value=_mock_response(401)) as mock_post:
        result = exp.export(SIMPLE_SAMPLES, START_NS, DURATION_NS)
    assert result is False
    assert mock_post.call_count == 1   # one attempt only, no retries


def test_403_increments_failure_counter():
    exp = _make_exporter()
    with patch.object(exp._session, "post", return_value=_mock_response(403)):
        exp.export(SIMPLE_SAMPLES, START_NS, DURATION_NS)
    assert exp._consecutive_failures == 1


# ── retry on 5xx ─────────────────────────────────────────────────────────────

def test_5xx_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("dt_profiler.otlp_exporter.time.sleep", lambda _: None)
    exp = _make_exporter()
    responses = [_mock_response(503), _mock_response(202)]
    with patch.object(exp._session, "post", side_effect=responses) as mock_post:
        result = exp.export(SIMPLE_SAMPLES, START_NS, DURATION_NS)
    assert result is True
    assert mock_post.call_count == 2
    assert exp._consecutive_failures == 0


def test_all_attempts_fail_returns_false_and_increments_failures(monkeypatch):
    monkeypatch.setattr("dt_profiler.otlp_exporter.time.sleep", lambda _: None)
    exp = _make_exporter()
    with patch.object(exp._session, "post", return_value=_mock_response(500)):
        result = exp.export(SIMPLE_SAMPLES, START_NS, DURATION_NS)
    assert result is False
    assert exp._consecutive_failures == 1   # one failure recorded after exhausting retries


# ── retry on timeout / connection error ───────────────────────────────────────

def test_timeout_retries(monkeypatch):
    monkeypatch.setattr("dt_profiler.otlp_exporter.time.sleep", lambda _: None)
    exp = _make_exporter()
    responses = [Timeout(), Timeout(), _mock_response(200)]
    with patch.object(exp._session, "post", side_effect=responses) as mock_post:
        result = exp.export(SIMPLE_SAMPLES, START_NS, DURATION_NS)
    assert result is True
    assert mock_post.call_count == 3


def test_connection_error_retries(monkeypatch):
    monkeypatch.setattr("dt_profiler.otlp_exporter.time.sleep", lambda _: None)
    exp = _make_exporter()
    responses = [ReqConnectionError("refused"), _mock_response(200)]
    with patch.object(exp._session, "post", side_effect=responses) as mock_post:
        result = exp.export(SIMPLE_SAMPLES, START_NS, DURATION_NS)
    assert result is True
    assert mock_post.call_count == 2


# ── circuit breaker ───────────────────────────────────────────────────────────

def test_circuit_opens_after_consecutive_failures(monkeypatch):
    monkeypatch.setattr("dt_profiler.otlp_exporter.time.sleep", lambda _: None)
    exp = _make_exporter()
    # Each export exhausts 3 attempts → 1 failure recorded per export call
    with patch.object(exp._session, "post", return_value=_mock_response(500)):
        for _ in range(exp._CIRCUIT_OPEN_AFTER):
            exp.export(SIMPLE_SAMPLES, START_NS, DURATION_NS)

    assert exp._circuit_open_until > time.monotonic()


def test_circuit_open_skips_export(monkeypatch):
    monkeypatch.setattr("dt_profiler.otlp_exporter.time.sleep", lambda _: None)
    exp = _make_exporter()
    exp._circuit_open_until = time.monotonic() + 9999  # force open

    with patch.object(exp._session, "post") as mock_post:
        result = exp.export(SIMPLE_SAMPLES, START_NS, DURATION_NS)

    assert result is False
    mock_post.assert_not_called()


def test_circuit_resets_after_backoff(monkeypatch):
    monkeypatch.setattr("dt_profiler.otlp_exporter.time.sleep", lambda _: None)
    exp = _make_exporter()
    exp._circuit_open_until = time.monotonic() - 1  # expired — circuit half-open
    exp._consecutive_failures = 5

    with patch.object(exp._session, "post", return_value=_mock_response(200)):
        result = exp.export(SIMPLE_SAMPLES, START_NS, DURATION_NS)

    assert result is True
    assert exp._consecutive_failures == 0
