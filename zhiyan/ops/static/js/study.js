// zhiyan web — 学习：考试备考与语言学习。
// 经典 <script>、共享全局作用域（无构建、无模块）。加载顺序与规矩：static/README.md。
//
// 这一页自己渲染项目头、页签和内容；main.js 只负责路由和轮询。
// 三条不显然的规矩：
//   1. 复习进行中（studyReviewing）时 5 秒轮询不许重建 #view；
//   2. 任何表单聚焦都调 markEditing()，否则输入到一半会被轮询抹掉；
//   3. 写操作返回的 study_info 快照直接替换 D.study_info，不再多发一次 /api/data。

let studySelected = {exam: null, language: null};
let studyCache = {};
let studyReviewing = false;
let studyReview = null;
let studyModal = null;
let studyNotice = "";
let studyBusy = false;
let studyBusyUnit = "";
let studyCardSel = new Set();
let studyVocabQuery = "";
let studyVocabFilter = "all";
let studyLastHash = "";

function studyPct(value) { return Math.round(Number(value || 0) * 100); }

function studyTypeLabel(value) {
  return ({qa: "问答", cloze: "填空", vocab: "词汇", dictation: "听写",
           translation: "翻译", grammar: "语法"})[value] || value;
}

function studyRoleLabel(value) {
  return ({syllabus: "考纲", textbook: "教材", notes: "笔记",
           wordlist: "词表"})[value] || value;
}

function studyKindLabel(value) {
  return ({learn: "学习", review: "复习", vocab_new: "新词", listening: "听力",
           speaking: "口语", reading: "阅读", writing: "写作"})[value] || value;
}

function studyStatusLabel(value) {
  return ({candidate: "待确认", active: "已激活", suspended: "已暂停"})[value] || value;
}

function studyLocalDate() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000)
    .toISOString().slice(0, 10);
}

function studyInfo(d) {
  return (d && d.study_info) || {projects: [], kinds: [], material_roles: [], card_types: []};
}

function studyProjects(d, track) {
  return (studyInfo(d).projects || []).filter((item) => item.project.kind === track);
}

function studyProject(d, track) {
  const projects = studyProjects(d, track);
  if (!projects.length) { studySelected[track] = null; return null; }
  let id = studySelected[track];
  if (!projects.some((item) => item.project.id === id)) id = projects[0].project.id;
  studySelected[track] = id;
  return projects.find((item) => item.project.id === id);
}

function studyCacheKey(track, sub, projectId) {
  return track + ":" + sub + ":" + projectId;
}

function studyGo(track, sub) {
  studyNotice = "";
  location.hash = "#" + track + "/" + sub;
}

function studySetProject(track, id) {
  studySelected[track] = id;
  studyCardSel.clear();
  studyCache = {};
  render();
}

function studyNoticeHtml() {
  if (!studyNotice) return "";
  return `<div class="study-notice">${esc(studyNotice)}
    <button class="study-x" onclick="studyClearNotice()" aria-label="关闭">×</button></div>`;
}

function studyClearNotice() {
  studyNotice = "";
  if (studyReviewing) studyPaintReview(); else render();
}

function studyPaintReview() {
  // main.js's poll guard deliberately skips rebuilding #view while a review is
  // open. Explicit review actions therefore paint the player directly; the
  // guard only protects it from the 5s poll.
  const view = document.getElementById("view");
  if (view) view.innerHTML = studyReviewView();
}

function studyCloseModal() {
  studyModal = null;
  editing = false;
}

// --------------------------------------------------------------------------
// API
// --------------------------------------------------------------------------
async function studyCall(payload) {
  try {
    const r = await postJSON("/api/study", payload);
    if (!r || !r.ok) {
      studyNotice = (r && r.error) || "操作失败";
      return null;
    }
    if (r.data) D.study_info = r.data;
    return r;
  } catch (error) {
    studyNotice = String((error && error.message) || error);
    return null;
  }
}

async function studyWrite(payload) {
  studyBusy = true;
  studyNotice = "";
  const r = await studyCall(payload);
  studyBusy = false;
  if (!r) { render(); return null; }
  studyCache = {};
  studyCloseModal();
  render();
  return r;
}

async function studyModalWrite(payload, onResult) {
  studyBusy = true;
  studyNotice = "";
  const r = await studyCall(payload);
  studyBusy = false;
  if (!r) {
    if (studyModal) {
      studyModal.error = studyNotice || "操作失败";
      studyNotice = "";
    }
    render();
    studyRenderModalContent();
    return null;
  }
  studyCache = {};
  if (onResult) onResult(r.result);
  render();
  studyRenderModalContent();
  return r;
}

