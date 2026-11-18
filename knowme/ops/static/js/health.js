// knowme web — 健康 / 训练：按天的手账、训练、身体指标、目标与计划、食物库。
// 经典 <script>、共享全局作用域（无构建、无模块）。加载顺序与规矩：static/README.md。
//
// 这一页和阅读器那条路不一样：它**没有自己的对话面板**。原因是这里的每一样东西
// 都在库里，agent 自己的工具就能读到，没有"只有页面才知道的现场"需要递过去。
// 所以健康 agent 就是一个普通的侧栏 agent（#agent/health），不在这里再造一套
// 对话管道。（阅读器那个面板存在，是因为它要绑"正在读的是哪一篇"。）
//
// 页面形状由 views.js 里那条规矩决定：**标记写在拥有它的交互代码旁边**。
// 所以 VIEWS.health 在这里赋值，views.js 里没有第二份（那份会静默失效）。
//
// 手账这一页有三条不显然的规矩，写在各自代码旁边，改之前先看：
//   1. 会改变显示的动作要先 `editing = false` 再 refresh()（main.js 的轮询守卫）
//   2. 只重画一小块的动作**不许** refresh()（整页会抹掉你正在填的表单）
//   3. 热量和宏量一律"有一顿没填就显示 —"，而且合计数是服务端算好的（不在这里重算）

VIEWS.health = function(d, sub){
  return healthView((d && d.health_info) || {}, sub || "diary");
};

// 页面状态，全在内存里（不写 localStorage：那是"我选过的"和"页面默认"的区别，
// 不值得记）。页面被 5 秒轮询重画以后，这些选择要留住 —— 不然你点开"昨天"，
// 5 秒后自己跳回今天。
let hpTrendMetric = null;   // 身体指标页看的那个指标
let hpDay = "";             // 手账/训练选中的日期（"" = 今天）
let hpMode = "pick";        // 记一笔：pick 选食物 / manual 手动填 / new 新建食物
let hpPick = null;          // 选中但还没保存的那条（食物 + 每 100 单位的数值）
let hpFoodCat = "";         // 食物筛选：分类
let hpFoodQ = "";           // 食物筛选：关键词
let hpEditFood = null;      // 食物库页正在改的那个食物 id（null = 在新建）

