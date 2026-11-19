"""Word-list parsing, importing, writing correction, and dictation feedback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from zhiyan.applications import study
from zhiyan.config import Settings
from zhiyan.db import connect
from zhiyan.ops import study_language


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)])


def _language_project(conn):
    return study.create_project(
        conn, kind="language", title="英语", start_date="2026-01-01",
        exam_date=None, target_items=100,
    )


def test_parse_word_list_supports_equals_tabs_and_spaces():
    entries, skipped = study_language.parse_word_list(
        "meticulous=一丝不苟的\n"
        "alleviate\t减轻，缓解\n"
        "inevitable  不可避免的\n"
        "lonely\n"
        "meticulous=重复\n"
        "# 注释\n"
    )
    assert [e["term"] for e in entries] == ["meticulous", "alleviate", "inevitable"]
    assert [e["meaning"] for e in entries] == ["一丝不苟的", "减轻，缓解", "不可避免的"]
    assert len(skipped) == 2
    assert skipped[0]["reason"] == "缺少释义"
    assert skipped[1]["reason"] == "词表内重复"


def test_import_words_adds_candidates_and_skips_existing_terms(tmp_path):
    conn = connect(tmp_path)
    project = _language_project(conn)
    study.add_card(conn, project["id"], card_type="vocab", front="meticulous",
                   back="一丝不苟的")
    result = study_language.import_words(
        conn, project["id"],
        "meticulous=旧释义\nalleviate=减轻\ninevitable=不可避免的\n",
    )
    assert len(result["added"]) == 2
    assert result["duplicates"] == 1
    assert result["skipped"] == []
    cards = study.list_cards(conn, project["id"], status="candidate")
    assert {card["front"] for card in cards} == {
        "meticulous", "alleviate", "inevitable"}


def test_import_words_refuses_an_exam_project(tmp_path):
    conn = connect(tmp_path)
    exam = study.create_project(conn, kind="exam", title="数学",
                                start_date="2026-01-01", exam_date="2026-02-01")
    with pytest.raises(ValueError, match="语言项目"):
        study_language.import_words(conn, exam["id"], "a=b")


def test_correct_writing_returns_a_corrected_text_and_notes(tmp_path):
    conn = connect(tmp_path)
    project = _language_project(conn)
    client = FakeClient(
        'Here: {"corrected":"I go over my notes every evening.",'
        '"notes":["go over 比 review 更口语。","主谓一致正确。"]}')
    result = study_language.correct_writing(
        Settings(home=tmp_path), conn, project["id"],
        "I reviews my notes every evening.", client=client, model="fake",
    )
    assert result["ok"] is True
    assert result["corrected"] == "I go over my notes every evening."
    assert len(result["notes"]) == 2


def test_correct_writing_handles_bad_json_without_raising(tmp_path):
    conn = connect(tmp_path)
    project = _language_project(conn)
    result = study_language.correct_writing(
        Settings(home=tmp_path), conn, project["id"], "hello",
        client=FakeClient("not json"), model="fake",
    )
    assert result["ok"] is False
    assert "JSON" in result["error"]


def test_correct_writing_refuses_an_exam_project(tmp_path):
    conn = connect(tmp_path)
    exam = study.create_project(conn, kind="exam", title="数学",
                                start_date="2026-01-01", exam_date="2026-02-01")
    result = study_language.correct_writing(
        Settings(home=tmp_path), conn, exam["id"], "hello",
        client=FakeClient("{}"), model="fake",
    )
    assert result["ok"] is False
    assert "语言项目" in result["error"]


def test_dictation_feedback_maps_score_to_a_rating():
    exact = study_language.dictation_feedback("The quick brown fox", "the quick brown fox")
    assert exact["score"] == 1.0
    assert exact["rating"] == 4
    partial = study_language.dictation_feedback("The quick brown fox", "the quick")
    assert partial["rating"] == 0
    assert partial["label"] == "重来"