"""LLM-based PARA classification via Ollama Cloud."""

import json
import logging
import re
from datetime import date, datetime

import httpx

from app.config import settings

logger = logging.getLogger("para.classifier")

PARA_CATEGORIES = {"projects", "areas", "resources", "archives"}

CLASSIFY_PROMPT = """You are a PARA note classifier. Respond with a JSON object with exactly these keys:
- para_category: exactly one of "projects", "areas", "resources", "archives"
  - "projects": Active work with a deadline or specific goal
  - "areas": Ongoing responsibility, no end date
  - "resources": Reference material, useful info, no action needed
  - "archives": Completed or no longer relevant
- sub_category: short label (1-3 words)
- priority: low | medium | high
- deadline: ISO date (YYYY-MM-DD) if found in text, else null
- tags: array of 3-7 relevant tags (mix Thai and English as appropriate)
- confidence: 0.0 to 1.0
- reasoning: short explanation in Thai (1-2 sentences)

Respond as JSON ONLY, using the exact key name "para_category" for the classification. No markdown, no explanation outside JSON.

Note title: {title}
Note content: {content}
"""

DEFAULT_RESULT = {
    "para_category": "inbox",
    "sub_category": None,
    "priority": "medium",
    "deadline": None,
    "tags": [],
    "confidence": 0.0,
    "llm_model": None,
    "reasoning": "LLM classification failed, placed in inbox",
}


async def call_ollama(model: str, prompt: str, format: str | None = "json") -> str:
    """Call the Ollama Cloud OpenAI-compatible chat completions endpoint."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if format:
        payload["format"] = format

    async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
        resp = await client.post(
            f"{settings.OLLAMA_BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {settings.OLLAMA_API_KEY}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def _extract_json(text: str) -> dict:
    """Best-effort extraction of a JSON object from a model response."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise json.JSONDecodeError("No JSON object found", text, 0)


async def classify_note(title: str, content: str) -> dict:
    """Call the LLM (primary, then fallback) to classify a note. Never raises."""
    prompt = CLASSIFY_PROMPT.format(title=title, content=content)

    for model in [settings.LLM_PRIMARY, settings.LLM_FALLBACK]:
        for attempt in range(settings.LLM_MAX_RETRIES):
            try:
                raw = await call_ollama(model, prompt, format="json")
                result = _extract_json(raw)

                if result["para_category"] not in PARA_CATEGORIES:
                    raise ValueError(f"Invalid para_category: {result.get('para_category')!r}")
                if not 0.0 <= float(result["confidence"]) <= 1.0:
                    raise ValueError(f"Invalid confidence: {result.get('confidence')!r}")

                result["llm_model"] = model
                result.setdefault("sub_category", None)
                result.setdefault("priority", "medium")
                result.setdefault("deadline", None)
                result.setdefault("tags", [])
                result.setdefault("reasoning", "")
                return result
            except (json.JSONDecodeError, KeyError, TypeError, ValueError,
                     httpx.TimeoutException, httpx.HTTPError) as e:
                logger.warning("LLM %s attempt %d failed: %s", model, attempt + 1, e)
                continue

    logger.warning("All LLM models failed, defaulting to inbox")
    return dict(DEFAULT_RESULT)


THAI_MONTHS = {
    "ม.ค.": 1, "มกราคม": 1,
    "ก.พ.": 2, "กุมภาพันธ์": 2,
    "มี.ค.": 3, "มีนาคม": 3,
    "เม.ย.": 4, "เมษายน": 4,
    "พ.ค.": 5, "พฤษภาคม": 5,
    "มิ.ย.": 6, "มิถุนายน": 6,
    "ก.ค.": 7, "กรกฎาคม": 7,
    "ส.ค.": 8, "สิงหาคม": 8,
    "ก.ย.": 9, "กันยายน": 9,
    "ต.ค.": 10, "ตุลาคม": 10,
    "พ.ย.": 11, "พฤศจิกายน": 11,
    "ธ.ค.": 12, "ธันวาคม": 12,
}


def extract_deadline_from_text(text: str) -> date | None:
    """Best-effort extraction of a Thai/ISO date from free text."""
    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso_match:
        try:
            return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        except ValueError:
            pass

    for month_name, month_num in THAI_MONTHS.items():
        pattern = rf"(\d{{1,2}})\s*{re.escape(month_name)}\s*(\d{{4}})?"
        match = re.search(pattern, text)
        if match:
            day = int(match.group(1))
            year_raw = match.group(2)
            if year_raw:
                year = int(year_raw)
                if year > 2400:  # Buddhist Era -> Gregorian
                    year -= 543
            else:
                year = datetime.now().year
            try:
                return date(year, month_num, day)
            except ValueError:
                continue

    return None
