/*
 * C# OTLP profiling demo — mirrors sample-app/app.py
 *
 * Exports two signals to DT_ENDPOINT:
 *   OTLP Logs   → /api/v2/otlp/v1/logs   (profiling data, one record per section)
 *   OTLP Traces → /api/v2/otlp/v1/traces (one span per HTTP request)
 *
 * Profile records carry traceId/spanId so they link to their request trace in
 * the Distributed Traces app — same DQL queries as the Python sample:
 *
 *   fetch logs
 *   | filter log.source == "continuous_profiler" and trace.id == "<id>"
 *   | summarize cpu_ms = sum(toLong(profile.cpu_ns))/1000000, by:{profile.leaf_function}
 *   | sort cpu_ms desc
 */

using DynatraceOtlpProfiler;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

var builder = WebApplication.CreateBuilder(args);

var dtEndpoint  = Environment.GetEnvironmentVariable("DT_ENDPOINT")         ?? "";
var dtApiToken  = Environment.GetEnvironmentVariable("DT_API_TOKEN")         ?? "";
var serviceName = Environment.GetEnvironmentVariable("OTEL_SERVICE_NAME")    ?? "csharp-profiling-demo";
var serviceVer  = Environment.GetEnvironmentVariable("OTEL_SERVICE_VERSION") ?? "1.0.0";

// OpenTelemetry tracing — exports spans to Dynatrace when a real endpoint is set;
// falls back to no-op exporter in dev mode (DT_ENDPOINT unset or pointing at validator).
var isDev = string.IsNullOrEmpty(dtEndpoint) || dtEndpoint.Contains("validator");

builder.Services.AddOpenTelemetry()
    .WithTracing(b =>
    {
        b.SetResourceBuilder(
                ResourceBuilder.CreateDefault()
                    .AddService(serviceName, serviceVersion: serviceVer))
            .AddAspNetCoreInstrumentation();

        if (!isDev && !string.IsNullOrEmpty(dtEndpoint))
        {
            b.AddOtlpExporter(o =>
            {
                o.Endpoint = new Uri($"{dtEndpoint.TrimEnd('/')}/api/v2/otlp/v1/traces");
                o.Headers  = $"Authorization=Api-Token {dtApiToken}";
            });
            builder.Logging.AddConsole();
        }
        // In dev mode traces are not exported — trace IDs are still visible in the
        // JSON responses so you can copy them into DQL for local testing.
    });

builder.Services.AddControllers();

var app = builder.Build();

// Start profiler before serving requests — every request from here on is profiled.
DtProfiler.Start(loggerFactory: app.Services.GetRequiredService<ILoggerFactory>());

// Register per-request profiling scope; reads Activity.Current for trace correlation.
app.UseDtProfiling();

app.MapControllers();

app.Run();
