import anyio
import httpx
import pytest
from mcp.server.transport_security import TransportSecurityMiddleware
from mcp.types import CallToolResult, TextContent
from starlette.requests import Request

import mcp_server

_REQUEST = httpx.Request("GET", "http://prometheus:9090/api/v1/query_range")


def _raise(exc: Exception):
    def stub(*_args, **_kwargs):
        raise exc

    return stub


def test_tools_are_registered_with_the_server():
    tools = anyio.run(mcp_server.mcp.list_tools)
    assert {tool.name for tool in tools} == {"list_metrics", "query_metrics"}


def test_call_tool_returns_tool_output_as_text(monkeypatch):
    monkeypatch.setattr(mcp_server, "list_metric_names", lambda: ["up", "app_requests_total"])

    result = anyio.run(mcp_server.mcp.call_tool, "list_metrics", {})

    assert isinstance(result, CallToolResult)
    assert [block.text for block in result.content if isinstance(block, TextContent)] == [
        "up\napp_requests_total"
    ]
    assert len(result.content) == 1


@pytest.mark.parametrize(
    "stub,expected",
    [
        pytest.param(lambda: ["up", "app_requests_total"], "up\napp_requests_total", id="names"),
        pytest.param(
            lambda: [],
            "Prometheus is reachable but holds no metrics yet. Generate traffic first.",
            id="empty",
        ),
        pytest.param(
            _raise(httpx.ConnectError("connection refused", request=_REQUEST)),
            "Could not reach Prometheus: connection refused",
            id="unreachable",
        ),
    ],
)
def test_list_metrics(monkeypatch, stub, expected):
    monkeypatch.setattr(mcp_server, "list_metric_names", stub)
    assert mcp_server.list_metrics() == expected


@pytest.mark.parametrize(
    "minutes,expected_step",
    [
        pytest.param(5, "15s", id="short_window_floors_at_15s"),
        pytest.param(15, "15s", id="exactly_max_points"),
        pytest.param(60, "60s", id="wide_window_capped_to_60_points"),
    ],
)
def test_query_metrics_caps_resolution(monkeypatch, minutes, expected_step):
    calls = []

    def fake_query(promql, mins, step):
        calls.append((promql, mins, step))
        return {"data": {"result": []}}

    monkeypatch.setattr(mcp_server, "query_prometheus", fake_query)
    monkeypatch.setattr(mcp_server, "format_metrics_for_llm", lambda raw: "formatted")

    assert mcp_server.query_metrics("up", minutes) == "formatted"
    assert calls == [("up", minutes, expected_step)]


@pytest.mark.parametrize(
    "exc,expected",
    [
        pytest.param(
            httpx.HTTPStatusError(
                "400 Bad Request",
                request=_REQUEST,
                response=httpx.Response(400, text='{"error":"parse error"}', request=_REQUEST),
            ),
            'Query rejected by Prometheus: {"error":"parse error"}',
            id="bad_promql_returns_prometheus_body",
        ),
        pytest.param(
            httpx.ConnectError("connection refused", request=_REQUEST),
            "Could not reach Prometheus: connection refused",
            id="unreachable",
        ),
    ],
)
def test_query_metrics_errors(monkeypatch, exc, expected):
    monkeypatch.setattr(mcp_server, "query_prometheus", _raise(exc))
    assert mcp_server.query_metrics("rate(up[", 30) == expected


def _status_for(headers: dict[str, str]) -> int:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )
    middleware = TransportSecurityMiddleware(mcp_server._LOCAL_ONLY)
    response = anyio.run(middleware.validate_request, request, True)
    return 200 if response is None else response.status_code


_JSON = {"Content-Type": "application/json"}


@pytest.mark.parametrize(
    "headers,expected",
    [
        pytest.param({"Host": "localhost:8001"}, 200, id="localhost"),
        pytest.param({"Host": "127.0.0.1:8001"}, 200, id="ipv4_loopback"),
        pytest.param({"Host": "[::1]:8001"}, 200, id="ipv6_loopback"),
        pytest.param(
            {"Host": "localhost:8001", "Origin": "http://localhost:3000"}, 200, id="local_page"
        ),
        pytest.param({"Host": "evil.example:8001"}, 421, id="rebound_host"),
        pytest.param({"Host": "localhost.evil.example:8001"}, 421, id="lookalike_host"),
        pytest.param(
            {"Host": "localhost:8001", "Origin": "http://evil.example"}, 403, id="foreign_origin"
        ),
    ],
)
def test_transport_security_accepts_only_local_requests(headers, expected):
    assert _status_for({**_JSON, **headers}) == expected
