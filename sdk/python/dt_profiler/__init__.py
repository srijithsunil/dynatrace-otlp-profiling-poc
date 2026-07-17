"""
dt-otlp-profiler: Dynatrace OTLP continuous profiler for Python.

Profiling data is exported as OTLP Logs — one log record per unique stack
trace per flush window. Query in DT Logs with:
    fetch logs | filter log.source == "continuous_profiler"
      | summarize cpu_ms = sum(toLong(profile.cpu_ns))/1000000,
                  by:{profile.leaf_function}
      | sort cpu_ms desc

Minimal integration (2 lines):
    from dt_profiler import start_profiler
    start_profiler()

Trace correlation — link profiles to distributed traces in Dynatrace
─────────────────────────────────────────────────────────────────────
Option 1 — automatic (requires opentelemetry-api):
    Use init_flask_profiling(app) for Flask, or wrap any handler with
    auto_trace_context() to read the active OTel span automatically.

Option 2 — manual (no dependencies):
    from dt_profiler import trace_context
    with trace_context(trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
                       span_id="00f067aa0ba902b7"):
        handle_request()

All configuration is read from environment variables by default:
    DT_ENDPOINT        — your Dynatrace tenant URL
    DT_API_TOKEN       — API token with logs.ingest scope
    OTEL_SERVICE_NAME  — name shown in DT (default: "unknown-service")
    DEPLOYMENT_ENV     — environment tag (default: "production")
"""
import atexit
import os
import signal
import threading
import time
import logging
from contextlib import contextmanager
from typing import Optional, Dict, Generator

from .sampler import StackSampler, register_thread_trace, unregister_thread_trace
from .otlp_exporter import DynatraceOTLPProfileExporter

__version__ = "0.2.0"
__all__ = [
    "start_profiler",
    "stop_profiler",
    "trace_context",
    "auto_trace_context",
    "init_flask_profiling",
    "register_thread_trace",
    "unregister_thread_trace",
]

log = logging.getLogger(__name__)

_sampler: Optional[StackSampler] = None
_exporter: Optional[DynatraceOTLPProfileExporter] = None
_flush_thread: Optional[threading.Thread] = None
_running = False
_flush_interval_s: int = 30


def start_profiler(
    endpoint: Optional[str] = None,
    api_token: Optional[str] = None,
    service_name: Optional[str] = None,
    service_version: str = "1.0.0",
    environment: Optional[str] = None,
    sample_interval_ms: float = 10.0,
    flush_interval_s: int = 30,
    extra_attributes: Optional[Dict[str, str]] = None,
) -> None:
    """
    Start the continuous profiler in background daemon threads.

    Registers SIGTERM and atexit handlers so the final window is always
    flushed on graceful shutdown (docker stop, Kubernetes SIGTERM, etc).

    Call once at application startup — before your web server begins serving
    requests. All parameters fall back to the environment variables listed
    at the top of this module.

    Args:
        endpoint:            Dynatrace tenant base URL.
                             e.g. "https://abc123.live.dynatrace.com"
                             For local testing: "http://localhost:8888"
        api_token:           Dynatrace API token.
        service_name:        Service name shown in Dynatrace.
        service_version:     Service version tag.
        environment:         Deployment environment tag (prod, staging, …).
        sample_interval_ms:  How often to snapshot thread stacks (default 10ms).
        flush_interval_s:    How often to export a profile window (default 30s).
        extra_attributes:    Additional OTLP resource attributes to attach.
    """
    global _sampler, _exporter, _flush_thread, _running, _flush_interval_s

    if _running:
        log.warning("dt_profiler already running — ignoring duplicate start_profiler() call")
        return

    endpoint     = endpoint     or os.environ.get("DT_ENDPOINT", "")
    api_token    = api_token    or os.environ.get("DT_API_TOKEN", "")
    service_name = service_name or os.environ.get("OTEL_SERVICE_NAME", "unknown-service")
    environment  = environment  or os.environ.get("DEPLOYMENT_ENV", "production")

    if not endpoint:
        log.warning(
            "DT_ENDPOINT is not set — profiler will sample stacks but not export. "
            "Set DT_ENDPOINT to your Dynatrace tenant URL."
        )
    if not api_token:
        log.warning(
            "DT_API_TOKEN is not set — exports will be rejected with 401. "
            "Set DT_API_TOKEN to a token with continuousProfilingStorage.ingest scope."
        )

    attrs: Dict[str, str] = {"host.name": os.environ.get("HOSTNAME", "unknown")}
    if extra_attributes:
        attrs.update(extra_attributes)

    _exporter = DynatraceOTLPProfileExporter(
        endpoint=endpoint,
        api_token=api_token,
        service_name=service_name,
        service_version=service_version,
        environment=environment,
        sample_interval_ns=int(sample_interval_ms * 1_000_000),
        extra_attributes=attrs,
    )

    _sampler = StackSampler(interval_ms=sample_interval_ms)
    _sampler.start()
    _running = True
    _flush_interval_s = flush_interval_s

    def _flush_loop() -> None:
        while _running:
            time.sleep(flush_interval_s)
            if not _running:
                break
            try:
                if _sampler and _exporter:
                    samples, start_ns, duration_ns = _sampler.flush()
                    _exporter.export(samples, start_ns, duration_ns)
            except Exception:
                log.exception("Unexpected error in profiler flush loop — continuing")

    _flush_thread = threading.Thread(
        target=_flush_loop, daemon=True, name="dt-profiler-flush"
    )
    _flush_thread.start()

    # Register graceful shutdown — flushes the final window before process exits.
    # This covers: docker stop, Kubernetes SIGTERM, gunicorn graceful reload,
    # and normal process exit via sys.exit() or end-of-script.
    atexit.register(stop_profiler)
    _register_sigterm()

    log.info(
        "dt_profiler %s started — service=%s  interval=%sms  flush=%ss  target=%s",
        __version__, service_name, sample_interval_ms, flush_interval_s,
        endpoint or "(no endpoint set)",
    )


