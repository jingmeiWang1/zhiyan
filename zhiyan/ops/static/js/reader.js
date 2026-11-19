// zhiyan dashboard — the Reader: your document library and the reading pane.
// Split out of main.js (it was ~200 lines of an unrelated concern sharing a file
// with the render loop) because this app is the one that grows per format.
// Classic <script>, shared global scope (no build step, no modules).
// Load order + rules: static/README.md.
//
// WHAT IS STORED WHERE: every document lives in the LIBRARY on the server —
// the original bytes plus the extracted text (applications/library.py). This
// file holds no documents; it holds the state of the reading session:
//
//   currentDoc    the open document (metadata + text + its file_url)
//   readerShown   how much of a long document has been RENDERED so far
//   pdfSession    the loaded pdf.js document, its zoom, and the window of pages
//                 currently drawn (see "the page window" far below)
//
// All three survive the 5s poll because render() skips rebuilding this view
// (see main.js); restoreReaderState() repaints from them on every tick.

let currentDoc = null;
let libraryDocs = [];
let libraryLoaded = false;
let readerQuery = "";
const READER_DOC_KEY = "zhiyan_reader_open_doc";
// How much of the open document is on screen. A 400 KB document rendered in one
// innerHTML is a multi-second freeze in the tab, so text is painted in chunks
// and the rest is one click away. See renderReaderText().
const READER_CHUNK = 60000;
let readerShown = READER_CHUNK;
// The loaded pdf.js document and the state of the page window on screen.
// Shape and rules: the "PDF rendering" section far below.
let pdfSession = null;
const PDF_ZOOM_MIN = 0.25;      // ctrl+wheel zoom of the page, not of the window
const PDF_ZOOM_MAX = 4;
const PDF_ZOOM_STEP = 1.15;
let pdfPendingAnchor = null;    // where under the pointer the zoom is pinned
let pdfZoomTimer = null;
let pdfBarQueued = false;       // one page-number repaint per animation frame
let pdfScrollWired = false;     // the scroll listener is attached once per page
let pdfFindSeq = 0;             // bumps to cancel a 本文内搜索 still running

VIEWS.reader = function(){
  const askOpen = askPanelOpen();
  return `<section class="reader-shell${askOpen ? " ask-open" : ""}">
    <aside class="reader-library">
      <div class="rl-head">
        <span class="reader-kicker">文档库 · ${esc(ACTIVE_AGENT || "general")}</span>
        <label class="reader-drop"><input type="file" id="rc-file-input"
          accept=".md,.markdown,.txt,.py,.js,.ts,.json,.csv,.html,.htm,.xml,.pdf"
          onchange="readerLoad(this)"><span>+ 添加文件</span></label>
      </div>
      <div class="reader-url">
        <input type="url" id="rc-url-input" placeholder="粘贴 https:// 链接" autocomplete="off">
        <button class="sessbtn" onclick="handleUrlInput()">读取</button>
      </div>
      <input type="search" id="rc-search" class="reader-search" placeholder="搜索文档内容…"
             autocomplete="off" oninput="readerSearch()">
      <div id="rc-library" class="reader-docs"></div>
    </aside>
    <div class="reader-pane">
      <div class="reader-paper-head">
        <span id="rc-title">当前文档</span>
        <span id="rc-doc-acts" style="display:none">
          <button class="sessbtn" onclick="readerRename()"
                  title="只改文档库里的显示名，磁盘上的文件不动">重命名</button>
          <button class="sessbtn" onclick="readerClose()"
                  title="关掉这份文档：Agent 也就不再看得到它">&times;</button>
        </span>
        <span class="meta" id="rc-meta">选中文本后可在右侧提问</span>
        <button class="sessbtn ask-toggle" onclick="toggleAskPanel()"
                title="随时就正在读的内容提问">${askOpen ? "收起提问" : "问 Agent"}</button>
      </div>
      <div id="rc-pagebar" class="pdf-pagebar" style="display:none">
        <button class="sessbtn" onclick="pdfStep(-1)">上一页</button>
        <span class="pdf-pageinfo" id="rc-pageinfo">第 1 / 1 页</span>
        <button class="sessbtn" onclick="pdfStep(1)">下一页</button>
        <input type="number" id="rc-pagebox" class="pdf-pagebox" min="1" value="1"
               onkeydown="pdfPageBoxKey(event)">
        <button class="sessbtn" onclick="pdfJumpFromInput()">跳转</button>
        <input type="search" id="rc-pdfind" class="pdf-find" autocomplete="off"
               placeholder="本文内搜索（整份 PDF）" oninput="pdfFindInput()">
        <span class="pdf-findmeta" id="rc-pdfindmeta"></span>
        <div id="rc-pdfhits" class="pdf-hits"></div>
      </div>
      <div id="rc-content"><p class="empty">从左边选一份文档，或先添加一份。</p></div>
      <div id="rc-selection" class="reader-selection">
        <div><b>选中文本</b> · 已捕获</div><div id="rc-text"></div>
        <div class="rs-actions">
          <button class="save" onclick="rcSend()">在右侧提问</button>
          <button class="sessbtn" onclick="rcClearSelection()">取消</button>
        </div>
      </div>
    </div>
    ${askOpen ? askPanelHTML() : ""}
  </section>`;
};

// --- the ask panel: the Reader agent, next to the document --------------------
//
// Same chat machine as the conversation view (sendChatTo in render.js), pointed
// at its own message list and its own agent. It is NOT a second copy of the
// chat: this file supplies three things — where the messages live (ASK), who
// answers (ASK_AGENT), and how to repaint (.asklog) — and reuses everything
// else, including the timeline cards.
//
// The log element carries `.asklog`, deliberately NOT `.chatlog`: syncChatLogs()
// fans out to every `.chatlog` in the document, so a shared class name would
// paint the MAIN conversation's messages into this panel. The stats toggle is
// therefore wired to `.asklog` as well (style.css).
const ASK_AGENT = "reader";
const ASK = [];
// Which thread the panel currently holds — the ID, not a yes/no. It used to be a
// one-way `askLoaded = true`, so the first thread loaded stayed the panel's
// thread for the life of the page: opening another document changed nothing
// until you collapsed and reopened the panel, which rebuilt the whole view and
// started the state over. Comparing ids is what lets a document's own thread
// arrive without a rebuild.
let askLoadedFor = null;

function askPanelOpen(){
  const saved = localStorage.getItem("zhiyan_ask_open");
  return saved === null ? true : saved === "1";   // on by default: it is the point
}
function toggleAskPanel(){ setAskPanel(!askPanelOpen()); }
function setAskPanel(open){
  localStorage.setItem("zhiyan_ask_open", open ? "1" : "0");
  // The panel is part of the view markup, so it only follows this by being
  // REBUILT -- and a bare render() does not rebuild the Reader: the
  // `view === "reader" && !subChanged` branch skips the rebuild on purpose, so
  // the 5s poll cannot destroy the file input or a live text selection. That
  // guard also swallowed the 收起提问 click, which is why it did nothing.
  // Clearing activeView makes render() see a view change, exactly as a
  // navigation would, so it rebuilds this one time and the poll stays quiet.
  activeView = null;
  render();
}
function askPanelHTML(){
  return `<aside class="reader-ask">
    <div class="ra-head">
      <span class="reader-kicker">提问 · READER AGENT</span>
      <span class="meta" id="ra-title">${esc(currentDoc ? currentDoc.title : "还没有打开文档")}</span>
    </div>
    <div class="asklog"></div>
    <div class="attpreview" id="aatt" hidden></div>
    <div class="chatbar">
      <input type="file" id="afile" accept="image/*" multiple hidden>
      <button id="apick" class="attachbtn" title="发图片（也可以直接把截图粘贴进来）">🖼</button>
      <input id="amsg" placeholder="就这份材料提问…" autocomplete="off">
      <button id="asend">发送</button>
    </div>
  </aside>`;
}

// The panel's header names the document, and it is repainted from the ONE place
// that paints the document — renderReaderDoc(). It used to be baked into the
// markup when the view was built, so it named whatever document happened to be
// open at that instant and nothing ever revised it afterwards.
function paintAskTitle(){
  const el = document.getElementById("ra-title");
  if (el) el.textContent = currentDoc ? currentDoc.title : "还没有打开文档";
}

const ASK_CHAT = {chat: ASK, agentId: () => ASK_AGENT,
                  repaint: force => syncLogClass("asklog", ASK, ASK_EMPTY, force, linkCitations),
                  key: "a", attach: []};
const ASK_EMPTY = "问点什么吧——这个 Agent 看得到你正在读的文档，也能自己查文档库。";

// --- 出处可点：把回复里的「第 N 页」变成能跳过去的东西 -----------------------
//
// The retrieval itself is the AGENT's business now — it decides whether to call
// semantic_search, and the tool result carries the page numbers. What is left
// for this file is the one thing only the page can do: turn a citation the model
// wrote into a way to go and check it. A citation nobody can verify is a
// citation taken on faith, which is the whole reason the index records pages.
function citeJump(page){
  if (!page || !currentDoc || currentDoc.kind !== "pdf" || !pdfSession) return;
  pdfJumpTo(page - 1);
  pdfFlashPage(page - 1);
}

