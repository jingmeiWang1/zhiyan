"""健康 / 健身 —— 身体指标、训练、饮食、目标与计划。

六张表，都是**第一次用到才建**（跟 applications/library.py 一样），
所以已经在用的工作目录不需要迁移：

    health_metrics    一行 = 某天量的一个指标（体重、体脂、腰围……）
    health_workouts   一行 = 某天做的一个动作（几组 × 几次 × 多重）
    health_meals      一行 = 一顿里的一个食物（热量和三个宏量都可以留空）
    health_plans      一行 = 一份计划的**一个版本**（条目存在 items_json 里）
    health_targets    一行 = 每日营养目标。**全库只有一行**（id 恒为 1）
    health_foods      一行 = 食物库里的一个食物，数值都是「每 100 单位」

几个不显然的选择，写在这里，免得下次有人"顺手"把它们改掉：

1. **指标是键值对，不是固定列。** 指标会加（体脂、腰围、静息心率、睡眠……），
   固定列每加一个就要改表。趋势 = 按 metric 分组查同一张表。
   但 `metric` **只认 METRICS 这个常量表**（页面上是下拉框），否则"体重"和
   "体重(kg)"会变成两条各自只有一半数据的线，而且看不出来。

2. **计划的历史版本就是同一个 slug 的旧行，不删。** 保存一份新计划 = 插一行
   version+1，并把同一个 slug 原来 active 的那行改成 archived。这样"上个月那份
   饮食计划是怎么写的"随时能翻回来 —— 用户要的就是这个，不是覆盖。

3. **可勾选的条目存在 items_json 里，勾选改的是当前那一行**（archived 的版本
   一个字都不动）。条目只在它那份计划里有意义，不值得单独一张表加一次 JOIN。

4. **热量和宏量可以留空，留空就是 unknown，不是 0。** 用户记"米饭"时不知道热量
   是常态；把不知道记成 0 会让"今天吃了多少"变成一个自信的错数。所以合计里
   只要有一条没填，那一天的总数就是 None，页面显示"—"。

5. **目标只有"现在这一份"，不留历史。** 用户要的是"我现在的每日目标是多少"，
   不是"目标怎么变的"。所以 health_targets 固定一行、直接 upsert。
   真要记目标的变化史，用 health_plans（goal 类）——那张表本来就为这个存在。

6. **食物库的数值是"每 100 单位"，不是一份用量。** 记一顿饭 = 选一个食物 +
   写吃了多少克，热量由页面算（`grams × 每100g值 / 100`），**算出来的数存进
   health_meals 那一行**（连同 grams）。存下来的是**当时算的那一份快照**：
   以后改了食物库里的数值，已经记过的那些饭一笔都不会变 —— 它记的是"那天吃了
   什么"，不是"现在这个食物是多少卡"。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

# 下拉框里能选的指标。key 是存进库的名字，value 是单位。
# 想加指标就在这里加一行 —— 表结构不用动（原因见文件开头第 1 条）。
METRICS: dict[str, str] = {
    "体重": "kg",
    "体脂": "%",
    "腰围": "cm",
    "胸围": "cm",
    "静息心率": "bpm",
    "睡眠": "h",
}

MEALS = ("早餐", "午餐", "晚餐", "加餐")

# 一份计划属于哪一类。goal=目标 training=训练 diet=饮食。
PLAN_KINDS = {"goal": "目标", "training": "训练", "diet": "饮食"}

# 食物库的分类。顺序就是页面上那排筛选按钮的顺序。
# 想加一类就在这里加一行 —— 表结构不用动（和 METRICS 同一个道理）。
FOOD_CATEGORIES: dict[str, str] = {
    "主食": "主食", "肉蛋奶": "肉蛋奶", "蔬菜": "蔬菜", "水果": "水果",
    "饮品": "饮品", "零食": "零食", "其他": "其他",
}

# 食物库的量词。牛奶、饮料按 100ml 记，别的按 100g。
FOOD_UNITS = ("g", "ml")

_MAX_TEXT = 400


# --------------------------------------------------------------------------
# 建表
# --------------------------------------------------------------------------
def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS health_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,              -- YYYY-MM-DD
        metric TEXT NOT NULL,            -- METRICS 里的键
        value REAL NOT NULL,
        unit TEXT DEFAULT '',
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS health_workouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        exercise TEXT NOT NULL,
        sets INTEGER, reps INTEGER, weight REAL, rpe REAL,
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS health_meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        meal TEXT NOT NULL,              -- 早餐/午餐/晚餐/加餐
        food TEXT NOT NULL,
        kcal REAL, protein REAL, carbs REAL, fat REAL,
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS health_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL,              -- 一份计划的稳定身份；同 slug 的多行 = 历史版本
        version INTEGER NOT NULL,
        kind TEXT NOT NULL,              -- goal / training / diet
        title TEXT NOT NULL,
        note TEXT DEFAULT '',
        status TEXT NOT NULL,            -- active / archived
        items_json TEXT DEFAULT '[]',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS health_targets (
        id INTEGER PRIMARY KEY CHECK (id = 1),   -- 全库只有这一行，见文件开头第 5 条
        kcal REAL, protein REAL, carbs REAL, fat REAL,   -- 留空 = 没设这一项
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS health_foods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,       -- 重名会被拒，不静默覆盖
        category TEXT NOT NULL,          -- FOOD_CATEGORIES 里的键
        unit TEXT NOT NULL DEFAULT 'g',  -- g 或 ml（都是「每 100 单位」）
        kcal REAL, protein REAL, carbs REAL, fat REAL,
        custom INTEGER NOT NULL DEFAULT 0,   -- 0=内置种子 1=用户自己建的
        created_at TEXT NOT NULL
    );
    """)
    _add_missing_columns(conn)
    conn.commit()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """给**已经在用的**库补上后加的列。

    建表用的 CREATE TABLE IF NOT EXISTS 对老库是空操作 —— 表已经在了，
    新列不会自己出现。所以后加的列要在这里补一句 ALTER TABLE。
    SQLite 的 ADD COLUMN 是原地改、不动已有的行，老行拿到的就是 NULL
    （"不知道"，不是 0，正好是我们要的语义）。

    目前只有一列：health_meals.grams（选食物记饭要记吃了多少克）。
    **以后再加列就往这张小表里加一行。**
    """
    needed = {"health_meals": ("grams", "REAL")}
    for table, (column, kind) in needed.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


