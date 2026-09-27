"""
ollama chat wrapper.

one in-flight call (shared limiter) and a 30s http timeout.
think steps stay sequential — don't stack prompts.
"""

from __future__ import annotations

from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_ollama import ChatOllama

from archetype.config import Settings

# shared bucket so two think steps can't hit ollama together
_limiter = InMemoryRateLimiter(
    requests_per_second=1.0,
    check_every_n_seconds=0.1,
    max_bucket_size=1,
)


def make_chat(settings: Settings) -> ChatOllama:
    """chat model pointed at the local ollama daemon."""
    timeout = settings.ollama_timeout_sec
    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        temperature=0.2,
        num_ctx=2048,
        num_predict=180,
        format="json",
        rate_limiter=_limiter,
        client_kwargs={"timeout": timeout},
        sync_client_kwargs={"timeout": timeout},
    )