// 「第 30 页」/「第 30-35 页」→ 可点。挂在 syncLogClass 的 transform 上，也就是
// 拼好 html、比较之前，所以 `_shown` 那个"没变就不重画"的守卫看的是变换后的字符串
// ——否则每 5 秒都会白重画一次日志。
const CITE_RE = /第\s*(\d{1,5})(?:\s*[–—-]\s*(\d{1,5}))?\s*页/g;

// 这是拿正则改 HTML 字符串，所以先把"不能碰的地方"挖出来，只在剩下的文字里替换。
// 挖的是这两种，而它们各自都真出过一次事：
//
//   1. **标签本身**。回复是以 `data-text="…"` 存在复制按钮上的，页码正好落在
//      那里面时，注入的 `<span class="cite" onclick="…">` 带着自己的引号，会把
//      属性提前关掉：属性断成两截，剩下的正文被浏览器当成**按钮的文字**。那个
//      按钮是 `.card:hover` 才 display:block 的，于是鼠标一移到消息上，整篇生
//      markdown 就盖住回复，移开又好了 —— 用户报的"显示时好时坏"就是它。
//   2. **`<pre>` 里面**。代码块里的「第 3 页」是内容不是引用。原来这里写的是
//      `<div class="mdcode">`，可真的代码块里还有一个 `<div class="mdcode-head">`
//      （util.js 的 renderMarkdown 拼的），非贪婪的 `[\s\S]*?<\/div>` 在标题栏
//      那儿就收尾了，等于只护住了标题栏、把 `<pre>` 正文漏在外面。写 `<pre>`
//      就与拼法无关了，工具输出的预览（`.tt-short`）也顺便护住 —— 它同样是
//      文档原文的引文，不是模型写的引用。
//
// 第一种靠 `[^>]*` 就够：属性值里的尖括号在写出来之前已经 `esc` 过了，所以一个
// 标签里不会出现裸的 `>`，整条标签（属性在内）一次匹配完。split() 带捕获组会把
// 挖出来的部分放在奇数位，那些正是要原样留着的地方。
const CITE_HOLES = /(<pre[\s\S]*?<\/pre>|<[^>]*>)/g;

function linkCitations(html){
  // ONLY when there is a PDF to land on. In the main conversation nothing is
  // open, and a page number there must stay plain text rather than become a link
  // that silently does nothing.
  if (!currentDoc || currentDoc.kind !== "pdf" || !pdfSession) return html;
  return html.split(CITE_HOLES)
             .map((part, index) => (index % 2 ? part : _citeWrap(part)))
             .join("");
}

function _citeWrap(html){
  return html.replace(CITE_RE, (whole, first) =>
    `<span class="cite" onclick="citeJump(${first})">${whole}</span>`);
}

// Called by wireChat() alongside the main composer. Re-binding on every rebuild
// is required because the panel's markup is generated.
function wireAsk(){
  const b = document.getElementById("asend"), i = document.getElementById("amsg");
  if (!b && !i) return;
  if (b) b.onclick = () => sendChatTo(ASK_CHAT, i);
  if (i) i.onkeydown = e => { if (e.key === "Enter") sendChatTo(ASK_CHAT, i); };
  wireComposer(ASK_CHAT);   // 📎 + paste, same code as the main composer
  if (askLoadedFor !== askSessionId()) loadAskThread();
  syncLogClass("asklog", ASK, ASK_EMPTY, false, linkCitations);
}

// Show the reader agent's current thread. Read-only: it loads the agent's
// existing session without touching the main conversation's SESSION, and the
// next question continues it (sending with agent_id=reader appends server-side).
async function loadAskThread(){
  const sid = askSessionId();
  let r;
  try {
    r = await postJSON("/api/session", {action: "history", id: sid, agent_id: ASK_AGENT});
  } catch (error) {
    return;
  }
  if (!r.ok) return;
  askLoadedFor = sid;
  fillAsk(r.history || []);
}

// Put a thread's rows into the panel. In place — the `.asklog` node is the live
// one and syncLogClass() paints it (see render.js), so nothing here needs the
// view to be rebuilt, which is what would drop a text selection.
function fillAsk(history){
  ASK.length = 0;
  history.map(histItem).forEach(m => ASK.push(m));
  syncLogClass("asklog", ASK, ASK_EMPTY, true, linkCitations);  // 开的线程从末尾开始
}

// Point the panel at the thread the OPEN DOCUMENT owns (see askSessionId).
// 'switch' is what makes the next question belong to this document, and its
// response already carries that thread's rows, so the panel is painted from it
// rather than fetching the same history a second time. Wrapped in try/catch on
// purpose: switching is the one call that BUILDS the reader agent, and building
// it needs a provider key — a machine without one must still open a document.
async function openAskThread(){
  const sid = askSessionId();
  if (!currentDoc){ askLoadedFor = null; await loadAskThread(); return; }
  try {
    const r = await postJSON("/api/session",
                             {action: "switch", id: sid, agent_id: ASK_AGENT});
    if (r && r.ok){ askLoadedFor = sid; fillAsk(r.history || []); }
  } catch (error) {
    /* keep reading; the panel just will not have this document's thread yet */
  }
}

// --- the library list --------------------------------------------------------

// Fetched once per page load, and again after anything that changes it (add,
// delete) — NOT on the 5s poll, because the poll does not rebuild this view and
// the list is not something the server pushes.
async function loadLibrary(force){
  if (libraryLoaded && !force) return libraryDocs;
  try {
    const res = await postJSON("/api/library", {action: "list", agent_id: ACTIVE_AGENT});
    libraryDocs = (res && res.documents) || [];
    libraryLoaded = true;
    renderLibrary();
    return libraryDocs;
  } catch (error) {
    // Keep the retry visible instead of leaving an empty list that looks like
    // a successful, brand-new library.  `loadLibrary(true)` is safe to call
    // again after the user restarts the Dashboard or fixes its port.
    const el = document.getElementById("rc-library");
    if (el) {
      const message = esc(error.message || error);
      el.innerHTML = `<div class="empty">文档库加载失败：${message}<br>
        <button class="sessbtn" onclick="loadLibrary(true)">重试</button></div>`;
    }
    return [];
  }
}

// Paint the list (or the search results, which have the same row shape plus a
// snippet). Stamp-guarded like the document pane: this runs on the poll, and
// rewriting it would drop the highlight as you arrow through results.
function renderLibrary(){
  const el = document.getElementById("rc-library");
  if (!el) return;
  const rows = readerQuery ? librarySearchRows : libraryDocs;
  // The stamp must cover EVERYTHING the markup below depends on, or the
  // early return keeps a stale list. It was (query | row ids), and the row ids
  // do not change when you open a DIFFERENT document -- so the `.on` highlight
  // stayed on the document you first opened and never followed your clicks.
  // The open document's id is part of the markup, so it is part of the stamp.
  // The NAME is in there too, for the same reason: 重命名 changes what the row
  // says and changes no id at all, so an id-only stamp would leave the old name
  // in the list until something else happened to move the highlight.
  // …and the index state is part of it too, for exactly the same reason as the
  // name: it changes without changing any id, so a stamp that ignored it would
  // leave a finished document reading 「索引 12/54」 forever.
  const stamp = `${readerQuery}|${currentDoc ? currentDoc.id : ""}|`
                + rows.map(d => `${d.id}:${d.title}:${indexBadgeText(d)}`).join(",");
  // BEFORE the early return, or it would never re-arm: the whole point is that
  // the list has NOT changed yet and the row's badge is a number the server is
  // moving along. (This is the poll the deleted semantic panel used to provide.)
  scheduleLibraryPoll(rows);
  if (el.dataset.stamp === stamp) return;
  el.dataset.stamp = stamp;
  if (!rows.length){
    el.innerHTML = `<div class="empty">${readerQuery ? "没有匹配的文档" : "文档库还是空的"}</div>`;
    return;
  }
  el.innerHTML = rows.map(d => `<div class="rdoc ${currentDoc && currentDoc.id === d.id ? "on" : ""}"
      onclick="readerOpen('${esc(d.id)}')">
    <span class="rdoc-kind">${esc(kindBadge(d.kind))}</span>
    <span class="rdoc-title">${esc(d.title)}</span>
    <span class="rdoc-meta">${esc(documentMeta(d))}</span>
    ${indexBadgeText(d) ? `<span class="rdoc-index ${d.index.state === "error" ? "bad" : ""}"
      ${d.index.state === "error" ? `title="点一下重新建立索引" onclick="event.stopPropagation();readerReindex('${esc(d.id)}')"` : ""}
      >${esc(indexBadgeText(d))}</span>` : ""}
    ${d.snippet ? `<span class="rdoc-snippet">${esc(d.snippet)}</span>` : ""}
    <button class="rdoc-del" title="从文档库删除"
            onclick="event.stopPropagation();readerDelete('${esc(d.id)}')">&times;</button>
  </div>`).join("");
}

// Re-fetch the library while something in it is still being indexed, and STOP as
// soon as nothing is. 「索引 137/400」 is a number the server is moving, so the list
// has to ask again; 「已索引」 is a fact, so asking again would be a request every
// two seconds for ever. Self-limiting by construction: the timer is only re-armed
// by a render that still sees work in progress.
let libraryPollTimer = null;

