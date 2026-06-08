"""
Converts stack-sample data to OTLP Profiles format and exports to Dynatrace.

OTLP Profiles spec:
  opentelemetry/opentelemetry-proto  →  profiles/v1development/profiles.proto

We use the JSON encoding (application/json) — semantically identical to the
binary protobuf, easier to inspect during POC development.

Profile structure summary
─────────────────────────
  ResourceProfiles
    └─ ScopeProfiles
         └─ Profile
              ├─ sampleType[]   — what we measured (cpu / nanoseconds)
              ├─ sample[]       — each unique stack + its measured value
              │    ├─ locationId[]  — frame references (innermost first)
              │    └─ value[]       — [cpu_ns, raw_count]
              ├─ location[]     — code point (function + line)
              ├─ function[]     — function metadata (name, file)
              ├─ stringTable[]  — interned strings (index 0 = "")
              ├─ timeNanos      — window start (Unix ns)
              ├─ durationNanos  — window length
              └─ period         — sampling interval in ns
"""

import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)


# ── Profile builder ────────────────────────────────────────────────────────────

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

    # Interned indices for our value types
    cpu_type_idx     = intern("cpu")
    ns_unit_idx      = intern("nanoseconds")
    samples_type_idx = intern("samples")
    count_unit_idx   = intern("count")

    functions = {}   # (filename, funcname) → id
    locations = {}   # (filename, lineno, funcname) → id
    profile_functions = []
    profile_locations = []
    profile_samples   = []

    for stack, count in samples.items():
        location_ids = []

        # OTLP sample.locationId order: innermost (leaf) → outermost (root)
        for frame in reversed(stack):
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
                str(count * sample_interval_ns),  # cpu nanoseconds
                str(count),                        # raw count
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


# ── Exporter ──────────────────────────────────────────────────────────────────

class DynatraceOTLPProfileExporter:
    """
    Exports OTLP profiles to Dynatrace via HTTP.

    Endpoint:  POST {dt_endpoint}/api/v2/otlp/v1/profiles
    Auth:      Authorization: Api-Token <token>
    Body:      OTLP ExportProfilesServiceRequest (JSON encoding)

    The same payload can also be sent to the OTel Collector OTLP HTTP receiver
    at port 4318 (no auth needed locally) for pipeline testing.
    """

    def __init__(
        self,
        endpoint: str,
        api_token: str,
        service_name: str,
        service_version: str = "1.0.0",
        environment: str = "poc",
        sample_interval_ns: int = 10_000_000,
        extra_attributes: Optional[Dict[str, str]] = None,
    ):
        self.endpoint           = endpoint.rstrip("/")
        self.service_name       = service_name
        self.service_version    = service_version
        self.environment        = environment
        self.sample_interval_ns = sample_interval_ns
        self._extra             = extra_attributes or {}
        self._headers = {
            "Authorization": f"Api-Token {api_token}",
            "Content-Type":  "application/json",
        }

    def export(
        self,
        samples: dict[tuple, int],
        start_time_ns: int,
        duration_ns: int,
    ) -> bool:  # type: ignore[override]
        if not samples:
            log.debug("No samples to export, skipping flush")
            return True

        profile = build_otlp_profile(
            samples, self.sample_interval_ns, start_time_ns, duration_ns
        )

        # Resource attributes follow OTel semantic conventions
        resource_attrs = [
            {"key": "service.name",              "value": {"stringValue": self.service_name}},
            {"key": "service.version",            "value": {"stringValue": self.service_version}},
            {"key": "deployment.environment",     "value": {"stringValue": self.environment}},
            {"key": "telemetry.sdk.name",         "value": {"stringValue": "dynatrace-otlp-profiler"}},
            {"key": "telemetry.sdk.language",     "value": {"stringValue": "python"}},
            {"key": "telemetry.sdk.version",      "value": {"stringValue": "0.1.0"}},
        ]
        for k, v in self._extra.items():
            resource_attrs.append({"key": k, "value": {"stringValue": v}})

        # ExportProfilesServiceRequest envelope
        payload = {
            "resourceProfiles": [{
                "resource": {"attributes": resource_attrs},
                "scopeProfiles": [{
                    "scope": {
                        "name":    "dynatrace-otlp-profiler",
                        "version": "0.1.0",
                    },
                    "profiles": [profile],
                }],
            }]
        }

        url = f"{self.endpoint}/api/v2/otlp/v1/profiles"
        try:
            resp = requests.post(url, json=payload, headers=self._headers, timeout=15)
            if resp.status_code in (200, 202, 204):
                total_samples = sum(samples.values())
                log.info(
                    "Exported profile → %s | stacks=%d total_samples=%d window=%.1fs",
                    resp.status_code, len(samples), total_samples, duration_ns / 1e9,
                )
                return True
            else:
                log.error(
                    "Export rejected: HTTP %s — %s",
                    resp.status_code, resp.text[:300],
                )
                return False
        except requests.RequestException as exc:
            log.error("Export failed: %s", exc)
            return False

    def dump_json(
        self,
        samples: dict[tuple, int],
        start_time_ns: int,
        duration_ns: int,
    ) -> str:
        """Return the OTLP JSON payload as a string — useful for debugging."""
        profile = build_otlp_profile(
            samples, self.sample_interval_ns, start_time_ns, duration_ns
        )
        resource_attrs = [
            {"key": "service.name", "value": {"stringValue": self.service_name}},
        ]
        payload = {
            "resourceProfiles": [{
                "resource": {"attributes": resource_attrs},
                "scopeProfiles": [{"profiles": [profile]}],
            }]
        }
        return json.dumps(payload, indent=2)
