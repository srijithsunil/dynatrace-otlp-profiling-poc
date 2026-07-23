# C# — Dynatrace OTLP Continuous Profiling

ASP.NET Core 8 demo that exports profiling data as OTLP Logs to Dynatrace.
Mirrors the Python `sample-app/` — same endpoints, same DQL queries, same trace correlation.

---

## How the C# profiler works

Python can call `sys._current_frames()` to snapshot every OS thread's actual call stack at
any moment. The C# SDK uses the **EventPipe** equivalent — the .NET runtime's own
`Microsoft-DotNETCore-SampleProfiler` EventSource, which fires a `ThreadSample` event with
a real managed call stack every 10 ms for every active thread.

1. Each request calls `DtProfiler.AutoTraceContext()` (via `app.UseDtProfiling()`) or
   `DtProfiler.Section("MethodName")` to register the current thread in a shared dictionary.
2. The EventPipe subscriber receives `ThreadSample` events every 10 ms, walks the thread's
   real call stack innermost → outermost, and records the leaf function (actually executing
   frame at that moment).
3. After 30 seconds the sampler flushes: the frequency map becomes OTLP log records
   (one per unique stack × trace context pair) and is POSTed to Dynatrace.

Unlike the old cooperative model (which recorded the same static label for every sample),
EventPipe captures what the CPU is **actually doing** at each tick — recursive sub-calls,
library internals — matching Python's depth fidelity.

```
request arrives
    ↓
UseDtProfiling() middleware
    → DtProfiler.AutoTraceContext("GET /fibonacci")   ← registers thread
        ↓
BenchmarkController.Fibonacci()
    → (calls RecursiveHelper → RecursiveHelper → ...)
        ↓
        [EventPipe fires every 10ms — records actual leaf frame at that moment]
        [10 ms #1: "BenchmarkController.RecursiveHelper(Int32)"]
        [10 ms #2: "BenchmarkController.RecursiveHelper(Int32)"]
        [10 ms #3: "BenchmarkController.Fibonacci(Int32)"]
        ↓
    scope disposed → thread unregistered
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
# Via NuGet (recommended)
dotnet add package DynatraceOtlpProfiler

# Or via ProjectReference from this repo (adjust the path):
dotnet add reference ../../sdk/csharp/DynatraceOtlpProfiler/DynatraceOtlpProfiler.csproj
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