// Plan / units / cards / audio are read on demand, never in the 5s payload.
function studyEnsure(track, sub, project) {
  if (!project) return;
  const projectId = project.project.id;
  const key = studyCacheKey(track, sub, projectId);
  if (studyCache[key]) return;
  let payload = null;
  if (sub === "plan") payload = {action: "plan", project_id: projectId};
  else if (sub === "materials") payload = {action: "list_units", project_id: projectId};
  else if (sub === "cards") payload = {action: "list_cards", project_id: projectId, limit: 500};
  else if (sub === "vocab") payload = {
    action: "list_cards", project_id: projectId, card_type: "vocab", limit: 1000};
  else if (sub === "listening") payload = {action: "list_audio", project_id: projectId};
  if (!payload) return;
  studyCache[key] = {loading: true};
  postJSON("/api/study", payload).then((r) => {
    studyCache[key] = (r && r.ok) ? {result: r.result}
      : {error: (r && r.error) || "加载失败"};
    if (!studyReviewing && location.hash.startsWith("#" + track)) render();
  }).catch((error) => {
    studyCache[key] = {error: String((error && error.message) || error)};
    if (!studyReviewing && location.hash.startsWith("#" + track)) render();
  });
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------
function studyPage(d, track, sub) {
  if (studyLastHash && studyLastHash !== location.hash) studyNotice = "";
  studyLastHash = location.hash;
  if (studyReviewing && studyReview && studyReview.track === track) {
    // Navigating to another tab/page ends the review instead of trapping the
    // user in the player; clicking the same page's tab leaves it running.
    if (studyReview.hash && location.hash !== studyReview.hash) {
      studyReviewing = false;
      studyReview = null;
    } else {
      return studyReviewView();
    }
  }
  const project = studyProject(d, track);
  if (!project) return studyEmpty(track);
  studyEnsure(track, sub, project);
  return studyHeader(project, track, sub) + studyTabs(project, track, sub)
    + studyNoticeHtml() + studyContent(d, track, sub, project)
    + '<div id="study-modal-root"></div>';
}

function studyEmpty(track) {
  const label = track === "exam" ? "考试备考" : "语言学习";
  return `<div class="card study-empty">
    <div class="study-kicker">${label}</div>
    <div class="study-empty-title">还没有学习项目</div>
    <p class="meta">新建一个项目，上传考纲/教材或课文，系统会切出单元、排期，并把复习卡片排进每天。</p>
    <button class="save" onclick="studyOpenProjectModal('${track}')">＋ 新建项目</button>
    <div id="study-modal-root"></div>
  </div>`;
}

function studyHeader(project, track, sub) {
  const p = project.project;
  const stats = project.stats || {};
  let subline = "";
  if (track === "exam") {
    subline = [
      stats.exam_days != null ? `距考试 ${stats.exam_days} 天` : "未设考试日期",
      `每天 ${p.daily_minutes} 分钟`,
      `覆盖 ${studyPct(stats.coverage)}%`,
      `单元 ${stats.units_done || 0}/${stats.units_total || 0}`,
      `连续 ${stats.streak || 0} 天`,
    ].join(" · ");
  } else {
    const today = project.today || {};
    subline = [
      stats.exam_days != null ? `距考试 ${stats.exam_days} 天` : "日常提升",
      `活跃词 ${stats.vocab_active || 0}${stats.target_items ? " / " + stats.target_items : ""}`,
      `到期 ${today.due_count || 0} · 新卡 ${today.new_count || 0}`,
      `连续 ${stats.streak || 0} 天`,
    ].join(" · ");
  }
  const projects = studyProjects(D, track);
  const switcher = projects.length > 1
    ? `<select class="study-select" onchange="studySetProject('${track}', this.value)">
        ${projects.map((item) => `<option value="${esc(item.project.id)}"
          ${item.project.id === p.id ? "selected" : ""}>${esc(item.project.title)}</option>`).join("")}
      </select>` : "";
  return `<div class="study-head">
    <div class="study-head-main">
      <div class="study-kicker">${track === "exam" ? "EXAM PREP" : "LANGUAGE"}</div>
      <h2 class="study-title">${esc(p.title)}</h2>
      <div class="study-sub">${subline}${p.status !== "active" ? " · " + esc(p.status) : ""}</div>
    </div>
    <div class="study-head-actions">
      ${switcher}
      <button class="save ghost" onclick="studyOpenProjectModal('${track}')">＋ 新建项目</button>
    </div>
  </div>`;
}

function studyTabs(project, track, sub) {
  const candidate = (project.stats && project.stats.cards_by_status
    && project.stats.cards_by_status.candidate) || 0;
  const audio = (project.audio || []).length;
  const tabs = track === "exam"
    ? [["today", "今日"], ["plan", "计划"], ["materials", "资料与单元"],
       ["cards", "卡片审核", candidate]]
    : [["today", "今日"], ["vocab", "词汇"], ["listening", "听力", audio],
       ["materials", "资料与单元"], ["cards", "卡片审核", candidate]];
  return subtabBar(track, tabs, sub);
}

function studyContent(d, track, sub, project) {
  if (track === "exam") {
    if (sub === "plan") return studyExamPlan(project);
    if (sub === "materials") return studyMaterials(project, track);
    if (sub === "cards") return studyCards(project, track);
    return studyExamToday(project);
  }
  if (sub === "vocab") return studyVocab(project);
  if (sub === "listening") return studyListening(project);
  if (sub === "materials") return studyMaterials(project, track);
  if (sub === "cards") return studyCards(project, track);
  return studyLanguageToday(project);
}

// --------------------------------------------------------------------------
// Today
// --------------------------------------------------------------------------
function studyExamToday(project) {
  const p = project.project;
  const today = project.today || {items: [], due_count: 0, new_count: 0};
  const items = today.items || [];
  const units = items.flatMap((item) => item.units || []);
  const planMinutes = items.reduce((sum, item) => sum + (item.minutes || 0), 0);
  const stats = project.stats || {};
  return `
    <div class="tiles">
      <div class="tile"><b>${planMinutes}</b><span>今日计划分钟</span></div>
      <div class="tile"><b>${today.due_count || 0}</b><span>到期复习</span></div>
      <div class="tile"><b>${stats.units_done || 0}<span class="study-dim"> / ${stats.units_total || 0}</span></b><span>已完成单元</span></div>
      <div class="tile"><b>${stats.streak || 0}</b><span>连续学习天数</span></div>
    </div>
    ${items.length ? "" : `<div class="card study-inline-empty">
      <b>还没有生成计划</b>
      <div class="meta">先到「资料与单元」添加资料并提取单元，再生成排期。</div>
      <button class="save" onclick="studyBuildPlan('${esc(p.id)}')">生成计划</button>
    </div>`}
    <div class="study-grid2">
      <div>
        <div class="study-section"><h2>今天的学习</h2>
          <span class="meta">${units.length} 个单元 · ${planMinutes} 分钟</span></div>
        <div class="card study-list">
          ${units.length ? units.map((unit) => studyUnitRow(unit, true)).join("")
            : '<div class="empty">今天没有安排学习单元。</div>'}
        </div>
        ${studyCoverageCard(project)}
      </div>
      <aside>
        ${studyReviewCta(today, p.id)}
        <div class="card">
          <div class="study-kicker">资料</div>
          ${(project.materials || []).length
            ? project.materials.map((m) => `<div class="study-material-line">
                <span class="role">${esc(studyRoleLabel(m.role))}</span>
                <span class="study-ellipsis">${esc(m.title)}</span></div>`).join("")
            : '<div class="empty">还没有关联资料。</div>'}
          <button class="save ghost study-wide" onclick="studyGo('exam', 'materials')">去资料与单元</button>
        </div>
      </aside>
    </div>`;
}

function studyCoverageCard(project) {
  const stats = project.stats || {};
  const value = studyPct(stats.coverage);
  return `<div class="card study-coverage">
    <div class="study-row"><span class="study-kicker">考前覆盖</span>
      <span class="study-spacer"></span><b class="study-mono">${value}%</b></div>
    <div class="study-progress"><span style="width:${value}%"></span></div>
    <div class="meta">按每天 ${project.project.daily_minutes} 分钟计算；覆盖不完的部分会明确列出，不假装排得下。</div>
  </div>`;
}

function studyReviewCta(today, projectId) {
  const due = today.due_count || 0;
  const fresh = today.new_count || 0;
  return `<div class="card study-review-cta">
    <div class="study-kicker">到期复习</div>
    <div class="study-big">${due}</div>
    <div class="meta">张到期卡 · ${fresh} 张新卡可学</div>
    <button class="save study-wide" ${due + fresh ? "" : "disabled"}
      onclick="studyStartReview('${esc(projectId)}', '${studyTrackOf(projectId)}')">开始复习</button>
    <div class="meta">评分会写入复习日志，下次到期由 SM-2 重新计算。</div>
  </div>`;
}

function studyTrackOf(projectId) {
  const item = (studyInfo(D).projects || []).find((x) => x.project.id === projectId);
  return item ? item.project.kind : "exam";
}

function studyLanguageToday(project) {
  const p = project.project;
  const stats = project.stats || {};
  const today = project.today || {items: [], due_count: 0, new_count: 0};
  const items = (today.items || []).filter((item) => item.kind !== "review"
    || item.minutes);
  const mix = studyMixItems(project, items);
  return `
    <div class="tiles">
      <div class="tile"><b>${stats.vocab_active || 0}</b><span>活跃词</span>
        ${stats.target_items ? `<small>目标 ${stats.target_items}</small>` : ""}</div>
      <div class="tile"><b>${stats.target_coverage != null ? studyPct(stats.target_coverage) : "—"}%</b><span>目标覆盖率</span></div>
      <div class="tile"><b>${today.due_count || 0}</b><span>到期复习</span></div>
      <div class="tile"><b>${stats.streak || 0}</b><span>连续学习天数</span></div>
    </div>
    <div class="study-section"><h2>今日配比</h2><span class="meta">按考试日与每日 ${p.daily_minutes} 分钟生成</span></div>
    <div class="study-mix-grid">
      ${mix.map((item) => studyMixCard(project, item)).join("")}
    </div>
    <div class="study-section"><h2>建议顺序</h2><span class="meta">一个入口，减少每天做决定</span></div>
    <div class="card study-flow">
      ${mix.map((item, index) => `${index ? '<span class="study-arrow">›</span>' : ""}
        <span class="study-step"><b>${String(index + 1).padStart(2, "0")}</b>${esc(item.label)}</span>`).join("")}
    </div>
    <div class="card" style="margin-top:14px">
      <div class="study-kicker">语言进度</div>
      <div class="study-row"><span class="meta">成熟词</span><span class="study-spacer"></span><b class="study-mono">${stats.vocab_mature || 0}</b></div>
      <div class="study-row"><span class="meta">未学词</span><span class="study-spacer"></span><b class="study-mono">${stats.vocab_unseen || 0}</b></div>
      <div class="study-row"><span class="meta">近 7 天正确率</span><span class="study-spacer"></span>
        <b class="study-mono">${stats.accuracy_7d != null ? studyPct(stats.accuracy_7d) + "%" : "—"}</b></div>
      <button class="save ghost study-wide" onclick="studyGo('language', 'vocab')">打开词汇</button>
    </div>`;
}

function studyMixItems(project, items) {
  const labels = {vocab_new: "新词", review: "复习", listening: "听力",
                  speaking: "口语", reading: "阅读", writing: "写作"};
  const titles = {vocab_new: "今日新词", review: "到期复习", listening: "听写练习",
                  speaking: "话题练习", reading: "精读", writing: "句子写作"};
  if (items.length) {
    return items.map((item) => ({
      kind: item.kind,
      label: labels[item.kind] || item.kind,
      title: titles[item.kind] || labels[item.kind] || item.kind,
      value: studyMixValue(item, project),
      minutes: item.minutes || 0,
      target: item.target || {},
    }));
  }
  const mix = (project.project.settings || {}).mix || {};
  return [
    {kind: "vocab_new", label: "新词", title: "今日新词", value: `${mix.new_words || 0} 个`, minutes: 0},
    {kind: "review", label: "复习", title: "到期复习", value: `${(project.today || {}).due_count || 0} 张`, minutes: 0},
    {kind: "listening", label: "听力", title: "听写练习", value: `${mix.listening_min || 0} 分钟`, minutes: mix.listening_min || 0},
    {kind: "speaking", label: "口语", title: "话题练习", value: `${mix.speaking_min || 0} 分钟`, minutes: mix.speaking_min || 0},
    {kind: "reading", label: "阅读", title: "精读", value: `${mix.reading_items || 0} 篇`, minutes: 0},
    {kind: "writing", label: "写作", title: "句子写作", value: `${mix.writing_sentences || 0} 句`, minutes: 0},
  ];
}

function studyMixValue(item, project) {
  const target = item.target || {};
  if (item.kind === "vocab_new") return `${target.new_words || 0} 个`;
  if (item.kind === "review") {
    const today = (project && project.today) || {};
    return `${(today.due_count || 0) + (today.new_count || 0)} 张`;
  }
  if (item.kind === "reading") return `${target.items || 0} 篇`;
  if (item.kind === "writing") return `${target.sentences || 0} 句`;
  return `${item.minutes || 0} 分钟`;
}

function studyMixAction(kind, projectId) {
  // Keep each handler name inside a literal onclick="..." here: bootstrap.js
  // exports only names it can see in the source, and an indirection object would
  // leave these four functions undefined at click time.
  if (kind === "vocab_new" || kind === "review") {
    return `onclick="studyStartReview('${esc(projectId)}', 'language')"`;
  }
  if (kind === "listening") return 'onclick="studyGo(\'language\', \'listening\')"';
  if (kind === "speaking") {
    return `onclick="studyOpenSpeaking('${esc(projectId)}')"`;
  }
  if (kind === "reading") return 'onclick="studyGo(\'language\', \'materials\')"';
  if (kind === "writing") return `onclick="studyOpenWriting('${esc(projectId)}')"`;
  return "";
}

function studyMixCard(project, item) {
  return `<div class="study-mix">
    <div class="study-row"><span class="study-kicker">${esc(item.label)}</span>
      <span class="study-spacer"></span><b class="study-mix-value">${esc(item.value)}</b></div>
    <div class="study-mix-title">${esc(item.title)}</div>
    <div class="meta">${item.minutes ? `计划 ${item.minutes} 分钟` : "按今日计划"}</div>
    <button class="save ghost study-wide" ${studyMixAction(item.kind, project.project.id)}>开始</button>
  </div>`;
}

// --------------------------------------------------------------------------
// Plan / materials / cards
// --------------------------------------------------------------------------
function studyExamPlan(project) {
  const cache = studyCache[studyCacheKey("exam", "plan", project.project.id)];
  if (!cache || cache.loading) return '<div class="card empty">正在加载计划…</div>';
  if (cache.error) return `<div class="card empty">${esc(cache.error)}</div>`;
  const plan = cache.result;
  if (!plan) {
    return `<div class="card study-inline-empty">
      <b>还没有生成计划</b>
      <div class="meta">先生成计划，系统会按考试日期和每天可用时间排出学习单元。</div>
      <button class="save" onclick="studyBuildPlan('${esc(project.project.id)}')">生成计划</button>
    </div>`;
  }
  return `<div class="study-plan-note">
    <b>当前计划 v${plan.version}</b>
    <div>${esc(plan.note)}</div>
    <div class="meta">${plan.horizon_start} → ${plan.horizon_end}</div>
    <button class="save ghost" onclick="studyReplan('${esc(project.project.id)}')">重排剩余计划</button>
  </div>
  <div class="card study-list">
    ${(plan.items || []).map((item) => studyPlanRow(item)).join("")
      || '<div class="empty">计划里还没有条目。</div>'}
  </div>
  ${studyVersions(project)}`;
}

function studyPlanRow(item) {
  const names = (item.units || []).map((unit) => unit.title).join("、");
  return `<div class="study-plan-row">
    <div class="study-plan-date"><b>${esc(item.date.slice(5))}</b><span>${esc(item.date)}</span></div>
    <div class="study-plan-body">
      <span class="study-chip ${item.kind === "learn" ? "learn" : ""}">${esc(studyKindLabel(item.kind))}</span>
      <span>${esc(names || (item.kind === "review" ? "复习与缓冲" : "—"))}</span>
    </div>
    <div class="study-mono study-plan-minutes">${item.minutes || 0} 分钟</div>
  </div>`;
}

function studyVersions(project) {
  const versions = project.plan_versions || [];
  if (!versions.length) return "";
  return `<div class="study-section"><h2>版本历史</h2><span class="meta">旧版本只留档，不删除</span></div>
    <div class="card study-list">
      ${versions.map((v) => `<div class="study-version">
        <span class="study-version-v">v${v.version}</span>
        <span class="study-spacer"></span>
        <span class="badge ${v.status === "active" ? "retrieve" : ""}">${v.status === "active" ? "生效中" : "已归档"}</span>
        <span class="meta">${esc(v.created_at.slice(0, 10))} · ${esc(v.horizon_start)} → ${esc(v.horizon_end)}</span>
      </div>`).join("")}
    </div>`;
}

function studyMaterials(project, track) {
  const projectId = project.project.id;
  const cache = studyCache[studyCacheKey(track, "materials", projectId)];
  const units = cache && cache.result ? cache.result : [];
  const materials = project.materials || [];
  return `<div class="study-section"><h2>项目资料</h2>
    <span class="meta">复用文档库 · 不复制文件</span></div>
  <div class="card study-list">
    ${materials.length ? materials.map((m) => `<div class="study-material-row">
      <span class="role">${esc(studyRoleLabel(m.role))}</span>
      <div class="study-material-main">
        <div class="study-material-title">${esc(m.title)}</div>
        <div class="meta">${esc(m.doc_kind)} · ${m.chars || 0} 字</div>
      </div>
      <button class="save ghost study-small" onclick="studyRemoveMaterial('${esc(projectId)}', '${esc(m.doc_id)}')">移除</button>
    </div>`).join("") : '<div class="empty">还没有关联资料。先添加教材、考纲或课文。</div>'}
  </div>
  <div class="study-actions">
    <button class="save" onclick="studyOpenMaterial('${esc(projectId)}')">＋ 添加资料</button>
    <button class="save ghost" onclick="studyRebuildUnits('${esc(projectId)}')">重建单元</button>
    ${track === "language" ? `<button class="save ghost" onclick="studyOpenImport('${esc(projectId)}')">导入词表</button>` : ""}
  </div>
  <div class="study-section"><h2>单元</h2>
    <span class="meta">${units.length} 个 · 按顺序</span></div>
  <div class="card study-list">
    ${cache && cache.loading ? '<div class="empty">正在加载单元…</div>'
      : units.length ? units.map((unit) => studyUnitRow(unit, false, track)).join("")
      : '<div class="empty">还没有单元。添加资料后点「重建单元」。</div>'}
  </div>`;
}

function studyUnitRow(unit, today, track) {
  const status = unit.status === "done" ? "已完成"
    : unit.status === "learning" ? "学习中" : "未开始";
  const tone = unit.status === "done" ? "good" : unit.status === "learning" ? "warn" : "";
  const pages = unit.page_start
    ? (unit.page_start === unit.page_end ? `第 ${unit.page_start} 页` : `第 ${unit.page_start}–${unit.page_end} 页`)
    : "";
  const source = track === "language" ? "提取词条" : "生成卡片";
  const busy = studyBusyUnit === unit.id;
  return `<div class="study-unit">
    <span class="study-dot ${unit.status === "done" ? "done" : unit.status === "learning" ? "learning" : "new"}"></span>
    <div class="study-unit-main">
      <div class="study-unit-title">${esc(unit.title)}</div>
      <div class="meta">约 ${unit.est_minutes} 分钟${pages ? " · " + pages : ""}</div>
    </div>
    <span class="badge ${tone}">${status}</span>
    <div class="study-unit-actions">
      <button class="save ghost study-small" onclick="studyOpenUnit('${esc(unit.id)}')">读</button>
      <button class="save ghost study-small" ${busy ? "disabled" : ""}
        onclick="studyGenerate('${esc(unit.id)}', '${track || "exam"}')">${busy ? "生成中…" : source}</button>
      ${today && unit.status !== "done" ? `<button class="save study-small" onclick="studyMarkUnit('${esc(unit.id)}')">完成</button>` : ""}
    </div>
  </div>`;
}

function studyCards(project, track) {
  const projectId = project.project.id;
  const key = studyCacheKey(track, "cards", projectId);
  const cache = studyCache[key];
  if (!cache || cache.loading) return '<div class="card empty">正在加载卡片…</div>';
  if (cache.error) return `<div class="card empty">${esc(cache.error)}</div>`;
  const cards = cache.result || [];
  const candidates = cards.filter((card) => card.status === "candidate");
  const active = cards.filter((card) => card.status !== "candidate");
  return `<div class="study-section"><h2>待确认候选</h2>
    <span class="meta">${candidates.length} 张 · 确认后才进入复习</span></div>
  <div class="card study-list">
    <div class="study-actions">
      <button class="save ghost study-small" onclick="studySelectAllCards()">全选</button>
      <button class="save ghost study-small" onclick="studyOpenManualCard('${esc(projectId)}')">＋ 手动建卡</button>
      <span class="meta study-spacer"></span>
      <button class="save study-small" ${candidates.length ? "" : "disabled"}
        onclick="studyAcceptSelected('${esc(projectId)}')">确认选中</button>
    </div>
    ${candidates.length ? candidates.map((card) => studyCandidateRow(card)).join("")
      : '<div class="empty">没有待确认的卡片。去资料与单元生成，或导入词表。</div>'}
  </div>
  <div class="study-section"><h2>已激活</h2>
    <span class="meta">${active.length} 张 · 到期 ${(project.stats || {}).due || 0} 张</span></div>
  <div class="card study-list">
    ${active.length ? active.map((card) => studyActiveCardRow(card)).join("")
      : '<div class="empty">还没有已激活的卡片。</div>'}
  </div>`;
}

function studyCandidateRow(card) {
  const checked = studyCardSel.has(card.id) ? "checked" : "";
  const extra = card.extra || {};
  const meaning = card.card_type === "vocab" && extra.phonetic
    ? `${esc(extra.phonetic)} · ${esc(card.back)}` : esc(card.back);
  return `<div class="study-cand">
    <input type="checkbox" ${checked} onchange="studyToggleCard('${esc(card.id)}')">
    <div class="study-cand-main">
      <div class="study-cand-front">${esc(card.front)}</div>
      <div class="study-cand-back">${meaning}</div>
      ${card.source_quote ? `<div class="study-source">${esc(card.source_quote)}</div>` : ""}
    </div>
    <button class="save ghost study-small" onclick="studyOpenEditCard('${esc(card.id)}')">编辑</button>
  </div>`;
}

function studyActiveCardRow(card) {
  const due = card.due ? ` · 到期 ${esc(card.due)}` : "";
  return `<div class="study-cand">
    <span class="study-dot ${card.status === "suspended" ? "" : "new"}"></span>
    <div class="study-cand-main">
      <div class="study-cand-front">${esc(card.front)}</div>
      <div class="study-cand-back">${esc(card.back)}</div>
      <div class="meta">${esc(studyTypeLabel(card.card_type))} · ${esc(studyStatusLabel(card.status))}${due}</div>
    </div>
    <div class="study-unit-actions">
      <button class="save ghost study-small" onclick="studySetCardStatus('${esc(card.id)}', '${card.status === "suspended" ? "active" : "suspended"}')">${card.status === "suspended" ? "恢复" : "暂停"}</button>
      <button class="save ghost study-small" onclick="studyDeleteCard('${esc(card.id)}')">删除</button>
    </div>
  </div>`;
}

function studyVocab(project) {
  const projectId = project.project.id;
  const key = studyCacheKey("language", "vocab", projectId);
  const cache = studyCache[key];
  if (!cache || cache.loading) return '<div class="card empty">正在加载词汇…</div>';
  if (cache.error) return `<div class="card empty">${esc(cache.error)}</div>`;
  const cards = (cache.result || []).filter((card) => card.card_type === "vocab");
  const filtered = cards.filter((card) => {
    const stage = studyCardStage(card);
    if (studyVocabFilter !== "all" && stage.key !== studyVocabFilter) return false;
    const hay = `${card.front} ${card.back} ${(card.extra || {}).example || ""}`.toLowerCase();
    return !studyVocabQuery || hay.includes(studyVocabQuery.toLowerCase());
  });
  return `<div class="study-toolbar">
    <input class="study-input" placeholder="搜索单词、释义或例句…" value="${esc(studyVocabQuery)}"
      oninput="studySetVocabQuery(this.value)">
    <div class="study-chips">
      ${[["all", "全部"], ["new", "新词"], ["learning", "学习中"], ["mature", "成熟"]].map(([k, label]) =>
        `<button class="study-chip-btn ${studyVocabFilter === k ? "on" : ""}" onclick="studySetVocabFilter('${k}')">${label}</button>`).join("")}
    </div>
    <span class="study-spacer"></span>
    <button class="save ghost" onclick="studyOpenImport('${esc(projectId)}')">导入词表</button>
    <button class="save" onclick="studyStartReview('${esc(projectId)}', 'language')">开始复习</button>
  </div>
  <div class="card study-list" id="study-vocab-list">
    ${filtered.length ? filtered.map((card) => studyVocabRow(card, project)).join("")
      : '<div class="empty">没有匹配的词条。</div>'}
  </div>`;
}

function studyVocabRow(card, project) {
  const extra = card.extra || {};
  const stage = studyCardStage(card);
  const lang = (project.project.target_language || "en-US") === "英语" ? "en-US"
    : (project.project.target_language || "en-US");
  return `<div class="study-vocab-row">
    <button class="study-speak" onclick="studySpeak(${JSON.stringify(card.front)}, '${esc(lang)}')" title="朗读">▶</button>
    <div class="study-vocab-main">
      <div><span class="study-word">${esc(card.front)}</span>
        ${extra.phonetic ? `<span class="study-phon">${esc(extra.phonetic)}</span>` : ""}
        ${extra.pos ? `<span class="study-pos">${esc(extra.pos)}</span>` : ""}</div>
      <div class="study-meaning">${esc(card.back)}</div>
      ${extra.example ? `<div class="meta">${esc(extra.example)}</div>` : ""}
      ${card.source_quote ? `<div class="study-source">${esc(card.source_quote)}</div>` : ""}
    </div>
    <span class="study-stage ${stage.key}">${stage.label}</span>
    <span class="study-due">${card.due ? "到期 " + esc(card.due) : ""}</span>
  </div>`;
}

function studyCardStage(card) {
  if (card.status === "candidate") return {key: "new", label: "待确认"};
  if (!card.reps) return {key: "new", label: "新词"};
  if (card.interval_days >= 21) return {key: "mature", label: "成熟"};
  return {key: "learning", label: "学习中"};
}

function studyListening(project) {
  const projectId = project.project.id;
  const key = studyCacheKey("language", "listening", projectId);
  const cache = studyCache[key];
  if (!cache || cache.loading) return '<div class="card empty">正在加载音频…</div>';
  if (cache.error) return `<div class="card empty">${esc(cache.error)}</div>`;
  const audio = cache.result || [];
  return `<div class="study-section"><h2>听力材料</h2>
    <span class="meta">有 transcript 才能建听写卡</span></div>
  <div class="card study-list">
    ${audio.length ? audio.map((item) => studyAudioRow(item)).join("")
      : '<div class="empty">还没有音频。上传一段带 transcript 的材料。</div>'}
  </div>
  <div class="study-actions">
    <button class="save" onclick="studyOpenAudio('${esc(projectId)}')">＋ 上传音频</button>
    <span class="meta">支持 mp3 / m4a / wav / ogg；文件按 uuid 存储。</span>
  </div>`;
}

function studyAudioRow(audio) {
  const transcript = audio.transcript || "";
  return `<div class="study-audio-row">
    <div class="study-audio-main">
      <div class="study-audio-title">${esc(audio.title)}</div>
      ${transcript ? `<div class="meta">${esc(transcript.slice(0, 180))}</div>` : '<div class="meta">没有 transcript，只能泛听。</div>'}
    </div>
    <audio controls preload="none" src="/api/study/audio?id=${encodeURIComponent(audio.id)}"></audio>
    <button class="save ghost study-small" ${transcript ? "" : "disabled"}
      onclick="studyCreateDictation('${esc(audio.id)}')">建听写卡</button>
  </div>`;
}

// --------------------------------------------------------------------------
// Review player
// --------------------------------------------------------------------------
async function studyStartReview(projectId, track) {
  studyBusy = true;
  studyNotice = "";
  const r = await studyCall({action: "review_queue", project_id: projectId});
  studyBusy = false;
  if (!r) { render(); return; }
  const queue = [...(r.result.due || []), ...(r.result.new || [])];
  if (!queue.length) {
    studyNotice = "今天没有到期或可学的卡片。";
    render();
    return;
  }
  const project = (studyInfo(D).projects || []).find((x) => x.project.id === projectId);
  studyReview = {
    track: track || (project ? project.project.kind : "exam"),
    projectId: projectId,
    language: (project && project.project.target_language) || "en-US",
    queue: queue,
    index: 0,
    flipped: false,
    done: false,
    correct: 0,
    again: 0,
    startedAt: Date.now(),
    checked: false,
    score: null,
    typed: "",
    hash: location.hash,
  };
  studyReviewing = true;
  studyPaintReview();
}

function studyExitReview() {
  studyReviewing = false;
  studyReview = null;
  render();
}

function studyFlip() {
  if (!studyReviewing || !studyReview || studyReview.done) return;
  if (studyReview.queue[studyReview.index].card_type === "dictation") return;
  studyReview.flipped = !studyReview.flipped;
  studyPaintReview();
}

function studyReviewView() {
  if (studyReview.done) return studyReviewSummary();
  const card = studyReview.queue[studyReview.index];
  const total = studyReview.queue.length;
  const width = Math.round(((studyReview.index + 1) / total) * 100);
  return `<div class="study-review-shell">
    <div class="study-review-top">
      <button class="save ghost study-small" onclick="studyExitReview()">退出复习</button>
      <span class="badge retrieve">${esc(studyTypeLabel(card.card_type))}</span>
      <span class="meta">${total - studyReview.index - 1} 张剩余</span>
      <span class="study-spacer"></span>
      <span class="study-mono">${studyReview.index + 1} / ${total}</span>
    </div>
    <div class="study-progress"><span style="width:${width}%"></span></div>
    ${studyNoticeHtml()}
    ${studyReviewBody(card)}
  </div>`;
}

function studyReviewBody(card) {
  if (card.card_type === "dictation") return studyDictationBody(card);
  const vocab = card.card_type === "vocab";
  const extra = card.extra || {};
  const front = vocab
    ? `<div class="study-flash-word">${esc(card.front)}</div>
       <div class="study-phon">${esc(extra.phonetic || "")} ${esc(extra.pos || "")}</div>
       <button class="save ghost study-small" onclick="event.stopPropagation(); studySpeak(${JSON.stringify(card.front)}, '${esc(studyReview.language)}')">朗读单词</button>
       <div class="study-hint">点击卡片翻面 · 空格也可以</div>`
    : `<div class="study-kicker">${esc(card.unit_id ? "学习卡片" : "卡片")}</div>
       <div class="study-flash-question">${esc(card.front)}</div>
       <div class="study-hint">点击卡片翻面 · 空格也可以</div>`;
  const back = vocab
    ? `<div class="study-kicker">释义与例句</div>
       <div class="study-flash-word study-flash-small">${esc(card.front)}</div>
       <div class="study-flash-meaning">${esc(card.back)}</div>
       ${extra.example ? `<div class="study-flash-example">${esc(extra.example)}</div>` : ""}
       ${card.source_quote ? `<div class="study-source">${esc(card.source_quote)}</div>` : ""}`
    : `<div class="study-kicker">答案</div>
       <div class="study-flash-answer">${esc(card.back)}</div>
       ${card.source_quote ? `<div class="study-source">${esc(card.source_quote)}</div>` : ""}`;
  return `<div class="study-flash" onclick="studyFlip()">${studyReview.flipped ? back : front}</div>
    ${studyReview.flipped ? studyGradeButtons() : `
      <div class="study-grade-row study-grade-single">
        <button class="study-grade good" onclick="studyFlip()"><b>翻面看答案</b><small>空格</small></button>
      </div>`}`;
}

function studyDictationBody(card) {
  const extra = card.extra || {};
  const audioId = extra.audio_id || "";
  return `<div class="card study-dictation">
    <div class="study-kicker">听这段材料，写下你听到的内容</div>
    ${audioId ? `<audio controls preload="auto" src="/api/study/audio?id=${encodeURIComponent(audioId)}"></audio>` : '<div class="meta">这张卡没有关联音频。</div>'}
    <textarea id="study-dictation-input" ${studyReview.checked ? "disabled" : ""}
      placeholder="在这里写下你听到的句子…"
      oninput="studyReview.typed=this.value">${esc(studyReview.typed || "")}</textarea>
    ${studyReview.checked ? studyDictationScore(card) : `
      <button class="save" onclick="studyCheckDictation()">检查</button>
      <span class="meta">标点、大小写不计。</span>`}
  </div>
  ${studyReview.checked ? studyGradeButtons() : ""}`;
}

function studyDictationScore(card) {
  const score = studyReview.score || 0;
  const tone = score >= 0.95 ? "good" : score >= 0.7 ? "warn" : "";
  return `<div class="study-score ${tone}">
    <b>${Math.round(score * 100)}%</b>
    <span class="meta">逐词准确率 · 原文：${esc(card.back)}</span>
  </div>`;
}

function studyGradeButtons() {
  return `<div class="study-grade-row">
    <button class="study-grade again" onclick="studyGrade(0)"><b>重来</b><small>1</small></button>
    <button class="study-grade hard" onclick="studyGrade(3)"><b>困难</b><small>2</small></button>
    <button class="study-grade good" onclick="studyGrade(4)"><b>良好</b><small>3</small></button>
    <button class="study-grade easy" onclick="studyGrade(5)"><b>简单</b><small>4</small></button>
  </div>`;
}

async function studyGrade(rating) {
  const review = studyReview;
  if (!review || review.done) return;
  const card = review.queue[review.index];
  if (card.card_type === "dictation" && !review.checked) {
    studyNotice = "先检查听写，再评分。";
    studyPaintReview();
    return;
  }
  let r;
  try {
    r = await postJSON("/api/study", {
      action: "grade_card", card_id: card.id, rating: rating,
      elapsed_ms: Date.now() - review.startedAt, score: review.score,
    });
  } catch (error) {
    studyNotice = String((error && error.message) || error);
    studyPaintReview();
    return;
  }
  if (!r || !r.ok) {
    studyNotice = (r && r.error) || "评分失败";
    studyPaintReview();
    return;
  }
  if (r.data) D.study_info = r.data;
  if (rating >= 3) review.correct += 1; else review.again += 1;
  review.index += 1;
  review.flipped = false;
  review.checked = false;
  review.score = null;
  review.typed = "";
  if (review.index >= review.queue.length) review.done = true;
  studyPaintReview();
}

function studyCheckDictation() {
  const input = document.getElementById("study-dictation-input");
  if (!input) return;
  const typed = input.value;
  if (!typed.trim()) { studyNotice = "先写下你听到的内容。"; studyPaintReview(); return; }
  const card = studyReview.queue[studyReview.index];
  studyReview.typed = typed;
  studyReview.score = studyDictationScoreValue(card.back, typed);
  studyReview.checked = true;
  studyPaintReview();
}

function studyNormalize(text) {
  return String(text || "").toLowerCase()
    .replace(/[^\w\s]/g, " ").replace(/\s+/g, " ").trim();
}

function studyDictationScoreValue(expected, actual) {
  const want = studyNormalize(expected).split(" ").filter(Boolean);
  const got = studyNormalize(actual).split(" ").filter(Boolean);
  if (!want.length) return 0;
  let same = 0;
  for (let i = 0; i < Math.min(want.length, got.length); i += 1) {
    if (want[i] === got[i]) same += 1;
  }
  return Math.round((same / want.length) * 1000) / 1000;
}

function studyReviewSummary() {
  const review = studyReview;
  const total = review.queue.length;
  return `<div class="study-review-shell">
    <div class="card study-summary">
      <div class="study-kicker">本轮完成</div>
      <div class="study-big">${total}</div>
      <div class="meta">张卡片已复习</div>
      <div class="study-summary-nums">
        <div><b class="study-good">${review.correct}</b><span class="meta">记得</span></div>
        <div><b class="study-bad">${review.again}</b><span class="meta">需要重来</span></div>
      </div>
      <div class="study-actions">
        <button class="save ghost" onclick="studyExitReview()">返回今日</button>
        <button class="save" onclick="studyRestartReview()">再复习一轮</button>
      </div>
      <div class="meta">评分已写入复习日志；下次到期由 SM-2 重新计算。</div>
    </div>
  </div>`;
}

function studyRestartReview() {
  if (!studyReview) return;
  const {projectId, track} = studyReview;
  studyReviewing = false;
  studyReview = null;
  studyStartReview(projectId, track);
}

document.addEventListener("keydown", (event) => {
  if (!studyReviewing || !studyReview || studyReview.done) return;
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  if (event.code === "Space") { event.preventDefault(); studyFlip(); return; }
  if (!["1", "2", "3", "4"].includes(event.key)) return;
  const ratings = [0, 3, 4, 5];
  const card = studyReview.queue[studyReview.index];
  if (card.card_type === "dictation") {
    if (studyReview.checked) studyGrade(ratings[Number(event.key) - 1]);
  } else if (studyReview.flipped) {
    studyGrade(ratings[Number(event.key) - 1]);
  }
});

// --------------------------------------------------------------------------
// Actions
// --------------------------------------------------------------------------
function studyBuildPlan(projectId) {
  return studyWrite({action: "build_plan", project_id: projectId});
}

function studyReplan(projectId) {
  if (!confirm("从明天开始重排剩余计划？今天已经安排的内容不会改变。")) return;
  return studyWrite({action: "replan", project_id: projectId});
}

function studyMarkUnit(unitId) {
  return studyWrite({action: "update_unit", unit_id: unitId, status: "done"});
}

async function studyGenerate(unitId, track) {
  if (studyBusy) return null;
  studyBusy = true;
  studyBusyUnit = unitId;
  studyNotice = track === "language"
    ? "正在提取词条…模型调用可能需要几秒到一分钟。"
    : "正在生成候选卡片…模型调用可能需要几秒到一分钟。";
  render();
  const r = await studyCall({
    action: track === "language" ? "extract_vocab" : "generate_cards",
    unit_id: unitId, count: 6,
  });
  studyBusy = false;
  studyBusyUnit = "";
  if (!r) {
    studyNotice = (studyNotice || "生成失败") + " 也可以到「卡片审核」手动建卡。";
    render();
    return null;
  }
  studyCache = {};
  const created = (r.result && r.result.created) || [];
  const rejected = (r.result && r.result.rejected) || [];
  studyNotice = `已生成 ${created.length} 张候选卡（拒收 ${rejected.length} 张）。`;
  if (r.result && r.result.fallback) {
    studyNotice += " 摘要模型没有回复，已改用主模型。";
  }
  if (r.result && r.result.truncated) {
    studyNotice += " 模型输出被截断，只收到完整的那几张。";
  }
  if (!created.length && rejected.length) {
    studyNotice += " 首个拒收原因：" + (rejected[0].reason || "未知") + "。";
  }
  studyNotice += " 去「卡片审核」逐张确认后才进入复习。";
  render();
  return r;
}

function studyRemoveMaterial(projectId, docId) {
  if (!confirm("把这份资料从项目里移除？文档库里的原文件不受影响。")) return;
  return studyWrite({action: "remove_material", project_id: projectId, doc_id: docId});
}

function studyRebuildUnits(projectId) {
  if (!confirm("重建单元会重新切分项目资料；已有卡片时会被拒绝。")) return;
  return studyWrite({action: "extract_units", project_id: projectId});
}

function studyToggleCard(id) {
  if (studyCardSel.has(id)) studyCardSel.delete(id); else studyCardSel.add(id);
}

function studySelectAllCards() {
  const project = studyProject(D, activeView);
  if (!project) return;
  const cache = studyCache[studyCacheKey(activeView, "cards", project.project.id)];
  const cards = (cache && cache.result) || [];
  const candidates = cards.filter((card) => card.status === "candidate");
  const all = candidates.every((card) => studyCardSel.has(card.id));
  candidates.forEach((card) => {
    if (all) studyCardSel.delete(card.id); else studyCardSel.add(card.id);
  });
  render();
}

function studyAcceptSelected(projectId) {
  if (!studyCardSel.size) { studyNotice = "先选中至少一张候选卡。"; render(); return; }
  studyWrite({action: "accept_cards", card_ids: [...studyCardSel]}).then(() => {
    studyCardSel.clear();
  });
}

function studySetCardStatus(cardId, status) {
  return studyWrite({action: "set_card_status", card_id: cardId, status: status});
}

function studyDeleteCard(cardId) {
  if (!confirm("删除这张卡片？它的复习记录也会一起删除。")) return;
  return studyWrite({action: "delete_card", card_id: cardId});
}

function studySetVocabQuery(value) {
  studyVocabQuery = value;
  const list = document.getElementById("study-vocab-list");
  const project = studyProject(D, "language");
  if (list && project) list.innerHTML = studyVocabRows(project);
}

function studyVocabRows(project) {
  const key = studyCacheKey("language", "vocab", project.project.id);
  const cards = ((studyCache[key] || {}).result || []).filter((card) => card.card_type === "vocab");
  const filtered = cards.filter((card) => {
    if (studyVocabFilter !== "all" && studyCardStage(card).key !== studyVocabFilter) return false;
    const hay = `${card.front} ${card.back} ${(card.extra || {}).example || ""}`.toLowerCase();
    return !studyVocabQuery || hay.includes(studyVocabQuery.toLowerCase());
  });
  return filtered.length ? filtered.map((card) => studyVocabRow(card, project)).join("")
    : '<div class="empty">没有匹配的词条。</div>';
}

function studySetVocabFilter(value) {
  studyVocabFilter = value;
  render();
}

function studySpeak(text, lang) {
  if (!("speechSynthesis" in window)) { studyNotice = "当前浏览器不支持语音朗读。"; render(); return; }
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = lang || "en-US";
  utterance.rate = 0.9;
  window.speechSynthesis.speak(utterance);
}

function studyCreateDictation(audioId) {
  return studyWrite({action: "create_dictation", audio_id: audioId}).then((r) => {
    if (r) studyNotice = "已建候选听写卡；去「卡片审核」确认。";
    render();
  });
}

// --------------------------------------------------------------------------
// Modals
// --------------------------------------------------------------------------
function studyOpenModal(kind, payload) {
  studyModal = {kind: kind, payload: payload || {}, data: null, error: ""};
  markEditing();
  render();
  studyRenderModalContent();
}

function studyOpenProjectModal(track) {
  studyOpenModal("project", {track: track});
}

async function studyOpenMaterial(projectId) {
  let r;
  try { r = await postJSON("/api/library", {action: "list"}); }
  catch (error) { studyNotice = String((error && error.message) || error); render(); return; }
  if (!r || !r.ok) { studyNotice = (r && r.error) || "文档库打不开"; render(); return; }
  studyModal = {kind: "material", payload: {projectId: projectId},
                data: {documents: r.documents || []}, error: ""};
  markEditing(); render(); studyRenderModalContent();
}

async function studyOpenUnit(unitId) {
  let r;
  try { r = await postJSON("/api/study", {action: "read_unit", unit_id: unitId}); }
  catch (error) { studyNotice = String((error && error.message) || error); render(); return; }
  if (!r || !r.ok) { studyNotice = (r && r.error) || "单元打不开"; render(); return; }
  studyModal = {kind: "unit", payload: {unitId: unitId}, data: r.result, error: ""};
  markEditing(); render(); studyRenderModalContent();
}

function studyOpenEditCard(cardId) {
  const project = studyProject(D, activeView);
  if (!project) return;
  const cache = studyCache[studyCacheKey(activeView, "cards", project.project.id)];
  const card = ((cache && cache.result) || []).find((item) => item.id === cardId);
  if (!card) return;
  studyOpenModal("card", {card: card});
}

async function studyOpenManualCard(projectId) {
  let units = [];
  try {
    const r = await postJSON("/api/study", {action: "list_units", project_id: projectId});
    if (r && r.ok) units = r.result || [];
  } catch (error) {
    units = [];
  }
  studyModal = {kind: "manual", payload: {projectId: projectId, units: units}, error: ""};
  markEditing();
  render();
  studyRenderModalContent();
}

function studyOpenImport(projectId) {
  studyOpenModal("import", {projectId: projectId});
}

function studyOpenAudio(projectId) {
  studyOpenModal("audio", {projectId: projectId});
}

function studyOpenWriting(projectId) {
  studyOpenModal("writing", {projectId: projectId});
}

function studyOpenSpeaking(projectId) {
  studyOpenModal("speaking", {projectId: projectId});
}

function studyRenderModalContent() {
  const root = document.getElementById("study-modal-root");
  if (!root) return;
  if (!studyModal) { root.innerHTML = ""; return; }
  root.innerHTML = `<div class="connmodal-back" onclick="studyModalBack(event)">
    <section class="connmodal study-modal" role="dialog" aria-modal="true"
      onclick="event.stopPropagation()" onkeydown="studyModalKey(event)">
      ${studyModalHtml()}
    </section>
  </div>`;
}

function studyModalBack(event) {
  if (event.target === event.currentTarget) studyClose();
}

function studyModalKey(event) {
  if (event.key === "Escape") studyClose();
}

function studyClose() {
  studyCloseModal();
  render();
}

function studyModalHtml() {
  const modal = studyModal;
  if (!modal) return "";
  if (modal.kind === "project") return studyProjectModal(modal);
  if (modal.kind === "material") return studyMaterialModal(modal);
  if (modal.kind === "unit") return studyUnitModal(modal);
  if (modal.kind === "import") return studyImportModal(modal);
  if (modal.kind === "audio") return studyAudioModal(modal);
  if (modal.kind === "writing") return studyWritingModal(modal);
  if (modal.kind === "manual") return studyManualCardModal(modal);
  if (modal.kind === "card") return studyCardModal(modal);
  if (modal.kind === "speaking") return studySpeakingModal(modal);
  return "";
}

function studyModalHead(title, subtitle) {
  return `<header class="connmodal-head">
    <div class="connmodal-title"><h3>${esc(title)}</h3>
      ${subtitle ? `<div class="meta">${esc(subtitle)}</div>` : ""}</div>
    <button class="connmodal-close" type="button" onclick="studyClose()" aria-label="关闭">关闭</button>
  </header>`;
}

function studyField(label, id, value, type, hint) {
  return `<label class="study-field"><span>${esc(label)}</span>
    <input class="study-input" id="${id}" type="${type || "text"}" value="${esc(value || "")}"
      ${hint ? `placeholder="${esc(hint)}"` : ""} onfocus="markEditing()"></label>`;
}

function studyProjectModal(modal) {
  const track = modal.payload.track || "exam";
  const language = track === "language";
  return `${studyModalHead("新建学习项目", language ? "语言学习" : "考试备考")}
    ${modal.error ? `<div class="study-notice">${esc(modal.error)}</div>` : ""}
    ${studyField("项目名称", "study-np-title", language ? "英语 · CET-6" : "高等数学 · 期末冲刺")}
    <div class="study-field-row">
      ${studyField(language ? "考试日期（可选）" : "考试日期", "study-np-exam", "", "date")}
      ${studyField("每天可用分钟", "study-np-daily", language ? "60" : "90", "number")}
    </div>
    ${language ? `<div class="study-field-row">
      ${studyField("目标语言", "study-np-lang", "英语")}
      ${studyField("当前水平（可选）", "study-np-level", "B2")}
      ${studyField("目标词量（可选）", "study-np-target", "4500", "number")}
    </div>` : '<div class="meta">创建后从文档库关联教材、考纲或笔记；没有资料就没有单元。</div>'}
    <div class="connmodal-actions">
      <button class="save" onclick="studyCreateProject('${track}')" ${studyBusy ? "disabled" : ""}>创建项目</button>
      <button class="save ghost" onclick="studyClose()">取消</button>
    </div>`;
}

async function studyCreateProject(track) {
  const title = (document.getElementById("study-np-title") || {}).value || "";
  const exam = (document.getElementById("study-np-exam") || {}).value || "";
  const daily = Number((document.getElementById("study-np-daily") || {}).value || 60);
  const payload = {
    action: "create_project", kind: track, title: title.trim(),
    exam_date: exam || null, daily_minutes: daily,
    start_date: studyLocalDate(),
  };
  if (track === "language") {
    payload.target_language = (document.getElementById("study-np-lang") || {}).value || "";
    payload.level = (document.getElementById("study-np-level") || {}).value || "";
    const target = (document.getElementById("study-np-target") || {}).value;
    if (target) payload.target_items = Number(target);
  }
  if (!payload.title) { studyModal.error = "项目名称不能为空"; studyRenderModalContent(); return; }
  if (track === "exam" && !exam) {
    studyModal.error = "考试项目需要考试日期";
    studyRenderModalContent();
    return;
  }
  const r = await studyModalWrite(payload, (result) => {
    studySelected[track] = result.id;
    studyModal = null;
    editing = false;
  });
  if (r) studyNotice = "已创建项目：" + payload.title;
  render();
}

function studyMaterialModal(modal) {
  const documents = (modal.data && modal.data.documents) || [];
  const busy = modal.uploading ? "disabled" : "";
  return `${studyModalHead("添加资料", "上传自己的文件，或从文档库关联已有资料")}
    ${modal.error ? `<div class="study-notice">${esc(modal.error)}</div>` : ""}
    ${modal.uploading ? '<div class="study-notice">正在解析文件并提取单元…大文件可能要几十秒。</div>' : ""}
    <div class="study-upload-box">
      <div class="study-kicker">上传自己的资料</div>
      <label class="study-field"><span>文件</span>
        <input class="study-input" type="file" id="study-material-file"
          accept=".pdf,.md,.markdown,.txt,.html,.htm,.json,.csv,.xml,.py,.js,.ts,.sh" onfocus="markEditing()"></label>
      <label class="study-field"><span>角色</span>
        <select class="study-input" id="study-mat-role">
          ${(studyInfo(D).material_roles || []).map((role) =>
            `<option value="${esc(role.key)}" ${role.key === "textbook" ? "selected" : ""}>${esc(role.label)}</option>`).join("")}
        </select></label>
      <button class="save" onclick="studyUploadMaterial('${esc(modal.payload.projectId)}')" ${busy}>上传并关联</button>
      <div class="meta">上传后会提取单元；已有卡片时不会重建单元。</div>
    </div>
    <div class="study-upload-box">
      <div class="study-kicker">从文档库关联</div>
      ${documents.length ? `
        <label class="study-field"><span>文档</span>
          <select class="study-input" id="study-mat-doc">
            ${documents.map((doc) => `<option value="${esc(doc.id)}">${esc(doc.title)} · ${esc(doc.kind)} · ${doc.chars || 0} 字</option>`).join("")}
          </select></label>
        <button class="save ghost" onclick="studyAddMaterial('${esc(modal.payload.projectId)}')" ${busy}>关联已有资料</button>`
        : '<div class="empty">文档库还是空的。上面的上传入口可以直接开始。</div>'}
    </div>
    <div class="connmodal-actions">
      <button class="save ghost" onclick="studyClose()">取消</button>
    </div>`;
}

async function studyMaterialFollowup(projectId) {
  const extract = await postJSON("/api/study", {action: "extract_units", project_id: projectId});
  if (extract && extract.data) D.study_info = extract.data;
  if (!extract || !extract.ok) {
    return {ok: false, error: (extract && extract.error) || "单元提取失败", plan: false};
  }
  const units = (extract.result && extract.result.units) || 0;
  const project = (studyInfo(D).projects || []).find((item) => item.project.id === projectId);
  const hasPlan = project && (project.plan_versions || []).length > 0;
  let plan = false;
  let planError = "";
  if (!hasPlan) {
    const built = await postJSON("/api/study", {action: "build_plan", project_id: projectId});
    if (built && built.data) D.study_info = built.data;
    plan = !!(built && built.ok);
    if (!plan) planError = (built && built.error) || "计划生成失败";
  }
  return {ok: true, units: units, plan: plan, planError: planError};
}

async function studyUploadMaterial(projectId) {
  const fileInput = document.getElementById("study-material-file");
  const file = fileInput && fileInput.files && fileInput.files[0];
  const role = (document.getElementById("study-mat-role") || {}).value || "textbook";
  if (!file) {
    studyModal.error = "先选择一个文件";
    studyRenderModalContent();
    return;
  }
  studyBusy = true;
  studyModal.error = "";
  studyModal.uploading = true;
  studyRenderModalContent();
  let payload;
  try {
    const agent = (typeof ACTIVE_AGENT !== "undefined" && ACTIVE_AGENT) || "default";
    const url = "/api/library/upload?name=" + encodeURIComponent(file.name)
      + "&agent_id=" + encodeURIComponent(agent);
    const response = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": file.type || "application/octet-stream"},
      body: file,
    });
    payload = await response.json();
  } catch (error) {
    studyBusy = false;
    studyModal.uploading = false;
    studyModal.error = "上传失败：" + String((error && error.message) || error);
    studyRenderModalContent();
    return;
  }
  if (!payload || !payload.ok) {
    studyBusy = false;
    studyModal.uploading = false;
    studyModal.error = (payload && payload.error) || "上传失败";
    studyRenderModalContent();
    return;
  }

  const add = await postJSON("/api/study", {
    action: "add_material", project_id: projectId,
    doc_id: payload.document.id, role: role,
  });
  if (!add || !add.ok) {
    studyBusy = false;
    studyModal.uploading = false;
    studyModal.error = "文件已上传到文档库，但关联失败：" + ((add && add.error) || "未知错误");
    studyRenderModalContent();
    return;
  }
  if (add.data) D.study_info = add.data;

  const follow = await studyMaterialFollowup(projectId);
  studyCache = {};
  studyBusy = false;
  studyModal = null;
  editing = false;
  studyNotice = `已上传《${payload.document.title}》并关联。`;
  if (follow.ok) {
    studyNotice += ` 提取了 ${follow.units} 个单元。`;
    if (follow.plan) studyNotice += " 已生成第一版计划。";
    else if (follow.planError) studyNotice += " " + follow.planError;
  } else {
    studyNotice += " 单元提取：" + follow.error;
  }
  render();
}

