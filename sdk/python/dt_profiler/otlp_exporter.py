"""
Converts stack-sample data to OTLP Profiles format and exports to Dynatrace.

Production features
───────────────────
- Retry with exponential backoff (3 attempts: 2s, 4s delays)
- Circuit breaker: opens after 5 consecutive failures, resets after 60s
- Connection pooling via requests.Session (keep-alive, reuse)
- No retry on 4xx (client errors — fix config, don't hammer)

OTLP Profiles spec:
  opentelemetry/opentelemetry-proto → profiles/v1development/profiles.proto
"""

import json
import logging
import time
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)

_RETRY_DELAYS        = (2, 4)   # seconds between attempts 1→2 and 2→3
_CIRCUIT_OPEN_AFTER  = 5        # consecutive failures before circuit opens
_CIRCUIT_BACKOFF_S   = 60       # seconds circuit stays open before half-open probe


# ── Profile builder ──────────────────────────────────────────────────────────

def build_otlp_profile(
    samples: Dict[tuple, int],
    sample_interval_ns: int,
    start_time_ns: int,
    duration_ns: int,
) -> Dict[str, Any]:
    """
    Convert { stack_tuple: count } → OTLP Profile JSON object.

    stack_tuple elements: (filename, lineno, funcname)  outermost → innermost
    count: number of times this exact stack was observed during the window

    Value semantics
    ───────────────
    value[0] = count * sample_interval_ns  → approximate CPU nanoseconds
    value[1] = count                        → raw sample count
    """
    string_table = [""]  # index 0 MUST be empty string per spec

    def intern(s: str) -> int:
        try:
            return string_table.index(s)
        except ValueError:
            string_table.append(s)
            return len(string_table) - 1

    cpu_type_idx     = intern("cpu")
    ns_unit_idx      = intern("nanoseconds")
    samples_type_idx = intern("samples")
    count_unit_idx   = intern("count")

    functions: Dict[tuple, int] = {}
    locations: Dict[tuple, int] = {}
    profile_functions = []
    profile_locations = []
    profile_samples   = []

    for stack, count in samples.items():
        location_ids = []

        for frame in reversed(stack):   # innermost first per OTLP spec
            filename, lineno, funcname = frame
            frame_key = (filename, lineno, funcname)

            if frame_key not in locations:
                func_key = (filename, funcname)
                if func_key not in functions:
                    fid = len(profile_functions) + 1
                    functions[func_key] = fid
                    profile_functions.append({
                        "id":         str(fid),
                        "name":       str(intern(funcname)),
                        "systemName": str(intern(funcname)),
                        "filename":   str(intern(filename)),
                        "startLine":  str(lineno),
                    })

                lid = len(profile_locations) + 1
                locations[frame_key] = lid
                profile_locations.append({
                    "id": str(lid),
                    "line": [{
                        "functionId": str(functions[(filename, funcname)]),
                        "line":       str(lineno),
                    }],
                })

            location_ids.append(str(locations[frame_key]))

        profile_samples.append({
            "locationId": location_ids,
            "value": [
                str(count * sample_interval_ns),
                str(count),
            ],
        })

    return {
        "sampleType": [
            {"type": str(cpu_type_idx),     "unit": str(ns_unit_idx)},
            {"type": str(samples_type_idx), "unit": str(count_unit_idx)},
        ],
        "sample":      profile_samples,
        "location":    profile_locations,
        "function":    profile_functions,
        "stringTable": string_table,
        "timeNanos":       str(start_time_ns),
        "durationNanos":   str(duration_ns),
        "periodType":  {"type": str(cpu_type_idx), "unit": str(ns_unit_idx)},
        "period":      str(sample_interval_ns),
    }


# ── Exporter ─────────────────────────────────────────────────────────────────

class DynatraceOTLPProfileExporter:
    """
    Exports OTLP profiles to Dynatrace via HTTP.

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

        profile = build_otlp_profile(
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
            "resourceProfiles": [{
                "resource": {"attributes": resource_attrs},
                "scopeProfiles": [{
                    "scope": {"name": "dynatrace-otlp-profiler", "version": "0.1.0"},
                    "profiles": [profile],
                }],
            }]
        }

        url = f"{self.endpoint}/api/v2/otlp/v1/profiles"
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
        """Return the OTLP JSON payload as a string — useful for debugging."""
        profile = build_otlp_profile(
            samples, self.sample_interval_ns, start_time_ns, duration_ns
        )
        payload = {
            "resourceProfiles": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": self.service_name}},
                ]},
                "scopeProfiles": [{"profiles": [profile]}],
            }]
        }
        return json.dumps(payload, indent=2)