# --------------------------------------------------------------------------
# 输入清洗：坏输入要在这里挡掉，而不是写进库里
# --------------------------------------------------------------------------
def _clean_date(value, field: str = "日期") -> str:
    """'' 或 None = 今天。别的必须是 YYYY-MM-DD，否则拒绝。

    替用户猜日期是很危险的：他说"昨天练了"而系统记成今天，一开始谁也看不出来。
    """
    if value in (None, ""):
        return date.today().isoformat()
    text = str(value).strip()
    try:
        # 只要日历上的那一天，不要时刻，所以这里本来就是"无时区"的整数日期。
        # （和 tools/apple.py 同一个理由，也是同一个 noqa。）
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()  # noqa: DTZ007
    except ValueError as exc:
        raise ValueError(f"{field}要写成 YYYY-MM-DD，收到的是「{text}」") from exc


def _clean_num(value, field: str, *, required: bool = False):
    """数字字段。'' / None = 不知道（None）；写不成数字就拒绝。"""
    if value in (None, ""):
        if required:
            raise ValueError(f"{field}不能为空")
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是数字，收到的是「{value}」") from exc


def _clean_int(value, field: str):
    number = _clean_num(value, field)
    if number is None:
        return None
    if number != int(number) or number < 0:
        raise ValueError(f"{field}必须是 0 或正整数，收到的是「{value}」")
    return int(number)


def _clean_text(value, field: str, *, required: bool = False) -> str:
    text = str(value or "").strip()[:_MAX_TEXT]
    if required and not text:
        raise ValueError(f"{field}不能为空")
    return text


