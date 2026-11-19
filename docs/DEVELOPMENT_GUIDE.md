# 开发指南

知研 是一个无框架的 Personal Agent Platform。Agent Loop、会话、上下文、记忆和工具系统都由项目自己实现；不同 Agent 与 Application 在这套公共运行时上组合能力。

这份文档只说明当前代码如何运行和扩展，不记录开发过程日志。

## 本地环境

项目需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
uv sync --extra dev
uv run zhiyan
```

模型服务商和 API Key 可以在 Web 的“模型”页面配置。`.env`、`.zhiyan/`、运行轨迹和本机凭证都不能提交。

## 代码阅读顺序

1. `zhiyan/core/spec.py`：一个 Agent 可以声明哪些差异。
2. `zhiyan/agents/catalog.py`：内置 Agent 如何组合提示词和工具。
3. `zhiyan/core/runtime.py`：一次完整 Turn 如何装配。
4. `zhiyan/core/loop.py`：模型与工具之间的原生循环。
5. `zhiyan/core/session.py`：会话历史、恢复和持久化。
6. `zhiyan/core/context/`：工具结果预算、裁剪和状态摘要。
7. `zhiyan/memory/`：记忆门控、存储、整理和技能。
8. `zhiyan/applications/context_bridge.py`：Application 如何把页面状态交给 Agent。
9. `zhiyan/ops/web/` 与 `zhiyan/ops/static/`：Web API、SSE 和原生前端。

## 新增 Agent

Agent 是 `AgentSpec`，不是一套新的运行时代码。通常只需要在 `zhiyan/agents/catalog.py` 中增加一个 `AgentProfile`：

```python
AgentProfile(
    id="planner",
    name="Planner",
    icon="◇",
    description="把复杂目标拆成可执行计划。",
    spec=AgentSpec(
        name="planner",
        system_prompt="You are a planning agent...",
        tools=frozenset({"search_web", "save_note"}),
    ),
)
```

运行时会自动处理模型调用、会话、上下文策略、记忆和轨迹。需要注意：

- `id` 必须稳定，因为会话和运行记录会使用它。
- `tools` 是白名单；不要给 Agent 暴露不需要的能力。
- 如果默认上下文策略不合适，可以在 `AgentSpec.context_policy` 中提供另一条 `ContextPolicy`。
- 新增 Profile 后要覆盖列表接口、页面切换、工具边界和会话恢复测试。

## 新增 Application

Application 是带状态的工作界面。Reader 会发布当前文档和选区，知识库会维护笔记和文件夹；它们最终都把需要的现场信息交给同一个 Agent Runtime。

新增 Application 时按下面的边界组织：

1. 在 `zhiyan/applications/` 中实现领域逻辑和数据操作。
2. 在 `zhiyan/ops/web/server.py` 与 `data.py` 中增加明确的 API。
3. 在 `zhiyan/ops/static/js/` 中增加页面和交互，并在 `bootstrap.js` 中登记模块。
4. 需要 Agent 理解当前页面时，通过 `ApplicationContextBridge.publish()` 发布有界的纯文本快照。**不是每个 Application 都要发**：页面上没有"Agent 自己拉不到"的现场状态时就别发。
5. 发起对话时，用 `ApplicationContextBridge.render()` 的结果作为 `extra_context`，不要把前端对象直接传进 Core。
6. 为页面状态隔离、上下文长度边界和 API 行为补确定性测试。

Application 的持久数据应该进入自己的存储；Context Bridge 只保存当前页面的临时状态。

## 新增 Tool

1. 在 `zhiyan/tools/` 中实现工具，保持输入和输出容易测试。
2. 在 `zhiyan/tools/__init__.py` 中注册 `Tool` 和参数 Schema。
3. 如果工具只属于某些 Agent，把名称加入对应的工具白名单。
4. 给成功、失败和权限边界补确定性测试。
5. 如果工具会访问外部服务，在 `SECURITY.md` 中说明配置和数据流向。

## 前端约定

Web 前端目前是原生 HTML、CSS 和 JavaScript，没有 Node 构建链。`zhiyan/ops/static/js/bootstrap.js` 按顺序加载各功能模块；新增模块时必须显式加入 `FEATURES`。

修改静态文件后刷新页面即可看到结果；修改 Python 后端后需要重启 `uv run zhiyan`。

两条容易踩的约定：

- **内联 handler 里调的函数必须顶格写。** `bootstrap.js` 靠"行首 `function` 声明 + 出现在 `onclick=` 之类的字符串里"把函数挂到 `window`。缩进一格就静默不导出——按钮点了没反应，控制台也不报错。
- **`page.evaluate()` 读不到页面的业务状态。** 十几个 `.js` 被塞进同一个 `new Function` 作用域，只有被内联 handler 点过名的函数才在 `window` 上；`currentDoc` 这种 `const`/`let` 一律 `ReferenceError`。要从 DOM 读，或者直接问接口。

## 阅读器的 PDF 是怎么加载的

阅读区（`#reader`）打开一份 PDF 时走两条路，各自的理由不同：

