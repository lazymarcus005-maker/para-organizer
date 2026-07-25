"""Shared helpers for converting DB rows to API-friendly dicts."""

import json


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
    return d
