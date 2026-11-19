"""Card generation must be grounded in the unit, not in the model's memory.

The deterministic eval never talks to a provider: a fake client returns a fixed
JSON array, and the server-side quote check decides what survives. The two cards
below are deliberately the same shape — one quote is in the material, one is
invented — because that is the failure mode that matters.
"""

from __future__ import annotations

from types import SimpleNamespace

from zhiyan.applications import library, study
from zhiyan.config import Settings
from zhiyan.db import connect
from zhiyan.ops import study_cards

EXAM_TEXT = (
    "# 第三章 微分中值定理\n\n"
    "若函数 f(x) 在闭区间 [a,b] 上连续，在开区间 (a,b) 内可导，"
    "则至少存在一点 ξ∈(a,b)，使得 f(b)-f(a)=f′(ξ)(b-a)。\n\n"
    "这个定理是罗尔定理的推广，也是证明不等式和估计函数变化的基础。\n"
)

LANGUAGE_TEXT = (
    "# Unit 3 · Environment\n\n"
    "The company will mitigate the risk by planting trees along the river. "
    "This is one of several measures described in the article.\n\n"
    "The word mitigate means to make something less severe or harmful.\n"
)


class _Messages:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)])


class FakeClient:
    def __init__(self, text):
        self.messages = _Messages(text)


class BoomClient:
    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise RuntimeError("provider is down")


def _unit_for(conn, home, name, text, kind):
    doc = library.save_document(conn, home, name=name, raw=text.encode("utf-8"),
                                text=text)
    project = study.create_project(
        conn, kind=kind, title="测试项目",
        start_date="2026-01-01",
        exam_date="2026-02-01" if kind == "exam" else None,
    )
    study.add_material(conn, project["id"], doc["id"], "textbook")
    study.extract_units(conn, home, project["id"])
    unit = study.list_units(conn, project["id"])[0]
    return project, unit


def _settings(home):
    return Settings(home=home)


def test_exam_cards_reject_a_fabricated_quote_and_keep_the_real_one(tmp_path):
    conn = connect(tmp_path)
    _, unit = _unit_for(conn, tmp_path, "chapter.md", EXAM_TEXT, "exam")
    client = FakeClient("""
    Here are the cards:
    [
      {"type":"qa","front":"拉格朗日中值定理的条件？",
       "back":"闭区间连续、开区间可导。",
       "source_quote":"若函数 f(x) 在闭区间 [a,b] 上连续"},
      {"type":"qa","front":"这个定理还能推出什么？",
       "back":"很多结论。",
       "source_quote":"这句话是模型编造的，原文里没有。"}
    ]
    """)

    result = study_cards.generate_cards(_settings(tmp_path), conn, unit["id"],
                                        count=2, client=client, model="fake")

    assert result["ok"] is True
    assert len(result["created"]) == 1
    assert len(result["rejected"]) == 1
    assert "引文不在" in result["rejected"][0]["reason"]
    card = result["created"][0]
    assert card["status"] == "candidate"
    assert card["card_type"] == "qa"
    assert exam_slice(conn, card, EXAM_TEXT).startswith("若函数 f(x)")
    assert card["source_quote"] == "若函数 f(x) 在闭区间 [a,b] 上连续"


def exam_slice(conn, card, text):
    return text[card["source_offset"]:card["source_offset"] + 40]


def test_language_vocab_keeps_phonetic_and_example(tmp_path):
    conn = connect(tmp_path)
    _, unit = _unit_for(conn, tmp_path, "unit.md", LANGUAGE_TEXT, "language")
    client = FakeClient("""
    [
      {"type":"vocab","term":"mitigate","phonetic":"/ˈmɪtɪɡeɪt/","pos":"v.",
       "meaning":"减轻，缓解",
       "example":"The company will mitigate the risk by planting trees.",
       "example_translation":"公司会通过种树来减轻风险。",
       "source_quote":"The company will mitigate the risk by planting trees"}
    ]
    """)

    result = study_cards.generate_cards(_settings(tmp_path), conn, unit["id"],
                                        count=3, client=client, model="fake")
    assert result["ok"] is True
    assert len(result["created"]) == 1
    card = result["created"][0]
    assert card["card_type"] == "vocab"
    assert (card["front"], card["back"]) == ("mitigate", "减轻，缓解")
    assert card["extra"]["phonetic"] == "/ˈmɪtɪɡeɪt/"
    assert card["extra"]["example"].startswith("The company")
    assert card["source_offset"] >= 0


