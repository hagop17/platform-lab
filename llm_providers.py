import os


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

    client = Groq()
    msg = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.choices[0].message.content


def _complete_with_anthropic(prompt: str) -> str:
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

    return adapter(prompt)