**1. 文件本身走 HTTP Range 分块传（`zhiyan/ops/web/http_range.py`）**

`GET /api/library/file` 支持 `Range`，答 200 / 206 / 416。这条不是优化而是前提：pdf.js 是从**文件末尾**开始读的（先 trailer，再 xref，再一页一页取），而它只有在响应里看到 `Accept-Ranges: bytes` 时才会去试 Range。不支持 Range 的服务器上，pdf.js 只能把整份文件下下来——900 页 PDF 就是几十 MB。

`parse_range()` 是纯函数，三态必须分清：`None` = 忽略这个头、整个文件返回；元组 = 206 那一段；`RangeError` = 416。**最要紧的是别把"要不到"答成 200**：那等于让客户端为了一个字节下 20 MB。

**2. 页面靠虚拟化渲染（`zhiyan/ops/static/js/reader.js`）**

打开时**立刻给每一页建一个占位元素**（高度先用第 1 页的比例估），所以滚动条从第一秒就是对的、`pdfJumpTo(n)` 只是对偏移量做算术、不需要渲染中间的页。然后由 `IntersectionObserver` 驱动 `pdfEnsureWindow()`——**"画哪几页、放掉哪几页"只有这一个决策点**：视野内缺的 `pdfRenderPage()`，出视野的 `pdfReleasePage()`（占位高度不还，所以释放不会让正在读的那一页跳走）。缩放同理：重画的是屏幕上那两三页，不是已渲染的全部。

几条会咬人的细节：

- **滚动位置和页码都从 `.pagehead`（应用自己那条 sticky 表头）底下量起**，见 `pdfHeadClearance()` / `pdfPageBarLead()`。表头 z-index 更高，分页栏贴 `top:0` 会整条藏在它背后——DOM 里全在、`getBoundingClientRect()` 也正常，就是一个按钮都点不到。而且这个高度必须同时用于"跳到第几页"和"现在是第几页"两处计算：两边不一致的表现，就是跳到 350 却显示 349。
- **学到真实页高时要补偿滚动位置**（`pdfLearnSize`），否则上面那些页一长高，你正在看的内容就会往下溜。
- **有文本被选中时拒绝 `pdfReleasePage()`**，和 5 秒轮询不许重建面板是同一个道理。
- 页与页之间的间隙是 JS 常量 `PDF_PAGE_GAP`，不是 CSS 的 `gap`——`pdfLayout()` 要把它算进每页偏移量，一个数只能有一个地方说了算。

**已知取舍：浏览器的 Ctrl+F 只搜得到已经渲染出来的那几页**（虚拟化的必然代价）。补上的是工具栏里的「本文内搜索」，它逐页问 pdf.js 要文本，覆盖整份 PDF，结果显示页码、点了跳过去。页内高亮本轮没做。

**还没做、留给下一轮 RAG 的**：按页存文本的 `document_pages` 表。切块本来就按页，页表到那时候一起引入最划算；本轮不引入，是因为虚拟化渲染不需要它。

## 文档库的语义检索（RAG）

