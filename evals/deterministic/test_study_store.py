"""学习数据的存取：项目、资料、单元、计划、SM-2、复习日志、音频。

这一层是纯 SQLite + 纯函数，不碰网络、不调模型、不读写页面。所以这里钉住的
都是“数据本身对不对”：单元字符区间能不能对上原文、排期有没有超过每日分钟、
覆盖率是不是真实算出来的、复习日志能不能重算。

所有日期都用固定值或本地 ``today_iso()``，不依赖运行测试的时区时刻。
"""

from __future__ import annotations

import pytest

from zhiyan.applications import library, study
from zhiyan.db import connect


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _project(conn, kind="exam", **kwargs):
    if kind == "exam":
        kwargs.setdefault("exam_date", "2026-01-10")
        kwargs.setdefault("start_date", "2026-01-01")
    params = {"kind": kind, "title": kwargs.pop("title", "测试项目"), **kwargs}
    return study.create_project(conn, **params)


def _doc(conn, home, name, text, raw=None):
    return library.save_document(
        conn, home, name=name, raw=raw if raw is not None else text.encode("utf-8"),
        text=text,
    )


def _insert_unit(conn, project_id, ordinal, *, minutes=5.0, status="new", title=None):
    unit_id = f"u{ordinal}"
    conn.execute(
        """INSERT INTO study_units
           (id, project_id, doc_id, ordinal, title, start_offset, end_offset,
            page_start, page_end, est_minutes, status, weight, created_at)
           VALUES (?,?,'doc',?,?,0,10,NULL,NULL,?,?,1,'2026-01-01T00:00:00+00:00')""",
        (unit_id, project_id, ordinal, title or f"单元 {ordinal}", minutes, status),
    )
    return unit_id


def _insert_card(conn, project_id, index, *, card_type="vocab", status="active",
                 reps=0, interval=0.0, due=None, front=None, back=None):
    card_id = f"c{index}"
    conn.execute(
        """INSERT INTO study_cards
           (id, project_id, unit_id, card_type, front, back, extra_json,
            source_doc_id, source_offset, source_quote, tags, status, ease,
            interval_days, reps, lapses, due, last_reviewed, created_at)
           VALUES (?,?,NULL,?,?,?,'{}','',NULL,'','',?,2.5,?,?,0,?,NULL,
                   '2026-01-01T00:00:00+00:00')""",
        (card_id, project_id, card_type, front or f"词 {index}",
         back or f"释义 {index}", status, interval, reps,
         due or study.today_iso()),
    )
    return card_id


# --------------------------------------------------------------------------
# 表 / 项目
# --------------------------------------------------------------------------
def test_tables_are_created_lazily(tmp_path):
    conn = connect(tmp_path)
    before = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'study_%'"
    ).fetchall()
    assert before == []
    study.list_projects(conn)
    after = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'study_%'"
    ).fetchall()}
    assert after == {
        "study_projects", "study_materials", "study_units", "study_plans",
        "study_cards", "study_reviews", "study_audio", "study_sessions",
    }


def test_exam_project_requires_an_exam_date_and_valid_kind(tmp_path):
    conn = connect(tmp_path)
    with pytest.raises(ValueError, match="考试日期"):
        study.create_project(conn, kind="exam", title="没有日期")
    with pytest.raises(ValueError, match="未知的学习类型"):
        study.create_project(conn, kind="nope", title="x")
    with pytest.raises(ValueError, match="不能早于"):
        study.create_project(conn, kind="exam", title="x", start_date="2026-02-01",
                             exam_date="2026-01-01")


def test_language_project_has_a_default_daily_mix_and_can_update_it(tmp_path):
    conn = connect(tmp_path)
    project = _project(conn, "language", title="英语", exam_date=None,
                       start_date="2026-01-01", target_items=300)
    assert project["settings"]["mix"] == study.DEFAULT_MIX
    assert project["settings"]["mix"]["new_words"] == 15

    updated = study.update_project(conn, project["id"],
                                   settings={"mix": {"new_words": 25}})
    assert updated["settings"]["mix"]["new_words"] == 25
    assert updated["settings"]["mix"]["listening_min"] == 10
    with pytest.raises(ValueError, match="语言练习配额"):
        study.update_project(conn, project["id"],
                             settings={"mix": {"new_words": -1}})


