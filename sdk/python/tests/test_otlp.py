"""
Tests for build_otlp_logs() — validates OTLP Logs profiling payload shape.
"""
import pytest
from dt_profiler.otlp_exporter import build_otlp_logs


def _s(stack, trace_id="", span_id=""):
    """Wrap a stack tuple into the (stack, trace_id, span_id) key format."""
    return (stack, trace_id, span_id)


def _attr(record, key):
    """Extract a single attribute value dict from a log record."""
    for a in record.get("attributes", []):
        if a["key"] == key:
            return a["value"]
    return None


def _int_attr(record, key) -> int:
    v = _attr(record, key)
    return int(v["intValue"]) if v else 0


def _str_attr(record, key) -> str:
    v = _attr(record, key)
    return v.get("stringValue", "") if v else ""


# ── basic shape ───────────────────────────────────────────────────────────────

def test_empty_samples_returns_empty_list():
    records = build_otlp_logs({}, 10_000_000, 0, 1_000_000_000)
    assert records == []


def test_one_stack_produces_one_record():
    stack = (("app.py", 42, "handler"),)
    records = build_otlp_logs({_s(stack): 3}, 10_000_000, 0, 30_000_000_000)
    assert len(records) == 1


def test_multiple_stacks_produce_multiple_records():
    s1 = (("a.py", 1, "alpha"),)
    s2 = (("b.py", 2, "beta"),)
    records = build_otlp_logs({_s(s1): 10, _s(s2): 7}, 10_000_000, 0, 30_000_000_000)
    assert len(records) == 2


def test_same_stack_different_traces_produce_separate_records():
    """The same hot function in two different traces must produce two records."""
    stack = (("app.py", 10, "work"),)
    tid1, sid1 = "a" * 32, "b" * 16
    tid2, sid2 = "c" * 32, "d" * 16
    samples = {
        _s(stack, tid1, sid1): 4,
        _s(stack, tid2, sid2): 2,
    }
    records = build_otlp_logs(samples, 10_000_000, 0, 30_000_000_000)
    assert len(records) == 2


# ── attribute values ──────────────────────────────────────────────────────────

def test_sample_count_and_cpu_ns():
    stack = (("app.py", 10, "work"),)
    interval_ns = 10_000_000
    count = 5

    records = build_otlp_logs({_s(stack): count}, interval_ns, 0, 30_000_000_000)
    r = records[0]

    assert _int_attr(r, "profile.sample_count") == count
    assert _int_attr(r, "profile.cpu_ns") == count * interval_ns


def test_leaf_is_innermost_frame():
    outer = ("app.py", 1, "main")
    inner = ("db.py",  20, "query")
    stack = (outer, inner)   # outermost → innermost

    records = build_otlp_logs({_s(stack): 1}, 10_000_000, 0, 30_000_000_000)
    r = records[0]

    assert _str_attr(r, "profile.leaf_function") == "query"
    assert _str_attr(r, "profile.leaf_file")     == "db.py"
    assert _int_attr(r, "profile.leaf_line")     == 20


def test_root_is_outermost_frame():
    outer = ("app.py", 1, "main")
    inner = ("db.py",  20, "query")
    stack = (outer, inner)

    records = build_otlp_logs({_s(stack): 1}, 10_000_000, 0, 30_000_000_000)
    assert _str_attr(records[0], "profile.root_function") == "main"


def test_stack_depth():
    stack = (("a.py", 1, "f1"), ("b.py", 2, "f2"), ("c.py", 3, "f3"))
    records = build_otlp_logs({_s(stack): 1}, 10_000_000, 0, 30_000_000_000)
    assert _int_attr(records[0], "profile.stack_depth") == 3


def test_log_source_attribute():
    stack = (("app.py", 1, "fn"),)
    records = build_otlp_logs({_s(stack): 1}, 10_000_000, 0, 30_000_000_000)
    assert _str_attr(records[0], "log.source") == "continuous_profiler"