**检索是 Agent 手上的一个工具，不是页面上的一块面板。** Reader agent 有
`semantic_search`（按意思找）和 `search_documents`（按字面找），它自己判断该不该调、
调哪个——所以 `zhiyan/rag/` 里**没有**任何负责"生成答案"的代码，回答由调工具的同一个模型写。
`zhiyan/rag/` 七个文件，各管一段：

| 文件 | 干什么 |
|---|---|
| `chunk.py` | 文本 → 块。**纯函数**，切块策略全在这里 |
| `embed.py` | 文本 → 向量。一个 HTTP 请求到 OpenAI 兼容的 `/v1/embeddings` |
| `store.py` | 向量 ↔ SQLite。float32 打包、余弦、索引状态 |
| `index.py` | 一份文档 → 它的那些行（worker 跑的就是这个） |
| `worker.py` | 唯一持有线程的地方 |
| `rerank.py` | 候选 → 相关性分。可选的一层，配了才用（见下面「重排」） |
| `query.py` | 检索（`semantic_search`），工具和页面都调它 |

**怎么开**：`ZHIYAN_EMBED_PROVIDER` + `ZHIYAN_EMBED_MODEL` 都填上才算开
（例如 `local_ollama` / `bge-m3`）。本地 ollama：`ollama pull bge-m3`，
再把 `local_ollama` 这个自定义服务商指向 `http://127.0.0.1:11434/v1`。
**两个都空 = 功能整个关着**：不建表、不索引，行为与加这个功能之前一字不差。

**GPU 不用我们管**：ollama 自己探硬件、自己决定跑 GPU 还是 CPU，还会在两者之间切
（`ollama ps` 会打印 `100% GPU` / `37%/63% CPU/GPU`）。知研 这边没有可检测的东西，
检索那点余弦是纯 Python、零点几毫秒。**别为这件事写代码。**

### 切块：为什么是"结构优先、尺寸兜底"

1. **Markdown 标题是硬边界**。先按标题切成章节，每块带上它的**标题路径**当地址
   （`长上下文 › 检索`）。两个理由：标题是文档自己的话题划分；而且短章节全靠这个地址
   才有上下文——`## 缓存 / 用 KV cache。` 单独嵌出来是没有意义的。
   **不跨标题合并**：那会把「检索」的尾巴和「缓存」的头塞进同一块，用前者的地址命名，
   正是让向量什么都像、什么都不像的那种稀释。
2. **太长就递归切**：段落 → 行 → 句子 → 从句 → 词 → 字。只有强的分隔符切不动了才用弱的，
   所以只有"整段没有任何标点"的文本才会被从中间砍断。中文认 `。！？；`，英文认 `. ! ?`。
3. **太短就并起来**到目标长度，相邻块留一点重叠（跨接缝的那句话两边都还搜得到）。

**PDF 不按页切**。页是排版单位，不是话题单位：一页可以同时装着上一节的尾巴和下一节的开头，
跨页断开的一段会被拦腰截断。所以 PDF 也当文本来切，切完再把每块的**字符偏移**映射回页码
（`第 5–6 页`）——引用照样精确、可跳转、可人工核对，但不让页去决定话题边界。
偏移能算得准，是因为切分器一个字符都不丢。

**不做语义分块**（逐句 embed 找话题断点）：索引时要多跑一遍全文档的 embedding，
而且模型一换边界就全变。结构和递归切分能拿到大部分好处，还不用额外花钱。

### 索引什么时候建

**上传之后自动建，没有"建立索引"按钮。** 做法不是往队列里推，而是 worker 每秒**反过来问**
"还有哪份文档没建好"——一条 LEFT JOIN：没有索引行的、排队的、上次被中断的、
以及**用另一个模型建的**。这样上传、URL 抓取、Agent 自己的 `fetch_document`
三种入口都不需要知道这个模块存在，而且启用功能之前就在库里的文档**第一次启用时会自动补上**。

