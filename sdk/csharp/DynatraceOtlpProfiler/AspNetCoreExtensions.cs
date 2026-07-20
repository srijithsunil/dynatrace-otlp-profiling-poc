using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;

namespace DynatraceOtlpProfiler;

public static class AspNetCoreExtensions
{
    /// <summary>
    /// Registers DtProfiler middleware that creates one profiling scope per HTTP request.
    ///
    /// The initial leaf function name is "{METHOD} {Path}". Once routing completes
    /// (inside OnStarting), it is refined to the matched endpoint's display name so
    /// you see e.g. "GET /fibonacci" rather than a generic route pattern.
    ///
    /// Trace/span IDs are read from Activity.Current, which is set automatically by
    /// OpenTelemetry ASP.NET Core instrumentation (AddAspNetCoreInstrumentation).
    ///
    /// Usage in Program.cs:
    ///   app.UseDtProfiling();   // call after UseRouting, before MapControllers
    /// </summary>
    public static IApplicationBuilder UseDtProfiling(this IApplicationBuilder app)
    {
        return app.Use(async (context, next) =>
        {
            var sectionName = $"{context.Request.Method} {context.Request.Path}";
            using var scope = DtProfiler.AutoTraceContext(sectionName);

            // Refine the section name once the matched route is known.
            // OnStarting fires before the first response byte is written.
            context.Response.OnStarting(() =>
            {
                var endpoint = context.GetEndpoint();
                if (endpoint?.DisplayName is { } name && scope is SamplingContext sc)
                    sc.UpdateFunction(name);
                return Task.CompletedTask;
            });

            await next(context);
        });
    }
}