def stop_profiler() -> None:
    """
    Stop the profiler and flush the final window to Dynatrace.

    Called automatically on SIGTERM and process exit if start_profiler()
    was used. Safe to call multiple times.
    """
    global _running, _sampler, _exporter

    if not _running:
        return

    _running = False
    log.info("dt_profiler stopping — flushing final window...")

    if _sampler and _exporter:
        try:
            samples, start_ns, duration_ns = _sampler.flush()
            if samples:
                _exporter.export(samples, start_ns, duration_ns)
        except Exception:
            log.exception("Error flushing final profile window")
        finally:
            _sampler.stop()

    _sampler = None
    _exporter = None
    log.info("dt_profiler stopped")


# ── Trace correlation helpers ─────────────────────────────────────────────────

@contextmanager
def trace_context(trace_id: str, span_id: str) -> Generator[None, None, None]:
    """
    Bind a trace/span ID pair to the calling thread so that all stack samples
    captured while the context is active are tagged with those IDs.

    Use this when you already have the IDs and don't rely on opentelemetry-api:

        with trace_context(trace_id="4bf92f...", span_id="00f067..."):
            handle_request()
    """
    register_thread_trace(trace_id, span_id)
    try:
        yield
    finally:
        unregister_thread_trace()


@contextmanager
def auto_trace_context() -> Generator[None, None, None]:
    """
    Read the active OTel span from the current thread and register its IDs.
    Requires opentelemetry-api to be installed; silently no-ops if it is absent
    or if there is no active span.

        with auto_trace_context():
            handle_request()
    """
    try:
        from opentelemetry import trace as _otel_trace  # type: ignore[import]
        ctx = _otel_trace.get_current_span().get_span_context()
        if ctx.is_valid:
            register_thread_trace(
                format(ctx.trace_id, "032x"),
                format(ctx.span_id,  "016x"),
            )
    except ImportError:
        pass
    try:
        yield
    finally:
        unregister_thread_trace()


def init_flask_profiling(app) -> None:
    """
    Register before_request / teardown_request hooks on a Flask app so that
    every request is automatically tagged with its OTel trace context.

    Requires opentelemetry-api. Call once after create_app():

        from dt_profiler import start_profiler, init_flask_profiling
        start_profiler()
        init_flask_profiling(app)
    """
    @app.before_request
    def _profiler_before_request():
        try:
            from opentelemetry import trace as _otel_trace  # type: ignore[import]
            ctx = _otel_trace.get_current_span().get_span_context()
            if ctx.is_valid:
                register_thread_trace(
                    format(ctx.trace_id, "032x"),
                    format(ctx.span_id,  "016x"),
                )
        except ImportError:
            pass

    @app.teardown_request
    def _profiler_teardown_request(exc):
        unregister_thread_trace()


def _register_sigterm() -> None:
    """Register SIGTERM handler to flush before container/process is killed."""
    try:
        existing = signal.getsignal(signal.SIGTERM)

        def _handler(signum, frame):
            stop_profiler()
            # Chain to any previously registered handler (e.g. gunicorn's own handler)
            if callable(existing) and existing not in (signal.SIG_DFL, signal.SIG_IGN):
                existing(signum, frame)

        signal.signal(signal.SIGTERM, _handler)
    except (OSError, ValueError):
        # Can't set signal handler in non-main threads (gunicorn workers call
        # start_profiler in post_fork from the main thread, so this is fine).
        # If called from a non-main thread, atexit still covers normal shutdown.
        log.debug("Could not register SIGTERM handler (non-main thread) — atexit will handle shutdown")