function scheduleLibraryPoll(rows){
  const working = rows.some(d => {
    const state = (d.index || {}).state;
    return state === "queued" || state === "running";
  });
  if (!working){
    if (libraryPollTimer){ clearTimeout(libraryPollTimer); libraryPollTimer = null; }
    return;
  }
  if (libraryPollTimer) return;          // one timer, never a stack of them
  libraryPollTimer = setTimeout(() => {
    libraryPollTimer = null;
    loadLibrary(true);
  }, 2000);
}

// The row badge speaks only when there is something to say. A badge on every
// row ("已索引" eleven times) is a wall, and a document that is simply indexed
// is the normal case, not news. Progress and failure are news.
const indexBadgeText = doc => {
  const info = doc.index || {};
  if (info.state === "running" || info.state === "queued")
    return `索引 ${info.done}/${info.total}`;
  if (info.state === "error") return "索引失败";
  if (info.stale) return "索引待重建";
  return "";
};

const kindBadge = kind => ({pdf: "PDF", markdown: "MD", html: "HTML", csv: "CSV",
  json: "JSON", code: "COD", text: "TXT"}[kind] || "TXT");

function documentMeta(d){
  // 0 chars is not "0 字" — it is a scan, and it is worth saying so in the list
  // rather than letting it look like an empty file until you open it.
  const size = d.chars ? (d.chars >= 1000 ? `${Math.round(d.chars / 1000)}k 字` : `${d.chars} 字`)
                       : "无文字层";
  return `${size} · ${(d.created_at || "").slice(0, 10)}`;
}

let librarySearchRows = [];
let searchTimer = null;
function readerSearch(){
  const input = document.getElementById("rc-search");
  readerQuery = (input && input.value || "").trim();
  clearTimeout(searchTimer);
  // Debounced: search is a round trip, and this fires on every keystroke.
  searchTimer = setTimeout(async () => {
    if (!readerQuery){ librarySearchRows = []; renderLibrary(); return; }
    try {
      const res = await postJSON("/api/library", {action: "search", query: readerQuery});
      librarySearchRows = (res && res.results) || [];
      renderLibrary();
    } catch (error) {
      const el = document.getElementById("rc-library");
      if (el) el.innerHTML = `<div class="empty">搜索失败：${esc(error.message || error)}</div>`;
    }
  }, 220);
}

// --- adding documents --------------------------------------------------------

// `input` is the element the change event fired on (the markup passes `this`).
// Reading the file off the event target instead of looking the input up by id
// keeps this correct even if the view is rebuilt underneath us: the element
// that actually received the pick still holds it. The id lookup remains as a
// fallback for any caller that does not pass one.
async function readerLoad(input){
  input = input || document.getElementById("rc-file-input");
  if (!input || !input.files || !input.files[0]) { alert("请选择一个文件"); return; }
  const file = input.files[0];
  // The file goes up AS THE REQUEST BODY, not as a data URL inside JSON. Base64
  // turns a 20 MB PDF into 27 MB of text, and the tab then holds that text three
  // times over (the JS string, the JSON body, the decoded bytes) — which is a
  // large part of why long PDFs never made it in. `body: file` hands the browser
  // the File object it already has and lets it stream.
  const list = document.getElementById("rc-library");
  // Pulling the text out of a 900-page PDF happens on the server's request
  // thread and takes tens of seconds. Say so BEFORE waiting: an unchanged pane is
  // indistinguishable from a hung one. (Real background extraction would mean
  // storing first and parsing later, with a status to poll — not this round.)
  if (list) list.innerHTML =
    `<div class="empty">正在解析《${esc(file.name)}》…（大文件可能要几十秒）</div>`;
  try {
    const url = "/api/library/upload?name=" + encodeURIComponent(file.name)
                + "&agent_id=" + encodeURIComponent(ACTIVE_AGENT);
    const response = await fetch(url, {method: "POST", body: file});
    const payload = await response.json();
    if (!payload.ok) throw new Error(payload.error || `上传失败（HTTP ${response.status}）`);
    input.value = "";                       // let the same file be picked again
    await loadLibrary(true);                // the new document is in the list now
    await readerOpen(payload.document.id);
  } catch (error) {
    // The "正在解析…" placeholder is still in the list, and it says the upload is
    // happening. Leaving it there would be a lie, so the failure replaces it.
    if (list) list.innerHTML = `<div class="empty">文件加载失败：${esc(error.message || error)}</div>`;
    alert("文件加载失败：" + (error.message || error));
  }
}

async function handleUrlInput(){
  const input = document.getElementById("rc-url-input");
  const url = input && input.value.trim();
  if (!url) return;
  try {
    const response = await postJSON("/api/library", {action: "url", url, agent_id: ACTIVE_AGENT});
    if (!response.ok) throw new Error(response.error || "URL 解析失败");
    input.value = "";
    await loadLibrary(true);
    await readerOpen(response.document.id);
  } catch (error) {
    alert("URL 加载失败（目标可能不允许服务端读取，或不是可解析的文档）：" + error.message);
  }
}

// Ask for a document's index to be built again. The only way back from
// 「索引失败」: the worker deliberately never retries a failed document (a broken
// endpoint hammered once a second is worse than waiting to be asked), so without
// this the document stays unsearchable and nothing on screen can change it.
async function readerReindex(docId){
  try {
    await postJSON("/api/library", {action: "reindex", doc_id: docId,
                                    agent_id: ACTIVE_AGENT});
  } catch (error) {
    alert("重新索引失败：" + (error.message || error));
    return;
  }
  await loadLibrary(true);
}

async function readerDelete(docId){
  if (!confirm("从文档库删除这份文档？原文文件、以及这份文档自己的对话都会一起删除，不可恢复。"))
    return;
  const wasOpen = !!(currentDoc && currentDoc.id === docId);
  try {
    await postJSON("/api/library", {action: "delete", doc_id: docId});
  } catch (error) {
    alert("删除失败：" + (error.message || error));
    return;
  }
  // Deleting the document you are reading closes it, and closing it is what
  // takes its snapshot off the bridge. Deleting some OTHER document is not a
  // reason to touch the key this page filed for the one on screen.
  if (wasOpen) await readerClose();
  await loadLibrary(true);
  renderLibrary();
  renderReaderDoc();
}

// --- opening and painting a document ----------------------------------------

async function readerOpen(docId){
  let res;
  try {
    res = await postJSON("/api/library", {action: "open", doc_id: docId});
  } catch (error) {
    alert("文档打不开：" + (error.message || error));
    return;
  }
  if (!res.ok) { alert("文档打不开：" + (res.error || "未知错误")); return; }
  currentDoc = {...res.document, text: res.text, has_more: res.has_more,
                file_url: res.file_url};
  localStorage.setItem(READER_DOC_KEY, docId);
  readerShown = READER_CHUNK;             // a new document starts at the top
  pdfTeardown();                          // and any previous PDF is gone
  await publishReaderContext({selection: ""});
  renderLibrary();
  renderReaderDoc();
  // Paint first, then move the panel: the question you ask next belongs to THIS
  // document, and the panel has to say so before anything else can go wrong.
  await openAskThread();
}

// Close the document: forget it on screen AND take it off the bridge.
//
// The bridge used to be write-only — publishReaderContext() filed a snapshot
// under two keys and nothing ever removed it, so an agent went on "seeing" a
// document you had closed hours ago. The server has had the action all along
// (extras_action's "clear"); this is the caller it never had.
async function readerClose(){
  await clearReaderContext();             // before currentDoc is gone: it needs the key
  currentDoc = null;
  localStorage.removeItem(READER_DOC_KEY);
  readerShown = READER_CHUNK;
  pdfTeardown();
  askLoadedFor = null;
  renderLibrary();
  renderReaderDoc();
  // …and leave the document's own thread. Keeping it would mean a question asked
  // with no document open continues the conversation of the document you just
  // closed — the panel would go on answering about a document that is gone.
  try {
    const r = await postJSON("/api/session", {action: "new", agent_id: ASK_AGENT});
    if (r && r.session_id){
      D.current_sessions = {...(D.current_sessions || {}), [ASK_AGENT]: r.session_id};
      askLoadedFor = null;
      fillAsk([]);
    }
  } catch (error) {
    /* the panel just keeps its thread; nothing about reading depends on this */
  }
  paintAskTitle();
}

// Rename the document in the LIBRARY only (documents.title). The stored file,
// its id and its content are untouched — this is the name you read, not the name
// on disk.
async function readerRename(){
  if (!currentDoc) return;
  const asked = prompt("文档显示名（只改库里的名字，磁盘上的文件不动）", currentDoc.title);
  if (asked === null) return;
  let r;
  try {
    r = await postJSON("/api/library",
                       {action: "rename", doc_id: currentDoc.id, title: asked});
  } catch (error) {
    alert("重命名失败：" + (error.message || error));
    return;
  }
  if (!r || !r.ok){ alert("重命名失败：" + ((r && r.error) || "未知错误")); return; }
  currentDoc.title = r.title;             // the name the server actually stored
  await loadLibrary(true);                // the list shows the new name too
  renderLibrary();
  renderReaderDoc();
  // The bridge carries the NAME as well as the text (metadata.resource), so a
  // rename that skipped this would leave the agent calling your document by its
  // old name for as long as it stayed open — the two-places-one-fact bug again.
  await publishReaderContext({selection: ""});
}

