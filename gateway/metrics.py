"""gateway.metrics — dependency-free Prometheus metrics + structured request log.

No prometheus_client in this env, so we keep a tiny in-memory registry (request
counts + latency sum/count, keyed by method+route template+status) and render it
in Prometheus text format at /metrics. The same middleware stamps each request
with an id and logs a structured one-liner (method, route, status, dur, id).
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict

from starlette.requests import Request

log = logging.getLogger("gateway.access")

_count: dict[tuple[str, str, int], int] = defaultdict(int)
_dur_sum: dict[tuple[str, str, int], float] = defaultdict(float)


def _route_template(request: Request) -> str:
    # Prefer the matched route's path template (e.g. /strategies/{strategy_id}/stats)
    # to keep metric cardinality bounded; fall back to the raw path.
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def record(method: str, route: str, status: int, dur: float) -> None:
    key = (method, route, status)
    _count[key] += 1
    _dur_sum[key] += dur


async def metrics_middleware(request: Request, call_next):
    rid = uuid.uuid4().hex[:12]
    request.state.request_id = rid
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        dur = time.perf_counter() - start
        route = _route_template(request)
        record(request.method, route, status, dur)
        log.info(
            "rid=%s method=%s route=%s status=%s dur_ms=%.1f",
            rid, request.method, route, status, dur * 1000,
        )


def render() -> str:
    lines = [
        "# HELP helivex_requests_total Total HTTP requests by method/route/status",
        "# TYPE helivex_requests_total counter",
    ]
    for (method, route, status), n in sorted(_count.items()):
        lines.append(
            f'helivex_requests_total{{method="{method}",route="{route}",status="{status}"}} {n}'
        )
    lines += [
        "# HELP helivex_request_duration_seconds_sum Cumulative request duration",
        "# TYPE helivex_request_duration_seconds_sum counter",
    ]
    for (method, route, status), s in sorted(_dur_sum.items()):
        lines.append(
            f'helivex_request_duration_seconds_sum{{method="{method}",route="{route}",status="{status}"}} {s:.6f}'
        )
    return "\n".join(lines) + "\n"