async function studyAddMaterial(projectId) {
  const docId = (document.getElementById("study-mat-doc") || {}).value;
  const role = (document.getElementById("study-mat-role") || {}).value || "textbook";
  if (!docId) return;
  studyBusy = true;
  studyModal.error = "";
  studyModal.uploading = true;
  studyRenderModalContent();
  const add = await postJSON("/api/study", {
    action: "add_material", project_id: projectId, doc_id: docId, role: role,
  });
  if (!add || !add.ok) {
    studyBusy = false;
    studyModal.uploading = false;
    studyModal.error = (add && add.error) || "关联失败";
    studyRenderModalContent();
    return;
  }
  if (add.data) D.study_info = add.data;
  const follow = await studyMaterialFollowup(projectId);
  studyCache = {};
  studyBusy = false;
  studyModal = null;
  editing = false;
  studyNotice = "资料已关联。";
  if (follow.ok) {
    studyNotice += ` 提取了 ${follow.units} 个单元。`;
    if (follow.plan) studyNotice += " 已生成第一版计划。";
    else if (follow.planError) studyNotice += " " + follow.planError;
  } else {
    studyNotice += " 单元提取：" + follow.error;
  }
  render();
}

function studyUnitModal(modal) {
  const data = modal.data || {};
  const unit = data.unit || {};
  const doc = data.document || {};
  return `${studyModalHead(unit.title || "单元", `${doc.title || ""}${unit.page_start ? " · 第 " + unit.page_start + " 页" : ""}`)}
    <pre class="study-pre">${esc(data.text || "")}</pre>
    ${data.has_more ? '<div class="meta">这个单元还有更多内容。</div>' : ""}
    <div class="connmodal-actions">
      <button class="save ghost" onclick="studyClose()">关闭</button>
      <button class="save" onclick="studyClose(); studyGenerate('${esc(unit.id)}', '${esc(studyTrackOf(unit.project_id))}')">生成卡片</button>
    </div>`;
}