def _clean_metric(value) -> str:
    name = _clean_text(value, "指标", required=True)
    if name not in METRICS:
        raise ValueError(f"没有「{name}」这个指标，可选的是：{'、'.join(METRICS)}")
    return name


def _clean_kind(value) -> str:
    kind = _clean_text(value, "计划类型", required=True)
    if kind not in PLAN_KINDS:
        raise ValueError(f"计划类型只能是 {'、'.join(PLAN_KINDS)}")
    return kind


def _slugify(text: str) -> str:
    """标题 → slug：去空格、转小写。标题一样的两份计划就是同一条线的新版本。"""
    return "-".join(text.split()).lower()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _days_ago(days: int) -> str:
    """days=0 表示"不限时间"。"""
    if not days:
        return "0000-01-01"
    return (date.today() - timedelta(days=int(days))).isoformat()


# --------------------------------------------------------------------------
# 身体指标
# --------------------------------------------------------------------------
def add_metric(conn: sqlite3.Connection, *, date=None, metric, value, note="") -> dict:
    """记一条指标。同一个指标同一天记多次时，**以最新一条为准**（不合并、不平均）。"""
    _ensure_table(conn)
    name = _clean_metric(metric)
    row = {
        "date": _clean_date(date),
        "metric": name,
        "value": _clean_num(value, name, required=True),
        "unit": METRICS[name],
        "note": _clean_text(note, "备注"),
        "created_at": _now(),
    }
    cur = conn.execute(
        "INSERT INTO health_metrics (date, metric, value, unit, note, created_at) "
        "VALUES (:date, :metric, :value, :unit, :note, :created_at)", row)
    conn.commit()
    return _metric_row(conn.execute(
        "SELECT * FROM health_metrics WHERE id=?", (cur.lastrowid,)).fetchone())


def list_metrics(conn: sqlite3.Connection, metric: str = "", days: int = 120) -> list[dict]:
    _ensure_table(conn)
    sql = "SELECT * FROM health_metrics WHERE date >= ?"
    args: list = [_days_ago(days)]
    if metric:
        sql += " AND metric = ?"
        args.append(_clean_metric(metric))
    sql += " ORDER BY date DESC, id DESC"
    return [_metric_row(r) for r in conn.execute(sql, args).fetchall()]


def _metric_row(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "date": row["date"], "metric": row["metric"],
            "value": row["value"], "unit": row["unit"], "note": row["note"]}


def metric_series(points: list[dict], metric: str) -> list[dict]:
    """一个指标的时间序列，**从早到晚**（画图要这个顺序；列表要倒序）。

    同一天有多条时只留当天最后记的那一条（points 是倒序进来的）。
    """
    same = [p for p in points if p["metric"] == metric]
    by_day: dict[str, dict] = {}
    for point in same:
        by_day.setdefault(point["date"], point)     # 倒序遍历 ⇒ 先见到的是最新的
    return sorted(by_day.values(), key=lambda p: p["date"])


# --------------------------------------------------------------------------
# 训练
# --------------------------------------------------------------------------
def add_workout(conn: sqlite3.Connection, *, date=None, exercise, sets=None, reps=None,
                weight=None, rpe=None, note="") -> dict:
    _ensure_table(conn)
    row = {
        "date": _clean_date(date),
        "exercise": _clean_text(exercise, "动作", required=True),
        "sets": _clean_int(sets, "组数"),
        "reps": _clean_int(reps, "每组次数"),
        "weight": _clean_num(weight, "重量"),
        "rpe": _clean_num(rpe, "RPE"),
        "note": _clean_text(note, "备注"),
        "created_at": _now(),
    }
    cur = conn.execute(
        "INSERT INTO health_workouts (date, exercise, sets, reps, weight, rpe, note, created_at) "
        "VALUES (:date, :exercise, :sets, :reps, :weight, :rpe, :note, :created_at)", row)
    conn.commit()
    return _workout_row(conn.execute(
        "SELECT * FROM health_workouts WHERE id=?", (cur.lastrowid,)).fetchone())


