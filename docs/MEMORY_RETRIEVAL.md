# 记忆检索流程

KnowMe 每轮对话先调用低成本门控模型。门控模型只负责给出一个初步建议：

- `retrieve`：这轮是否可能需要长期记忆；
- `query`：建议使用的检索词；
- `reason`：门控模型的简短判断原因。

程序会按 `query` 搜索事实记忆和情景记忆，然后把 `retrieve`、`query`、`reason`
以及初步命中结果一起放进主模型当前轮的上下文。上下文会明确说明这只是初步判断，
主模型需要自己核对它是否和用户问题匹配。

如果主模型发现门控判断不对、检索词不准确或结果不完整，可以调用只读工具
`search_memory`，用新的检索词重新查询事实和情景记忆。这个工具不会修改记忆。
需要更正、删除或合并记忆时，才使用 `manage_memory`。

## Per-session consolidation

Consolidation is counted independently for each `session_id`. The default threshold is six
exchanges per session. A summarizer reads only that session's unconsolidated messages, so
separate conversations are not mixed. The Web memory page shows pending progress per session
and the recent consolidation history.
