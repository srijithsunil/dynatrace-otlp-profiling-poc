"""
Local OTLP logs validator — mimics the Dynatrace /api/v2/otlp/v1/logs
endpoint for profiling data exported by dt-otlp-profiler.

Receives the exact same payload the real endpoint would receive, decodes
it, and prints a flame-graph-style hotspot summary to stdout.

Use this to verify the OTLP payload shape before pointing the exporter at
a real Dynatrace tenant.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
received: list = []   # keep last N summaries in memory for inspection


# ── Helpers ───────────────────────────────────────────────────────────────────

def decode_log_records(records: list) -> dict:
    """
    Aggregate profiler log records into a hotspot summary keyed by leaf function.

    Each record has attributes: profile.sample_count, profile.cpu_ns,
    profile.leaf_function, profile.leaf_file, profile.window_start_ns,
    profile.window_duration_ns.
    """
    func_cpu: dict  = defaultdict(int)
    func_cnt: dict  = defaultdict(int)
    func_file: dict = {}
    window_start_ns = 0
    duration_ns     = 0

    for rec in records:
        attrs = {a["key"]: a.get("value", {}) for a in rec.get("attributes", [])}

        count    = int(attrs.get("profile.sample_count",       {}).get("intValue", 0))
        cpu_ns   = int(attrs.get("profile.cpu_ns",             {}).get("intValue", 0))
        leaf_fn  = attrs.get("profile.leaf_function",  {}).get("stringValue", "?")
        leaf_f   = attrs.get("profile.leaf_file",      {}).get("stringValue", "?")

        if not window_start_ns:
            window_start_ns = int(attrs.get("profile.window_start_ns",    {}).get("intValue", 0))
        if not duration_ns:
            duration_ns     = int(attrs.get("profile.window_duration_ns", {}).get("intValue", 0))

        func_cpu[leaf_fn]  += cpu_ns
        func_cnt[leaf_fn]  += count
        func_file[leaf_fn]  = leaf_f

    hotspots = sorted(
        [
            {
                "function": fname,
                "file":     func_file.get(fname, "?"),
                "cpu_ms":   round(func_cpu[fname] / 1_000_000, 2),
                "samples":  func_cnt[fname],
            }
            for fname in func_cpu
        ],
        key=lambda x: x["cpu_ms"],
        reverse=True,
    )

    start_str = (
        datetime.fromtimestamp(window_start_ns / 1e9, tz=timezone.utc).isoformat()
        if window_start_ns else "unknown"
    )

    return {
        "window_start":  start_str,
        "duration_s":    round(duration_ns / 1e9, 2),
        "total_samples": sum(func_cnt.values()),
        "hotspots":      hotspots[:20],
    }


def print_flame_summary(service: str, summary: dict) -> None:
    bar_width = 40
    top       = summary["hotspots"][:10]
    max_cpu   = top[0]["cpu_ms"] if top else 1

    print()
    print("─" * 70)
    print(f"  SERVICE   : {service}")
    print(f"  WINDOW    : {summary['window_start']}")
    print(f"  DURATION  : {summary['duration_s']}s")
    print(f"  SAMPLES   : {summary['total_samples']}")
    print()
    print(f"  {'FUNCTION':<30} {'FILE':<20} {'CPU':>8}   BAR")
    print(f"  {'-'*30} {'-'*20} {'-'*8}   {'-'*bar_width}")
    for h in top:
        bar_len    = int((h["cpu_ms"] / max_cpu) * bar_width) if max_cpu else 0
        bar        = "█" * bar_len
        short_file = h["file"].split("/")[-1][-20:]
        print(f"  {h['function']:<30} {short_file:<20} {h['cpu_ms']:>6.0f}ms   {bar}")
    print("─" * 70)
    print()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/api/v2/otlp/v1/logs", methods=["POST"])
def receive_logs():
    body = request.get_json(force=True, silent=True) or {}

    for rl in body.get("resourceLogs", []):
        attrs = {
            a["key"]: a["value"].get("stringValue", "")
            for a in rl.get("resource", {}).get("attributes", [])
        }
        service = attrs.get("service.name", "unknown")

        for sl in rl.get("scopeLogs", []):
            all_records = sl.get("logRecords", [])

            # Filter to profiler records only (non-profiler logs pass through silently)
            profiler_records = [
                r for r in all_records
                if any(
                    a["key"] == "log.source"
                    and a.get("value", {}).get("stringValue") == "continuous_profiler"
                    for a in r.get("attributes", [])
                )
            ]

            if profiler_records:
                summary = decode_log_records(profiler_records)
                print_flame_summary(service, summary)
                received.append({"service": service, "summary": summary})

    if len(received) > 50:
        del received[:-50]

    return jsonify({"partialSuccess": {}}), 202


@app.route("/api/v2/otlp/v1/logs", methods=["GET"])
def list_received():
    """Quick inspection endpoint — see what profiler windows have been received."""
    return jsonify({"count": len(received), "profiles": received[-5:]})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "received": len(received)})


if __name__ == "__main__":
    log.info("OTLP Profiler Log Validator listening on :8888")
    log.info("Endpoint: POST http://localhost:8888/api/v2/otlp/v1/logs")
    app.run(host="0.0.0.0", port=8888, threaded=True)