def list_workouts(conn: sqlite3.Connection, days: int = 30, exercise: str = "") -> list[dict]:
    _ensure_table(conn)
    sql = "SELECT * FROM health_workouts WHERE date >= ?"
    args: list = [_days_ago(days)]
    if exercise:
        sql += " AND exercise = ?"
        args.append(_clean_text(exercise, "动作", required=True))
    sql += " ORDER BY date DESC, id DESC"
    return [_workout_row(r) for r in conn.execute(sql, args).fetchall()]


def _workout_row(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "date": row["date"], "exercise": row["exercise"],
            "sets": row["sets"], "reps": row["reps"], "weight": row["weight"],
            "rpe": row["rpe"], "note": row["note"]}


def workout_volume(rows: list[dict]) -> float:
    """训练总容量 = 组 × 次 × 重量，只算三个数都填了的行。

    任何一个没填就不进合计 —— 跟饮食那边同一个道理：算不出来的东西不进总数。
    """
    return round(sum(r["sets"] * r["reps"] * r["weight"] for r in rows
                     if r["sets"] and r["reps"] and r["weight"]), 1)


# --------------------------------------------------------------------------
# 饮食
# --------------------------------------------------------------------------
def add_meal(conn: sqlite3.Connection, *, date=None, meal, food, grams=None, kcal=None,
             protein=None, carbs=None, fat=None, note="") -> dict:
    """记一顿里的一个食物。`grams` 是吃了多少（选食物时才有；手打的可以不留）。"""
    _ensure_table(conn)
    row = {
        "date": _clean_date(date),
        "meal": _clean_text(meal, "哪一餐", required=True),
        "food": _clean_text(food, "食物", required=True),
        "grams": _clean_num(grams, "份量"),
        "kcal": _clean_num(kcal, "热量"),
        "protein": _clean_num(protein, "蛋白质"),
        "carbs": _clean_num(carbs, "碳水"),
        "fat": _clean_num(fat, "脂肪"),
        "note": _clean_text(note, "备注"),
        "created_at": _now(),
    }
    cur = conn.execute(
        "INSERT INTO health_meals (date, meal, food, grams, kcal, protein, carbs, fat, note, "
        "created_at) VALUES (:date, :meal, :food, :grams, :kcal, :protein, :carbs, :fat, "
        ":note, :created_at)", row)
    conn.commit()
    return _meal_row(conn.execute(
        "SELECT * FROM health_meals WHERE id=?", (cur.lastrowid,)).fetchone())


def list_meals(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT * FROM health_meals WHERE date >= ? ORDER BY date DESC, id DESC",
        (_days_ago(days),)).fetchall()
    return [_meal_row(r) for r in rows]


def _meal_row(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "date": row["date"], "meal": row["meal"], "food": row["food"],
            "grams": row["grams"], "kcal": row["kcal"], "protein": row["protein"],
            "carbs": row["carbs"], "fat": row["fat"], "note": row["note"]}


def _totals_rows(conn: sqlite3.Connection, days: int, group: str, order: str) -> list[dict]:
    """合计的公共部分：按 `group`（date，或 date+meal）分组求和。

    真正的判断在 `_totals_one` —— 热量的"有一条没填就不出数"和宏量是**同一条规矩**，
    所以两个合计函数（按天 / 按天按餐）共用它。**不要在这里另写一套判断。**
    """
    _ensure_table(conn)
    rows = conn.execute(
        f"""SELECT {group}, COUNT(*) AS items,
                   SUM(kcal) AS kcal, SUM(protein) AS protein,
                   SUM(carbs) AS carbs, SUM(fat) AS fat,
                   SUM(CASE WHEN kcal IS NULL THEN 1 ELSE 0 END) AS kcal_unknown,
                   SUM(CASE WHEN protein IS NULL THEN 1 ELSE 0 END) AS protein_unknown,
                   SUM(CASE WHEN carbs IS NULL THEN 1 ELSE 0 END) AS carbs_unknown,
                   SUM(CASE WHEN fat IS NULL THEN 1 ELSE 0 END) AS fat_unknown
              FROM health_meals WHERE date >= ?
             GROUP BY {group} ORDER BY {order}""", (_days_ago(days),)).fetchall()
    return [dict(r) for r in rows]


