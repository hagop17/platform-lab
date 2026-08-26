import os

from opentelemetry import trace

# Resolved lazily against whatever provider main.py installs, so importing this
# module before main.py's TracerProvider setup is fine.
tracer = trace.get_tracer(__name__)


def _import_provider_sdk(module_name: str, extra_name: str):
    try:
        return __import__(module_name)
    except ImportError:
        raise ImportError(
            f"The {module_name!r} SDK isn't installed. Run `uv sync --extra "
            f"{extra_name}` to install it."
        ) from None


def _complete_with_groq(prompt: str) -> str:
    Groq = _import_provider_sdk("groq", "groq").Groq

    # Env-overridable because Groq decommissions models on a few weeks' notice
    # (llama-3.3-70b-versatile went away 2026-08-16), and a redeploy shouldn't
    # need a code change. The span attribute below records what actually ran.
    model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    span = trace.get_current_span()
    span.set_attribute("gen_ai.request.model", model)

    client = Groq()
    msg = client.chat.completions.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    if msg.usage:
        span.set_attribute("gen_ai.usage.input_tokens", msg.usage.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", msg.usage.completion_tokens)

    return msg.choices[0].message.content


def _complete_with_anthropic(prompt: str) -> str:
    Anthropic = _import_provider_sdk("anthropic", "anthropic").Anthropic

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    span = trace.get_current_span()
    span.set_attribute("gen_ai.request.model", model)

    client = Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )

    span.set_attribute("gen_ai.usage.input_tokens", msg.usage.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", msg.usage.output_tokens)

    return next(block.text for block in msg.content if block.type == "text")


_PROVIDERS = {
    "groq": _complete_with_groq,
    "anthropic": _complete_with_anthropic,
}


def complete(prompt: str) -> str:
    """Send a single prompt to whichever LLM provider LLM_PROVIDER selects (default: groq)."""
    provider = os.environ.get("LLM_PROVIDER", "groq")
    try:
        adapter = _PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"Unknown LLM_PROVIDER {provider!r}; supported: {sorted(_PROVIDERS)}"
        ) from None

    # The httpx spans below this show the raw POST; this one adds the part they
    # can't know — which provider/model ran and what it cost in tokens (set by
    # the adapter, which is the only thing holding the response object).
    with tracer.start_as_current_span("llm.complete") as span:
        span.set_attribute("gen_ai.system", provider)
        span.set_attribute("gen_ai.prompt.chars", len(prompt))
        return adapter(prompt)
