"""
ollama chat wrapper.

one in-flight call (shared limiter). timeout comes from settings
(cold load + a long reply blew past 30s on this machine).
think steps stay sequential — don't stack prompts.
"""

from __future__ import annotations

import os

from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_ollama import ChatOllama

from archetype.config import Settings

# same window for warmup and a real think. a smaller warmup made ollama reload.
OLLAMA_NUM_CTX = 2048

# shared bucket so two think steps can't hit ollama together
_limiter = InMemoryRateLimiter(
    requests_per_second=1.0,
    check_every_n_seconds=0.1,
    max_bucket_size=1,
)


def exc_text(exc: BaseException) -> str:
    """str() is blank on some http timeouts, so fall back to the type name."""
    text = str(exc).strip()
    line = text.splitlines()[0] if text else type(exc).__name__
    return line[:240]


def ollama_num_thread(settings: Settings) -> int:
    """threads for ollama. 0 in settings means leave one cpu free and use the rest."""
    asked = int(getattr(settings, "ollama_num_thread", 0) or 0)
    if asked > 0:
        return asked
    logical = os.cpu_count() or 4
    return max(2, logical - 1)


def make_chat(settings: Settings) -> ChatOllama:
    """chat model pointed at the local ollama daemon."""
    timeout = settings.ollama_timeout_sec
    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        temperature=0.2,
        num_ctx=OLLAMA_NUM_CTX,
        # llama.cpp was only using the 2 performance cores unless we say otherwise
        num_thread=ollama_num_thread(settings),
        # -1 = no cap. a fixed cap was cutting the json mid-sentence.
        num_predict=-1,
        # keep the weights loaded between coins. a cold 800-token prompt missed 90s.
        keep_alive="30m",
        rate_limiter=_limiter,
        client_kwargs={"timeout": timeout},
        sync_client_kwargs={"timeout": timeout},
    )
