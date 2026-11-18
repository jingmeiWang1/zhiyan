"""健康数据的存取：能存能读、坏输入进不去、计划有版本。

这一层是纯函数加 SQLite，不碰网络也不碰页面，所以这里能钉住的都是"数据本身
对不对"——合计不能把"不知道"算成 0、历史版本不能被勾动、同一个指标同一天
只算一条。
"""

import pytest

from knowme.applications.health import (
    METRICS,
    active_plans,
    add_food,
    add_meal,
    add_metric,
    add_workout,
    archive_plan,
    daily_totals,
    delete_food,
    delete_record,
    get_target,
    list_foods,
    list_meals,
    list_metrics,
    list_plans,
    list_workouts,
    meal_totals,
    metric_series,
    save_plan,
    set_target,
    toggle_item,
    update_food,
    workout_volume,
)
from knowme.db import connect


def test_tables_are_created_lazily_and_the_db_is_untouched_until_used(tmp_path):
    conn = connect(tmp_path)
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'health_%'").fetchall()
    list_metrics(conn)          # 第一次用到才建表
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'health_%'")}
    assert names == {"health_metrics", "health_workouts", "health_meals", "health_plans",
                     "health_targets", "health_foods"}


def test_a_metric_round_trips_with_its_unit(tmp_path):
    conn = connect(tmp_path)
    add_metric(conn, date="2026-10-01", metric="体重", value="72.4", note="早上称的")
    add_metric(conn, date="2026-10-03", metric="体重", value=71.8)
    add_metric(conn, date="2026-10-03", metric="体脂", value=18)

    weights = list_metrics(conn, metric="体重")
    assert [w["value"] for w in weights] == [71.8, 72.4]      # 新的在前
    assert weights[0]["unit"] == "kg"                        # 单位来自 METRICS 表
    assert weights[1]["note"] == "早上称的"
    # 全部指标：按日期倒序，同一天按录入顺序倒过来（最近记的在前）
    assert [p["metric"] for p in list_metrics(conn)] == ["体脂", "体重", "体重"]


def test_a_metric_series_is_oldest_first_and_one_point_per_day(tmp_path):
    conn = connect(tmp_path)
    add_metric(conn, date="2026-10-03", metric="体重", value=71.0)
    add_metric(conn, date="2026-10-01", metric="体重", value=73.0)
    add_metric(conn, date="2026-10-03", metric="体重", value=71.8)   # 当天晚些时候再称
    add_metric(conn, date="2026-10-02", metric="体脂", value=18)      # 另一个指标不掺进来

    series = metric_series(list_metrics(conn, metric="体重"), "体重")
    assert [p["date"] for p in series] == ["2026-10-01", "2026-10-03"]
    assert series[-1]["value"] == 71.8       # 同一天以最后记的那条为准


def test_a_bad_value_is_refused_instead_of_stored(tmp_path):
    conn = connect(tmp_path)
    with pytest.raises(ValueError, match="数字"):
        add_metric(conn, metric="体重", value="七十公斤")
    with pytest.raises(ValueError, match="没有"):
        add_metric(conn, metric="身高", value=180)       # 不在 METRICS 里的指标
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        add_metric(conn, date="昨天", metric="体重", value=70)
    assert list_metrics(conn) == [], "坏输入一个字都不该进库"


def test_a_missing_date_defaults_to_today_not_to_a_guess(tmp_path):
    from datetime import date
    conn = connect(tmp_path)
    add_metric(conn, metric="体重", value=70)
    assert list_metrics(conn)[0]["date"] == date.today().isoformat()


def test_workout_volume_only_counts_rows_that_have_all_three_numbers(tmp_path):
    conn = connect(tmp_path)
    add_workout(conn, date="2026-10-01", exercise="深蹲", sets=5, reps=5, weight=100)
    add_workout(conn, date="2026-10-01", exercise="卧推", sets=4, reps=8)      # 没写重量
    assert workout_volume(list_workouts(conn)) == 2500.0
    assert list_workouts(conn, exercise="卧推")[0]["weight"] is None


def test_workout_rejects_a_fractional_set_count(tmp_path):
    conn = connect(tmp_path)
    with pytest.raises(ValueError, match="正整数"):
        add_workout(conn, exercise="深蹲", sets="3.5", reps=5, weight=100)


