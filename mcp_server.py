"""
MCP server exposing this service's own Prometheus metrics as tools.

Mounted into the FastAPI app in `main.py` rather than run as a separate
process — MCP's streamable-HTTP transport is just an ASGI app, so it can
live inside the app whose metrics it reads. That means the tools call
`metrics_analysis` directly instead of going back out over HTTP.

Deliberately *no* LLM call anywhere in this module. `/api/v1/analyze`
asks an LLM one fixed question about one fixed window; here the model is
the client, so the tools only fetch and format, and Claude decides what
to query, when to query again, and what it means.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import FastAPI
from mcp_use import MCPServer

from metrics_analysis import format_metrics_for_llm, list_metric_names, query_prometheus

mcp = MCPServer(
    name="platform-lab-metrics",
    version="0.1.0",
    instructions=(
        "Tools for investigating the live health of the platform-lab service "
        "by querying its Prometheus metrics. Use these to diagnose latency, "
        "error rates, and traffic anomalies. Start with list_metrics to learn "
        "what this service actually exports before writing any PromQL."
    ),
)

# A range query returns one sample per step, and every sample becomes a line
# of text for the model to read. 15s over 60 minutes is 240 lines per series,
# which crowds out the model's own reasoning. Cap the resolution instead so a
# wide window stays readable.
_MAX_POINTS = 60


def _step_for(minutes: int) -> str:
    return f"{max(15, (minutes * 60) // _MAX_POINTS)}s"


@mcp.tool()
def list_metrics() -> str:
    """List every metric name currently available in Prometheus.

    ALWAYS call this first, before query_metrics. This service exports
    OpenTelemetry metrics whose exact names differ from the usual Prometheus
    conventions, so a guessed metric name will silently return no data.
    """
    try:
        names = list_metric_names()
    except httpx.HTTPError as exc:
        return f"Could not reach Prometheus: {exc}"

    if not names:
        return "Prometheus is reachable but holds no metrics yet. Generate traffic first."

    return "\n".join(names)


@mcp.tool()
def query_metrics(promql: str, minutes: int = 30) -> str:
    """Run a PromQL range query and return the resulting time series as text.

    Call list_metrics first so the metric names are real rather than guessed.

    Args:
        promql: A PromQL expression. Wrap counters in rate() — a raw counter
            only ever climbs, so it shows nothing about current behaviour.
            Example: rate(app_requests_total[5m])
        minutes: How far back to look. Default 30. Use at least 15, since a
            shorter window rarely contains enough points to show a trend.

    Diagnosing a slowdown takes more than one call: compare request rate
    against latency over the same window before concluding anything. Flat
    traffic with rising latency points at the service itself; both rising
    together points at load.
    """
    try:
        raw = query_prometheus(promql, minutes, step=_step_for(minutes))
    except httpx.HTTPStatusError as exc:
        # Prometheus puts the parse error in the body; passing it back lets the
        # model correct its own PromQL instead of guessing at a bare 400.
        return f"Query rejected by Prometheus: {exc.response.text}"
    except httpx.HTTPError as exc:
        return f"Could not reach Prometheus: {exc}"

    return format_metrics_for_llm(raw)


@asynccontextmanager
async def mcp_lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Start the MCP session manager alongside the host application.

    Starlette runs the lifespan of the top-level app only — a mounted sub-app
    never receives startup or shutdown. `mcp.app` would normally create this
    task group in its own lifespan, so mounting it silently skips that step:
    tools/list still answers, but anything that streams has no task group to
    run in. Creating it here and handing it over is mcp-use's documented
    workaround for the mount case.

    `_task_group` is private, so pin the mcp-use version — a rename would
    surface as streaming failures rather than an ImportError.
    """
    async with anyio.create_task_group() as tg:
        mcp.session_manager._task_group = tg  # noqa: SLF001 — see docstring
        yield
