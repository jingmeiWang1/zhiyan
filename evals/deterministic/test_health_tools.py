"""The health tools the Health agent works through.

Two things are worth locking here, and neither is "it returned a string":

1. **The numbers in the answer are the numbers in the database.** A tool that
   says "练得不错" instead of naming the session is useless to the model, so
   every read is checked for the actual value it should be carrying.
2. **A missing value is never printed as 0.** The whole health application is
   built on that rule (see the header of applications/health.py); if a tool
   summarises "unknown" as a number, the agent confidently tells the user they
   ate 0 kcal.
"""

from datetime import date

from knowme.db import connect
from knowme.tools.health import make_health_tools


def _tools(tmp_path):
    conn = connect(tmp_path)
    return conn, make_health_tools(conn)


def test_overview_on_an_empty_database_says_there_is_nothing(tmp_path):
    """An agent whose first call raises would open every health conversation
    with an apology. It has to answer, and the answer has to say 没有."""
    _, tools = _tools(tmp_path)
    text = tools["health_overview"].fn()
    assert "体重：没有记录" in text
    assert "没有记录" in text and "总容量" not in text


def test_a_logged_metric_shows_up_in_the_overview_with_its_change(tmp_path):
    _, tools = _tools(tmp_path)
    tools["log_metric"].fn(date="2026-10-01", metric="体重", value=72.4)
    tools["log_metric"].fn(date="2026-10-03", metric="体重", value=71.6)

    text = tools["health_overview"].fn()
    assert "72.4kg" in text and "71.6kg" in text
    assert "-0.8kg" in text, text
    assert "共 2 天有记录" in text


def test_the_overview_only_counts_a_workout_it_can_actually_add_up(tmp_path):
    """总容量 = 组 × 次 × 重量, and a row missing any of the three is left out
    rather than treated as 0 (which would just be the same number, but the rule
    is what keeps the diet side honest too)."""
    _, tools = _tools(tmp_path)
    tools["log_workout"].fn(date="2026-10-01", exercise="深蹲", sets=5, reps=5, weight=100)
    tools["log_workout"].fn(date="2026-10-02", exercise="跑步")          # no numbers at all
    text = tools["health_overview"].fn()
    assert "2500kg" in text, text
    assert "2 天有记录，共 2 条" in text


def test_meals_written_after_the_fact_can_be_read_back(tmp_path):
    _, tools = _tools(tmp_path)
    tools["log_meal"].fn(date="2026-10-03", meal="午餐", food="鸡胸肉沙拉")
    tools["log_meal"].fn(date="2026-10-03", meal="晚餐", food="米饭",
                         kcal=250, protein=5)

    out = tools["query_meals"].fn(days=7)
    assert "鸡胸肉沙拉" in out and "米饭" in out
    assert "250" in out


def test_an_unknown_kcal_is_never_summed_as_zero(tmp_path):
    """The one that would actually mislead: one meal with no kcal on a day whose
    other meal has 250. A naive SUM says 250 and the agent reports it as the
    day's intake."""
    _, tools = _tools(tmp_path)
    tools["log_meal"].fn(date="2026-10-03", meal="午餐", food="鸡胸肉沙拉")   # no kcal
    tools["log_meal"].fn(date="2026-10-03", meal="晚餐", food="米饭", kcal=250)

    out = tools["query_meals"].fn(days=7)
    assert "热量 —" in out, out
    assert "1 顿没填热量" in out, out
    # and the tool must explain the dash, or the model reads it as "no data"
    assert "不是 0" in out


def test_a_metric_series_is_oldest_first_and_one_point_per_day(tmp_path):
    """画趋势要的是时间顺序；同一天记两次只留最后一条。"""
    _, tools = _tools(tmp_path)
    tools["log_metric"].fn(date="2026-10-02", metric="体重", value=71.0)
    tools["log_metric"].fn(date="2026-10-01", metric="体重", value=72.4)
    tools["log_metric"].fn(date="2026-10-02", metric="体重", value=70.5)   # 当天再记一次

    out = tools["query_metrics"].fn(metric="体重", days=30)
    assert "71.0" not in out, "同一天的后一条没被前一条盖掉"
    order = [line for line in out.splitlines() if line.startswith("- ")]
    assert order[0].startswith("- 2026-10-01") and order[1].startswith("- 2026-10-02"), order
    assert "70.5" in order[1] and "72.4" in order[0]


def test_bad_input_comes_back_as_a_readable_sentence(tmp_path):
    """The model reads the error and retries. A traceback in the result is a
    dead end; "能记的指标有：…" is a next step."""
    _, tools = _tools(tmp_path)
    out = tools["log_metric"].fn(date="2026-10-01", metric="体重(kg)", value=71)
    assert out.startswith("没记成")
    assert "体重(kg)" in out, "得把用户写错的那个名字原样说出来"
    assert "静息心率" in out, "并且给出可选清单，否则模型只能瞎猜"
    bad_query = tools["query_metrics"].fn(metric="不存在")
    assert bad_query.startswith("查不了") and "体重" in bad_query


