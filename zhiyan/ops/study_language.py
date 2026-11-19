"""Language-specific helpers: word-list import, writing correction, dictation.

These are deliberately outside ``applications/study.py``: the store and the
scheduler stay pure SQLite/pure functions, while the two things that talk to a
model (writing correction) or parse a user's pasted list (word import) live
here. Card generation is in ``ops/study_cards.py`` for the same reason.
"""

from __future__ import annotations

import json
import re
import sqlite3

from zhiyan.applications import study
from zhiyan.ops.study_cards import model_for, provider_error

MAX_IMPORT_LINES = 500
MAX_WRITING_CHARS = 4000

_WRITING_PROMPT = """\
You correct a language learner's writing. Keep the learner's meaning and
language level; fix grammar, word choice, and punctuation. Do not rewrite it
into an advanced essay.

Return ONLY this JSON object, with no prose and no markdown fence:
{{"corrected": "<the corrected text>", "notes": ["<one short note in Chinese>"]}}

At most 5 notes. Each note names one concrete change and why. Do not give a
score or claim a CEFR level.

Learner's text:
<writing>
{text}
</writing>
"""


def parse_word_list(text: str) -> tuple[list[dict], list[dict]]:
    """Parse the three line formats the page accepts.

    ``word=meaning``, ``word<TAB>meaning``, and ``word  meaning``. A single
    space is the last resort so a pasted ``word meaning`` list still works.
    Bad lines are returned with a reason instead of being guessed at.
    """
    entries: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for number, raw in enumerate((text or "").lstrip("﻿").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", "//")):
            continue
        if "=" in line:
            term, meaning = line.split("=", 1)
        elif "\t" in line:
            term, meaning = line.split("\t", 1)
        else:
            parts = re.split(r"\s{2,}", line, maxsplit=1)
            if len(parts) == 2:
                term, meaning = parts
            else:
                parts = line.split(None, 1)
                if len(parts) != 2:
                    skipped.append({"line": number, "text": line[:80],
                                    "reason": "缺少释义"})
                    continue
                term, meaning = parts
        term = " ".join(term.split())[:200]
        meaning = " ".join(meaning.split())[:500]
        if not term or not meaning:
            skipped.append({"line": number, "text": line[:80], "reason": "词条或释义为空"})
            continue
        key = study._normalize_term(term)
        if key in seen:
            skipped.append({"line": number, "text": line[:80], "reason": "词表内重复"})
            continue
        seen.add(key)
        entries.append({"term": term, "meaning": meaning})
    return entries, skipped


def import_words(conn: sqlite3.Connection, project_id: str, text: str) -> dict:
    """Add a pasted word list as candidate vocab cards."""
    project = study.get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    if project["kind"] != "language":
        raise ValueError("词表导入只适用于语言项目")
    entries, skipped = parse_word_list(text)
    if not entries:
        return {"ok": True, "added": [], "duplicates": 0,
                "skipped": skipped, "error": "没有可导入的词条"}
    if len(entries) > MAX_IMPORT_LINES:
        skipped.extend({"line": 0, "text": "", "reason": "超过单次导入上限"}
                       for _ in entries[MAX_IMPORT_LINES:])
        entries = entries[:MAX_IMPORT_LINES]

    added: list[dict] = []
    duplicates = 0
    for entry in entries:
        card = study.add_card(
            conn, project_id, card_type="vocab", front=entry["term"],
            back=entry["meaning"], extra={"source": "import"},
            tags="import", status="candidate",
        )
        if card is None:
            duplicates += 1
        else:
            added.append(card)
    return {"ok": True, "added": added, "duplicates": duplicates,
            "skipped": skipped}


def correct_writing(settings, conn: sqlite3.Connection, project_id: str, text: str, *,
                    client=None, model=None) -> dict:
    """Return a corrected version and a short list of notes. No score."""
    project = study.get_project(conn, project_id)
    if project is None:
        return {"ok": False, "error": "这个学习项目已经不在了"}
    if project["kind"] != "language":
        return {"ok": False, "error": "写作批改只适用于语言项目"}
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "先写一点内容再提交"}
    if len(text) > MAX_WRITING_CHARS:
        return {"ok": False, "error": f"一次最多批改 {MAX_WRITING_CHARS} 个字符"}

    try:
        client, model = model_for(settings, client, model)
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            timeout=60,
            messages=[{"role": "user", "content": _WRITING_PROMPT.format(text=text)}],
        )
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - the caller needs a message
        return {"ok": False, "error": provider_error(exc)}

    raw = "".join(
        getattr(block, "text", "") for block in getattr(response, "content", []) or []
        if getattr(block, "type", "") == "text"
    )
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {"ok": False, "error": "模型没有返回 JSON 对象"}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {"ok": False, "error": "模型返回的 JSON 无法解析"}
    corrected = str(data.get("corrected") or "").strip()
    notes = data.get("notes")
    if not corrected:
        return {"ok": False, "error": "模型没有给出修改稿"}
    if not isinstance(notes, list):
        notes = []
    notes = [" ".join(str(note).split())[:300] for note in notes if str(note).strip()][:5]
    return {"ok": True, "corrected": corrected, "notes": notes, "model": model}


def dictation_feedback(expected: str, actual: str) -> dict:
    """The objective half of a dictation review: score and suggested rating."""
    score = study.dictation_score(expected, actual)
    rating = study.rating_for_score(score)
    return {"score": score, "rating": rating,
            "label": study.RATINGS.get(rating, ""),
            "expected": expected}