- 线程归 `server.main()` 起，`tick()` 是普通函数，**测试直接调它，永远不会意外起线程**。
- 进度写回数据库（不是内存变量），所以刷新页面还看得见「建立中 137/400」。
- 出错记在行上、**不吞**；也**不自动重试**（对一个坏端点每秒重试不如等着被叫）。
- **模型换了会自动重新排队**。两个 embedding 模型把同一句话放在毫不相干的位置，
  跨模型比较**不会报错**，只会给出很自信的废话——所以"这张表的向量是谁建的"必须记下来。

### 检索与回答

- `semantic_search()` 只检索，不调模型，所以它便宜、可测，是 Agent 工具直接调的东西。
- **回答由 Agent 自己写**，服务器上不再有第二条生成路径——否则同一个问题从不同的门进来
  会有两个答案，而且都在各自决定怎么标页码。
- **引用必须可核对，所以回复里的「第 N 页」是可点的**：`linkCitations()` 在日志渲染
  **比较之前**把页码包成 `<span class="cite">`（挂在 `syncLogClass` 的 `transform` 参数上），
  点了走 `citeJump` → 上一轮做好的 `pdfJumpTo`。**只有开着 PDF 时才包**，否则主对话里
  到处都是点了没反应的假链接。

#### 包页码这一步踩过的两个坑（已修）

包页码是**拿正则改 HTML 字符串**，所以得先知道哪些地方不能碰。漏了会出事，而且两次都是真出过的：

1. **同一条回复在页面上有两份**：正文里一份，复制按钮的 `data-text="…"` 属性里还存着一份。
   页码正好落在属性里的时候，注入的 `<span class="cite" onclick="…">` 自带引号，会把
   `data-text` 提前关掉——属性断成两截，剩下的整篇正文被浏览器当成**按钮的文字**。
   而那个按钮是 `.card:hover` 才显示的，于是鼠标一移到消息上，整篇生 markdown 就盖住回复，
   移开又好了。用户报的"显示时好时坏"就是它。
2. **代码块原来只护住了一半**。保护那一句写的是 `<div class="mdcode">…</div>`，可真的
   代码块里还有一层 `<div class="mdcode-head">`（`renderMarkdown` 拼的），非贪婪匹配在
   标题栏就收尾了：只护住标题栏，`<pre>` 正文里的页码照样被包。工具输出的预览 `.tt-short`
   同样是 `<pre>`，一样漏。

现在挖的是 `CITE_HOLES = /(<pre[\s\S]*?<\/pre>|<[^>]*>)/g`——**整条标签**和 **`<pre>` 正文**
一起跳过，只在剩下的文字里替换。`[^>]*` 就够用，是因为属性值里的尖括号早就 `esc` 过了，
一条标签里不会出现裸的 `>`。

**测试也跟着修了**：原来那条"代码块里的页码不能动"的断言，是**手写**一个 `<div class="mdcode">`
喂进去的——真渲染器从来不产出这个形状，所以它一直是绿的，等于没测。现在测试真的 `eval`
一份 `util.js`，用 `renderMarkdown` 拼出来的 markup 来喂；另外补了一条"存在按钮属性里的
回复一个字都不能动"。**自己手搓一份看起来像的输入，是这类测试最容易自欺的地方。**

### 重排：为什么要两段

**向量检索只排个大概。** 它是"双塔"——问题和每一个块**各自**编码成向量，两个编码器
从没见过对方，靠余弦比远近。这样才快得起来（几万块也能一次扫完），代价就是排序粗：
真正能回答问题的那个块，经常躺在第 12、第 15 位，而我们只取前 6，于是它根本没送到模型手上。

**重排是"交叉编码"。** 把问题和**一条候选**拼成一整串一起送进模型，模型直接吐一个相关性分。
慢得多（每条候选一次前向），但排序质量高一个档次。所以标准做法就是两段：

```
向量检索先捞 30 条候选  →  reranker 逐条打分  →  返回分数最高的 6 条
```

**怎么开**：`ZHIYAN_RERANK_PROVIDER` + `ZHIYAN_RERANK_MODEL` 两个都填上才算开，
和 embedding 那条一模一样的规矩。**两个都空 = 不加这一层，检索顺序和以前一字不差**
（向量检索要多少条就给多少条，`RECALL` 这个数根本不会被问到）。