// Repaint the document pane from currentDoc. Called on open, on "load more",
// and on every poll via restoreReaderState().
function renderReaderDoc(){
  const contentEl = document.getElementById("rc-content");
  const titleEl = document.getElementById("rc-title");
  const metaEl = document.getElementById("rc-meta");
  if (!contentEl) return;
  if (titleEl) titleEl.textContent = currentDoc ? currentDoc.title : "当前文档";
  if (metaEl && currentDoc)
    metaEl.textContent = `${kindBadge(currentDoc.kind)} · ${currentDoc.chars} 字 · 选中文本后可发送给 Agent`;
  // 重命名 / 关闭 only mean something with a document open, and the ask panel's
  // header names the same document. Both follow this one paint: it runs on open,
  // on delete, on 继续加载, on the poll's restore, and after a rename — so there is
  // no second place for the panel's title to go stale.
  const acts = document.getElementById("rc-doc-acts");
  if (acts) acts.style.display = currentDoc ? "" : "none";
  paintAskTitle();
  if (!currentDoc){
    contentEl.dataset.stamp = "";
    contentEl.dataset.pdf = "";
    contentEl.innerHTML = '<p class="empty">从左边选一份文档，或先添加一份。</p>';
    // …and let go of the PDF. `.reader-pdfwrap` is a flex column and the page bar
    // is a toolbar; neither means anything with nothing open. The empty branch
    // used to leave the class behind, so closing a PDF left the pane in a
    // centred column with the next document inside it.
    contentEl.classList.remove("reader-pdfwrap");
    pdfTeardown();
    pdfPaintPageBar();
    return;
  }
  // One stamp per document AND per rendered length: a poll ("same document, same
  // amount shown") must not touch innerHTML, or it drops the text you have
  // selected mid-drag — the bug this whole guard exists for. A change either way
  // (different document, or you clicked 继续加载) repaints.
  const stamp = `${currentDoc.id}:${readerShown}`;
  if (contentEl.dataset.stamp === stamp) return;
  contentEl.dataset.stamp = stamp;
  if (currentDoc.kind === "pdf") renderReaderPdf(contentEl);
  else renderReaderText(contentEl);
}

function renderReaderText(contentEl){
  contentEl.dataset.pdf = "";
  contentEl.classList.remove("reader-pdfwrap");
  pdfTeardown();                     // a text document has no pages to keep alive
  pdfPaintPageBar();
  const total = currentDoc.text.length;
  // A document with no extractable text is not a bug to report as one: a
  // scanned PDF renders fine, it just has no characters for us to search or
  // quote. Saying which it is beats showing an empty pane.
  if (!total){
    contentEl.innerHTML = `<p class="empty">这份文档没有可提取的文字（可能是扫描件，
      里面是文字的图像而不是文字）。你可以正常阅读它，但搜索和「发送给 Agent」都看不到它的内容。</p>`;
    return;
  }
  const shown = Math.min(total, readerShown);
  const body = renderMarkdown(readerChunk(currentDoc.text, shown));
  // Rendered markdown must live inside .r: every .md* style in style.css is
  // scoped to it, so without the wrapper headings, lists and tables come out
  // unstyled — which is exactly how "markdown 渲染不了" looked.
  const more = shown < total
    ? `<div class="reader-more"><button class="sessbtn" onclick="readerMore()">
        继续加载（还有 ${Math.round((total - shown) / 1000)}k 字）</button></div>`
    : "";
  contentEl.innerHTML = `<div class="r">${body}</div>${more}`;
}

// Cut at a blank line so a chunk never splits a code fence or a table in half,
// which would change what the markdown renders to. Falls back to the hard limit
// when there is no boundary to cut on.
function readerChunk(text, shown){
  if (shown >= text.length) return text;
  const cut = text.lastIndexOf("\n\n", shown);
  return text.slice(0, cut > shown * 0.6 ? cut : shown);
}

function readerMore(){
  readerShown += READER_CHUNK;
  renderReaderDoc();
}

// --- PDF rendering (pdf.js) --------------------------------------------------
//
// The ONE non-classic dependency in the frontend, loaded by dynamic import():
// native browser modules, no bundler, no build step. The import is lazy and
// cached, so opening a markdown document never downloads a 1.2 MB worker.
//
// Version and provenance: static/vendor/pdfjs/README.md. In short: 6.x because
// anything before 4.2.67 has CVE-2024-4367 (JS execution out of a crafted PDF).
//
// HOW A LONG PDF IS SHOWN — two separate things, and both had to change:
//
//   THE FILE arrives a RANGE at a time. pdf.js reads the tail first (the
//   cross-reference table lives there), then asks for the pieces it needs as you
//   reach them, so a 900-page paper is not downloaded end to end before page 1
//   appears and 60 MB of pixels are never fetched to show three pages. That needs
//   `Accept-Ranges: bytes` and a 206 on the server side — ops/web/http_range.py.
//
//   THE PAGES are windowed. Every page gets a correctly-sized placeholder the
//   moment the document opens, and a page gets a canvas + text layer only while
//   it is near the viewport. 900 pages is then 900 empty divs, not 900 canvases,
//   and the scrollbar is honest from the first frame. This is also what makes
//   跳到第 700 页 an assignment of scrollTop rather than a fetch, and what makes
//   zooming cheap: the pages you can see are redrawn, not every page you had
//   ever scrolled past.
//
//   The cost is real and worth saying out loud: the browser's own Ctrl+F only
//   finds text on the pages that happen to be rendered. The pane's own 本文内搜索
//   box reads every page's text with pdf.js and reports page numbers, which is
//   the honest replacement rather than a pretence that nothing changed.
let pdfLibPromise = null;
function loadPdfLib(){
  if (pdfLibPromise) return pdfLibPromise;
  pdfLibPromise = import("/static/vendor/pdfjs/build/pdf.min.mjs").then(lib => {
    lib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/build/pdf.worker.min.mjs";
    return lib;
  }).catch(err => { pdfLibPromise = null; throw err; });   // retry on a later open
  return pdfLibPromise;
}

// pdf.js 6 computes a document fingerprint with Uint8Array.prototype.toHex
// (pdf.worker.min.mjs, six call sites), so on a browser without it EVERY pdf
// fails — not an edge case, it is the first thing getDocument does. toHex is
// from 2025 (Chrome/Edge 140, Firefox 133, Safari 18.2). Checking here, before
// 1.7 MB of renderer is fetched, turns an internal "n.toHex is not a function"
// into a sentence that names the fix.
//
// toHex is the one and only hard requirement: it is asked for on every document.
// Nothing else belongs in this check — an earlier version of it also demanded
// Uint8Array.prototype.fromBase64, which does not exist because fromBase64 is a
// STATIC method on the constructor (Uint8Array.fromBase64, used once in the
// worker for base64-embedded content). Reading it off the prototype returned
// undefined on every browser ever made, so that check refused to render ANY pdf,
// everywhere, and looked exactly like "PDF 不支持". Ask for what the worker
// really calls, on the object that really carries it.
function pdfUnsupportedReason(){
  if (typeof Uint8Array.prototype.toHex === "function") return "";
  return "当前浏览器缺少 PDF 渲染需要的 JavaScript 特性（Uint8Array.toHex）。" +
         "Chrome/Edge 140+、Firefox 133+、Safari 18.2+ 才有；升级浏览器即可。";
}
// pdf.js's defaults assume a bundler, so every asset path is given explicitly.
// Without cmaps a Chinese or Japanese PDF renders as blank pages, which is the
// failure that looks most like "PDF 不支持".
const PDF_ASSETS = {
  cMapUrl: "/static/vendor/pdfjs/cmaps/", cMapPacked: true,
  standardFontDataUrl: "/static/vendor/pdfjs/standard_fonts/",
  wasmUrl: "/static/vendor/pdfjs/image_decoders/",
  iccUrl: "/static/vendor/pdfjs/iccs/",
};

// How far past the viewport a page stays rendered. Big enough that scrolling
// does not outrun the renderer, small enough that only a handful of pages are
// ever alive at once — which is the entire point of the window.
const PDF_WINDOW_MARGIN = 1000;
// The white gap between two pages. It is written by pdfBuildPages() rather than
// by a flex `gap` in style.css, because pdfLayout() adds it into every page
// offset: one number in two places is how the scroll position and the pixels
// quietly stop agreeing.
const PDF_PAGE_GAP = 14;
// What a page is assumed to look like before anything has been rendered: US
// Letter, which is also pdf.js's own default. Corrected the moment a page is
// drawn (pdfLearnSize).
const PDF_DEFAULT_SIZE = {w: 612, h: 792};