def test_daily_totals_do_not_turn_unknown_kcal_into_zero(tmp_path):
    conn = connect(tmp_path)
    add_meal(conn, date="2026-10-01", meal="早餐", food="燕麦", kcal=350, protein=12)
    add_meal(conn, date="2026-10-01", meal="午餐", food="米饭")
    add_meal(conn, date="2026-10-02", meal="早餐", food="鸡蛋", kcal=140, protein=12)

    totals = {t["date"]: t for t in daily_totals(conn)}
    assert totals["2026-10-01"]["kcal"] is None, "有一顿不知道热量，这天就不能给总数"
    assert totals["2026-10-01"]["kcal_unknown"] == 1
    # 宏量和热量是**同一条规矩**：那一顿没写蛋白，当天的蛋白就是算不出来。
    # （以前这里断言 12.0 —— "填了的宏量照样算"。那个数看着很确定，但它是错的：
    #  同一顿还有一碗米饭没写蛋白，报 12 就是在一句话里混进了未知的一半。
    #  改成"有一行没填就不出数"以后，热量和宏量的判断只剩一处。）
    assert totals["2026-10-01"]["protein"] is None, "有一顿没填蛋白，这天也不能给总数"
    assert totals["2026-10-02"]["kcal"] == 140.0
    assert totals["2026-10-02"]["protein"] == 12.0, "全都填了才出数"
    assert [m["food"] for m in list_meals(conn)] == ["鸡蛋", "米饭", "燕麦"]


def test_saving_a_plan_again_makes_a_new_version_and_archives_the_old_one(tmp_path):
    conn = connect(tmp_path)
    save_plan(conn, kind="diet", title="减脂饮食", items=["早饭吃够蛋白", "晚饭少吃碳水"])
    second = save_plan(conn, kind="diet", title="减脂饮食", items=["早饭 40g 蛋白"])

    assert second["version"] == 2
    assert [p["version"] for p in list_plans(conn, kind="diet")] == [2, 1]
    assert [p["version"] for p in active_plans(conn)] == [2]
    old = next(p for p in list_plans(conn) if p["version"] == 1)
    assert old["status"] == "archived"
    assert [i["text"] for i in old["items"]] == ["早饭吃够蛋白", "晚饭少吃碳水"], "旧版本原样留着"


def test_a_different_title_starts_a_separate_line(tmp_path):
    conn = connect(tmp_path)
    save_plan(conn, kind="diet", title="减脂饮食", items=["a"])
    other = save_plan(conn, kind="diet", title="增肌饮食", items=["b"])
    assert other["version"] == 1
    assert len(active_plans(conn)) == 2


def test_toggling_an_item_changes_only_the_active_version(tmp_path):
    conn = connect(tmp_path)
    first = save_plan(conn, kind="training", title="力量周计划", items=["周一推", "周三拉"])
    toggle_item(conn, first["id"], 1, True)
    assert [i["done"] for i in list_plans(conn)[0]["items"]] == [0, 1]

    second = save_plan(conn, kind="training", title="力量周计划", items=["周一推", "周三拉"])
    toggle_item(conn, second["id"], 0, True)
    archived = next(p for p in list_plans(conn) if p["version"] == 1)
    assert [i["done"] for i in archived["items"]] == [0, 1], "历史版本保持原样"


def test_an_archived_version_cannot_be_toggled(tmp_path):
    conn = connect(tmp_path)
    first = save_plan(conn, kind="training", title="力量周计划", items=["周一推"])
    save_plan(conn, kind="training", title="力量周计划", items=["周一推"])
    with pytest.raises(ValueError, match="历史版本"):
        toggle_item(conn, first["id"], 0, True)
    with pytest.raises(ValueError, match="没有这一条"):
        toggle_item(conn, list_plans(conn)[0]["id"], 9, True)


def test_plan_items_accept_plain_strings_and_only_keep_non_empty_ones(tmp_path):
    conn = connect(tmp_path)
    plan = save_plan(conn, kind="goal", title="今年目标", items=["体脂到 15%", "  ", ""])
    assert [i["text"] for i in plan["items"]] == ["体脂到 15%"]
    assert plan["kind_label"] == "目标"