function studyImportModal(modal) {
  return `${studyModalHead("导入词表", "每行 word=释义 / word<Tab>释义 / word  释义")}
    ${modal.error ? `<div class="study-notice">${esc(modal.error)}</div>` : ""}
    <label class="study-field"><span>词表内容</span>
      <textarea class="study-input study-textarea" id="study-import-text"
        onfocus="markEditing()" placeholder="meticulous=一丝不苟的"></textarea></label>
    <div class="meta">已有词条按词形去重；导入后进入待确认。</div>
    <div class="connmodal-actions">
      <button class="save" onclick="studyImportVocab('${esc(modal.payload.projectId)}')">导入</button>
      <button class="save ghost" onclick="studyClose()">取消</button>
    </div>`;
}

async function studyImportVocab(projectId) {
  const text = (document.getElementById("study-import-text") || {}).value || "";
  if (!text.trim()) { studyModal.error = "先粘贴词表"; studyRenderModalContent(); return; }
  const r = await studyModalWrite({action: "import_vocab", project_id: projectId, text: text},
                                  () => { studyModal = null; editing = false; });
  if (r) {
    const added = (r.result && r.result.added) || [];
    studyNotice = `导入完成：新增 ${added.length} 个，重复 ${(r.result || {}).duplicates || 0} 个。`;
  }
  render();
}