**本机怎么起 reranker**：ollama **没有** rerank 接口（`/api/rerank` 是 404），
所以不能挂在 ollama 上。用 llama.cpp 的 `llama-server`，它加一个开关就注册
`/rerank`、`/v1/rerank` 三个路由：

```
llama-server.exe -m bge-reranker-v2-m3-Q8_0.gguf --reranking ^
  --host 127.0.0.1 --port 8082 -c 8192 --no-slots
```

然后在模型页加一个自定义服务商 `local_rerank`（kind 选 `openai`，base_url 指
`http://127.0.0.1:8082/v1`），`.env` 里填上面那两个键。**这个服务是用户自己起的常驻服务，
不是项目依赖**——和 ollama 一样。

**不设分数阈值**：重排完照样取前 6，一条都不筛。这和向量检索的规矩是同一条——
分数是给模型看的材料，不是过滤条件。

**为什么每条候选只送前 512 字**（`DOC_CHARS`）：交叉编码器一次只处理
`问题 + 这一条候选`，而这个序列得塞进服务端的物理批大小（`n_ubatch`，默认 512 个 token）。
实测：600 个汉字约 478 token，能过；800 个汉字约 633 token，直接回 HTTP 500
`input is too large to process`。所以送整个块（最长 1600 字）会让**每一次**请求都失败——
那看起来像"重排坏了"，其实是"送太长了"。512 字约 410 token，留了余量给长问题，
而且判断"这段跟问题相不相关"本来也不需要读完整个块。

**重排挂了，检索不能跟着挂。** 服务没起、模型没拉、端点报 500——这些都退回**向量顺序**，
而不是让 `semantic_search` 整个不可用。但**不许静默**：失败原因打到 stderr，
每条命中带一个 `reranked` 标记，工具那边据此换标签——

- `reranked` 为真 ⇒ 打 `（相关度 -3.65）`，这是交叉编码器打的分；
- `reranked` 为假 ⇒ 打 `（相似度 0.48）`，这是余弦。

**两个尺度不能穿同一件衣服。** 余弦永远在 0~1，看着就像"像不像"；交叉编码器的分
是**有正有负**的（相关就是正、不相关就是负）。所以酸面包那种查不到的查询，
返回的会是一排负分——实测模型看到这排负数，正确地说了"文档库里没有相关内容"，
还把 `semantic_search` 返回的那几段点成了"假命中"。它把分数当材料读，没当结论照抄。

**按端点报的 `index` 归位，不信列表顺序**：标准响应不保证 `results` 按你发出去的顺序排
（llama.cpp 自己的测试都要遍历着找最大最小值）。照着列表位置填分，会把分数安到**别的**
文本头上，而且错得很像对的。

### 模型怎么知道该去查

`[application context]` 里那份文档**是截断的**：bridge 在 `publish()` 里截到 24,000 字。
以前**没有任何一行说这件事**，于是模型读完 24,000 字、正文停了，它只能认为文档就这么长——
REACT 那篇 11 万字，它会拿前 22% 当全文回答，而且理直气壮。

现在 `ApplicationState.content_chars` 记下**截断前**的真实长度，`render()` 在两者不等时
追一行尾注说明"上面只是开头多少字、全文多少字"。

**尾注只陈述事实，不写"用 open_document 去读"**——bridge 是应用无关的（换成别的
应用就未必有 `open_document`），工具名写错了比不写更糟。**"下一步该调哪个工具"
属于 Agent 的 system prompt，"我把你看到的截了多少"属于事实**，这条线不混。

### 这一轮的取舍

- **不引入向量库也不引入 numpy**：几千块、暴力余弦，纯 Python 一次查询零点几秒。
  哪天真跑不动了，就是该重新考虑的那天——这句话写下来了。
