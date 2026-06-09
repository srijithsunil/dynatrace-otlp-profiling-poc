"""
Wall-clock sampling profiler — periodically captures all Python thread stacks.

This mirrors what a native profiler (eBPF, async-profiler, py-spy) does at the
OS level: interrupt execution on a timer, record the current call stack.
"""
import logging
import sys
import time
import threading
from collections import defaultdict
from typing import Dict, Optional

log = logging.getLogger(__name__)

# Hard cap on unique stacks stored between flushes.
# At ~200 bytes per stack tuple, 50k stacks ≈ 10MB max.
# Beyond this limit new samples are dropped (counted in _overflow_drops).
_MAX_UNIQUE_STACKS = 50_000


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
        self._samples: Dict[tuple, int] = defaultdict(int)
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
        with self._lock:
            for _tid, frame in frames.items():
                stack = []
                f = frame
                while f is not None:
                    fname = f.f_code.co_filename
                    if not any(fname.endswith(m) for m in self._SKIP_MODULES):
                        stack.append((fname, f.f_lineno, f.f_code.co_name))
                    f = f.f_back
                if stack:
                    key = tuple(reversed(stack))
                    if key not in self._samples and len(self._samples) >= _MAX_UNIQUE_STACKS:
                        self._overflow_drops += 1
                        continue
                    self._samples[key] += 1
                    self._total_samples += 1
