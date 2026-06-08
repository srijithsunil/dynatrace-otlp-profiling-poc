# Django integration

## Install

```bash
# From this repo (not on PyPI yet)
pip install ./sdk/python
```

## Integration — choose one entry point

### Option A: `manage.py` (development)

```python
# manage.py
import os
import sys
from django.core.management import execute_from_command_line

def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

    from dt_profiler import start_profiler
    start_profiler()                   # <-- add this

    execute_from_command_line(sys.argv)
```

### Option B: `wsgi.py` (production via gunicorn/uwsgi)

```python
# myproject/wsgi.py
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

from dt_profiler import start_profiler
start_profiler()                       # <-- add this

application = get_wsgi_application()
```

### Option C: Gunicorn post-fork (recommended for multi-worker setups)

Each worker process gets its own profiler instance — profiles are
correlated by `service.name` in Dynatrace.

```python
# gunicorn.conf.py
import os

bind = "0.0.0.0:8000"
workers = 4

def post_fork(server, worker):
    from dt_profiler import start_profiler
    start_profiler(
        extra_attributes={"worker.pid": str(os.getpid())},
    )
```

```bash
gunicorn myproject.wsgi:application -c gunicorn.conf.py
```

## Environment variables

```bash
export DT_ENDPOINT=https://<env>.live.dynatrace.com
export DT_API_TOKEN=<token>
export OTEL_SERVICE_NAME=my-django-app
export DEPLOYMENT_ENV=production
```