// --------------------------------------------------------------------------
// 小工具
// --------------------------------------------------------------------------
function healthToday(){
  // 不能用 toISOString()：那是 UTC，东八区下午会被算成"明天"。
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function hpNum(value){
  // null / undefined / "" 都表示"不知道"，页面上一律显示 —，不显示 0。
  return (value === null || value === undefined || value === "") ? "—" : value;
}

// 表单里的输入框都要在获得焦点时 markEditing()：main.js 有一条守卫，
// editing 为真时 5 秒轮询不重建 #view —— 否则你正填到一半的那一行会被抹掉。
function hpField(id, type, placeholder){
  return `<input class="hp-input" id="${id}" type="${type}" placeholder="${esc(placeholder||"")}" onfocus="markEditing()">`;
}

// 录入表单里的日期框默认是今天（或正在看的那天）；用户改了日期就按他写的记。
function hpDateInput(id, value){
  return `<input class="hp-input hp-date" id="${id}" type="date" value="${esc(value || healthToday())}" onfocus="markEditing()">`;
}

function hpDayValue(){ return hpDay || healthToday(); }

// 往前/往后挪一天。**必须先把 editing 放下**：render() 里那条守卫在 editing
// 为真时会跳过重建（那本来是为了不抹掉你正在打的字），不放下它，点 ◀ 之后
// 日期根本不会变 —— 看起来就像按钮坏了。
function hpShiftDay(step){
  const base = new Date(hpDayValue() + "T00:00:00");
  base.setDate(base.getDate() + step);
  hpDay = new Date(base.getTime() - base.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  if (hpDay === healthToday()) hpDay = "";
  editing = false;
  return refresh();
}

function hpGoToday(){ hpDay = ""; editing = false; return refresh(); }

// 手账能往前翻到哪天。**这个数来自服务端**（health_info.windows.meals）——
// 页面自己写一个 30 就会和服务端的窗口各说各话，翻过那条线之后"还没有记录"
// 就不再是"那天没吃"，而是"我没把你那天的数据拿下来"。
function hpDayFloor(info){
  const days = ((info || {}).windows || {}).meals || 30;
  const base = new Date(healthToday() + "T00:00:00");
  base.setDate(base.getDate() - days);
  return new Date(base.getTime() - base.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function hpShortDay(day){
  // "2026-10-08" → "10-08"（表里一排日期，年份是噪音）
  return (day || "").slice(5);
}

// 正在看的这一天的合计（服务端算好的那一行）。没有这一天的记录时返回 null ——
// "这一天什么都没有"和"这一天吃了 0"是两句话。
function hpDayTotal(info, day){
  return (info.totals || []).find(t => t.date === day) || null;
}

function hpMealTotal(info, day, meal){
  return (info.meal_totals || []).find(t => t.date === day && t.meal === meal) || null;
}

// --------------------------------------------------------------------------
// 页面
// --------------------------------------------------------------------------
function healthView(info, sub){
  const tabs = [
    ["diary", "手账", null],
    ["training", "训练", (info.workouts || []).length],
    ["metrics", "身体指标", (info.metrics || []).length],
    ["plans", "目标与计划", (info.plans || []).length],
    ["foods", "食物库", (info.foods || []).length],
  ];
  let h = subtabBar("health", tabs, sub);
  // 这一页没有自己的对话面板（理由见文件开头），所以入口就是把侧栏那个 agent
  // 打开。它在下面这几张表里看到的，和它自己工具读到的是同一份。
  h += `<div class="hp-row" style="margin:8px 0 0">
    <button class="sessbtn" onclick="openAgent('health')">问健康 Agent →</button>
    <span class="meta">它读的就是这里记的东西；让它换算"150 克鸡胸肉"、定目标、写计划都行。</span>
  </div>`;
  if (sub === "training") return h + hpWorkouts(info);
  if (sub === "metrics") return h + hpMetrics(info);
  if (sub === "plans") return h + hpPlans(info);
  if (sub === "foods") return h + hpFoods(info);
  return h + hpDiary(info);
}

// ---- 手账（第一屏）--------------------------------------------------------
function hpDiary(info){
  const day = hpDayValue();
  return `
  ${hpDayBar(info, day)}
  <div class="hp-diary">
    ${hpRingBox(info, day)}
    <div class="card hp-pick-card">${hpPickPanel(info)}</div>
  </div>
  <h2 style="margin-top:20px">这一天吃了什么</h2>
  ${hpMealCards(info, day)}
  ${hpDayTraining(info, day)}`;
}

function hpDayBar(info, day){
  const floor = hpDayFloor(info);
  const atFloor = day <= floor;
  const today = healthToday();
  return `<div class="card hp-daybar">
    <button class="hp-step" title="${atFloor ? `只保留最近 ${((info.windows||{}).meals)||30} 天` : "前一天"}"
            ${atFloor ? "disabled" : ""} onclick="hpShiftDay(-1)">‹</button>
    <b class="hp-day">${esc(day)}</b>
    <span class="meta">${day === today ? "今天" : (day > today ? "以后" : "往前看")}</span>
    <button class="hp-step" title="后一天" ${day >= today ? "disabled" : ""}
            onclick="hpShiftDay(1)">›</button>
    ${day === today ? "" : `<button class="sessbtn" onclick="hpGoToday()">回到今天</button>`}
    <span class="meta hp-dayfloor">饮食只加载最近 ${((info.windows||{}).meals)||30} 天${
      atFloor ? " —— 再往前就没有了" : ""}</span>
  </div>`;
}

// ---- 能量环 ---------------------------------------------------------------
// 手写 SVG，不引图表库：两个同心圆，上面那个用 stroke-dasharray/dashoffset
// 画出一段弧，从 12 点开始（rotate(-90)）。
//
// **环上没有"已消耗"那一行**：我们没有可信的运动消耗数据（那要从 MET 表 +
// 体重 + 时长去估），编一个数比不显示更糟 —— 这就是这个应用的"不知道 ≠ 0"。
//
// 两种降级都是有意的，不是碰巧画出来的：
//   · 没设目标 → 不画进度弧（画出来就是拿 0 当分母）
//   · 这天的热量没填全 → 也不画（画出来就是一个假的百分比）
function hpRingSVG(ratio, over){
  const R = 52, C = 2 * Math.PI * R;
  // ratio === null 表示"这个数我们不知道"（没目标 / 这天没记录 / 有没填的顿）。
  // 轨道照画 —— 那是个形状不是数据；进度弧一根都不画。
  const arc = ratio === null ? "" :
    `<circle class="hp-ring-arc" cx="64" cy="64" r="${R}"
             stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${(C * (1 - ratio)).toFixed(1)}"
             transform="rotate(-90 64 64)"></circle>`;
  return `<svg class="hp-ring${over ? " over" : ""}" viewBox="0 0 128 128" role="img">
    <circle class="hp-ring-track" cx="64" cy="64" r="${R}"></circle>${arc}</svg>`;
}

function hpRingBox(info, day){
  const target = info.target || {};
  const row = hpDayTotal(info, day);
  const eaten = row ? row.kcal : null;
  let ratio = null, over = false, big = "—", tone = "";
  if (target.kcal === null || target.kcal === undefined){
    big = "还没设目标";
  } else if (!row){
    // 这一天一条都没记。这和"记了但没填热量"是两句话：那个是"不知道"，
    // 这个是"你还没告诉我"。两个都不画弧，但说法必须不一样。
    big = "还没有记录";
    tone = "meta";
  } else if (eaten === null){
    big = "算不出来";
    tone = "meta";
  } else {
    ratio = Math.min(eaten / target.kcal, 1);
    over = eaten > target.kcal;
    big = Math.round((eaten / target.kcal) * 100) + "%";
    tone = over ? "over" : "";
  }
  const left = (target.kcal != null && eaten != null)
    ? Math.round((target.kcal - eaten) * 10) / 10 : null;
  return `<div class="card hp-energy">
    <div class="hp-ring-box">
      ${hpRingSVG(ratio, over)}
      <div class="hp-ring-mid"><span class="hp-ring-num ${tone}">${esc(big)}</span>
        ${ratio === null ? "" : `<span class="meta">已摄入 / 目标</span>`}</div>
    </div>
    <div class="hp-kv">
      <div><span class="meta">已摄入</span><b>${hpNum(eaten)}</b> kcal</div>
      <div><span class="meta">目标</span><b>${hpNum(target.kcal)}</b> kcal</div>
      <div><span class="meta">${left !== null && left < 0 ? "超出" : "剩余"}</span>
        <b>${left === null ? "—" : Math.abs(left)}</b> kcal</div>
      ${row && row.kcal_unknown ? `<div class="hp-note">
        <span class="badge hp-unknown">${row.kcal_unknown} 顿没填热量</span>
        这一天的总数算不出来（不是 0）</div>` : ""}
      ${target.kcal == null ? `<div class="hp-note">
        设一个每日目标，这个环才有基准 ——
        <a href="#health/plans">去「目标与计划」</a></div>` : ""}
    </div>
    ${hpMacroBars(target, row)}
    <div class="hp-note meta">这里没有"运动消耗"那一项：没有可信的消耗数据，
      编一个数出来会让"今天到底吃了多少"变成一句很自信的错话。</div>
  </div>`;
}

// 蛋白 / 碳水 / 脂肪三条细进度条。和环一样：没目标或者没填全就只显示数、不画进度。
function hpMacroBars(target, row){
  const items = [["蛋白质", "protein", "g"], ["碳水", "carbs", "g"], ["脂肪", "fat", "g"]];
  return `<div class="hp-macros">${items.map(([label, key, unit]) => {
    const goal = target[key], got = row ? row[key] : null;
    const width = (goal != null && got != null)
      ? Math.min(Math.round((got / goal) * 100), 100) : 0;
    const over = goal != null && got != null && got > goal;
    return `<div class="hp-macro">
      <div class="hp-macro-top"><span>${label}</span>
        <span class="meta"><b>${hpNum(got)}</b> / ${hpNum(goal)}${unit}</span></div>
      <div class="hp-macro-bar"><i class="${over ? "over" : ""}" style="width:${width}%"></i></div>
    </div>`;
  }).join("")}</div>`;
}

// ---- 记一笔 ---------------------------------------------------------------
function hpPickPanel(info){
  const modes = [["pick", "选食物"], ["manual", "手动填"]];
  let h = `<div class="hp-row">${modes.map(([key, label]) =>
    `<button class="hp-mode ${hpMode === key ? "on" : ""}" onclick="hpSetMode('${key}')">${label}</button>`
  ).join("")}<span class="meta">记的是 <b>${esc(hpDayValue())}</b> 这一天</span></div>`;
  if (hpMode === "manual") return h + hpManualForm(info);
  if (hpMode === "new") return h + hpNewFoodForm(info);
  return h + hpFoodPicker(info);
}

function hpSetMode(mode){
  hpMode = mode;
  hpPick = null;
  // 只重画这一块：整页 refresh() 会把你正在别处填的东西抹掉（见文件开头第 2 条）。
  const box = document.querySelector(".hp-pick-card");
  if (box && D && D.health_info) box.innerHTML = hpPickPanel(D.health_info);
}

function hpFoodPicker(info){
  const cats = info.food_categories || [];
  const hits = hpFilterFoods(info).slice(0, 40);
  const total = hpFilterFoods(info).length;
  return `
  <div class="hp-row hp-chips">
    <button class="hp-chip ${hpFoodCat === "" ? "on" : ""}" onclick="hpFoodCatPick('')">全部</button>
    ${cats.map(c => `<button class="hp-chip ${hpFoodCat === c ? "on" : ""}"
      onclick="hpFoodCatPick('${esc(c)}')">${esc(c)}</button>`).join("")}
  </div>
  <input class="hp-input" id="hp-food-q" value="${esc(hpFoodQ)}" placeholder="选个食物…（名字里的一部分就行）"
         oninput="hpFoodQuery(this.value)">
  <div class="hp-foodlist" id="hp-foodlist">${hpFoodListHTML(info, hits, total)}</div>
  ${hpPick ? hpPickRow() : ""}
  <div class="hp-note meta">找不到？<a href="#" onclick="hpSetMode('new');return false">把「${esc(hpFoodQ || "…")}」建进食物库</a>
    —— 或者在食物库子标签里改掉某个内置的数值。库里选中的食物，热量按每 100 克算，填克数自动换算。</div>`;
}

function hpFoodListHTML(info, hits, total){
  if (!hpFilterFoods(info).length)
    return `<div class="meta hp-empty">没有匹配的食物。换个词，或者用上面的「新建」把它加进库。</div>`;
  const unit = f => `每 100${f.unit}`;
  return hits.map(f => `<button class="hp-foodrow" onclick="hpPickFood(${f.id})">
    <span class="hp-foodname">${esc(f.name)}</span>
    <span class="meta">${esc(unit(f))} · ${hpNum(f.kcal)} kcal</span>
    ${f.custom ? `<span class="badge">自建</span>` : ""}
  </button>`).join("")
  + (total > hits.length ? `<div class="meta hp-empty">还有 ${total - hits.length} 条，把关键词写具体一点</div>` : "");
}

function hpFilterFoods(info){
  const q = hpFoodQ.trim();
  return (info.foods || []).filter(f =>
    (!hpFoodCat || f.category === hpFoodCat) && (!q || f.name.indexOf(q) >= 0));
}

// 选中一个食物：不立刻保存，先摆出"吃了多少"那一行（可以改算出来的数）。
// 只重画这一块 —— 不 refresh()，否则你刚敲进去的搜索词和整页表单都会被重建。
function hpPickFood(id){
  if (!D || !D.health_info) return;
  const food = (D.health_info.foods || []).find(f => f.id === id);
  if (!food) return;
  hpPick = { name: food.name, unit: food.unit, grams: 100,
             per: { kcal: food.kcal, protein: food.protein, carbs: food.carbs, fat: food.fat } };
  const box = document.querySelector(".hp-pick-card");
  if (box) box.innerHTML = hpPickPanel(D.health_info);
}

function hpCalc(per, grams){
  // 每 100 单位的值 × 克数 ÷ 100。没填的那一项仍然是空的（不知道，不是 0）。
  if (per === null || per === undefined) return "";
  const g = Number(grams);
  if (!isFinite(g)) return "";
  return Math.round(per * g) / 100;
}

function hpPickRow(){
  const grams = hpPick.grams;
  const nums = [["kcal", "kcal"], ["protein", "蛋白 g"], ["carbs", "碳水 g"], ["fat", "脂肪 g"]];
  return `<div class="hp-pick" id="hp-pick">
    <div class="hp-row">
      <b>${esc(hpPick.name)}</b>
      <span class="meta">每 100${esc(hpPick.unit)}：${hpNum(hpPick.per.kcal)} kcal</span>
      <button class="hp-del" title="不记这条" onclick="hpPickCancel()">&times;</button>
    </div>
    <div class="hp-row">
      <input class="hp-input hp-narrow" id="hp-pick-grams" type="text" value="${grams}"
             title="吃了多少${esc(hpPick.unit)}" oninput="hpRecalc()" onfocus="markEditing()">
      <span class="meta">${esc(hpPick.unit)}</span>
      <select class="hp-input hp-narrow" id="hp-pick-meal" onfocus="markEditing()">${
        (D.health_info.meal_slots || []).map(s =>
          `<option value="${esc(s)}" ${s === hpMealGuess() ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
    </div>
    <div class="hp-row">${nums.map(([key, label]) =>
      `<input class="hp-input hp-narrow" id="hp-pick-${key}" type="text" value="${
        hpCalc(hpPick.per[key], grams)}" placeholder="${label}" onfocus="markEditing()">`).join("")}</div>
    <div class="hp-row">
      <button class="save" onclick="hpSavePick()">记到这一天</button>
      <span class="meta">数字是照食物库算的，可以改（比如只吃了一半）。</span>
    </div>
  </div>`;
}

// 改克数就把四个数重算一遍 —— 只改那几个输入框的值，不重画任何东西
// （重画会把你正在敲的克数框顶掉，光标也会丢）。
function hpRecalc(){
  if (!hpPick) return;
  const grams = hpValue("hp-pick-grams");
  hpPick.grams = grams;
  ["kcal", "protein", "carbs", "fat"].forEach(key => {
    const el = document.getElementById("hp-pick-" + key);
    if (el) el.value = hpCalc(hpPick.per[key], grams);
  });
}

function hpPickCancel(){
  hpPick = null;
  if (D && D.health_info) hpSetMode(hpMode);
}

function hpMealGuess(){
  const hour = new Date().getHours();
  if (hour < 10) return "早餐";
  if (hour < 15) return "午餐";
  if (hour < 21) return "晚餐";
  return "加餐";
}

async function hpSavePick(){
  if (!hpPick) return;
  const meal = hpValue("hp-pick-meal") || hpMealGuess();
  const payload = { action: "add_meal", date: hpDayValue(), meal: meal, food: hpPick.name,
                    grams: hpValue("hp-pick-grams"), kcal: hpValue("hp-pick-kcal"),
                    protein: hpValue("hp-pick-protein"), carbs: hpValue("hp-pick-carbs"),
                    fat: hpValue("hp-pick-fat") };
  // 先清掉再保存：这样重画出来的页面是"已经记好了"的样子。存失败就把它放回去，
  // 不然用户填的那一行会凭空消失（重画时 hpPick 已经是 null 了）。
  const saved = hpPick;
  hpPick = null;
  if (!await hpPost(payload)) hpPick = saved;
}

function hpManualForm(info){
  const slots = info.meal_slots || [];
  return `<div class="hp-form" style="margin-top:8px">
    ${hpDateInput("hp-e-date", hpDayValue())}
    <select class="hp-input hp-narrow" id="hp-e-meal" onfocus="markEditing()">${slots.map(s =>
      `<option value="${esc(s)}" ${s === hpMealGuess() ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
    ${hpField("hp-e-food", "text", "食物")}
    ${hpField("hp-e-kcal", "text", "kcal")}
    ${hpField("hp-e-protein", "text", "蛋白 g")}
    ${hpField("hp-e-carbs", "text", "碳水 g")}
    ${hpField("hp-e-fat", "text", "脂肪 g")}
    <button class="save" onclick="healthAdd('meal')">记下</button>
    <span class="meta">热量和宏量都可以留空 —— <b>留空是"不知道"，不是 0</b>。</span>
  </div>`;
}

// 新建一个食物：存进食物库，然后直接变成"已选中"（下一步就是填克数）。
function hpNewFoodForm(info){
  const cats = info.food_categories || [];
  return `<div class="hp-pick" style="margin-top:8px">
    <div class="hp-row">
      <input class="hp-input" id="hp-nf-name" type="text" value="${esc(hpFoodQ)}"
             placeholder="食物名，如 我妈做的红烧肉" onfocus="markEditing()">
      <select class="hp-input hp-narrow" id="hp-nf-cat" onfocus="markEditing()">${cats.map(c =>
        `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select>
      <select class="hp-input hp-narrow" id="hp-nf-unit" onfocus="markEditing()">
        <option value="g">每 100g</option><option value="ml">每 100ml</option></select>
    </div>
    <div class="hp-row">
      <input class="hp-input hp-narrow" id="hp-nf-kcal" type="text" placeholder="kcal" onfocus="markEditing()">
      <input class="hp-input hp-narrow" id="hp-nf-protein" type="text" placeholder="蛋白 g" onfocus="markEditing()">
      <input class="hp-input hp-narrow" id="hp-nf-carbs" type="text" placeholder="碳水 g" onfocus="markEditing()">
      <input class="hp-input hp-narrow" id="hp-nf-fat" type="text" placeholder="脂肪 g" onfocus="markEditing()">
    </div>
    <div class="hp-row"><button class="save" onclick="hpAddFoodFromPick()">存进食物库并选它</button>
      <span class="meta">数值是"每 100 克"的；不知道的项留空就是不知道。存完就能填克数了。</span></div>
  </div>`;
}

async function hpAddFoodFromPick(){
  const name = hpValue("hp-nf-name");
  if (!name) return alert("先写一个食物名");
  const payload = { action: "add_food", name: name, category: hpValue("hp-nf-cat"),
                    unit: hpValue("hp-nf-unit"), kcal: hpValue("hp-nf-kcal"),
                    protein: hpValue("hp-nf-protein"), carbs: hpValue("hp-nf-carbs"),
                    fat: hpValue("hp-nf-fat") };
  let res;
  try { res = await postJSON("/api/health", payload); }
  catch (error){ return alert("保存失败：" + (error.message || error)); }
  if (!res.ok) return alert(res.error || "保存失败");
  const food = ((res.data || {}).foods || []).find(f => f.name === name);
  hpMode = "pick";
  hpPick = food ? { name: food.name, unit: food.unit, grams: 100,
                    per: { kcal: food.kcal, protein: food.protein,
                           carbs: food.carbs, fat: food.fat } } : null;
  editing = false;
  await refresh();
  // refresh() 重建整页以后，回到"选食物"这一栏，让用户接着填克数。
  if (food) hpSetMode("pick");
}

// ---- 三餐（按天）----------------------------------------------------------
function hpMealCards(info, day){
  const slots = info.meal_slots || [];
  return `<div class="hp-meals">${slots.map(meal => {
    const rows = (info.meals || []).filter(m => m.date === day && m.meal === meal);
    const total = hpMealTotal(info, day, meal);
    return `<div class="card hp-meal">
      <div class="hp-row">
        <b>${esc(meal)}</b>
        <span class="meta hp-meal-sum">${
          total ? `${hpNum(total.kcal)} kcal${total.kcal_unknown
            ? `<span class="badge hp-unknown">${total.kcal_unknown} 条没填</span>` : ""}`
          : "还没记"}</span>
      </div>
      ${rows.length ? `<ul class="hp-food-list">${rows.map(m => `<li>
        <span>${esc(m.food)}${m.grams != null ? `<span class="meta"> ${m.grams}g</span>` : ""}</span>
        <span class="meta">${hpNum(m.kcal)} kcal</span>
        <button class="hp-del" title="删除这条" onclick="healthDelete('meal',${m.id})">&times;</button>
      </li>`).join("")}</ul>` : `<div class="meta">还没记。</div>`}
    </div>`;
  }).join("")}</div>`;
}

// ---- 当天的训练（手账里的摘要）--------------------------------------------
function hpDayTraining(info, day){
  const rows = (info.workouts || []).filter(w => w.date === day);
  const volume = hpVolume(rows);
  return `<h2 style="margin-top:20px">这一天的训练</h2>
  <div class="card">
    <div class="hp-row">
      <b>${rows.length} 个动作</b>
      ${volume ? `<span class="meta">总容量 ${volume} kg
        （组 × 次 × 重量；没记重量或次数的那些不算进来）</span>` : ""}
      <a class="sessbtn" href="#health/training">去训练页记 →</a>
    </div>
    ${rows.length ? `<ul class="hp-food-list">${rows.map(w => `<li>
      <span>${esc(w.exercise)}</span>
      <span class="meta">${hpNum(w.sets)}×${hpNum(w.reps)} @ ${hpNum(w.weight)}kg</span>
      <span class="meta hp-last">${esc(hpLastLabel(info, w))}</span>
    </li>`).join("")}</ul>`
    : `<div class="meta">这一天还没有训练记录。用「去训练页」记，或者直接跟健康 Agent 说。</div>`}
  </div>`;
}

// ---- 训练 -----------------------------------------------------------------
function hpWorkouts(info){
  const rows = info.workouts || [];
  const byDay = {};
  rows.forEach(w => { (byDay[w.date] = byDay[w.date] || []).push(w); });
  const days = Object.keys(byDay).sort().reverse();
  const volume = hpVolume(rows);
  return `
  <div class="hp-intro">一行 = 某天做的一个动作（几组 × 几次 × 多重）。每个动作旁边是
    <b>上一次做它</b>的样子 —— 进步就是这么看的。总容量只累加三项都填了的行，
    没写重量的不算进去，也不假装它是 0。</div>
  <div class="card hp-form">
    ${hpDateInput("hp-w-date", hpDayValue())}
    ${hpField("hp-w-exercise", "text", "动作，如 深蹲")}
    ${hpField("hp-w-sets", "text", "组")}
    ${hpField("hp-w-reps", "text", "次")}
    ${hpField("hp-w-weight", "text", "重量 kg")}
    ${hpField("hp-w-rpe", "text", "RPE")}
    <button class="save" onclick="healthAdd('workout')">记下</button>
  </div>
  <div class="hp-row meta" style="margin:12px 0 6px">
    <span>最近 ${rows.length} 条记录${volume ? ` · 总容量 <b>${volume}</b> kg` : ""}</span>
  </div>
  ${days.length ? days.map(day => `<div class="card hp-day-group">
    <div class="hp-row"><b>${esc(day)}</b>
      <span class="meta">${byDay[day].length} 个动作 · ${hpVolume(byDay[day])} kg</span></div>
    <table><thead><tr><th>动作</th><th>组×次</th><th>重量</th><th>RPE</th><th>上次</th><th></th></tr></thead>
    <tbody>${byDay[day].map(w => `<tr>
      <td class="dbcell">${esc(w.exercise)}</td>
      <td class="dbcell">${hpNum(w.sets)}×${hpNum(w.reps)}</td>
      <td class="dbcell">${hpNum(w.weight)}</td>
      <td class="dbcell">${hpNum(w.rpe)}</td>
      <td class="dbcell meta">${esc(hpLastLabel(info, w))}</td>
      <td class="dbcell"><button class="hp-del" title="删除这条"
        onclick="healthDelete('workout',${w.id})">&times;</button></td>
    </tr>`).join("")}</tbody></table>
  </div>`).join("") : `<div class="card empty">还没有训练记录。</div>`}`;
}

// "上一次做这个动作"是什么样。纯函数（好测）：在拿到的这些行里找
// (date, id) **严格早于**当前这一行的、同一个动作的最近一条。
//
// 找不到就说"这 N 天里没有更早的记录" —— **不写"第一次做"**：我们只加载了
// 最近 N 天的训练，这件事我们并不知道。上一行缺组数/次数/重量时明说"没记全"，
// 不拿 NULL 当 0。
function hpLastBefore(rows, row){
  let best = null;
  rows.forEach(other => {
    if (other.exercise !== row.exercise || other.id === row.id) return;
    const earlier = other.date < row.date || (other.date === row.date && other.id < row.id);
    if (!earlier) return;
    if (!best || other.date > best.date || (other.date === best.date && other.id > best.id))
      best = other;
  });
  return best;
}

function hpLastLabel(info, row){
  const days = ((info.windows || {}).workouts) || 60;
  const last = hpLastBefore(info.workouts || [], row);
  if (!last) return `这 ${days} 天里没有更早的记录`;
  const what = (last.sets || last.reps || last.weight)
    ? `${hpNum(last.sets)}×${hpNum(last.reps)} @ ${hpNum(last.weight)}kg`
    : "没记全";
  return `上次 ${hpShortDay(last.date)} · ${what}`;
}

function hpVolume(rows){
  const total = rows.reduce((sum, w) =>
    (w.sets && w.reps && w.weight) ? sum + w.sets * w.reps * w.weight : sum, 0);
  return total ? Math.round(total * 10) / 10 : 0;
}

// ---- 身体指标 -------------------------------------------------------------
function hpMetrics(info){
  const options = info.metric_options || [];
  const wanted = hpTrendMetric || (options[0] || {}).name || "";
  hpTrendMetric = wanted;
  return `
  <div class="hp-intro">这里的每一个指标都单独记，互不干扰。趋势图按<b>天</b>取最后一条，
    所以同一天称两次不会画出两个点。</div>
  <div class="card hp-form">
    ${hpDateInput("hp-m-date")}
    <select class="hp-input" id="hp-m-metric" onchange="markEditing()">${options.map(o =>
      `<option value="${esc(o.name)}">${esc(o.name)}（${esc(o.unit)}）</option>`).join("")}</select>
    ${hpField("hp-m-value", "text", "数值")}
    ${hpField("hp-m-note", "text", "备注（可选）")}
    <button class="save" onclick="healthAdd('metric')">记下</button>
  </div>
  ${hpTrendBox(info, wanted)}
  ${hpMetricList(info)}`;
}

function hpTrendBox(info, metric){
  const points = hpSeries((info.metrics || []), metric);
  const options = (info.metric_options || []);
  return `<div class="card">
    <div class="hp-row">
      <b>趋势</b>
      <select class="hp-input hp-narrow" id="hp-trend-metric" onchange="healthTrendPick()">${options.map(o =>
        `<option value="${esc(o.name)}" ${o.name === metric ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</select>
      <span class="meta">共 ${points.length} 个点${points.length ? `（${esc(points[0].date)} → ${esc(points[points.length-1].date)}）` : ""}</span>
    </div>
    <div id="hp-trend">${hpTrendSVG(points)}</div>
  </div>`;
}

// 一条折线。没有图表库 —— 三十行 SVG 就够了，也不必为它引一个依赖。
function hpTrendSVG(points){
  if (!points.length) return `<div class="meta">还没有这个指标的数据。</div>`;
  const unit = points[0].unit || "";
  if (points.length === 1)
    return `<div class="meta">只有一条记录：${esc(points[0].date)} ${points[0].value}${esc(unit)}。
      至少两条才能连线（多记几天就有趋势了）。</div>`;
  const W = 560, H = 150, PAD = 26;
  const values = points.map(p => p.value);
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = (hi - lo) || 1;                       // 全等的时候别除以零
  const x = i => PAD + i * (W - 2 * PAD) / (points.length - 1);
  const y = v => H - PAD - (v - lo) * (H - 2 * PAD) / span;
  const line = points.map((p, i) => `${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const dots = points.map((p, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(p.value).toFixed(1)}" r="2.5" fill="var(--accent)"></circle>`).join("");
  const first = points[0].value, last = points[points.length-1].value;
  const delta = Math.round((last - first) * 10) / 10;
  return `
  <svg class="hp-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <polyline points="${line}" fill="none" stroke="var(--accent)" stroke-width="1.6"></polyline>
    ${dots}
  </svg>
  <div class="hp-row meta">
    <span>最新 <b>${last}${esc(unit)}</b>（${esc(points[points.length-1].date)}）</span>
    <span>区间 ${lo}–${hi}${esc(unit)}</span>
    <span>这段时间变化 ${delta > 0 ? "+" : ""}${delta}${esc(unit)}</span>
  </div>`;
}

// 同一个指标同一天只留最后记的那一条（和 applications/health.py 的 metric_series 同一条规矩）。
function hpSeries(metrics, metric){
  const byDay = {};
  metrics.filter(m => m.metric === metric).forEach(m => {   // 传进来是倒序，先见到的更新
    if (!byDay[m.date]) byDay[m.date] = m;
  });
  return Object.values(byDay).sort((a, b) => a.date < b.date ? -1 : 1);
}

function hpMetricList(info){
  const rows = info.metrics || [];
  if (!rows.length) return `<div class="card empty">还没有记录。上面填一条试试。</div>`;
  return `<div class="scrolly" style="margin-top:14px"><table>
    <thead><tr><th>日期</th><th>指标</th><th>数值</th><th>备注</th><th></th></tr></thead>
    <tbody>${rows.map(m => `<tr>
      <td class="dbcell">${esc(m.date)}</td>
      <td class="dbcell">${esc(m.metric)}</td>
      <td class="dbcell"><b>${m.value}</b> ${esc(m.unit || "")}</td>
      <td class="dbcell">${esc(m.note || "")}</td>
      <td class="dbcell"><button class="hp-del" title="删除这条" onclick="healthDelete('metric',${m.id})">&times;</button></td>
    </tr>`).join("")}</tbody></table></div>`;
}

// ---- 目标与计划 -----------------------------------------------------------
function hpPlans(info){
  const kinds = info.plan_kinds || [];
  const active = info.plans || [];
  return `
  <h2>每日目标</h2>
  ${hpTargetCard(info)}
  <div class="hp-intro">保存一份新计划 = 新开一个版本，旧的那一版自动归档、<b>不删</b>，
    所以"上个月那份饮食计划是怎么写的"随时能翻回来。勾选只改当前生效的那一版。</div>
  <div class="card hp-form hp-stack">
    <div class="hp-row">
      <select class="hp-input hp-narrow" id="hp-p-kind" onchange="markEditing()">${kinds.map(k =>
        `<option value="${esc(k.key)}">${esc(k.label)}</option>`).join("")}</select>
      ${hpField("hp-p-title", "text", "计划标题，如 减脂饮食")}
    </div>
    <textarea class="hp-input hp-items" id="hp-p-items" rows="4" onfocus="markEditing()"
      placeholder="每行一条，例如：&#10;每天蛋白质 110g&#10;每周三次力量训练"></textarea>
    <div class="hp-row"><button class="save" onclick="healthSavePlan()">保存为新版本</button>
      <span class="meta">标题一样 = 同一条计划的下一个版本；换了标题就是另一条计划。</span></div>
  </div>
  ${active.length ? active.map(p => hpPlanCard(p)).join("")
    : `<div class="card empty">还没有生效的计划。上面写一个，或者让健康 Agent 帮你定一份。</div>`}
  ${hpVersionList(info)}`;
}

// 每日目标：手账那个环就是照着它画的。留空 = 没设那一项（不是 0）。
function hpTargetCard(info){
  const t = info.target || {};
  const field = (id, label, key, unit) =>
    `<input class="hp-input hp-narrow" id="${id}" type="text" placeholder="${label}"
      value="${t[key] == null ? "" : t[key]}" onfocus="markEditing()"><span class="meta">${unit}</span>`;
  const set = (t.kcal != null || t.protein != null || t.carbs != null || t.fat != null);
  return `<div class="card">
    <div class="hp-row">
      <b>每天吃多少</b>
      <span class="meta">${set ? `设于 ${esc((t.updated_at || "").slice(0, 10))}`
                               : "还没设 —— 手账那个环现在没有基准"}</span>
      <button class="save" onclick="hpSaveTarget()">保存目标</button>
      ${set ? `<button class="sessbtn" onclick="hpClearTarget()">清空</button>` : ""}
    </div>
    <div class="hp-row">
      ${field("hp-t-kcal", "热量", "kcal", "kcal")}
      ${field("hp-t-protein", "蛋白", "protein", "g")}
      ${field("hp-t-carbs", "碳水", "carbs", "g")}
      ${field("hp-t-fat", "脂肪", "fat", "g")}
    </div>
    <div class="meta">留空就是"这一项没设"。四个都空 = 没有目标（环不画进度，只显示已摄入）。</div>
  </div>`;
}

function hpPlanCard(plan){
  const items = plan.items || [];
  const done = items.filter(i => i.done).length;
  return `<div class="card hp-plan">
    <div class="hp-row">
      <span class="badge">${esc(plan.kind_label || plan.kind)}</span>
      <b>${esc(plan.title)}</b>
      <span class="meta">v${plan.version} · ${esc(plan.created_at.slice(0, 10))}
        · ${done}/${items.length} 已完成</span>
      <button class="sessbtn" title="收起这份计划（历史版本都还在）"
              onclick="healthArchive(${plan.id})">归档</button>
    </div>
    ${plan.note ? `<div class="meta">${esc(plan.note)}</div>` : ""}
    ${items.length ? `<ul class="hp-items-list">${items.map((it, i) => `
      <li class="${it.done ? "done" : ""}">
        <label><input type="checkbox" ${it.done ? "checked" : ""}
          onchange="healthToggle(${plan.id},${i},this.checked)"> ${esc(it.text)}</label>
        ${it.done && it.done_at ? `<span class="meta">${esc(it.done_at.slice(0, 10))}</span>` : ""}
      </li>`).join("")}</ul>`
      : `<div class="meta">这份计划还没有条目。</div>`}
  </div>`;
}

// 历史版本只给一行元信息；点开才去 /api/health 要那一版的条目 ——
// 版本会越攒越多，全塞进 /api/data 就是让它每 5 秒陪跑一次。
function hpVersionList(info){
  const versions = info.versions || [];
  if (versions.length <= (info.plans || []).length)
    return `<div class="meta" style="margin-top:14px">还没有历史版本。</div>`;
  return `<h2 style="margin-top:22px">版本历史</h2>
  <div class="scrolly"><table>
    <thead><tr><th>类型</th><th>标题</th><th>版本</th><th>状态</th><th>建立日期</th><th></th></tr></thead>
    <tbody>${versions.map(v => `<tr>
      <td class="dbcell">${esc(v.kind_label || v.kind)}</td>
      <td class="dbcell">${esc(v.title)}</td>
      <td class="dbcell">v${v.version}</td>
      <td class="dbcell">${v.status === "active" ? '<span class="badge retrieve">生效中</span>'
                                                : '<span class="badge">已归档</span>'}</td>
      <td class="dbcell">${esc(v.created_at.slice(0, 10))}</td>
      <td class="dbcell"><button class="sessbtn" onclick="healthViewPlan(${v.id})">看这一版</button></td>
    </tr>`).join("")}</tbody></table></div>
  <div id="hp-plan-view"></div>`;
}

// ---- 食物库 ---------------------------------------------------------------
function hpFoods(info){
  const hits = hpFilterFoods(info);
  return `
  <div class="hp-intro">选食物记饭的时候就是从这里查的。这里的数值是<b>每 100 单位</b>的
    参考值 —— 换个部位、牌子、做法都会差，所以内置的也能改、能删。
    改过的数值只影响以后记的饭：已经记下的那一笔是当时的快照，不会跟着变。</div>
  <div class="hp-row hp-chips">
    <button class="hp-chip ${hpFoodCat === "" ? "on" : ""}" onclick="hpFoodCatPick('')">全部</button>
    ${(info.food_categories || []).map(c => `<button class="hp-chip ${hpFoodCat === c ? "on" : ""}"
      onclick="hpFoodCatPick('${esc(c)}')">${esc(c)}</button>`).join("")}
  </div>
  <input class="hp-input" id="hp-food-q" value="${esc(hpFoodQ)}" placeholder="搜名字…"
         oninput="hpFoodQuery(this.value)">
  <div id="hp-food-edit" style="margin:12px 0">${hpFoodEditBox(info)}</div>
  <div class="meta" style="margin-bottom:6px">${hits.length} 条${
    hpFoodQ || hpFoodCat ? "（筛过的）" : ""}</div>
  <div class="scrolly"><table>
    <thead><tr><th>名称</th><th>分类</th><th>热量</th><th>蛋白</th><th>碳水</th><th>脂肪</th><th></th></tr></thead>
    <tbody>${hits.map(f => `<tr>
      <td class="dbcell">${esc(f.name)}${f.custom ? ' <span class="badge">自建</span>' : ""}</td>
      <td class="dbcell">${esc(f.category)}</td>
      <td class="dbcell"><b>${hpNum(f.kcal)}</b><span class="meta">/${esc(f.unit)}</span></td>
      <td class="dbcell">${hpNum(f.protein)}</td>
      <td class="dbcell">${hpNum(f.carbs)}</td>
      <td class="dbcell">${hpNum(f.fat)}</td>
      <td class="dbcell">
        <button class="sessbtn" onclick="hpEditFoodOpen(${f.id})">改</button>
        <button class="hp-del" title="从食物库删掉（已经记过的饭不受影响）"
                onclick="hpDeleteFood(${f.id})">&times;</button>
      </td>
    </tr>`).join("")}</tbody></table></div>`;
}

// 新建 / 编辑共用一个表单：hpEditFood 是 null 就是新建。
function hpFoodEditBox(info){
  const editing = hpEditFood
    ? (info.foods || []).find(f => f.id === hpEditFood) : null;
  const v = key => {
    if (!editing) return "";
    const value = editing[key];
    return value == null ? "" : value;
  };
  return `<div class="card hp-form">
    <input class="hp-input" id="hp-f-name" type="text" placeholder="食物名"
           value="${editing ? esc(editing.name) : esc(hpFoodQ)}" onfocus="markEditing()">
    <select class="hp-input hp-narrow" id="hp-f-cat" onfocus="markEditing()">${
      (info.food_categories || []).map(c =>
        `<option value="${esc(c)}" ${editing && editing.category === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>
    <select class="hp-input hp-narrow" id="hp-f-unit" onfocus="markEditing()">
      <option value="g" ${editing && editing.unit === "ml" ? "" : "selected"}>每 100g</option>
      <option value="ml" ${editing && editing.unit === "ml" ? "selected" : ""}>每 100ml</option></select>
    <input class="hp-input hp-narrow" id="hp-f-kcal" type="text" placeholder="kcal"
           value="${v("kcal")}" onfocus="markEditing()">
    <input class="hp-input hp-narrow" id="hp-f-protein" type="text" placeholder="蛋白 g"
           value="${v("protein")}" onfocus="markEditing()">
    <input class="hp-input hp-narrow" id="hp-f-carbs" type="text" placeholder="碳水 g"
           value="${v("carbs")}" onfocus="markEditing()">
    <input class="hp-input hp-narrow" id="hp-f-fat" type="text" placeholder="脂肪 g"
           value="${v("fat")}" onfocus="markEditing()">
    <button class="save" onclick="hpSaveFood()">${editing ? "保存修改" : "新建食物"}</button>
    ${editing ? `<button class="sessbtn" onclick="hpEditFoodOpen(null)">取消</button>
      <span class="meta">在改「${esc(editing.name)}」</span>`
      : `<span class="meta">留空的数值就是"不知道"，选它记饭时那一项也是空的。</span>`}
  </div>`;
}

// 切换"在改哪一个"：只重画那个盒子，不动整页（食物库的列表和筛选保持原样）。
function hpEditFoodOpen(id){
  hpEditFood = id;
  const box = document.getElementById("hp-food-edit");
  if (box && D && D.health_info) box.innerHTML = hpFoodEditBox(D.health_info);
}

// 筛选用的是内存里那份食物库（100 行），敲一个字不用发请求 —— 所以只重画列表。
function hpFoodQuery(text){
  hpFoodQ = text;
  hpRepaintFoodLists();
}

function hpFoodCatPick(cat){
  hpFoodCat = cat;
  hpRepaintFoodLists();
}

function hpRepaintFoodLists(){
  if (!D || !D.health_info) return;
  const info = D.health_info;
  const picker = document.getElementById("hp-foodlist");
  if (picker){
    const hits = hpFilterFoods(info).slice(0, 40);
    picker.innerHTML = hpFoodListHTML(info, hits, hpFilterFoods(info).length);
  }
  const tab = document.getElementById("hp-food-edit");
  if (tab){                                   // 食物库那一页：整页重画一遍列表区
    const box = document.getElementById("view");
    if (box && VIEWS.health) box.innerHTML = VIEWS.health(D, "foods");
  }
}

// --------------------------------------------------------------------------
// 动作：每一个都以 "editing = false; refresh()" 收尾
// --------------------------------------------------------------------------
// refresh() 是唯一会把 #view 重画一遍的东西（main.js）。而 render() 里有一条
// 守卫：**只要 editing 还是 true 就不重建**（否则你正在打的字会被 5 秒轮询抹掉）。
// 所以写完必须先把 editing 放下，否则新记的这条不会出现 —— 看起来就像"点了没反应"。
function hpValue(id){ const el = document.getElementById(id); return el ? el.value.trim() : ""; }

async function healthAdd(record){
  const payload = { action: "add_" + record };
  if (record === "metric"){
    Object.assign(payload, { date: hpValue("hp-m-date"), metric: hpValue("hp-m-metric"),
                             value: hpValue("hp-m-value"), note: hpValue("hp-m-note") });
  } else if (record === "workout"){
    Object.assign(payload, { date: hpValue("hp-w-date"), exercise: hpValue("hp-w-exercise"),
                             sets: hpValue("hp-w-sets"), reps: hpValue("hp-w-reps"),
                             weight: hpValue("hp-w-weight"), rpe: hpValue("hp-w-rpe") });
  } else {
    Object.assign(payload, { date: hpValue("hp-e-date"), meal: hpValue("hp-e-meal"),
                             food: hpValue("hp-e-food"), kcal: hpValue("hp-e-kcal"),
                             protein: hpValue("hp-e-protein"), carbs: hpValue("hp-e-carbs"),
                             fat: hpValue("hp-e-fat") });
  }
  await hpPost(payload);
}

async function healthDelete(kind, id){
  if (!confirm("删除这条记录？不可撤销。")) return;
  await hpPost({ action: "delete", kind, id });
}

async function healthToggle(planId, index, done){
  await hpPost({ action: "toggle", plan_id: planId, index, done });
}

async function healthArchive(planId){
  if (!confirm("把这份计划收起来？它的版本历史都还在。")) return;
  await hpPost({ action: "archive", plan_id: planId });
}

async function healthSavePlan(){
  const title = hpValue("hp-p-title");
  if (!title) return alert("请先写一个计划标题");
  const items = hpValue("hp-p-items").split("\n").map(s => s.trim()).filter(Boolean);
  await hpPost({ action: "save_plan", kind: hpValue("hp-p-kind"), title, items });
}

async function hpSaveTarget(){
  await hpPost({ action: "set_target", kcal: hpValue("hp-t-kcal"),
                 protein: hpValue("hp-t-protein"), carbs: hpValue("hp-t-carbs"),
                 fat: hpValue("hp-t-fat") });
}

async function hpClearTarget(){
  if (!confirm("清空每日目标？手账那个环就没有基准了。")) return;
  await hpPost({ action: "set_target" });
}

async function hpSaveFood(){
  const name = hpValue("hp-f-name");
  if (!name) return alert("请先写一个食物名");
  const payload = { action: hpEditFood ? "update_food" : "add_food",
                    name: name, category: hpValue("hp-f-cat"), unit: hpValue("hp-f-unit"),
                    kcal: hpValue("hp-f-kcal"), protein: hpValue("hp-f-protein"),
                    carbs: hpValue("hp-f-carbs"), fat: hpValue("hp-f-fat") };
  if (hpEditFood) payload.food_id = hpEditFood;
  hpEditFood = null;
  await hpPost(payload);
}

async function hpDeleteFood(id){
  if (!confirm("把这个食物从库里删掉？已经记过的饭不受影响。")) return;
  if (hpEditFood === id) hpEditFood = null;
  await hpPost({ action: "delete_food", food_id: id });
}

// 看历史版本：只重画那一小块，不动整页（这样表单里没提交的内容不会被抹掉）。
async function healthViewPlan(planId){
  const box = document.getElementById("hp-plan-view");
  if (!box) return;
  let res;
  try { res = await postJSON("/api/health", { action: "plan", plan_id: planId }); }
  catch (error){ return alert("读取失败：" + (error.message || error)); }
  if (!res.ok) return alert(res.error || "读取失败");
  const plan = res.plan;
  box.innerHTML = `<div class="card hp-plan" style="margin-top:14px">
    <div class="hp-row"><span class="badge">${esc(plan.kind_label || plan.kind)}</span>
      <b>${esc(plan.title)} v${plan.version}</b>
      <span class="meta">${esc(plan.created_at.slice(0, 10))} · ${plan.status === "active" ? "生效中" : "已归档（只读，勾不了）"}</span></div>
    <ul class="hp-items-list">${(plan.items || []).map(it =>
      `<li class="${it.done ? "done" : ""}"><label><input type="checkbox" disabled ${it.done ? "checked" : ""}> ${esc(it.text)}</label></li>`).join("")}</ul>
  </div>`;
}

async function hpPost(payload){
  let res;
  try { res = await postJSON("/api/health", payload); }
  catch (error){ alert("保存失败：" + (error.message || error)); return false; }
  if (!res.ok){ alert(res.error || "保存失败"); return false; }
  editing = false;          // 见上面那段注释：不放下 editing，页面就不会重画
  await refresh();
  return true;
}

// 切换趋势图的指标：那一小块自己重画，不用整页（整页会把你正在填的表单清空）。
function healthTrendPick(){
  const select = document.getElementById("hp-trend-metric");
  const box = document.getElementById("hp-trend");
  if (!select || !box || !D || !D.health_info) return;
  hpTrendMetric = select.value;
  box.innerHTML = hpTrendSVG(hpSeries(D.health_info.metrics || [], hpTrendMetric));
}