def _totals_one(row: dict) -> dict:
    """一组记录（一天，或一天里的一餐）的合计。

    **只要有一行没填某一项，那一项就是 None 而不是 0** —— 用户不知道热量是常态，
    把它算成 0 会让"今天摄入多少"变成一句很自信的错话（见文件开头第 4 条）。

    宏量和热量用**同一条规矩**：SUM 会跳过 NULL 行，所以"三顿里有一顿没写蛋白"
    如果用 SUM 的结果，报出来的会是一个偏小的数 —— 看着很确定，但是错的。
    数一数有几行没填（*_unknown），有就说明这一项算不出来。
    """
    out = {"items": row["items"], "kcal_unknown": row["kcal_unknown"] or 0}
    for key in ("kcal", "protein", "carbs", "fat"):
        unknown = row[f"{key}_unknown"] or 0
        out[key] = None if unknown else round(row[key] or 0, 1)
    return out


def daily_totals(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    """每天的热量与宏量合计，新的日子在前。"""
    return [{"date": row["date"], **_totals_one(row)}
            for row in _totals_rows(conn, days, "date", "date DESC")]


def meal_totals(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """**按天、按餐**的小计（手账里每个餐次右上角那个数）。

    在服务端算的理由和上面一样：这个"有一条没填就不出数"的规矩只该有一份实现。
    页面拿到就直接显示，不自己算。
    """
    return [{"date": row["date"], "meal": row["meal"], **_totals_one(row)}
            for row in _totals_rows(conn, days, "date, meal", "date DESC")]


# --------------------------------------------------------------------------
# 目标与计划
# --------------------------------------------------------------------------
def save_plan(conn: sqlite3.Connection, *, kind, title, items=None, slug="", note="") -> dict:
    """存一份计划 = 插一个新的版本行，并把同 slug 原来 active 的那行改成 archived。

    `slug` 不给就按标题生成（见 _slugify）：标题一样 ⇒ 同一条线的新版本；
    标题不一样 ⇒ 另起一条线。旧版本一行都不删，回看就在这些行里。
    """
    _ensure_table(conn)
    clean_kind = _clean_kind(kind)
    clean_title = _clean_text(title, "计划标题", required=True)
    clean_slug = _clean_text(slug, "slug") or f"{clean_kind}/{_slugify(clean_title)}"
    clean_items = _clean_items(items)

    previous = conn.execute(
        "SELECT MAX(version) AS v FROM health_plans WHERE slug=?", (clean_slug,)).fetchone()
    version = (previous["v"] or 0) + 1
    conn.execute(
        "UPDATE health_plans SET status='archived' WHERE slug=? AND status='active'",
        (clean_slug,))
    cur = conn.execute(
        "INSERT INTO health_plans (slug, version, kind, title, note, status, items_json, created_at) "
        "VALUES (?,?,?,?,?,'active',?,?)",
        (clean_slug, version, clean_kind, clean_title, _clean_text(note, "备注"),
         json.dumps(clean_items, ensure_ascii=False), _now()))
    conn.commit()
    return _plan_row(conn.execute(
        "SELECT * FROM health_plans WHERE id=?", (cur.lastrowid,)).fetchone())


def _clean_items(items) -> list[dict]:
    """条目 = [{"text": ..., "done": 0/1}]。允许传字符串列表（第一次写计划时）。"""
    if items in (None, ""):
        return []
    if not isinstance(items, list):
        raise TypeError("条目应该是一个列表")
    out = []
    for item in items:
        if isinstance(item, dict):
            text = _clean_text(item.get("text"), "条目")
            done = 1 if item.get("done") else 0
        else:
            text, done = _clean_text(item, "条目"), 0
        if text:
            out.append({"text": text, "done": done, "done_at": None})
    return out


def toggle_item(conn: sqlite3.Connection, plan_id: int, index: int, done) -> dict:
    """勾掉/取消一份计划里的第 N 条。**只有当前生效的那一版能勾** ——

    archived 的版本是一份"当时是怎么写的"的记录，动它等于篡改历史。
    """
    _ensure_table(conn)
    row = conn.execute("SELECT * FROM health_plans WHERE id=?", (int(plan_id),)).fetchone()
    if row is None:
        raise ValueError("这份计划不存在")
    if row["status"] != "active":
        raise ValueError("这是历史版本，只能看不能勾")
    items = json.loads(row["items_json"] or "[]")
    if not items:
        raise ValueError("这份计划还没有条目")
    if not 0 <= int(index) < len(items):
        raise ValueError("没有这一条")
    items[int(index)]["done"] = 1 if done else 0
    items[int(index)]["done_at"] = _now() if done else None
    conn.execute("UPDATE health_plans SET items_json=? WHERE id=?",
                 (json.dumps(items, ensure_ascii=False), row["id"]))
    conn.commit()
    return _plan_row(conn.execute("SELECT * FROM health_plans WHERE id=?", (row["id"],)).fetchone())


def archive_plan(conn: sqlite3.Connection, plan_id: int) -> dict:
    """把一份计划收起来（它的版本历史还在）。"""
    _ensure_table(conn)
    conn.execute("UPDATE health_plans SET status='archived' WHERE id=?", (int(plan_id),))
    conn.commit()
    row = conn.execute("SELECT * FROM health_plans WHERE id=?", (int(plan_id),)).fetchone()
    if row is None:
        raise ValueError("这份计划不存在")
    return _plan_row(row)


def list_plans(conn: sqlite3.Connection, kind: str = "") -> list[dict]:
    """所有版本，新的在前。`kind` 空 = 三类都要。"""
    _ensure_table(conn)
    sql = "SELECT * FROM health_plans"
    args: list = []
    if kind:
        sql += " WHERE kind=?"
        args.append(_clean_kind(kind))
    sql += " ORDER BY created_at DESC, id DESC"
    return [_plan_row(r) for r in conn.execute(sql, args).fetchall()]


def active_plans(conn: sqlite3.Connection) -> list[dict]:
    """每个 slug 当前生效的那一版。"""
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT * FROM health_plans WHERE status='active' ORDER BY kind, created_at DESC").fetchall()
    return [_plan_row(r) for r in rows]


def get_plan(conn: sqlite3.Connection, plan_id) -> dict:
    """一份计划的某一个版本（回看历史版本时用）。"""
    _ensure_table(conn)
    row = conn.execute("SELECT * FROM health_plans WHERE id=?", (int(plan_id),)).fetchone()
    if row is None:
        raise ValueError("这份计划不存在")
    return _plan_row(row)


def _plan_row(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "slug": row["slug"], "version": row["version"],
            "kind": row["kind"], "kind_label": PLAN_KINDS.get(row["kind"], row["kind"]),
            "title": row["title"], "note": row["note"], "status": row["status"],
            "items": json.loads(row["items_json"] or "[]"), "created_at": row["created_at"]}


# --------------------------------------------------------------------------
# 每日目标（全库一行）
# --------------------------------------------------------------------------
def get_target(conn: sqlite3.Connection) -> dict:
    """现在的每日目标。**没设过也回一个四个键都是 None 的字典**，不回 None ——

    页面每 5 秒要画一次这个数，让每个调用方都写一遍 `if target is None` 是自找的。
    None 在这里就是"这一项没设"，和别的表里"不知道"是同一个意思。
    """
    _ensure_table(conn)
    row = conn.execute("SELECT * FROM health_targets WHERE id=1").fetchone()
    if row is None:
        return {"kcal": None, "protein": None, "carbs": None, "fat": None, "updated_at": None}
    return {"kcal": row["kcal"], "protein": row["protein"], "carbs": row["carbs"],
            "fat": row["fat"], "updated_at": row["updated_at"]}


def set_target(conn: sqlite3.Connection, *, kcal=None, protein=None, carbs=None, fat=None) -> dict:
    """设每日目标。四项全留空 = 取消目标（行还在，四个数都是空的）。"""
    _ensure_table(conn)
    row = {
        "kcal": _clean_num(kcal, "目标热量"),
        "protein": _clean_num(protein, "目标蛋白质"),
        "carbs": _clean_num(carbs, "目标碳水"),
        "fat": _clean_num(fat, "目标脂肪"),
        "updated_at": _now(),
    }
    conn.execute(
        "INSERT INTO health_targets (id, kcal, protein, carbs, fat, updated_at) "
        "VALUES (1, :kcal, :protein, :carbs, :fat, :updated_at) "
        "ON CONFLICT(id) DO UPDATE SET kcal=:kcal, protein=:protein, carbs=:carbs, "
        "fat=:fat, updated_at=:updated_at", row)
    conn.commit()
    return get_target(conn)


# --------------------------------------------------------------------------
# 食物库
# --------------------------------------------------------------------------
def _clean_food_name(value) -> str:
    return _clean_text(value, "食物名", required=True)


def _clean_category(value) -> str:
    name = _clean_text(value, "分类", required=True)
    if name not in FOOD_CATEGORIES:
        raise ValueError(f"没有「{name}」这个分类，可选的是：{'、'.join(FOOD_CATEGORIES)}")
    return name


def _clean_unit(value) -> str:
    unit = _clean_text(value, "单位") or "g"
    if unit not in FOOD_UNITS:
        raise ValueError(f"单位只能是 {'、'.join(FOOD_UNITS)}")
    return unit


def _food_row(row: sqlite3.Row) -> dict:
    """食物的一行。**每个数值都可能是 None = 不知道**（种子数据里就有没填的项）。"""
    return {"id": row["id"], "name": row["name"], "category": row["category"],
            "unit": row["unit"], "kcal": row["kcal"], "protein": row["protein"],
            "carbs": row["carbs"], "fat": row["fat"], "custom": row["custom"]}


def list_foods(conn: sqlite3.Connection, category: str = "", query: str = "") -> list[dict]:
    """食物库。`category` / `query` 都空 = 全部。

    自己建的排在最前面（`custom DESC`）：他刚建的那个一定是他现在要用的。
    """
    _seed_foods(conn)
    sql = "SELECT * FROM health_foods"
    args: list = []
    where = []
    if category:
        where.append("category = ?")
        args.append(_clean_category(category))
    if query:
        where.append("name LIKE ?")
        args.append(f"%{_clean_text(query, '关键词')}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY custom DESC, category, name"
    return [_food_row(r) for r in conn.execute(sql, args).fetchall()]


def get_food(conn: sqlite3.Connection, food_id) -> dict:
    _ensure_table(conn)
    row = conn.execute("SELECT * FROM health_foods WHERE id=?", (int(food_id),)).fetchone()
    if row is None:
        raise ValueError("食物库里没有这个食物")
    return _food_row(row)


def add_food(conn: sqlite3.Connection, *, name, category, unit="g", kcal=None,
             protein=None, carbs=None, fat=None) -> dict:
    """往食物库里加一个食物。**重名会被拒，不覆盖** ——

    同名多半是"我记得库里有"或者手滑，而覆盖会把别人正在用的那个数值悄悄改掉，
    已经按它记过的饭对不上账。要改就用 update_food。
    """
    _ensure_table(conn)
    row = {
        "name": _clean_food_name(name),
        "category": _clean_category(category),
        "unit": _clean_unit(unit),
        "kcal": _clean_num(kcal, "热量"),
        "protein": _clean_num(protein, "蛋白质"),
        "carbs": _clean_num(carbs, "碳水"),
        "fat": _clean_num(fat, "脂肪"),
        "created_at": _now(),
    }
    if conn.execute("SELECT 1 FROM health_foods WHERE name=?", (row["name"],)).fetchone():
        raise ValueError(f"食物库里已经有一个叫「{row['name']}」的了，改它就行")
    cur = conn.execute(
        "INSERT INTO health_foods (name, category, unit, kcal, protein, carbs, fat, custom, "
        "created_at) VALUES (:name, :category, :unit, :kcal, :protein, :carbs, :fat, 1, "
        ":created_at)", row)
    conn.commit()
    return _food_row(conn.execute(
        "SELECT * FROM health_foods WHERE id=?", (cur.lastrowid,)).fetchone())


def update_food(conn: sqlite3.Connection, food_id, *, name, category, unit, kcal=None,
                protein=None, carbs=None, fat=None) -> dict:
    """改一个食物（整行替换 —— 页面上那个小表单本来就是四项都摆出来的）。

    `custom` 不动：内置的种子被改过之后仍然是内置的，它只是"这一条我调过"，
    不是"这一条是我建的"。
    """
    _ensure_table(conn)
    food = get_food(conn, food_id)
    row = {
        "id": food["id"],
        "name": _clean_food_name(name),
        "category": _clean_category(category),
        "unit": _clean_unit(unit),
        "kcal": _clean_num(kcal, "热量"),
        "protein": _clean_num(protein, "蛋白质"),
        "carbs": _clean_num(carbs, "碳水"),
        "fat": _clean_num(fat, "脂肪"),
    }
    other = conn.execute("SELECT id FROM health_foods WHERE name=? AND id<>?",
                         (row["name"], food["id"])).fetchone()
    if other is not None:
        raise ValueError(f"食物库里已经有一个叫「{row['name']}」的了")
    conn.execute(
        "UPDATE health_foods SET name=:name, category=:category, unit=:unit, kcal=:kcal, "
        "protein=:protein, carbs=:carbs, fat=:fat WHERE id=:id", row)
    conn.commit()
    return get_food(conn, food["id"])


def delete_food(conn: sqlite3.Connection, food_id) -> bool:
    """从食物库删掉一个。**已经记过的饮食行一个字都不变** ——

    那些行里存的是当时算出来的数（快照），不是指向食物库的外键。
    删掉"鸡胸肉"不该让上周二那顿饭消失。
    """
    _ensure_table(conn)
    cur = conn.execute("DELETE FROM health_foods WHERE id=?", (int(food_id),))
    conn.commit()
    return cur.rowcount > 0


def _seed_foods(conn: sqlite3.Connection) -> None:
    """食物库是空的时候，把内置的那份参考值种进去。

    **只在空表时种**（不是每次补缺）：/api/data 每 5 秒读一次食物库，写成
    "缺哪条补哪条"就是每 5 秒查一遍 100 行。代价写在 food_data.py 开头：
    以后往种子表里加食物，已经在用的库看不到。
    """
    _ensure_table(conn)
    if conn.execute("SELECT 1 FROM health_foods LIMIT 1").fetchone() is not None:
        return
    from knowme.applications.food_data import FOOD_SEED
    conn.executemany(
        "INSERT INTO health_foods (name, category, unit, kcal, protein, carbs, fat, custom, "
        "created_at) VALUES (?,?,?,?,?,?,?,0,?)",
        [(name, category, unit, kcal, protein, carbs, fat, _now())
         for name, category, unit, kcal, protein, carbs, fat in FOOD_SEED])
    conn.commit()


# --------------------------------------------------------------------------
# 删除
# --------------------------------------------------------------------------
_TABLES = {"metric": "health_metrics", "workout": "health_workouts",
           "meal": "health_meals", "plan": "health_plans"}


def delete_record(conn: sqlite3.Connection, kind: str, record_id) -> bool:
    """删一条记录。kind ∈ {metric, workout, meal, plan}（plan 删的是那**一个版本**）。"""
    table = _TABLES.get(kind)
    if table is None:
        raise ValueError(f"不知道要删什么：「{kind}」")
    _ensure_table(conn)
    cur = conn.execute(f"DELETE FROM {table} WHERE id=?", (int(record_id),))
    conn.commit()
    return cur.rowcount > 0