function studyAudioModal(modal) {
  return `${studyModalHead("上传听力音频", "有 transcript 才能建听写卡")}
    ${modal.error ? `<div class="study-notice">${esc(modal.error)}</div>` : ""}
    <label class="study-field"><span>音频文件</span>
      <input class="study-input" type="file" id="study-audio-file" accept="audio/*"></label>
    <label class="study-field"><span>标题</span>
      <input class="study-input" id="study-audio-title" value="新听力材料" onfocus="markEditing()"></label>
    <label class="study-field"><span>Transcript（可稍后补）</span>
      <textarea class="study-input study-textarea" id="study-audio-transcript"
        onfocus="markEditing()" placeholder="粘贴这段音频的原文…"></textarea></label>
    <div class="connmodal-actions">
      <button class="save" onclick="studyUploadAudio('${esc(modal.payload.projectId)}')" ${studyBusy ? "disabled" : ""}>上传</button>
      <button class="save ghost" onclick="studyClose()">取消</button>
    </div>`;
}

async function studyUploadAudio(projectId) {
  const input = document.getElementById("study-audio-file");
  const file = input && input.files && input.files[0];
  if (!file) { studyModal.error = "先选择一个音频文件"; studyRenderModalContent(); return; }
  const title = (document.getElementById("study-audio-title") || {}).value || file.name;
  const transcript = (document.getElementById("study-audio-transcript") || {}).value || "";
  studyBusy = true;
  let r;
  try {
    const response = await fetch(`/api/study/audio/upload?project_id=${encodeURIComponent(projectId)}&name=${encodeURIComponent(file.name)}`, {
      method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: file,
    });
    r = await response.json();
  } catch (error) {
    studyBusy = false;
    studyModal.error = String((error && error.message) || error);
    studyRenderModalContent();
    return;
  }
  studyBusy = false;
  if (!r || !r.ok) { studyModal.error = (r && r.error) || "上传失败"; studyRenderModalContent(); return; }
  if (r.data) D.study_info = r.data;
  if (transcript.trim() && r.result && r.result.id) {
    const updated = await postJSON("/api/study", {
      action: "update_audio", audio_id: r.result.id,
      title: title, transcript: transcript,
    });
    if (updated && updated.data) D.study_info = updated.data;
  }
  studyCache = {};
  studyModal = null;
  editing = false;
  studyNotice = "音频已上传。";
  render();
}