def test_generation_is_idempotent_on_the_same_front(tmp_path):
    conn = connect(tmp_path)
    _, unit = _unit_for(conn, tmp_path, "unit.md", LANGUAGE_TEXT, "language")
    client = FakeClient("""
    [{"type":"vocab","term":"mitigate","meaning":"减轻",
      "example":"The company will mitigate the risk by planting trees.",
      "source_quote":"The company will mitigate the risk by planting trees"}]
    """)
    settings = _settings(tmp_path)

    first = study_cards.generate_cards(settings, conn, unit["id"],
                                       client=client, model="fake")
    second = study_cards.generate_cards(settings, conn, unit["id"],
                                        client=client, model="fake")
    assert len(first["created"]) == 1
    assert first["duplicates"] == 0
    assert second["created"] == []
    assert second["duplicates"] == 1


def test_track_isolation_is_enforced_by_the_generator(tmp_path):
    conn = connect(tmp_path)
    _, exam_unit = _unit_for(conn, tmp_path, "chapter.md", EXAM_TEXT, "exam")
    _, language_unit = _unit_for(
        conn, tmp_path, "unit.md", LANGUAGE_TEXT, "language")

    bad = study_cards.generate_cards(_settings(tmp_path), conn, exam_unit["id"],
                                     card_types=("vocab",), client=FakeClient("[]"),
                                     model="fake")
    assert bad["ok"] is False
    assert "不能生成" in bad["error"]

    bad_again = study_cards.generate_cards(
        _settings(tmp_path), conn, language_unit["id"], card_types=("qa",),
        client=FakeClient("[]"), model="fake")
    assert bad_again["ok"] is False


def test_model_failure_is_returned_as_text_not_raised(tmp_path):
    conn = connect(tmp_path)
    _, unit = _unit_for(conn, tmp_path, "chapter.md", EXAM_TEXT, "exam")
    result = study_cards.generate_cards(_settings(tmp_path), conn, unit["id"],
                                        client=BoomClient(), model="fake")
    assert result["ok"] is False
    assert "模型调用失败" in result["error"]


def test_a_card_without_a_quote_is_rejected(tmp_path):
    conn = connect(tmp_path)
    _, unit = _unit_for(conn, tmp_path, "chapter.md", EXAM_TEXT, "exam")
    client = FakeClient('[{"type":"qa","front":"问题","back":"答案"}]')
    result = study_cards.generate_cards(_settings(tmp_path), conn, unit["id"],
                                        client=client, model="fake")
    assert result["ok"] is True
    assert result["created"] == []
    assert result["rejected"][0]["reason"] == "原文引文不在单元里"


def test_truncated_json_array_keeps_the_complete_cards(tmp_path):
    conn = connect(tmp_path)
    _, unit = _unit_for(conn, tmp_path, "chapter.md", EXAM_TEXT, "exam")
    # The closing ] and the second object are missing: a reasoning model can
    # hit max_tokens mid-array. The first complete card is still usable.
    client = FakeClient(
        '[{"type":"qa","front":"问题一","back":"答案一",'
        '"source_quote":"若函数 f(x) 在闭区间 [a,b] 上连续"},'
        '{"type":"qa","front":"问题二","back":"答案二"')
    result = study_cards.generate_cards(_settings(tmp_path), conn, unit["id"],
                                        client=client, model="fake")
    assert result["ok"] is True
    assert len(result["created"]) == 1
    assert result["created"][0]["front"] == "问题一"


def test_quota_errors_become_an_actionable_message():
    message = study_cards.provider_error(RuntimeError(
        "Error code: 403 - {'error': {'type': 'insufficient_quota', "
        "'message': 'Free quota exhausted.'}}"))
    assert "额度不足" in message
    assert "模型" in message and "服务商" in message


def test_locate_quote_folds_whitespace_but_not_wording():
    text = "The company will\nmitigate the risk."
    assert study_cards.locate_quote(text, "The company will mitigate") is not None
    assert study_cards.locate_quote(text, "The company might mitigate") is None


def test_locate_quote_forgives_punctuation_but_not_paraphrase():
    text = "若函数 f(x) 在闭区间 [a,b] 上连续，则至少存在一点 ξ。"
    assert study_cards.locate_quote(
        text, "若函数 f(x) 在闭区间 [a,b] 上连续 则至少存在一点 ξ") is not None
    assert study_cards.locate_quote(
        text, "若函数在闭区间上连续，则存在一点 ξ") is None
