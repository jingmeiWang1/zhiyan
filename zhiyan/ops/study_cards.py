"""生成学习卡片候选，并校验它们真的出自用户资料。

ONE model call, then a hard server-side check:

    the model returns JSON
    every card must carry source_quote
    source_quote must occur in the unit text (exact, or after whitespace folding)

If the quote is not there, the card is rejected. That check is the whole point:
an LLM asked to make cards will happily invent a plausible sentence, and a card
with an invented source is worse than no card — it looks verifiable and isn't.

Cards are written as ``candidate``. The user confirms them on the page; only
then do they enter the review queue. Nothing here accepts cards on the user's
behalf.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from zhiyan.applications import library, study

MAX_UNIT_CHARS = 6000
MAX_CARDS = 12
MIN_UNIT_CHARS = 40

_EXAM_TYPES = ("qa", "cloze")
_LANGUAGE_TYPES = ("vocab", "cloze", "translation", "grammar")

_PROMPT = """\
You write study cards from exactly ONE unit of the user's own material.

The unit text is below, between <unit> and </unit>. Use only this text. Do not
add outside facts. If the unit does not support the requested number of cards,
return fewer.

Return ONLY a JSON array. No prose, no markdown fence.

Allowed card types: {allowed}
Requested count: {count}

For type "qa":
  {{"type":"qa","front":"<question>","back":"<answer>","source_quote":"<verbatim sentence from the unit>"}}
For type "cloze":
  {{"type":"cloze","front":"<sentence with ____>","back":"<missing answer>","source_quote":"<verbatim sentence>"}}
For type "vocab":
  {{"type":"vocab","term":"<word or phrase>","phonetic":"<optional>","pos":"<optional>",
    "meaning":"<Chinese meaning>","example":"<sentence from the unit using the term>",
    "example_translation":"<optional Chinese translation>","source_quote":"<verbatim sentence>"}}
For types "translation" or "grammar":
  {{"type":"translation","front":"<prompt>","back":"<reference answer>","source_quote":"<verbatim sentence>"}}

Hard rules:
- source_quote MUST be copied verbatim from the unit. Whitespace may differ;
  wording may not. A card without a source_quote is invalid.
- One fact per card. front/term at most 120 characters; back/meaning at most
  100 characters. Do not paste a whole paragraph into a card.
- For vocab, "example" must also come from the unit, not from memory.

<unit>
{text}
</unit>
"""


def model_for(settings, client=None, model=None):
    """The client/model pair used by card generation and writing correction.

    Passing a client is the eval seam: it keeps the gate and card generator on
    the same injected model instead of quietly reaching for the network.
    Without one, the summary role (core/models.py:roles_for) is used — card
    generation is a bounded extraction task, not a job for the expensive loop
    model.
    """
    from zhiyan.core.models import get_client, roles_for

    if client is not None and model:
        return client, model
    base = client or get_client(settings)
    roles = roles_for(settings, base)
    if client is None:
        return roles.summary_client, model or roles.summary_model
    return client, model or roles.summary_model


def provider_error(exc: BaseException) -> str:
    """Turn a provider failure into a sentence the user can act on."""
    text = str(exc)
    lowered = text.lower()
    if ("insufficient_quota" in lowered or "free quota exhausted" in lowered
            or "quota" in lowered):
        return ("模型服务商返回额度不足（403 insufficient_quota）。"
                "请到「模型」页更换服务商/模型，或关闭“仅使用免费额度”/充值后重试。")
    if "401" in lowered or "authentication" in lowered or "invalid api key" in lowered:
        return "模型服务商认证失败（401）。请到「模型」页检查 API Key。"
    if "429" in lowered or "rate limit" in lowered:
        return "模型服务商限流（429）。稍后重试，或切换到另一个模型。"
    return f"模型调用失败：{text}"


def _request_cards(client, model: str, prompt: str):
    return client.messages.create(
        model=model,
        # Reasoning models spend part of the budget before the JSON; 2048 was
        # measured returning thinking-only text for a 6-card request.
        max_tokens=4096,
        timeout=60,
        messages=[{"role": "user", "content": prompt}],
    )


def _response_text(response: Any) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


def _json_array(text: str) -> list | None:
    """Parse the array, or salvage every complete object if the output was cut.

    A reasoning model can spend its output budget before the closing ``]``.
    Throwing away a reply that contains five complete cards because the sixth
    was truncated is worse than keeping the five and saying so.
    """
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            data = data.get("cards")
        if isinstance(data, list):
            return data

    decoder = json.JSONDecoder()
    items: list = []
    index = text.find("{")
    while index >= 0:
        try:
            obj, consumed = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        if isinstance(obj, dict):
            nested = obj.get("cards")
            if isinstance(nested, list):
                items.extend(nested)
            else:
                items.append(obj)
        index = text.find("{", consumed)
    return items or None


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace, remembering where each output char came from."""
    chars: list[str] = []
    positions: list[int] = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            chars.append(" ")
            positions.append(index)
            while index < len(text) and text[index].isspace():
                index += 1
        else:
            chars.append(text[index])
            positions.append(index)
            index += 1
    return "".join(chars), positions


