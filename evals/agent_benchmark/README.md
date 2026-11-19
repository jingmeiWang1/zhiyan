# Agent 评测集 v1：83 条任务

这是一份面向 知研 现有能力的中文合成评测集。先固定问题、前置数据和评分依据，后续再接真实 Agent 运行器。**当前只包含数据与离线校验，不包含模型成绩或自动评分运行器。** 原有 `evals/dataset.jsonl` 保持不变。

| 评测方向 | 数量 | 覆盖内容 | 目标指标 |
| --- | ---: | --- | --- |
| Agent Loop / Tools | 23 | 工具选择、参数、依赖顺序、最终数据、追问与不执行 | Success Rate、Tool Accuracy |
| Context Engineering | 30 | 早期信息、长工具输出、旧值修正、多轮计算、约束与未知信息 | Accuracy、Token Saving |
| Agentic RAG | 30 | 单段检索、跨章节/文档问答、版本冲突、引用、资料不足 | Recall@K、Faithfulness |
| 合计 | 83 | | |

## 文件与校验

- [loop_tools.jsonl](loop_tools.jsonl)：23 条工具任务。
- [context_engineering.jsonl](context_engineering.jsonl)：30 条长上下文任务。
- [agentic_rag.jsonl](agentic_rag.jsonl)：30 条文档问答任务。
- [fixtures/context_histories.json](fixtures/context_histories.json)：6 份固定对话，每份48条消息、约4.7万字符；每份对应5个问题。
- [fixtures/rag_documents.json](fixtures/rag_documents.json)：9 份虚构 Markdown 文档、38个章节，含相似项目和归档版本。
- [validate.py](validate.py)：离线校验脚本。

在仓库根目录运行：

```bash
uv run python evals/agent_benchmark/validate.py
uv run ruff check evals/agent_benchmark
```

已有虚拟环境时，也可以用 `python evals/agent_benchmark/validate.py`。脚本检查83条数量、唯一ID/问题、工具Schema与标准参数、前置数据引用、证据原文和字符位置、开发/测试划分。它还会在临时SQLite和目录中执行工具任务的标准调用、核对预期数据，并用项目真实切分器确认RAG证据可以进入索引。不会调用模型、读取API Key或连接外部日历。

**校验通过表示材料及标准调用可用，不表示 Agent 已通过评测。**

## 每条任务怎么看

公共字段：

| 字段 | 含义 |
| --- | --- |
| `id` / `category` | 稳定任务编号与评测方向 |
| `split` | `dev`供调试；`test`留到配置确定后再跑 |
| `input` | 给Agent的用户问题 |
| `setup` | 工具白名单、数据库种子或材料引用；由运行器准备 |
| `expected` | 评分依据，**不能传给被测Agent** |
| `tags` | 用于按能力分组看结果 |

工具任务的 `expected.tool_calls` 是最低必要调用及参考参数，不要求整段轨迹完全相同。参数默认按给定字段精确匹配；`match: {"content": "semantic"}`允许长期记忆内容同义改写。未列出的可选参数也必须符合任务要求，不能用额外参数填入猜测值。

`call_policy.allowed_extra`允许必要的只读定位操作，例如先搜索笔记获取ID。`order`只约束显式依赖；`no_write`禁止写入，`no_tools`禁止所有调用。不要因为存在工具就要求一定调用。

`state_checks`检查执行后的数据库行或本地文件。`where`为筛选字段，`count`为命中行数，`values`为预期值；`null`代表SQL NULL。`content_semantic`和`content_semantic_all`检查单条/多条事实的含义，`items_text`检查计划条目的顺序。离线脚本只验证参考答案的固定文字，真实回答需接受同义表达。`answer_facts`是最终回答需要满足的内容与边界，也允许同义表达。

笔记种子中的 `alias`是运行器标签。`@notes.main.id`等占位符要解析成创建后得到的真实ID，不能把标签当成工具参数或提前把ID透露给Agent。每题使用新环境、清空对话与记忆；前置笔记通过工具查找。

## Agent Loop / Tools 怎么评分

使用任务里列出的20个现有本地工具；外部集成关闭，`list_events`仅查询临时数据库，`send_message`只生成本地草稿。给运行器设置相同模型、系统提示和迭代预算；任务的全部工具均提供给模型，让它自行选择。

- **Success Rate** = 满足最终数据、回答与任务限制的任务数 / 30。创建日程要确实写入正确时间；追加笔记要保留原文；删除要只删目标；消息草稿不能宣称已经发送。只说“完成了”不算成功。
- **Tool Accuracy（本集采用任务级口径）** = 必要工具与参数正确、显式顺序正确、无不允许调用的任务数 / 30。遗漏必要调用也判失败。允许的额外只读操作不扣分；错误参数或无关写入判失败。无工具任务按其禁止调用规则评分。
- 不以模型复述成功替代数据库/文件检查。语义内容和追问/拒绝的文字可以人工核验，后续再加入经人工校准的Judge。

工具准确但任务没做完，或数据正确但错误宣称外部发送，应能在报告里分别定位原因。

## Context Engineering 怎么评分

6份对话都包含：早期任务信息、已作废的旧值、中间长工具输出、多轮数字输入、后段约束。每份5题分别检查实体、修正、计算、约束和未提供的信息；最后一类共6题。

对每个问题分别运行两次，使用同一原始历史、问题和模型：

1. **Full**：关闭上下文裁剪与摘要，提供完整历史。
2. **Compressed**：使用项目Context Policy，包括工具结果预算、旧工具输出指针和状态摘要；预算设到确实触发压缩。