def test_window_timing_attributes():
    start_ns    = 1_700_000_000_000_000_000
    duration_ns = 30_000_000_000
    stack = (("app.py", 1, "fn"),)

    records = build_otlp_logs({_s(stack): 1}, 10_000_000, start_ns, duration_ns)
    r = records[0]

    assert _int_attr(r, "profile.window_start_ns")    == start_ns
    assert _int_attr(r, "profile.window_duration_ns") == duration_ns
    assert int(r["timeUnixNano"])                     == start_ns


# ── severity ──────────────────────────────────────────────────────────────────

def test_severity_is_info():
    stack = (("app.py", 1, "fn"),)
    records = build_otlp_logs({_s(stack): 1}, 10_000_000, 0, 30_000_000_000)
    r = records[0]
    assert r["severityNumber"] == 9
    assert r["severityText"]   == "INFO"


# ── body format ───────────────────────────────────────────────────────────────

def test_body_contains_function_and_file():
    stack = (("app.py", 42, "handle_request"), ("db.py", 15, "query"))
    records = build_otlp_logs({_s(stack): 1}, 10_000_000, 0, 30_000_000_000)
    body = records[0]["body"]["stringValue"]

    assert "handle_request" in body
    assert "query"          in body
    assert "app.py"         in body
    assert "db.py"          in body


def test_body_outermost_before_leaf():
    """Body must list frames outermost → innermost (Python traceback style)."""
    outer = ("app.py", 1, "main")
    inner = ("db.py",  5, "query")
    stack = (outer, inner)
    records = build_otlp_logs({_s(stack): 1}, 10_000_000, 0, 30_000_000_000)
    body = records[0]["body"]["stringValue"]

    assert body.index("main") < body.index("query"), \
        "outermost frame should appear before leaf frame in body text"


# ── leaf filename is basename only ────────────────────────────────────────────

def test_leaf_file_is_basename():
    stack = (("/usr/app/src/workers/db.py", 10, "query"),)
    records = build_otlp_logs({_s(stack): 1}, 10_000_000, 0, 30_000_000_000)
    assert _str_attr(records[0], "profile.leaf_file") == "db.py"


# ── trace correlation ─────────────────────────────────────────────────────────

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID  = "00f067aa0ba902b7"


def test_trace_ids_set_top_level_fields():
    """traceId / spanId must appear as top-level OTLP log record fields."""
    stack = (("app.py", 1, "handler"),)
    records = build_otlp_logs({_s(stack, TRACE_ID, SPAN_ID): 1}, 10_000_000, 0, 30_000_000_000)
    r = records[0]
    assert r["traceId"] == TRACE_ID
    assert r["spanId"]  == SPAN_ID


def test_trace_ids_set_queryable_attributes():
    """trace.id / span.id must also appear as log attributes for DQL filtering."""
    stack = (("app.py", 1, "handler"),)
    records = build_otlp_logs({_s(stack, TRACE_ID, SPAN_ID): 1}, 10_000_000, 0, 30_000_000_000)
    r = records[0]
    assert _str_attr(r, "trace.id") == TRACE_ID
    assert _str_attr(r, "span.id")  == SPAN_ID


def test_no_trace_ids_when_empty():
    """Records without trace context must not include traceId/spanId fields."""
    stack = (("app.py", 1, "fn"),)
    records = build_otlp_logs({_s(stack): 1}, 10_000_000, 0, 30_000_000_000)
    r = records[0]
    assert "traceId" not in r
    assert "spanId"  not in r
    assert _attr(r, "trace.id") is None
    assert _attr(r, "span.id")  is None


def test_trace_id_does_not_bleed_across_records():
    """A traced sample and an untraced sample must not share trace fields."""
    stack = (("app.py", 1, "fn"),)
    samples = {
        _s(stack, TRACE_ID, SPAN_ID): 2,
        _s(stack):                    5,
    }
    records = build_otlp_logs(samples, 10_000_000, 0, 30_000_000_000)
    assert len(records) == 2

    traced   = next(r for r in records if r.get("traceId"))
    untraced = next(r for r in records if not r.get("traceId"))

    assert traced["traceId"]  == TRACE_ID
    assert traced["spanId"]   == SPAN_ID
    assert "traceId" not in untraced
    assert "spanId"  not in untraced