- 扫描件/纯图片 PDF 没有文字层 ⇒ 索引出来是空的，**明说**而不是静默成功。
- 索引失败**不会自动重试**（对一个坏端点每秒重试不如等着被叫）；文档库那行的「索引失败」
  徽章**可以点**，点了重新排队。这是唯一的回头路。
- 页内高亮没做。

### research 的两个材料来源

`research` agent 现在同时拿着**笔记工具**和**文档库工具**（`list_documents` /
`search_documents` / `semantic_search` / `open_document` / `fetch_document`），
提示词也改成从"你有什么"开始，而不是假设自己知道内容。它和 Reader 的区别就在开头：
**没有人递给 research 一份文档**，所以它得先看库里有什么、再自己去读；Reader 是一上来
就有当前打开的那篇。

工具一个都没新写——`zhiyan/tools/documents.py` 里早就有，只是没进 research 的白名单。

## 工具结果：模型收到的那一份必须是文本

`Tool.fn` 声明 `-> str`，但**不是每个工具都守约**：`list_notes` / `search_notes` /
`list_folders` / `get_linked_notes` 返回 Python 列表，`get_note` 返回 dict。

`zhiyan/core/loop.py` 原来把工具返回值**原样**塞进 `tool_result`，只在写记录时用
`as_text()` 转一遍。于是一个返回列表的工具跑完，下一轮请求里就出现这样的消息：

```json
{"role": "tool", "tool_call_id": "...", "content": [{"id": 3, "title": "训练日志"}]}
```

两条线都不接受这个形状：DeepSeek 回 `422 ... missing field 'type'`（它要的是字符串，
或者每块都带 `type` 的内容块数组），Anthropic 同样按非法内容块拒掉。

**它的表现很误导人**：出错的不是调用它的那一轮，而是**同一段对话的下一轮** —— 那一轮
才把历史里的工具结果一起发出去。现场就是这样：research 答对了一个只有文档库里才有的
问题，紧接着问第二个问题，回来的是一整段 422 错误。

现在 `as_text()` 在**唯一的那个位置**（拼消息之前）转一次，记录和模型看到的是同一份
文本；字符串输入逐字节不变。

教训：**"测试绿了"和"形状对"是两件事。** 这一条没有单测能发现 —— `ScriptedClient`
什么都收、什么都不校验，只有真服务商才会说"你这个 JSON 我不认"。现在
`evals/deterministic/test_tool_result_shape.py` 钉住了它（把 `loop.py` 那行改回去，
恰好红两条）。

## 这一轮删掉了整个 Coding

Coding Workspace 不要了，所以是**连根删**，不是把它藏进菜单：`coding` 这个 Agent Profile、
侧栏入口、`#coding` 那一页的 js 与 css、`applications/coding_workspace.py`（项目/文件/改动的
领域逻辑）、`applications/coding_verify.py`（改完自动跑验收）、`applications/coding_runs.py`
（每次改动的收据）、`tools/coding.py`（知研 自己那套读写文件的工具），以及
`core/runtime.py` 里"改完验收、没过再修一轮"那个重试循环，全部删掉。

**`delegate_task` 跟着一起走了，这一条最反直觉**：它看着像"另一条线"（把编程活交给本机装的
pi / claude / codex CLI 去干），但它的开关就是 Coding 页上那几个复选框，配置读的是那个模块
里的 `coding.json`。Coding 一走，它既没有入口能打开、也没有地方读配置，留着就是一调用就
`ImportError`。`tools/workspace.py`（给委派任务准备的落地目录）和 `tools/_env.py`（给
子进程过滤环境变量）是同一套东西的两半，一起删。

**留下的**：磁盘上的旧数据（`coding.json`、`coding_runs/`、`chat_log` 里
`agent_id='coding'` 的行）不迁移、不删除。它们不会再被任何界面读到，但删掉是不可逆的，
没有理由顺手做。

### 两个值得记下来的点

1. **"跑一遍测试"比"读一遍代码"更能发现依赖。** 删完第一遍，全量测试冒出来 17 条红，
   其中 10 条来自 `test_delegate.py`/`test_delegate_env.py` —— 这个依赖关系读代码时看
   不出来（`import` 写在函数体里面，是惰性的），只有真跑才炸。删功能时先在干净树上跑一次
   全量测试，比 grep 一遍 import 更可靠。
