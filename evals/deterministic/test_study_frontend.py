"""The study page: two tracks, the review player, and no network in tests.

This runs the REAL study.js against a DOM stub in node.
The claims are about control flow and the wire shape, not pixels:

  1. both tracks install a VIEWS entry and render their own project header/tabs;
  2. exam Today shows units and a review CTA; language Today shows the daily mix;
  3. the review player flips, grades through /api/study, and reaches a summary;
  4. a generated candidate is never silently accepted;
  5. study.js is in the bundle, the sidebar has both entries, and main.js guards
     the poll while a review is open.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = (Path(__file__).resolve().parents[2]
          / "zhiyan" / "ops" / "static")
JS = STATIC / "js"
STUDY_JS = JS / "study.js"
MAIN_JS = JS / "main.js"
BOOTSTRAP_JS = JS / "bootstrap.js"
INDEX_HTML = STATIC / "index.html"

HARNESS = r"""
const fs = require("fs");
const studySrc = fs.readFileSync(process.argv[2], "utf8");
let failures = 0;
const assert = (ok, msg) => {
  if (!ok) failures++;
  console.log((ok ? "PASS  " : "FAIL  ") + msg);
};
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function subtabBar(view, tabs, active) {
  return tabs.map(([key, label, n]) =>
    `<a href="#${view}/${key}">${label}${n == null ? "" : "(" + n + ")"}</a>`).join("");
}
const viewNode = {innerHTML: ""};
const modalNode = {innerHTML: ""};
const document = {
  addEventListener() {},
  getElementById(id) {
    if (id === "view") return viewNode;
    if (id === "study-modal-root") return modalNode;
    return null;
  },
  querySelector() { return null; }, querySelectorAll() { return []; },
  activeElement: null,
};
const window = {speechSynthesis: null};
let editing = false, renderCount = 0;
function markEditing() { editing = true; }
function render() { renderCount++; }
function openAgent() {}
const confirm = () => true;
const location = {hash: "#exam/today"};
let D = null;
let postCalls = [];
let nextQueue = [];
let nextGradeData = null;
async function postJSON(url, body) {
  postCalls.push({url, ...body});
  if (url === "/api/library") {
    return {ok: true, documents: [{id: "d1", title: "教材", kind: "md", chars: 100}]};
  }
  if (body.action === "review_queue") return {ok: true, result: {due: [], new: nextQueue}};
  if (body.action === "grade_card") return {ok: true, data: nextGradeData || D.study_info, result: {}};
  return {ok: true, result: null};
}

const VIEWS = {};
const api = eval(studySrc + `
;({VIEWS, studyReviewView, studyStartReview, studyFlip, studyGrade, studyExitReview,
   studyOpenProjectModal, studyOpenMaterial, studyOpenManualCard,
   getReviewing: () => studyReviewing, getReview: () => studyReview,
   setCache: (key, value) => { studyCache[key] = value; },
   dictationScore: studyDictationScoreValue})
`);

const examUnit = {id: "u1", title: "微分中值定理", status: "learning",
                  est_minutes: 45, page_start: 35, page_end: 52};
