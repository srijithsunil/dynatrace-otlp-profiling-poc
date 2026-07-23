# DynatraceOtlpProfiler

Continuous CPU profiler for .NET 8+ that exports stack samples as **OTLP Logs** directly
to Dynatrace — no OTel Collector required.

## How it works

Uses the .NET runtime's **EventPipe** (`Microsoft-DotNETCore-SampleProfiler`) to capture
real managed call stacks every 10 ms — the .NET equivalent of Python's `sys._current_frames()`.
`profile.leaf_function` reflects what the CPU is *actually* executing at each sample point,
not a static label. Every 30 seconds the frequency map is serialised as OTLP log records
(`log.source = "continuous_profiler"`) and POSTed to your Dynatrace tenant. Query results in DQL:

```dql
fetch logs
| filter log.source == "continuous_profiler"
| summarize cpu_ms = sum(toLong(profile.cpu_ns)) / 1000000, by:{profile.leaf_function}
| sort cpu_ms desc
```

## Quickstart — ASP.NET Core

```bash
dotnet add package DynatraceOtlpProfiler
```

```csharp
// Program.cs
using DynatraceOtlpProfiler;

// Reads DT_ENDPOINT and DT_API_TOKEN from environment automatically.
DtProfiler.Start(loggerFactory: app.Services.GetRequiredService<ILoggerFactory>());

// Per-request middleware: tags every request with its OTel trace/span ID.
app.UseDtProfiling();

app.Run();
```

```csharp
// Annotate CPU-heavy methods
using (DtProfiler.Section("ParseCsvFile", file: "MyService.cs", line: 42))
{
    // every 10 ms timer tick records "ParseCsvFile" as the leaf function
}
```

## Environment variables

| Variable | Description |
|---|---|
| `DT_ENDPOINT` | Dynatrace tenant base URL — e.g. `https://abc123.live.dynatrace.com` |
| `DT_API_TOKEN` | API token with `logs.ingest` scope |
| `OTEL_SERVICE_NAME` | Service name shown in Dynatrace |
| `OTEL_SERVICE_VERSION` | Version tag (default: `1.0.0`) |
| `DEPLOYMENT_ENV` | Environment tag (default: `production`) |

## Trace correlation

```csharp
// Automatic — reads Activity.Current set by any OTel instrumentation:
using (DtProfiler.AutoTraceContext("HandleRequest")) { … }

// Manual — supply explicit IDs with no OTel dependency:
using (DtProfiler.TraceContext(traceId, spanId, "HandleRequest")) { … }
```

## Minimal API / generic host

```csharp
// Minimal API
var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();
DtProfiler.Start();
app.UseDtProfiling();
app.MapGet("/", () => Results.Ok());
app.Run();
```

```csharp
// Generic host (worker service, console app)
DtProfiler.Start();  // call before host.Run()
using (DtProfiler.Section("ProcessBatch")) { … }
```

## Full source & examples

[github.com/srijithsunil/dynatrace-otlp-profiling-poc](https://github.com/srijithsunil/dynatrace-otlp-profiling-poc)