2. **`#agent/<已经不存在的 id>` 这条老路本来就有一个小毛病，和删 Coding 无关。**
   如果 localStorage 里存的就是那个已经不存在的 id（`zhiyan_active_agent`），
   `chat.js` 的 `recoverUnknownAgent()` 会把人送回 General，这条路是好的。
   但如果 localStorage 里存的是一个**还活着**的 agent，而网址里是
   `#agent/不存在的id`，那么 `selectAgent()` 会**静默拒绝**（它只接受 `D.agents` 里有的 id），
   于是网址和页面标题对不上，页面显示的是上一个 agent 的会话。拿一个从来不存在的 id
   （`#agent/zzz-nope`）试，现象一模一样 —— 所以这是老问题，不是这轮弄出来的，**这轮没动它**。

## 学习应用（考试备考 / 语言学习）

学习功能有两条轨道，共用一套数据与复习内核。改之前先分清这两件事：

| | 考试备考 `exam` | 语言学习 `language` |
| --- | --- | --- |
| 计划 | 到考试日的单元覆盖，预留复习缓冲 | 每日配比：新词 / 复习 / 听力 / 口语 / 阅读 / 写作 |
| 卡片 | `qa` / `cloze` | `vocab` / `dictation` / `translation` / `grammar` / `cloze` |
| 度量 | 覆盖率、单元完成度、正确率 | 活跃词、成熟词、目标词量覆盖、练习分钟 |
| Agent | Exam Coach | Language Tutor |

### 模块

| 位置 | 职责 |
| --- | --- |
| `zhiyan/applications/study.py` | 懒建 `study_*` 表；项目、资料、单元、计划、卡片、复习、音频、会话 |
| `zhiyan/ops/study_cards.py` | 一次模型调用生成候选卡；原文引文校验；去重 |
| `zhiyan/ops/study_language.py` | 词表解析/导入、写作批改、听写反馈 |
| `zhiyan/tools/study.py` | 学习工具；每个项目级工具都按 `kind` 校验轨道 |
| `zhiyan/agents/catalog.py` | `exam` / `language` 两个 AgentProfile |
| `zhiyan/ops/web/data.py` | `study_info` 快照、`study_action`、音频上传 |
| `zhiyan/ops/web/server.py` | `/api/study`、音频 GET/POST |
| `zhiyan/ops/static/js/study.js` | `VIEWS.exam` / `VIEWS.language` 和复习播放器 |

### 数据形状

- `study_projects`：`kind`、目标、考试日、每日分钟、每日新卡上限、`settings_json`。
- `study_units`：`doc_id` + `ordinal` + `start_offset/end_offset` + `page_start/page_end`。
  **偏移是对 `documents.content` 的精确区间**，不是估算。
- `study_plans`：版本化；重排 = 旧版 `archived`、插入 `version+1`；`items_json` 里考试是
  `unit_ids`，语言是每日配比的 `target`。
- `study_cards`：候选/激活/暂停；`front/back/extra_json`；`source_doc_id/source_offset/source_quote`。
- `study_reviews`：只追加。`review_date` 是本地日历日；`prev_reps` 用来计算“今天已经引入几张新卡”。
- `study_audio`：文件在 `.zhiyan/study/audio/<uuid><suffix>`，文件名永不用用户输入。
- `study_sessions`：听力/口语/写作/阅读的练习分钟，不参与 SRS。

### 单元提取

- Markdown：复用 `zhiyan/rag/chunk.py` 的 `_sections()`，标题路径作为单元标题。
- PDF：`reader.parse_pages()` 取每页文本，重建 `documents.content` 并核对；一致才用页码，
  不一致退化为按段落并返回 warning。超长页/段落用 `_split_units` 再切。
- 短行大纲：每个非空行一个单元。
- 有卡片引用时，`extract_units()` 拒绝重建，避免 `source_doc_id` 悬空。

### 排期

