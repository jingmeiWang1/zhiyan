"""What the model is handed back for a tool call has to be TEXT.

Found live, one turn after the research agent answered a document question
correctly: the next question in the same conversation came back as

    422 ... messages[10]: missing field `type`

The notes tools (`list_notes` / `search_notes` / `list_folders` /
`get_linked_notes`) return Python lists, and `get_note` returns a dict — they
always have. The loop put that return value into the tool_result verbatim, so
the tool message on the OpenAI wire was `content: [{"id": 3, ...}]`. Neither
wire accepts that: DeepSeek wants a string or content blocks that each carry a
`type`, and Anthropic rejects the same shape as an invalid block. It only shows
up on the turn AFTER the tool ran, which is why it looked like a second-question
bug.
"""

from __future__ import annotations

from types import SimpleNamespace

from evals.helpers import response, text_block, tool_block
from knowme.core.loop import run_loop
from knowme.core.tools import Tool, ToolRegistry


class _Capturing:
    """A scripted client that keeps every message list it was called with."""

    def __init__(self, script):
        self._script = list(script)
        self.seen: list[list] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.seen.append([dict(m) for m in kwargs["messages"]])
        return self._script.pop(0)


def _registry(fn) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(name="notes", description="d",
                           input_schema={"type": "object", "properties": {}}, fn=fn))
    return registry


def _run(fn) -> tuple[_Capturing, str]:
    client = _Capturing([response([tool_block("notes", {})]), response([text_block("ok")])])
    result = run_loop(client, "m", "sys", [{"role": "user", "content": "hi"}], _registry(fn))
    return client, result.reply


def test_a_list_returning_tool_reaches_the_model_as_text():
    client, reply = _run(lambda: [{"id": 3, "title": "训练日志"}])
    assert reply == "ok"
    # The second call is the one carrying the tool result.
    tool_results = client.seen[-1][-1]["content"]
    assert isinstance(tool_results[0]["content"], str), "a raw list went back to the model"
    assert '"训练日志"' in tool_results[0]["content"]


def test_a_dict_returning_tool_reaches_the_model_as_text():
    client, _ = _run(lambda: {"id": 3, "title": "训练日志"})
    assert isinstance(client.seen[-1][-1]["content"][0]["content"], str)


def test_a_string_returning_tool_is_not_re_encoded():
    """The overwhelmingly common case must stay byte-for-byte what it was."""
    client, _ = _run(lambda: "LINE ONE\nLINE TWO")
    assert client.seen[-1][-1]["content"][0]["content"] == "LINE ONE\nLINE TWO"
