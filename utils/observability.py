"""
Request IDs, structured logging and lightweight metrics.

* Every request gets an ID (a sane inbound X-Request-ID is honoured so a CDN/load balancer
  trace continues; otherwise one is generated) and it is echoed back in the response.
* Jobs carry the ID of the request that created them, so API logs and worker logs for the
  same job can be joined.
* Logs never contain bodies, headers, cookies, file contents or secrets: only method,
  path, status, duration and IDs.
* Metrics are per-process counters rendered in Prometheus text format at /api/metrics.
  They are a stop-gap: in production scrape every instance (or use the platform's
  metrics) and aggregate there.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
import uuid

_request_id = contextvars.ContextVar("omixa_request_id", default="-")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")


def new_request_id(inbound: str | None = None) -> str:
    if inbound and _SAFE_ID.match(inbound):
        return inbound
    return uuid.uuid4().hex


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str:
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key in ("job_id", "worker_id", "duration_ms", "status", "attempt"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            # type only: tracebacks can embed data fragments from parsing errors
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=True)


def configure_logging(level: int, fmt: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)


# ---------------------------------------------------------------- metrics

_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
_lock = threading.Lock()
_counters: dict[tuple, float] = {}
_hist: dict[str, list[float]] = {}   # route-group -> [bucket counts..., sum, count]


def inc(name: str, value: float = 1, **labels) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        _counters[key] = _counters.get(key, 0) + value


def observe_request(group: str, status: int, seconds: float) -> None:
    inc("omixa_http_requests_total", group=group, status=f"{status // 100}xx")
    with _lock:
        h = _hist.setdefault(group, [0.0] * (len(_BUCKETS) + 2))
        for i, b in enumerate(_BUCKETS):
            if seconds <= b:
                h[i] += 1
        h[-2] += seconds
        h[-1] += 1


def route_group(path: str) -> str:
    """Low-cardinality label: never the raw path (job ids would explode the series)."""
    if path.startswith("/api/upload"):
        return "upload"
    if path.startswith("/api/process"):
        return "process"
    if path.startswith("/api/jobs"):
        return "job_status"
    if path.startswith("/api/download"):
        return "download"
    if path.startswith("/api/billing"):
        return "billing"
    if path.startswith("/api/"):
        return "api_other"
    if path.startswith("/admin"):
        return "admin"
    if path.startswith("/static"):
        return "static"
    return "page"


def render_prometheus(extra_gauges: dict[str, float] | None = None) -> str:
    lines = []
    with _lock:
        for (name, labels), val in sorted(_counters.items()):
            lab = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{lab}}} {val}" if lab else f"{name} {val}")
        for group, h in sorted(_hist.items()):
            for i, b in enumerate(_BUCKETS):
                lines.append(f'omixa_http_request_seconds_bucket{{group="{group}",le="{b}"}} {h[i]}')
            lines.append(f'omixa_http_request_seconds_bucket{{group="{group}",le="+Inf"}} {h[-1]}')
            lines.append(f'omixa_http_request_seconds_sum{{group="{group}"}} {h[-2]}')
            lines.append(f'omixa_http_request_seconds_count{{group="{group}"}} {h[-1]}')
    for name, val in sorted((extra_gauges or {}).items()):
        lines.append(f"{name} {val}")
    return "\n".join(lines) + "\n"


def reset_metrics_for_tests() -> None:
    with _lock:
        _counters.clear()
        _hist.clear()
