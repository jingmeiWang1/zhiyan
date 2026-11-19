"""学习 / 复习 —— 考试备考与语言学习两条轨道。

这个模块只碰 SQLite 和纯函数：不调模型、不发网络请求、不读页面状态。
卡片生成在 ``ops/study_cards.py``，写作批改在 ``ops/study_language.py``。

两条轨道共用同一套「单元 → 计划 → 卡片 → 复习记录」，但计划形状不同：

    exam      到考试日为止，把未完成单元按每天可用分钟装进日历。
    language  每日练习配比（新词 / 复习 / 听力 / 口语 / 阅读 / 写作），
              有考试日时按剩余天数提高新词量。

几条不显然、但改之前必须知道的选择：

1. **表是第一次用到才建的**，和 library 一样，不在 db.py 的 SCHEMA 里。
   已经在用的工作目录不需要迁移，没用过学习功能的工作目录永远不长这些表。

2. **日期是本地日历日。** ``due``、计划日期、复习日期都存 ``YYYY-MM-DD``。
   时间戳仍用 UTC。混用会让东八区下午的复习被记到第二天。

3. **复习记录只追加。** ``study_cards`` 上的 ease/interval/due 是物化结果，
   ``study_reviews`` 才是事实。以后换 FSRS 时可以按日志重算，不必让用户
   把卡片重新学一遍。

4. **计划按版本保存。** 重排不是覆盖，而是把 old active 改成 archived、
   插入 version+1：用户随时能翻回上一版。

5. **排期由代码算，模型只解释。** 日期、容量、覆盖率是确定性的；一句
   "每天 60 分钟，到考试前只能覆盖 68%" 必须来自真实算术，而不是模型印象。

6. **AI 卡片先进候选。** 生成结果 ``status='candidate'``，用户确认后才
   ``active``。没有 source_quote 的卡片允许存在（手动建卡），但 AI 生成路径
   会拒收伪造引文 —— 见 ops/study_cards.py。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from zhiyan.rag.chunk import SEPARATORS, _sections, _split_units, has_content

KINDS: dict[str, str] = {"exam": "考试备考", "language": "语言学习"}
MATERIAL_ROLES: dict[str, str] = {
    "syllabus": "考纲", "textbook": "教材", "notes": "笔记", "wordlist": "词表",
}
CARD_TYPES: dict[str, str] = {
    "qa": "问答", "cloze": "填空", "vocab": "词汇", "dictation": "听写",
    "translation": "翻译", "grammar": "语法",
}
EXAM_CARD_TYPES = frozenset({"qa", "cloze"})
LANGUAGE_CARD_TYPES = frozenset({"vocab", "dictation", "translation", "grammar", "cloze"})

UNIT_STATUS = ("new", "learning", "done")
CARD_STATUS = ("candidate", "active", "suspended")
PROJECT_STATUS = ("active", "paused", "done")
PRACTICE_KINDS = ("vocab", "review", "listening", "speaking", "reading", "writing")

DEFAULT_MIX = {
    "new_words": 15,
    "listening_min": 10,
    "speaking_min": 10,
    "reading_items": 1,
    "writing_sentences": 3,
}

RATINGS = {0: "重来", 3: "困难", 4: "良好", 5: "简单"}

# 单元的上限：一个单元要能在一次学习里读完。超过这个长度的标题节再切。
MAX_UNIT_CHARS = 2400
# PDF 的一页是读者和引用都认的天然单位。目录页常常 2500 字上下，用 2400
# 去切会把一整页点导引剁成几十个碎片；只有超过 PDF_UNIT_MAX 的页才切，
# 切完再打包回 PDF_UNIT_TARGET。
PDF_UNIT_TARGET = 3000
PDF_UNIT_MAX = 6000
# 估算阅读速度：中文约 400 字/分钟。练习系数把「读一遍」变成「学会它」。
READ_CHARS_PER_MIN = 400
PRACTICE_FACTOR = 1.5

AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".ogg", ".webm", ".aac", ".flac"}
MAX_AUDIO_BYTES = 64 * 1024 * 1024

_MAX_TEXT = 400


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def today_iso() -> str:
    """本地日历日。不能用 utcnow()：东八区下午会被算成明天。"""
    return date.today().isoformat()


def _clean_date(value, field: str = "日期", *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{field}不能为空")
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field}必须是 YYYY-MM-DD") from exc


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _clean_text(value, field: str, *, required: bool = False,
                limit: int = _MAX_TEXT) -> str:
    text = " ".join(str(value or "").split())[:limit]
    if required and not text:
        raise ValueError(f"{field}不能为空")
    return text


def _clean_raw(value, field: str, *, required: bool = False,
               limit: int = _MAX_TEXT) -> str:
    """Like _clean_text, but keeps newlines — transcripts and answers use them."""
    text = str(value or "").strip()[:limit]
    if required and not text:
        raise ValueError(f"{field}不能为空")
    return text


def _clean_int(value, field: str, *, low: int, high: int, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是整数") from exc
    if not low <= number <= high:
        raise ValueError(f"{field}必须在 {low}–{high} 之间")
    return number


def _clean_float(value, field: str, *, low: float, high: float, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是数字") from exc
    if not low <= number <= high:
        raise ValueError(f"{field}必须在 {low}–{high:g} 之间")
    return number


def _decode(raw: str, fallback):
    try:
        value = json.loads(raw or "")
        return value if isinstance(value, type(fallback)) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------
# 建表
# --------------------------------------------------------------------------
def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS study_projects (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,                 -- exam / language
        title TEXT NOT NULL,
        subject TEXT DEFAULT '',
        target_language TEXT DEFAULT '',
        level TEXT DEFAULT '',
        start_date TEXT NOT NULL,
        exam_date TEXT,
        daily_minutes INTEGER NOT NULL DEFAULT 60,
        new_cards_per_day INTEGER NOT NULL DEFAULT 20,
        target_items INTEGER,
        settings_json TEXT DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'active',
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS study_materials (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        doc_id TEXT NOT NULL,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(project_id, doc_id)
    );
    CREATE TABLE IF NOT EXISTS study_units (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        doc_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        title TEXT NOT NULL,
        start_offset INTEGER NOT NULL,
        end_offset INTEGER NOT NULL,
        page_start INTEGER,
        page_end INTEGER,
        est_minutes REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        weight REAL NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS study_plans (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        items_json TEXT NOT NULL DEFAULT '[]',
        horizon_start TEXT NOT NULL,
        horizon_end TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS study_cards (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        unit_id TEXT,
        card_type TEXT NOT NULL,
        front TEXT NOT NULL,
        back TEXT NOT NULL,
        extra_json TEXT DEFAULT '{}',
        source_doc_id TEXT DEFAULT '',
        source_offset INTEGER,
        source_quote TEXT DEFAULT '',
        tags TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'candidate',
        ease REAL NOT NULL DEFAULT 2.5,
        interval_days REAL NOT NULL DEFAULT 0,
        reps INTEGER NOT NULL DEFAULT 0,
        lapses INTEGER NOT NULL DEFAULT 0,
        due TEXT,
        last_reviewed TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS study_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_id TEXT NOT NULL,
        reviewed_at TEXT NOT NULL,
        review_date TEXT NOT NULL,
        rating INTEGER NOT NULL,
        score REAL,
        prev_reps INTEGER NOT NULL DEFAULT 0,
        prev_interval REAL NOT NULL DEFAULT 0,
        next_interval REAL NOT NULL DEFAULT 0,
        prev_ease REAL NOT NULL DEFAULT 2.5,
        next_ease REAL NOT NULL DEFAULT 2.5,
        elapsed_ms INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS study_audio (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        title TEXT NOT NULL,
        path TEXT NOT NULL,
        transcript TEXT DEFAULT '',
        duration_sec REAL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS study_sessions (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        date TEXT NOT NULL,
        kind TEXT NOT NULL,
        minutes REAL NOT NULL DEFAULT 0,
        meta_json TEXT DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS study_cards_due_idx
        ON study_cards (project_id, status, due);
    CREATE INDEX IF NOT EXISTS study_cards_type_idx
        ON study_cards (project_id, card_type);
    CREATE INDEX IF NOT EXISTS study_units_order_idx
        ON study_units (project_id, ordinal);
    CREATE INDEX IF NOT EXISTS study_reviews_card_idx
        ON study_reviews (card_id, reviewed_at);
    CREATE INDEX IF NOT EXISTS study_plans_active_idx
        ON study_plans (project_id, status);
    """)
    conn.commit()