def test_saving_a_plan_archives_the_previous_version_instead_of_replacing_it(tmp_path):
    _, tools = _tools(tmp_path)
    first = tools["save_plan"].fn(kind="diet", title="减脂饮食", items=["蛋白质 110g"])
    assert "v1" in first
    second = tools["save_plan"].fn(kind="diet", title="减脂饮食", items=["蛋白质 120g"])
    assert "v2" in second and "已归档" in second

    listed = tools["list_plans"].fn()
    assert "[archived]" in listed and "[active]" in listed
    assert "蛋白质 110g" in listed, "旧版本的条目还要看得见"


def test_list_plans_can_be_filtered_by_kind(tmp_path):
    _, tools = _tools(tmp_path)
    tools["save_plan"].fn(kind="diet", title="减脂饮食", items=["a"])
    tools["save_plan"].fn(kind="training", title="增力", items=["b"])
    only_diet = tools["list_plans"].fn(kind="diet")
    assert "减脂饮食" in only_diet and "增力" not in only_diet


def test_health_tools_are_registered_for_everyone(tmp_path):
    """build_registry must actually wire them: a tool the agent's whitelist
    names but the registry never built is an 'unknown tool' at runtime, and the
    whitelist test would still be green."""
    from knowme.config import Settings
    from knowme.tools import build_registry

    settings = Settings(home=tmp_path / "reg")
    settings.ensure_home()
    registry = build_registry(connect(settings.home), settings, None)
    built = {schema["name"] for schema in registry.schemas()}
    for name in ("health_overview", "query_metrics", "query_workouts", "query_meals",
                 "log_metric", "log_workout", "log_meal", "search_food", "set_target",
                 "save_plan", "list_plans"):
        assert name in built, f"{name} 没被注册进 registry"


def test_search_food_gives_the_per_100_values_the_model_has_to_do_the_math_on(tmp_path):
    """"150 克鸡胸肉" has to become a number somehow. The tool hands over the
    per-100g row and the conversion rule; it does not do the eating for them."""
    _, tools = _tools(tmp_path)
    out = tools["search_food"].fn(query="鸡胸")
    assert "鸡胸肉" in out
    assert "每 100g" in out and "133" in out
    assert "× 克数 ÷ 100" in out, "换算规则要写出来，否则模型会自己编一个数"


def test_search_food_says_it_does_not_know_instead_of_guessing(tmp_path):
    _, tools = _tools(tmp_path)
    out = tools["search_food"].fn(query="我妈做的红烧肉")
    assert "没有" in out
    assert "不要凭印象报热量" in out


def test_set_target_is_the_thing_that_makes_the_ring_have_a_baseline(tmp_path):
    _, tools = _tools(tmp_path)
    out = tools["set_target"].fn(kcal=2280, protein=140)
    assert "2280 kcal" in out and "140g" in out
    # 没设的那两项报 —（不是 0）：这条规矩和整个应用一致，也让模型知道
    # "碳水这项没设"而不是"目标是 0 克碳水"。
    assert "碳水 —" in out and "脂肪 —" in out, out
    assert "清空" in tools["set_target"].fn()
    assert "还没" in _tools(tmp_path)[1]["health_overview"].fn()


def test_the_overview_answers_how_much_is_left_today(tmp_path):
    """"我今天还能吃多少" is the question the 30-day summary cannot answer."""
    _, tools = _tools(tmp_path)
    tools["set_target"].fn(kcal=2000, protein=150)
    tools["log_meal"].fn(date=date.today().isoformat(),
                         meal="午餐", food="鸡胸肉", grams=150,
                         kcal=199.5, protein=46.5, carbs=0, fat=5.4)

    text = tools["health_overview"].fn()
    assert "今天：目标 2000 kcal" in text, text
    assert "已摄入 199.5 kcal" in text
    assert "还剩 1800.5" in text
    assert "150g 鸡胸肉" in text or "蛋白目标 150g" in text
    assert "已摄入 46.5g" in text


def test_the_overview_refuses_to_invent_a_remaining_number(tmp_path):
    """One meal with no kcal ⇒ 已摄入 is unknown ⇒ 剩余 is unknown too. Printing
    "还剩 2000" would be the confident-wrong-number failure this app is built
    against."""
    _, tools = _tools(tmp_path)
    tools["set_target"].fn(kcal=2000)
    tools["log_meal"].fn(date=date.today().isoformat(),
                         meal="午餐", food="外卖")
    text = tools["health_overview"].fn()
    assert "算不出来" in text and "不是 0" in text
    assert "还剩" not in text