def test_archive_and_list_projects(tmp_path):
    conn = connect(tmp_path)
    exam = _project(conn, "exam", title="考试")
    language = _project(conn, "language", title="语言", exam_date=None,
                        start_date="2026-01-01")
    assert [p["id"] for p in study.list_projects(conn, kind="exam")] == [exam["id"]]
    assert [p["id"] for p in study.list_projects(conn, kind="language")] == [language["id"]]
    assert study.archive_project(conn, exam["id"]) is True
    assert study.get_project(conn, exam["id"])["status"] == "done"
    assert study.archive_project(conn, exam["id"]) is False
    assert study.list_projects(conn) == [language]


# --------------------------------------------------------------------------
# 资料 / 单元
# --------------------------------------------------------------------------
def test_material_round_trip_role_update_and_delete_guard(tmp_path):
    conn = connect(tmp_path)
    project = _project(conn)
    doc = _doc(conn, tmp_path, "chapter.md", "# 第一章\n\n这一章的内容足够长，用来通过正文有效性检查。")

    material = study.add_material(conn, project["id"], doc["id"], "textbook")
    assert material["role"] == "textbook"
    assert study.add_material(conn, project["id"], doc["id"], "syllabus")["role"] == "syllabus"
    assert len(study.list_materials(conn, project["id"])) == 1

    study.add_card(conn, project["id"], card_type="qa", front="问题",
                   back="答案", source_doc_id=doc["id"])
    with pytest.raises(ValueError, match="已经有卡片"):
        study.remove_material(conn, project["id"], doc["id"])


def test_markdown_units_keep_exact_offsets_and_heading_paths(tmp_path):
    conn = connect(tmp_path)
    text = (
        "# 第一章 函数与极限\n\n"
        "函数是微积分研究的基本对象，本章从集合与映射开始，逐步引入函数的概念、性质与常见初等函数。\n\n"
        "## 极限\n\n"
        "极限描述的是当自变量无限接近某个值时，函数值的变化趋势。理解极限是理解导数与积分的前提。\n\n"
        "## 连续\n\n"
        "连续函数在一点的极限等于该点的函数值。连续性是许多定理成立的条件，也是判断间断点类型的基础。\n"
    )
    doc = _doc(conn, tmp_path, "chapter.md", text)
    project = _project(conn)
    study.add_material(conn, project["id"], doc["id"], "textbook")
    result = study.extract_units(conn, tmp_path, project["id"])
    assert result["units"] == 3
    assert result["warnings"] == []

    units = study.list_units(conn, project["id"])
    assert [u["title"] for u in units] == [
        "第一章 函数与极限",
        "第一章 函数与极限 › 极限",
        "第一章 函数与极限 › 连续",
    ]
    for unit in units:
        assert unit["page_start"] is None
        assert unit["est_minutes"] >= 5
        assert text[unit["start_offset"]:unit["end_offset"]].strip()
    assert "极限是理解" in text[units[1]["start_offset"]:units[1]["end_offset"]]
    assert study.unit_text(conn, units[1]["id"])["text"].startswith("## 极限")


def test_outline_lines_become_one_unit_per_topic(tmp_path):
    conn = connect(tmp_path)
    lines = [f"{i}. 考纲主题 {i}" for i in range(1, 11)]
    text = "\n".join(lines) + "\n"
    doc = _doc(conn, tmp_path, "syllabus.txt", text)
    project = _project(conn)
    study.add_material(conn, project["id"], doc["id"], "syllabus")
    study.extract_units(conn, tmp_path, project["id"])
    units = study.list_units(conn, project["id"])
    assert len(units) == 10
    assert units[0]["title"] == "1. 考纲主题 1"
    assert units[-1]["title"] == "10. 考纲主题 10"
    for unit in units:
        assert text[unit["start_offset"]:unit["end_offset"]] == unit["title"]


