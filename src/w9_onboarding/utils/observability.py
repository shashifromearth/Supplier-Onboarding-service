"""Structured logging, lightweight tracing spans and metrics.

Prototype: JSON logs to stderr + in-process metric registry.
Production: the same call sites map 1:1 onto OpenTelemetry (azure-monitor-opentelemetry
-> Application Insights): `span()` -> tracer.start_as_current_span, `METRICS.*` -> OTel
counters/histograms, log records -> AppTraces with trace/span ids attached.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from w9_onboarding.utils.security import RedactingFilter, redact

_trace_ctx: ContextVar[dict[str, str]] = ContextVar("trace_ctx", default={})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_trace_ctx.get(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        return redact(json.dumps(payload, default=str))


def get_logger(name: str = "w9") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(JsonFormatter())
        h.addFilter(RedactingFilter())
        logger.addHandler(h)
        logger.propagate = False
        logger.setLevel(logging.INFO)
    return logger


def log_event(logger: logging.Logger, msg: str, **fields: Any) -> None:
    """Log with structured fields. NEVER pass raw field values (names/TINs) here - ids, codes, scores only."""
    logger.info(msg, extra={"fields": fields})


@contextmanager
def trace_context(**ids: str) -> Iterator[str]:
    trace_id = ids.pop("trace_id", None) or uuid.uuid4().hex
    token = _trace_ctx.set({"trace_id": trace_id, **ids})
    try:
        yield trace_id
    finally:
        _trace_ctx.reset(token)


class Metrics:
    def __init__(self) -> None:
        self.counters: dict[str, float] = defaultdict(float)
        self.histograms: dict[str, list[float]] = defaultdict(list)

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> str:
        return name + ("{" + ",".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}" if labels else "")

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        self.counters[self._key(name, labels)] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.histograms[self._key(name, labels)].append(value)

    def snapshot(self) -> dict[str, Any]:
        def summary(v: list[float]) -> dict[str, float]:
            s = sorted(v)
            return {"count": len(s), "p50": s[len(s) // 2], "p95": s[min(len(s) - 1, int(len(s) * 0.95))],
                    "mean": sum(s) / len(s)}
        return {"counters": dict(self.counters),
                "histograms": {k: summary(v) for k, v in self.histograms.items() if v}}


METRICS = Metrics()


@contextmanager
def span(name: str, timings: dict[str, float], logger: logging.Logger | None = None) -> Iterator[None]:
    """Time a pipeline stage; record latency histogram and a span log line."""
    t0 = time.perf_counter()
    status = "ok"
    try:
        yield
    except Exception:
        status = "error"
        METRICS.inc("w9_stage_errors_total", stage=name)
        raise
    finally:
        ms = (time.perf_counter() - t0) * 1000
        timings[name] = round(ms, 1)
        METRICS.observe("w9_stage_latency_ms", ms, stage=name)
        if logger:
            log_event(logger, "span", span=name, duration_ms=round(ms, 1), status=status)
