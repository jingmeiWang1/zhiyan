"""The Reader must survive the dashboard's 5 second refresh.

Regression lock for a live bug: after picking a file the page alerted
"请选择一个文件", intermittently.  render() rebuilt the whole #view innerHTML on
every poll, which destroyed the <input type="file"> while the native picker was
still open; readerLoad() then re-queried the DOM by id and found the fresh,
empty replacement -- so the file the user had just chosen was nowhere.

The intermittency was the tell: it only happened when a poll landed inside the
few seconds the picker was open.

These tests run the REAL frontend source in node against a minimal DOM stub.
That is deliberate -- the bug lives in control flow (which branch render()
takes, what readerLoad() reads), not in how anything renders, so a headless
browser would prove less for a lot more machinery.  node is a hard requirement
of this file and a soft one elsewhere, so skip rather than fail without it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parents[2]
      / "zhiyan" / "ops" / "static" / "js")
MAIN_JS = JS / "main.js"
# The Reader moved out of main.js into its own file when it grew a document
# library and a PDF renderer. render() stayed put — the harness slices it out of
# main.js by shape — but the reader's own functions are read from reader.js.
READER_JS = JS / "reader.js"
# Same reason as reader.js: the harness loads util.js WHOLESALE rather than
# copying anything out of it, so a test that needs renderMarkdown() can feed the
# citation code the markup renderMarkdown ACTUALLY produces.  Hand-writing that
# markup is how the code-block case below passed while the real page was broken.
UTIL_JS = JS / "util.js"

# The harness reads main.js, lifts out the functions under test, and drives them
# on stubs.  It prints one PASS/FAIL line per claim and exits non-zero if any
# failed, so a failure here shows the user which claim broke, in English.
HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");        // main.js: the loop
const readerSrc = fs.readFileSync(process.argv[3], "utf8");  // reader.js: the app
const utilSrc = fs.readFileSync(process.argv[4], "utf8");    // util.js: renderMarkdown
// Loaded whole, so any test that needs to know what a reply LOOKS like on screen
// asks the renderer instead of writing markup by hand.  (const/let stay inside
// this eval; only the function declarations -- renderMarkdown, mdInline, ... --
// come through, which is all the tests below use.)
eval(utilSrc);
let failures = 0;
const assert = (ok, msg) => {
  if (!ok) failures++;
  console.log((ok ? "PASS  " : "FAIL  ") + msg);
};

// ---- render(): does the 5s poll rebuild the Reader's DOM? -------------------
(function testRender() {
  const renderFn = src.slice(src.indexOf("function setCount("),
                             src.indexOf("\nlet lastFetch"));
  let activeView = null, activeSub = null, animating = false;
  const VIEWS = { reader: () => "<reader>", loop: () => "<loop>" };
  const TITLES = { reader: "阅读器" };
  const D = { provider: "p", model: "m", chat_log: [], stats: { turns: 0, tool_errors: 0 },
              graph: { stats: { quick: 0, full: 0 } }, facts: [], episodes: [],
              calendar: [], outbox: [], db: { all_tables: [] }, home: "H",
              agents: [], current_sessions: {} };
  const els = {};
  const document = {
    querySelectorAll: () => [],
    querySelector: () => ({ scrollTop: 0 }),
    getElementById: id => (els[id] = els[id] || { textContent: "", innerHTML: "", style: {} }),
  };
  const window = { addEventListener() {}, getSelection: () => ({ toString: () => "" }) };
  const esc = s => String(s);
  const location = { hash: "#reader" };
  let restoreCalls = 0, wireCalls = 0;
  function restoreReaderState() { restoreCalls++; }
  function wireChat() { wireCalls++; }
  function syncAgentChrome() {}
  eval(renderFn);

  // The user is already on the Reader, with a document loaded. A poll arrives.
  activeView = "reader"; activeSub = null;
  els["view"] = { innerHTML: "<rendered>", style: {} };
  render();
  assert(els["view"].innerHTML === "<rendered>",
         "a poll does NOT rebuild the reader (the file input survives)");
  assert(restoreCalls === 1,
         "a poll still calls restoreReaderState() (the document stays painted)");
  assert(wireCalls === 1,
         "a poll still wires the Reader's ask panel (its composer is generated markup)");

  render(); render();
  assert(els["view"].innerHTML === "<rendered>", "repeated polls still do not rebuild");

  // Navigating away and back must still rebuild -- the guard is about the
  // poll, not about refusing to ever redraw this view.
  location.hash = "#loop";
  render();
  assert(els["view"].innerHTML === "<loop>", "navigating away rebuilds the target view");
  location.hash = "#reader";
  render();
  assert(els["view"].innerHTML === "<reader>", "navigating back rebuilds the reader");
})();

// ---- the Reader: repaint only when the document or its length changed -------
// Runs the REAL reader.js against a DOM stub rather than slicing a function out
// of it. The reader now collaborates with the library list, the chunker and a
// PDF branch, so driving it end to end proves more than lifting one function
// would -- and it does not break every time a helper is renamed.
const pendingTests = [];
pendingTests.push((function testReaderPane() {
  let els, paints, fetches = 0;
  function watch(el) {
    let v = el.innerHTML;
    Object.defineProperty(el, "innerHTML", { get: () => v, set(nv) { v = nv; paints++; } });
  }
  function reset() {
    els = {};
    for (const id of ["rc-content", "rc-title", "rc-meta", "rc-selection", "rc-text",
                      "rc-library", "rc-search", "rc-file-input", "rc-url-input",
                      "amsg", "ra-title", "rc-doc-acts"])
      els[id] = { id, innerHTML: "", dataset: {}, style: {}, textContent: "", value: "",
                  classList: { add() {}, remove() {}, toggle() {} },
                  appendChild() {}, insertAdjacentHTML() {}, focus() {} };
    // the library list is watched too: it must not flicker on the poll either
    paints = 0;
    watch(els["rc-content"]); watch(els["rc-library"]);
  }
  const VIEWS = {};
  const document = {
    getElementById: id => els[id] || null,
    createElement: () => ({ style: {}, append() {}, classList: { add() {} } }),
  };
  const window = { getSelection: () => ({ toString: () => "" }), devicePixelRatio: 1 };
  // node has no localStorage. The reader keeps the open document's id there so a
  // full page refresh comes back to the same document -- and readerOpen() writes
  // to it unconditionally, so without a stub the whole harness dies on a
  // ReferenceError instead of reporting on the logic under test.
  const storage = {};
  const localStorage = {
    getItem: k => (k in storage ? storage[k] : null),
    setItem: (k, v) => { storage[k] = String(v); },
    removeItem: k => { delete storage[k]; },
  };
  const esc = s => String(s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // markdown rendering is util.js's job and is tested by using the app; here it
  // only has to be a pure function of the text so the stamps can be compared.
  const renderMarkdown = t => `<md>${String(t)}</md>`;
  const ACTIVE_AGENT = "default", SESSION = "s1";
  // main.js owns the real render(); here it only has to report that it ran, so
  // toggleAskPanel() can be checked for asking for one. NOT a const: clearing
  // activeView is exactly how the reader makes render() rebuild the view.
  let activeView = "reader", renderCalls = 0;
  function render() { renderCalls++; }
  const alert = m => { throw new Error("unexpected alert: " + m); };
  const confirm = () => true;
  let renameTo = null;                          // what the rename dialog returns
  const prompt = () => renameTo;
  // The ask panel paints with the SAME primitives as the main conversation
  // (histItem/histLogClass live in render.js), and opening a document now moves
  // the panel's thread — so both are reachable from here for the first time and
  // both need a stub, or the harness dies on a ReferenceError instead of
  // reporting on the logic under test.
  const histItem = m => m;
  const askPaints = [];
  function syncLogClass(cls, chat, empty, force){
    askPaints.push({cls, count: chat.length, force: !!force});
  }
  const D = { current_sessions: { reader: "sr" } };
  let opened = null;
  // Every /api/extras body, so the Context Bridge publish can be checked for the
  // agent it is filed under (see the assertions after readerOpen below).
  const extras = [];
  // Every /api/session body: which thread the panel is switched to (and which
  // one it reads) is the whole point of one-document-one-conversation.
  const sessions = [];
  // The library as the server would hold it: rename really changes the row here,
  // because "the list shows the new name" is only checkable if the next list call
  // answers with the new name.
  const documents = [
    { id: "d1", title: "alpha", kind: "markdown", chars: 22, created_at: "2026-09-21" },
    { id: "d2", title: "beta", kind: "text", chars: 5, created_at: "2026-09-21" }];
  const postJSON = async (url, body) => {
    fetches++;
    if (url === "/api/extras") extras.push(body);
    if (url === "/api/session") sessions.push(body);
    if (body.action === "list") return { ok: true, documents };
    if (body.action === "open") {
      opened = body.doc_id;
      const title = body.doc_id === "d2" ? "beta" : "alpha";
      return { ok: true, document: { id: body.doc_id, title, kind: "markdown",
                                     chars: 22, created_at: "2026-09-21" },
               text: "para one\n\npara two", has_more: false,
               file_url: "/api/library/file?id=" + body.doc_id };
    }
    if (body.action === "rename") {
      const doc = documents.find(d => d.id === body.doc_id);
      if (doc) doc.title = body.title;
      return { ok: true, title: body.title };
    }
    return { ok: true };
  };
  // Which document row the library list is lighting up, read out of its markup
  // ("class=rdoc on" is the only place the open document shows up in the list).
  const lit = html => (String(html).match(
    /class="rdoc on"\s+onclick="readerOpen\('([^']+)'\)/) || [])[1] || null;
  // Spying on the router: quoting a sentence must NOT navigate away from the
  // document (that is the bug the selection-send had).
  let navigated = 0;
  function openAgent() { navigated++; }
  eval(readerSrc);

  reset();
  return (async () => {
    // 收起提问 must actually close the panel. The panel is part of the VIEW
    // markup, so it only follows by being rebuilt — and the poll guard
    // (`view === "reader" && !subChanged`) skips exactly that rebuild, so the
    // click used to do nothing at all. Clearing activeView is what asks for it.
    assert(askPanelOpen(), "the ask panel is open by default");
    toggleAskPanel();
    assert(!askPanelOpen(), "收起提问 closes the panel and remembers the choice");
    assert(activeView === null && renderCalls === 1,
           "…by asking render() for a rebuild the poll guard would otherwise skip");
    toggleAskPanel();
    assert(askPanelOpen() && renderCalls === 2, "clicking it again reopens the panel");

    await readerOpen("d1");
    assert(opened === "d1", "opening a document asks the server for it by id");
    assert(els["rc-content"].innerHTML.includes("para one"),
           "the document is painted from its extracted text");
    assert(els["rc-title"].textContent === "alpha", "the pane header names the document");
    assert(storage["zhiyan_reader_open_doc"] === "d1",
           "opening a document remembers its id for the next page load");
    await restoreReaderState();          // settle the one-time library fetch

    // The one that matters: a poll must not touch innerHTML, or a text selection
    // made with the mouse is dropped mid-drag. This is the ORIGINAL bug, and the
    // reason the reader has a stamp at all.
    const painted = paints, fetched = fetches;
    await restoreReaderState();
    await restoreReaderState();
    assert(paints === painted, "a poll repaints NOTHING (a live selection survives)");
    assert(fetches === fetched, "a poll does not re-fetch the document it already has");

    // A rebuilt view is a FRESH element with no stamp -- it MUST repaint, or the
    // document would silently go blank (which is how the last reader regressed).
    // Note the elements are genuinely new: restoring the old ones would keep
    // their stamps and quietly test nothing.
    reset();
    await restoreReaderState();
    assert(paints > 0 && els["rc-content"].innerHTML.includes("para one"),
           "a rebuilt view (fresh element, no stamp) repaints the document");

    // The library's row highlight has to follow the document you open. It did
    // not: the list's stamp was (query | row ids), and opening a DIFFERENT
    // document changes neither, so the early return kept the previous list with
    // the old row still lit — you click a second document and the highlight
    // stays on the first one.
    assert(lit(els["rc-library"].innerHTML) === "d1",
           "the library highlights the document that is open");

    // The open document has to reach the Context Bridge under the READER agent,
    // because that is who the panel asks as — the bridge is keyed by
    // (agent, session) and the server looks it up as (agent.agent_id,
    // agent.session.session_id). Filed only under the active agent, the panel
    // answered "我看不到你打开的是哪篇": it looked under ("reader", ...) and
    // found nothing. The active agent's copy stays, for 选中文字→发送 (rcSend).
    const posted = extras.map(p => p.agent_id + "/" + p.session_id);
    assert(posted.includes("reader/doc-d1"),
           "opening a document publishes it under the READER agent's key for THIS "
           + "document (one document, one conversation): " + posted.join(", "));
    assert(posted.includes("default/s1"),
           "…and still under the active agent's thread (the 选中文字→发送 flow)");
    assert(extras.filter(p => p.action !== "clear")
                 .every(p => p.resource === "alpha" && p.content.includes("para one")),
           "both snapshots carry the document you just opened");

    // The pane and the panel move TOGETHER. You complained that the panel only
    // followed a document switch after collapsing and reopening it — because it
    // was never told about the switch AND was reading the reader agent's single
    // process-wide thread. Both halves are asserted here: which thread it is on,
    // and that the panel was repainted from it.
    assert(sessions.some(b => b.action === "switch" && b.id === "doc-d1"
                              && b.agent_id === "reader"),
           "打开文档 = 把阅读面板切到这份文档自己的会话上: " + JSON.stringify(sessions));
    assert(els["ra-title"].textContent === "alpha",
           "the panel's header names the document you just opened");
    assert(askPaints.length && askPaints[askPaints.length - 1].count === 0,
           "…and the panel is painted from that thread (empty: no questions here yet)");

    // Quoting a sentence asks in the panel on the right, not in the main
    // conversation. The old code prefilled #dmsg and, when that was not on
    // screen, navigated to #agent/<active> to go find it — so 发送给 Agent threw
    // you out of the document you were reading, which is the opposite of what a
    // reader view is for.
    window.getSelection = () => ({ rangeCount: 1, toString: () => "para one" });
    const sent = extras.length;
    await rcSend();
    // ...and drain one macrotask, so a future version that routes from inside
    // the promise chain without returning it is still caught here.
    await new Promise(r => setTimeout(r, 0));
    assert(navigated === 0,
           "quoting a sentence asks in the right-hand panel (it does NOT navigate away)");
    assert(els["amsg"].value.startsWith("[选中文本] para one"),
           "the quote lands in the ask panel's composer, ready for your question");
    assert(extras.length > sent && extras.some(p => p.selection === "para one"),
           "and the selection is published to the bridge (the agent reads it as context)");
    assert(els["rc-selection"].style.display === "none",
           "the 选中文本 box is dismissed once it has been handed over");

    // A different document, and the same document made longer, both repaint:
    // that second one is the 继续加载 button, and a stamp keyed on the id alone
    // would silently swallow it.
    const views = renderCalls;
    await readerOpen("d2");
    assert(els["rc-content"].innerHTML.includes("para one"), "a different document repaints");
    assert(lit(els["rc-library"].innerHTML) === "d2",
           "and the highlight follows to the document you just opened");
    // 换文档就换面板 — without the view being rebuilt. A rebuild is what used to
    // make this work, and it also throws away the text you have selected, which
    // is exactly what the poll guard exists to prevent.
    assert(els["ra-title"].textContent === "beta",
           "switching documents switches the panel's header right away");
    assert(sessions.some(b => b.action === "switch" && b.id === "doc-d2"),
           "…and moves the panel to the new document's own conversation");
    assert(renderCalls === views, "…without rebuilding the reader view");
    const before = paints;
    readerMore();
    assert(paints > before, "continuing a long document repaints it");

    // Deleting the open document empties the pane -- and clears the stamp, so
    // opening the SAME id again later still paints (a stale stamp would leave
    // the pane blank for a document that had been re-added).
    await readerDelete("d2");
    assert(els["rc-content"].innerHTML.includes("选一份文档"), "the empty state is shown");
    assert(els["rc-content"].dataset.stamp === "", "the empty state clears the stamp");
    assert(!("zhiyan_reader_open_doc" in storage),
           "deleting the open document forgets it (a reload must not chase a dead id)");
    // …and takes it OFF THE BRIDGE. The snapshot used to be write-only, so an
    // agent went on "seeing" a document you had closed or deleted hours ago.
    // Both keys, because the document was filed under both.
    const clears = extras.filter(p => p.action === "clear")
                         .map(p => p.agent_id + "/" + p.session_id);
    assert(clears.includes("reader/doc-d2"),
           "deleting the open document clears it from the panel's key: " + clears.join(", "));
    assert(clears.includes("default/s1"),
           "…and from the active agent's key (that copy is the document too)");

    // A full page refresh: brand-new module state, same localStorage. The pane
    // has to come back to the document being read rather than the empty state --
    // which is the whole point of the id in localStorage, and the reason the
    // restore is allowed to call readerOpen() from a poll's code path.
    await readerOpen("d1");
    eval(readerSrc);                    // reload: fresh currentDoc/libraryDocs
    reset();                            // ...and fresh elements, as a reload gives
    await restoreReaderState();
    assert(opened === "d1", "a reload re-opens the document you were reading");
    assert(els["rc-content"].innerHTML.includes("para one"),
           "and paints it, instead of coming up empty");

    // 重命名 changes the name and NOTHING else — no new id, no new file, no
    // re-parse. And the bridge is republished, because the snapshot carries the
    // NAME: without that the agent would go on calling the document by its old
    // name for as long as you kept it open.
    renameTo = "阿尔法";
    await readerRename();
    assert(els["rc-title"].textContent === "阿尔法", "重命名后页头就是新名字");
    assert(els["ra-title"].textContent === "阿尔法", "面板标题一起换（同一个绘制路径）");
    // …and so does the row in the LIBRARY. Its stamp was (query | open id | row
    // ids) and a rename changes none of those, so the list kept the old name
    // until some unrelated click happened to move the highlight. The name is part
    // of what the markup says, so it has to be part of the stamp.
    assert(els["rc-library"].innerHTML.includes("阿尔法"),
           "重命名后文档列表那一行也是新名字（stamp 要带上名字，只认 id 会留着旧的）");
    assert(extras.filter(p => p.action !== "clear").slice(-2)
                 .every(p => p.resource === "阿尔法"),
           "…并且把快照重发一遍，桥里的 resource 就是文档名");

    // 关闭文档: the affordance that did not exist at all, so the only way to take
    // a document off the bridge was to restart the server.
    const clearsBefore = extras.filter(p => p.action === "clear").length;
    await readerClose();
    assert(extras.filter(p => p.action === "clear").length === clearsBefore + 2,
           "关闭文档把两个键都从桥上撤掉（和 publish 用同一份目标清单）");
    assert(els["rc-content"].innerHTML.includes("选一份文档"), "关掉就回到空态");
    assert(!("zhiyan_reader_open_doc" in storage), "…并且不再记住它");
    assert(els["ra-title"].textContent === "还没有打开文档", "面板也说没有打开文档了");
  })();
})());

// ---- the PDF pane: a WINDOW of pages, not every page ------------------------
// pdf.js itself is stubbed (getDocument/TextLayer/canvas). What is under test is
// the thing that made a 900-page PDF unusable: which pages exist, which of them
// have a canvas, and whether the ones without one still take up the right space.
//
// The DOM stub therefore has to be a small LAYOUT engine, not a pile of empty
// objects. The reader reads rectangles back — where is the page under the
// pointer, how far down the document has the viewport got — so a stub that
// answered 0 for every rect would let the windowing arithmetic be wrong and
// still pass.
pendingTests.push((function testPdfWindow() {
  const els = {};
  let prevented = 0;

  const innerHeight = el => parseFloat(el.style.height) || el.clientHeight || 800;
  const outerHeight = el => innerHeight(el) + (parseFloat(el.style.marginBottom) || 0);
  const matchesSel = (el, sel) => (sel.startsWith(".")
    ? String(el.className).split(/\s+/).includes(sel.slice(1))
    : el.tag === sel);
  const descendants = (root, sel) => {
    const found = [];
    (function walk(node) {
      for (const child of node.children) {
        if (matchesSel(child, sel)) found.push(child);
        walk(child);
      }
    })(root);
    return found;
  };

  function stubEl(id, tag) {
    const classes = new Set();
    return {
      id, tag: tag || "div", dataset: {}, style: {}, children: [], value: "",
      textContent: "", clientWidth: 800, clientHeight: 800, offsetHeight: 0,
      scrollTop: 0, width: 0, height: 0,
      listeners: {}, _html: "", parentElement: null,
      classList: {
        add: c => classes.add(c), remove: c => classes.delete(c),
        contains: c => classes.has(c),
        toggle: (c, on) => (on === undefined
          ? (classes.has(c) ? classes.delete(c) : classes.add(c))
          : (on ? classes.add(c) : classes.delete(c))),
      },
      addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
      querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
      querySelectorAll(sel) { return descendants(this, sel); },
      appendChild(node) { node.parentElement = this; this.children.push(node); return node; },
      append(...nodes) { for (const n of nodes) this.appendChild(n); },
      remove() {
        if (this.parentElement)
          this.parentElement.children = this.parentElement.children.filter(c => c !== this);
        this.parentElement = null;
      },
      focus() { document.activeElement = this; },
      insertAdjacentHTML() {},
      getContext: () => ({}),
      // The fake layout: an element's top is the sum of its previous siblings'
      // outer heights (the reader stacks its pages in one column), and viewport
      // coordinates subtract however far the scroll container has moved.
      // Computed on every call, so it stays right while the reader rewrites page
      // heights under it.
      getBoundingClientRect() {
        let top = 0;
        const parent = this.parentElement;
        if (parent)
          for (const sib of parent.children) { if (sib === this) break; top += outerHeight(sib); }
        let scroll = 0;
        for (let node = parent; node; node = node.parentElement)
          if (node.__scroller) { scroll = node.scrollTop; break; }
        const height = innerHeight(this);
        return { top: top - scroll, height, bottom: top - scroll + height };
      },
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = v; this.children = []; },
    };
  }

  for (const id of ["rc-content", "rc-title", "rc-meta", "rc-selection", "rc-text",
                    "rc-library", "rc-search", "rc-file-input", "rc-url-input",
                    "rc-pagebar", "rc-pageinfo", "rc-pagebox", "rc-pdfind",
                    "rc-pdfindmeta", "rc-pdfhits", "ra-title", "amsg"])
    els[id] = stubEl(id);
  // Two sticky things sit above the pages and the reader measures both: the
  // app's own header (.pagehead), which outranks the page bar on z-index and so
  // forces the bar to stick *below* it, and the page bar itself. Jumping tucks
  // the target page under both, and "which page am I on" has to measure from
  // that same line. Stubs with height 0 make both numbers come out right for the
  // wrong reason — real Chrome reported 第 349 right after jumping to 350, and
  // had the page bar completely hidden behind the header, while this harness was
  // green. So both get a height, and PAGEBAR_LEAD is what they add up to.
  const PAGEHEAD_H = 90, PAGEBAR_H = 40, PAGEBAR_GAP = 8;
  const PAGEBAR_LEAD = PAGEHEAD_H + PAGEBAR_H + PAGEBAR_GAP;
  els["rc-pagebar"].offsetHeight = PAGEBAR_H;
  const pagehead = stubEl("pagehead", "header");
  pagehead.offsetHeight = PAGEHEAD_H;
  // <main>: the element that actually scrolls (style.css gives it
  // `height:100vh;overflow-y:auto`), with #rc-content as its only child so the
  // content's top is the scroll origin. scrollParent() finds it the way it finds
  // the real one — by asking for its computed overflow.
  const scroller = stubEl("main");
  scroller.__scroller = true;
  els["rc-content"].parentElement = scroller;
  // Counts writes to the pane's innerHTML. "A poll repaints nothing" is exactly
  // the kind of claim that is invisible otherwise: a repaint that produces the
  // same markup still throws away a live text selection.
  let paintCount = 0;
  {
    const el = els["rc-content"];
    let html = el.innerHTML;
    Object.defineProperty(el, "innerHTML",
      { get: () => html, set: v => { html = v; paintCount++; } });
  }

  const document = {
    activeElement: null,
    getElementById: id => els[id] || null,
    querySelector: sel => (sel === "header.pagehead" ? pagehead : null),
    createElement: tag => stubEl("", tag),
    documentElement: { scrollTop: 0 },
  };
  const getComputedStyle = node => ({ overflowY: node && node.__scroller ? "auto" : "visible" });
  const window = { devicePixelRatio: 1, getSelection: () => ({ toString: () => "" }),
                   addEventListener() {} };
  const storage = {};
  const localStorage = { getItem: k => (k in storage ? storage[k] : null),
                         setItem: (k, v) => { storage[k] = String(v); },
                         removeItem: k => { delete storage[k]; } };
  const esc = s => String(s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // renderMarkdown deliberately NOT stubbed here: it comes from the real util.js
  // eval'd at the top. It used to be `t => String(t)` in this scope, and that
  // identity is precisely why the code-block assertion below used to be fed a
  // shape no renderer ever produces.
  const ACTIVE_AGENT = "default", SESSION = "s1", activeView = "reader";
  const VIEWS = {};                    // reader.js assigns VIEWS.reader at load
  const alert = m => { throw new Error("unexpected alert: " + m); };
  const confirm = () => true;
  const D = { current_sessions: {} };
  // What the semantic panel's requests look like on the wire — recorded so the
  // assertions can check that the QUESTION and the document id were sent, not
  // just that something got painted.
  const ragCalls = [];
  const postJSON = async (url, body) => {
    if (url === "/api/rag") {
      ragCalls.push(body);
      if (body.action === "status") return { ok: true, info: INDEX_INFO };
      return { ok: true, answer: "答案 [第 9 页]", error: "", hits: [
        { label: "第 9 页", snippet: "the needle is here", page: 9, page_end: 9,
          score: 0.8, doc_id: "p1", title: "paper" },
        { label: "第 3 页", snippet: "something else", page: 3, page_end: 3,
          score: 0.4, doc_id: "p1", title: "paper" }] };
    }
    if (body.action === "list") return { ok: true, documents: [
      { id: "p1", title: "paper", kind: "pdf", chars: 10, created_at: "2026-09-21" }] };
    if (body.action === "open") return { ok: true, text: "one two", has_more: false,
      document: { id: body.doc_id, title: "paper", kind: "pdf", chars: 10,
                  created_at: "2026-09-21", index: INDEX_INFO },
      file_url: "/api/library/file?id=" + body.doc_id };
    return { ok: true };
  };
  // Mutable, because the panel's header is supposed to follow the server's
  // answer rather than whatever it guessed when the view was built.
  const INDEX_INFO = { enabled: true, model: "local_ollama:bge-m3", state: "ready",
                       done: 12, total: 12, error: "", stale: false };
  // The observer the browser would give us, minus the browser: it records what
  // it was asked to watch and can be fired by hand, which is how the tests say
  // "the user scrolled the first two pages into view".
  const observers = [];
  class FakeIntersectionObserver {
    constructor(callback, options) {
      this.callback = callback; this.options = options;
      this.targets = []; this.disconnected = false;
      observers.push(this);
    }
    observe(el) { this.targets.push(el); }
    unobserve(el) { this.targets = this.targets.filter(t => t !== el); }
    disconnect() { this.disconnected = true; this.targets = []; }
    fire() { this.callback([], this); }
  }
  const IntersectionObserver = FakeIntersectionObserver;
  // Drawing is promise-only once pdf.js is stubbed, so ONE macrotask boundary
  // drains it; only the zoom's debounce needs real time.
  const settle = ms => new Promise(r => setTimeout(r, ms === undefined ? 0 : ms));
  eval(readerSrc);

  // What pdf.js would provide: a document, a page, and a text layer. renderCount
  // is the measurement — "how many pages did this actually draw" is the whole
  // claim virtualisation makes.
  let renderCount = 0, docFetches = 0;
  const fakePage = n => ({
    getViewport: ({ scale }) => ({ width: 600 * scale, height: 800 * scale }),
    render: () => { renderCount++; return { promise: Promise.resolve(), cancel() {} }; },
    getTextContent: () => Promise.resolve({ items: [
      { str: `page ${n} text`, hasEOL: true },
      ...(n === 9 ? [{ str: " the needle is here", hasEOL: true }] : []),
    ] }),
  });
  class FakeTextLayer { constructor() {} render() { return Promise.resolve(); } }
  loadPdfLib = async () => ({
    getDocument: () => {
      docFetches++;
      return { promise: Promise.resolve({
        numPages: 12, getPage: async n => fakePage(n) }) };
    },
    TextLayer: FakeTextLayer,
    GlobalWorkerOptions: {},
  });
  pdfUnsupportedReason = () => "";        // node's Uint8Array is not the contract here

  const content = () => els["rc-content"];
  const pageEls = () => content().children;
  // The page's own top, in the same coordinates pdfLayout() uses — recomputed
  // from the boxes on screen, so it is an independent check on the arithmetic
  // rather than a copy of it.
  const pageTop = i => {
    let top = 0;
    for (let k = 0; k < i; k++) top += outerHeight(pageEls()[k]);
    return top;
  };
  const hasCanvas = i => pageEls()[i].querySelectorAll("canvas").length > 0;
  const pageWidth = () => parseFloat(pageEls()[0].style.width);
  const wheel = ev => content().listeners.wheel[0]({
    preventDefault: () => { prevented++; }, clientY: 0, ...ev });
  // Bring a page's top to the top of the viewport and let the reader react, the
  // way a real scroll would.
  const scrollTo = async top => {
    scroller.scrollTop = top;
    (scroller.listeners.scroll || []).forEach(fn => fn());
    observers[observers.length - 1].fire();
    await settle();
  };

  return (async () => {
    await readerOpen("p1");
    await settle();

    assert(pageEls().length === 12,
           "every page gets a placeholder at open — 12 of them, not a first batch");
    assert(pageTop(11) > 10000,
           "…and the placeholders have real heights, so the scrollbar is honest: "
           + Math.round(pageTop(11)));
    assert(pageEls().every(el => !el.querySelectorAll("canvas").length),
           "nothing is drawn until the window says which pages are on screen");
    assert(els["rc-pageinfo"].textContent === "第 1 / 12 页",
           "the page bar knows the document's length");
    assert(els["rc-pagebar"].style.display !== "none",
           "the page bar is shown for a PDF");
    assert(els["rc-pagebar"].style.top === PAGEHEAD_H + "px",
           "…and it sticks BELOW the app header, which is sticky over the same "
           + "scrollport and paints on top: pinned at top:0 every button is there "
           + "and none of them is reachable. Got: " + els["rc-pagebar"].style.top);

    observers[0].fire();
    await settle();
    assert(hasCanvas(0) && hasCanvas(1) && !hasCanvas(2),
           "the pages near the viewport are drawn, and the ones past the window are not");

    assert(renderCount <= 4,
           `the first screen draws a handful of pages, not 12: ${renderCount}`);
    assert(pageEls()[0].querySelectorAll(".textLayer").length === 1,
           "a drawn page carries its text layer (that is what makes it selectable)");

    // Scrolling far down: the pages you left give their canvases back, and
    // nothing moves while that happens — the placeholder kept the space.
    const heights = pageEls().map(el => el.style.height);
    await scrollTo(pageTop(6) + 10);
    assert(!hasCanvas(0), "a page that leaves the window gives its canvas back");
    assert(hasCanvas(6) && hasCanvas(7), "…and the pages that arrived are drawn");
    assert(pageEls().map(el => el.style.height).join() === heights.join(),
           "releasing a page does NOT change any height (so the page you are "
           + "reading cannot jump)");
    // Scrolling back releases page 6 — and releasing must not touch the scroll
    // position either, or the page you came back to would slide away.
    await scrollTo(pageTop(1));
    assert(scroller.scrollTop === pageTop(1),
           `releasing pages does not move the scroll position: `
           + `${scroller.scrollTop} vs ${pageTop(1)}`);

    // 跳到第 N 页 is arithmetic on the placeholders: no fetch, no render of the
    // pages in between.
    const beforeJump = renderCount;
    pdfJumpTo(9);
    await settle();
    assert(scroller.scrollTop === pageTop(9) - PAGEBAR_LEAD,
           `跳转 scrolls the placeholder under the sticky bar: `
           + `${scroller.scrollTop} vs ${pageTop(9) - PAGEBAR_LEAD}`);
    assert(els["rc-pageinfo"].textContent === "第 10 / 12 页",
           "…and the page bar follows. It has to measure from under that same bar, "
           + "or it reports the page you just left: " + els["rc-pageinfo"].textContent);
    assert(hasCanvas(9), "the page you jumped to is drawn");
    assert(renderCount - beforeJump <= 4,
           "…without drawing the six pages it scrolled past");

    // 本文内搜索 reads every page with pdf.js, which is what replaces the
    // browser's Ctrl+F now that most pages are not in the DOM.
    els["rc-pdfind"].value = "needle";
    await pdfFindInput();
    assert(/第 9 页/.test(els["rc-pdfhits"].innerHTML),
           "本文内搜索 finds a word on a page that was never rendered: "
           + els["rc-pdfhits"].innerHTML);
    assert(/needle/.test(els["rc-pdfhits"].innerHTML), "…and shows it in context");
    assert(els["rc-pdfindmeta"].textContent.includes("1 处"),
           "…and says how many hits: " + els["rc-pdfindmeta"].textContent);
    els["rc-pdfind"].value = "not-in-the-document";
    await pdfFindInput();
    assert(els["rc-pdfhits"].innerHTML === "" && /没有找到/.test(els["rc-pdfindmeta"].textContent),
           "a miss says so instead of showing an empty list");

    // Zoom. Two claims: the listener is attached once, and a zoom re-renders the
    // pages on screen rather than every page the document has.
    wirePdfZoom(content());
    wirePdfZoom(content());
    assert(content().listeners.wheel.length === 1,
           "the wheel listener is attached once, not once per repaint");

    const width = pageWidth();
    const beforeZoom = renderCount;
    wheel({ ctrlKey: true, deltaY: -1 });
    await settle(260);                     // the redraw is debounced; let it run
    assert(prevented === 1,
           "ctrl+wheel does NOT zoom the browser page (preventDefault is called)");
    assert(pageWidth() > width, "ctrl+wheel zooms the PDF itself");
    assert(pageEls().length === 12, "zooming does not add or drop placeholders");
    assert(renderCount - beforeZoom <= 4,
           `zooming redraws the pages on screen, not all of them: ${renderCount - beforeZoom}`);

    wheel({ ctrlKey: false, deltaY: -1 });
    assert(prevented === 1, "a plain wheel is still the browser's own scrolling");

    for (let i = 0; i < 20; i++) wheel({ ctrlKey: true, deltaY: -1 });
    await settle(260);
    const capped = pageWidth();
    wheel({ ctrlKey: true, deltaY: -1 });
    await settle(260);
    assert(pageWidth() === capped, "the zoom stops at its maximum instead of growing forever");
    assert(capped > width, "...and it did grow before it stopped");

    // A repaint of the SAME document (which is what opening or closing the ask
    // panel does — it rebuilds the whole view) must not re-download the file, and
    // must let go of the old observer rather than leaking it.
    const paints = observers.length;
    await renderReaderPdf(content());
    await settle();
    assert(docFetches === 1, "repainting the same document does not re-fetch it");
    assert(observers.length === paints + 1, "…but it does attach a fresh observer");
    assert(observers[paints - 1].disconnected,
           "…and disconnects the one it left behind (no leak)");

    // 出处可点. The retrieval itself is the agent's business now — it decides
    // whether to call the semantic_search tool, and the page numbers come back
    // in the tool result. What this file still owns is the one thing only the
    // page can do: turn 「第 30 页」 in a reply into a way to go and LOOK. A
    // citation nobody can check is a citation taken on faith.
    assert(linkCitations("<div class=\"mdp\">见第 9 页。</div>")
           === "<div class=\"mdp\">见<span class=\"cite\" onclick=\"citeJump(9)\">第 9 页</span>。</div>",
           "回复里的页码要被包成可点的: " + linkCitations("<div>见第 9 页。</div>"));
    assert(linkCitations("<p>第 3–4 页</p>")
           === '<p><span class="cite" onclick="citeJump(3)">第 3–4 页</span></p>',
           "跨页引用也认，点第一个页码: " + linkCitations("<p>第 3–4 页</p>"));
    // A real code block: what renderMarkdown makes for a fenced sample, NOT a
    // hand-written `<div class="mdcode">…</div>`. The hand-written one is what
    // let this through — the real block carries a mdcode-head div, and the
    // non-greedy `[\s\S]*?<\/div>` stopped there, so only the head bar was
    // protected and the page number in the <pre> body got wrapped anyway.
    const fenced = renderMarkdown('```\nprint("第 9 页")\n```');
    assert(fenced.includes("<pre>") && fenced.includes('class="mdcode-head"'),
           "下面喂进去的是 renderMarkdown 真拼出来的代码块: " + fenced);
    assert(linkCitations(fenced) === fenced,
           "代码块里的『第 9 页』是内容不是引用，不能动它: " + linkCitations(fenced));

    // The same reply is also stored on the copy button as data-text="…" (see
    // chatTurnCard in render.js). A page number in THERE used to take the span's
    // own quotes with it: the attribute closed early, and everything after it
    // became the button's TEXT — on a button that only comes out on :hover. So
    // the whole raw reply (asterisks, pipes and all) covered the answer the
    // moment the pointer crossed the message, and vanished when it left.
    const card = '<div class="card">'
      + '<button class="msg-copy" onclick="copyMsg(this)" data-text="见第 8 页。"'
      + ' title="复制回复">复制</button>'
      + '<div class="r">' + renderMarkdown("见第 8 页。") + '</div></div>';
    const linked = linkCitations(card);
    assert(linked.includes('data-text="见第 8 页。"'),
           "存进属性的回复一个字都不能动（否则复制按钮会吞掉整篇正文）: " + linked);
    assert((linked.match(/class="cite"/g) || []).length === 1,
           "…而正文里那一处照样包成可点: " + linked);

    // …and the whole loop: the citation lands where it says.
    const beforeCite = scroller.scrollTop;
    citeJump(9);
    await settle();
    assert(scroller.scrollTop === pageTop(8) - PAGEBAR_LEAD,
           `点引用跳到那一页: ${scroller.scrollTop} vs ${pageTop(8) - PAGEBAR_LEAD}`);
    assert(els["rc-pageinfo"].textContent === "第 9 / 12 页",
           "…页码栏也跟上了: " + els["rc-pageinfo"].textContent);
    assert(scroller.scrollTop !== beforeCite, "…而且确实动了");
    // A document with no pages has nowhere to land, so its hits carry page 0 and
    // clicking one must leave the reader exactly where it was.
    const parked = scroller.scrollTop;
    citeJump(0);
    assert(scroller.scrollTop === parked, "没有页码的引用不会去动滚动位置");


    // The 5s poll reaches this pane through restoreReaderState(), and must not
    // touch any of it.
    const polls = observers.length, shown = paintCount;
    await restoreReaderState();
    await restoreReaderState();
    assert(observers.length === polls && paintCount === shown,
           "a poll does not repaint the PDF, so a text selection survives it");
  })();
})());

// ---- readerLoad(): reads the file off the event target ----------------------
(function testReaderLoad() {
  assert(/function readerLoad\(input\)/.test(readerSrc),
         "readerLoad takes the element the change event fired on");
  assert(/input = input \|\| document\.getElementById\("rc-file-input"\)/.test(readerSrc),
         "readerLoad still falls back to looking the input up by id");
  assert(/onchange="readerLoad\(this\)"/.test(readerSrc),
         "the markup passes `this` into readerLoad");
  // A long PDF has to go up as the file itself. Base64 turns 20 MB into 27 MB of
  // text, and the tab holds it three times over (the string, the JSON body, the
  // decoded bytes) — which is a large part of why long documents never arrived.
  assert(/fetch\(url, \{method: "POST", body: file\}\)/.test(readerSrc),
         "the upload sends the File as the request body, not as a data URL");
  assert(!/readAsDataURL/.test(readerSrc),
         "…so nothing base64-encodes the document on the way up");
  assert(readerSrc.includes("正在解析"),
         "a big upload says what it is doing (extraction is synchronous server-side)");
  // 语义检索不再是页面上的一块面板：模型自己决定要不要调工具去检索。
  // A leftover panel would mean two places deciding what to retrieve.
  assert(!/rag-panel|rag-status/.test(readerSrc),
         "语义检索面板已经拆掉，检索交给 agent 的工具");
})();

// ---- the Reader is defined exactly once, in its own file --------------------
// A second definition in views.js used to shadow-override nothing (main.js
// loads last, so IT won) and silently ate edits made in the wrong file. The
// same trap moved with the code: the reader now lives in reader.js, and a
// stale copy left behind in main.js would win (it loads later).
(function testSingleDefinition() {
  const dir = process.argv[2].replace(/main\.js$/, "");
  const viewsJs = fs.readFileSync(dir + "views.js", "utf8");
  assert(!/^\s*reader\s*\(d\)\s*\{/m.test(viewsJs),
         "views.js does not define a duplicate VIEWS.reader");
  assert((readerSrc.match(/VIEWS\.reader\s*=/g) || []).length === 1,
         "VIEWS.reader is assigned exactly once, in reader.js");
  assert(!/VIEWS\.reader\s*=/.test(src),
         "main.js no longer carries a stale VIEWS.reader");
  // the pages the reader opens with must exist in the shell, or the pane is
  // built against ids nothing renders
  const html = fs.readFileSync(dir + ".." + "/index.html", "utf8");
  for (const id of ["rc-content", "rc-library", "rc-file-input"])
    assert(html.includes('id="' + id + '"') || readerSrc.includes('id="' + id + '"'),
           "the shell or the view markup provides #" + id);
  // The Reader IS an agent, but not a chat you navigate to: it exists only in the
  // reader's own ask panel (ASK_AGENT). Listed beside General/Research it read as a
  // fifth conversation, so the sidebar entry was removed on purpose.
  assert(!/data-agent="reader"/.test(html),
         "the sidebar lists no Reader agent (it belongs to the reader's ask panel)");
  // The machinery that capped a PDF at 60 pages is gone, not merely bypassed: a
  // "继续加载" batch loader left in place next to the page window would be two
  // answers to "what is on screen", and the dead button it ended in could never
  // reach page 61 anyway.
  for (const dead of ["PDF_MAX_PAGES", "PDF_PAGES_PER_BATCH", "function pdfDrawPages(",
                      "function pdfMore(", "function pdfRedraw("])
    assert(!readerSrc.includes(dead), `${dead} is gone`);
})();

// ---- the conversation panel (#agent/<id>) -----------------------------------
// Same class of bug as the Reader's, so it gets the same lock. The panel is
// generated markup that carries the composer and the log, and render() rebuilds
// it on every navigation -- if the 5s poll rebuilt it too, the draft in #dmsg
// and the scroll position would die mid-sentence every five seconds.
(function testAgentPollGuard() {
  const renderFn = src.slice(src.indexOf("function setCount("),
                             src.indexOf("\nlet lastFetch"));
  let activeView = null, activeSub = null, animating = false, editing = false;
  const VIEWS = { agent: (d, sub) => "<agent:" + sub + ">", loop: () => "<loop>" };
  const TITLES = { agent: "对话" };
  const D = { provider: "p", model: "m", chat_log: [], stats: { turns: 0, tool_errors: 0 },
              graph: { stats: { quick: 0, full: 0 } }, facts: [], episodes: [],
              calendar: [], outbox: [], db: { all_tables: [] }, home: "H",
              agents: [{ id: "default" }, { id: "research" }], current_sessions: {} };
  const els = {};
  const document = {
    querySelectorAll: () => [],
    querySelector: () => ({ scrollTop: 0 }),
    getElementById: id => (els[id] = els[id] || { textContent: "", innerHTML: "", style: {} }),
  };
  const window = { addEventListener() {}, getSelection: () => ({ toString: () => "" }) };
  const esc = s => String(s);
  const location = { hash: "#agent/research" };
  let ACTIVE_AGENT = "default";
  const switched = [];
  let wired = 0;
  // The real selectAgent() is async but assigns ACTIVE_AGENT before its first
  // await, and render() relies on exactly that -- so a synchronous stand-in is
  // faithful to the contract being tested.
  function selectAgent(id) { switched.push(id); ACTIVE_AGENT = id; }
  function syncAgentChrome() {}
  function restoreReaderState() {}
  function wireChat() { wired++; }
  eval(renderFn);

  els["view"] = { innerHTML: "", style: {} };
  render();
  assert(switched.length === 1 && switched[0] === "research",
         "a #agent/<id> hash that disagrees with the state switches agents");
  assert(els["view"].innerHTML === "<agent:research>",
         "the conversation is built for the agent named in the hash");
  assert(wired === 1, "the generated panel is bound after it is built");

  render(); render();
  assert(els["view"].innerHTML === "<agent:research>",
         "a poll does NOT rebuild the conversation (the #dmsg draft survives)");
  assert(wired === 3, "a poll still re-binds and repaints the log");
  assert(switched.length === 1, "render() stops switching once hash and state agree");

  location.hash = "#agent/default";
  render();
  assert(els["view"].innerHTML === "<agent:default>", "changing agent rebuilds the panel");
})();

// ---- the panel's element ids are a contract with the rest of the app --------
// #dmsg/#dsend are bound by wireChat(), #teletoggle by applyTele(), #modelchip
// by syncModelChip(), and .chatlog is what body.no-tele hides. All four are
// looked up by id/class from another file, so dropping one breaks a feature
// with no error anywhere.
(function testPanelContract() {
  const chatSrc = fs.readFileSync(
    process.argv[2].replace(/main\.js$/, "chat.js"), "utf8");
  const from = chatSrc.indexOf("function activeAgentData(){");
  const to = chatSrc.indexOf("\n};", chatSrc.indexOf("VIEWS.agent = function(){")) + 3;
  let ACTIVE_AGENT = "research";
  let D = { agents: [{ id: "research", name: "Research", icon: "◎", status: "idle" }],
            current_sessions: {} };
  const VIEWS = {};
  const esc = s => String(s).replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  eval(chatSrc.slice(from, to));
  const html = VIEWS.agent(D, "research");

  for (const [needle, why] of [
    ['class="chatlog"', ".chatlog is what body.no-tele hides for the stats toggle"],
    ['id="dmsg"', "wireChat() binds the composer by id"],
    ['id="dsend"', "wireChat() binds Send by id"],
    ['id="teletoggle"', "applyTele() syncs the stats button by id"],
    ['id="modelchip"', "syncModelChip() syncs the model pill by id"],
  ]) assert(html.includes(needle), why);
  assert(html.includes("Research"), "the header names the active agent");
  assert(!/id="dock/.test(chatSrc), "no dock ids survive in the panel");

  // The one-directional routing that keeps this from recursing: openAgent()
  // writes the hash, selectAgent() only switches state, and render() is the
  // only thing that maps one onto the other.
  const selectFn = chatSrc.match(/async function selectAgent\(agentId\)\{[\s\S]*?\n\}/);
  assert(selectFn && !/location\.hash/.test(selectFn[0]),
         "selectAgent never writes the hash (that is what keeps it from recursing)");
  assert(chatSrc.match(/function openAgent\(agentId\)\{[\s\S]*?\n\}/)[0]
           .includes('"#agent/" + agentId'),
         "openAgent routes to #agent/<id>");
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


def test_reader_survives_the_five_second_refresh(node, tmp_path):
    """The whole lock: render(), the reader pane and readerLoad()."""
    for path in (MAIN_JS, READER_JS, UTIL_JS):
        assert path.exists(), f"missing frontend source: {path}"
    harness = tmp_path / "reader_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")

    proc = subprocess.run([node, str(harness), str(MAIN_JS), str(READER_JS), str(UTIL_JS)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=60)

    # Show the claims either way: a bare "exit 1" tells the user nothing.
    print(proc.stdout)
    assert proc.returncode == 0, f"frontend checks failed:\n{proc.stdout}\n{proc.stderr}"
