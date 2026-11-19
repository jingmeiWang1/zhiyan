"""Study tools — Exam Coach and Language Tutor's hands on the study tables.

The tools read and write ``applications/study.py`` only. Card generation and
word-list import go through ``ops/study_cards.py`` and
``ops/study_language.py`` so the web page and the agent use ONE implementation.

Two boundaries are enforced here, not just in the prompts:

    track isolation   every project-scoped tool checks the project's kind.
                     An Exam tool refuses a language project and vice versa.
    candidate cards   AI cards are written as ``candidate``; the user confirms
                     them on the page. The tools never accept on their behalf.

The write tools are deliberately small. ``mark_unit`` exists because the user
says "这一节我看完了"; the agent must not decide that on its own. Plans are
rebuilt by code (``replan_study``), not composed by the model.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from zhiyan.applications import study
from zhiyan.core.tools import Tool
from zhiyan.ops import study_cards, study_language


def _project(conn: sqlite3.Connection, project_id: str) -> dict:
    project = study.get_project(conn, project_id)
    if project is None:
        raise ValueError(f"没有 id 为 {project_id} 的学习项目")
    return project


def _check_track(track: str) -> str:
    track = (track or "").strip()
    if track and track not in study.KINDS:
        raise ValueError("track 只能是 exam 或 language")
    return track


def _project_line(project: dict, stats: dict) -> str:
    days = stats.get("exam_days")
    if project["kind"] == "exam":
        bits = [f"距考试 {days} 天" if days is not None else "未设考试日期",
                f"每天 {project['daily_minutes']} 分钟",
                f"覆盖 {round(stats.get('coverage', 0) * 100)}%",
                f"单元 {stats['units_done']}/{stats['units_total']}",
                f"到期 {stats['due']} 张"]
        label = "考试"
    else:
        target = stats.get("target_items")
        bits = [f"活跃词 {stats['vocab_active']}" + (f"/{target}" if target else ""),
                f"成熟 {stats['vocab_mature']}",
                f"到期 {stats['due']} 张",
                f"连续 {stats['streak']} 天"]
        if days is not None:
            bits.insert(0, f"距考试 {days} 天")
        label = "语言"
    if project["status"] != "active":
        bits.append(f"状态 {project['status']}")
    return (f"- [{label}] {project['title']} [id={project['id']}] "
            + " · ".join(bits))


def make_study_tools(conn: sqlite3.Connection, home: Path,
                     settings) -> dict[str, Tool]:
    """Build the study tools. `home` is kept for the audio path later; the
    current tools only need the connection and settings."""

    def _overview(track: str = "") -> str:
        try:
            track = _check_track(track)
        except ValueError as exc:
            return f"查不了：{exc}。"
        projects = study.list_projects(conn, kind=track or None, status=None)[:8]
        if not projects:
            return "还没有学习项目。先在页面新建一个考试或语言项目。"
        lines = [f"学习项目（{len(projects)} 个）："]
        for project in projects:
            try:
                stats = study.project_stats(conn, project["id"])
                agenda = study.today_agenda(conn, project["id"])
            except ValueError:
                continue
            lines.append(_project_line(project, stats))
            lines.append(
                f"  今天：到期 {len(agenda['due'])} 张，新卡 {len(agenda['new'])} 张，"
                f"计划 {len(agenda['items'])} 项。")
        return "\n".join(lines)

    def _projects(track: str = "") -> str:
        try:
            track = _check_track(track)
        except ValueError as exc:
            return f"查不了：{exc}。"
        projects = study.list_projects(conn, kind=track or None, status=None)
        if not projects:
            return "还没有学习项目。"
        lines = []
        for project in projects:
            lines.append(
                f"- {project['title']} [id={project['id']}] kind={project['kind']} "
                f"status={project['status']} 每天 {project['daily_minutes']} 分钟"
                + (f" 考试日 {project['exam_date']}" if project.get("exam_date") else ""))
        return "学习项目：\n" + "\n".join(lines)

    def _plan(project_id: str) -> str:
        plan = study.active_plan(conn, project_id)
        if plan is None:
            return "这个项目还没有生成计划。"
        units = {unit["id"]: unit for unit in study.list_units(conn, project_id)}
        lines = [(f"当前计划 v{plan['version']}（{plan['horizon_start']} → "
                  f"{plan['horizon_end']}）"), f"说明：{plan['note']}"]
        items = plan["items"]
        for item in items[:30]:
            if item.get("unit_ids"):
                body = "、".join(units[unit_id]["title"] for unit_id in item["unit_ids"]
                                 if unit_id in units)
            else:
                body = "复习 / 练习"
            target = item.get("target") or {}
            extra = ""
            if target:
                extra = " " + " ".join(f"{key}={value}" for key, value in target.items())
            lines.append(f"- {item['date']} {item['kind']}  {body}  "
                         f"（{item.get('minutes', 0)} 分钟）{extra}")
        if len(items) > 30:
            lines.append(f"……还有 {len(items) - 30} 条，请在页面查看。")
        return "\n".join(lines)

    def _units(project_id: str, status: str = "") -> str:
        try:
            units = study.list_units(conn, project_id, status.strip() or None)
        except ValueError as exc:
            return f"查不了：{exc}。"
        if not units:
            return "这个项目还没有单元。先添加资料并提取单元。"
        lines = [f"单元（{len(units)} 个，按顺序）："]
        for unit in units[:50]:
            pages = ""
            if unit.get("page_start"):
                pages = (f" · 第 {unit['page_start']} 页" if unit["page_start"] == unit.get("page_end")
                         else f" · 第 {unit['page_start']}–{unit['page_end']} 页")
            lines.append(f"- [{unit['id']}] {unit['title']} · {unit['status']}"
                         f" · 约 {unit['est_minutes']:g} 分钟{pages}")
        if len(units) > 50:
            lines.append(f"……还有 {len(units) - 50} 个，请在页面查看。")
        return "\n".join(lines)

    def _read_unit(unit_id: str) -> str:
        try:
            data = study.unit_text(conn, unit_id)
        except ValueError as exc:
            return f"读不了：{exc}。"
        unit, document = data["unit"], data["document"]
        pages = ""
        if unit.get("page_start"):
            pages = (f" · 第 {unit['page_start']} 页" if unit["page_start"] == unit.get("page_end")
                     else f" · 第 {unit['page_start']}–{unit['page_end']} 页")
        head = f"《{document['title']}》 · {unit['title']}{pages}"
        tail = "\n\n[单元结束]" if not data["has_more"] else "\n\n[还有更多内容]"
        return head + "\n\n" + data["text"] + tail

    def _create(unit_id: str, count: int = 6) -> str:
        result = study_cards.generate_cards(settings, conn, unit_id, count=count)
        if not result.get("ok"):
            return f"没生成：{result.get('error', '未知错误')}。"
        created = result["created"]
        rejected = result["rejected"]
        head = (f"生成了 {len(created)} 张候选卡"
                f"（拒收 {len(rejected)} 张，重复 {result['duplicates']} 张）。"
                "候选卡要用户在页面上确认后才进入复习。")
        lines = [head]
        for card in created[:8]:
            lines.append(f"- [{card['card_type']}] {card['front']} — {card['back']}"
                         f"（出处：{card['source_quote'][:60]}）")
        if rejected:
            reasons = "；".join(str(item.get("reason", "")) for item in rejected[:3])
            lines.append(f"拒收原因示例：{reasons}")
        if not created:
            lines.append("没有可用的新卡；可以换一个单元或先补充资料。")
        return "\n".join(lines)

    def _extract_vocab(unit_id: str, count: int = 8) -> str:
        unit = study.get_unit(conn, unit_id)
        if unit is None:
            return "这个单元已经不在了。"
        project = study.get_project(conn, unit["project_id"])
        if project is None or project["kind"] != "language":
            return "提取词条只适用于语言项目。"
        result = study_cards.generate_cards(
            settings, conn, unit_id, count=count, card_types=("vocab",))
        if not result.get("ok"):
            return f"没提取成：{result.get('error', '未知错误')}。"
        created = result["created"]
        lines = [(f"从「{unit['title']}」提取了 {len(created)} 个候选词条"
                  f"（拒收 {len(result['rejected'])} 个，重复 {result['duplicates']} 个）。"
                  "确认后才进入词汇调度。")]
        for card in created[:10]:
            lines.append(f"- {card['front']} — {card['back']}")
        if not created:
            lines.append("没有可用词条；换一段课文或先补充资料。")
        return "\n".join(lines)

    def _import_vocab(project_id: str, text: str) -> str:
        try:
            result = study_language.import_words(conn, project_id, text)
        except ValueError as exc:
            return f"没导入：{exc}。"
        lines = [(f"导入完成：新增 {len(result['added'])} 个候选词条，"
                  f"重复 {result['duplicates']} 个，跳过 {len(result['skipped'])} 行。")]
        for card in result["added"][:10]:
            lines.append(f"- {card['front']} — {card['back']}")
        if result.get("error"):
            lines.append(result["error"])
        if result["skipped"]:
            sample = "；".join(f"第 {item.get('line')} 行 {item.get('reason')}"
                               for item in result["skipped"][:3])
            lines.append("跳过的示例：" + sample)
        return "\n".join(lines)

    def _cards(project_id: str, status: str = "", query: str = "",
               limit: int = 20) -> str:
        if study.get_project(conn, project_id) is None:
            return f"没有 id 为 {project_id} 的学习项目。"
        cards = study.list_cards(conn, project_id, status=status.strip() or None,
                                 query=query, limit=max(1, min(int(limit or 20), 50)))
        if not cards:
            what = f"匹配「{query}」的" if query else ""
            return f"没有{what}卡片。"
        lines = [f"卡片（{len(cards)} 张）："]
        for card in cards:
            due = f" · 到期 {card['due']}" if card["due"] else ""
            lines.append(f"- [{card['id']}] {card['card_type']} · {card['status']}"
                         f" · {card['front']} — {card['back']}{due}")
        return "\n".join(lines)

    def _progress(project_id: str) -> str:
        project = _project(conn, project_id)
        if project["kind"] != "language":
            return "语言进度只适用于语言项目。"
        stats = study.language_progress(conn, project_id)
        target = stats.get("target_items")
        lines = [
            f"{project['title']} 语言进度：",
            f"- 活跃词 {stats['vocab_active']}" + (f" / 目标 {target}" if target else ""),
            f"- 成熟词 {stats['vocab_mature']}，未学词 {stats['vocab_unseen']}",
            f"- 到期 {study.project_stats(conn, project_id)['due']} 张",
            "- 近 7 天正确率 "
            + (f"{round(stats['accuracy_7d'] * 100)}%" if stats["accuracy_7d"] is not None
               else "还没有复习记录"),
            f"- 连续学习 {study.project_stats(conn, project_id)['streak']} 天",
        ]
        if stats.get("target_coverage") is not None:
            lines.append(f"- 目标词量覆盖 {round(stats['target_coverage'] * 100)}%")
        practice = stats.get("practice_7d") or {}
        if practice:
            lines.append("- 近 7 天练习：" + "、".join(
                f"{kind} {minutes:g} 分钟" for kind, minutes in practice.items()))
        return "\n".join(lines)

    def _mark(unit_id: str, status: str) -> str:
        try:
            unit = study.update_unit(conn, unit_id, status=status)
        except ValueError as exc:
            return f"没改成：{exc}"
        return f"已把「{unit['title']}」标记为 {unit['status']}。"

    def _replan(project_id: str) -> str:
        try:
            plan = study.replan(conn, project_id)
        except ValueError as exc:
            return f"没重排：{exc}"
        return (f"已生成计划 v{plan['version']}，从明天开始重排；今天的安排没有动。\n"
                f"{plan['note']}")

    def _dictation(audio_id: str, transcript: str = "") -> str:
        audio = study.get_audio(conn, audio_id)
        if audio is None:
            return "没有这段音频。"
        project = study.get_project(conn, audio["project_id"])
        if project is None or project["kind"] != "language":
            return "听写卡只适用于语言项目。"
        try:
            card = study.create_dictation_card(conn, project["id"], audio_id,
                                               transcript=transcript)
        except ValueError as exc:
            return f"没建成：{exc}"
        return (f"已建候选听写卡 [{card['id']}]：{audio['title']}。"
                "用户在页面上确认后才会进入复习。")

    return {
        "study_overview": Tool(
            name="study_overview",
            description="一眼看出所有学习项目的状态：考试倒计时、覆盖率、单元完成度、"
                        "到期复习、语言活跃词与连续天数。**开始聊学习话题时先调它**，"
                        "不要先问一圈用户「你有哪些项目」。",
            input_schema={"type": "object", "properties": {
                "track": {"type": "string",
                          "description": "exam 只看考试，language 只看语言，留空看全部"},
            }},
            fn=_overview,
        ),
        "list_study_projects": Tool(
            name="list_study_projects",
            description="列出学习项目及其 id、状态、考试日期和每日分钟。"
                        "需要具体项目 id 时用；一般先调 study_overview。",
            input_schema={"type": "object", "properties": {
                "track": {"type": "string", "description": "exam / language / 留空"},
            }},
            fn=_projects,
        ),
        "study_plan": Tool(
            name="study_plan",
            description="当前生效的学习计划：版本、日期范围、每天学哪些单元/练习、"
                        "容量说明。日期与覆盖率是代码算的，不要自己心算。",
            input_schema={"type": "object", "properties": {
                "project_id": {"type": "string", "description": "学习项目 id"},
            }, "required": ["project_id"]},
            fn=_plan,
        ),
        "list_units": Tool(
            name="list_units",
            description="按顺序列出项目的学习单元（单元 id、标题、状态、预计时间、页码）。"
                        "要读某一节先在这里拿 id。",
            input_schema={"type": "object", "properties": {
                "project_id": {"type": "string", "description": "学习项目 id"},
                "status": {"type": "string",
                           "description": "new / learning / done，留空看全部"},
            }, "required": ["project_id"]},
            fn=_units,
        ),
        "read_unit": Tool(
            name="read_unit",
            description="读一个单元的真实正文（来自用户资料，带文档标题和页码）。"
                        "只在单元范围内回答；不要凭记忆补充原文没有的内容。",
            input_schema={"type": "object", "properties": {
                "unit_id": {"type": "string", "description": "单元 id"},
            }, "required": ["unit_id"]},
            fn=_read_unit,
        ),
        "create_cards": Tool(
            name="create_cards",
            description="从一个单元生成候选卡片。卡必须带原文引文，服务端会校验引文确实"
                        "在单元里；生成的是 candidate，**不要替用户确认**。考试项目出问答/"
                        "填空，语言项目出词汇/翻译/语法。",
            input_schema={"type": "object", "properties": {
                "unit_id": {"type": "string", "description": "单元 id"},
                "count": {"type": "integer", "description": "想要几张（1–12，默认 6）"},
            }, "required": ["unit_id"]},
            fn=_create,
        ),
        "extract_vocab": Tool(
            name="extract_vocab",
            description="语言项目专用：从一段课文/单元提取候选词条（词、音标、词性、"
                        "释义、例句），必须带原文例句。生成的是候选，确认后才进入调度。",
            input_schema={"type": "object", "properties": {
                "unit_id": {"type": "string", "description": "单元 id"},
                "count": {"type": "integer", "description": "想要几个（默认 8）"},
            }, "required": ["unit_id"]},
            fn=_extract_vocab,
        ),
        "import_vocab": Tool(
            name="import_vocab",
            description="语言项目专用：导入用户粘贴的词表。每行支持 word=释义、"
                        "word<Tab>释义、word  释义；重复词条会跳过。",
            input_schema={"type": "object", "properties": {
                "project_id": {"type": "string", "description": "语言项目 id"},
                "text": {"type": "string", "description": "词表全文"},
            }, "required": ["project_id", "text"]},
            fn=_import_vocab,
        ),
        "list_cards": Tool(
            name="list_cards",
            description="列出某个项目的卡片，可按状态和关键词过滤。用来看候选卡、"
                        "到期卡或用户问「我有哪些卡」时调用。",
            input_schema={"type": "object", "properties": {
                "project_id": {"type": "string", "description": "学习项目 id"},
                "status": {"type": "string",
                           "description": "candidate / active / suspended，留空看全部"},
                "query": {"type": "string", "description": "按正面/背面搜索（可省）"},
                "limit": {"type": "integer", "description": "最多返回多少张（默认 20，上限 50）"},
            }, "required": ["project_id"]},
            fn=_cards,
        ),
        "language_progress": Tool(
            name="language_progress",
            description="语言项目的进度：活跃词、成熟词、未学词、近 7 天正确率、"
                        "目标词量覆盖、练习分钟、连续天数。只描述数据，不推断等级。",
            input_schema={"type": "object", "properties": {
                "project_id": {"type": "string", "description": "语言项目 id"},
            }, "required": ["project_id"]},
            fn=_progress,
        ),
        "mark_unit": Tool(
            name="mark_unit",
            description="把单元标记为 new / learning / done。**只有用户明确说「这一节"
                        "学完了」时才调用**；不要根据聊天内容自己判断。",
            input_schema={"type": "object", "properties": {
                "unit_id": {"type": "string", "description": "单元 id"},
                "status": {"type": "string", "description": "new / learning / done"},
            }, "required": ["unit_id", "status"]},
            fn=_mark,
        ),
        "replan_study": Tool(
            name="replan_study",
            description="从明天开始重排剩余计划：生成新版本，今天的安排不变，旧版本保留。"
                        "只有用户明确要求重排或确认落后时才调用。",
            input_schema={"type": "object", "properties": {
                "project_id": {"type": "string", "description": "学习项目 id"},
            }, "required": ["project_id"]},
            fn=_replan,
        ),
        "create_dictation": Tool(
            name="create_dictation",
            description="语言项目专用：用一段已有音频和它的 transcript 建一张候选听写卡。"
                        "没有 transcript 就不能建听写卡，只能泛听。",
            input_schema={"type": "object", "properties": {
                "audio_id": {"type": "string", "description": "音频 id"},
                "transcript": {"type": "string",
                               "description": "这段音频的原文（音频已带 transcript 时可留空）"},
            }, "required": ["audio_id"]},
            fn=_dictation,
        ),
    }