function studyWritingModal(modal) {
  const data = modal.data;
  if (data && data.corrected) {
    return `${studyModalHead("写作批改", "修改稿与说明")}
      <div class="study-corrected">${esc(data.corrected)}</div>
      <ul class="study-notes">${(data.notes || []).map((note) => `<li>${esc(note)}</li>`).join("")}</ul>
      <div class="meta">不输出分数或等级；只给修改建议。</div>
      <div class="connmodal-actions">
        <button class="save" onclick="studyClose()">完成</button>
      </div>`;
  }
  return `${studyModalHead("写作练习", "提交后返回修改稿与简短说明")}
    ${modal.error ? `<div class="study-notice">${esc(modal.error)}</div>` : ""}
    <label class="study-field"><span>你的句子</span>
      <textarea class="study-input study-textarea" id="study-writing-text"
        onfocus="markEditing()" placeholder="Write a few sentences…"></textarea></label>
    <div class="connmodal-actions">
      <button class="save" onclick="studySubmitWriting('${esc(modal.payload.projectId)}')" ${studyBusy ? "disabled" : ""}>保存并批改</button>
      <button class="save ghost" onclick="studyClose()">取消</button>
    </div>`;
}

async function studySubmitWriting(projectId) {
  const text = (document.getElementById("study-writing-text") || {}).value || "";
  if (!text.trim()) { studyModal.error = "先写几句"; studyRenderModalContent(); return; }
  await studyModalWrite({action: "submit_writing", project_id: projectId,
                         text: text, minutes: 3},
                        (result) => { studyModal.data = result; });
}

