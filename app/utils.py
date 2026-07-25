"""Shared helpers for converting DB rows to API-friendly dicts."""

import json


def row_to_note(row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["source_metadata"] = json.loads(d.get("source_metadata") or "{}")
    return d
