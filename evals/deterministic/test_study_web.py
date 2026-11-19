"""The /api/study contract: writes, reads, compact /api/data, and audio bytes."""

from __future__ import annotations

import json
from types import SimpleNamespace

from zhiyan.applications import library, study
from zhiyan.config import Settings
from zhiyan.db import connect
from zhiyan.ops import study_cards, study_language
from zhiyan.ops.web import data as web_data

EXAM_TEXT = (
    "# 第三章 微分中值定理\n\n"
    "若函数 f(x) 在闭区间 [a,b] 上连续，在开区间 (a,b) 内可导，"
    "则至少存在一点 ξ∈(a,b)，使得 f(b)-f(a)=f′(ξ)(b-a)。\n\n"
    "这个定理是罗尔定理的推广，也是证明不等式和估计函数变化的基础。\n"
)

LANGUAGE_TEXT = (
    "# Unit 3 · Environment\n\n"
    "The company will mitigate the risk by planting trees along the river. "
    "This is one of several measures described in the article.\n"
)


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)])


def _settings(tmp_path, monkeypatch):
    settings = Settings(home=tmp_path)
    monkeypatch.setattr(web_data, "load_settings", lambda: settings)
    return settings


def _doc(conn, home, name, text):
    return library.save_document(conn, home, name=name,
                                 raw=text.encode("utf-8"), text=text)


def _project_via_api(kind, title, **extra):
    payload = {"action": "create_project", "kind": kind, "title": title,
               "start_date": study.today_iso(), **extra}
    if kind == "exam":
        payload.setdefault("exam_date", study._shift(study.today_iso(), 10))
    return web_data.study_action(payload)


def test_study_info_is_compact_and_json_safe(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch)
    conn = connect(tmp_path)
    project = study.create_project(
        conn, kind="exam", title="数学", start_date="2026-01-01",
        exam_date="2026-02-01")
    study.add_card(conn, project["id"], card_type="qa",
                   front="秘密卡片正面", back="答案", status="active")
    language = study.create_project(
        conn, kind="language", title="英语", start_date="2026-01-01",
        exam_date=None)
    study.save_audio(conn, tmp_path, language["id"], name="clip.mp3",
                     raw=b"audio", transcript="秘密原文")

    info = web_data.study_info(conn)
    dumped = json.dumps(info, ensure_ascii=False)
    assert "数学" in dumped and "英语" in dumped
    assert "秘密卡片正面" not in dumped
    assert "秘密原文" not in dumped
    language_item = next(
        item for item in info["projects"] if item["project"]["id"] == language["id"])
    assert language_item["audio"][0]["has_transcript"] is True
    assert "transcript" not in language_item["audio"][0]
    assert "path" not in language_item["audio"][0]


def test_study_action_creates_material_units_and_plan(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch)
    conn = connect(tmp_path)
    doc = _doc(conn, tmp_path, "chapter.md", EXAM_TEXT)

    created = _project_via_api("exam", "数学", daily_minutes=30)
    assert created["ok"] is True
    project_id = created["result"]["id"]
    assert any(item["project"]["id"] == project_id
               for item in created["data"]["projects"])

    material = web_data.study_action({
        "action": "add_material", "project_id": project_id,
        "doc_id": doc["id"], "role": "textbook"})
    assert material["ok"] is True

    extracted = web_data.study_action({
        "action": "extract_units", "project_id": project_id})
    assert extracted["ok"] is True and extracted["result"]["units"] >= 1

    units = web_data.study_action({
        "action": "list_units", "project_id": project_id})["result"]
    assert units and units[0]["title"]
    unit = web_data.study_action({
        "action": "read_unit", "unit_id": units[0]["id"]})["result"]
    assert "闭区间" in unit["text"]

    plan = web_data.study_action({
        "action": "build_plan", "project_id": project_id})
    assert plan["ok"] is True and plan["result"]["version"] == 1
    assert "cover" in plan["result"]["note"].lower() or "覆盖" in plan["result"]["note"]

    updated = web_data.study_action({
        "action": "update_project", "project_id": project_id,
        "title": "数学（改名）"})
    assert updated["result"]["title"] == "数学（改名）"

    archived = web_data.study_action({
        "action": "archive_project", "project_id": project_id})
    assert archived["ok"] is True
    assert study.get_project(conn, project_id)["status"] == "done"

    unknown = web_data.study_action({"action": "nope"})
    assert unknown["ok"] is False and "未知操作" in unknown["error"]


