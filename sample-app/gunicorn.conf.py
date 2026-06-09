"""
Gunicorn configuration for the profiling demo app.

Important: preload_app = False (default) so each worker imports the app
independently. start_profiler() is called at module level in app.py, so
each worker gets its own profiler instance on import. The post_fork hook
is a safety net for deployments that enable preloading.
"""
import os

bind    = "0.0.0.0:8080"
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
keepalive  = 5
preload_app = False   # each worker imports fresh → start_profiler() runs per worker

accesslog  = "-"    # stdout
errorlog   = "-"    # stderr
loglevel   = os.getenv("LOG_LEVEL", "info")


def post_fork(server, worker):
    """
    Safety net: if preload_app is ever enabled, this ensures the profiler
    starts in each worker rather than only in the master process.
    The _running check in start_profiler() makes this a no-op when the
    profiler is already running (non-preload case).
    """
    from dt_profiler import start_profiler
    start_profiler()


def worker_exit(server, worker):
    """Flush the final profile window when a worker exits gracefully."""
    from dt_profiler import stop_profiler
    stop_profiler()
