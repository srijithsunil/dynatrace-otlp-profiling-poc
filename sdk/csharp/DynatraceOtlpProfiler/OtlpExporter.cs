using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Logging;

namespace DynatraceOtlpProfiler;

/// <summary>
/// Converts SampleKey frequency maps to OTLP Logs JSON and POSTs them to
/// {DT_ENDPOINT}/api/v2/otlp/v1/logs.
///
/// Production features:
///   • Retry with exponential backoff — 3 attempts (2 s, 4 s delays)
///   • No retry on 4xx — client errors need a config fix, not hammering
///   • Circuit breaker — opens after 5 consecutive failures, resets after 60 s
///   • Single HttpClient instance (keep-alive / connection pooling)
/// </summary>
internal sealed class OtlpExporter : IDisposable
{
    private readonly string _endpoint;
    private readonly string _serviceName;
    private readonly string _serviceVersion;
    private readonly string _environment;
    private readonly long   _intervalNs;
    private readonly Dictionary<string, string> _extraAttrs;
    private readonly ILogger    _logger;
    private readonly HttpClient _http;

    private int  _consecutiveFailures;
    private long _circuitOpenUntilTicks;          // Environment.TickCount64 ms
    private const int CircuitOpenAfter  = 5;
    private const int CircuitBackoffMs  = 60_000;
    private static readonly int[] RetryDelaysMs = [2_000, 4_000];

    public OtlpExporter(
        string endpoint,
        string apiToken,
        string serviceName,
        string serviceVersion,
        string environment,
        long   intervalNs,
        Dictionary<string, string>? extraAttrs,
        ILogger logger)
    {
        _endpoint       = endpoint.TrimEnd('/');
        _serviceName    = serviceName;
        _serviceVersion = serviceVersion;
        _environment    = environment;
        _intervalNs     = intervalNs;
        _extraAttrs     = extraAttrs ?? [];
        _logger         = logger;

        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        _http.DefaultRequestHeaders.Authorization =
            new AuthenticationHeaderValue("Api-Token", apiToken);
        _http.DefaultRequestHeaders.Accept.Add(
            new MediaTypeWithQualityHeaderValue("application/json"));
    }

    public async Task<bool> ExportAsync(
        Dictionary<SampleKey, int> samples,
        long startNs,
        long durationNs)
    {
        if (samples.Count == 0) return true;

        if (Environment.TickCount64 < _circuitOpenUntilTicks)
        {
            var remaining = (_circuitOpenUntilTicks - Environment.TickCount64) / 1_000;
            _logger.LogWarning(
                "Circuit breaker open — skipping export ({s}s remaining). " +
                "Check DT_ENDPOINT and DT_API_TOKEN.", remaining);
            return false;
        }

        var records = BuildLogRecords(samples, startNs, durationNs);
        var payload = BuildPayload(records);
        var url     = $"{_endpoint}/api/v2/otlp/v1/logs";
        return await PostWithRetryAsync(url, payload, samples.Values.Sum(), durationNs);
    }

    // ── OTLP record builder ───────────────────────────────────────────────────

    private List<object> BuildLogRecords(
        Dictionary<SampleKey, int> samples,
        long startNs,
        long durationNs)
    {
        var records = new List<object>(samples.Count);

        foreach (var (key, count) in samples)
        {
            var cpuNs = count * _intervalNs;

            // Show a condensed view: root → ... → leaf (matches Python traceback style).
            string body;
            if (key.StackDepth <= 1 || key.RootFunction == key.LeafFunction)
            {
                body = string.IsNullOrEmpty(key.LeafFile)
                    ? $"  in {key.LeafFunction}"
                    : $"  File \"{key.LeafFile}\", line {key.LeafLine}, in {key.LeafFunction}";
            }
            else
            {
                var middle = key.StackDepth > 2 ? $"\n  ... ({key.StackDepth - 2} frames) ...\n" : "\n";
                var leaf   = string.IsNullOrEmpty(key.LeafFile)
                    ? $"  in {key.LeafFunction}"
                    : $"  File \"{key.LeafFile}\", line {key.LeafLine}, in {key.LeafFunction}";
                body = $"  in {key.RootFunction}{middle}{leaf}";
            }

            var attrs = new List<object>
            {
                Attr("log.source",                 "continuous_profiler"),
                Attr("profile.sample_count",       count,    isInt: true),
                Attr("profile.cpu_ns",             cpuNs,    isInt: true),
                Attr("profile.leaf_function",      key.LeafFunction),
                Attr("profile.leaf_file",          Path.GetFileName(key.LeafFile)),
                Attr("profile.leaf_line",          key.LeafLine, isInt: true),
                Attr("profile.root_function",      key.RootFunction),
                Attr("profile.stack_depth",        key.StackDepth, isInt: true),
                Attr("profile.window_start_ns",    startNs,  isInt: true),
                Attr("profile.window_duration_ns", durationNs, isInt: true),
                Attr("telemetry.sdk.language",     "csharp"),
            };

            if (!string.IsNullOrEmpty(key.TraceId))
                attrs.Add(Attr("trace.id", key.TraceId));
            if (!string.IsNullOrEmpty(key.SpanId))
                attrs.Add(Attr("span.id", key.SpanId));

            var record = new Dictionary<string, object>
            {
                ["timeUnixNano"]         = startNs.ToString(),
                ["observedTimeUnixNano"] = startNs.ToString(),
                ["severityNumber"]       = 9,
                ["severityText"]         = "INFO",
                ["body"]                 = new { stringValue = body },
                ["attributes"]           = attrs,
            };

            // traceId / spanId are first-class OTLP log record fields (§4.3).
            // Dynatrace reads these to link the record to a distributed trace.
            if (!string.IsNullOrEmpty(key.TraceId)) record["traceId"] = key.TraceId;
            if (!string.IsNullOrEmpty(key.SpanId))  record["spanId"]  = key.SpanId;

            records.Add(record);
        }

        return records;
    }

