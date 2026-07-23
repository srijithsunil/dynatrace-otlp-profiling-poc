/*
 * DynatraceOtlpProfiler — public API
 *
 * Minimal integration (3 lines in Program.cs):
 *
 *   DtProfiler.Start();          // reads DT_ENDPOINT / DT_API_TOKEN from env
 *   app.UseDtProfiling();        // ASP.NET Core middleware (optional)
 *   // and in tight loops:
 *   using (DtProfiler.Section("MyMethod")) { DoWork(); }
 *
 * Query results in Dynatrace (DQL):
 *
 *   fetch logs
 *   | filter log.source == "continuous_profiler"
 *   | summarize cpu_ms = sum(toLong(profile.cpu_ns)) / 1000000,
 *               by:{profile.leaf_function}
 *   | sort cpu_ms desc
 */

using System.Diagnostics;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;

namespace DynatraceOtlpProfiler;

public static class DtProfiler
{
    private static StackSampler?              _sampler;
    private static OtlpExporter?              _exporter;
    private static CancellationTokenSource?   _cts;
    private static Task?                      _flushTask;
    private static volatile bool              _running;
    private static ILogger                    _logger = NullLogger.Instance;

    public const string Version = "0.2.0";

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    /// <summary>
    /// Start the continuous profiler in background threads.
    ///
    /// All parameters fall back to environment variables:
    ///   DT_ENDPOINT        — Dynatrace tenant base URL
    ///   DT_API_TOKEN       — API token with logs.ingest scope
    ///   OTEL_SERVICE_NAME  — service name shown in Dynatrace
    ///   DEPLOYMENT_ENV     — environment tag (default: "production")
    ///
    /// Registers ProcessExit and CancelKeyPress handlers so the final window
    /// is always flushed on graceful shutdown.
    /// </summary>
    public static void Start(
        string?  endpoint         = null,
        string?  apiToken         = null,
        string?  serviceName      = null,
        string   serviceVersion   = "1.0.0",
        string?  environment      = null,
        int      sampleIntervalMs = 10,
        int      flushIntervalS   = 30,
        Dictionary<string, string>? extraAttributes = null,
        ILoggerFactory? loggerFactory = null)
    {
        if (_running)
        {
            _logger.LogWarning("DtProfiler already running — ignoring duplicate Start() call");
            return;
        }

        _logger = loggerFactory?.CreateLogger("DynatraceOtlpProfiler") ?? NullLogger.Instance;

        endpoint    = endpoint    ?? Environment.GetEnvironmentVariable("DT_ENDPOINT")       ?? "";
        apiToken    = apiToken    ?? Environment.GetEnvironmentVariable("DT_API_TOKEN")      ?? "";
        serviceName = serviceName ?? Environment.GetEnvironmentVariable("OTEL_SERVICE_NAME") ?? "unknown-service";
        environment = environment ?? Environment.GetEnvironmentVariable("DEPLOYMENT_ENV")    ?? "production";

        if (string.IsNullOrEmpty(endpoint))
            _logger.LogWarning(
                "DT_ENDPOINT not set — profiler will sample stacks but not export. " +
                "Set DT_ENDPOINT to your Dynatrace tenant URL.");
        if (string.IsNullOrEmpty(apiToken))
            _logger.LogWarning(
                "DT_API_TOKEN not set — exports will be rejected with 401. " +
                "Set DT_API_TOKEN to a token with logs.ingest scope.");

        var attrs = new Dictionary<string, string>
        {
            ["host.name"] = Environment.MachineName,
        };
        if (extraAttributes != null)
            foreach (var (k, v) in extraAttributes) attrs[k] = v;

        _sampler  = new StackSampler(sampleIntervalMs, _logger);
        _exporter = new OtlpExporter(
            endpoint, apiToken, serviceName, serviceVersion, environment,
            sampleIntervalMs * 1_000_000L, attrs, _logger);

        _sampler.Start();
        _running = true;
        _cts     = new CancellationTokenSource();

        _flushTask = RunFlushLoopAsync(flushIntervalS, _cts.Token);

        AppDomain.CurrentDomain.ProcessExit += (_, _) => Stop();
        Console.CancelKeyPress              += (_, _) => Stop();

        _logger.LogInformation(
            "DtProfiler {v} started (EventPipe) — service={s} interval={i}ms flush={f}s target={t}",
            Version, serviceName, sampleIntervalMs, flushIntervalS,
            string.IsNullOrEmpty(endpoint) ? "(no endpoint set)" : endpoint);
    }