def test_archive_and_delete_report_what_they_did(tmp_path):
    conn = connect(tmp_path)
    plan = save_plan(conn, kind="goal", title="今年目标", items=["体脂到 15%"])
    assert archive_plan(conn, plan["id"])["status"] == "archived"
    assert active_plans(conn) == []

    add_metric(conn, metric="体重", value=70)
    metric_id = list_metrics(conn)[0]["id"]
    assert delete_record(conn, "metric", metric_id) is True
    assert delete_record(conn, "metric", metric_id) is False, "删过了就说没删到"
    with pytest.raises(ValueError, match="不知道要删什么"):
        delete_record(conn, "soul", 1)


def test_every_selectable_metric_has_a_unit(tmp_path):
    assert all(METRICS.values()), "下拉框里的每个指标都得有单位"


# ---- 每日目标 -------------------------------------------------------------
def test_a_target_is_one_row_that_gets_overwritten(tmp_path):
    conn = connect(tmp_path)
    never = get_target(conn)
    assert never == {"kcal": None, "protein": None, "carbs": None, "fat": None,
                     "updated_at": None}, "没设过要回四个 None，不是 None"

    set_target(conn, kcal=2280, protein=140, carbs=230, fat=70)
    assert get_target(conn)["kcal"] == 2280
    set_target(conn, kcal=2000, protein=150)          # 再设一次是覆盖，不是新增
    target = get_target(conn)
    assert target["kcal"] == 2000 and target["protein"] == 150
    assert target["carbs"] is None, "这次没填的项就是没设，不保留上一次的值"
    assert conn.execute("SELECT COUNT(*) FROM health_targets").fetchone()[0] == 1


def test_clearing_every_field_means_no_target(tmp_path):
    conn = connect(tmp_path)
    set_target(conn, kcal=2280)
    set_target(conn)                                   # 四个都留空 = 取消目标
    assert get_target(conn)["kcal"] is None


def test_a_bad_target_number_is_refused(tmp_path):
    conn = connect(tmp_path)
    with pytest.raises(ValueError, match="目标热量"):
        set_target(conn, kcal="两千")
    assert get_target(conn)["kcal"] is None, "坏输入一个字都不该进库"


# ---- 食物库 ---------------------------------------------------------------
def test_the_food_library_is_seeded_once_and_only_when_empty(tmp_path):
    conn = connect(tmp_path)
    foods = list_foods(conn)
    assert len(foods) > 80, "内置的食物库得有足够的东西可选"
    assert any(f["name"] == "鸡胸肉" for f in foods)
    assert all(f["custom"] == 0 for f in foods), "种进去的都是内置的"

    # 再读一次不会翻倍（种子只种空表；/api/data 每 5 秒会读一次这条路径）
    assert len(list_foods(conn)) == len(foods)


def test_a_food_can_be_filtered_by_category_and_by_name(tmp_path):
    conn = connect(tmp_path)
    list_foods(conn)                                   # 先让它种进去
    meat = list_foods(conn, category="肉蛋奶")
    assert meat and all(f["category"] == "肉蛋奶" for f in meat)
    assert [f["name"] for f in list_foods(conn, query="鸡胸")] == ["鸡胸肉"]
    assert list_foods(conn, category="肉蛋奶", query="不存在的食物") == []


def test_a_custom_food_sorts_first_and_a_duplicate_name_is_refused(tmp_path):
    conn = connect(tmp_path)
    list_foods(conn)
    mine = add_food(conn, name="我妈做的红烧肉", category="肉蛋奶", kcal=380, protein=14)
    assert mine["custom"] == 1
    assert list_foods(conn)[0]["name"] == "我妈做的红烧肉", "自己刚建的排最前面"
    with pytest.raises(ValueError, match="已经有一个"):
        add_food(conn, name="我妈做的红烧肉", category="肉蛋奶")


def test_editing_a_builtin_food_keeps_it_builtin(tmp_path):
    conn = connect(tmp_path)
    food = next(f for f in list_foods(conn) if f["name"] == "鸡胸肉")
    assert food["kcal"] == 133
    changed = update_food(conn, food["id"], name="鸡胸肉", category="肉蛋奶", unit="g",
                          kcal=165, protein=31, carbs=0, fat=3.6)
    assert changed["kcal"] == 165
    assert changed["custom"] == 0, "改过的种子仍然是种子（它只是被我调过）"