def test_study_action_cards_review_and_track_isolation(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch)
    conn = connect(tmp_path)
    doc = _doc(conn, tmp_path, "unit.md", LANGUAGE_TEXT)
    created = _project_via_api("language", "英语")
    project_id = created["result"]["id"]
    web_data.study_action({"action": "add_material", "project_id": project_id,
                           "doc_id": doc["id"], "role": "textbook"})
    web_data.study_action({"action": "extract_units", "project_id": project_id})
    unit = web_data.study_action({
        "action": "list_units", "project_id": project_id})["result"][0]

    fake = FakeClient(
        '[{"type":"vocab","term":"mitigate","meaning":"减轻，缓解",'
        '"example":"The company will mitigate the risk by planting trees",'
        '"source_quote":"The company will mitigate the risk by planting trees"}]')
    monkeypatch.setattr(study_cards, "model_for",
                        lambda settings, client=None, model=None: (fake, "fake"))

    generated = web_data.study_action({
        "action": "generate_cards", "unit_id": unit["id"], "count": 1})
    assert generated["ok"] is True
    card = generated["result"]["created"][0]
    assert card["status"] == "candidate"

    listed = web_data.study_action({
        "action": "list_cards", "project_id": project_id,
        "status": "candidate"})["result"]
    assert [item["front"] for item in listed] == ["mitigate"]

    accepted = web_data.study_action({
        "action": "accept_cards", "card_ids": [card["id"]]})
    assert accepted["result"]["accepted"] == 1

    queue = web_data.study_action({
        "action": "review_queue", "project_id": project_id})["result"]
    assert any(item["id"] == card["id"] for item in queue["new"])

    graded = web_data.study_action({
        "action": "grade_card", "card_id": card["id"], "rating": 4,
        "elapsed_ms": 900})
    assert graded["result"]["card"]["reps"] == 1
    assert graded["result"]["due"] == study._shift(study.today_iso(), 1)

    suspended = web_data.study_action({
        "action": "set_card_status", "card_id": card["id"], "status": "suspended"})
    assert suspended["ok"] is True
    assert study.get_card(conn, card["id"])["status"] == "suspended"

    deleted = web_data.study_action({"action": "delete_card", "card_id": card["id"]})
    assert deleted["ok"] is True
    assert study.get_card(conn, card["id"]) is None

    exam_doc = _doc(conn, tmp_path, "exam.md", EXAM_TEXT)
    exam = _project_via_api("exam", "考试")
    exam_id = exam["result"]["id"]
    web_data.study_action({"action": "add_material", "project_id": exam_id,
                           "doc_id": exam_doc["id"], "role": "textbook"})
    web_data.study_action({"action": "extract_units", "project_id": exam_id})
    exam_unit = web_data.study_action({
        "action": "list_units", "project_id": exam_id})["result"][0]
    refused = web_data.study_action({
        "action": "extract_vocab", "unit_id": exam_unit["id"]})
    assert refused["ok"] is False
    refused_import = web_data.study_action({
        "action": "import_vocab", "project_id": exam_id, "text": "a=b"})
    assert refused_import["ok"] is False
    assert "语言项目" in refused_import["error"]


def test_add_card_action_creates_a_candidate_and_rejects_a_duplicate(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch)
    conn = connect(tmp_path)
    doc = _doc(conn, tmp_path, "chapter.md", EXAM_TEXT)
    created = _project_via_api("exam", "手动建卡")
    project_id = created["result"]["id"]
    web_data.study_action({"action": "add_material", "project_id": project_id,
                           "doc_id": doc["id"], "role": "textbook"})
    web_data.study_action({"action": "extract_units", "project_id": project_id})
    unit = web_data.study_action({
        "action": "list_units", "project_id": project_id})["result"][0]

    first = web_data.study_action({
        "action": "add_card", "project_id": project_id, "unit_id": unit["id"],
        "card_type": "qa", "front": "手动问题", "back": "手动答案",
        "source_quote": "若函数 f(x) 在闭区间 [a,b] 上连续"})
    assert first["ok"] is True
    assert first["result"]["status"] == "candidate"

    duplicate = web_data.study_action({
        "action": "add_card", "project_id": project_id,
        "card_type": "qa", "front": "手动问题", "back": "另一个答案"})
    assert duplicate["ok"] is False
    assert "同样" in duplicate["error"]


def test_study_action_language_writing_audio_and_sessions(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch)
    conn = connect(tmp_path)
    created = _project_via_api("language", "英语", target_items=100)
    project_id = created["result"]["id"]

    imported = web_data.study_action({
        "action": "import_vocab", "project_id": project_id,
        "text": "meticulous=一丝不苟的\nalleviate=减轻\n"})
    assert imported["ok"] is True
    assert len(imported["result"]["added"]) == 2

    fake = FakeClient(
        '{"corrected":"I go over my notes every evening.",'
        '"notes":["go over 更口语。","主谓一致正确。"]}')
    monkeypatch.setattr(study_language, "model_for",
                        lambda settings, client=None, model=None: (fake, "fake"))
    written = web_data.study_action({
        "action": "submit_writing", "project_id": project_id,
        "text": "I reviews my notes every evening.", "minutes": 3})
    assert written["ok"] is True
    assert written["result"]["corrected"] == "I go over my notes every evening."
    assert written["result"]["session"]["kind"] == "writing"
    assert written["result"]["session"]["minutes"] == 3

    progress = web_data.study_action({
        "action": "language_progress", "project_id": project_id})
    assert progress["ok"] is True and "vocab_active" in progress["result"]

    audio = study.save_audio(conn, tmp_path, project_id, name="clip.mp3",
                             raw=b"audio", transcript="")
    listed = web_data.study_action({
        "action": "list_audio", "project_id": project_id})["result"]
    assert [item["id"] for item in listed] == [audio["id"]]
    updated = web_data.study_action({
        "action": "update_audio", "audio_id": audio["id"],
        "transcript": "The company will mitigate the risk."})
    assert updated["result"]["transcript"].startswith("The company")
    dictation = web_data.study_action({
        "action": "create_dictation", "audio_id": audio["id"]})
    assert dictation["ok"] is True
    assert dictation["result"]["card_type"] == "dictation"

    # A second audio has no card, so it can be deleted cleanly.
    spare = study.save_audio(conn, tmp_path, project_id, name="spare.mp3",
                             raw=b"audio2")
    removed = web_data.study_action({
        "action": "delete_audio", "audio_id": spare["id"]})
    assert removed["ok"] is True

    uploaded = web_data.study_audio_upload(project_id, "../../evil.mp3", b"audio3")
    assert uploaded["ok"] is True
    assert ".." not in uploaded["result"]["path"]
    bad = web_data.study_audio_upload(project_id, "evil.exe", b"x")
    assert bad["ok"] is False


def test_study_info_is_part_of_the_shared_payload():
    from zhiyan.ops.web import server

    source = __import__("inspect").getsource(server)
    assert '"/api/study": study_action' in source
    assert "/api/study/audio" in source