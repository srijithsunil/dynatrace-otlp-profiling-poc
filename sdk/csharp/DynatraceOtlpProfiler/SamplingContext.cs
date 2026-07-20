using System.Collections.Concurrent;

namespace DynatraceOtlpProfiler;

/// <summary>
/// Represents one active profiling scope (a request, a named section, etc.).
/// Registered in the sampler's active-context dictionary on creation; removed on Dispose.
///
/// The sampler timer reads LeafFunction/TraceId/SpanId on every tick — these are
/// declared volatile so the timer thread always sees the latest values written by
/// the application thread without a full lock.
/// </summary>
public sealed class SamplingContext : IDisposable
{
    private readonly ConcurrentDictionary<Guid, SamplingContext> _registry;
    private bool _disposed;

    public Guid Id { get; } = Guid.NewGuid();

    // volatile: written by the request thread, read by the sampler timer thread.
    public volatile string LeafFunction;
    public volatile string LeafFile;
    public volatile int    LeafLine;
    public volatile string RootFunction;
    public volatile int    StackDepth;
    public string TraceId { get; set; } = "";
    public string SpanId  { get; set; } = "";

    internal SamplingContext(
        ConcurrentDictionary<Guid, SamplingContext> registry,
        string initialFunction,
        string traceId = "",
        string spanId  = "")
    {
        _registry    = registry;
        LeafFunction = initialFunction;
        LeafFile     = "";
        LeafLine     = 0;
        RootFunction = initialFunction;
        StackDepth   = 1;
        TraceId      = traceId;
        SpanId       = spanId;
        registry[Id] = this;
    }

    /// <summary>
    /// Update the current leaf function — call this when entering a named code section.
    /// The next sampler tick will record the new name.
    /// </summary>
    public void UpdateFunction(string functionName, string file = "", int line = 0)
    {
        LeafFunction = functionName;
        LeafFile     = file;
        LeafLine     = line;
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _registry.TryRemove(Id, out _);
    }
}
