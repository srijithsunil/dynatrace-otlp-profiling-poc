using System.Collections.Concurrent;

namespace DynatraceOtlpProfiler;

/// <summary>
/// Marks a thread as being inside a named profiling scope.
///
/// On creation, registers Environment.CurrentManagedThreadId in the sampler's
/// ActiveContexts dictionary so that EventPipe thread samples for this thread
/// are attributed to the correct trace context.  Removes the registration on
/// Dispose (end of request / section).
///
/// The actual call stack is captured by the EventPipe subscriber in StackSampler —
/// no LeafFunction label needs to be set manually.
/// </summary>
public sealed class SamplingContext : IDisposable
{
    private readonly ConcurrentDictionary<int, (string traceId, string spanId)> _registry;
    private readonly int  _threadId;
    private          bool _disposed;

    internal SamplingContext(
        ConcurrentDictionary<int, (string traceId, string spanId)> registry,
        string traceId = "",
        string spanId  = "")
    {
        _registry = registry;
        _threadId = Environment.CurrentManagedThreadId;
        registry[_threadId] = (traceId, spanId);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _registry.TryRemove(_threadId, out _);
    }
}