def test_a_single_huge_paragraph_is_split_without_losing_offsets(tmp_path):
    conn = connect(tmp_path)
    sentence = "这是一段用于测试长段落切分的文字。"
    text = sentence * 300
    doc = _doc(conn, tmp_path, "long.txt", text)
    project = _project(conn)
    study.add_material(conn, project["id"], doc["id"], "textbook")
    study.extract_units(conn, tmp_path, project["id"])
    units = study.list_units(conn, project["id"])
    assert len(units) > 1
    covered = 0
    for unit in units:
        slice_ = text[unit["start_offset"]:unit["end_offset"]]
        assert slice_.strip()
        assert len(slice_) <= study.MAX_UNIT_CHARS
        covered += len(slice_)
    assert covered == len(text)


def test_pdf_pages_map_to_character_offsets(tmp_path, monkeypatch):
    conn = connect(tmp_path)
    pages = [
        "第一页的内容。" + "甲" * 60,
        "第二页的内容。" + "乙" * 60,
    ]
    text = "\n\n".join(pages)
    doc = _doc(conn, tmp_path, "book.pdf", text, raw=b"%PDF-1.4\nfake")
    assert doc["kind"] == "pdf"
    monkeypatch.setattr("zhiyan.applications.reader.parse_pages",
                        lambda *args, **kwargs: pages)

    project = _project(conn, "exam", start_date="2026-01-01", exam_date="2026-02-01")
    study.add_material(conn, project["id"], doc["id"], "textbook")
    result = study.extract_units(conn, tmp_path, project["id"])
    assert result["warnings"] == []
    units = study.list_units(conn, project["id"])
    assert len(units) == 1
    unit = units[0]
    assert (unit["page_start"], unit["page_end"]) == (1, 2)
    assert text[unit["start_offset"]:unit["end_offset"]] == text


def test_pdf_falls_back_to_paragraphs_when_text_does_not_match(tmp_path, monkeypatch):
    conn = connect(tmp_path)
    text = "第一段的内容。" + "甲" * 60 + "\n\n第二段的内容。" + "乙" * 60
    doc = _doc(conn, tmp_path, "book.pdf", text, raw=b"%PDF-1.4\nfake")
    monkeypatch.setattr("zhiyan.applications.reader.parse_pages",
                        lambda *args, **kwargs: ["完全不同的文本"])
    project = _project(conn, "exam", start_date="2026-01-01", exam_date="2026-02-01")
    study.add_material(conn, project["id"], doc["id"], "textbook")
    result = study.extract_units(conn, tmp_path, project["id"])
    assert any("不一致" in warning for warning in result["warnings"])
    units = study.list_units(conn, project["id"])
    assert units
    assert all(unit["page_start"] is None for unit in units)


def test_pdf_a_single_toc_page_stays_one_unit(tmp_path, monkeypatch):
    page = ("目录\n" + ("第一章 马克思主义自然观 " + "." * 40 + " 1\n") * 45).strip()
    assert 2400 < len(page) <= study.PDF_UNIT_MAX
    conn = connect(tmp_path)
    doc = _doc(conn, tmp_path, "toc.pdf", page, raw=b"%PDF-1.4\nfake")
    monkeypatch.setattr("zhiyan.applications.reader.parse_pages",
                        lambda *args, **kwargs: [page])
    project = _project(conn, "exam", start_date="2026-01-01", exam_date="2026-02-01")
    study.add_material(conn, project["id"], doc["id"], "textbook")
    result = study.extract_units(conn, tmp_path, project["id"])
    assert result["warnings"] == []
    units = study.list_units(conn, project["id"])
    assert len(units) == 1, "a 2.5k-character TOC page must not become 38 fragments"
    unit = units[0]
    assert unit["title"] == "第 1 页"
    assert page[unit["start_offset"]:unit["end_offset"]] == page


