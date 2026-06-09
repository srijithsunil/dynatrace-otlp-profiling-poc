"""
Tests for build_otlp_profile() — validates OTLP Profiles spec compliance.
"""
import pytest
from dt_profiler.otlp_exporter import build_otlp_profile


def _simple_stack(fname="app.py", lineno=10, func="handler"):
    """One-frame stack: outermost == innermost."""
    return (fname, lineno, func)


def test_string_table_starts_with_empty():
    profile = build_otlp_profile({}, 10_000_000, 0, 1_000_000_000)
    assert profile["stringTable"][0] == "", "index 0 must be empty string per OTLP spec"


def test_empty_samples_produces_no_sample_records():
    profile = build_otlp_profile({}, 10_000_000, 0, 1_000_000_000)
    assert profile["sample"] == []
    assert profile["function"] == []
    assert profile["location"] == []


def test_single_frame_values():
    stack = (_simple_stack(),)
    samples = {stack: 5}
    interval_ns = 10_000_000

    profile = build_otlp_profile(samples, interval_ns, 0, 500_000_000)

    assert len(profile["sample"]) == 1
    s = profile["sample"][0]
    # value[0] = count * interval_ns
    assert int(s["value"][0]) == 5 * interval_ns
    # value[1] = raw count
    assert int(s["value"][1]) == 5


def test_location_order_innermost_first():
    """OTLP spec: locationId[0] = leaf (innermost) frame."""
    outer = ("app.py", 1, "main")
    inner = ("app.py", 20, "do_work")
    # stack tuple is outermost → innermost
    stack = (outer, inner)
    samples = {stack: 1}

    profile = build_otlp_profile(samples, 10_000_000, 0, 1_000_000_000)
    assert len(profile["sample"]) == 1
    loc_ids = profile["sample"][0]["locationId"]

    # Map location IDs back to function names
    loc_map = {loc["id"]: loc for loc in profile["location"]}
    func_map = {fn["id"]: fn for fn in profile["function"]}
    st = profile["stringTable"]

    leaf_func_name = st[int(func_map[loc_map[loc_ids[0]]["line"][0]["functionId"]]["name"])]
    root_func_name = st[int(func_map[loc_map[loc_ids[-1]]["line"][0]["functionId"]]["name"])]

    assert leaf_func_name == "do_work", f"first location must be leaf (innermost), got {leaf_func_name}"
    assert root_func_name == "main",    f"last location must be root (outermost), got {root_func_name}"


def test_function_deduplication():
    """Same function at the same line must reuse one Function record."""
    frame = ("mod.py", 5, "fn")
    stack_a = (frame,)
    stack_b = (frame, ("mod.py", 10, "other"))
    samples = {stack_a: 2, stack_b: 3}

    profile = build_otlp_profile(samples, 10_000_000, 0, 1_000_000_000)
    func_ids = {fn["id"] for fn in profile["function"]}
    # 'fn' at line 5 and 'other' at line 10 → 2 unique functions
    assert len(profile["function"]) == 2
    assert len(func_ids) == 2


def test_sample_type_structure():
    profile = build_otlp_profile({}, 10_000_000, 0, 1_000_000_000)
    assert len(profile["sampleType"]) == 2
    st = profile["stringTable"]
    types = {st[int(t["type"])] for t in profile["sampleType"]}
    assert "cpu" in types
    assert "samples" in types


def test_timing_fields():
    start = 1_700_000_000_000_000_000
    duration = 30_000_000_000
    profile = build_otlp_profile({}, 10_000_000, start, duration)
    assert int(profile["timeNanos"]) == start
    assert int(profile["durationNanos"]) == duration


def test_multiple_stacks_produce_multiple_samples():
    s1 = (("a.py", 1, "f1"),)
    s2 = (("b.py", 2, "f2"),)
    samples = {s1: 10, s2: 7}
    profile = build_otlp_profile(samples, 10_000_000, 0, 1_000_000_000)
    assert len(profile["sample"]) == 2
    total = sum(int(s["value"][1]) for s in profile["sample"])
    assert total == 17
