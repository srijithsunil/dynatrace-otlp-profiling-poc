"""
Wall-clock sampling profiler — periodically captures all Python thread stacks.

This mirrors what a native profiler (eBPF, async-profiler, py-spy) does at the
OS level: interrupt execution on a timer, record the current call stack.
"""
import sys
import time
import threading
from collections import defaultdict
from typing import Dict, Optional, Tuple


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
        self._lock          = threading.Lock()
        self._running       = False
        self._thread: Optional[threading.Thread] = None
        self._start_time_ns = 0
        self._total_samples = 0

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

    def flush(self) -> tuple[dict, int, int]:
        """
        Returns (samples_snapshot, start_time_ns, duration_ns) and resets counters.
        Call this on a regular interval to get profile windows.
        """
        with self._lock:
            snapshot        = dict(self._samples)
            start           = self._start_time_ns
            duration        = time.time_ns() - start
            self._samples.clear()
            self._start_time_ns = time.time_ns()
            self._total_samples = 0
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
                stack: list[tuple[str, int, str]] = []
                f = frame
                while f is not None:
                    fname = f.f_code.co_filename
                    if not any(fname.endswith(m) for m in self._SKIP_MODULES):
                        stack.append((fname, f.f_lineno, f.f_code.co_name))
                    f = f.f_back
                if stack:
                    # Store outermost → innermost (natural call order)
                    self._samples[tuple(reversed(stack))] += 1
                    self._total_samples += 1