def test_pdf_a_huge_page_is_split_into_readable_packed_units(tmp_path, monkeypatch):
    page = ("这是一段用于测试超长 PDF 页面的文字。" * 500).strip()
    assert len(page) > study.PDF_UNIT_MAX
    conn = connect(tmp_path)
    doc = _doc(conn, tmp_path, "huge.pdf", page, raw=b"%PDF-1.4\nfake")
    monkeypatch.setattr("zhiyan.applications.reader.parse_pages",
                        lambda *args, **kwargs: [page])
    project = _project(conn, "exam", start_date="2026-01-01", exam_date="2026-02-01")
    study.add_material(conn, project["id"], doc["id"], "textbook")
    study.extract_units(conn, tmp_path, project["id"])
    units = study.list_units(conn, project["id"])
    assert 1 < len(units) < 10, "a huge page is split, but packed into readable units"
    for unit in units:
        assert page[unit["start_offset"]:unit["end_offset"]].strip()
        assert len(page[unit["start_offset"]:unit["end_offset"]]) <= study.PDF_UNIT_MAX


def test_extract_units_refuses_to_invalidate_existing_cards(tmp_path):
    conn = connect(tmp_path)
    text = "# 章节\n\n" + "这一章的内容足够长，用来通过正文有效性检查。" * 3
    doc = _doc(conn, tmp_path, "chapter.md", text)
    project = _project(conn)
    study.add_material(conn, project["id"], doc["id"], "textbook")
    study.extract_units(conn, tmp_path, project["id"])
    unit = study.list_units(conn, project["id"])[0]
    study.add_card(conn, project["id"], card_type="qa", front="问题",
                   back="答案", unit_id=unit["id"])
    with pytest.raises(ValueError, match="已经有卡片"):
        study.extract_units(conn, tmp_path, project["id"])


def test_unit_update_merge_and_delete(tmp_path):
    conn = connect(tmp_path)
    text = (
        "# 第一章\n\n" + "甲" * 60 + "\n\n"
        "## 第一节\n\n" + "乙" * 60 + "\n\n"
        "## 第二节\n\n" + "丙" * 60 + "\n"
    )
    doc = _doc(conn, tmp_path, "chapter.md", text)
    project = _project(conn)
    study.add_material(conn, project["id"], doc["id"], "textbook")
    study.extract_units(conn, tmp_path, project["id"])
    units = study.list_units(conn, project["id"])
    assert len(units) == 3

    study.update_unit(conn, units[1]["id"], title="改名后的单元", weight=2, status="learning")
    changed = study.get_unit(conn, units[1]["id"])
    assert (changed["title"], changed["weight"], changed["status"]) == (
        "改名后的单元", 2, "learning")

    merged = study.merge_units(conn, project["id"], [units[1]["id"], units[2]["id"]])
    assert " + " in merged["title"]
    assert merged["start_offset"] == units[1]["start_offset"]
    assert merged["end_offset"] == units[2]["end_offset"]
    assert len(study.list_units(conn, project["id"])) == 2

    study.add_card(conn, project["id"], card_type="qa", front="问题",
                   back="答案", unit_id=merged["id"])
    with pytest.raises(ValueError, match="已经有卡片"):
        study.delete_unit(conn, merged["id"])


# --------------------------------------------------------------------------
# 考试排期
# --------------------------------------------------------------------------
def test_exam_plan_respects_daily_minutes_and_reports_coverage(tmp_path):
    conn = connect(tmp_path)
    project = _project(conn, "exam", start_date="2026-01-01", exam_date="2026-01-03",
                       daily_minutes=30)
    for index in range(20):
        _insert_unit(conn, project["id"], index, minutes=5)
    conn.commit()

    plan = study.build_exam_plan(conn, project["id"], start="2026-01-01")
    learn = [item for item in plan["items"] if item["kind"] == "learn"]
    assert len(learn) == 2
    assert all(item["minutes"] <= 30 for item in learn)
    assert sum(len(item["unit_ids"]) for item in learn) == 12
    assert "60%" in plan["note"]
    assert plan["horizon_start"] == "2026-01-01"
    assert plan["horizon_end"] == "2026-01-03"

    second = study.build_exam_plan(conn, project["id"], start="2026-01-01")
    assert second["version"] == 2
    versions = study.list_plans(conn, project["id"])
    assert versions[0]["status"] == "active"
    assert versions[1]["status"] == "archived"


