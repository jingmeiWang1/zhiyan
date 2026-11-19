"""HERO MOMENT #1 — the gate that decides WHETHER to retrieve memory at all.

The top audience question across platforms: "why hit the memory store every
turn?" Default-on retrieval is (a) slow — an extra search before every reply —
and (b) worse: irrelevant memories bias the answer ("over-interpretation").

So before touching any store, a cheap fast model answers one question:
    does THIS message need the user's memory?
"what's 2+2" → no. "when am I meeting Alex?" → yes, and here's the search query.

Cost: one small-model call (~a few hundred tokens). Payoff: retrieval only
when it helps. This is the same judge pattern as LLM-as-judge in evals —
a small model making one narrow decision.
"""

from __future__ import annotations

import json

import anthropic

GATE_PROMPT = """\
You are a retrieval gate for a personal assistant's long-term memory.
Given the user's current message, decide whether answering well requires the user's stored long-term memory.
Long-term memory may contain facts, preferences, past events, prior conversations, decisions, plans, ongoing work, or personal constraints.

Reply with ONLY this JSON, nothing else:
{{"retrieve": true/false, "query": "<2-6 high-signal search keywords if true, else empty>", "reason": "<一句中文，给用户看，不超过15字>"}}

Rules:
- General knowledge, math, coding, small talk, translation, rewriting, and other self-contained requests usually do not need memory.
- Retrieve when important information needed to answer is missing from the current message but may exist in long-term memory.
- Retrieve when relevant stored preferences, constraints, previous decisions, ongoing projects, or personal history would materially improve the answer or prevent a conflicting answer.
- Do NOT retrieve merely because the message mentions the user, their life, another person, a project, or the past.
- If the current message already provides all personal information needed to answer well, do NOT retrieve.
- When retrieve=true, query must contain 2-6 concise, high-signal keywords derived ONLY from the current message. Do not invent hidden facts or answers.
- For Chinese queries, separate important search terms with ASCII spaces.
- When retrieve=false, query must be an empty string.
- Never return an array.
User message: {message}"""


def should_retrieve(
    client: anthropic.Anthropic, small_model: str, message: str
) -> tuple[bool, str, str]:
    """Returns (retrieve?, search_query, reason). Fails open: if the gate
    itself errors, we retrieve — a stale memory beats a lost one.

    The reason is shown to the user on the turn's timeline, so it is written in
    Chinese (the prompt asks the model for 中文) and the fail-open lines say what
    happened in 中文 too — keeping the exception name, which is the one part a
    log reader actually needs."""
    try:
        response = client.messages.create(
            model=small_model,
            # generous budget: reasoning models (Kimi K3, ...) spend a thinking
            # block BEFORE the JSON — 100 tokens was truncating the answer away
            max_tokens=600,
            messages=[{"role": "user", "content": GATE_PROMPT.format(message=message)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        if "{" not in text:   # a reasoning-only / truncated reply, not an error
            return True, message, "门控没有回 JSON，按「检索」处理"
        decision = json.loads(text[text.index("{") : text.rindex("}") + 1])
        return bool(decision.get("retrieve")), decision.get("query", message), decision.get("reason", "")
    except Exception as exc:
        return True, message, f"门控出错，按「检索」处理（{type(exc).__name__}）"
