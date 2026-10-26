"""OTLP/HTTP JSON builders (spec section 5). Only what Argus reads.

JSON rather than protobuf so the package needs no protobuf dependency. 64-bit
integers are strings, as the OTLP JSON mapping requires.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence

AttrValue = Any
Attrs = Mapping[str, Any]

SEVERITY = {"trace": 1, "debug": 5, "info": 9, "warn": 13, "error": 17, "fatal": 21}

#: Histogram bucket bounds, the same as argus-js so dashboards line up.
HIST_BOUNDS = (5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0)


def attrs(a: Optional[Attrs]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not a:
        return out
    for key, v in a.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out.append({"key": str(key), "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": str(key), "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")):
                out.append({"key": str(key), "value": {"stringValue": str(v)}})
            else:
                out.append({"key": str(key), "value": {"doubleValue": v}})
        else:
            out.append({"key": str(key), "value": {"stringValue": str(v)}})
    return out


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class LogRecord(NamedTuple):
    time_ns: int
    severity: str
    body: str
    attributes: Optional[Dict[str, Any]]
    trace_id: Optional[str]
    span_id: Optional[str]


def _resource(resource: Attrs) -> Dict[str, Any]:
    return {"attributes": attrs(resource)}


def logs_request(resource: Attrs, records: Sequence[LogRecord]) -> str:
    out = []
    for r in records:
        rec: Dict[str, Any] = {
            "timeUnixNano": str(r.time_ns),
            "observedTimeUnixNano": str(r.time_ns),
            "severityNumber": SEVERITY.get(r.severity, 9),
            "severityText": r.severity.upper(),
            "body": {"stringValue": r.body},
            "attributes": attrs(r.attributes),
        }
        if r.trace_id:
            rec["traceId"] = r.trace_id
        if r.span_id:
            rec["spanId"] = r.span_id
        out.append(rec)
    return _dumps(
        {
            "resourceLogs": [
                {
                    "resource": _resource(resource),
                    "scopeLogs": [{"scope": {"name": "argus"}, "logRecords": out}],
                }
            ]
        }
    )


class SpanData(NamedTuple):
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    #: 1 internal, 2 server, 3 client.
    kind: int
    start_ns: int
    end_ns: int
    error: Optional[str]
    attributes: Optional[Dict[str, Any]]


def traces_request(resource: Attrs, spans: Sequence[SpanData]) -> str:
    out = []
    for s in spans:
        span: Dict[str, Any] = {"traceId": s.trace_id, "spanId": s.span_id}
        if s.parent_span_id:
            span["parentSpanId"] = s.parent_span_id
        span.update(
            {
                "name": s.name,
                "kind": s.kind,
                "startTimeUnixNano": str(s.start_ns),
                "endTimeUnixNano": str(s.end_ns),
                "attributes": attrs(s.attributes),
                "status": {"code": 2, "message": s.error} if s.error else {"code": 1},
            }
        )
        out.append(span)
    return _dumps(
        {
            "resourceSpans": [
                {
                    "resource": _resource(resource),
                    "scopeSpans": [{"scope": {"name": "argus"}, "spans": out}],
                }
            ]
        }
    )


class MetricPoint(NamedTuple):
    kind: str  # "sum" | "gauge" | "histogram"
    name: str
    unit: Optional[str]
    attributes: Optional[Dict[str, Any]]
    value: float
    start_ns: int
    end_ns: int
    samples: int = 0
    total: float = 0.0
    minimum: float = 0.0
    maximum: float = 0.0
    buckets: Sequence[int] = ()


def metrics_request(resource: Attrs, points: Sequence[MetricPoint]) -> str:
    metrics = []
    for p in points:
        m: Dict[str, Any] = {"name": p.name}
        if p.unit:
            m["unit"] = p.unit
        a = attrs(p.attributes)
        if p.kind == "sum":
            m["sum"] = {
                "aggregationTemporality": 1,  # delta
                "isMonotonic": True,
                "dataPoints": [
                    {
                        "attributes": a,
                        "startTimeUnixNano": str(p.start_ns),
                        "timeUnixNano": str(p.end_ns),
                        "asDouble": float(p.value),
                    }
                ],
            }
        elif p.kind == "gauge":
            m["gauge"] = {
                "dataPoints": [
                    {"attributes": a, "timeUnixNano": str(p.end_ns), "asDouble": float(p.value)}
                ]
            }
        else:
            m["histogram"] = {
                "aggregationTemporality": 1,
                "dataPoints": [
                    {
                        "attributes": a,
                        "startTimeUnixNano": str(p.start_ns),
                        "timeUnixNano": str(p.end_ns),
                        "count": str(p.samples),
                        "sum": p.total,
                        "min": p.minimum,
                        "max": p.maximum,
                        "explicitBounds": list(HIST_BOUNDS),
                        "bucketCounts": [str(b) for b in p.buckets],
                    }
                ],
            }
        metrics.append(m)
    return _dumps(
        {
            "resourceMetrics": [
                {
                    "resource": _resource(resource),
                    "scopeMetrics": [{"scope": {"name": "argus"}, "metrics": metrics}],
                }
            ]
        }
    )