# --------------------------------------------------------------------------
# 项目
# --------------------------------------------------------------------------
def _project(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["settings"] = _decode(out.pop("settings_json", "{}"), {})
    return out


def create_project(conn: sqlite3.Connection, *, kind: str, title: str,
                   subject: str = "", target_language: str = "", level: str = "",
                   start_date=None, exam_date=None, daily_minutes=60,
                   new_cards_per_day=20, target_items=None,
                   settings: dict | None = None, note: str = "") -> dict:
    _ensure_table(conn)
    kind = str(kind or "").strip()
    if kind not in KINDS:
        raise ValueError(f"未知的学习类型：{kind or '空'}")
    title = _clean_text(title, "项目名称", required=True, limit=200)
    subject = _clean_text(subject, "科目", limit=100)
    target_language = _clean_text(target_language, "目标语言", limit=40)
    level = _clean_text(level, "水平", limit=40)
    start = _clean_date(start_date, "开始日期") or today_iso()
    exam = _clean_date(exam_date, "考试日期")
    if kind == "exam" and not exam:
        raise ValueError("考试项目需要考试日期")
    if exam and exam < start:
        raise ValueError("考试日期不能早于开始日期")
    daily = _clean_int(daily_minutes, "每日分钟", low=10, high=600, default=60)
    per_day = _clean_int(new_cards_per_day, "每日新卡", low=0, high=200, default=20)
    target = None
    if target_items not in (None, ""):
        target = _clean_int(target_items, "目标词量", low=1, high=100000, default=1)

    config = dict(settings or {})
    if kind == "language":
        mix = dict(DEFAULT_MIX)
        mix.update({k: v for k, v in (config.get("mix") or {}).items() if k in DEFAULT_MIX})
        mix["new_words"] = _clean_int(mix["new_words"], "每日新词", low=0, high=200,
                                      default=DEFAULT_MIX["new_words"])
        for key in ("listening_min", "speaking_min"):
            mix[key] = _clean_int(mix[key], "练习分钟", low=0, high=300,
                                  default=DEFAULT_MIX[key])
        mix["reading_items"] = _clean_int(mix["reading_items"], "阅读篇数", low=0, high=20,
                                          default=DEFAULT_MIX["reading_items"])
        mix["writing_sentences"] = _clean_int(mix["writing_sentences"], "写作句数", low=0,
                                              high=50,
                                              default=DEFAULT_MIX["writing_sentences"])
        config["mix"] = mix
    elif kind == "exam":
        config["review_reserve"] = _clean_float(
            config.get("review_reserve", 0.2), "复习预留比例", low=0.0, high=0.5, default=0.2)

    project_id = _new_id()
    conn.execute(
        """INSERT INTO study_projects
           (id, kind, title, subject, target_language, level, start_date, exam_date,
            daily_minutes, new_cards_per_day, target_items, settings_json, status,
            note, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (project_id, kind, title, subject, target_language, level, start, exam,
         daily, per_day, target, json.dumps(config, ensure_ascii=False), "active",
         _clean_text(note, "备注", limit=2000), _now()),
    )
    conn.commit()
    return get_project(conn, project_id)


def get_project(conn: sqlite3.Connection, project_id: str) -> dict | None:
    _ensure_table(conn)
    row = conn.execute("SELECT * FROM study_projects WHERE id=?", (project_id,)).fetchone()
    return _project(row) if row else None


def list_projects(conn: sqlite3.Connection, *, kind: str | None = None,
                  status: str | None = "active") -> list[dict]:
    _ensure_table(conn)
    where, args = [], []
    if kind:
        where.append("kind=?")
        args.append(kind)
    if status:
        where.append("status=?")
        args.append(status)
    sql = "SELECT * FROM study_projects"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, rowid DESC"
    return [_project(r) for r in conn.execute(sql, args).fetchall()]


def update_project(conn: sqlite3.Connection, project_id: str, **fields) -> dict:
    project = get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    allowed = {"title", "subject", "target_language", "level", "start_date",
               "exam_date", "daily_minutes", "new_cards_per_day", "target_items",
               "status", "note", "settings"}
    updates: dict[str, object] = {}
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "title":
            updates[key] = _clean_text(value, "项目名称", required=True, limit=200)
        elif key in {"subject", "target_language", "level"}:
            updates[key] = _clean_text(value, "字段", limit=100)
        elif key == "start_date":
            updates[key] = _clean_date(value, "开始日期", required=True)
        elif key == "exam_date":
            updates[key] = _clean_date(value, "考试日期")
        elif key == "daily_minutes":
            updates[key] = _clean_int(value, "每日分钟", low=10, high=600, default=60)
        elif key == "new_cards_per_day":
            updates[key] = _clean_int(value, "每日新卡", low=0, high=200, default=20)
        elif key == "target_items":
            updates[key] = _clean_int(value, "目标词量", low=1, high=100000, default=1)
        elif key == "status":
            status = str(value).strip()
            if status not in PROJECT_STATUS:
                raise ValueError("未知的项目状态")
            updates[key] = status
        elif key == "note":
            updates[key] = _clean_text(value, "备注", limit=2000)
        elif key == "settings":
            config = dict(project.get("settings") or {})
            if isinstance(value, dict):
                config.update(value)
            if project["kind"] == "language":
                mix = dict(DEFAULT_MIX)
                mix.update(config.get("mix") or {})
                for name in ("new_words", "listening_min", "speaking_min",
                             "reading_items", "writing_sentences"):
                    mix[name] = _clean_int(mix[name], "语言练习配额", low=0, high=500,
                                           default=DEFAULT_MIX[name])
                config["mix"] = mix
            elif project["kind"] == "exam":
                config["review_reserve"] = _clean_float(
                    config.get("review_reserve", 0.2), "复习预留比例",
                    low=0.0, high=0.5, default=0.2)
            updates["settings_json"] = json.dumps(config, ensure_ascii=False)
    if (updates.get("exam_date") and updates.get("start_date")
            and updates["exam_date"] < updates["start_date"]):
        raise ValueError("考试日期不能早于开始日期")
    if updates:
        sets = ", ".join(f"{key}=?" for key in updates)
        conn.execute(f"UPDATE study_projects SET {sets} WHERE id=?",
                     (*updates.values(), project_id))
        conn.commit()
    return get_project(conn, project_id)


def archive_project(conn: sqlite3.Connection, project_id: str) -> bool:
    _ensure_table(conn)
    cursor = conn.execute(
        "UPDATE study_projects SET status='done' WHERE id=? AND status<>'done'",
        (project_id,))
    conn.commit()
    return bool(cursor.rowcount)


# --------------------------------------------------------------------------
# 资料
# --------------------------------------------------------------------------
def add_material(conn: sqlite3.Connection, project_id: str, doc_id: str,
                 role: str = "textbook") -> dict:
    _ensure_table(conn)
    if get_project(conn, project_id) is None:
        raise ValueError("这个学习项目已经不在了")
    role = str(role or "").strip()
    if role not in MATERIAL_ROLES:
        raise ValueError(f"未知的资料角色：{role or '空'}")
    from zhiyan.applications import library

    if library.get_document(conn, doc_id) is None:
        raise ValueError("文档库里没有这份资料")
    conn.execute(
        """INSERT INTO study_materials (id, project_id, doc_id, role, created_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(project_id, doc_id) DO UPDATE SET role=excluded.role""",
        (_new_id(), project_id, doc_id, role, _now()),
    )
    conn.commit()
    for material in list_materials(conn, project_id):
        if material["doc_id"] == doc_id:
            return material
    raise ValueError("资料没有保存成功")


def remove_material(conn: sqlite3.Connection, project_id: str, doc_id: str) -> bool:
    _ensure_table(conn)
    if conn.execute("SELECT COUNT(*) FROM study_cards WHERE project_id=? AND source_doc_id=?",
                    (project_id, doc_id)).fetchone()[0]:
        raise ValueError("这个项目已经有卡片引用这份资料，先删除相关卡片")
    cursor = conn.execute(
        "DELETE FROM study_materials WHERE project_id=? AND doc_id=?",
        (project_id, doc_id))
    conn.execute(
        "DELETE FROM study_units WHERE project_id=? AND doc_id=?",
        (project_id, doc_id))
    conn.commit()
    return bool(cursor.rowcount)


def list_materials(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    _ensure_table(conn)
    try:
        rows = conn.execute(
            """SELECT m.id, m.project_id, m.doc_id, m.role, m.created_at,
                      d.title, d.kind AS doc_kind, d.chars
               FROM study_materials m JOIN documents d ON d.id=m.doc_id
               WHERE m.project_id=? ORDER BY m.created_at, m.rowid""",
            (project_id,)).fetchall()
    except sqlite3.OperationalError:
        # A brand-new workspace may not have the library's `documents` table
        # yet. That means "no materials", not "the dashboard is broken".
        return []
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# 单元提取
# --------------------------------------------------------------------------
def _est_minutes(chars: int) -> float:
    return max(5.0, round(chars / READ_CHARS_PER_MIN * PRACTICE_FACTOR, 1))


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """把 span 两端的空白去掉，但保留精确的字符位置。"""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _pack_pieces(pieces: list[tuple[int, str]], target: int) -> list[tuple[int, str]]:
    """Pack split pieces back up to `target`, keeping the first piece's offset.

    `_split_units` cuts at every separator; a dot-leader table of contents can
    therefore come back as 38 tiny pieces. Packing them back is what makes the
    result a unit a person can read, not a list of fragments.
    """
    out: list[tuple[int, str]] = []
    buffered: list[str] = []
    start = 0
    size = 0
    for offset, body in pieces:
        if buffered and size + len(body) > target:
            out.append((start, "".join(buffered)))
            buffered, size = [], 0
        if not buffered:
            start = offset
        buffered.append(body)
        size += len(body)
    if buffered:
        out.append((start, "".join(buffered)))
    return out


def _unit(title: str, start: int, end: int, *, page_start=None, page_end=None) -> dict:
    return {
        "title": title or "未命名单元",
        "start_offset": start,
        "end_offset": end,
        "page_start": page_start,
        "page_end": page_end,
        "est_minutes": _est_minutes(end - start),
    }


def _markdown_units(text: str, doc_title: str) -> list[dict]:
    out: list[dict] = []
    for start, end, heading in _sections(text):
        start, end = _trim_span(text, start, end)
        if end <= start or not has_content(text[start:end]):
            continue
        pieces = _pack_pieces(
            _split_units(text[start:end], MAX_UNIT_CHARS, SEPARATORS, base=start),
            MAX_UNIT_CHARS)
        for index, (offset, body) in enumerate(pieces):
            piece_start, piece_end = _trim_span(text, offset, offset + len(body))
            if piece_end <= piece_start or not has_content(text[piece_start:piece_end]):
                continue
            title = heading or f"{doc_title} · 开头"
            if len(pieces) > 1:
                title = f"{title}（{index + 1}/{len(pieces)}）"
            out.append(_unit(title, piece_start, piece_end))
    return out


def _line_units(text: str) -> list[dict]:
    lines = text.splitlines(keepends=True)
    out: list[dict] = []
    offset = 0
    for line in lines:
        body = line.strip()
        if body:
            start = offset + (len(line) - len(line.lstrip()))
            out.append(_unit(body[:60], start, start + len(body)))
        offset += len(line)
    return out


def _looks_like_outline(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 8:
        return False
    short = sum(1 for line in lines if len(line) < 80)
    return short / len(lines) >= 0.9


def _paragraph_units(text: str, doc_title: str) -> list[dict]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in re.finditer(r"\n\s*\n", text):
        spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text)))

    # A single paragraph can be longer than MAX_UNIT_CHARS (a pasted article
    # with no blank lines, for example). Split it with the same recursive
    # splitter the RAG chunker uses, so the unit stays readable and the
    # character offsets stay exact.
    pieces: list[tuple[int, int]] = []
    for start, end in spans:
        start, end = _trim_span(text, start, end)
        if end <= start:
            continue
        if end - start > MAX_UNIT_CHARS:
            for offset, body in _split_units(text[start:end], MAX_UNIT_CHARS,
                                             SEPARATORS, base=start):
                piece_start, piece_end = _trim_span(text, offset, offset + len(body))
                if piece_end > piece_start:
                    pieces.append((piece_start, piece_end))
        else:
            pieces.append((start, end))

    out: list[dict] = []
    group_start = group_end = None
    for start, end in pieces:
        if group_start is None:
            group_start, group_end = start, end
        elif end - group_start <= MAX_UNIT_CHARS:
            group_end = end
        else:
            prefix = text[group_start:group_end].strip().splitlines()[0][:40]
            out.append(_unit(prefix or f"{doc_title} · 第 {len(out) + 1} 段",
                             group_start, group_end))
            group_start, group_end = start, end
    if group_start is not None:
        prefix = text[group_start:group_end].strip().splitlines()[0][:40]
        out.append(_unit(prefix or f"{doc_title} · 第 {len(out) + 1} 段",
                         group_start, group_end))
    return out


def _pdf_units(conn: sqlite3.Connection, home: Path, row: sqlite3.Row) -> tuple[list[dict], list[str]]:
    """按页分组。页码和字符区间必须能对上 ``documents.content``。"""
    from zhiyan.applications.library import file_path
    from zhiyan.applications.reader import parse_pages

    warnings: list[str] = []
    target = file_path(conn, home, row["id"])
    if target is None:
        return _paragraph_units(row["content"], row["title"]), ["原始 PDF 不在了，退化为按段落提取"]
    try:
        pages = parse_pages(row["title"], target.read_bytes())
    except Exception as exc:  # noqa: BLE001 - 解析失败是用户数据问题，不是崩溃点
        return (_paragraph_units(row["content"], row["title"]),
                [f"PDF 重新解析失败（{exc}），退化为按段落提取"])

    parts = [page.strip() for page in pages]
    raw = "\n\n".join(parts)
    lead = len(raw) - len(raw.lstrip())
    text = raw.strip()
    starts: list[int] = []
    cursor = 0
    for page in parts:
        starts.append(max(0, cursor - lead))
        cursor += len(page)
        cursor += 2  # "\n\n"
    if text != row["content"]:
        return (_paragraph_units(row["content"], row["title"]),
                ["重建文本与文档库正文不一致，退化为按段落提取（页码不可用）"])

    out: list[dict] = []
    group: list[int] = []
    group_chars = 0
    for index, page in enumerate(parts):
        if not page:
            continue
        if group and group_chars + len(page) > PDF_UNIT_TARGET:
            out.extend(_page_group(text, parts, starts, group, len(out)))
            group, group_chars = [], 0
        group.append(index)
        group_chars += len(page)
    if group:
        out.extend(_page_group(text, parts, starts, group, len(out)))
    return out, warnings


def _page_group(text: str, parts: list[str], starts: list[int], group: list[int],
                ordinal: int) -> list[dict]:
    first, last = group[0], group[-1]
    start = starts[first]
    end = starts[last] + len(parts[last])
    label = (f"第 {first + 1} 页" if first == last
             else f"第 {first + 1}–{last + 1} 页")
    if end - start <= PDF_UNIT_MAX:
        return [_unit(label, start, end, page_start=first + 1, page_end=last + 1)]
    pieces = _pack_pieces(
        _split_units(text[start:end], PDF_UNIT_MAX, SEPARATORS, base=start),
        PDF_UNIT_TARGET)
    out: list[dict] = []
    for index, (offset, body) in enumerate(pieces):
        piece_start, piece_end = _trim_span(text, offset, offset + len(body))
        if piece_end <= piece_start:
            continue
        out.append(_unit(f"{label}（{index + 1}/{len(pieces)}）", piece_start, piece_end,
                         page_start=first + 1, page_end=last + 1))
    return out


def _units_for_document(conn: sqlite3.Connection, home: Path, row: sqlite3.Row,
                        role: str) -> tuple[list[dict], list[str]]:
    text = row["content"] or ""
    if not text:
        return [], [f"《{row['title']}》没有可提取的文字（扫描件？）"]
    if row["kind"] == "pdf":
        return _pdf_units(conn, home, row)
    if row["kind"] == "markdown":
        return _markdown_units(text, row["title"]), []
    if role == "syllabus" and _looks_like_outline(text):
        return _line_units(text), []
    if _looks_like_outline(text):
        return _line_units(text), []
    return _paragraph_units(text, row["title"]), []


def extract_units(conn: sqlite3.Connection, home: Path, project_id: str) -> dict:
    """从项目资料重建单元。已有卡片时拒绝，避免引用悬空。"""
    _ensure_table(conn)
    project = get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    materials = list_materials(conn, project_id)
    if not materials:
        raise ValueError("先给项目添加资料，再提取单元")
    if conn.execute("SELECT COUNT(*) FROM study_cards WHERE project_id=?",
                    (project_id,)).fetchone()[0]:
        raise ValueError("这个项目已经有卡片了，重建单元会让卡片失去出处；先删除卡片")

    warnings: list[str] = []
    units: list[dict] = []
    for material in materials:
        if material["role"] == "wordlist":
            continue
        row = conn.execute("SELECT * FROM documents WHERE id=?",
                           (material["doc_id"],)).fetchone()
        if row is None:
            warnings.append(f"资料《{material['title']}》已不在文档库")
            continue
        document_units, document_warnings = _units_for_document(conn, home, row, material["role"])
        warnings.extend(document_warnings)
        for unit in document_units:
            unit["doc_id"] = row["id"]
            units.append(unit)

    if not units:
        raise ValueError("这些资料里没有可用的单元")

    conn.execute("DELETE FROM study_units WHERE project_id=?", (project_id,))
    for ordinal, unit in enumerate(units):
        conn.execute(
            """INSERT INTO study_units
               (id, project_id, doc_id, ordinal, title, start_offset, end_offset,
                page_start, page_end, est_minutes, status, weight, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'new', 1, ?)""",
            (_new_id(), project_id, unit["doc_id"], ordinal, unit["title"],
             unit["start_offset"], unit["end_offset"], unit["page_start"],
             unit["page_end"], unit["est_minutes"], _now()),
        )
    conn.commit()
    return {"units": len(units), "warnings": warnings}


def list_units(conn: sqlite3.Connection, project_id: str,
               status: str | None = None) -> list[dict]:
    _ensure_table(conn)
    sql = "SELECT * FROM study_units WHERE project_id=?"
    args: list = [project_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY ordinal"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_unit(conn: sqlite3.Connection, unit_id: str) -> dict | None:
    _ensure_table(conn)
    return _row(conn.execute("SELECT * FROM study_units WHERE id=?", (unit_id,)).fetchone())


def update_unit(conn: sqlite3.Connection, unit_id: str, *, title=None, weight=None,
                status=None) -> dict:
    unit = get_unit(conn, unit_id)
    if unit is None:
        raise ValueError("这个单元已经不在了")
    updates: dict[str, object] = {}
    if title is not None:
        updates["title"] = _clean_text(title, "单元标题", required=True, limit=200)
    if weight is not None:
        updates["weight"] = _clean_float(weight, "权重", low=0.1, high=10, default=1)
    if status is not None:
        if status not in UNIT_STATUS:
            raise ValueError("未知的单元状态")
        updates["status"] = status
    if updates:
        sets = ", ".join(f"{key}=?" for key in updates)
        conn.execute(f"UPDATE study_units SET {sets} WHERE id=?",
                     (*updates.values(), unit_id))
        conn.commit()
    return get_unit(conn, unit_id)


def merge_units(conn: sqlite3.Connection, project_id: str, unit_ids: Iterable[str]) -> dict:
    ids = [str(unit_id) for unit_id in unit_ids]
    if len(ids) < 2:
        raise ValueError("至少选择两个单元才能合并")
    units = [get_unit(conn, unit_id) for unit_id in ids]
    if any(unit is None for unit in units):
        raise ValueError("有单元已经不在了")
    units = sorted(units, key=lambda u: u["ordinal"])
    if len({unit["project_id"] for unit in units}) != 1:
        raise ValueError("只能合并同一个项目里的单元")
    if len({unit["doc_id"] for unit in units}) != 1:
        raise ValueError("只能合并同一份资料里的单元")
    ordinals = [unit["ordinal"] for unit in units]
    if ordinals != list(range(ordinals[0], ordinals[0] + len(ordinals))):
        raise ValueError("只能合并相邻的单元")
    marks = ",".join("?" for _ in ids)
    if conn.execute(f"SELECT COUNT(*) FROM study_cards WHERE unit_id IN ({marks})",
                    ids).fetchone()[0]:
        raise ValueError("这些单元已经有卡片了，先删除卡片再合并")

    first, last = units[0], units[-1]
    title = " + ".join(unit["title"] for unit in units)[:200]
    conn.execute(
        "UPDATE study_units SET title=?, start_offset=?, end_offset=?, page_start=?, "
        "page_end=?, est_minutes=? WHERE id=?",
        (title, first["start_offset"], last["end_offset"], first["page_start"],
         last["page_end"], _est_minutes(last["end_offset"] - first["start_offset"]),
         first["id"]),
    )
    delete_marks = ",".join("?" for _ in ids[1:])
    conn.execute(f"DELETE FROM study_units WHERE id IN ({delete_marks})", ids[1:])
    conn.commit()
    return get_unit(conn, first["id"])


def delete_unit(conn: sqlite3.Connection, unit_id: str) -> bool:
    _ensure_table(conn)
    if conn.execute("SELECT COUNT(*) FROM study_cards WHERE unit_id=?", (unit_id,)).fetchone()[0]:
        raise ValueError("这个单元已经有卡片了，先删除卡片")
    cursor = conn.execute("DELETE FROM study_units WHERE id=?", (unit_id,))
    conn.commit()
    return bool(cursor.rowcount)


def unit_text(conn: sqlite3.Connection, unit_id: str) -> dict:
    """单元正文，走 library.text_window 的同一套窗口语义。"""
    from zhiyan.applications import library

    unit = get_unit(conn, unit_id)
    if unit is None:
        raise ValueError("这个单元已经不在了")
    row = library.get_document(conn, unit["doc_id"])
    if row is None:
        raise ValueError("这个单元所属的资料已经不在了")
    window = library.text_window(
        row["content"][unit["start_offset"]:unit["end_offset"]], 0, 4000)
    return {"unit": unit, "document": {"id": row["id"], "title": row["title"],
                                       "kind": row["kind"]},
            "text": window["text"], "chars": window["chars"],
            "has_more": window["has_more"]}


# --------------------------------------------------------------------------
# 计划
# --------------------------------------------------------------------------
def _plan(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["items"] = _decode(out.pop("items_json", "[]"), [])
    return out


def active_plan(conn: sqlite3.Connection, project_id: str) -> dict | None:
    _ensure_table(conn)
    row = conn.execute(
        "SELECT * FROM study_plans WHERE project_id=? AND status='active' "
        "ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()
    return _plan(row) if row else None


def list_plans(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT id, project_id, version, horizon_start, horizon_end, status, note, "
        "created_at FROM study_plans WHERE project_id=? ORDER BY version DESC",
        (project_id,)).fetchall()
    return [dict(r) for r in rows]


def save_plan(conn: sqlite3.Connection, project_id: str, items: list[dict], *,
              horizon_start: str, horizon_end: str, note: str = "") -> dict:
    _ensure_table(conn)
    if get_project(conn, project_id) is None:
        raise ValueError("这个学习项目已经不在了")
    conn.execute(
        "UPDATE study_plans SET status='archived' WHERE project_id=? AND status='active'",
        (project_id,))
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM study_plans WHERE project_id=?",
        (project_id,)).fetchone()[0]
    plan_id = _new_id()
    conn.execute(
        """INSERT INTO study_plans
           (id, project_id, version, items_json, horizon_start, horizon_end,
            status, note, created_at)
           VALUES (?,?,?,?,?,?, 'active', ?, ?)""",
        (plan_id, project_id, version, json.dumps(items, ensure_ascii=False),
         horizon_start, horizon_end, _clean_text(note, "计划说明", limit=1000), _now()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM study_plans WHERE id=?", (plan_id,)).fetchone()
    return _plan(row)


def _unit_minutes(unit: dict) -> float:
    return max(5.0, round(float(unit["est_minutes"]) * float(unit["weight"] or 1), 1))


def _exam_requirements(project: dict, units: list[dict],
                       start: str | None = None) -> dict:
    daily = int(project["daily_minutes"])
    today = start or today_iso()
    exam = project.get("exam_date")
    days = 1
    if exam:
        days = max(1, (date.fromisoformat(exam) - date.fromisoformat(today)).days + 1)
    reserve_ratio = float((project.get("settings") or {}).get("review_reserve", 0.2))
    reserve = max(1, round(days * reserve_ratio)) if days > 2 else 0
    learn_days = max(1, days - reserve)
    required = sum(_unit_minutes(unit) for unit in units)
    capacity = learn_days * daily
    coverage = 1.0 if not required else min(1.0, capacity / required)
    return {"days": days, "reserve": reserve, "learn_days": learn_days,
            "required": required, "capacity": capacity, "coverage": coverage,
            "daily": daily}


def _exam_items(project: dict, units: list[dict], start: str) -> tuple[list[dict], dict]:
    req = _exam_requirements(project, units, start)
    daily = req["daily"]
    items: list[dict] = []
    index = 0
    scheduled = 0.0
    for offset in range(req["learn_days"]):
        day = _shift(start, offset)
        budget = daily
        chosen: list[str] = []
        chosen_minutes = 0.0
        while index < len(units):
            minutes = _unit_minutes(units[index])
            if chosen and minutes > budget:
                break
            chosen.append(units[index]["id"])
            chosen_minutes += minutes
            budget -= minutes
            scheduled += minutes
            index += 1
        if chosen:
            items.append({"date": day, "kind": "learn", "unit_ids": chosen,
                          "minutes": round(chosen_minutes, 1)})
    for offset in range(req["learn_days"], req["days"]):
        items.append({"date": _shift(start, offset), "kind": "review", "unit_ids": [],
                      "minutes": daily})
    coverage = 1.0 if not req["required"] else min(1.0, scheduled / req["required"])
    note = (f"每天 {daily} 分钟；预计需要 {req['required']:.0f} 分钟，"
            f"可用 {req['capacity']} 分钟。")
    if coverage < 0.999:
        note += f" 按当前节奏可覆盖约 {coverage * 100:.0f}% 的内容。"
    else:
        note += " 当前容量足够覆盖全部单元。"
    return items, {"coverage": coverage, "note": note}


def build_exam_plan(conn: sqlite3.Connection, project_id: str, *,
                    start: str | None = None) -> dict:
    project = get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    if project["kind"] != "exam":
        raise ValueError("这不是考试项目")
    if not project.get("exam_date"):
        raise ValueError("考试项目需要考试日期")
    start = _clean_date(start, "开始日期") or today_iso()
    units = [unit for unit in list_units(conn, project_id) if unit["status"] != "done"]
    if not units:
        raise ValueError("没有待学习的单元；先添加资料并提取单元")
    items, meta = _exam_items(project, units, start)
    horizon_end = _shift(start, max(0, (date.fromisoformat(project["exam_date"])
                                       - date.fromisoformat(start)).days))
    return save_plan(conn, project_id, items, horizon_start=start,
                     horizon_end=horizon_end, note=meta["note"])


def build_language_plan(conn: sqlite3.Connection, project_id: str, *,
                        start: str | None = None, horizon_days: int = 14) -> dict:
    project = get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    if project["kind"] != "language":
        raise ValueError("这不是语言项目")
    start = _clean_date(start, "开始日期") or today_iso()
    mix = dict(DEFAULT_MIX)
    mix.update((project.get("settings") or {}).get("mix") or {})
    cap = int(project["new_cards_per_day"] or 0)
    daily = int(project["daily_minutes"])

    new_target = max(0, int(mix["new_words"]))
    coverage = 1.0
    exam_days = None
    if project.get("exam_date"):
        exam_days = max(1, (date.fromisoformat(project["exam_date"])
                            - date.fromisoformat(start)).days + 1)
        unseen = conn.execute(
            "SELECT COUNT(*) FROM study_cards WHERE project_id=? AND card_type='vocab' "
            "AND status='active' AND reps=0", (project_id,)).fetchone()[0]
        if unseen:
            needed = math.ceil(unseen / exam_days)
            new_target = max(new_target, needed)
            if cap:
                new_target = min(new_target, cap)
            coverage = min(1.0, (new_target * exam_days) / unseen)
    if cap:
        new_target = min(new_target, cap)
    # A language plan is a daily template, not a precise far-future schedule:
    # keep the generated window at two weeks even when the exam is months out.
    horizon = min(exam_days, 14) if exam_days else horizon_days
    horizon = max(1, horizon)

    items: list[dict] = []
    for offset in range(horizon):
        day = _shift(start, offset)
        vocab_minutes = max(5.0, new_target * 0.5) if new_target else 0.0
        review_minutes = max(10.0, daily * 0.3)
        fixed = vocab_minutes + review_minutes
        practice = {
            "listening": float(mix["listening_min"]),
            "speaking": float(mix["speaking_min"]),
            "reading": max(5.0, mix["reading_items"] * 15.0),
            "writing": max(5.0, mix["writing_sentences"] * 3.0),
        }
        if not any(practice.values()):
            practice = {}
        available = max(0.0, daily - fixed)
        total_practice = sum(practice.values())
        if total_practice > available and total_practice:
            scale = available / total_practice
            practice = {key: round(value * scale, 1) for key, value in practice.items()}
        base = [{"date": day, "kind": "vocab_new", "minutes": round(vocab_minutes, 1),
                 "target": {"new_words": new_target}},
                {"date": day, "kind": "review", "minutes": round(review_minutes, 1),
                 "target": {}}]
        if practice.get("listening"):
            base.append({"date": day, "kind": "listening", "minutes": practice["listening"],
                         "target": {"minutes": practice["listening"]}})
        if practice.get("speaking"):
            base.append({"date": day, "kind": "speaking", "minutes": practice["speaking"],
                         "target": {"minutes": practice["speaking"]}})
        if practice.get("reading"):
            base.append({"date": day, "kind": "reading", "minutes": practice["reading"],
                         "target": {"items": mix["reading_items"]}})
        if practice.get("writing"):
            base.append({"date": day, "kind": "writing", "minutes": practice["writing"],
                         "target": {"sentences": mix["writing_sentences"]}})
        items.extend(base)

    note = f"每日新词 {new_target} 个；每天约 {daily} 分钟。"
    if exam_days:
        note += f" 距考试 {exam_days} 天。"
        if coverage < 0.999:
            note += f" 按当前上限，考试前可覆盖约 {coverage * 100:.0f}% 的未学词。"
        else:
            note += " 按当前上限可以覆盖全部未学词。"
    return save_plan(conn, project_id, items, horizon_start=start,
                     horizon_end=_shift(start, horizon - 1), note=note)


def build_plan(conn: sqlite3.Connection, project_id: str) -> dict:
    project = get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    if project["kind"] == "exam":
        return build_exam_plan(conn, project_id)
    return build_language_plan(conn, project_id)


def replan(conn: sqlite3.Connection, project_id: str) -> dict:
    """从明天开始重排；今天正在执行的条目原样保留。"""
    project = get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    current = active_plan(conn, project_id)
    today = today_iso()
    keep = [item for item in (current["items"] if current else []) if item.get("date") == today]
    tomorrow = _shift(today, 1)

    if project["kind"] == "exam":
        units = [unit for unit in list_units(conn, project_id) if unit["status"] != "done"]
        if not units:
            raise ValueError("没有待学习的单元")
        items, meta = _exam_items(project, units, tomorrow)
        note = meta["note"] + " 本次重排从明天开始。"
        plan = save_plan(conn, project_id, keep + items, horizon_start=today,
                         horizon_end=_shift(tomorrow, max(0, (date.fromisoformat(
                             project["exam_date"]) - date.fromisoformat(tomorrow)).days)),
                         note=note)
    else:
        tomorrow_plan = build_language_plan(conn, project_id, start=tomorrow)
        items = tomorrow_plan["items"]
        plan = save_plan(conn, project_id, keep + items, horizon_start=today,
                         horizon_end=tomorrow_plan["horizon_end"],
                         note=tomorrow_plan["note"] + " 本次重排从明天开始。")
    return plan


def plan_items_for(conn: sqlite3.Connection, project_id: str, day: str) -> list[dict]:
    plan = active_plan(conn, project_id)
    if not plan:
        return []
    units = {unit["id"]: unit for unit in list_units(conn, project_id)}
    out: list[dict] = []
    for item in plan["items"]:
        if item.get("date") != day:
            continue
        copy = dict(item)
        copy["units"] = [units[unit_id] for unit_id in item.get("unit_ids", [])
                         if unit_id in units]
        out.append(copy)
    return out


# --------------------------------------------------------------------------
# 卡片
# --------------------------------------------------------------------------
def _card(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["extra"] = _decode(out.pop("extra_json", "{}"), {})
    return out


def _normalize_term(text: str) -> str:
    return " ".join((text or "").lower().split())


def _escape_like(term: str) -> str:
    """`%` and `_` are LIKE wildcards; a user typing them means the character."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def existing_terms(conn: sqlite3.Connection, project_id: str) -> set[str]:
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT front FROM study_cards WHERE project_id=?", (project_id,)).fetchall()
    return {_normalize_term(row["front"]) for row in rows}


def add_card(conn: sqlite3.Connection, project_id: str, *, card_type: str, front: str,
             back: str, unit_id: str | None = None, source_doc_id: str = "",
             source_offset: int | None = None, source_quote: str = "",
             extra: dict | None = None, tags: str = "",
             status: str = "candidate") -> dict | None:
    _ensure_table(conn)
    project = get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    card_type = str(card_type or "").strip()
    if card_type not in CARD_TYPES:
        raise ValueError(f"未知的卡片类型：{card_type or '空'}")
    if project["kind"] == "exam" and card_type not in EXAM_CARD_TYPES:
        raise ValueError("考试项目只能使用问答或填空卡片")
    if project["kind"] == "language" and card_type not in LANGUAGE_CARD_TYPES:
        raise ValueError("语言项目只能使用词汇、听写、翻译、语法或填空卡片")
    if status not in CARD_STATUS:
        raise ValueError("未知的卡片状态")
    front = _clean_text(front, "卡片正面", required=True, limit=500)
    back = _clean_raw(back, "卡片背面", required=True, limit=1000)
    # Every dictation card has the same prompt ("请听写这段音频"), so its
    # identity is the audio, not the front text. Dedupe those separately.
    if card_type != "dictation" and _normalize_term(front) in existing_terms(conn, project_id):
        return None
    card_id = _new_id()
    conn.execute(
        """INSERT INTO study_cards
           (id, project_id, unit_id, card_type, front, back, extra_json,
            source_doc_id, source_offset, source_quote, tags, status,
            ease, interval_days, reps, lapses, due, last_reviewed, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 2.5, 0, 0, 0, ?,
                   NULL, ?)""",
        (card_id, project_id, unit_id, card_type, front, back,
         json.dumps(extra or {}, ensure_ascii=False), source_doc_id, source_offset,
         _clean_raw(source_quote, "原文引文", limit=1000), _clean_text(tags, "标签", limit=200),
         status, today_iso() if status == "active" else None, _now()),
    )
    conn.commit()
    return get_card(conn, card_id)


def get_card(conn: sqlite3.Connection, card_id: str) -> dict | None:
    _ensure_table(conn)
    row = conn.execute("SELECT * FROM study_cards WHERE id=?", (card_id,)).fetchone()
    return _card(row) if row else None


def list_cards(conn: sqlite3.Connection, project_id: str, *, status: str | None = None,
               card_type: str | None = None, query: str = "", limit: int = 500) -> list[dict]:
    _ensure_table(conn)
    where, args = ["project_id=?"], [project_id]
    if status:
        where.append("status=?")
        args.append(status)
    if card_type:
        where.append("card_type=?")
        args.append(card_type)
    if query.strip():
        where.append("(front LIKE ? ESCAPE '\\' OR back LIKE ? ESCAPE '\\')")
        like = f"%{_escape_like(query.strip())}%"
        args.extend([like, like])
    args.append(max(1, min(int(limit), 2000)))
    rows = conn.execute(
        f"SELECT * FROM study_cards WHERE {' AND '.join(where)} "
        "ORDER BY created_at, rowid LIMIT ?", args).fetchall()
    return [_card(row) for row in rows]


def update_card(conn: sqlite3.Connection, card_id: str, *, front=None, back=None,
                extra=None, tags=None) -> dict:
    card = get_card(conn, card_id)
    if card is None:
        raise ValueError("这张卡片已经不在了")
    updates: dict[str, object] = {}
    if front is not None:
        updates["front"] = _clean_text(front, "卡片正面", required=True, limit=500)
    if back is not None:
        updates["back"] = _clean_text(back, "卡片背面", required=True, limit=1000)
    if extra is not None:
        updates["extra_json"] = json.dumps(extra, ensure_ascii=False)
    if tags is not None:
        updates["tags"] = _clean_text(tags, "标签", limit=200)
    if updates:
        sets = ", ".join(f"{key}=?" for key in updates)
        conn.execute(f"UPDATE study_cards SET {sets} WHERE id=?",
                     (*updates.values(), card_id))
        conn.commit()
    return get_card(conn, card_id)


def accept_cards(conn: sqlite3.Connection, card_ids: Iterable[str]) -> int:
    _ensure_table(conn)
    ids = [str(card_id) for card_id in card_ids]
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    cursor = conn.execute(
        f"UPDATE study_cards SET status='active', due=COALESCE(due, ?) "
        f"WHERE id IN ({marks}) AND status='candidate'",
        (today_iso(), *ids))
    conn.commit()
    return int(cursor.rowcount)


def set_card_status(conn: sqlite3.Connection, card_id: str, status: str) -> bool:
    if status not in CARD_STATUS:
        raise ValueError("未知的卡片状态")
    if status == "active":
        # Activating a suspended card must also give it a due date; otherwise
        # it is active but can never enter the queue.
        cursor = conn.execute(
            "UPDATE study_cards SET status='active', due=COALESCE(due, ?) WHERE id=?",
            (today_iso(), card_id))
    else:
        cursor = conn.execute("UPDATE study_cards SET status=? WHERE id=?", (status, card_id))
    conn.commit()
    return bool(cursor.rowcount)


def delete_card(conn: sqlite3.Connection, card_id: str) -> bool:
    _ensure_table(conn)
    conn.execute("DELETE FROM study_reviews WHERE card_id=?", (card_id,))
    cursor = conn.execute("DELETE FROM study_cards WHERE id=?", (card_id,))
    conn.commit()
    return bool(cursor.rowcount)


# --------------------------------------------------------------------------
# 复习调度
# --------------------------------------------------------------------------
def sm2(ease: float, interval: float, reps: int, lapses: int,
        rating: int) -> dict:
    """SM-2 的一步。评分只有四档：0 重来，3 困难，4 良好，5 简单。"""
    if rating not in RATINGS:
        raise ValueError("评分必须是重来/困难/良好/简单之一")
    ease = max(1.3, float(ease) + 0.1 - (5 - rating) * (0.08 + (5 - rating) * 0.02))
    if rating < 3:
        lapses += 1
        reps = 0
        interval = 1.0
    else:
        if reps == 0:
            interval = 1.0
        elif reps == 1:
            interval = 6.0
        else:
            interval = max(1.0, round(float(interval) * ease, 1))
        reps += 1
    return {"ease": round(ease, 3), "interval_days": interval, "reps": reps,
            "lapses": lapses}


def due_cards(conn: sqlite3.Connection, project_id: str, *,
              day: str | None = None, include_new: bool = True,
              card_types: Iterable[str] | None = None) -> list[dict]:
    _ensure_table(conn)
    day = day or today_iso()
    where = ["project_id=?", "status='active'", "due IS NOT NULL", "due<=?"]
    args: list = [project_id, day]
    if card_types:
        types = list(card_types)
        where.append(f"card_type IN ({','.join('?' for _ in types)})")
        args.extend(types)
    if not include_new:
        where.append("reps>0")
    rows = conn.execute(
        f"SELECT * FROM study_cards WHERE {' AND '.join(where)} "
        "ORDER BY due, created_at, rowid", args).fetchall()
    return [_card(row) for row in rows]


def new_cards_remaining(conn: sqlite3.Connection, project_id: str,
                        day: str | None = None) -> int:
    project = get_project(conn, project_id)
    if project is None:
        return 0
    day = day or today_iso()
    introduced = conn.execute(
        "SELECT COUNT(*) FROM study_reviews r JOIN study_cards c ON c.id=r.card_id "
        "WHERE c.project_id=? AND r.review_date=? AND r.prev_reps=0",
        (project_id, day)).fetchone()[0]
    return max(0, int(project["new_cards_per_day"] or 0) - introduced)


def review_queue(conn: sqlite3.Connection, project_id: str, *,
                 day: str | None = None) -> dict:
    day = day or today_iso()
    due = [card for card in due_cards(conn, project_id, day=day, include_new=False)]
    new = [card for card in due_cards(conn, project_id, day=day, include_new=True)
           if card["reps"] == 0]
    allowance = new_cards_remaining(conn, project_id, day)
    new = new[:allowance]
    return {"due": due, "new": new, "new_remaining": allowance,
            "total": len(due) + len(new)}


def grade_card(conn: sqlite3.Connection, card_id: str, rating: int, *,
               day: str | None = None, elapsed_ms: int = 0,
               score: float | None = None) -> dict:
    card = get_card(conn, card_id)
    if card is None:
        raise ValueError("这张卡片已经不在了")
    if card["status"] != "active":
        raise ValueError("只能复习已激活的卡片")
    day = day or today_iso()
    result = sm2(card["ease"], card["interval_days"], card["reps"], card["lapses"],
                 int(rating))
    due = _shift(day, max(1, round(result["interval_days"])))
    conn.execute(
        """UPDATE study_cards SET ease=?, interval_days=?, reps=?, lapses=?,
           due=?, last_reviewed=? WHERE id=?""",
        (result["ease"], result["interval_days"], result["reps"], result["lapses"],
         due, _now(), card_id))
    conn.execute(
        """INSERT INTO study_reviews
           (card_id, reviewed_at, review_date, rating, score, prev_reps,
            prev_interval, next_interval, prev_ease, next_ease, elapsed_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (card_id, _now(), day, int(rating), score, card["reps"],
         card["interval_days"], result["interval_days"], card["ease"],
         result["ease"], max(0, int(elapsed_ms))),
    )
    conn.commit()
    return {"card": get_card(conn, card_id), "due": due, **result}


def review_log(conn: sqlite3.Connection, card_id: str) -> list[dict]:
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT * FROM study_reviews WHERE card_id=? ORDER BY id", (card_id,)).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# 今日队列 / 进度
# --------------------------------------------------------------------------
def today_agenda(conn: sqlite3.Connection, project_id: str,
                 day: str | None = None) -> dict:
    day = day or today_iso()
    project = get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    items = plan_items_for(conn, project_id, day)
    queue = review_queue(conn, project_id, day=day)
    units = list_units(conn, project_id)
    done_today = conn.execute(
        "SELECT COUNT(*) FROM study_reviews r JOIN study_cards c ON c.id=r.card_id "
        "WHERE c.project_id=? AND r.review_date=?", (project_id, day)).fetchone()[0]
    return {
        "date": day,
        "project": project,
        "items": items,
        "due": queue["due"],
        "new": queue["new"],
        "new_remaining": queue["new_remaining"],
        "units_total": len(units),
        "units_done": sum(1 for unit in units if unit["status"] == "done"),
        "reviews_today": done_today,
        "sessions": sessions_for(conn, project_id, day),
    }


def sessions_for(conn: sqlite3.Connection, project_id: str, day: str) -> list[dict]:
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT * FROM study_sessions WHERE project_id=? AND date=? ORDER BY created_at",
        (project_id, day)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["meta"] = _decode(item.pop("meta_json", "{}"), {})
        out.append(item)
    return out


def log_session(conn: sqlite3.Connection, project_id: str, *, kind: str,
                minutes: float, day: str | None = None,
                meta: dict | None = None) -> dict:
    _ensure_table(conn)
    if get_project(conn, project_id) is None:
        raise ValueError("这个学习项目已经不在了")
    kind = str(kind or "").strip()
    if kind not in PRACTICE_KINDS:
        raise ValueError(f"未知的练习类型：{kind or '空'}")
    minutes = _clean_float(minutes, "练习分钟", low=0, high=1440, default=0)
    session_id = _new_id()
    conn.execute(
        """INSERT INTO study_sessions
           (id, project_id, date, kind, minutes, meta_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (session_id, project_id, _clean_date(day, "日期") or today_iso(), kind,
         minutes, json.dumps(meta or {}, ensure_ascii=False), _now()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM study_sessions WHERE id=?", (session_id,)).fetchone()
    out = dict(row)
    out["meta"] = _decode(out.pop("meta_json", "{}"), {})
    return out


def _active_days(conn: sqlite3.Connection, project_id: str, days: int = 90) -> set[str]:
    reviewed = conn.execute(
        "SELECT DISTINCT r.review_date FROM study_reviews r "
        "JOIN study_cards c ON c.id=r.card_id WHERE c.project_id=?",
        (project_id,)).fetchall()
    sessions = conn.execute(
        "SELECT DISTINCT date FROM study_sessions WHERE project_id=?",
        (project_id,)).fetchall()
    return ({row[0] for row in reviewed} | {row[0] for row in sessions}) - {None}


def _streak(days: set[str], today: str | None = None) -> int:
    if not days:
        return 0
    cursor = date.fromisoformat(today) if today else date.today()
    if cursor.isoformat() not in days:
        cursor -= timedelta(days=1)
    count = 0
    while cursor.isoformat() in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def project_stats(conn: sqlite3.Connection, project_id: str,
                  day: str | None = None) -> dict:
    _ensure_table(conn)
    day = day or today_iso()
    project = get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    units = list_units(conn, project_id)
    card_rows = conn.execute(
        "SELECT status, card_type, COUNT(*) AS n, "
        "SUM(CASE WHEN reps>0 THEN 1 ELSE 0 END) AS started, "
        "SUM(CASE WHEN interval_days>=21 THEN 1 ELSE 0 END) AS mature "
        "FROM study_cards WHERE project_id=? GROUP BY status, card_type",
        (project_id,)).fetchall()
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for row in card_rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + row["n"]
        by_type[row["card_type"]] = by_type.get(row["card_type"], 0) + row["n"]
    due = conn.execute(
        "SELECT COUNT(*) FROM study_cards WHERE project_id=? AND status='active' "
        "AND due IS NOT NULL AND due<=?", (project_id, day)).fetchone()[0]
    reviews = conn.execute(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN rating>=3 THEN 1 ELSE 0 END) AS good "
        "FROM study_reviews r JOIN study_cards c ON c.id=r.card_id "
        "WHERE c.project_id=? AND r.review_date>=?",
        (project_id, _shift(day, -6))).fetchone()
    total = reviews["n"] or 0
    accuracy = round((reviews["good"] or 0) / total, 3) if total else None
    active_days = _active_days(conn, project_id)
    stats = {
        "project": project,
        "units_total": len(units),
        "units_done": sum(1 for unit in units if unit["status"] == "done"),
        "cards_total": sum(by_status.values()),
        "cards_by_status": by_status,
        "cards_by_type": by_type,
        "due": due,
        "reviews_7d": total,
        "accuracy_7d": accuracy,
        "streak": _streak(active_days, day),
        "active_days": len(active_days),
    }
    if project.get("exam_date"):
        stats["exam_days"] = max(0, (date.fromisoformat(project["exam_date"])
                                     - date.fromisoformat(day)).days)
    if project["kind"] == "exam":
        pending = [unit for unit in units if unit["status"] != "done"]
        req = _exam_requirements(project, pending, day)
        stats["coverage"] = req["coverage"]
    else:
        stats.update(language_progress(conn, project_id, day=day))
    return stats


def language_progress(conn: sqlite3.Connection, project_id: str, *,
                      day: str | None = None) -> dict:
    _ensure_table(conn)
    day = day or today_iso()
    project = get_project(conn, project_id)
    if project is None:
        raise ValueError("这个学习项目已经不在了")
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN reps>0 THEN 1 ELSE 0 END) AS active, "
        "SUM(CASE WHEN interval_days>=21 THEN 1 ELSE 0 END) AS mature, "
        "SUM(CASE WHEN reps=0 THEN 1 ELSE 0 END) AS unseen "
        "FROM study_cards WHERE project_id=? AND card_type='vocab' AND status='active'",
        (project_id,)).fetchone()
    total = row["total"] or 0
    active = row["active"] or 0
    mature = row["mature"] or 0
    unseen = row["unseen"] or 0
    recent = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN rating>=3 THEN 1 ELSE 0 END) AS good "
        "FROM study_reviews r JOIN study_cards c ON c.id=r.card_id "
        "WHERE c.project_id=? AND r.review_date>=?",
        (project_id, _shift(day, -6))).fetchone()
    minutes = conn.execute(
        "SELECT kind, COALESCE(SUM(minutes), 0) AS m FROM study_sessions "
        "WHERE project_id=? AND date>=? GROUP BY kind",
        (project_id, _shift(day, -6))).fetchall()
    practice = {row["kind"]: round(row["m"], 1) for row in minutes}
    target = project.get("target_items")
    coverage = (round(active / target, 3) if target else None)
    return {
        "vocab_total": total,
        "vocab_active": active,
        "vocab_mature": mature,
        "vocab_unseen": unseen,
        "target_items": target,
        "target_coverage": coverage,
        "reviews_7d": recent["n"] or 0,
        "accuracy_7d": (round((recent["good"] or 0) / recent["n"], 3)
                        if recent["n"] else None),
        "practice_7d": practice,
    }


def overview(conn: sqlite3.Connection, day: str | None = None,
             *, limit: int = 20) -> dict:
    """给 Web 的摘要：项目、今日议程、到期与进度。不含卡片正文。"""
    _ensure_table(conn)
    day = day or today_iso()
    projects = list_projects(conn, status=None)[:limit]
    out = []
    for project in projects:
        try:
            stats = project_stats(conn, project["id"], day)
            agenda = today_agenda(conn, project["id"], day)
        except ValueError:
            continue
        out.append({
            "project": project,
            "stats": stats,
            "today": {
                "items": agenda["items"],
                "due_count": len(agenda["due"]),
                "new_count": len(agenda["new"]),
                "reviews_today": agenda["reviews_today"],
                "sessions": agenda["sessions"],
            },
            "materials": list_materials(conn, project["id"]),
        })
    return {"date": day, "projects": out,
            "kinds": [{"key": key, "label": label} for key, label in KINDS.items()],
            "material_roles": [{"key": key, "label": label}
                               for key, label in MATERIAL_ROLES.items()],
            "card_types": [{"key": key, "label": label}
                           for key, label in CARD_TYPES.items()]}


# --------------------------------------------------------------------------
# 音频
# --------------------------------------------------------------------------
def _audio(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def save_audio(conn: sqlite3.Connection, home: Path, project_id: str, *,
               name: str, raw: bytes, transcript: str = "") -> dict:
    _ensure_table(conn)
    if get_project(conn, project_id) is None:
        raise ValueError("这个学习项目已经不在了")
    if not raw:
        raise ValueError("音频是空的")
    if len(raw) > MAX_AUDIO_BYTES:
        raise ValueError(f"音频太大（最多 {MAX_AUDIO_BYTES // 1024 // 1024} MB）")
    suffix = Path(str(name or "")).suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ValueError("只支持 mp3 / m4a / wav / ogg / webm / aac / flac")
    folder = home / "study" / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    audio_id = _new_id()
    relative = Path("study") / "audio" / f"{audio_id}{suffix}"
    (home / relative).write_bytes(raw)
    conn.execute(
        """INSERT INTO study_audio (id, project_id, title, path, transcript,
           duration_sec, created_at) VALUES (?,?,?,?,?, NULL, ?)""",
        (audio_id, project_id, _clean_text(Path(name).stem or "未命名音频", "音频标题",
                                          limit=200),
         str(relative).replace("\\", "/"), _clean_raw(transcript, "原文", limit=10000),
         _now()),
    )
    conn.commit()
    return get_audio(conn, audio_id)


def get_audio(conn: sqlite3.Connection, audio_id: str) -> dict | None:
    _ensure_table(conn)
    return _audio(conn.execute("SELECT * FROM study_audio WHERE id=?",
                               (audio_id,)).fetchone())


def list_audio(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT * FROM study_audio WHERE project_id=? ORDER BY created_at DESC",
        (project_id,)).fetchall()
    return [dict(row) for row in rows]


def update_audio(conn: sqlite3.Connection, audio_id: str, *, title=None,
                 transcript=None) -> dict:
    audio = get_audio(conn, audio_id)
    if audio is None:
        raise ValueError("这段音频已经不在了")
    updates: dict[str, object] = {}
    if title is not None:
        updates["title"] = _clean_text(title, "音频标题", required=True, limit=200)
    if transcript is not None:
        updates["transcript"] = _clean_raw(transcript, "原文", limit=10000)
    if updates:
        sets = ", ".join(f"{key}=?" for key in updates)
        conn.execute(f"UPDATE study_audio SET {sets} WHERE id=?",
                     (*updates.values(), audio_id))
        conn.commit()
    return get_audio(conn, audio_id)


def audio_file_path(conn: sqlite3.Connection, home: Path, audio_id: str) -> Path | None:
    audio = get_audio(conn, audio_id)
    if audio is None:
        return None
    target = (home / audio["path"]).resolve()
    if home.resolve() not in target.parents or not target.is_file():
        return None
    return target


def delete_audio(conn: sqlite3.Connection, home: Path, audio_id: str) -> bool:
    audio = get_audio(conn, audio_id)
    if audio is None:
        return False
    if conn.execute(
            "SELECT COUNT(*) FROM study_cards WHERE project_id=? AND extra_json LIKE ?",
            (audio["project_id"], f'%{audio_id}%')).fetchone()[0]:
        raise ValueError("已经有听写卡引用这段音频，先删除卡片")
    target = (home / audio["path"]).resolve()
    if home.resolve() in target.parents:
        target.unlink(missing_ok=True)
    cursor = conn.execute("DELETE FROM study_audio WHERE id=?", (audio_id,))
    conn.commit()
    return bool(cursor.rowcount)


def create_dictation_card(conn: sqlite3.Connection, project_id: str, audio_id: str,
                          *, transcript: str = "", tags: str = "") -> dict:
    audio = get_audio(conn, audio_id)
    if audio is None:
        raise ValueError("这段音频已经不在了")
    text = _clean_raw(transcript or audio.get("transcript") or "", "原文", limit=10000)
    if not text:
        raise ValueError("听写卡需要原文 transcript")
    existing = conn.execute(
        "SELECT id FROM study_cards WHERE project_id=? AND card_type='dictation' "
        "AND extra_json LIKE ?", (project_id, f'%"{audio_id}"%')).fetchone()
    if existing:
        raise ValueError("这段音频已经有听写卡")
    card = add_card(
        conn, project_id, card_type="dictation", front="请听写这段音频", back=text,
        extra={"audio_id": audio_id}, tags=tags, status="candidate",
    )
    if card is None:
        raise ValueError("这个项目里已经有同样的听写卡")
    return card


# --------------------------------------------------------------------------
# 听写判分（纯函数，供页面/工具共用）
# --------------------------------------------------------------------------
def normalize_dictation(text: str) -> str:
    text = (text or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def dictation_score(expected: str, actual: str) -> float:
    """按词位置比对的简单准确率。归一化后完全一致就是 1.0。"""
    want = normalize_dictation(expected).split()
    got = normalize_dictation(actual).split()
    if not want:
        return 0.0
    same = sum(1 for a, b in zip(want, got) if a == b)
    return round(same / len(want), 3)


def rating_for_score(score: float) -> int:
    if score >= 0.95:
        return 4
    if score >= 0.7:
        return 3
    return 0