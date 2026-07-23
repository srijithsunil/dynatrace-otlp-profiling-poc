/*
 * StackSampler — EventPipe-based CPU sampling.
 *
 * Subscribes to the runtime's Microsoft-DotNETCore-SampleProfiler EventSource,
 * which fires a ThreadSample event every <intervalMs> milliseconds for every
 * managed thread — the .NET equivalent of Python's sys._current_frames() loop.
 *
 * For each sample:
 *   1. Check whether the thread has an active profiling scope (registered in
 *      ActiveContexts by SamplingContext when the scope is entered).
 *   2. Walk the call stack innermost → outermost via TraceCallStack.Caller.
 *   3. Accumulate: (leafFn, rootFn, depth, traceId, spanId) → count.
 *
 * We also enable Microsoft-Windows-DotNETRuntime with JITSymbols + Threading
 * keywords so TraceEvent can:
 *   a. Resolve managed method names from JIT compilation events.
 *   b. Build the OS-thread-ID → managed-thread-ID mapping used by data.Thread().
 */

using System.Collections.Concurrent;
using System.Diagnostics;
using Microsoft.Diagnostics.NETCore.Client;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Parsers;
using Microsoft.Diagnostics.Tracing.Parsers.Clr;
using Microsoft.Extensions.Logging;

namespace DynatraceOtlpProfiler;

internal sealed class StackSampler : IDisposable
{
    private readonly long    _intervalNs;
    private readonly ILogger _logger;

    // Key: Environment.CurrentManagedThreadId of the thread that opened the scope.
    // Value: (traceId, spanId) for that scope's request context.
    // SamplingContext adds/removes entries; OnThreadSample reads them.
    public ConcurrentDictionary<int, (string traceId, string spanId)> ActiveContexts { get; } = new();

    private readonly ConcurrentDictionary<SampleKey, int> _samples = new();
    private long _startNs;
    private const int MaxUniqueSamples = 50_000;

    private EventPipeSession?      _session;
    private EventPipeEventSource?  _source;
    private Thread?                _reader;

    // Frames whose full method name starts with any of these prefixes are omitted
    // from the captured stack — they're infrastructure noise, not user CPU.
    private static readonly string[] _skipPrefixes =
    [
        "System.Threading.",
        "System.Runtime.",
        "DynatraceOtlpProfiler.",
        "Microsoft.Diagnostics.",
    ];

    public StackSampler(int intervalMs, ILogger logger)
    {
        _intervalNs = intervalMs * 1_000_000L;
        _logger     = logger;
        _startNs    = NowNs();
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    public void Start()
    {
        try
        {
            var client = new DiagnosticsClient(Process.GetCurrentProcess().Id);

            var providers = new[]
            {
                // Primary: CPU thread samples with call stacks.
                new EventPipeProvider(
                    "Microsoft-DotNETCore-SampleProfiler",
                    EventLevel.Informational,
                    (long)ClrTraceEventParser.Keywords.None),

                // Required for two things:
                //  JITSymbols  → resolves managed method names from JIT events.
                //  Threading   → maps OS thread IDs to managed thread IDs so
                //                data.Thread()?.ManagedThreadId works correctly.
                new EventPipeProvider(
                    "Microsoft-Windows-DotNETRuntime",
                    EventLevel.Informational,
                    (long)(ClrTraceEventParser.Keywords.JITSymbols |
                           ClrTraceEventParser.Keywords.Threading)),
            };

            _session = client.StartEventPipeSession(providers, circularBufferMB: 512);
            _reader  = new Thread(ReadLoop) { IsBackground = true, Name = "dt-profiler-reader" };
            _reader.Start();
            _logger.LogInformation("StackSampler started via EventPipe (Microsoft-DotNETCore-SampleProfiler)");
        }
        catch (Exception ex)
        {
            // Diagnostic port not available (e.g. trimmed/single-file publish with
            // DOTNET_DiagnosticPorts=off, or insufficient permissions). Log a clear
            // warning so the user knows profiling is inactive.
            _logger.LogWarning(ex,
                "Could not start EventPipe session. " +
                "Ensure DOTNET_EnableDiagnostics is not set to 0 and the process " +
                "has access to its diagnostic port. Profiling will be inactive.");
        }
    }

    private void ReadLoop()
    {
        if (_session == null) return;
        try
        {
            _source = new EventPipeEventSource(_session.EventStream);
            _source.Clr.ThreadSample += OnThreadSample;
            _source.Process(); // blocks until session is stopped or stream ends
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "EventPipe reader exited");
        }
    }

    public void Stop()
    {
        try { _session?.Stop(); } catch { /* ignore */ }
    }

    // ── Sample handler ────────────────────────────────────────────────────────

    private void OnThreadSample(ThreadSampleTraceData data)
    {
        // Correlate the sampled thread to an active profiling scope.
        // data.Thread() returns a TraceThread populated by the Threading keyword events.
        // ManagedThreadId matches Environment.CurrentManagedThreadId on the registering thread.
        int managedId = data.Thread()?.ManagedThreadId ?? -1;
        if (managedId < 0) return;
        if (!ActiveContexts.TryGetValue(managedId, out var ctx)) return;

        // Walk the call stack innermost (leaf) → outermost (root).
        var frames = new List<string>(32);
        for (var cs = data.CallStack(); cs != null; cs = cs.Caller)
        {
            var name = cs.CodeAddress.FullMethodName;
            if (!string.IsNullOrEmpty(name) && !IsInfraFrame(name))
                frames.Add(name);
        }

        if (frames.Count == 0) return;

        var key = new SampleKey(
            LeafFunction: frames[0],
            LeafFile:     "",    // file/line require PDB loading; omit for now
            LeafLine:     0,
            RootFunction: frames[^1],
            StackDepth:   frames.Count,
            TraceId:      ctx.traceId,
            SpanId:       ctx.spanId);

        if (!_samples.ContainsKey(key) && _samples.Count >= MaxUniqueSamples)
            return;  // cap exceeded; drop rather than OOM

        _samples.AddOrUpdate(key, 1, (_, n) => n + 1);
    }

    private static bool IsInfraFrame(string name)
    {
        foreach (var prefix in _skipPrefixes)
            if (name.StartsWith(prefix, StringComparison.Ordinal))
                return true;
        return false;
    }

    // ── Flush ─────────────────────────────────────────────────────────────────

    /// <summary>Atomically snapshot and reset the sample map.</summary>
    public (Dictionary<SampleKey, int> samples, long startNs, long durationNs) Flush()
    {
        var nowNs    = NowNs();
        var snapshot = new Dictionary<SampleKey, int>(_samples);
        var start    = Interlocked.Exchange(ref _startNs, nowNs);
        _samples.Clear();
        return (snapshot, start, nowNs - start);
    }

    private static long NowNs() =>
        Stopwatch.GetTimestamp() * 1_000_000_000L / Stopwatch.Frequency;

    public void Dispose() => Stop();
}

/// <summary>
/// Immutable key for one unique (call stack, trace context) combination observed in a window.
/// </summary>
public readonly record struct SampleKey(
    string LeafFunction,  // innermost managed frame (actually executing function)
    string LeafFile,
    int    LeafLine,
    string RootFunction,  // outermost managed frame
    int    StackDepth,
    string TraceId,
    string SpanId
);
