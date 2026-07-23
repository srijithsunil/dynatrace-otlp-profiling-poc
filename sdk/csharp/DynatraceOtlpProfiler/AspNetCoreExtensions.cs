using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;

namespace DynatraceOtlpProfiler;

public static class AspNetCoreExtensions
{
    /// <summary>
    /// Registers DtProfiler middleware that marks each HTTP request thread as a
    /// profiling scope for the duration of the request.
    ///
    /// While the scope is active, EventPipe samples the thread's real call stack
    /// every 10 ms.  Trace/span IDs are read from Activity.Current, set automatically
    /// by OpenTelemetry ASP.NET Core instrumentation (AddAspNetCoreInstrumentation).
    ///
    /// Usage in Program.cs:
    ///   app.UseDtProfiling();   // call after UseRouting, before MapControllers
    /// </summary>
    public static IApplicationBuilder UseDtProfiling(this IApplicationBuilder app)
    {
        return app.Use(async (context, next) =>
        {
            using var _ = DtProfiler.AutoTraceContext($"{context.Request.Method} {context.Request.Path}");
            await next(context);
        });
    }
}