def _loose_with_map(text: str) -> tuple[str, list[int]]:
    """Keep only letters/digits/CJK, so quote punctuation differences don't reject.

    Wording still has to match exactly and in order; this only forgives the
    quote marks, commas and ellipses a model likes to reflow.
    """
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text):
        if char.isalnum():
            chars.append(char.lower())
            positions.append(index)
    return "".join(chars), positions


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def locate_quote(text: str, quote: str) -> int | None:
    """Where the quote starts in the unit, or None when it is not there.

    Exact first (so the offset is exact), then whitespace-folded. The folded
    path exists because a model often reflows a PDF sentence; the characters
    are still the document's characters.
    """
    quote = (quote or "").strip()
    if not quote:
        return None
    exact = text.find(quote)
    if exact >= 0:
        return exact
    folded_text, positions = _norm_with_map(text)
    folded_quote = _norm_text(quote)
    if folded_quote:
        at = folded_text.find(folded_quote)
        if at >= 0:
            return positions[at]

    loose_text, loose_positions = _loose_with_map(text)
    loose_quote, _ = _loose_with_map(quote)
    if not loose_quote:
        return None
    at = loose_text.find(loose_quote)
    return loose_positions[at] if at >= 0 else None


def _clean(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _exam_card(item: dict, allowed: tuple[str, ...]) -> tuple[dict | None, str]:
    card_type = str(item.get("type") or "qa").strip().lower()
    if card_type not in allowed:
        return None, f"类型 {card_type} 不属于这个项目"
    front = _clean(item.get("front") or item.get("question"), 500)
    back = _clean(item.get("back") or item.get("answer"), 1000)
    if not front or not back:
        return None, "正面或背面为空"
    return {"card_type": card_type, "front": front, "back": back}, ""


def _language_card(item: dict, allowed: tuple[str, ...]) -> tuple[dict | None, str]:
    card_type = str(item.get("type") or "vocab").strip().lower()
    if card_type not in allowed:
        return None, f"类型 {card_type} 不属于这个项目"
    if card_type == "vocab":
        term = _clean(item.get("term") or item.get("front"), 500)
        meaning = _clean(item.get("meaning") or item.get("back"), 1000)
        if not term or not meaning:
            return None, "词条或释义为空"
        extra = {
            "phonetic": _clean(item.get("phonetic"), 120),
            "pos": _clean(item.get("pos"), 40),
            "example": _clean(item.get("example"), 500),
            "example_translation": _clean(item.get("example_translation"), 500),
        }
        return {"card_type": "vocab", "front": term, "back": meaning,
                "extra": extra}, ""
    front = _clean(item.get("front") or item.get("question"), 500)
    back = _clean(item.get("back") or item.get("answer"), 1000)
    if not front or not back:
        return None, "正面或背面为空"
    return {"card_type": card_type, "front": front, "back": back}, ""


def generate_cards(settings, conn: sqlite3.Connection, unit_id: str, *,
                   count: int = 6, card_types: tuple[str, ...] | None = None,
                   client=None, model=None) -> dict:
    """Generate candidate cards for one unit.

    Returns a dict rather than raising: the caller is a tool or an HTTP action,
    and both need a human-readable reason instead of a traceback.
    """
    unit = study.get_unit(conn, unit_id)
    if unit is None:
        return {"ok": False, "error": "这个单元已经不在了"}
    project = study.get_project(conn, unit["project_id"])
    if project is None:
        return {"ok": False, "error": "这个单元所属的项目已经不在了"}

    allowed = _EXAM_TYPES if project["kind"] == "exam" else _LANGUAGE_TYPES
    if card_types:
        allowed = tuple(name for name in card_types if name in allowed)
    if not allowed:
        return {"ok": False, "error": "这个项目不能生成这种类型的卡片"}

    document = library.get_document(conn, unit["doc_id"])
    if document is None:
        return {"ok": False, "error": "这个单元所属的资料已经不在了"}
    text = (document["content"] or "")[unit["start_offset"]:unit["end_offset"]]
    text = text.strip()
    if len(text) < MIN_UNIT_CHARS:
        return {"ok": False, "error": "这个单元的文字太少，无法出卡"}
    text = text[:MAX_UNIT_CHARS]
    count = max(1, min(MAX_CARDS, int(count or 6)))

    injected = client is not None
    prompt = _PROMPT.format(allowed=", ".join(allowed), count=count, text=text)
    try:
        client, model = model_for(settings, client, model)
        response = _request_cards(client, model, prompt)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - a tool must answer, not crash
        return {"ok": False, "error": provider_error(exc)}

    raw = _response_text(response)
    fallback = False
    fallback_tried = False
    if not raw.strip() and not injected:
        # The summary role is often a cheaper/reasoning model. If it returns no
        # text at all, try the main model once; the user can still fix the role
        # later, but a silent empty reply should not block card generation.
        fallback_tried = True
        try:
            from zhiyan.core.models import get_client, resolve_models

            main_client = get_client(settings)
            main_model = settings.model or resolve_models(settings)[0]
            if main_client is not client or main_model != model:
                response = _request_cards(main_client, main_model, prompt)
                raw = _response_text(response)
                client, model, fallback = main_client, main_model, True
        except (Exception, SystemExit):  # noqa: BLE001 - fallback is best-effort
            pass

    items = _json_array(raw)
    if items is None:
        excerpt = " ".join(raw.split())[:240]
        if "quota" in raw.lower() or "insufficient" in raw.lower():
            return {"ok": False, "error": provider_error(RuntimeError(raw))}
        if not excerpt:
            if fallback_tried:
                return {"ok": False, "error": (
                    "摘要模型和主模型都没有返回文本。请到「模型」页检查摘要模型，"
                    "或换一个支持 JSON 输出的模型后重试。")}
            return {"ok": False, "error": (
                "模型返回了空回复（推理模型可能只输出了思考内容，"
                "或服务商额度不足）。请换一个摘要模型后重试。")}
        return {"ok": False, "error": (
            "模型没有按格式返回 JSON 数组。原始回复开头："
            f"{excerpt}")}

    created: list[dict] = []
    rejected: list[dict] = []
    duplicates = 0
    for item in items:
        if not isinstance(item, dict):
            rejected.append({"reason": "不是对象", "item": item})
            continue
        quote = str(item.get("source_quote") or "").strip()
        offset = locate_quote(text, quote)
        if offset is None:
            rejected.append({"reason": "原文引文不在单元里", "item": item})
            continue
        if project["kind"] == "exam":
            card, reason = _exam_card(item, allowed)
        else:
            card, reason = _language_card(item, allowed)
        if card is None:
            rejected.append({"reason": reason, "item": item})
            continue
        card_row = study.add_card(
            conn, project["id"], card_type=card["card_type"], front=card["front"],
            back=card["back"], unit_id=unit["id"], source_doc_id=unit["doc_id"],
            source_offset=unit["start_offset"] + offset,
            source_quote=_clean(quote, 1000), extra=card.get("extra"),
            tags="ai", status="candidate",
        )
        if card_row is None:
            duplicates += 1
        else:
            created.append(card_row)

    return {"ok": True, "created": created, "rejected": rejected,
            "duplicates": duplicates, "unit": unit, "model": model,
            "truncated": getattr(response, "stop_reason", "") == "max_tokens",
            "fallback": fallback}
