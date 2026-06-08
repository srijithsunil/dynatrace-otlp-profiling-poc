# Build from the repo root, not from this directory:
#   docker build -f examples/python-flask/Dockerfile .
#
# Or use the docker-compose.yml in this directory:
#   docker compose up --build

FROM python:3.12-slim
WORKDIR /app

# Install the SDK from the repo (swap for: pip install dt-otlp-profiler once on PyPI)
COPY sdk/python /sdk/python
RUN pip install --no-cache-dir /sdk/python

COPY examples/python-flask/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY examples/python-flask/app.py .
CMD ["python", "app.py"]
