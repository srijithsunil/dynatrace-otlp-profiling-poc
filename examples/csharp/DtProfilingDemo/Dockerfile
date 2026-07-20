# Build context must be the repo root (docker-compose.yml sets context: .)
# so that both the SDK and the app project can be copied in.

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src

# Copy SDK class library
COPY sdk/csharp/DynatraceOtlpProfiler/ sdk/csharp/DynatraceOtlpProfiler/

# Copy example project (ProjectReference resolves to the SDK above)
COPY examples/csharp/DtProfilingDemo/ examples/csharp/DtProfilingDemo/

WORKDIR /src/examples/csharp/DtProfilingDemo
RUN dotnet restore
RUN dotnet publish -c Release -o /app --no-restore

# ── Runtime image ─────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_URLS=http://+:8081
EXPOSE 8081
ENTRYPOINT ["dotnet", "DtProfilingDemo.dll"]