// The PDF branch could not render. Fall back to the extracted text when there
// is any, and say plainly when there is not (a scan has nothing to fall back to).
function pdfFallback(contentEl, message){
  contentEl.classList.remove("reader-pdfwrap");
  pdfTeardown();                     // nothing of the PDF is on screen any more
  pdfPaintPageBar();
  contentEl.innerHTML = currentDoc.text
    ? `<div class="meta">${message}。下面是从文件中抽取的文字。</div>
       <pre class="pdf-fallback">${esc(currentDoc.text.slice(0, READER_CHUNK))}</pre>`
    : `<div class="meta">${message}。</div>
       <p class="empty">这份 PDF 也没有可提取的文字，所以没有可以退而求其次显示的正文。</p>`;
}

async function renderReaderPdf(contentEl){
  contentEl.innerHTML = `<div class="meta">正在加载 PDF…</div>`;
  contentEl.classList.add("reader-pdfwrap");
  const unsupported = pdfUnsupportedReason();
  if (unsupported){ pdfFallback(contentEl, unsupported); return; }
  let lib;
  try {
    lib = await loadPdfLib();
  } catch (error) {
    // pdf.js missing or blocked. The extracted text is still here, so show it
    // rather than an error page — and when there is no text either (a scan),
    // say what happened instead of rendering an empty box.
    pdfFallback(contentEl, `PDF 渲染器加载失败（${esc(String(error))}）`);
    return;
  }
  // Repainting the SAME document keeps the loaded pdf.js document, the pages
  // already fetched and the reader's zoom. That happens every time the ask panel
  // is opened or closed, because the panel is part of the view markup and the
  // whole view is rebuilt. Everything DRAWn, though, went with the old elements:
  // #rc-content is a new node, and the observer and the placeholders in it are
  // new too (pdfResetView).
  const same = pdfSession && pdfSession.docId === currentDoc.id;
  let session = same ? pdfSession : null;
  if (session) pdfResetView(session);
  else pdfTeardown();
  try {
    if (!session){
      const doc = await lib.getDocument({url: currentDoc.file_url, ...PDF_ASSETS}).promise;
      session = pdfNewSession(currentDoc.id, doc, lib);
    }
  } catch (error) {
    pdfFallback(contentEl, `PDF 打不开：${esc(String(error))}`);
    return;
  }
  pdfSession = session;
  session._root = scrollParent(contentEl);
  session.paneW = Math.max(320, contentEl.clientWidth || 720);
  pdfBuildPages(contentEl, session);
  wirePdfZoom(contentEl);
  wirePdfScroll(contentEl);
  // Zoomed in, a page is wider than the pane, so the pane scrolls sideways and
  // centring has to go (see the .zoomed rule in style.css).
  contentEl.classList.toggle("zoomed", session.zoom !== 1);
  pdfPaintPageBar();
  // Page 1 says what shape the rest of the document is. Until it arrives every
  // placeholder is drawn at US Letter, which is wrong for A4 by a few percent —
  // harmless for reading, but it is the difference between 跳到第 700 页 landing on
  // page 700 and landing on page 698. One getPage() is enough to fix the lot.
  if (!session.unit[0]){
    try {
      const first = await session.doc.getPage(1);
      const box = first.getViewport({scale: 1});
      session.unit[0] = {w: box.width, h: box.height};
      pdfLayout();
    } catch (error) {
      /* Letter-sized placeholders are a fine fallback */
    }
  }
  // Nothing is drawn here on purpose. The observer delivers an initial callback
  // for every target it is handed, so the first screen arrives on the next frame
  // without this having to guess — and "who decided to draw page 3" stays in one
  // function instead of two.
}

// --- the page window ---------------------------------------------------------
//
// WHAT IS IN pdfSession
//   doc, lib       the loaded pdf.js document and the module that made it
//   docId          which document this is, so a repaint can reuse it
//   zoom           the reader's own zoom (ctrl+wheel), 1 by default
//   generation     bumped whenever the DOM this session drew into is gone; an
//                  in-flight page render compares it and throws its own work away
//   paneW          the width pages are fitted to
//   unit[i]        page i's REAL size at scale 1, once a render has reported it
//   size[i]        what page i's placeholder is currently set to (unit × zoom)
//   offsets[i]     page i's top, in px, inside #rc-content — the prefix sum that
//                  makes jumping to a page arithmetic instead of a fetch
//   pages[i]       the placeholder element for page i (always present)
//   live           pages that currently have a canvas + text layer
//   pending        pages being rendered right now (page -> {gen, task})
//   text[i]        page i's text, once 本文内搜索 has read it
//
// Every page gets a placeholder up front and keeps it. That is the whole trick:
// the scrollbar is honest from the first frame, 跳到第 700 页 is a scrollTop
// assignment on a page nobody has fetched, and pdfPointerAnchor()/
// pdfRestoreAnchor() — which count `.pdf-page` elements — keep working untouched,
// because element i IS page i for the whole life of the document.
function pdfNewSession(docId, doc, lib){
  return {
    docId, doc, lib, zoom: 1, generation: 0, paneW: 720,
    unit: [], size: [], offsets: [], pages: [], text: [],
    live: new Set(), pending: new Map(),
    observer: null, _root: null,
  };
}

function pdfTeardown(){
  const session = pdfSession;
  pdfSession = null;
  pdfClearFind();
  if (!session) return;
  pdfResetView(session);
}

// Drop everything this session has DRAWn, keeping what it has FETCHED. Called
// both by pdfTeardown (the session itself is going) and by a repaint of the same
// document (only the elements are).
function pdfResetView(session){
  session.generation++;            // any render in flight is now stale
  if (session.observer){ session.observer.disconnect(); session.observer = null; }
  for (const [, entry] of session.pending){
    if (entry && entry.task && entry.task.cancel) entry.task.cancel();
  }
  session.pending.clear();
  session.live.clear();
}

function pdfStillCurrent(session, generation){
  return pdfSession === session && session.generation === generation;
}

// Build every placeholder, from page 1 to page 900. Cheap enough to do in one
// go — these are empty divs, not canvases.
function pdfBuildPages(contentEl, session){
  pdfResetView(session);
  session.pages = [];
  session.size = [];
  session.offsets = [];
  contentEl.innerHTML = "";
  for (let i = 0; i < session.doc.numPages; i++){
    const el = document.createElement("div");
    el.className = "pdf-page";
    // The gap between pages is written here, not by a flex `gap` in style.css:
    // pdfLayout() adds PDF_PAGE_GAP into every page offset, and a number that
    // exists in two places is how the scroll position and the pixels start to
    // disagree.
    el.style.marginBottom = `${PDF_PAGE_GAP}px`;
    el.appendChild(pdfPlaceholderLabel(i));
    session.pages.push(el);
    contentEl.appendChild(el);
  }
  pdfLayout();
  pdfObserve(session);
}

// The page's number, sitting on the empty sheet. It is also what separates "not
// drawn yet" from "drawn, and this page really is blank" at a glance.
function pdfPlaceholderLabel(i){
  const label = document.createElement("span");
  label.className = "pdf-ph-n";
  label.textContent = String(i + 1);
  return label;
}

function pdfObserve(session){
  if (typeof IntersectionObserver !== "function"){
    // No observer (never a real browser in 2025) -> draw the first screen and
    // leave it at that, rather than showing an empty pane.
    pdfEnsureWindow();
    return;
  }
  // The callback is a TRIGGER, not a decision: it says "what you are looking at
  // moved", and pdfEnsureWindow() works out what that means. Pages are observed
  // once and never unobserved — they stay in the DOM for the life of the
  // session, and a page that scrolls back into view has to be able to ask for a
  // canvas again.
  session.observer = new IntersectionObserver(() => pdfEnsureWindow(), {
    root: session._root,
    rootMargin: `${PDF_WINDOW_MARGIN}px 0px`,
  });
  for (const el of session.pages) session.observer.observe(el);
}

// THE decision point. Scrolling, zooming, jumping and rebuilding all end here,
// so there is exactly one answer to "which pages are drawn".
function pdfEnsureWindow(){
  const session = pdfSession;
  if (!session || !session.pages.length) return;
  const {from, to} = pdfWindow(session);
  for (let i = from; i <= to; i++) pdfRenderPage(i);
  // Pages that left the window give their canvases back. A live selection wins:
  // dropping the text layer under the user's cursor would drop what they had
  // highlighted (the same reason the 5s poll does not repaint this pane).
  for (const i of Array.from(session.live)) if (i < from || i > to) pdfReleasePage(i);
  // Pages that left the window while they were still being fetched or drawn:
  // stop the drawing. The entry is NOT deleted here — pdfRenderPage() owns its
  // own pending entry and cleans up after itself (which is what keeps a
  // cancelled render from leaving half a canvas behind).
  for (const [i, entry] of Array.from(session.pending)){
    if (i >= from && i <= to) continue;
    if (entry && entry.task && entry.task.cancel) entry.task.cancel();
  }
}

// Which pages overlap the viewport, plus one page of slack on each side so a
// fast scroll meets a page that is already drawn.
function pdfWindow(session){
  const root = session._root;
  const view = root.clientHeight || window.innerHeight || 800;
  const at = pdfScrollOffset(session);
  const lo = at - PDF_WINDOW_MARGIN;
  const hi = at + view + PDF_WINDOW_MARGIN;
  let from = pdfLowerBound(session.offsets, lo);
  // A page that STARTS above lo can still reach into the window.
  while (from > 0 && session.offsets[from - 1] + session.size[from - 1].h > lo) from--;
  from = Math.min(from, session.pages.length - 1);
  let to = from;
  while (to + 1 < session.pages.length && session.offsets[to + 1] <= hi) to++;
  return {from, to};
}