    /// <summary>
    /// Stop the profiler and flush the final window to Dynatrace.
    /// Called automatically on process exit. Safe to call multiple times.
    /// </summary>
    public static void Stop()
    {
        if (!_running) return;
        _running = false;
        _logger.LogInformation("DtProfiler stopping — flushing final window...");

        _cts?.Cancel();
        try { _flushTask?.Wait(TimeSpan.FromSeconds(5)); } catch { /* ignore cancellation */ }

        if (_sampler != null && _exporter != null)
        {
            try
            {
                var (samples, startNs, durationNs) = _sampler.Flush();
                if (samples.Count > 0)
                    _exporter.ExportAsync(samples, startNs, durationNs).GetAwaiter().GetResult();
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error flushing final profile window");
            }
            finally
            {
                _sampler.Stop();
            }
        }

        _sampler?.Dispose();
        _exporter?.Dispose();
        _sampler  = null;
        _exporter = null;
        _logger.LogInformation("DtProfiler stopped");
    }

    // ── Profiling scope helpers ───────────────────────────────────────────────

    /// <summary>
    /// Marks the current thread as inside a named profiling scope.
    ///
    /// While the scope is active, EventPipe samples this thread's real call stack
    /// every 10 ms and attributes the samples to it.  The <c>name</c> parameter is
    /// used for logging only — <c>profile.leaf_function</c> in Dynatrace is the
    /// actual innermost managed frame, not this label.
    ///
    ///   using (DtProfiler.Section("MatrixMultiply")) { DoWork(); }
    ///
    /// No-ops (returns a do-nothing disposable) when the profiler is not running.
    /// </summary>
    public static IDisposable Section(
        string  name,
        string  file    = "",
        int     line    = 0,
        string? traceId = null,
        string? spanId  = null)
    {
        if (_sampler == null) return NullDisposable.Instance;
        return new SamplingContext(_sampler.ActiveContexts, traceId ?? "", spanId ?? "");
    }

    /// <summary>
    /// Opens a profiling scope with an explicit trace/span ID pair (no OTel dependency).
    ///
    ///   using (DtProfiler.TraceContext(traceId, spanId, "HandleRequest")) { … }
    /// </summary>
    public static IDisposable TraceContext(
        string traceId,
        string spanId,
        string sectionName = "request")
    {
        if (_sampler == null) return NullDisposable.Instance;
        return new SamplingContext(_sampler.ActiveContexts, traceId, spanId);
    }

    /// <summary>
    /// Opens a profiling scope reading trace/span IDs from <see cref="Activity.Current"/>.
    /// Works automatically with any OTel instrumentation that sets Activity.Current.
    /// Silently no-ops if there is no active Activity or if the profiler is not running.
    ///
    ///   using (DtProfiler.AutoTraceContext("HandleRequest")) { … }
    /// </summary>
    public static IDisposable AutoTraceContext(string sectionName = "request")
    {
        if (_sampler == null) return NullDisposable.Instance;
        var activity = Activity.Current;
        var traceId  = activity?.TraceId.ToString() ?? "";
        var spanId   = activity?.SpanId.ToString()  ?? "";
        return new SamplingContext(_sampler.ActiveContexts, traceId, spanId);
    }

    // ── Internal ──────────────────────────────────────────────────────────────

    private static async Task RunFlushLoopAsync(int intervalS, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(TimeSpan.FromSeconds(intervalS), ct); }
            catch (TaskCanceledException) { break; }

            if (_sampler == null || _exporter == null) break;
            try
            {
                var (samples, startNs, durationNs) = _sampler.Flush();
                await _exporter.ExportAsync(samples, startNs, durationNs);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Unexpected error in profiler flush loop — continuing");
            }
        }
    }
}

internal sealed class NullDisposable : IDisposable
{
    public static readonly NullDisposable Instance = new();
    public void Dispose() { }
}
