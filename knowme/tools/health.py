"""健康工具 — 健康 Agent 读你身体数据、训练、饮食和计划的那一套。

读的是 ``applications/health.py`` 那六张表，不碰别的文件。所有写入都走那边的
``add_*`` / ``save_plan`` / ``set_target``，所以"用户手填"和"Agent 帮你记"落到
库里的是同一份形状，页面上看到的就是 Agent 记的。

三条设计上的取舍：

**为什么有 health_overview 这一个"什么都答"的工具。**
    没有它，模型每开一次对话都得先问一圈「你记过体重吗、练过什么、吃过什么」 ——
    五六个往返换来的只是让它站到起跑线上。一次调用把最近 30 天的摘要给全，
    它才有东西可聊。

**为什么读和写分成两组。**
    ``query_*`` 是查（「上个月体重怎么变的」要原始点，摘要里没有），``log_*`` 是记。
    分开是为了让描述里能写清各自的用法：查是随时可以做的，记是要先跟用户确认的。

**为什么每个数都可能是一个破折号。**
    库里"没填"是常态。热量没填就是不知道，不是 0 —— 所以这里的格式化函数把
    ``None`` 写成 ``—`` 并且明说有几条没填，绝不替用户把未知算成 0。这条规矩
    和页面上是同一个（applications/health.py 开头第 4 条）。

**为什么有一个查食物库的工具。**
    用户说"我中午吃了 150 克鸡胸肉"，模型自己报一个热量就是个很自信的猜。
    ``search_food`` 给的是库里的每 100 单位数值，换算（乘克数再除以 100）是
    一步算术，模型做得了。查不到就说查不到 —— 库里没有就去页面上建一个，
    不要凭印象编一个数出来。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from knowme.applications import health
from knowme.core.tools import Tool

# 摘要在没有数据时必须说清"是没有"而不是"是 0"。这几句就是那个"说清"。
NO_DATA = "还没有记录。"


def _num(value, unit: str = "") -> str:
    """数字（可能没有）。没有就是 —，不是 0。"""
    if value is None:
        return "—"
    text = f"{value:g}" if isinstance(value, float) else str(value)
    return f"{text}{unit}"


def _metric_names() -> str:
    return "、".join(f"{name}（{unit}）" for name, unit in health.METRICS.items())


def make_health_tools(conn: sqlite3.Connection) -> dict[str, Tool]:
    """按连接建出这套工具。和 make_document_tools 同一个形状：工厂拿连接，
    注册表拿工具。"""

    # ---- 读 -----------------------------------------------------------------
    def _overview() -> str:
        metrics = health.list_metrics(conn, days=30)
        workouts = health.list_workouts(conn, days=30)
        totals = health.daily_totals(conn, days=30)
        plans = health.active_plans(conn)

        lines = ["最近 30 天："]

        # 体重这一行单独拎出来：它是几乎所有目标都要看的那个数，而且"首尾变化"
        # 才是用户真正问的那件事（原始点由 query_metrics 给）。
        weight = health.metric_series(metrics, "体重")
        if not weight:
            lines.append("体重：没有记录。")
        elif len(weight) == 1:
            lines.append(f"体重：{_num(weight[0]['value'], weight[0]['unit'])}"
                         f"（{weight[0]['date']}，只有这一条，看不出趋势）。")
        else:
            first, last = weight[0], weight[-1]
            delta = round(last["value"] - first["value"], 1)
            lines.append(f"体重：{_num(first['value'], first['unit'])}（{first['date']}）→ "
                         f"{_num(last['value'], last['unit'])}（{last['date']}），"
                         f"变化 {delta:+g}{last['unit']}。共 {len(weight)} 天有记录。")

        # 别的指标只报"有没有、最新是多少"，让模型自己决定要不要再查。
        others = [name for name in health.METRICS if name != "体重"]
        seen = [f"{name} {_num(health.metric_series(metrics, name)[-1]['value'], health.METRICS[name])}"
                for name in others if health.metric_series(metrics, name)]
        lines.append("其它指标（最新值）：" + ("、".join(seen) if seen else "都没有记录。"))

        if not workouts:
            lines.append("训练：最近 30 天没有记录。")
        else:
            days = len({w["date"] for w in workouts})
            by_name: dict[str, int] = {}
            for w in workouts:
                by_name[w["exercise"]] = by_name.get(w["exercise"], 0) + 1
            top = "、".join(f"{name} {n} 次" for name, n in
                            sorted(by_name.items(), key=lambda kv: -kv[1])[:5])
            lines.append(f"训练：{days} 天有记录，共 {len(workouts)} 条（{top}）。"
                         f"总容量 {_num(health.workout_volume(workouts), 'kg')}"
                         "（只有组数、次数、重量都填了的行算进去）。")

        if not totals:
            lines.append("饮食：最近 30 天没有记录。")
        else:
            # 合计可能因为缺热量而算不出来 —— 说清楚是"算不出来"，不是"吃了 0"。
            known = [t for t in totals if t["kcal"] is not None]
            unknown = sum(t["kcal_unknown"] for t in totals)
            if known:
                avg = round(sum(t["kcal"] for t in known) / len(known), 1)
                kcal = f"{len(known)} 天里的日均热量 {avg} kcal"
            else:
                kcal = "热量一天都没填全，算不出日均"
            protein = [t["protein"] for t in totals if t["protein"] is not None]
            macro = (f"，这些天的日均蛋白质 {round(sum(protein) / len(protein), 1)} g"
                     if protein else "")
            lines.append(f"饮食：{len(totals)} 天有记录，{kcal}{macro}。"
                         + (f"另有 {unknown} 顿没填热量。" if unknown else ""))

        if not plans:
            lines.append("计划：现在没有生效的计划。")
        else:
            lines.append("计划：" + "；".join(
                f"{p['kind_label']}《{p['title']}》v{p['version']}"
                f"（{sum(1 for i in p['items'] if i.get('done'))}/{len(p['items'])} 已完成）"
                for p in plans) + "。")

        lines.append(_today())
        lines.append("要看某一段的原始记录用 query_metrics / query_workouts / query_meals；"
                     "换算食物热量先 search_food。")
        return "\n".join(lines)

    def _today() -> str:
        """今天这一行：目标 / 已摄入 / 剩余。

        单独一段是因为它是用户张嘴最可能问的那件事（"我今天还能吃多少"），
        而"最近 30 天"那一段谁也答不了这个问题。
        """
        target = health.get_target(conn)
        today = date.today().isoformat()
        row = next((t for t in health.daily_totals(conn, days=1) if t["date"] == today), None)
        if target["kcal"] is None:
            return ("今天：还没有设每日热量目标（页面上在「目标与计划」里设，"
                    "也可以让我用 set_target 设一个）。")
        head = f"今天：目标 {_num(target['kcal'], ' kcal')}"
        if row is None:
            return head + "，到现在还没记吃的。"
        if row["kcal"] is None:
            return (head + f"，已摄入算不出来（有 {row['kcal_unknown']} 顿没填热量）"
                    "，所以剩余也说不出来 —— 不是 0。")
        left = round(target["kcal"] - row["kcal"], 1)
        tail = f"，还剩 {left:g}" if left >= 0 else f"，已经超了 {abs(left):g}"
        macro = ""
        if target["protein"] is not None:
            macro = (f"；蛋白目标 {_num(target['protein'], 'g')}，"
                     f"已摄入 {_num(row['protein'], 'g')}")
        return head + f"，已摄入 {_num(row['kcal'], ' kcal')}{tail}{macro}。"

    def _foods(query: str, category: str = "") -> str:
        try:
            rows = health.list_foods(conn, category=category, query=query)
        except ValueError as exc:
            return f"查不了：{exc}。分类有：{'、'.join(health.FOOD_CATEGORIES)}。"
        if not rows:
            what = f"「{query}」" if query else "这个分类里"
            return (f"食物库里没有{what}。**不要凭印象报热量** —— 可以让用户在健康页的"
                    "「食物库」里新建一个，或者告诉我食物名和份量，我记一条不填热量的。")
        lines = [f"- {f['name']}（{f['category']}，每 100{f['unit']}）："
                 f"{_num(f['kcal'], ' kcal')}｜蛋白 {_num(f['protein'], 'g')}"
                 f"｜碳水 {_num(f['carbs'], 'g')}｜脂肪 {_num(f['fat'], 'g')}"
                 for f in rows[:20]]
        more = f"（还有 {len(rows) - 20} 条没列出来，把关键词写具体一点）" if len(rows) > 20 else ""
        return (f"食物库里匹配的记录（{len(rows)} 条，数值都是每 100 单位，"
                f"换算 = 数值 × 克数 ÷ 100）：\n" + "\n".join(lines) + more)

    def _set_target(kcal=None, protein=None, carbs=None, fat=None) -> str:
        try:
            target = health.set_target(conn, kcal=kcal, protein=protein, carbs=carbs, fat=fat)
        except ValueError as exc:
            return f"没设成：{exc}"
        if all(target[k] is None for k in ("kcal", "protein", "carbs", "fat")):
            return "已经把每日目标清空了（四项都没填）。"
        parts = [f"{label} {_num(target[key], unit)}" for key, unit, label in
                 (("kcal", " kcal", "热量"), ("protein", "g", "蛋白"),
                  ("carbs", "g", "碳水"), ("fat", "g", "脂肪"))]
        return ("设好了，每日目标：" + "、".join(parts)
                + "。页面上那个环会按它画。")

    def _metrics(metric: str, days: int = 90) -> str:
        try:
            points = health.metric_series(health.list_metrics(conn, metric, days=days), metric)
        except ValueError as exc:
            return f"查不了：{exc}。能查的指标有：{_metric_names()}。"
        if not points:
            return f"最近 {days} 天没有「{metric}」的记录。"
        rows = "\n".join(f"- {p['date']}  {_num(p['value'], p['unit'])}"
                         + (f"  （{p['note']}）" if p["note"] else "")
                         for p in points)
        first, last = points[0], points[-1]
        delta = round(last["value"] - first["value"], 1)
        return (f"「{metric}」最近 {days} 天有 {len(points)} 天的记录（同一天记多次只算最后一条）：\n"
                f"{rows}\n首尾变化 {delta:+g}{last['unit']}"
                f"（{first['date']} → {last['date']}）。")

    def _workouts(days: int = 30, exercise: str = "") -> str:
        rows = health.list_workouts(conn, days=days, exercise=exercise)
        if not rows:
            what = f"「{exercise}」" if exercise else ""
            return f"最近 {days} 天没有{what}训练记录。"
        lines = [f"- {w['date']}  {w['exercise']}  {_num(w['sets'])}×{_num(w['reps'])}"
                 f" × {_num(w['weight'], 'kg')}"
                 + (f"  RPE {_num(w['rpe'])}" if w["rpe"] is not None else "")
                 + (f"  （{w['note']}）" if w["note"] else "")
                 for w in rows]
        return (f"最近 {days} 天的训练（{len(rows)} 条）：\n" + "\n".join(lines)
                + f"\n总容量 {_num(health.workout_volume(rows), 'kg')}"
                  "（只算三项都填了的行）。")

    def _meals(days: int = 14) -> str:
        meals = health.list_meals(conn, days=days)
        if not meals:
            return f"最近 {days} 天没有饮食记录。"
        totals = health.daily_totals(conn, days=days)
        lines = [f"- {m['date']} {m['meal']}  {m['food']}"
                 f"  {_num(m['kcal'], ' kcal')}｜蛋白 {_num(m['protein'], 'g')}"
                 f"｜碳水 {_num(m['carbs'], 'g')}｜脂肪 {_num(m['fat'], 'g')}"
                 for m in meals]
        day_lines = []
        for t in totals:
            # 合计里的 — 是有意义的：那天有东西没填，所以合计算不出来。
            note = f"（{t['kcal_unknown']} 顿没填热量）" if t["kcal_unknown"] else ""
            day_lines.append(f"  {t['date']}：{t['items']} 条，热量 {_num(t['kcal'], ' kcal')}{note}"
                             f"，蛋白 {_num(t['protein'], 'g')}，碳水 {_num(t['carbs'], 'g')}，"
                             f"脂肪 {_num(t['fat'], 'g')}")
        return (f"最近 {days} 天的饮食记录（{len(meals)} 条）：\n" + "\n".join(lines)
                + "\n每日合计：\n" + "\n".join(day_lines)
                + "\n（合计里的 — 表示那天有记录没填这一项，所以合计算不出来，不是 0。）")

    def _plans(kind: str = "") -> str:
        try:
            rows = health.list_plans(conn, kind)
        except ValueError as exc:
            return f"查不了：{exc}。"
        if not rows:
            return "还没有任何计划。"
        lines = []
        for p in rows:
            items = "、".join(f"{'✓' if i.get('done') else '□'}{i['text']}" for i in p["items"])
            lines.append(f"- [{p['status']}] {p['kind_label']}《{p['title']}》v{p['version']}"
                         f"（{p['created_at'][:10]}）{items or '（没有条目）'}")
        return "所有计划版本（active 是现在生效的那一版，archived 是留档）：\n" + "\n".join(lines)

    # ---- 写 -----------------------------------------------------------------
    def _log_metric(date: str, metric: str, value, note: str = "") -> str:
        try:
            row = health.add_metric(conn, date=date, metric=metric, value=value, note=note)
        except ValueError as exc:
            return f"没记成：{exc}。能记的指标有：{_metric_names()}。"
        return (f"记下了：{row['date']} {row['metric']} {_num(row['value'], row['unit'])}"
                + (f"（{row['note']}）" if row["note"] else "") + "。")

    def _log_workout(date: str, exercise: str, sets=None, reps=None, weight=None,
                     rpe=None, note: str = "") -> str:
        try:
            row = health.add_workout(conn, date=date, exercise=exercise, sets=sets,
                                     reps=reps, weight=weight, rpe=rpe, note=note)
        except ValueError as exc:
            return f"没记成：{exc}"
        return (f"记下了：{row['date']} {row['exercise']} {_num(row['sets'])}×{_num(row['reps'])}"
                f" × {_num(row['weight'], 'kg')}。没填的项目在库里是空的，不是 0。")

    def _log_meal(date: str, meal: str, food: str, grams=None, kcal=None, protein=None,
                  carbs=None, fat=None, note: str = "") -> str:
        try:
            row = health.add_meal(conn, date=date, meal=meal, food=food, grams=grams,
                                  kcal=kcal, protein=protein, carbs=carbs, fat=fat, note=note)
        except ValueError as exc:
            return f"没记成：{exc}。餐次只能是：{'、'.join(health.MEALS)}。"
        weight = f"{_num(row['grams'], 'g')} " if row["grams"] is not None else ""
        kcal = (f"热量 {_num(row['kcal'], ' kcal')}）" if row["kcal"] is not None
                else "没填热量 —— 留空是不知道，不是 0）")
        return f"记下了：{row['date']} {row['meal']} {weight}{row['food']}（{kcal}。"

    def _save_plan(kind: str, title: str, items=None, note: str = "") -> str:
        try:
            row = health.save_plan(conn, kind=kind, title=title, items=items, note=note)
        except ValueError as exc:
            return f"没存成：{exc}"
        except TypeError as exc:
            return f"没存成：{exc}（items 是一个字符串列表，一项一条）。"
        prev = row["version"] - 1
        older = f"，上一版（v{prev}）已归档、没有删" if prev else ""
        return (f"存成了 {row['kind_label']}《{row['title']}》v{row['version']}，"
                f"{len(row['items'])} 条{older}。用户可以在健康页勾选条目。")

    return {
        "health_overview": Tool(
            name="health_overview",
            description="一眼看出用户最近 30 天的身体数据：体重首尾与变化、训练天数与总容量、"
                        "饮食日均、当前生效的计划。**开始聊健康话题时先调它**，"
                        "不要凭印象，也不要先问一圈用户「你有哪些数据」。",
            input_schema={"type": "object", "properties": {}},
            fn=_overview,
        ),
        "query_metrics": Tool(
            name="query_metrics",
            description="某一个身体指标按天的原始记录（从早到晚），比如「我上个月体重怎么变的」"
                        "这种要具体数字的问题。能查的指标：" + _metric_names() + "。",
            input_schema={"type": "object", "properties": {
                "metric": {"type": "string", "description": "指标名，必须和上面列的完全一致"},
                "days": {"type": "integer", "description": "往前查多少天（默认 90）"},
            }, "required": ["metric"]},
            fn=_metrics,
        ),
        "query_workouts": Tool(
            name="query_workouts",
            description="查训练记录（日期、动作、组×次、重量、RPE）。看某个动作有没有进步"
                        "就传 exercise 过滤，比如只看「卧推」。",
            input_schema={"type": "object", "properties": {
                "days": {"type": "integer", "description": "往前查多少天（默认 30）"},
                "exercise": {"type": "string", "description": "只看这个动作（可省）"},
            }},
            fn=_workouts,
        ),
        "query_meals": Tool(
            name="query_meals",
            description="查饮食记录和每天的合计（热量/蛋白/碳水/脂肪）。定饮食计划之前先看真实的"
                        "摄入量。**合计里的 — 表示那天有记录没填这一项，不是 0。**",
            input_schema={"type": "object", "properties": {
                "days": {"type": "integer", "description": "往前查多少天（默认 14）"},
            }},
            fn=_meals,
        ),
        "log_metric": Tool(
            name="log_metric",
            description="记一条身体指标（用户说「我今天 71 公斤」就是在说这个）。"
                        "日期不明确就先问，不要替用户猜。能记的指标：" + _metric_names() + "。",
            input_schema={"type": "object", "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "metric": {"type": "string", "description": "指标名，必须是上面列的那些"},
                "value": {"type": "number", "description": "数值"},
                "note": {"type": "string", "description": "备注（可省）"},
            }, "required": ["date", "metric", "value"]},
            fn=_log_metric,
        ),
        "log_workout": Tool(
            name="log_workout",
            description="记一次训练（一个动作一条）。没说重量或次数就留空，"
                        "**留空是「不知道」，不要填 0**。",
            input_schema={"type": "object", "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "exercise": {"type": "string", "description": "动作，如 深蹲"},
                "sets": {"type": "integer", "description": "组数（可省）"},
                "reps": {"type": "integer", "description": "每组次数（可省）"},
                "weight": {"type": "number", "description": "重量 kg（可省）"},
                "rpe": {"type": "number", "description": "主观强度 RPE（可省）"},
                "note": {"type": "string", "description": "备注（可省）"},
            }, "required": ["date", "exercise"]},
            fn=_log_workout,
        ),
        "log_meal": Tool(
            name="log_meal",
            description="记一顿里的一个食物。热量和宏量都可以留空 —— "
                        "**留空是「不知道」，不要填 0**，那会让当天的合计变成一个很自信的错数。"
                        "用户说了份量（「150 克鸡胸肉」）就先 search_food 查每 100 单位的数值、"
                        "自己换算好再记，并把 grams 一起写下来。",
            input_schema={"type": "object", "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "meal": {"type": "string", "description": "餐次：" + "、".join(health.MEALS)},
                "food": {"type": "string", "description": "食物名称"},
                "grams": {"type": "number", "description": "吃了多少克/毫升（可省）"},
                "kcal": {"type": "number", "description": "热量 kcal（可省）"},
                "protein": {"type": "number", "description": "蛋白质 g（可省）"},
                "carbs": {"type": "number", "description": "碳水 g（可省）"},
                "fat": {"type": "number", "description": "脂肪 g（可省）"},
            }, "required": ["date", "meal", "food"]},
            fn=_log_meal,
        ),
        "search_food": Tool(
            name="search_food",
            description="查食物库里某个食物每 100 单位的热量和宏量（用户说「我吃了 150 克 X」时"
                        "先用它查、再自己换算：数值 × 克数 ÷ 100）。"
                        "**查不到就说查不到，不要凭印象报热量。**",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "description": "食物名的一部分，如「鸡胸」"},
                "category": {"type": "string", "description": "只看某一类（可省）："
                             + "、".join(health.FOOD_CATEGORIES)},
            }, "required": ["query"]},
            fn=_foods,
        ),
        "set_target": Tool(
            name="set_target",
            description="设每日营养目标（热量和三个宏量）。这就是「定个每天吃多少的目标」"
                        "这个动作本身 —— 定完页面上那个环才有基准，所以不要只在回复里写一串数字。"
                        "四个都不填 = 清空目标。",
            input_schema={"type": "object", "properties": {
                "kcal": {"type": "number", "description": "每天热量 kcal（可省）"},
                "protein": {"type": "number", "description": "每天蛋白质 g（可省）"},
                "carbs": {"type": "number", "description": "每天碳水 g（可省）"},
                "fat": {"type": "number", "description": "每天脂肪 g（可省）"},
            }},
            fn=_set_target,
        ),
        "save_plan": Tool(
            name="save_plan",
            description="把一份计划存起来（这就是「制定计划」这个动作本身，不要只在回复里写出来）。"
                        "标题一样 = 同一份计划的新版本，上一版自动归档但不会删；换了标题就是另一份计划。",
            input_schema={"type": "object", "properties": {
                "kind": {"type": "string", "enum": list(health.PLAN_KINDS),
                         "description": "goal=目标，training=训练，diet=饮食"},
                "title": {"type": "string", "description": "计划标题，如 减脂饮食"},
                "items": {"type": "array", "items": {"type": "string"},
                          "description": "可勾选的条目，一项一条，如「每天蛋白质 110g」"},
                "note": {"type": "string", "description": "这份计划的依据/背景（可省）"},
            }, "required": ["kind", "title"]},
            fn=_save_plan,
        ),
        "list_plans": Tool(
            name="list_plans",
            description="看所有计划版本和它们的条目（勾没勾）。用户问「上个月那份计划是怎么写的」"
                        "或者要对比新旧版本时用。",
            input_schema={"type": "object", "properties": {
                "kind": {"type": "string", "enum": [""] + list(health.PLAN_KINDS),
                         "description": "只看某一类（可省，默认全部）"},
            }},
            fn=_plans,
        ),
    }