def test_exam_replan_keeps_today_and_starts_tomorrow(tmp_path):
    conn = connect(tmp_path)
    today = study.today_iso()
    tomorrow = study._shift(today, 1)
    project = _project(conn, "exam", start_date=today,
                       exam_date=study._shift(today, 10), daily_minutes=30)
    for index in range(12):
        _insert_unit(conn, project["id"], index, minutes=5)
    conn.commit()

    first = study.build_exam_plan(conn, project["id"])
    today_items = [item for item in first["items"] if item["date"] == today]
    assert today_items

    replanned = study.replan(conn, project["id"])
    assert replanned["version"] == 2
    assert replanned["items"][0]["date"] == today
    assert any(item["date"] == tomorrow for item in replanned["items"])
    assert "本次重排从明天开始" in replanned["note"]


# --------------------------------------------------------------------------
# 语言排期
# --------------------------------------------------------------------------
def test_language_plan_uses_daily_mix_and_scales_for_an_exam(tmp_path):
    conn = connect(tmp_path)
    project = _project(
        conn, "language", start_date="2026-01-01", exam_date="2026-01-10",
        daily_minutes=60, new_cards_per_day=20, target_items=300,
        settings={"mix": {"new_words": 10, "listening_min": 5, "speaking_min": 5,
                          "reading_items": 1, "writing_sentences": 2}},
    )
    for index in range(250):
        _insert_card(conn, project["id"], index, card_type="vocab", reps=0)
    conn.commit()

    plan = study.build_language_plan(conn, project["id"], start="2026-01-01")
    days = [item for item in plan["items"] if item["date"] == "2026-01-01"]
    kinds = {item["kind"] for item in days}
    assert kinds == {"vocab_new", "review", "listening", "speaking", "reading", "writing"}
    vocab = next(item for item in days if item["kind"] == "vocab_new")
    assert vocab["target"]["new_words"] == 20      # capped, not 25
    assert "80%" in plan["note"]
    assert plan["horizon_end"] == "2026-01-10"
    assert len(days) == 6


def test_language_plan_without_exam_uses_a_fourteen_day_window(tmp_path):
    conn = connect(tmp_path)
    project = _project(conn, "language", start_date="2026-01-01", exam_date=None,
                       daily_minutes=60)
    plan = study.build_language_plan(conn, project["id"], start="2026-01-01")
    assert plan["horizon_start"] == "2026-01-01"
    assert plan["horizon_end"] == "2026-01-14"
    vocab = next(item for item in plan["items"] if item["kind"] == "vocab_new")
    assert vocab["target"]["new_words"] == study.DEFAULT_MIX["new_words"]


# --------------------------------------------------------------------------
# SM-2 / 复习
# --------------------------------------------------------------------------
def test_sm2_sequence_and_lapse():
    first = study.sm2(2.5, 0, 0, 0, 4)
    assert (first["interval_days"], first["reps"], first["ease"]) == (1, 1, 2.5)
    second = study.sm2(first["ease"], first["interval_days"], first["reps"],
                       first["lapses"], 4)
    assert (second["interval_days"], second["reps"]) == (6, 2)
    third = study.sm2(second["ease"], second["interval_days"], second["reps"],
                      second["lapses"], 4)
    assert third["interval_days"] == 15
    assert third["reps"] == 3

    lapse = study.sm2(third["ease"], third["interval_days"], third["reps"],
                      third["lapses"], 0)
    assert lapse["interval_days"] == 1
    assert lapse["reps"] == 0
    assert lapse["lapses"] == 1
    assert lapse["ease"] < third["ease"]

    easy = study.sm2(2.5, 6, 2, 0, 5)
    assert easy["ease"] > 2.5
    with pytest.raises(ValueError, match="评分"):
        study.sm2(2.5, 1, 1, 0, 2)