const examProject = {
  project: {id: "e1", kind: "exam", title: "高等数学 · 期末冲刺",
            daily_minutes: 90, status: "active", settings: {}, target_language: ""},
  stats: {exam_days: 14, coverage: 0.83, units_done: 9, units_total: 31,
          streak: 12, due: 8, cards_by_status: {candidate: 2, active: 8}},
  today: {items: [{date: "2026-11-18", kind: "learn", minutes: 80,
                   units: [examUnit]}], due_count: 8, new_count: 2, sessions: []},
  materials: [{doc_id: "d1", role: "textbook", title: "高等数学（同济版）",
               doc_kind: "pdf", chars: 1000}],
  audio: [], plan_versions: [],
};
const languageCard = {
  id: "v1", card_type: "vocab", front: "meticulous", back: "一丝不苟的",
  extra: {phonetic: "/məˈtɪkjələs/", pos: "adj.",
          example: "She is meticulous about details."},
  source_quote: "She is meticulous about details.", status: "active",
  reps: 1, interval_days: 3, due: "2026-11-20",
};
const languageProject = {
  project: {id: "l1", kind: "language", title: "英语 · CET-6",
            daily_minutes: 60, status: "active", target_language: "英语",
            settings: {mix: {new_words: 15, listening_min: 10, speaking_min: 10,
                             reading_items: 1, writing_sentences: 3}}},
  stats: {exam_days: 31, vocab_active: 3560, target_items: 4500,
          vocab_mature: 1640, vocab_unseen: 940, target_coverage: 0.79,
          due: 12, streak: 21, accuracy_7d: 0.81,
          cards_by_status: {candidate: 1, active: 12}},
  today: {items: [
    {date: "2026-11-18", kind: "vocab_new", minutes: 10, target: {new_words: 15}},
    {date: "2026-11-18", kind: "review", minutes: 18, target: {}},
    {date: "2026-11-18", kind: "listening", minutes: 10, target: {minutes: 10}},
    {date: "2026-11-18", kind: "speaking", minutes: 10, target: {minutes: 10}},
    {date: "2026-11-18", kind: "reading", minutes: 15, target: {items: 1}},
    {date: "2026-11-18", kind: "writing", minutes: 9, target: {sentences: 3}},
  ], due_count: 12, new_count: 15, sessions: []},
  materials: [], audio: [{id: "a1", title: "CET-6 听力", duration_sec: 154,
                          has_transcript: true, created_at: "2026-11-01"}],
  plan_versions: [{id: "p1", version: 3, horizon_start: "2026-11-18",
                   horizon_end: "2026-12-01", status: "active",
                   note: "每天 60 分钟", created_at: "2026-11-18T08:00:00"}],
};
D = {study_info: {projects: [examProject, languageProject], material_roles: [],
                  card_types: []}};