用新会话装入原始 `role/content`消息，不能先经过历史加载时的截断。两组只允许 `read_tool_result`，关闭记忆门控/检索、后台记忆整理和应用上下文注入，避免从其他渠道补答案。历史里出现的 `search_notes`只是已经发生的工具记录，不是本轮可用工具。

压缩组保留工具结果归档目录，使 `read_tool_result`可以恢复原文；记录每次恢复调用和它消耗的token。这测的是项目完整压缩机制的效果。若要单独测摘要保留能力，可另跑两组都禁用恢复工具的实验，单独标注结果。

- **Accuracy**：每题正确事实数 / `answer_facts`条数，再对30题取平均。数字、单位、最新版本与否定约束都要正确；未提供信息要明确说未知。分别报告 Full、Compressed Accuracy 及差值。
- **Token Saving（首请求）** = `1 − 压缩组首请求input tokens总和 / 完整组首请求input tokens总和`。包含完整system、工具Schema、问题及历史，报告是否使用服务商usage或对应模型tokenizer；不能用字符数当token数。
- 另报**全流程输入token节省**，把摘要模型调用与恢复原文后的所有模型请求都计入压缩组。恢复后的上下文不能只算新增字符串。数值允许为负，不能隐藏压缩本身的成本。
- 同时记录压缩是否实际发生、摘要长度、恢复次数。完整组超出模型窗口时更换足够大窗口的模型或报告不可比较，不能悄悄截断。

## Agentic RAG 怎么评分

将全部9份文档的 `content`按Markdown导入**临时文档库**，为合成doc ID保存实际数据库ID的映射。用项目 `chunks_for`、真实embedding及可选reranker建立索引，等待索引完成后再开始。不能用测试用的假向量当真实检索成绩。

Agent只看到问题和文档工具，不提前注入文档全文、标准答案或证据标签。工具为 `list_documents`、`search_documents`、`semantic_search`和`open_document`。最终回答要求文档标题、章节及原文片段；此版材料为Markdown，**不测PDF页码定位**。

`expected.evidence`记录合成 `doc_id`、`section_id`、原文 `quote`和全文字符区间 `[start, end)`。同一章节可能标注多个关键句。证据使用固定原文，不依赖运行时会变化的chunk序号。

- **Recall@K**：先把金标准证据按 `(doc_id, section_id)`去重。某章节的所有标注片段都出现在前K个检索块中，才算该证据单元命中。每题 `命中的证据单元数 / 所需证据单元数`，再对25条可回答题取平均。跨章节问题必须召回多处依据，检索到文档标题不等于命中。
- 固定报告 **Recall@3、Recall@6**。当前 `semantic_search`默认返回6条；评测运行器必须记录真正返回的块、排名、doc ID和重排配置，不能把内部30条候选当作对Agent的top-6。
- 多轮Agent检索应分开报告：原始用户问题的一次检索Recall@K用于比较检索配置；Agent实际各次检索的top-K证据并集用于报告**最终证据覆盖率**。二者不能混写成一个Recall@K。
- **Faithfulness** = 有检索证据支持的回答事实数 / 回答中的事实总数。计算类结论允许从已召回数字推导。必须检查全部事实，包括额外添加的内容；不能只匹配标准答案关键词。由人工或校准后的Judge核对，并保留失败事实与证据。
- **引用准确性**单独核对：引用的标题/章节存在、原文片段真实且支持对应结论。文字有出处不等于引用正确；Faithfulness不直接代替引用正确率。
- 5条资料不足题不进入Recall@K分母，单独报告正确拒答率。没有事实的空回答不记为100% Faithfulness，记N/A；可回答题空答属于任务失败。
- Faithfulness高也可能漏答，仍需用 `answer_facts`核对答案完整性。证据不足、答案捏造和引用错误分别留在报告中。

## 真实模型实验

真实模型实验使用 [run.py](run.py)，结果记录在 [RESULTS_2026-10-09.md](RESULTS_2026-10-09.md)。原始结果保存在本机的 results/，不提交回答和轨迹。

Agent 自主检索同时记录四个检索指标：首次语义检索 Recall@3/@6、所有语义检索结果并集的累计 Recall@3/@6、只看语义结果的 semantic evidence coverage，以及把 search_documents/open_document 文本也算入后的最终 evidence coverage。固定管线的 Recall@K 仍指唯一一次 Dense 查询的前K结果；两者不要混成同一个数字。

## 划分、结果与简历

固定划分：工具6条dev / 24条test，上下文5 / 25，RAG5 / 25，总计16条dev、74条test。上下文按整份历史划分，避免同一历史同时进入两边；RAG两边共享资料库，划分的是问题。确定配置后不要再用test答案调提示词。

这是人工设计的合成起步集，旁支长对话有规则性，部分问题结构相似；它适合建立可复现的第一版评测，不代表真实用户分布或公开基准成绩。工具任务尚未覆盖执行失败后的恢复，RAG尚未覆盖扫描件与PDF页码；后续从真实失败样本补充。

每次真实运行至少保存：任务ID、split、模型/提示/代码版本、压缩/检索配置、完整工具轨迹、最终状态、回答、token usage和判分依据。先用dev检查流程，再冻结配置跑test；展示测试集指标时必须同时注明各方向的样本数，不能把74条说成90条。

目前可在简历写“构建覆盖Agent工具调用、上下文压缩和Agentic RAG的90条评测集，设计状态断言、信息保留与证据引用标注”。Success Rate、Token Saving等百分比需要真实运行后再填。

设计参考：[Anthropic：Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)，采用任务输入、轨迹和最终环境状态分开检查的做法。
