"""
Minimal Flask app with Dynatrace OTLP profiling.

Install:
    pip install flask dt-otlp-profiler

Run:
    DT_ENDPOINT=https://<env>.live.dynatrace.com \
    DT_API_TOKEN=<token> \
    OTEL_SERVICE_NAME=my-flask-app \
    python app.py
"""
from dt_profiler import start_profiler
from flask import Flask, jsonify

# Start before the app initialises — profiles every thread from this point on.
start_profiler()

app = Flask(__name__)


@app.route("/")
def index():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