(async () => {
  assert(typeof api.VIEWS.exam === "function", "study.js installs VIEWS.exam");
  assert(typeof api.VIEWS.language === "function", "study.js installs VIEWS.language");

  const emptyHtml = api.VIEWS.exam({study_info: {projects: []}});
  assert(emptyHtml.includes('id="study-modal-root"'),
         "the empty state still gives the new-project modal a place to render");
  api.studyOpenProjectModal("exam");
  assert(modalNode.innerHTML.includes("新建学习项目"),
         "clicking + 新建项目 on the empty page actually opens the modal");

  await api.studyOpenMaterial("l1");
  assert(modalNode.innerHTML.includes('type="file"'),
         "the material modal offers a real file upload, not only existing library docs");
  assert(modalNode.innerHTML.includes("上传并关联"),
         "the upload path tells the user what it will do");

  await api.studyOpenManualCard("l1");
  assert(modalNode.innerHTML.includes("手动建卡")
         && modalNode.innerHTML.includes("study-manual-front"),
         "the cards page can still create a card when the model is unavailable");

  const examHtml = api.VIEWS.exam(D);
  assert(examHtml.includes("高等数学 · 期末冲刺"), "exam header names the project");
  assert(examHtml.includes("微分中值定理"), "today shows the planned unit");
  assert(examHtml.includes("开始复习"), "today offers the review CTA");
  assert(examHtml.includes('href="#exam/plan"'), "exam has a plan tab");
  assert(examHtml.includes("覆盖 83%"), "coverage is reported from stats");
  assert(!examHtml.includes("今日配比"), "exam page does not show the language mix");

  api.setCache("exam:plan:e1", {result: {
    version: 3, horizon_start: "2026-11-18", horizon_end: "2026-12-01",
    note: "按每天 90 分钟，覆盖 83%。",
    items: [{date: "2026-11-18", kind: "learn", minutes: 45, units: [examUnit]}],
  }});
  const planHtml = api.VIEWS.exam(D, "plan");
  assert(planHtml.includes("v3") && planHtml.includes("微分中值定理"),
         "the plan tab renders the active plan and its unit");
  assert(planHtml.includes("重排剩余计划"), "the plan tab offers replanning");

  api.setCache("exam:materials:e1", {result: [examUnit]});
  const materialsHtml = api.VIEWS.exam(D, "materials");
  assert(materialsHtml.includes("重建单元") && materialsHtml.includes("微分中值定理"),
         "materials renders units and the rebuild action");

  const langHtml = api.VIEWS.language(D);
  assert(langHtml.includes("今日配比"), "language today shows the daily mix");
  assert(langHtml.includes("今日新词") && langHtml.includes("听力"),
         "the mix includes vocabulary and listening");
  assert(langHtml.includes("活跃词 3560"), "language header reports active words");

  api.setCache("language:vocab:l1", {result: [languageCard]});
  const vocabHtml = api.VIEWS.language(D, "vocab");
  assert(vocabHtml.includes("meticulous") && vocabHtml.includes("学习中"),
         "vocab list renders a word and its stage");
  assert(vocabHtml.includes("导入词表"), "vocab offers word-list import");

  api.setCache("language:listening:l1", {result: [{
    id: "a1", title: "CET-6 听力", transcript: "The university has announced...",
    }]});
  const listeningHtml = api.VIEWS.language(D, "listening");
  assert(listeningHtml.includes("/api/study/audio?id=a1"),
         "listening plays the archived audio through the study route");
  assert(listeningHtml.includes("建听写卡"), "listening offers dictation creation");

  api.setCache("language:cards:l1", {result: [
    {id: "c1", card_type: "qa", front: "问题", back: "答案",
     source_quote: "原文", status: "candidate", due: null, reps: 0,
     interval_days: 0, extra: {}},
  ]});
  const cardsHtml = api.VIEWS.language(D, "cards");
  assert(cardsHtml.includes("确认选中") && cardsHtml.includes("待确认"),
         "candidate cards are explicitly pending confirmation");

  // --- the review player ---------------------------------------------------
  nextQueue = [languageCard];
  await api.studyStartReview("l1", "language");
  assert(api.getReviewing() === true, "starting a review enters review mode");
  assert(viewNode.innerHTML.includes("meticulous"),
         "entering a review paints the player directly (the poll guard would skip render)");
  const frontHtml = api.studyReviewView();
  assert(frontHtml.includes("meticulous"), "the review front is the word");
  api.studyFlip();
  const backHtml = api.studyReviewView();
  assert(backHtml.includes("一丝不苟的"), "flipping shows the meaning");
  await api.studyGrade(4);
  const last = postCalls[postCalls.length - 1];
  assert(last.action === "grade_card" && last.rating === 4,
         "grading posts grade_card with the chosen rating");
  assert(api.studyReviewView().includes("本轮完成"),
         "the last card ends on a summary");
  api.studyExitReview();
  assert(api.getReviewing() === false, "exiting review leaves review mode");

  assert(api.dictationScore("The quick brown fox", "the quick") === 0.5,
         "dictation scoring is positional and normalized");
  assert(api.dictationScore("The quick brown fox", "THE QUICK BROWN FOX") === 1,
         "case and punctuation do not change a correct dictation");

  process.exit(failures ? 1 : 0);
})().catch(err => {
  console.log("FAIL  the harness threw: " + (err && err.stack || err));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def node():
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not installed — the frontend has no other test runner")
    return exe


def test_the_study_pages_and_review_player(node, tmp_path):
    for path in (STUDY_JS, MAIN_JS, BOOTSTRAP_JS, INDEX_HTML):
        assert path.exists(), f"missing frontend source: {path}"
    harness = tmp_path / "study_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")

    proc = subprocess.run(
        [node, str(harness), str(STUDY_JS)],
        capture_output=True, text=True, encoding="utf-8", timeout=60)

    print(proc.stdout)
    assert proc.returncode == 0, f"frontend checks failed:\n{proc.stdout}\n{proc.stderr}"


def test_study_is_wired_into_the_shell_and_the_poll_guard():
    index = INDEX_HTML.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP_JS.read_text(encoding="utf-8")
    main = MAIN_JS.read_text(encoding="utf-8")
    assert 'data-v="exam"' in index and 'data-v="language"' in index
    assert 'data-agent="exam"' in index and 'data-agent="language"' in index
    assert '"study.js"' in bootstrap, "bootstrap must load study.js"
    assert "studyReviewing" in main, "the poll must know about the review player"
    assert 'exam:"考试学习"' in main and 'language:"语言学习"' in main