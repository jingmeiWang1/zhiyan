"""The agent's tools. Flagship-task tools (calendar/notes/messages), memory
self-management (manage_memory/update_soul/create_skill), and opt-in adapters:
Apple ecosystem (ZHIYAN_APPLE_TOOLS=1) and MCP servers (.zhiyan/mcp.json)."""

from __future__ import annotations

import sqlite3

from zhiyan.config import Settings
from zhiyan.core.tools import ToolRegistry
from zhiyan.tools import (
    calendar,
    memory_admin,
    messages,
    notes,
    search,
    tool_results,
    webpage,
)


def build_registry(conn: sqlite3.Connection, settings: Settings,
                   memory=None) -> ToolRegistry:
    """Every tool this process knows how to run.

    Which of them one agent actually sees is AgentSpec.tools (applied in
    core/runtime.py), not this function — so nothing here is per-agent except
    the agent id stamped on what a tool writes.
    """
    registry = ToolRegistry()
    registry.register(
        calendar.make_tool(
            conn,
            settings.home,
            apple_calendar=settings.apple_calendar,
            google_calendar=settings.google_calendar,
            google_calendar_id=settings.google_calendar_id,
        )
    )
    # Read side: "what's on my calendar?" — one tool across every connected
    # source (Google when signed in, plus zhiyan's own), so the model never has
    # to guess which calendar the user meant.
    registry.register(calendar.make_list_tool(conn, settings.home))
    registry.register(notes.make_tool(conn))
    registry.register(messages.make_tool(settings.home))
    # Web search — pairs with create_event for the multi-tool loop demo
    # ("find the World Cup games left and add them to my calendar").
    registry.register(search.make_tool())
    # Search tells you WHICH pages are worth reading; this reads one. Registered
    # next to search because it is the second half of the same job.
    registry.register(webpage.make_tool())
    # The way back to a long tool result that was compressed on its way into
    # history (runtime/tool_budget.py). Registered unconditionally: whether a
    # turn needs it depends on how big that turn's tool output was, which is not
    # knowable here — and a missing pointer target is worse than an unused tool.
    registry.register(tool_results.make_tool(settings.home))

    # Memory self-management — the agent can correct/forget memory, learn rules,
    # and author its own skills (feels like a personal agent, not a black box).
    if memory is not None:
        registry.register(memory_admin.make_search_memory_tool(memory))
        registry.register(memory_admin.make_manage_memory_tool(memory))
        registry.register(memory_admin.make_update_soul_tool(settings))
        # Procedural memory, both halves: `skill` lists/loads (the model pulls),
        # `create_skill` authors (the model writes). Registered here rather than
        # injected into the prompt, so an unused skill costs nothing.
        registry.register(memory_admin.make_skill_tool(memory))
        registry.register(memory_admin.make_create_skill_tool(settings, memory))

    # Experimental tools — off by default; opt in with ZHIYAN_EXPERIMENTAL=1.
    # Everything here is still a skeleton that reports "coming soon".
    #
    # Trust settings.experimental alone. load_settings() already initializes it
    # from ZHIYAN_EXPERIMENTAL; re-reading the environment here would override an
    # explicit False supplied by a caller.
    if getattr(settings, "experimental", False):
        from zhiyan.tools import experimental

        for t in experimental.make_tools(settings):
            registry.register(t)

    # Apple ecosystem readers/writers (opt-in; first use triggers macOS prompts).
    if settings.apple_tools:
        from zhiyan.tools import apple

        for t in apple.make_tools():
            registry.register(t)

    # Read-only GitHub via the gh CLI (opt-in; uses gh's own auth, no token here).
    if getattr(settings, "gh_tool", False):
        from zhiyan.tools import github

        registry.register(github.make_tool(default_repo=getattr(settings, "gh_repo", "")))

    # MCP servers (opt-in via .zhiyan/mcp.json).
    mcp_config = settings.home / "mcp.json"
    if mcp_config.exists():
        try:
            from zhiyan.tools.mcp_client import MCPBridge

            bridge = MCPBridge(mcp_config)
            for t in bridge.start():
                registry.register(t)
            registry.mcp_bridge = bridge  # so Zhiyan.close() can stop the servers
        except ImportError:
            print("mcp.json found but the 'mcp' package is missing — pip install 'zhiyan-agent[mcp]'")

    # Reader tools — read local documents, get selection, add notes, highlight text.
    # Always available for the default agent.
    from zhiyan.tools.reader import make_reader_tools
    for tool in make_reader_tools().values():
        registry.register(tool)

    # Document-library tools — list/search/read/import the documents you added
    # to the Reader. Workspace-level, not agent-scoped (see library.py), so
    # unlike the knowledge tools they take no agent id.
    from zhiyan.tools.documents import make_document_tools
    for tool in make_document_tools(conn, settings.home,
                                    settings.tool_result_budget, settings).values():
        registry.register(tool)

    # Study tools — 考试备考与语言学习。同样是工作区级的：项目、单元、卡片
    # 不属于某个 Agent 的私有上下文。哪一类项目能做什么，由工具内部按
    # project.kind 再校验一次，不依赖 Agent 的提示词自觉。
    from zhiyan.tools.study import make_study_tools
    for tool in make_study_tools(conn, settings.home, settings).values():
        registry.register(tool)

    # Knowledge base tools — Sapphire-style notes with [[wiki-links]]. Like
    # memory, the notes are one shared pool: the agent id here only stamps what
    # create_note writes, so the dashboard can say which Agent captured a note.
    from zhiyan.tools.knowledge import make_knowledge_tools
    agent_id = getattr(memory, "agent_id", "default")
    for tool in make_knowledge_tools(conn, agent_id).values():
        registry.register(tool)

    return registry
