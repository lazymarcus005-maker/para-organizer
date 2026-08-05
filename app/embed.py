"""Embedding provider abstraction for hybrid RAG — local/cloud Ollama or any
OpenAI-compatible /v1/embeddings endpoint, selected via EMBED_PROVIDER."""

from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger("para.embed")


def _is_ollama() -> bool:
    return settings.EMBED_PROVIDER.startswith("ollama")


def _is_local() -> bool:
    return settings.EMBED_PROVIDER == "local"


def _embed_endpoint() -> str:
    if _is_ollama():
        return f"{settings.EMBED_BASE_URL}/api/embed"
    return f"{settings.EMBED_BASE_URL}/v1/embeddings"


def _embed_headers() -> dict[str, str]:
    if settings.EMBED_API_KEY:
        return {"Authorization": f"Bearer {settings.EMBED_API_KEY}"}
    return {}


async def embed_text(text: str) -> list[float] | None:
    """Embed a single string. Never raises — returns None on any failure.

    Supports three providers:
    - ``local`` → sentence-transformers via ``app.embed_local``
    - ``ollama*`` → Ollama /api/embed
    - anything else → OpenAI-compatible /v1/embeddings
    """
    if _is_local():
        from app.embed_local import embed_text as _local_embed
        return await _local_embed(text)

    payload = {"model": settings.EMBED_MODEL, "input": text}

    try:
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            resp = await client.post(_embed_endpoint(), json=payload, headers=_embed_headers())
            resp.raise_for_status()
            data = resp.json()
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        logger.warning("Embedding request failed: %s", e)
        return None

    try:
        if _is_ollama():
            return data["embeddings"][0]
        return data["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as e:
        logger.warning("Unexpected embedding response shape: %s", e)
        return None


async def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """Embed each text separately — Ollama's native /api/embed endpoint doesn't
    batch multiple inputs well, so each call is issued independently."""
    return [await embed_text(text) for text in texts]
