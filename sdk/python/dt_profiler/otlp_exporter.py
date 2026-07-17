"""
Converts stack-sample data to OTLP Logs format and exports to Dynatrace.

Each unique stack trace observed in a profiling window becomes one log record.
This uses the well-supported OTLP Logs ingest path rather than the alpha
OTLP Profiles signal, making it compatible with any Dynatrace environment.

Log record anatomy
──────────────────
  traceId     — hex trace ID (set when a request trace context was registered)
  spanId      — hex span ID  (set when a request trace context was registered)
  body        — formatted stack trace (outermost → innermost, Python traceback style)
  attributes  — profile.sample_count, profile.cpu_ns, profile.leaf_function,
                profile.leaf_file, profile.leaf_line, profile.root_function,
                profile.stack_depth, profile.window_start_ns,
                profile.window_duration_ns, log.source="continuous_profiler"
                trace.id, span.id (when trace context is present)

When traceId/spanId are set, Dynatrace automatically links these profile records
to the matching distributed trace in the Distributed Traces app.

DT Logs queries
───────────────
  // Top CPU consumers for a specific trace
  fetch logs | filter log.source == "continuous_profiler" and trace.id == "<id>"
    | summarize cpu_ms = sum(toLong(profile.cpu_ns))/1000000, by:{profile.leaf_function}
    | sort cpu_ms desc

  // All traces that touched a hot function
  fetch logs | filter log.source == "continuous_profiler"
                and profile.leaf_function == "slow_db_query"
    | fields trace.id, span.id, profile.cpu_ns, profile.leaf_file

Production features
───────────────────
- Retry with exponential backoff (3 attempts: 2s, 4s delays)
- Circuit breaker: opens after 5 consecutive failures, resets after 60s
- Connection pooling via requests.Session (keep-alive, reuse)
- No retry on 4xx (client errors — fix config, don't hammer)
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

_RETRY_DELAYS        = (2, 4)   # seconds between attempts 1→2 and 2→3
_CIRCUIT_OPEN_AFTER  = 5        # consecutive failures before circuit opens
_CIRCUIT_BACKOFF_S   = 60       # seconds circuit stays open before half-open probe


# ── Log builder ──────────────────────────────────────────────────────────────

def build_otlp_logs(
    samples: Dict[tuple, int],
    sample_interval_ns: int,
    start_time_ns: int,
    duration_ns: int,
) -> List[Dict[str, Any]]:
    """
    Convert { (stack_tuple, trace_id, span_id): count } → list of OTLP LogRecord objects.

    One log record per unique (stack, trace context) pair observed in the window.

    stack_tuple elements: (filename, lineno, funcname)  outermost → innermost
    trace_id / span_id:   lowercase hex strings; empty string when outside a trace
    count:                number of times this exact stack+context was observed
    """
    records = []

    for (stack, trace_id, span_id), count in samples.items():
        cpu_ns = count * sample_interval_ns

        # stack is outermost → innermost; leaf is the hot function
        leaf       = stack[-1] if stack else ("unknown", 0, "unknown")
        root       = stack[0]  if stack else ("unknown", 0, "unknown")
        leaf_file, leaf_line, leaf_func = leaf
        _,         _,         root_func = root

        # Python traceback style: outermost first, leaf last
        body = "\n".join(
            f'  File "{fname}", line {lineno}, in {func}'
            for fname, lineno, func in stack
        )

        attrs = [
            {"key": "log.source",                 "value": {"stringValue": "continuous_profiler"}},
            {"key": "profile.sample_count",       "value": {"intValue": str(count)}},
            {"key": "profile.cpu_ns",             "value": {"intValue": str(cpu_ns)}},
            {"key": "profile.leaf_function",      "value": {"stringValue": leaf_func}},
            {"key": "profile.leaf_file",          "value": {"stringValue": leaf_file.split("/")[-1]}},
            {"key": "profile.leaf_line",          "value": {"intValue": str(leaf_line)}},
            {"key": "profile.root_function",      "value": {"stringValue": root_func}},
            {"key": "profile.stack_depth",        "value": {"intValue": str(len(stack))}},
            {"key": "profile.window_start_ns",    "value": {"intValue": str(start_time_ns)}},
            {"key": "profile.window_duration_ns", "value": {"intValue": str(duration_ns)}},
        ]

        # Include trace/span IDs as queryable attributes when present.
        # These mirror the top-level traceId/spanId fields so DQL can filter on them.
        if trace_id:
            attrs.append({"key": "trace.id", "value": {"stringValue": trace_id}})
        if span_id:
            attrs.append({"key": "span.id",  "value": {"stringValue": span_id}})

        record: Dict[str, Any] = {
            "timeUnixNano":         str(start_time_ns),
            "observedTimeUnixNano": str(start_time_ns),
            "severityNumber":       9,       # INFO
            "severityText":         "INFO",
            "body":                 {"stringValue": body},
            "attributes":           attrs,
        }

        # traceId / spanId are first-class OTLP log record fields (hex-encoded per
        # OTLP spec §4.3).  Dynatrace reads these to link the record to a trace.
        if trace_id:
            record["traceId"] = trace_id
        if span_id:
            record["spanId"] = span_id

        records.append(record)

    return records


# ── Exporter ─────────────────────────────────────────────────────────────────

class DynatraceOTLPProfileExporter:
    """
    Exports profiling data as OTLP Logs to Dynatrace via HTTP.

    Each flush window produces one log record per unique stack trace.
    Query them in DT Logs with: filter log.source == "continuous_profiler"

    Production features:
    - Retry with exponential backoff on transient errors (5xx, timeout, connection error)
    - Circuit breaker stops hammering a down endpoint
    - requests.Session for connection pooling / keep-alive
    """

    _CIRCUIT_OPEN_AFTER = _CIRCUIT_OPEN_AFTER
    _CIRCUIT_BACKOFF_S  = _CIRCUIT_BACKOFF_S

    def __init__(
        self,
        endpoint: str,
        api_token: str,
        service_name: str,
        service_version: str = "1.0.0",
        environment: str = "production",
        sample_interval_ns: int = 10_000_000,
        timeout_s: int = 15,
        extra_attributes: Optional[Dict[str, str]] = None,
    ):
        self.endpoint           = endpoint.rstrip("/")
        self.service_name       = service_name
        self.service_version    = service_version
        self.environment        = environment
        self.sample_interval_ns = sample_interval_ns
        self._timeout_s         = timeout_s
        self._extra             = extra_attributes or {}

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Api-Token {api_token}",
            "Content-Type":  "application/json",
        })

        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until   = 0.0

    def export(
        self,
        samples: Dict[tuple, int],
        start_time_ns: int,
        duration_ns: int,
    ) -> bool:
        if not samples:
            log.debug("No samples to export, skipping flush")
            return True

        if self._circuit_open_until > time.monotonic():
            remaining = int(self._circuit_open_until - time.monotonic())
            log.warning(
                "Circuit breaker open — skipping export (%ds remaining). "
                "Check DT_ENDPOINT and DT_API_TOKEN.",
                remaining,
            )
            return False

        log_records = build_otlp_logs(
            samples, self.sample_interval_ns, start_time_ns, duration_ns
        )

        resource_attrs = [
            {"key": "service.name",          "value": {"stringValue": self.service_name}},
            {"key": "service.version",        "value": {"stringValue": self.service_version}},
            {"key": "deployment.environment", "value": {"stringValue": self.environment}},
            {"key": "telemetry.sdk.name",     "value": {"stringValue": "dynatrace-otlp-profiler"}},
            {"key": "telemetry.sdk.language", "value": {"stringValue": "python"}},
            {"key": "telemetry.sdk.version",  "value": {"stringValue": "0.1.0"}},
        ]
        for k, v in self._extra.items():
            resource_attrs.append({"key": k, "value": {"stringValue": v}})

        payload = {
            "resourceLogs": [{
                "resource": {"attributes": resource_attrs},
                "scopeLogs": [{
                    "scope": {"name": "dynatrace-otlp-profiler", "version": "0.1.0"},
                    "logRecords": log_records,
                }],
            }]
        }

        url = f"{self.endpoint}/api/v2/otlp/v1/logs"
        return self._post_with_retry(url, payload, sum(samples.values()), duration_ns)

    def _post_with_retry(
        self,
        url: str,
        payload: Dict[str, Any],
        total_samples: int,
        duration_ns: int,
    ) -> bool:
        delays = list(_RETRY_DELAYS) + [None]   # 3 attempts total

        for attempt, delay in enumerate(delays, start=1):
            try:
                resp = self._session.post(url, json=payload, timeout=self._timeout_s)

                if resp.status_code in (200, 202, 204):
                    self._consecutive_failures = 0
                    log.info(
                        "Exported profile → HTTP %s | total_samples=%d window=%.1fs",
                        resp.status_code, total_samples, duration_ns / 1e9,
                    )
                    return True

                if 400 <= resp.status_code < 500:
                    # Client error — retrying won't help, fix the config
                    log.error(
                        "Export rejected HTTP %s (not retrying — check token/endpoint): %s",
                        resp.status_code, resp.text[:300],
                    )
                    self._record_failure()
                    return False

                log.warning(
                    "Export HTTP %s on attempt %d/%d, will retry",
                    resp.status_code, attempt, len(delays),
                )

            except requests.Timeout:
                log.warning(
                    "Export timed out after %ds on attempt %d/%d, will retry",
                    self._timeout_s, attempt, len(delays),
                )
            except requests.ConnectionError as exc:
                log.warning(
                    "Export connection error on attempt %d/%d: %s, will retry",
                    attempt, len(delays), exc,
                )
            except requests.RequestException as exc:
                log.error("Export unexpected error (not retrying): %s", exc)
                self._record_failure()
                return False

            if delay is not None:
                time.sleep(delay)

        log.error(
            "Export failed after %d attempts — profile window dropped. "
            "Check network connectivity to %s.",
            len(delays), self.endpoint,
        )
        self._record_failure()
        return False

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._CIRCUIT_OPEN_AFTER:
            self._circuit_open_until = time.monotonic() + self._CIRCUIT_BACKOFF_S
            log.error(
                "Circuit breaker opened after %d consecutive failures — "
                "exports paused for %ds. Verify %s is reachable.",
                self._consecutive_failures, self._CIRCUIT_BACKOFF_S, self.endpoint,
            )

    def dump_json(
        self,
        samples: Dict[tuple, int],
        start_time_ns: int,
        duration_ns: int,
    ) -> str:
        """Return the OTLP Logs JSON payload as a string — useful for debugging."""
        log_records = build_otlp_logs(
            samples, self.sample_interval_ns, start_time_ns, duration_ns
        )
        payload = {
            "resourceLogs": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": self.service_name}},
                ]},
                "scopeLogs": [{"logRecords": log_records}],
            }]
        }
        return json.dumps(payload, indent=2)
