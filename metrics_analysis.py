import os
import time
from datetime import datetime, timezone

import httpx

PROMETHEUS_URL = "http://prometheus:9090"  # service name inside the compose network


def query_prometheus(query: str, minutes: int = 15) -> dict:
    end_ts = time.time()
    start_ts = end_ts - (minutes * 60)

    resp = httpx.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={
            "query": query,
            "start": start_ts,
            "end": end_ts,
            "step": "15s",
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def format_metrics_for_llm(prom_response: dict) -> str:
    """Turn Prometheus's nested JSON into compact, readable lines instead of dumping raw JSON."""
    results = prom_response.get("data", {}).get("result", [])
    if not results:
        return "No data returned for this query."

    lines = []
    for series in results:
        metric_labels = series.get("metric", {})
        label_str = ", ".join(f"{k}={v}" for k, v in metric_labels.items()) or "no labels"
        lines.append(f"Series [{label_str}]:")

        for ts, value in series.get("values", []):
            time_str = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%S")
            lines.append(f"  {time_str} -> {value}")

    return "\n".join(lines)


def _import_provider_sdk(module_name: str, extra_name: str):
    try:
        return __import__(module_name)
    except ImportError:
        raise ImportError(
            f"The {module_name!r} SDK isn't installed. Run `uv sync --extra "
            f"{extra_name}` to install it."
        ) from None


def _analyze_with_groq(prompt: str) -> str:
    Groq = _import_provider_sdk("groq", "groq").Groq

    client = Groq()
    msg = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.choices[0].message.content


def _analyze_with_anthropic(prompt: str) -> str:
    Anthropic = _import_provider_sdk("anthropic", "anthropic").Anthropic

    client = Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next(block.text for block in msg.content if block.type == "text")


_PROVIDERS = {
    "groq": _analyze_with_groq,
    "anthropic": _analyze_with_anthropic,
}


def analyze_with_llm(metrics_text: str) -> str:
    provider = os.environ.get("LLM_PROVIDER", "groq")
    try:
        adapter = _PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"Unknown LLM_PROVIDER {provider!r}; supported: {sorted(_PROVIDERS)}"
        ) from None

    prompt = (
        "Here is recent Prometheus metrics data:\n\n"
        f"{metrics_text}\n\n"
        "Identify any anomalies, trend changes, or concerning patterns. "
        "Be specific and concise."
    )
    return adapter(prompt)


def analyze_metrics(query: str, minutes: int = 15) -> str:
    """End-to-end: fetch -> format -> analyze."""
    raw = query_prometheus(query, minutes)
    formatted = format_metrics_for_llm(raw)
    return analyze_with_llm(formatted)