    private object BuildPayload(List<object> records)
    {
        var resAttrs = new List<object>
        {
            Attr("service.name",           _serviceName),
            Attr("service.version",        _serviceVersion),
            Attr("deployment.environment", _environment),
            Attr("telemetry.sdk.name",     "dynatrace-otlp-profiler"),
            Attr("telemetry.sdk.language", "csharp"),
            Attr("telemetry.sdk.version",  "0.2.0"),
        };
        foreach (var (k, v) in _extraAttrs)
            resAttrs.Add(Attr(k, v));

        return new
        {
            resourceLogs = new[]
            {
                new
                {
                    resource = new { attributes = resAttrs },
                    scopeLogs = new[]
                    {
                        new
                        {
                            scope      = new { name = "dynatrace-otlp-profiler", version = "0.2.0" },
                            logRecords = records,
                        }
                    }
                }
            }
        };
    }

    // ── HTTP + retry ──────────────────────────────────────────────────────────

    private async Task<bool> PostWithRetryAsync(
        string url,
        object payload,
        int    totalSamples,
        long   durationNs)
    {
        var json    = JsonSerializer.Serialize(payload);
        var delays  = RetryDelaysMs.Append(0).ToArray();   // 3 attempts total

        for (int attempt = 0; attempt < delays.Length; attempt++)
        {
            try
            {
                using var content = new StringContent(json, Encoding.UTF8, "application/json");
                using var resp    = await _http.PostAsync(url, content);
                var status = (int)resp.StatusCode;

                if (status is 200 or 202 or 204)
                {
                    _consecutiveFailures = 0;
                    _logger.LogInformation(
                        "Exported profile → HTTP {s} | samples={n} window={w:F1}s",
                        status, totalSamples, durationNs / 1e9);
                    return true;
                }

                if (status is >= 400 and < 500)
                {
                    var body = await resp.Content.ReadAsStringAsync();
                    _logger.LogError(
                        "Export rejected HTTP {s} (not retrying — check token/endpoint): {b}",
                        status, body[..Math.Min(300, body.Length)]);
                    RecordFailure();
                    return false;
                }

                _logger.LogWarning(
                    "Export HTTP {s} on attempt {a}/{t}, will retry",
                    status, attempt + 1, delays.Length);
            }
            catch (TaskCanceledException)
            {
                _logger.LogWarning(
                    "Export timed out on attempt {a}/{t}, will retry",
                    attempt + 1, delays.Length);
            }
            catch (HttpRequestException ex)
            {
                _logger.LogWarning(
                    "Export connection error on attempt {a}/{t}: {e}, will retry",
                    attempt + 1, delays.Length, ex.Message);
            }

            if (attempt < delays.Length - 1 && delays[attempt] > 0)
                await Task.Delay(delays[attempt]);
        }

        _logger.LogError(
            "Export failed after {n} attempts — profile window dropped. " +
            "Check network connectivity to {ep}.", delays.Length, _endpoint);
        RecordFailure();
        return false;
    }

    private void RecordFailure()
    {
        _consecutiveFailures++;
        if (_consecutiveFailures >= CircuitOpenAfter)
        {
            _circuitOpenUntilTicks = Environment.TickCount64 + CircuitBackoffMs;
            _logger.LogError(
                "Circuit breaker opened after {n} consecutive failures — " +
                "exports paused for {s}s. Verify {ep} is reachable.",
                _consecutiveFailures, CircuitBackoffMs / 1_000, _endpoint);
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private static object Attr(string key, object value, bool isInt = false) => isInt
        ? new { key, value = new { intValue    = value.ToString() } }
        : new { key, value = new { stringValue = value.ToString() } };

    public void Dispose() => _http.Dispose();
}