- `build_exam_plan()`：`exam_date - start + 1` 天；预留最后约 20%（至少 1 天）为复习/缓冲；
  单元按顺序装进每天 `daily_minutes`；`coverage = scheduled / required`，写进计划 note。
- `build_language_plan()`：14 天滚动模板；有考试日时按剩余天数提高新词量，受
  `new_cards_per_day` 上限约束；覆盖率写进 note。
- `replan()`：保留今天的 items，从明天重建；考试项目的 `horizon_end` 仍是考试日。
- 计划日期和 `due` 都是**本地日历日**，不是 UTC 时间戳。

### 卡片生成

`ops/study_cards.generate_cards()` 是 Web 动作和 Agent 工具的唯一入口：

1. 取单元原文（上限 6000 字），调用摘要模型返回 JSON。
2. 每条必须有 `source_quote`；先精确查找，再折叠空白查找；找不到就拒收。
3. `study.add_card()` 再按项目 `kind` 校验 card_type，并写为 `candidate`。
4. 去重按 `front` 的归一化文本；听写卡按 `audio_id` 去重。

**不要把 `source_quote` 校验删掉。** 模型很容易编一句看起来像原文的话；一张带假出处的卡
比没有卡更糟，因为它看起来可以核对。

### 工具与 Agent

`zhiyan/tools/study.py` 的工具按项目 kind 隔离：

- 语言专属：`extract_vocab`、`import_vocab`、`create_dictation`、`language_progress`。
- 考试/语言共用：`study_overview`、`study_plan`、`list_units`、`read_unit`、
  `create_cards`、`list_cards`、`mark_unit`、`replan_study`。
- `mark_unit` 是写操作，提示词要求只在用户明确说“学完了”时调用；不要根据聊天内容推断。

### Web 与前端

- `/api/data` 的 `study_info` 只给摘要：项目、今日议程、到期/新卡数、资料、音频元信息、
  卡片计数、计划版本。**不下发卡片正文和 transcript。**
- 读动作（`plan`、`list_units`、`list_cards`、`list_audio`、`review_queue`、`read_unit`）
  返回 `result`；写动作返回 `data` 快照，页面直接替换 `D.study_info`。
- `study.js` 的复习播放器用 `studyPaintReview()` 直接写 `#view`。这是故意的：
  `main.js` 的轮询守卫在 `studyReviewing` 为真时跳过重建，普通 `render()` 也会被跳过。
- 动态生成的 `onclick` 必须保留字面量函数名，否则 `bootstrap.js` 的导出扫描看不到它。
  `studyMixAction()` 就是为此存在（不要改回对象间接调用）。

### 测试

- `test_study_store.py`：懒建表、单元偏移、PDF 页码、两种排期、SM-2、复习日志、音频路径。
- `test_study_cards.py`：伪造引文拒收、重复去重、轨道隔离、模型失败返回文本。
- `test_study_language.py`：三种词表格式、导入去重、写作批改、听写映射。
- `test_study_tools.py`：无数据提示、计划/单元读取、跨轨道拒绝、候选卡不自动确认。
- `test_study_web.py`：`/api/study` 动作、`/api/data` 紧凑性、音频上传。
- `test_study_frontend.py`：两个 VIEWS、今日/计划/词汇/听力、复习翻面与评分、poll 守卫。

真浏览器验证可以参照 `docs/demo/README.md` 的操作路径；种数据时用一个独立的
`ZHIYAN_HOME`，不要把演示数据写进真实 `.zhiyan/`。

## 验证

```bash
uv run ruff check zhiyan evals
uv run python -m pytest -q evals/deterministic
```

确定性测试使用 `ScriptedClient`，不能依赖网络、真实 API Key 或本机已有的 `.zhiyan` 数据。需要真实模型判断的测试位于 `evals/judge/`。

提交前再确认：

- README 中的命令、链接和截图能在干净 checkout 中使用。
- 没有提交 `.env`、API Key、OAuth Token、`.zhiyan/` 或 `traces/`。
- 修改没有顺手重构无关代码。
- lint 和确定性测试都通过。