// The scroll container's own top in viewport coordinates. <html> reports
// -scrollTop (its box starts at the document origin, not at the viewport), which
// is not what "the top of the viewport" means here — call it 0 and let scrollTop
// carry the difference.
function pdfRootTop(root){
  return root === document.documentElement ? 0 : root.getBoundingClientRect().top;
}

// How far the top of the viewport is, measured from the top of #rc-content — the
// same origin pdfLayout() counts page offsets from, so the two are directly
// comparable. <main> is the scroll container in this layout (style.css), but
// this reads it out of scrollParent() rather than assuming it.
function pdfScrollOffset(session){
  const contentEl = document.getElementById("rc-content");
  if (!contentEl) return 0;
  const root = session._root;
  const base = contentEl.getBoundingClientRect().top - pdfRootTop(root) + (root.scrollTop || 0);
  return (root.scrollTop || 0) - base;
}

// First index whose offset is at or past `target`. The offsets are sorted by
// construction, and 900 pages is not a place to be clever.
function pdfLowerBound(offsets, target){
  let lo = 0, hi = offsets.length;
  while (lo < hi){
    const mid = (lo + hi) >> 1;
    if (offsets[mid] < target) lo = mid + 1; else hi = mid;
  }
  return lo;
}

// Recompute every placeholder's size and top from the zoom and the pane width.
// A few hundred adds, and idempotent, so every path that changes the geometry
// just calls it.
function pdfLayout(){
  const session = pdfSession;
  if (!session) return;
  let top = 0;
  for (let i = 0; i < session.pages.length; i++){
    const next = pdfPageSize(i);
    const prev = session.size[i];
    if (!prev || prev.w !== next.w || prev.h !== next.h){
      session.size[i] = next;
      const el = session.pages[i];
      el.style.width = `${next.w}px`;
      el.style.height = `${next.h}px`;
    }
    session.offsets[i] = top;
    top += next.h + PDF_PAGE_GAP;
  }
}

// The size at scale 1 that page i is drawn from: what a render has reported for
// it, else page 1's, else US Letter. Everything is measured from THIS, so a page
// whose real size arrives later only has to change unit[i] to be right.
function pdfBaseSize(i){
  const session = pdfSession;
  return session.unit[i] || session.unit[0] || PDF_DEFAULT_SIZE;
}

// Fit the page to the pane (never upscaling past 2x — that is just a blurry
// page) and apply the reader's own zoom on top. At zoom 1 this is what it always
// was.
function pdfScaleFor(i){
  return Math.min(2, pdfSession.paneW / pdfBaseSize(i).w) * pdfSession.zoom;
}

function pdfPageSize(i){
  const base = pdfBaseSize(i);
  const scale = pdfScaleFor(i);
  return {w: Math.max(1, Math.floor(base.w * scale)),
          h: Math.max(1, Math.floor(base.h * scale))};
}

// Draw page i: a canvas with pdf.js's text layer over it, so the text in the PDF
// can be selected and sent to the agent exactly like text in a markdown
// document. Idempotent — pdfEnsureWindow() cannot know what is already drawn.
async function pdfRenderPage(i){
  const session = pdfSession;
  if (!session || session.live.has(i) || session.pending.has(i)) return;
  const generation = session.generation;
  const entry = {generation, task: null};
  session.pending.set(i, entry);
  let page;
  try {
    page = await session.doc.getPage(i + 1);
  } catch (error) {
    session.pending.delete(i);    // left as a placeholder: a page we cannot get
    return;
  }
  // The await above is the reason for the token: the document may have been
  // closed, or another one opened, while the page was on its way.
  if (!pdfStillCurrent(session, generation)) return;
  pdfLearnSize(i, page.getViewport({scale: 1}));
  const viewport = page.getViewport({scale: pdfScaleFor(i)});
  const wrap = session.pages[i];
  const canvas = document.createElement("canvas");
  const ratio = window.devicePixelRatio || 1;         // crisp on HiDPI screens
  canvas.width = Math.floor(viewport.width * ratio);
  canvas.height = Math.floor(viewport.height * ratio);
  canvas.style.width = `${Math.floor(viewport.width)}px`;
  canvas.style.height = `${Math.floor(viewport.height)}px`;
  const textLayer = document.createElement("div");
  textLayer.className = "textLayer";
  wrap.appendChild(canvas);
  wrap.appendChild(textLayer);
  const label = wrap.querySelector(".pdf-ph-n");
  if (label) label.remove();
  let drawn = false;
  try {
    entry.task = page.render({canvasContext: canvas.getContext("2d"), viewport,
                              transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0]});
    await entry.task.promise;
    if (!pdfStillCurrent(session, generation)) return;
    // The text layer is what makes a PDF selectable. pdf.js positions the spans
    // and does the character matching; the styling it needs (.textLayer in
    // style.css) is transcribed from pdf.js's own web/pdf_viewer.css, which is
    // Apache-2.0 like the library itself.
    await new session.lib.TextLayer({
      textContentSource: await page.getTextContent(),
      container: textLayer,
      viewport,
    }).render();
    drawn = true;
  } catch (error) {
    // A cancelled render (the page left the window mid-draw) lands here. Not a
    // failure — but what is on the canvas is half a page, so it is thrown away
    // rather than kept: marking a blank page "live" would mean the window never
    // draws it again.
  }
  if (!pdfStillCurrent(session, generation)) return;
  session.pending.delete(i);
  if (drawn) session.live.add(i);
  else pdfStripPage(session, i);
}

// Take page i back down to a bare placeholder. The BOX does not move — only the
// canvas and the text layer go — so releasing can never shift what you are
// looking at.
function pdfStripPage(session, i){
  const el = session.pages[i];
  if (!el) return;
  const canvas = el.querySelector("canvas");
  const layer = el.querySelector(".textLayer");
  if (canvas) canvas.remove();
  if (layer) layer.remove();
  if (!el.querySelector(".pdf-ph-n")) el.appendChild(pdfPlaceholderLabel(i));
}

function pdfReleasePage(i){
  const session = pdfSession;
  if (!session || !session.live.has(i)) return;
  if (!pdfCanRelease()) return;
  pdfStripPage(session, i);
  session.live.delete(i);
}

// A page may not be released while the user has text selected: the selection
// lives in the text layer, and taking the layer away silently drops what they
// highlighted.
function pdfCanRelease(){
  const selection = window.getSelection && window.getSelection();
  return !selection || selection.isCollapsed !== false || !String(selection).trim();
}

// A page's real size, replacing the estimate it opened with. The pages below it
// move down, so the scroll position has to be corrected or the page you are
// reading slides out from under you.
function pdfLearnSize(i, base){
  const session = pdfSession;
  const before = session.size[i];
  session.unit[i] = {w: base.width, h: base.height};
  pdfLayout();
  const after = session.size[i];
  const oldH = before ? before.h : after.h;
  const grow = after.h - oldH;
  if (!grow || !session._root) return;
  // Reading the rect AFTER pdfLayout(): the page's top has not moved (only the
  // pages above it could move it, and they have not changed), but its height has.
  const rect = session.pages[i].getBoundingClientRect();
  const rootTop = pdfRootTop(session._root);
  // Everything below a point d px down the page moves by d x (new/old - 1). Put
  // the viewport where it was by scrolling exactly that much; d is clamped to the
  // page, so a page entirely above the viewport shifts everything by the full
  // growth.
  const d = Math.max(0, Math.min(oldH, rootTop - rect.top));
  session._root.scrollTop += d * (after.h / Math.max(1, oldH) - 1);
}

// --- jumping, and where am I ------------------------------------------------

// The app's own header (.pagehead) is sticky at the top of the same scrollport
// and outranks the page bar on z-index, so a bar pinned to top:0 spends the
// whole document tucked behind it: every button is in the DOM, every rect looks
// right, and not one of them can be seen or clicked. Measured rather than
// assumed, because its height is padding plus an h1 plus a line of text.
function pdfHeadClearance(){
  const head = typeof document.querySelector === "function"
    ? document.querySelector("header.pagehead") : null;
  return head && head.offsetHeight ? head.offsetHeight : 0;
}

// How much of the top of the viewport is covered while a PDF is open: the app
// header, then the page bar, then a small gap so the page's first line is not
// flush against the toolbar. pdfJumpTo() parks a page below this line and
// pdfCurrentPage() measures from the same line — if the two ever disagree, a
// jump to 350 reports 349 (that is exactly how this was found).
function pdfPageBarLead(){
  const bar = document.getElementById("rc-pagebar");
  const barH = bar && bar.offsetHeight ? bar.offsetHeight + 8 : 0;
  return pdfHeadClearance() + barH;
}

function pdfCurrentPage(){
  const session = pdfSession;
  if (!session || !session.pages.length) return 0;
  const at = Math.max(0, pdfScrollOffset(session) + pdfPageBarLead());
  let i = pdfLowerBound(session.offsets, at);
  if (i >= session.pages.length || session.offsets[i] > at) i--;
  return Math.max(0, Math.min(session.pages.length - 1, i));
}

