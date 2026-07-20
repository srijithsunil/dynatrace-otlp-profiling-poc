# C# — Dynatrace OTLP Continuous Profiling

ASP.NET Core 8 demo that exports profiling data as OTLP Logs to Dynatrace.
Mirrors the Python `sample-app/` — same endpoints, same DQL queries, same trace correlation.

---

## How the C# profiler works

Python can call `sys._current_frames()` to snapshot every OS thread's call stack at any
moment. .NET has no equivalent managed API.

The C# SDK uses a **cooperative sampling** model instead:

1. Each request (or named code section) calls `DtProfiler.AutoTraceContext()` or
   `DtProfiler.Section("MethodName")` to create a `SamplingContext` and register it in a
   shared `ConcurrentDictionary`.
2. A `System.Threading.Timer` fires every 10 ms and iterates all registered contexts,
   recording their current `LeafFunction` as one sample.
3. After 30 seconds the sampler flushes: the frequency map becomes OTLP log records
   (one per unique function × trace context pair) and is POSTed to Dynatrace.

This gives the same statistical profile semantics as Python: a function that appears in
many 10 ms snapshots was consuming CPU. The only difference is that attribution is
**per named scope**, not per OS thread stack frame.

```
request arrives
    ↓
UseDtProfiling() middleware
    → DtProfiler.AutoTraceContext("GET /fibonacci")   ← registers scope
        ↓
BenchmarkController.Fibonacci()
    → DtProfiler.Section("Fibonacci")                 ← narrows leaf function
        ↓
        [timer fires every 10ms — records "Fibonacci" as a sample]
        ↓
    scope disposed → removed from registry
        ↓
[every 30s] → flush → OTLP Logs → Dynatrace
```

---

## Quick start (Docker Compose)

```bash
# From repo root
cp .env.example .env
# Edit .env: set DT_ENDPOINT and DT_API_TOKEN

docker compose up csharp-demo validator --build
```

The C# demo listens on **port 8081**. Endpoints:

| Endpoint | Hotspot |
|---|---|
| `GET /health` | — |
| `GET /fibonacci?n=35` | Recursive Fibonacci |
| `GET /primes?limit=1000000` | Sieve of Eratosthenes |
| `GET /matrix?size=100` | Naive matrix multiply |
| `GET /sort?n=10000` | Repeated sort |

---

## Add profiling to your own C# app

### 1 — Reference the SDK

```bash
# From your project directory (adjust the path):
dotnet add reference ../../sdk/csharp/DynatraceOtlpProfiler/DynatraceOtlpProfiler.csproj

# Or once published to NuGet:
# dotnet add package DynatraceOtlpProfiler
```

### 2 — Start the profiler in `Program.cs`

```csharp
using DynatraceOtlpProfiler;

// Call before app.Run() — reads DT_ENDPOINT / DT_API_TOKEN from env automatically.
DtProfiler.Start(loggerFactory: app.Services.GetRequiredService<ILoggerFactory>());

// Optional: register per-request middleware (reads Activity.Current for trace IDs).
app.UseDtProfiling();
```

### 3 — Annotate CPU-heavy methods

```csharp
using (DtProfiler.Section("ParseCsvFile", file: "MyService.cs", line: 42))
{
    // work here — every 10ms timer tick records "ParseCsvFile"
}
```

### Environment variables

| Variable | Description |
|---|---|
| `DT_ENDPOINT` | Dynatrace tenant base URL, e.g. `https://abc123.live.dynatrace.com` |
| `DT_API_TOKEN` | API token with `logs.ingest` scope |
| `OTEL_SERVICE_NAME` | Service name shown in Dynatrace |
| `OTEL_SERVICE_VERSION` | Version tag (default: `1.0.0`) |
| `DEPLOYMENT_ENV` | Environment tag (default: `production`) |

---

## Trace correlation

`UseDtProfiling()` automatically reads `Activity.Current` set by OTel instrumentation.
For manual control:

```csharp
// Explicit IDs (no OTel dependency):
using (DtProfiler.TraceContext(traceId, spanId, "HandleRequest")) { … }

// Auto-read from Activity.Current:
using (DtProfiler.AutoTraceContext("HandleRequest")) { … }
```

---

## DQL queries

```dql
// Top CPU consumers
fetch logs
| filter log.source == "continuous_profiler" and `service.name` == "csharp-profiling-demo"
| summarize cpu_ms = sum(toLong(profile.cpu_ns)) / 1000000, by:{profile.leaf_function}
| sort cpu_ms desc

// CPU hotspots for one specific trace
fetch logs
| filter log.source == "continuous_profiler" and trace.id == "<paste trace_id from response>"
| summarize cpu_ms = sum(toLong(profile.cpu_ns)) / 1000000, by:{profile.leaf_function}
| sort cpu_ms desc

// All traces that hit a hot function
fetch logs
| filter log.source == "continuous_profiler" and profile.leaf_function == "MatrixMultiply"
| fields trace.id, span.id, profile.cpu_ns, profile.leaf_file
| sort profile.cpu_ns desc
```

---

## Project structure

```
sdk/csharp/DynatraceOtlpProfiler/
  SamplingContext.cs          — one active profiling scope (IDisposable)
  StackSampler.cs             — 10ms timer + frequency map
  OtlpExporter.cs             — OTLP Logs HTTP export + retry + circuit breaker
  Profiler.cs                 — public API: Start / Stop / Section / TraceContext
  AspNetCoreExtensions.cs     — UseDtProfiling() middleware

examples/csharp/DtProfilingDemo/
  Program.cs                  — app startup, OTel wiring, profiler start
  Controllers/
    BenchmarkController.cs    — /fibonacci /primes /matrix /sort /health
  Dockerfile                  — multi-stage, context = repo root
```