def test_deleting_a_food_leaves_the_meals_already_logged_alone(tmp_path):
    conn = connect(tmp_path)
    food = next(f for f in list_foods(conn) if f["name"] == "鸡胸肉")
    add_meal(conn, date="2026-10-01", meal="午餐", food="鸡胸肉", grams=150,
             kcal=199.5, protein=46.5)
    assert delete_food(conn, food["id"]) is True
    assert delete_food(conn, food["id"]) is False, "删过了就说没删到"
    assert not any(f["name"] == "鸡胸肉" for f in list_foods(conn))
    logged = list_meals(conn)[0]
    assert logged["food"] == "鸡胸肉" and logged["kcal"] == 199.5, "记过的那顿饭动不了"


def test_a_bad_food_is_refused_instead_of_stored(tmp_path):
    conn = connect(tmp_path)
    with pytest.raises(ValueError, match="分类"):
        add_food(conn, name="神秘食物", category="天上掉的")
    with pytest.raises(ValueError, match="单位"):
        add_food(conn, name="神秘食物", category="其他", unit="斤")
    with pytest.raises(ValueError, match="食物名"):
        add_food(conn, name="  ", category="其他")
    assert not any(f["name"] == "神秘食物" for f in list_foods(conn))


# ---- 每餐小计 / grams -----------------------------------------------------
def test_meal_totals_are_per_day_per_meal_with_the_same_unknown_rule(tmp_path):
    conn = connect(tmp_path)
    add_meal(conn, date="2026-10-01", meal="早餐", food="燕麦", kcal=350, protein=12,
             carbs=60, fat=6)
    add_meal(conn, date="2026-10-01", meal="早餐", food="鸡蛋", kcal=144, protein=13,
             carbs=3, fat=9)
    add_meal(conn, date="2026-10-01", meal="午餐", food="米饭")
    add_meal(conn, date="2026-10-02", meal="早餐", food="鸡蛋", kcal=144, protein=13,
             carbs=3, fat=9)

    by_key = {(t["date"], t["meal"]): t for t in meal_totals(conn)}
    breakfast = by_key[("2026-10-01", "早餐")]
    assert breakfast["kcal"] == 494.0 and breakfast["items"] == 2
    assert breakfast["protein"] == 25.0
    lunch = by_key[("2026-10-01", "午餐")]
    assert lunch["kcal"] is None and lunch["kcal_unknown"] == 1, "不知道就不是 0"
    assert lunch["protein"] is None


def test_a_meal_keeps_the_grams_it_was_logged_with(tmp_path):
    conn = connect(tmp_path)
    add_meal(conn, date="2026-10-01", meal="午餐", food="鸡胸肉", grams=150, kcal=199.5)
    add_meal(conn, date="2026-10-01", meal="午餐", food="外卖", kcal=800)   # 没称过
    rows = {m["food"]: m for m in list_meals(conn)}
    assert rows["鸡胸肉"]["grams"] == 150
    assert rows["外卖"]["grams"] is None, "没写份量就是不知道，不是 0"


def test_an_old_database_gets_the_grams_column_added_without_losing_rows(tmp_path):
    """这个文件第一次做迁移：给已经在用的 health_meals 补上 grams 列。

    建表那句 CREATE TABLE IF NOT EXISTS 对老库是空操作 —— 表已经在了，
    新列不会自己出现。所以拿一张"没有 grams 的老表"来喂它。
    """
    conn = connect(tmp_path)
    conn.execute("""CREATE TABLE health_meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, meal TEXT NOT NULL,
        food TEXT NOT NULL, kcal REAL, protein REAL, carbs REAL, fat REAL,
        note TEXT DEFAULT '', created_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO health_meals (date, meal, food, kcal, created_at) "
                 "VALUES ('2026-09-01','午餐','老数据',500,'2026-09-01T00:00:00+00:00')")
    conn.commit()

    rows = list_meals(conn, days=0)      # 第一次用到 ⇒ 补列（days=0 = 不限时间）
    assert rows[0]["food"] == "老数据", "老行还在"
    assert rows[0]["kcal"] == 500
    assert rows[0]["grams"] is None, "老行没有份量 = 不知道，不是 0"