// Jump by scrolling a placeholder into place. The target page does not have to
// be rendered, or even fetched — this is arithmetic on the offsets, which is the
// whole reason every page has a placeholder from the start.
function pdfJumpTo(i){
  const session = pdfSession;
  if (!session || !session.pages.length) return;
  const page = Math.max(0, Math.min(session.pages.length - 1, Math.round(i) || 0));
  const contentEl = document.getElementById("rc-content");
  if (!contentEl) return;
  const root = session._root;
  const base = contentEl.getBoundingClientRect().top - pdfRootTop(root) + (root.scrollTop || 0);
  // Leave the page bar's own height above the page: it is sticky, so a page
  // aligned to the very top would have its first lines behind the toolbar.
  root.scrollTop = base + session.offsets[page] - pdfPageBarLead();
  pdfFlashPage(page);
  pdfEnsureWindow();
  pdfPaintPageBar();
}

function pdfStep(delta){ pdfJumpTo(pdfCurrentPage() + delta); }

function pdfJumpFromInput(){
  const box = document.getElementById("rc-pagebox");
  if (!box) return;
  const wanted = parseInt(box.value, 10);
  if (Number.isFinite(wanted)) pdfJumpTo(wanted - 1);
}

function pdfPageBoxKey(ev){
  if (ev.key === "Enter") pdfJumpFromInput();
}

// A brief outline on the page you landed on. On a document with a text layer you
// can see where you are; on a scanned one every page looks like every other.
function pdfFlashPage(i){
  const session = pdfSession;
  const el = session && session.pages[i];
  if (!el) return;
  el.classList.add("pdf-flash");
  setTimeout(() => el.classList.remove("pdf-flash"), 700);
}

function pdfPaintPageBar(){
  const bar = document.getElementById("rc-pagebar");
  if (!bar) return;
  const session = pdfSession;
  if (!session || !session.pages.length){ bar.style.display = "none"; return; }
  bar.style.display = "";
  // Stick below the app header rather than at top:0 — see pdfHeadClearance().
  // Written only when it changes: this runs on every scroll frame.
  const top = pdfHeadClearance() + "px";
  if (bar.style.top !== top) bar.style.top = top;
  const current = pdfCurrentPage();
  const info = document.getElementById("rc-pageinfo");
  if (info) info.textContent = `第 ${current + 1} / ${session.pages.length} 页`;
  const box = document.getElementById("rc-pagebox");
  // Never write the number back into the box while it has focus: you would be
  // typing "38" and the bar would rewrite it as "3" halfway through.
  if (box && document.activeElement !== box) box.value = String(current + 1);
}

// The scroll listener lives on <main> (the real scroll container), which no
// rebuild ever replaces — so it is attached once for the life of the page, and a
// document opened later still drives it. Repainting the page number is the one
// thing that cannot wait for the IntersectionObserver: its root margin is a
// thousand pixels, and the number would lag a page behind.
function wirePdfScroll(contentEl){
  if (pdfScrollWired) return;
  pdfScrollWired = true;
  scrollParent(contentEl).addEventListener("scroll", () => {
    if (!pdfSession || pdfBarQueued) return;
    pdfBarQueued = true;
    const run = () => { pdfBarQueued = false; pdfPaintPageBar(); };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
    else setTimeout(run, 16);
  }, {passive: true});
}

// --- 本文内搜索 -------------------------------------------------------------
//
// THE HONESTY NOTE, and it is a real trade: because only the pages near the
// viewport have a text layer, the browser's own Ctrl+F finds text on those pages
// and nowhere else. So the pane searches every page itself — pdf.js will hand
// back a page's text without drawing it — and reports the page each hit is on.
// What it deliberately does NOT do yet is highlight inside the page: mapping a
// character offset back onto pdf.js's spans is a separate problem, and being
// able to find it and get there is the part that was missing.
const PDF_FIND_MAX = 200;      // more hits than this and nobody is reading them
const PDF_FIND_BATCH = 8;      // pages between two progress repaints
const PDF_FIND_SLACK = 30;     // characters of context on each side of a hit

// Forget the last search. The page bar belongs to the VIEW, not to the pane, so
// it is not rebuilt when another document is opened — without this, the hits
// from the PDF you just closed would still be listed under the next one.
function pdfClearFind(){
  pdfFindSeq++;                     // and stop a walk that is still in flight
  const box = document.getElementById("rc-pdfind");
  const meta = document.getElementById("rc-pdfindmeta");
  const hits = document.getElementById("rc-pdfhits");
  if (box) box.value = "";
  if (meta) meta.textContent = "";
  if (hits) hits.innerHTML = "";
}

// Searching 900 pages takes a while, so a new query has to be able to cancel the
// one before it — otherwise every keystroke queues another full walk. The token
// is a counter: the loop compares it after every await and gives up if it moved.
async function pdfFindInput(){
  const box = document.getElementById("rc-pdfind");
  const meta = document.getElementById("rc-pdfindmeta");
  const hits = document.getElementById("rc-pdfhits");
  const session = pdfSession;
  const query = (box && box.value || "").trim();
  const token = ++pdfFindSeq;
  if (!meta || !hits) return;
  hits.innerHTML = "";
  if (!session || !query){ meta.textContent = ""; return; }
  const needle = query.toLowerCase();
  const found = [];
  for (let i = 0; i < session.doc.numPages; i++){
    const text = await pdfPageText(session, i);
    if (token !== pdfFindSeq) return;
    const at = text.toLowerCase().indexOf(needle);
    if (at >= 0){
      found.push({page: i, snippet: pdfSnippet(text, at, query.length)});
      if (found.length >= PDF_FIND_MAX) break;
    }
    if (i % PDF_FIND_BATCH === 0){
      meta.textContent = `正在搜索 ${i + 1}/${session.doc.numPages} 页…`;
      await new Promise(resolve => setTimeout(resolve, 0));
    }
  }
  if (token !== pdfFindSeq) return;
  if (!found.length){ meta.textContent = `没有找到「${query}」`; return; }
  meta.textContent = found.length >= PDF_FIND_MAX
    ? `命中已达上限（前 ${PDF_FIND_MAX} 处）` : `${found.length} 处命中`;
  hits.innerHTML = found.map(hit => `<div class="pdf-hit" onclick="pdfJumpTo(${hit.page})">
      <span class="pdf-hit-page">第 ${hit.page + 1} 页</span>
      <span class="pdf-hit-text">${esc(hit.snippet)}</span>
    </div>`).join("");
}

// One page's text, read once and remembered. pdf.js returns positioned items,
// and the newlines it reports are what keep two lines from gluing together into
// a match that is not in the document.
async function pdfPageText(session, i){
  if (session.text[i] !== undefined) return session.text[i];
  let text = "";
  try {
    const page = await session.doc.getPage(i + 1);
    const content = await page.getTextContent();
    text = content.items.map(item => (item.str || "") + (item.hasEOL ? "\n" : "")).join("");
  } catch (error) {
    text = "";                  // a page we cannot read has no hits, not an error
  }
  session.text[i] = text;
  return text;
}

function pdfSnippet(text, at, length){
  const start = Math.max(0, at - PDF_FIND_SLACK);
  const end = Math.min(text.length, at + length + PDF_FIND_SLACK);
  return (start ? "…" : "") + text.slice(start, end).replace(/\s+/g, " ")
         + (end < text.length ? "…" : "");
}

// --- zooming the page, not the window ---------------------------------------

// Ctrl+wheel is the browser's own page zoom, which on a document viewer zooms
// the wrong thing: the sidebar and the library grow with the text and you lose
// your place. So the pane takes the gesture for itself — preventDefault needs a
// non-passive listener — and re-draws the pages bigger or smaller.
function wirePdfZoom(contentEl){
  // #rc-content survives a repaint (renderReaderDoc replaces its children, not
  // the element), so the flag lives on the element: a plain addEventListener
  // here would add one more zoom step on every repaint.
  if (contentEl.dataset.zoomWired) return;
  contentEl.dataset.zoomWired = "1";
  contentEl.addEventListener("wheel", (ev) => {
    if (!ev.ctrlKey || !pdfSession) return;
    ev.preventDefault();
    // Pin the zoom to the point under the pointer, the way a map does.
    pdfPendingAnchor = pdfPointerAnchor(contentEl, ev.clientY);
    const step = ev.deltaY < 0 ? PDF_ZOOM_STEP : 1 / PDF_ZOOM_STEP;
    pdfSession.zoom = Math.min(PDF_ZOOM_MAX, Math.max(PDF_ZOOM_MIN, pdfSession.zoom * step));
    contentEl.classList.toggle("zoomed", pdfSession.zoom !== 1);
    schedulePdfRedraw();
  }, {passive: false});
}

// Which page the pointer is over, and how far down it (0..1). The index is a
// real page number here -- every page has had a placeholder since the document
// opened -- so this needed no change to survive virtualisation.
function pdfPointerAnchor(contentEl, clientY){
  const pages = contentEl.querySelectorAll(".pdf-page");
  for (let i = 0; i < pages.length; i++){
    const rect = pages[i].getBoundingClientRect();
    if (clientY < rect.bottom){
      return {index: i, frac: Math.min(1, Math.max(0, (clientY - rect.top) / (rect.height || 1)))};
    }
  }
  return pages.length ? {index: pages.length - 1, frac: 0} : null;
}

