"""
Local OTLP profiles validator — mimics the Dynatrace /api/v2/otlp/v1/profiles
endpoint. Receives the exact same payload the real endpoint would receive,
decodes it, and prints a flame-graph-style hotspot summary to stdout.

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
received: list[dict] = []   # keep last N profiles in memory for inspection


# ── Helpers ───────────────────────────────────────────────────────────────────

def decode_profile(profile: dict) -> dict:
    """
    Decode a single OTLP Profile object into a human-readable summary.

    Returns:
      {
        "window_start": str,
        "duration_s": float,
        "period_ms": float,
        "total_samples": int,
        "hotspots": [ {"function": str, "file": str, "cpu_ms": float, "samples": int} ]
      }
    """
    st   = profile.get("stringTable", [])
    fns  = {f["id"]: f for f in profile.get("function", [])}
    locs = {l["id"]: l for l in profile.get("location", [])}

    time_ns     = int(profile.get("timeNanos", 0))
    duration_ns = int(profile.get("durationNanos", 0))
    period_ns   = int(profile.get("period", 10_000_000))

    # Accumulate cpu_ns and sample counts per function
    func_cpu: dict[str, int]     = defaultdict(int)
    func_cnt: dict[str, int]     = defaultdict(int)
    func_file: dict[str, str]    = {}

    for sample in profile.get("sample", []):
        values  = sample.get("value", ["0", "0"])
        cpu_ns  = int(values[0]) if values else 0
        cnt     = int(values[1]) if len(values) > 1 else 0
        for loc_id in sample.get("locationId", []):
            loc = locs.get(loc_id, {})
            for line in loc.get("line", []):
                fn = fns.get(line.get("functionId", ""), {})
                try:
                    fname = st[int(fn["name"])]     if fn.get("name")     else "?"
                    ffile = st[int(fn["filename"])] if fn.get("filename") else "?"
                except (IndexError, ValueError, KeyError):
                    fname, ffile = "?", "?"
                func_cpu[fname]  += cpu_ns
                func_cnt[fname]  += cnt
                func_file[fname]  = ffile

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
        datetime.fromtimestamp(time_ns / 1e9, tz=timezone.utc).isoformat()
        if time_ns else "unknown"
    )

    return {
        "window_start": start_str,
        "duration_s":   round(duration_ns / 1e9, 2),
        "period_ms":    round(period_ns / 1_000_000, 2),
        "total_samples": sum(func_cnt.values()),
        "hotspots":     hotspots[:20],
    }


def print_flame_summary(service: str, summary: dict) -> None:
    bar_width = 40
    top        = summary["hotspots"][:10]
    max_cpu    = top[0]["cpu_ms"] if top else 1

    print()
    print("─" * 70)
    print(f"  SERVICE   : {service}")
    print(f"  WINDOW    : {summary['window_start']}")
    print(f"  DURATION  : {summary['duration_s']}s  PERIOD: {summary['period_ms']}ms")
    print(f"  SAMPLES   : {summary['total_samples']}")
    print()
    print(f"  {'FUNCTION':<30} {'FILE':<20} {'CPU':>8}   BAR")
    print(f"  {'-'*30} {'-'*20} {'-'*8}   {'-'*bar_width}")
    for h in top:
        bar_len = int((h["cpu_ms"] / max_cpu) * bar_width) if max_cpu else 0
        bar     = "█" * bar_len
        short_file = h["file"].split("/")[-1][-20:]
        print(f"  {h['function']:<30} {short_file:<20} {h['cpu_ms']:>6.0f}ms   {bar}")
    print("─" * 70)
    print()


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.route("/api/v2/otlp/v1/profiles", methods=["POST"])
def receive_profiles():
    body = request.get_json(force=True, silent=True) or {}

    for rp in body.get("resourceProfiles", []):
        attrs = {
            a["key"]: a["value"].get("stringValue", "")
            for a in rp.get("resource", {}).get("attributes", [])
        }
        service = attrs.get("service.name", "unknown")

        for sp in rp.get("scopeProfiles", []):
            for profile in sp.get("profiles", []):
                summary = decode_profile(profile)
                print_flame_summary(service, summary)
                received.append({"service": service, "summary": summary})

    # Keep last 50
    if len(received) > 50:
        del received[:-50]

    # Mimic Dynatrace OTLP success response
    return jsonify({"partialSuccess": {}}), 202


@app.route("/api/v2/otlp/v1/profiles", methods=["GET"])
def list_received():
    """Quick inspection endpoint — see what profiles have been received."""
    return jsonify({"count": len(received), "profiles": received[-5:]})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "received": len(received)})


if __name__ == "__main__":
    log.info("OTLP Profile Validator listening on :8888")
    log.info("Endpoint: POST http://localhost:8888/api/v2/otlp/v1/profiles")
    app.run(host="0.0.0.0", port=8888, threaded=True)
