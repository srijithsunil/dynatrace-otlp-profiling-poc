FROM python:3.12-slim
WORKDIR /app

# Install the dt-otlp-profiler SDK first (built from repo root context)
COPY sdk/python /sdk/python
RUN pip install --no-cache-dir /sdk/python

# Install app dependencies
COPY sample-app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY sample-app/ .

EXPOSE 8080
CMD ["python", "app.py"]