function schedulePdfRedraw(){
  // One redraw per burst of wheel ticks: rendering a page takes longer than the
  // ticks arrive, so a redraw started per tick would only queue up.
  clearTimeout(pdfZoomTimer);
  pdfZoomTimer = setTimeout(pdfRescale, 160);
}

// The payoff of virtualisation, and the reason zooming a 900-page document used
// to be unusable: a zoom re-renders the two or three pages you can see, not the
// sixty that had been drawn. The placeholders take the new size, the pages in the
// window are drawn again at the new resolution (their canvases are the old one's
// and would be blurry), and the point under the pointer stays where it was.
async function pdfRescale(){
  const session = pdfSession;
  const contentEl = document.getElementById("rc-content");
  if (!session || !contentEl || !contentEl.classList.contains("reader-pdfwrap")) return;
  const anchor = pdfPendingAnchor;
  const pages = contentEl.querySelectorAll(".pdf-page");
  const page = anchor ? pages[Math.min(anchor.index, pages.length - 1)] : null;
  // Plain numbers, taken now: a rect is only useful while the page is still the
  // old size, and this keeps the arithmetic below readable.
  const rect = page ? page.getBoundingClientRect() : null;
  const beforeTop = rect ? rect.top : 0;
  const beforeHeight = rect ? rect.height : 0;
  // "live" is cleared first and deliberately: the canvases it names are about to
  // be thrown away BY this resize, so pdfEnsureWindow() below has to draw them
  // again rather than believe they are still there.
  session.live.clear();
  for (const [, entry] of session.pending){
    if (entry && entry.task && entry.task.cancel) entry.task.cancel();
  }
  session.pending.clear();
  session.generation++;
  for (let i = 0; i < session.pages.length; i++) pdfStripPage(session, i);
  pdfLayout();
  pdfEnsureWindow();
  if (rect) pdfRestoreAnchor(contentEl, anchor, beforeTop, beforeHeight);
  pdfPaintPageBar();
}

// Zoom changes the height of everything above the page you are looking at, so
// the scroll offset has to be corrected or the text jumps out from under the
// pointer. The anchored point sits `frac` of the way down its page: the page
// moved by (top - beforeTop), and grew by (height - beforeHeight), of which
// `frac` is above the point.
function pdfRestoreAnchor(contentEl, anchor, beforeTop, beforeHeight){
  const pages = contentEl.querySelectorAll(".pdf-page");
  const page = pages[Math.min(anchor.index, pages.length - 1)];
  if (!page) return;
  const rect = page.getBoundingClientRect();
  scrollParent(contentEl).scrollTop +=
    (rect.top - beforeTop) + anchor.frac * (rect.height - beforeHeight);
}

// The nearest scrolling ancestor — <main> in this layout. Writing to scrollTop
// works for whatever that turns out to be, so this does not hardcode the shell.
function scrollParent(el){
  for (let node = el.parentElement; node; node = node.parentElement){
    const overflowY = getComputedStyle(node).overflowY;
    if (overflowY === "auto" || overflowY === "scroll") return node;
  }
  return document.scrollingElement || document.documentElement;
}

// --- selection -> the agent --------------------------------------------------

// Publish the open document (and any selection) to the Context Bridge, so the
// next turn of an agent sees what you are reading. Kept out of the poll: this
// runs on open and on send, which are the moments the context actually changes.
//
// It publishes TWICE, because two different agents can be asked about this
// document and the bridge is keyed by (agent, session):
//   * ASK_AGENT — the panel on the right. It asks as `reader`, and the server
//     looks the snapshot up as (agent.agent_id, agent.session.session_id).
//   * ACTIVE_AGENT — the main conversation, for the 选中文字→发送 flow (rcSend),
//     which hands the text to the agent you are browsing as.
// Publishing only under ACTIVE_AGENT is why the panel used to answer "我看不到
// 你打开的是哪篇": the document was filed under ("default", <default thread>)
// while the panel's own turn ran as `reader` and looked up ("reader", ...).
function publishReaderContext(extra = {}){
  if (!currentDoc) return Promise.resolve({ok:false, error:"no document"});
  const snapshot = {
    application: "reader",
    resource: currentDoc.title,
    content: currentDoc.text,
    selection: extra.selection || "",
    metadata: {doc_id: currentDoc.id, kind: currentDoc.kind}
  };
  const targets = readerContextTargets();
  return Promise.all(targets.map(([agent_id, session_id]) =>
    postJSON("/api/extras", {...snapshot, agent_id, session_id})
  )).then(results => ({ok: results.every(r => r && r.ok), results}));
}

// Where the open document is filed. ONE list: publishReaderContext() writes
// these keys and clearReaderContext() deletes them, so the two cannot disagree
// about where the document lives.
function readerContextTargets(){
  return [
    [ASK_AGENT, askSessionId()],
    [ACTIVE_AGENT, SESSION || D?.current_sessions?.[ACTIVE_AGENT] || "default"]
  ];
}

// Take the document back off the bridge — the undo of publishReaderContext().
// Same two keys, same expression, and idempotent: clearing a key nothing was
// written to is not an error.
function clearReaderContext(){
  return Promise.all(readerContextTargets().map(([agent_id, session_id]) =>
    postJSON("/api/extras", {action: "clear", agent_id, session_id})
  )).catch(() => null);
}

// The reader agent's own thread: the one the OPEN DOCUMENT owns, so a document's
// questions stay with the document (one document, one conversation). The prefix
// is not arbitrary — the server treats `doc-` threads specially in two places
// (browser_agent.DOC_PREFIX: they are never resumed as "the recent chat", never
// rotated), so the two sides have to spell it the same way.
//
// No document open -> fall back to whatever thread the reader agent is in.
const ASK_DOC_PREFIX = "doc-";
function askSessionId(){
  if (currentDoc) return ASK_DOC_PREFIX + currentDoc.id;
  return (D && D.current_sessions && D.current_sessions[ASK_AGENT]) || "default";
}

function rcSend(){
  const sel = window.getSelection();
  if (!sel.rangeCount || !sel.toString().trim()) { alert("请先在文档中选中文字"); return; }
  const text = sel.toString();
  return publishReaderContext({selection: text}).then(res => {
    if (!res.ok){ alert("注入失败：" + (res.error || "未知错误")); return; }
    const panel = document.getElementById("rc-selection");
    if (panel) panel.style.display = "none";
    // It lands in the ask panel on the right, NOT in the main conversation.
    // This used to prefill #dmsg and, when that was not on screen, navigate to
    // #agent/<active> to go find it — so quoting a sentence threw you out of the
    // document you were reading. The panel is already next to the document, and
    // sending there is what the reader's own agent is for; the selection still
    // reaches the main conversation, because publishReaderContext() above files
    // it under BOTH agents.
    //
    // The panel's markup is generated, so a CLOSED panel has to be opened
    // through a rebuild (see setAskPanel) -- and the box can only be filled
    // after it, because the rebuild replaces it. An open panel is left alone:
    // rebuilding it would throw away the text selection for nothing.
    if (!askPanelOpen()) setAskPanel(true);
    const box = document.getElementById("amsg");
    if (!box) return;
    box.value = "[选中文本] " + text.substring(0, 200) + (text.length > 200 ? "..." : "");
    box.focus();
  }).catch(err => alert("网络错误：" + err));
}

function rcClearSelection(){
  window.getSelection()?.removeAllRanges();
  const panel = document.getElementById("rc-selection");
  if (panel) panel.style.display = "none";
}

// Detect a fresh selection and reveal the send panel. We never rewrite the
// document here — see the stamp guard in renderReaderDoc().
function syncReaderSelection(){
  const selEl = document.getElementById("rc-selection");
  const textEl = document.getElementById("rc-text");
  if (!selEl || !textEl) return;
  const text = window.getSelection().toString().trim();
  if (text){
    textEl.textContent = text;
    selEl.style.display = "block";
  } else {
    selEl.style.display = "none";
  }
}

// Called at the end of every render() (main.js) while the reader is the view.
// Async, and its result is deliberately ignored by the caller: render() is
// synchronous and must not block on a fetch. Awaiting the list here is what
// keeps the ORDER right — the list paints before the pane does, and a caller
// that does want to wait (the tests) can.
async function restoreReaderState(){
  // loadLibrary() itself fetches once per page (libraryLoaded), which is all the
  // restore below needs: after a refresh the JS state starts empty, so the first
  // call here does hit the server. Re-fetching on every poll would break the
  // "a poll does not re-fetch" rule this pane is built on.
  await loadLibrary();
  // The document itself lives in memory, so the 5s poll keeps it for free --
  // but a page reload starts with nothing. readerOpen() wrote the id to
  // localStorage for exactly that case: come back to the document you were
  // reading, unless it is gone from the library.
  if (!currentDoc){
    const savedId = localStorage.getItem(READER_DOC_KEY);
    if (savedId && libraryDocs.some(doc => doc.id === savedId)) await readerOpen(savedId);
  }
  renderLibrary();
  renderReaderDoc();
  syncReaderSelection();
}
