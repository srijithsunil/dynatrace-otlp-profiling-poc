using System.Collections.Concurrent;
using System.Diagnostics;
using Microsoft.Extensions.Logging;

namespace DynatraceOtlpProfiler;

/// <summary>
/// Fires a System.Threading.Timer every <paramref name="intervalMs"/> milliseconds
/// and snapshots every registered SamplingContext.
///
/// This is the .NET equivalent of Python's sys._current_frames() loop: instead of
/// OS-level interrupt + stack walk, each request/section registers itself in
/// ActiveContexts and the timer reads LeafFunction from each context on each tick.
/// The result is the same frequency-map semantics: functions that appear in many
/// snapshots were consuming the most CPU.
/// </summary>
internal sealed class StackSampler : IDisposable
{
    private readonly long   _intervalNs;
    private readonly ILogger _logger;

    private readonly ConcurrentDictionary<Guid, SamplingContext> _activeContexts = new();
    private readonly ConcurrentDictionary<SampleKey, int>        _samples         = new();

    private long   _startTimeNs;
    private Timer? _timer;
    private volatile bool _running;
    private int _totalSamples;
    private const int MaxUniqueSamples = 50_000;

    public ConcurrentDictionary<Guid, SamplingContext> ActiveContexts => _activeContexts;

    private readonly int _intervalMs;

    public StackSampler(int intervalMs, ILogger logger)
    {
        _intervalMs = intervalMs;
        _intervalNs = intervalMs * 1_000_000L;
        _logger     = logger;
    }

    public void Start()
    {
        _startTimeNs = NowNs();
        _running     = true;
        _timer = new Timer(_ => Capture(), null, _intervalMs, _intervalMs);
    }

    public void Stop()
    {
        _running = false;
        _timer?.Dispose();
        _timer = null;
    }

    /// <summary>
    /// Atomically snapshots and resets the sample map.
    /// Returns (samples, windowStartNs, windowDurationNs).
    /// </summary>
    public (Dictionary<SampleKey, int> samples, long startNs, long durationNs) Flush()
    {
        var nowNs    = NowNs();
        var snapshot = new Dictionary<SampleKey, int>(_samples);
        var start    = _startTimeNs;
        var duration = nowNs - start;
        _samples.Clear();
        _startTimeNs  = nowNs;
        _totalSamples = 0;
        return (snapshot, start, duration);
    }

    private void Capture()
    {
        if (!_running) return;
        foreach (var ctx in _activeContexts.Values)
        {
            var key = new SampleKey(
                ctx.LeafFunction,
                ctx.LeafFile,
                ctx.LeafLine,
                ctx.RootFunction,
                ctx.StackDepth,
                ctx.TraceId,
                ctx.SpanId);

            // Enforce cap: if key is new and we're at capacity, drop the sample.
            if (!_samples.ContainsKey(key) && _samples.Count >= MaxUniqueSamples)
                continue;

            _samples.AddOrUpdate(key, 1, (_, v) => v + 1);
            Interlocked.Increment(ref _totalSamples);
        }
    }

    private static long NowNs() =>
        Stopwatch.GetTimestamp() * 1_000_000_000L / Stopwatch.Frequency;

    public void Dispose() => Stop();
}

/// <summary>
/// Immutable key for one unique (function, trace context) combination observed in a window.
/// </summary>
public readonly record struct SampleKey(
    string LeafFunction,
    string LeafFile,
    int    LeafLine,
    string RootFunction,
    int    StackDepth,
    string TraceId,
    string SpanId
);
