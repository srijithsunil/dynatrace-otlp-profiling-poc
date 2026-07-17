"""
Wall-clock sampling profiler — periodically captures all Python thread stacks.

This mirrors what a native profiler (eBPF, async-profiler, py-spy) does at the
OS level: interrupt execution on a timer, record the current call stack.

Trace correlation
─────────────────
Call register_thread_trace(trace_id, span_id) at the start of a request and
unregister_thread_trace() at the end. The sampler reads the registry during each
capture so every stack collected while a request is in flight is tagged with that
request's trace/span IDs. The exporter then sets traceId/spanId on the OTLP log
record, which Dynatrace uses to link profiles to distributed traces.

    register_thread_trace("4bf92f3577b34da6...", "00f067aa0ba902b7")
    try:
        # handle request...
    finally:
        unregister_thread_trace()
"""
import logging
import sys
import time
import threading
from collections import defaultdict
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

# Hard cap on unique stacks stored between flushes.
# At ~200 bytes per stack tuple, 50k stacks ≈ 10MB max.
# Beyond this limit new samples are dropped (counted in _overflow_drops).
_MAX_UNIQUE_STACKS = 50_000

# ── Thread trace registry ─────────────────────────────────────────────────────
# Maps thread_id → (trace_id_hex, span_id_hex).  Populated by web framework
# middleware / context managers before a request is processed, cleared after.

_trace_registry: Dict[int, Tuple[str, str]] = {}
_registry_lock  = threading.Lock()


def register_thread_trace(trace_id: str, span_id: str) -> None:
    """Bind a trace/span ID pair to the calling thread for profiling correlation."""
    with _registry_lock:
        _trace_registry[threading.current_thread().ident] = (trace_id, span_id)


def unregister_thread_trace() -> None:
    """Remove the trace context for the calling thread (call in finally / teardown)."""
    with _registry_lock:
        _trace_registry.pop(threading.current_thread().ident, None)


class StackSampler:
    """
    Samples Python thread stacks at a fixed wall-clock interval.

    Results are a frequency map:
        { (frame_tuple, ...): sample_count }

    Where each frame_tuple = (filename, lineno, funcname).
    Stack order: outermost (main) → innermost (leaf).
    """

    # Modules to exclude from captured stacks — they're infrastructure noise
    _SKIP_MODULES = (
        "sampler.py",
        "threading.py",
        "socketserver.py",
        "_socketserver.py",
        "selectors.py",
        "queue.py",
    )

    def __init__(self, interval_ms: float = 10.0):
        self.interval_s     = interval_ms / 1000.0
        self.interval_ns    = int(interval_ms * 1_000_000)
        # Key: (stack_tuple, trace_id, span_id) — trace IDs are "" when no context.
        self._samples: Dict[Tuple[tuple, str, str], int] = defaultdict(int)
        self._lock           = threading.Lock()
        self._running        = False
        self._thread: Optional[threading.Thread] = None
        self._start_time_ns  = 0
        self._total_samples  = 0
        self._overflow_drops = 0   # samples discarded due to _MAX_UNIQUE_STACKS cap

    def start(self) -> None:
        self._start_time_ns = time.time_ns()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="stack-sampler"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def flush(self) -> tuple:
        """
        Returns (samples_snapshot, start_time_ns, duration_ns) and resets counters.
        Call this on a regular interval to get profile windows.
        """
        with self._lock:
            snapshot        = dict(self._samples)
            start           = self._start_time_ns
            duration        = time.time_ns() - start
            drops           = self._overflow_drops
            self._samples.clear()
            self._start_time_ns  = time.time_ns()
            self._total_samples  = 0
            self._overflow_drops = 0
        if drops:
            log.warning(
                "Profiler overflow: %d samples dropped this window "
                "(hit %d unique-stack cap). Increase flush frequency or cap.",
                drops, _MAX_UNIQUE_STACKS,
            )
        return snapshot, start, duration

    # ── internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while self._running:
            self._capture()
            time.sleep(self.interval_s)

    def _capture(self) -> None:
        frames = sys._current_frames()
        # Snapshot the trace registry outside the sampler lock to avoid lock ordering issues.
        with _registry_lock:
            trace_snapshot = dict(_trace_registry)
        with self._lock:
            for _tid, frame in frames.items():
                trace_id, span_id = trace_snapshot.get(_tid, ("", ""))
                stack = []
                f = frame
                while f is not None:
                    fname = f.f_code.co_filename
                    if not any(fname.endswith(m) for m in self._SKIP_MODULES):
                        stack.append((fname, f.f_lineno, f.f_code.co_name))
                    f = f.f_back
                if stack:
                    key = (tuple(reversed(stack)), trace_id, span_id)
                    if key not in self._samples and len(self._samples) >= _MAX_UNIQUE_STACKS:
                        self._overflow_drops += 1
                        continue
                    self._samples[key] += 1
                    self._total_samples += 1