def test_review_queue_limits_new_cards_and_logs_every_rating(tmp_path):
    conn = connect(tmp_path)
    project = _project(conn, "language", exam_date=None, start_date="2026-01-01",
                       new_cards_per_day=2)
    for index in range(3):
        _insert_card(conn, project["id"], index, card_type="vocab", reps=0)
    conn.commit()
    day = study.today_iso()

    queue = study.review_queue(conn, project["id"], day=day)
    assert len(queue["new"]) == 2
    assert queue["new_remaining"] == 2
    assert queue["total"] == 2

    first = queue["new"][0]
    result = study.grade_card(conn, first["id"], 4, day=day)
    assert result["due"] == study._shift(day, 1)
    assert result["card"]["reps"] == 1
    assert study.new_cards_remaining(conn, project["id"], day) == 1

    again = study.grade_card(conn, first["id"], 0, day=day)
    assert again["interval_days"] == 1
    assert again["reps"] == 0
    assert again["card"]["lapses"] == 1

    log = study.review_log(conn, first["id"])
    assert len(log) == 2
    assert log[0]["prev_reps"] == 0
    assert log[0]["next_interval"] == 1
    assert log[1]["rating"] == 0
    assert study.due_cards(conn, project["id"], day=day, include_new=False) == []


def test_project_stats_streak_accuracy_and_language_coverage(tmp_path):
    conn = connect(tmp_path)
    today = study.today_iso()
    yesterday = study._shift(today, -1)
    project = _project(conn, "language", exam_date=None, start_date="2026-01-01",
                       target_items=10)
    _insert_card(conn, project["id"], 0, card_type="vocab", reps=2, interval=21,
                 due=today)
    _insert_card(conn, project["id"], 1, card_type="vocab", reps=1, interval=3,
                 due=study._shift(today, 2))
    _insert_card(conn, project["id"], 2, card_type="vocab", reps=1, interval=3,
                 due=yesterday)
    conn.commit()

    study.grade_card(conn, "c0", 4, day=today)
    study.grade_card(conn, "c2", 0, day=yesterday)
    stats = study.project_stats(conn, project["id"], today)
    assert stats["streak"] == 2
    assert stats["accuracy_7d"] == 0.5
    assert stats["vocab_total"] == 3
    assert stats["vocab_mature"] == 1
    assert stats["vocab_active"] == 2
    assert stats["vocab_unseen"] == 1
    assert stats["target_coverage"] == 0.2


# --------------------------------------------------------------------------
# 音频 / 听写
# --------------------------------------------------------------------------
def test_audio_storage_uses_uuid_and_keeps_newlines_in_transcript(tmp_path):
    conn = connect(tmp_path)
    project = _project(conn, "language", exam_date=None, start_date="2026-01-01")
    audio = study.save_audio(conn, tmp_path, project["id"],
                             name="../../evil.mp3", raw=b"fake-audio",
                             transcript="hello\nworld")
    assert audio["path"].startswith("study/audio/")
    assert "evil" not in audio["path"]
    assert ".." not in audio["path"]
    assert audio["transcript"] == "hello\nworld"
    path = study.audio_file_path(conn, tmp_path, audio["id"])
    assert path is not None and path.is_file()

    with pytest.raises(ValueError, match="只支持"):
        study.save_audio(conn, tmp_path, project["id"], name="evil.exe", raw=b"x")

    card = study.create_dictation_card(conn, project["id"], audio["id"])
    assert card["card_type"] == "dictation"
    assert card["extra"]["audio_id"] == audio["id"]
    with pytest.raises(ValueError, match="已经有听写卡"):
        study.create_dictation_card(conn, project["id"], audio["id"])
    with pytest.raises(ValueError, match="已经有听写卡|已经有卡片"):
        study.delete_audio(conn, tmp_path, audio["id"])


def test_dictation_score_is_positional_and_ignores_punctuation():
    expected = "The university has announced that tuition fees will remain unchanged."
    assert study.dictation_score(expected, expected) == 1.0
    assert study.dictation_score(expected, expected.upper()) == 1.0
    assert study.dictation_score(expected, "the university announced that tuition fees") < 1.0
    assert study.normalize_dictation("Hello,   WORLD!") == "hello world"
    assert study.rating_for_score(1.0) == 4
    assert study.rating_for_score(0.8) == 3
    assert study.rating_for_score(0.2) == 0