function studyManualCardModal(modal) {
  const projectId = modal.payload.projectId;
  const track = studyTrackOf(projectId);
  const types = track === "language"
    ? [["vocab", "词汇"], ["translation", "翻译"], ["grammar", "语法"], ["cloze", "填空"]]
    : [["qa", "问答"], ["cloze", "填空"]];
  const units = modal.payload.units || [];
  return `${studyModalHead("手动建卡", "模型不可用时也能继续；建好后进入待确认")}
    ${modal.error ? `<div class="study-notice">${esc(modal.error)}</div>` : ""}
    <label class="study-field"><span>卡片类型</span>
      <select class="study-input" id="study-manual-type">
        ${types.map(([key, label]) => `<option value="${key}">${label}</option>`).join("")}
      </select></label>
    <label class="study-field"><span>关联单元（可选）</span>
      <select class="study-input" id="study-manual-unit">
        <option value="">不关联单元</option>
        ${units.map((unit) => `<option value="${esc(unit.id)}">${esc(unit.title)}</option>`).join("")}
      </select></label>
    <label class="study-field"><span>正面 / 问题</span>
      <textarea class="study-input study-textarea" id="study-manual-front"
        onfocus="markEditing()" placeholder="例如：拉格朗日中值定理的条件是什么？"></textarea></label>
    <label class="study-field"><span>背面 / 答案</span>
      <textarea class="study-input study-textarea" id="study-manual-back"
        onfocus="markEditing()" placeholder="例如：闭区间连续、开区间可导……"></textarea></label>
    <label class="study-field"><span>原文引文（可选）</span>
      <textarea class="study-input study-textarea" id="study-manual-quote"
        onfocus="markEditing()" placeholder="从资料里复制一句原文，方便以后核对"></textarea></label>
    <div class="connmodal-actions">
      <button class="save" onclick="studyCreateManualCard('${esc(projectId)}')">保存为候选卡</button>
      <button class="save ghost" onclick="studyClose()">取消</button>
    </div>`;
}

