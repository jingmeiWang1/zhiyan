"""The 健康 · 训练 page: keep what you are typing, write for real, and never
print a number it does not have.

Four rules, and none of them is visible in a screenshot:

  1. The 5 second poll must NOT rebuild the view while one of the entry forms
     has focus. Every input on this page carries onfocus="markEditing()", and
     render()'s health branch skips the rebuild while `editing` is true -- the
     same trap the Reader, the conversation and the knowledge base each fell
     into once.
  2. A write MUST rebuild it, and so must MOVING THE DATE. The lists, the day
     totals and the ring are baked into the view markup, so a saved row (or a
     day the user stepped back to) that does not trigger a repaint looks exactly
     like a click that did nothing. That is why those actions end with
     `editing = false; refresh()` -- dropping the `editing = false` would leave
     the guard holding the old page in place forever.
  3. Repainting ONE BOX must not go through refresh(). Picking a trend metric,
     filtering the food list and recomputing grams all redraw a fragment; a
     whole-page refresh would wipe the half-typed form next to them.
  4. "不知道 ≠ 0". A day whose calories were not filled in has an UNKNOWN total
     -- the ring must draw no arc and say so, not draw a confident percentage
     over a number that isn't there.

Beyond control flow the harness pins the wire shape: which ids the form reads,
and what the POST bodies look like. Those are the contract with
applications/health.py (see test_health_store.py for the other half).

Like the Reader's and the knowledge base's locks, this runs the REAL health.js
against a DOM stub in node. node is a hard requirement of this file, so skip
without it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = (Path(__file__).resolve().parents[2]
          / "knowme" / "ops" / "static")
JS = STATIC / "js"
MAIN_JS = JS / "main.js"
HEALTH_JS = JS / "health.js"
VIEWS_JS = JS / "views.js"
BOOTSTRAP_JS = JS / "bootstrap.js"
INDEX_HTML = STATIC / "index.html"

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");        // main.js: the loop
const healthSrc = fs.readFileSync(process.argv[3], "utf8");  // health.js: the app
const viewsSrc = fs.readFileSync(process.argv[4], "utf8");   // views.js: subtabBar
const bootstrapSrc = fs.readFileSync(process.argv[5], "utf8");
const htmlSrc = fs.readFileSync(process.argv[6], "utf8");
let failures = 0;
const assert = (ok, msg) => {
  if (!ok) failures++;
  console.log((ok ? "PASS  " : "FAIL  ") + msg);
};
// The one escape the page uses for user text. Real, so a stray unescaped value
// shows up here rather than as broken markup in the browser.
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- render(): when does the poll rebuild the health page? ------------------
// The slice is the same one four other frontend evals lift out of main.js --
// from the setCount helper down to `let lastFetch`. Anything render() reads has
// to be inside the window, and `editing` is declared by the harness (main.js
// declares it at the top of the file, outside the slice).
(function testRenderGuard() {
  const renderFn = src.slice(src.indexOf("function setCount("),
                             src.indexOf("\nlet lastFetch"));
  let activeView = null, activeSub = null, animating = false, editing = false;
  const els = {};
  const document = {
    querySelectorAll: () => [],
    querySelector: () => ({ scrollTop: 0 }),
    getElementById: id => (els[id] = els[id] || { id, textContent: "", innerHTML: "", style: {} }),
  };
  const window = { addEventListener() {}, getSelection: () => ({ toString: () => "" }) };
  const location = { hash: "#health/diary" };
  const VIEWS = { health: () => "<health>", overview: () => "<overview>" };
  const TITLES = { health: "健康 · 训练" };
  const D = { provider: "p", model: "m", chat_log: [], stats: { turns: 0, tool_errors: 0 },
              graph: { stats: { quick: 0, full: 0 } }, facts: [], episodes: [],
              calendar: [], outbox: [], db: { all_tables: [] }, home: "H",
              agents: [], current_sessions: {} };
  let restoreCalls = 0, wireCalls = 0;
  function wireChat() { wireCalls++; }
  function syncAgentChrome() {}
  eval(renderFn);

  // Without VIEWS.health the hash silently falls back to 总览 -- the page never
  // appears and nothing anywhere reports an error.
  assert(VIEWS[location.hash.slice(1).split("/")[0]] !== undefined,
         "#health resolves to a real view (not the overview fallback)");

  els["view"] = { innerHTML: "", style: {} };
  render();
  assert(els["view"].innerHTML === "<health>", "landing on #health renders the page");

  // The user clicks into the weight box; a poll arrives while they are typing.
  editing = true;
  els["view"].innerHTML = "<typed>";
  render();
  assert(els["view"].innerHTML === "<typed>",
         "a poll does NOT rebuild while a form on the page has focus");

  render(); render();
  assert(els["view"].innerHTML === "<typed>", "repeated polls still leave the form alone");

  // A write clears `editing` before calling refresh(), which is what makes the
  // new row show up. Same render(), editing now false.
  editing = false;
  render();
  assert(els["view"].innerHTML === "<health>",
         "a write (editing cleared) repaints the page the guard was holding back");

  // Switching sub-tabs is navigation, not a poll: it rebuilds even mid-typing.
  editing = true;
  location.hash = "#health/plans";
  render();
  assert(els["view"].innerHTML === "<health>", "switching sub-tabs rebuilds despite editing");
})();

// ---- the page itself, driven end to end against a DOM stub ------------------
// The real health.js, not a function sliced out of it: which ids the form reads
// and what the POST body looks like are the contract with the server.
const pendingTests = [];
pendingTests.push((function testHealthPage() {
  const els = {};
  function el(id) {
    if (els[id]) return els[id];
    const node = { id, _v: "", innerHTML: "", textContent: "", style: {}, dataset: {},
                   focus() {} };
    // A real input's .value is ALWAYS a string. hpRecalc() writes numbers into
    // those boxes, so a stub that keeps the number would fail hpValue()'s
    // .trim() on something the browser never does.
    Object.defineProperty(node, "value", {
      get: () => node._v, set: nv => { node._v = String(nv); } });
    return els[id] = node;
  }
  const document = {
    getElementById: id => els[id] || null,
    querySelectorAll: () => [],
    querySelector: () => ({ scrollTop: 0 }),
  };
  // subtabBar is real (sliced out of views.js): the five tabs are markup the app
  // and the user both depend on, and hand-writing it here would let a rename in
  // views.js pass.
  eval(viewsSrc.slice(viewsSrc.indexOf("function subtabBar("),
                      viewsSrc.indexOf("\nfunction dbTable(")));
  const alert = m => { throw new Error("unexpected alert: " + m); };
  const confirm = () => true;
  let editing = false;
  let ACTIVE_AGENT = "health";
  // The server, recorded.
  const calls = [];
  let refreshCalls = 0;
  function refresh() { refreshCalls++; }
  const postJSON = async (url, body) => {
    calls.push({ url, ...body });
    if (body.action === "plan") return { ok: true, plan: { id: body.plan_id, slug: "goal/x",
      version: 1, kind: "goal", kind_label: "目标", title: "x", status: "archived",
      created_at: "2026-10-01T09:00:00", items: [{ text: "a", done: true }] } };
    return { ok: true, data: { foods: body.name ? [{ id: 901, name: body.name, unit: "g",
      category: "其他", kcal: 100, protein: 1, carbs: 2, fat: 3, custom: 1 }] : [] } };
  };
  let D = null;
  const VIEWS = {};
  eval(healthSrc);

  assert(typeof VIEWS.health === "function", "health.js installs VIEWS.health");
  assert(!/VIEWS\.health\s*=/.test(viewsSrc),
         "…and views.js does not carry a second copy (that one would silently lose)");

  // --- the fixture the server would hand over -------------------------------
  const FOODS = [
    { id: 11, name: "鸡胸肉", category: "肉蛋奶", unit: "g", kcal: 133, protein: 31,
      carbs: 0, fat: 3.6, custom: 0 },
    { id: 12, name: "米饭（熟）", category: "主食", unit: "g", kcal: 116, protein: 2.6,
      carbs: 25.9, fat: 0.3, custom: 0 },
    { id: 13, name: "牛奶（全脂）", category: "肉蛋奶", unit: "ml", kcal: 54, protein: 3,
      carbs: 3.4, fat: 3.2, custom: 0 },
    { id: 14, name: "我妈做的红烧肉", category: "其他", unit: "g", kcal: null, protein: null,
      carbs: null, fat: null, custom: 1 },
  ];
  const info = {
    metric_options: [{ name: "体重", unit: "kg" }, { name: "体脂", unit: "%" }],
    meal_slots: ["早餐", "午餐", "晚餐", "加餐"],
    plan_kinds: [{ key: "goal", label: "目标" }],
    food_categories: ["主食", "肉蛋奶", "蔬菜", "水果", "饮品", "零食", "其他"],
    windows: { meals: 30, workouts: 60 },
    metrics: [
      { id: 2, date: "2026-10-03", metric: "体重", value: 71.2, unit: "kg", note: "" },
      { id: 1, date: "2026-10-01", metric: "体重", value: 72.0, unit: "kg", note: "" },
    ],
    workouts: [], meals: [], plans: [], versions: [], totals: [], meal_totals: [],
    target: { kcal: null, protein: null, carbs: null, fat: null, updated_at: null },
    foods: FOODS,
  };
  D = { health_info: info };

  // --- five sub-tabs, and the diary is the one you land on ------------------
  const tabs = ["diary", "training", "metrics", "plans", "foods"];
  const diaryHtml = VIEWS.health({ health_info: info });
  for (const key of tabs)
    assert(diaryHtml.includes('href="#health/' + key + '"'),
           "the page offers the " + key + " sub-tab");
  assert(diaryHtml.includes("hp-daybar") && diaryHtml.includes("hp-ring"),
         "with no sub-key given the page opens on the diary (date bar + energy ring)");
  assert(diaryHtml.includes("openAgent('health')"),
         "the page hands off to the health agent (it has no chat panel of its own)");

  assert(VIEWS.health({ health_info: info }, "training").includes("还没有训练记录"),
         "an empty workout list says so");
  assert(VIEWS.health({ health_info: info }, "plans").includes("还没有生效的计划"),
         "with no plan saved the page says so");
  assert(VIEWS.health({ health_info: info }, "metrics").includes("71.2"),
         "the metric list shows the recorded values");
  assert(VIEWS.health({ health_info: info }, "metrics").includes("<polyline"),
         "two points on the same metric draw a trend line");

  // The day bar knows how far back the data goes, and says where the edge is --
  // "not loaded" and "you ate nothing" must not look the same.
  assert(diaryHtml.includes("饮食只加载最近 30 天"),
         "the date bar names the window it can step back through");

  // --- the ring: three explicit degradations, none of them accidental -------
  assert(diaryHtml.includes("还没设目标") && !diaryHtml.includes("hp-ring-arc"),
         "no target ⇒ no arc at all (drawing one would divide by a missing number)");
  // "你还没记" and "记了但没填热量" are two different sentences, and neither one
  // is allowed to come out as 0%.
  const nothing = hpRingBox({ ...info, target: { ...info.target, kcal: 2000 } }, "2026-10-05");
  assert(nothing.includes("还没有记录") && !nothing.includes("hp-ring-arc"),
         "a target with no meals logged that day says 还没有记录, not 0%");

  const half = hpRingBox({ ...info, target: { ...info.target, kcal: 2000 },
    totals: [{ date: "2026-10-05", items: 3, kcal: 1000, kcal_unknown: 0,
               protein: 90, carbs: 120, fat: 40 }] }, "2026-10-05");
  assert(half.includes('stroke-dashoffset="163.4"'),
         "the arc's dashoffset encodes eaten/target (1000/2000 ⇒ half of 2πr)");
  assert(half.includes("50%"), "…and the number in the middle is that same ratio");
  assert(half.includes("<b>1000</b>") && half.includes("<b>2000</b>"),
         "已摄入 and 目标 are the real numbers, not the ratio");
  assert(/剩余<\/span>\s*<b>1000<\/b>/.test(half), "剩余 is target − eaten");

  const over = hpRingBox({ ...info, target: { ...info.target, kcal: 2000 },
    totals: [{ date: "2026-10-05", items: 4, kcal: 2400, kcal_unknown: 0,
               protein: 140, carbs: 210, fat: 70 }] }, "2026-10-05");
  assert(over.includes("hp-ring over") && over.includes("hp-ring-num over"),
         "over the target the arc fills and both it and the number go to --bad");

  // 不知道 ≠ 0: one unfilled meal makes the day's total unknown, and the ring
  // must refuse to draw a percentage rather than draw a wrong one.
  const unknown = hpRingBox({ ...info, target: { ...info.target, kcal: 2000 },
    totals: [{ date: "2026-10-05", items: 2, kcal: null, kcal_unknown: 1,
               protein: null, carbs: null, fat: null }] }, "2026-10-05");
  assert(unknown.includes("算不出来") && !unknown.includes("hp-ring-arc"),
         "an unknown day total draws no arc and says 算不出来 (not 0)");
  assert(unknown.includes("1 顿没填热量"), "…and says how many meals are missing");
  assert(/剩余<\/span>\s*<b>—<\/b>/.test(unknown),
         "…and the 剩余 cell is — rather than a confident number");

  // The ring deliberately has no "burned" row: there is no defensible source
  // for exercise calories, and a made-up number is worse than a missing one.
  assert(!diaryHtml.includes("已消耗"),
         "the ring does not show 已消耗 (no honest data source for it)");

  // --- 记一笔: pick a food, type grams, the four numbers follow -------------
  hpPickFood(11);
  const pending = hpPickPanel(info);
  assert(pending.includes("鸡胸肉") && pending.includes("记到这一天"),
         "picking a food shows the pending row");
  assert(!calls.some(c => c.action === "add_meal"),
         "…and writes nothing yet (picking is not logging)");
  // The four boxes exist in the real DOM (the pick row's markup); the stub has
  // to be told about them, or hpRecalc has nothing to write into.
  ["kcal", "protein", "carbs", "fat", "grams"].forEach(k => el("hp-pick-" + k));
  el("hp-pick-grams").value = "150";
  hpRecalc();
  assert(el("hp-pick-kcal").value === "199.5" && el("hp-pick-protein").value === "46.5"
         && el("hp-pick-carbs").value === "0" && el("hp-pick-fat").value === "5.4",
         "150g of a 133kcal/100g food fills in 199.5 / 46.5 / 0 / 5.4");
  // …and a food whose values were left blank keeps them blank, not 0.
  hpPickFood(14);
  el("hp-pick-grams").value = "200";
  hpRecalc();
  assert(el("hp-pick-kcal").value === "" ,
         "a food with no kcal recorded computes to empty, not to 0");

  hpPickFood(11);
  el("hp-pick-grams").value = "150";
  hpRecalc();
  el("hp-pick-meal").value = "午餐";
  editing = true;                       // the user had focused the form
  const beforePost = refreshCalls;
  return hpSavePick().then(() => {
    const call = calls.find(c => c.action === "add_meal");
    assert(!!call, "保存 posts action=add_meal");
    assert(call.food === "鸡胸肉" && call.grams === "150" && call.meal === "午餐",
           "…with the food, the grams and the meal slot");
    assert(call.kcal === "199.5" && call.protein === "46.5" && call.fat === "5.4",
           "…and the four computed numbers as text (the same ones the boxes show)");
    assert(/^\d{4}-\d{2}-\d{2}$/.test(call.date), "…dated by the day the diary is showing");
    assert(editing === false,
           "…and clears `editing`, or the poll guard keeps the old page");
    assert(refreshCalls === beforePost + 1, "…and repaints so the row appears");

    // A workout reads its own fields.
    el("hp-w-date").value = "2026-10-04";
    el("hp-w-exercise").value = "深蹲";
    el("hp-w-sets").value = "5"; el("hp-w-reps").value = "5";
    el("hp-w-weight").value = "100"; el("hp-w-rpe").value = "8";
    return healthAdd("workout");
  }).then(() => {
    const call = calls.find(c => c.action === "add_workout");
    assert(!!call, "记下 posts action=add_workout");
    assert(call.exercise === "深蹲" && call.sets === "5" && call.reps === "5"
           && call.weight === "100" && call.rpe === "8",
           "…with the exercise and the sets × reps × weight the form holds");

    // Ticking a plan item is one call with the plan, the index and the new state.
    return healthToggle(7, 1, false);
  }).then(() => {
    const call = calls.find(c => c.action === "toggle");
    assert(call && call.plan_id === 7 && call.index === 1 && call.done === false,
           "ticking an item posts plan_id + index + done (nothing else)");

    // Saving a plan splits the textarea into items, one per line, blanks dropped.
    el("hp-p-kind").value = "goal";
    el("hp-p-title").value = "减脂";
    el("hp-p-items").value = "蛋白质 110g\n\n  每周三次训练  \n";
    return healthSavePlan();
  }).then(() => {
    const call = calls.find(c => c.action === "save_plan");
    assert(!!call, "保存为新版本 posts action=save_plan");
    assert(call.title === "减脂" && call.kind === "goal",
           "…with the kind and title the form holds");
    assert(JSON.stringify(call.items) === JSON.stringify(["蛋白质 110g", "每周三次训练"]),
           "…and one item per non-empty line, trimmed");

    // The daily target: the thing the ring measures against.
    el("hp-t-kcal").value = "2280";
    el("hp-t-protein").value = "140";
    el("hp-t-carbs").value = ""; el("hp-t-fat").value = "";
    return hpSaveTarget();
  }).then(() => {
    const call = calls.find(c => c.action === "set_target");
    assert(!!call, "保存目标 posts action=set_target");
    assert(call.kcal === "2280" && call.protein === "140",
           "…with the numbers the form holds");
    assert(call.carbs === "" && call.fat === "",
           "…and blank for the rows left empty (blank = unset, not 0)");

    return hpClearTarget();
  }).then(() => {
    const clears = calls.filter(c => c.action === "set_target");
    assert(clears.length === 2 && clears[1].kcal === undefined,
           "清空 posts set_target carrying no numbers at all (that is what clears it)");

    // The food library: an edit carries the food_id, a new row does not.
    el("hp-food-edit");
    hpEditFoodOpen(13);
    const editHtml = el("hp-food-edit").innerHTML;
    assert(editHtml.includes('value="牛奶（全脂）"') && editHtml.includes('value="54"'),
           "opening a food for editing fills the form with what is stored");
    el("hp-f-name").value = "牛奶（全脂）";
    el("hp-f-cat").value = "肉蛋奶";
    el("hp-f-unit").value = "ml";
    el("hp-f-kcal").value = "60";
    return hpSaveFood();
  }).then(() => {
    const call = calls.find(c => c.action === "update_food");
    assert(!!call && call.food_id === 13,
           "改数值 posts action=update_food carrying the food_id");
    assert(call.name === "牛奶（全脂）" && call.kcal === "60" && call.unit === "ml",
           "…with the whole row (the server replaces, it does not patch)");

    el("hp-f-name").value = "我妈做的红烧肉";
    el("hp-f-cat").value = "其他";
    el("hp-f-unit").value = "g";
    el("hp-f-kcal").value = "420";
    return hpSaveFood();
  }).then(() => {
    const call = calls.find(c => c.action === "add_food");
    assert(!!call && call.food_id === undefined,
           "新建 posts action=add_food with NO food_id (that is the server's tell)");
    assert(call.name === "我妈做的红烧肉" && call.kcal === "420",
           "…with the name and the per-100 value the form holds");

    return hpDeleteFood(12);
  }).then(() => {
    const call = calls.find(c => c.action === "delete_food");
    assert(!!call && call.food_id === 12, "删 posts action=delete_food with the food_id");
  });
})());

// ---- moving the day: it must clear `editing`, or nothing moves ---------------
(function testDayStepper() {
  const els = {};
  const document = { getElementById: id => els[id] || null, querySelector: () => null };
  const alert = m => { throw new Error("unexpected alert: " + m); };
  let editing = false;
  let refreshCalls = 0;
  function refresh() { refreshCalls++; }
  const VIEWS = {};
  eval(healthSrc);

  const info = { windows: { meals: 30, workouts: 60 }, meal_slots: ["早餐", "午餐", "晚餐", "加餐"],
                 food_categories: [], foods: [], totals: [], meal_totals: [], workouts: [],
                 metrics: [], plans: [], versions: [], target: {}, metric_options: [] };
  let D = { health_info: info };

  // render()'s guard skips the rebuild while a form has focus. If stepping back
  // a day left `editing` true, the arrow would look broken: the day changes in
  // memory and the page never shows it.
  editing = true;
  const before = refreshCalls;
  hpShiftDay(-1);
  assert(editing === false, "◀ clears `editing` (else the guard holds the old day forever)");
  assert(refreshCalls === before + 1, "…and repaints, so the new day is on screen");

  // Stepping back to the edge of the loaded window has to be visible, and the
  // button has to be the thing that stops you: past it "还没有记录" would mean
  // "not loaded", which is a different sentence from "you ate nothing".
  hpGoToday();
  for (let i = 0; i < 30; i++) hpShiftDay(-1);
  const bar = hpDiary(D.health_info);
  assert(bar.includes("再往前就没有了"),
         "at the edge of the window the date bar says so");
  assert(/<button class="hp-step" title="只保留最近 30 天"\s+disabled/.test(bar),
         "…and disables ◀ rather than silently stopping");

  // Filtering the food list repaints a fragment: it must NOT refresh (that would
  // wipe the form) and must NOT clear `editing` (that would end the guard).
  editing = true;
  const paints = refreshCalls;
  hpFoodQuery("鸡");
  hpFoodCatPick("肉蛋奶");
  assert(editing === true, "filtering the food list leaves `editing` alone");
  assert(refreshCalls === paints, "…and does not rebuild the whole page");
})();

// ---- "上次做这个动作" is a pure function, so it can be pinned --------------
(function testLastTime() {
  const document = { getElementById: () => null, querySelector: () => null };
  const VIEWS = {};
  let D = null;
  eval(healthSrc);

  const rows = [
    { id: 5, date: "2026-10-05", exercise: "深蹲", sets: 5, reps: 5, weight: 100 },
    { id: 4, date: "2026-10-01", exercise: "深蹲", sets: 5, reps: 5, weight: 95 },
    { id: 3, date: "2026-10-01", exercise: "卧推", sets: 5, reps: 5, weight: 60 },
    { id: 2, date: "2026-10-01", exercise: "深蹲", sets: null, reps: null, weight: null },
    { id: 1, date: "2026-09-20", exercise: "深蹲", sets: 3, reps: 8, weight: 80 },
  ];
  const info = { windows: { workouts: 60 }, workouts: rows };

  const last = hpLastBefore(rows, rows[0]);      // the 10-05 session
  assert(last && last.id === 4,
         "the row picked is the LATEST one before this session, same exercise");
  assert(last.exercise === "深蹲",
         "…and never another exercise (卧推 is not 深蹲's history)");
  assert(hpLastLabel(info, rows[0]) === "上次 10-01 · 5×5 @ 95kg",
         "…and the label is that session, dated and spelled out");

  // Two sessions on the same day: the lower id is the earlier set, so the one
  // after it sees the lower-id row rather than itself.
  assert(hpLastBefore(rows, rows[1]).id === 2,
         "two sessions on one day fall back to id order");
  assert(hpLastLabel(info, rows[1]) === "上次 10-01 · 没记全",
         "…and an earlier row with no numbers says 没记全 instead of printing 0s");

  assert(hpLastBefore(rows, rows[4]) === null, "the oldest row has nothing before it");
  assert(hpLastLabel(info, rows[4]) === "这 60 天里没有更早的记录",
         "nothing earlier says 这 60 天里没有更早的记录 — never 第一次做 (we only loaded 60 days)");
  assert(hpLastLabel(info, { id: 9, date: "2026-10-06", exercise: "硬拉" })
         === "这 60 天里没有更早的记录",
         "an exercise with no history at all says the same thing, not 第一次做");
})();

// ---- the shell wires the page in -------------------------------------------
(function testShellWiring() {
  const features = bootstrapSrc.slice(bootstrapSrc.indexOf("const FEATURES"),
                                      bootstrapSrc.indexOf("];", bootstrapSrc.indexOf("const FEATURES")));
  assert(/"health\.js"/.test(features), "bootstrap.js loads health.js");
  assert(features.indexOf('"health.js"') < features.indexOf('"main.js"'),
         "…before main.js (it loads last and calls refresh())");
  assert(htmlSrc.includes('data-v="health"'), "index.html has a sidebar link to #health");
  assert(/health:/.test(src.slice(src.indexOf("const TITLES"), src.indexOf("TITLES.deepresearch"))),
         "main.js titles the page");
})();

Promise.all(pendingTests)
  .then(() => process.exit(failures ? 1 : 0))
  .catch(err => { console.log("FAIL  the harness threw: " + (err && err.stack || err));
                  process.exit(1); });
"""


@pytest.fixture(scope="module")
def node():
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not installed — the frontend has no other test runner")
    return exe


def test_the_health_page_keeps_and_rebuilds_the_right_things(node, tmp_path):
    """render()'s health branch, and what a write (or a new day) does to the page."""
    for path in (MAIN_JS, HEALTH_JS, VIEWS_JS, BOOTSTRAP_JS, INDEX_HTML):
        assert path.exists(), f"missing frontend source: {path}"
    harness = tmp_path / "health_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")

    proc = subprocess.run(
        [node, str(harness), str(MAIN_JS), str(HEALTH_JS), str(VIEWS_JS),
         str(BOOTSTRAP_JS), str(INDEX_HTML)],
        capture_output=True, text=True, encoding="utf-8", timeout=60)

    # Show the claims either way: a bare "exit 1" tells the user nothing.
    print(proc.stdout)
    assert proc.returncode == 0, f"frontend checks failed:\n{proc.stdout}\n{proc.stderr}"
