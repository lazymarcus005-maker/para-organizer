"""Shared helpers for converting DB rows to API-friendly dicts."""

import json

from app.config import settings


def row_to_note(row) -> dict:
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    try:
        d["source_metadata"] = json.loads(d.get("source_metadata") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["source_metadata"] = {}
    # A note is flagged for review when it was classified by the LLM but the
    # model wasn't confident enough (see classifier._apply_confidence_routing,
    # which also routes such notes to the inbox). Manually created notes
    # (llm_model is None) are never flagged.
    try:
        confidence = float(d.get("llm_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    d["review_needed"] = (
        d.get("llm_model") is not None
        and confidence < settings.RECLASSIFY_CONFIDENCE_THRESHOLD
    )
    return d