async function studyCreateManualCard(projectId) {
  const cardType = (document.getElementById("study-manual-type") || {}).value || "qa";
  const unitId = (document.getElementById("study-manual-unit") || {}).value || null;
  const front = (document.getElementById("study-manual-front") || {}).value || "";
  const back = (document.getElementById("study-manual-back") || {}).value || "";
  const quote = (document.getElementById("study-manual-quote") || {}).value || "";
  if (!front.trim() || !back.trim()) {
    studyModal.error = "正面和背面都不能为空";
    studyRenderModalContent();
    return;
  }
  const r = await studyModalWrite({
    action: "add_card", project_id: projectId, card_type: cardType,
    unit_id: unitId, front: front, back: back, source_quote: quote,
  }, () => { studyModal = null; editing = false; });
  if (r) {
    studyNotice = "已创建候选卡；到「卡片审核」确认后进入复习。";
    render();
  }
}

function studyCardModal(modal) {
  const card = modal.payload.card || {};
  return `${studyModalHead("编辑候选卡", card.source_quote || "")}
    ${modal.error ? `<div class="study-notice">${esc(modal.error)}</div>` : ""}
    <label class="study-field"><span>正面 / 问题</span>
      <textarea class="study-input study-textarea" id="study-edit-front"
        onfocus="markEditing()">${esc(card.front || "")}</textarea></label>
    <label class="study-field"><span>背面 / 答案</span>
      <textarea class="study-input study-textarea" id="study-edit-back"
        onfocus="markEditing()">${esc(card.back || "")}</textarea></label>
    <div class="connmodal-actions">
      <button class="save" onclick="studySaveCard('${esc(card.id)}')">保存</button>
      <button class="save ghost" onclick="studyClose()">取消</button>
    </div>`;
}

async function studySaveCard(cardId) {
  const front = (document.getElementById("study-edit-front") || {}).value || "";
  const back = (document.getElementById("study-edit-back") || {}).value || "";
  if (!front.trim() || !back.trim()) { studyModal.error = "正面和背面都不能为空"; studyRenderModalContent(); return; }
  await studyModalWrite({action: "update_card", card_id: cardId,
                         front: front, back: back},
                        () => { studyModal = null; editing = false; });
}

function studySpeakingModal(modal) {
  return `${studyModalHead("口语练习", "和 Language Tutor 对话，系统只记录练习分钟")}
    <div class="card study-speaking-prompt">
      <div class="study-kicker">话题</div>
      <div class="study-speaking-text">Describe a skill you would like to learn, and explain why.</div>
      <div class="meta">提示：讲清楚“是什么、为什么、怎么练”，用 3–5 句。</div>
    </div>
    <div class="connmodal-actions">
      <button class="save" onclick="studyClose(); openAgent('language')">打开 Language Tutor</button>
      <button class="save ghost" onclick="studyLogSpeaking('${esc(modal.payload.projectId)}')">记录 10 分钟</button>
      <button class="save ghost" onclick="studyClose()">取消</button>
    </div>`;
}

async function studyLogSpeaking(projectId) {
  await studyModalWrite({action: "log_session", project_id: projectId,
                         kind: "speaking", minutes: 10},
                        () => { studyModal = null; editing = false; });
  studyNotice = "已记录口语练习 10 分钟。";
  render();
}

VIEWS.exam = function(d, sub) {
  return studyPage(d, "exam", sub || "today");
};

VIEWS.language = function(d, sub) {
  return studyPage(d, "language", sub || "today");
};