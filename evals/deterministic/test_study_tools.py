"""The study tools: readable output, track isolation, and no hidden writes."""

from __future__ import annotations

from types import SimpleNamespace

from zhiyan.applications import library, study
from zhiyan.config import Settings
from zhiyan.db import connect
from zhiyan.ops import study_cards
from zhiyan.tools.study import make_study_tools

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


def _exam_unit(conn, home):
    doc = library.save_document(conn, home, name="chapter.md",
                                raw=EXAM_TEXT.encode("utf-8"), text=EXAM_TEXT)
    project = study.create_project(
        conn, kind="exam", title="高等数学 · 期末冲刺",
        start_date=study.today_iso(), exam_date=study._shift(study.today_iso(), 10),
        daily_minutes=30,
    )
    study.add_material(conn, project["id"], doc["id"], "textbook")
    study.extract_units(conn, home, project["id"])
    return project, study.list_units(conn, project["id"])[0]


def _language_unit(conn, home):
    doc = library.save_document(conn, home, name="unit.md",
                                raw=LANGUAGE_TEXT.encode("utf-8"), text=LANGUAGE_TEXT)
    project = study.create_project(
        conn, kind="language", title="英语 · CET-6",
        start_date="2026-01-01", exam_date=None, target_items=4500,
    )
    study.add_material(conn, project["id"], doc["id"], "textbook")
    study.extract_units(conn, home, project["id"])
    return project, study.list_units(conn, project["id"])[0]


def test_tools_tell_the_agent_when_there_is_no_data(tmp_path):
    conn = connect(tmp_path)
    tools = make_study_tools(conn, tmp_path, Settings(home=tmp_path))
    assert "还没有学习项目" in tools["study_overview"].fn()
    assert "还没有学习项目" in tools["list_study_projects"].fn()


def test_exam_tools_read_plan_units_and_mark_done(tmp_path):
    conn = connect(tmp_path)
    project, unit = _exam_unit(conn, tmp_path)
    study.build_exam_plan(conn, project["id"])
    tools = make_study_tools(conn, tmp_path, Settings(home=tmp_path))

    overview = tools["study_overview"].fn()
    assert "高等数学" in overview and "考试" in overview
    plan = tools["study_plan"].fn(project["id"])
    assert "v1" in plan and unit["title"] in plan and "说明" in plan
    units = tools["list_units"].fn(project["id"])
    assert unit["id"] in units and unit["title"] in units
    read = tools["read_unit"].fn(unit["id"])
    assert "闭区间" in read and "第三章" in read and "单元结束" in read

    replanned = tools["replan_study"].fn(project["id"])
    assert "v2" in replanned and "明天" in replanned
    marked = tools["mark_unit"].fn(unit["id"], "done")
    assert "done" in marked
    assert study.get_unit(conn, unit["id"])["status"] == "done"


def test_language_only_tools_refuse_an_exam_project(tmp_path):
    conn = connect(tmp_path)
    project, unit = _exam_unit(conn, tmp_path)
    tools = make_study_tools(conn, tmp_path, Settings(home=tmp_path))

    assert "只适用于语言项目" in tools["extract_vocab"].fn(unit["id"])
    assert "只适用于语言项目" in tools["language_progress"].fn(project["id"])
    imported = tools["import_vocab"].fn(project["id"], "a=b")
    assert "没导入" in imported and "语言项目" in imported


def test_language_tools_generate_vocab_import_and_dictation(tmp_path, monkeypatch):
    conn = connect(tmp_path)
    project, unit = _language_unit(conn, tmp_path)
    settings = Settings(home=tmp_path)
    client = FakeClient(
        '[{"type":"vocab","term":"mitigate","meaning":"减轻，缓解",'
        '"example":"The company will mitigate the risk by planting trees.",'
        '"source_quote":"The company will mitigate the risk by planting trees"}]')
    fake = client
    monkeypatch.setattr(study_cards, "model_for",
                        lambda settings, client=None, model=None: (fake, "fake"))

    tools = make_study_tools(conn, tmp_path, settings)
    generated = tools["create_cards"].fn(unit["id"], 2)
    assert "mitigate" in generated and "候选" in generated

    client.text = (
        '[{"type":"vocab","term":"risk","meaning":"风险",'
        '"example":"The company will mitigate the risk by planting trees.",'
        '"source_quote":"the risk by planting trees"}]')
    extracted = tools["extract_vocab"].fn(unit["id"], 1)
    assert "risk" in extracted and "候选" in extracted

    imported = tools["import_vocab"].fn(project["id"], "incentive=激励\n")
    assert "新增 1" in imported and "incentive" in imported

    cards = tools["list_cards"].fn(project["id"], query="mitigate")
    assert "mitigate" in cards and "candidate" in cards

    progress = tools["language_progress"].fn(project["id"])
    assert "活跃词" in progress and "成熟词" in progress

    audio = study.save_audio(conn, tmp_path, project["id"], name="clip.mp3",
                             raw=b"audio-bytes",
                             transcript="The company will mitigate the risk.")
    dictation = tools["create_dictation"].fn(audio["id"])
    assert "听写" in dictation and "候选" in dictation


def test_creating_cards_does_not_accept_them_for_the_user(tmp_path, monkeypatch):
    conn = connect(tmp_path)
    project, unit = _language_unit(conn, tmp_path)
    client = FakeClient(
        '[{"type":"vocab","term":"mitigate","meaning":"减轻",'
        '"example":"The company will mitigate the risk by planting trees.",'
        '"source_quote":"The company will mitigate the risk by planting trees"}]')
    fake = client
    monkeypatch.setattr(study_cards, "model_for",
                        lambda settings, client=None, model=None: (fake, "fake"))
    tools = make_study_tools(conn, tmp_path, Settings(home=tmp_path))
    tools["create_cards"].fn(unit["id"], 1)
    cards = study.list_cards(conn, project["id"])
    assert [card["status"] for card in cards] == ["candidate"]
    assert study.due_cards(conn, project["id"]) == []