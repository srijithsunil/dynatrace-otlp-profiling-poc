FROM python:3.12-slim

# Run as non-root
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --shell /bin/sh --create-home appuser

WORKDIR /app

# Install SDK then app deps (as root, before switching user)
COPY sdk/python /sdk/python
RUN pip install --no-cache-dir /sdk/python

COPY sample-app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sample-app/ .
RUN chown -R appuser:appgroup /app

USER appuser
EXPOSE 8080

# gunicorn: 2 workers × 4 threads each; tune via GUNICORN_WORKERS / GUNICORN_THREADS
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
