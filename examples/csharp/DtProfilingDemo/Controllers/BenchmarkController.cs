/*
 * CPU hotspot demo endpoints — mirrors sample-app/app.py
 *
 * Each endpoint wraps its CPU-heavy work in DtProfiler.Section() so the profiler
 * records the actual method name as profile.leaf_function, not just the route.
 * The trace_id field in every JSON response lets you copy it straight into DQL:
 *
 *   fetch logs
 *   | filter log.source == "continuous_profiler" and trace.id == "<paste here>"
 *   | summarize cpu_ms = sum(toLong(profile.cpu_ns))/1000000, by:{profile.leaf_function}
 *   | sort cpu_ms desc
 */

using System.Diagnostics;
using DynatraceOtlpProfiler;
using Microsoft.AspNetCore.Mvc;

namespace DtProfilingDemo.Controllers;

[ApiController]
public class BenchmarkController : ControllerBase
{
    [HttpGet("/health")]
    public IActionResult Health() =>
        Ok(new { status = "ok", service = "csharp-profiling-demo" });

    [HttpGet("/fibonacci")]
    public IActionResult Fibonacci([FromQuery] int n = 30)
    {
        n = Math.Min(n, 38);
        var sw = Stopwatch.StartNew();
        long result;
        using (DtProfiler.Section("Fibonacci", file: "BenchmarkController.cs", line: 28))
            result = DoFibonacci(n);
        return Ok(new
        {
            n,
            result,
            elapsed_ms = Math.Round(sw.Elapsed.TotalMilliseconds, 2),
            trace_id   = CurrentTraceId(),
        });
    }

    [HttpGet("/primes")]
    public IActionResult Primes([FromQuery] int limit = 500_000)
    {
        limit = Math.Min(limit, 2_000_000);
        var sw = Stopwatch.StartNew();
        int count;
        using (DtProfiler.Section("SieveOfEratosthenes", file: "BenchmarkController.cs", line: 43))
            count = SieveOfEratosthenes(limit);
        return Ok(new
        {
            limit,
            count,
            elapsed_ms = Math.Round(sw.Elapsed.TotalMilliseconds, 2),
            trace_id   = CurrentTraceId(),
        });
    }

    [HttpGet("/matrix")]
    public IActionResult Matrix([FromQuery] int size = 80)
    {
        size = Math.Min(size, 200);
        var sw = Stopwatch.StartNew();
        using (DtProfiler.Section("MatrixMultiply", file: "BenchmarkController.cs", line: 58))
            MatrixMultiply(size);
        return Ok(new
        {
            size,
            elapsed_ms = Math.Round(sw.Elapsed.TotalMilliseconds, 2),
            trace_id   = CurrentTraceId(),
        });
    }

    [HttpGet("/sort")]
    public IActionResult Sort([FromQuery] int n = 5_000)
    {
        n = Math.Min(n, 50_000);
        var sw = Stopwatch.StartNew();
        using (DtProfiler.Section("RepeatedSort", file: "BenchmarkController.cs", line: 72))
            RepeatedSort(n);
        return Ok(new
        {
            n,
            elapsed_ms = Math.Round(sw.Elapsed.TotalMilliseconds, 2),
            trace_id   = CurrentTraceId(),
        });
    }

    // ── CPU hotspot implementations ───────────────────────────────────────────

    private static long DoFibonacci(int n)
    {
        if (n <= 1) return n;
        return DoFibonacci(n - 1) + DoFibonacci(n - 2);
    }

    private static int SieveOfEratosthenes(int limit)
    {
        var sieve = new bool[limit + 1];
        Array.Fill(sieve, true);
        sieve[0] = sieve[1] = false;
        for (int i = 2; (long)i * i <= limit; i++)
            if (sieve[i])
                for (int j = i * i; j <= limit; j += i)
                    sieve[j] = false;
        int count = 0;
        foreach (var b in sieve) if (b) count++;
        return count;
    }

    private static void MatrixMultiply(int size)
    {
        var rng = new Random(42);
        var a   = new double[size, size];
        var b   = new double[size, size];
        var c   = new double[size, size];
        for (int i = 0; i < size; i++)
            for (int j = 0; j < size; j++)
            { a[i, j] = rng.NextDouble(); b[i, j] = rng.NextDouble(); }
        for (int i = 0; i < size; i++)
            for (int k = 0; k < size; k++)
                for (int j = 0; j < size; j++)
                    c[i, j] += a[i, k] * b[k, j];
    }

    private static void RepeatedSort(int n, int iterations = 25)
    {
        var rng  = new Random(42);
        var data = Enumerable.Range(0, n).Select(_ => rng.Next(100_000)).ToList();
        for (int i = 0; i < iterations; i++)
            data = [.. data.OrderBy(x => x % 7).ThenByDescending(x => x)];
    }

    private static string CurrentTraceId()
    {
        var ctx = Activity.Current?.Context;
        return ctx.HasValue && ctx.Value.IsValid() ? ctx.Value.TraceId.ToString() : "";
    }
}
