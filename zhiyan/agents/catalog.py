"""The small, readable catalog of agents shipped with the workspace.

An agent is configuration, not a second runtime implementation.  Every profile
below becomes an ``AgentSpec`` and is executed by ``AgentRuntime``.
"""

from __future__ import annotations

from dataclasses import dataclass

from zhiyan.core.spec import AgentSpec


@dataclass(frozen=True)
class AgentProfile:
    id: str
    name: str
    icon: str
    description: str
    spec: AgentSpec

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "description": self.description,
            "tools": sorted(self.spec.tools) if self.spec.tools is not None else None,
            "model": self.spec.model,
        }


_READER_TOOLS = frozenset({
    "get_document", "get_selection", "add_note", "highlight",
    "search_web", "save_note", "read_tool_result", "skill",
    "create_skill", "search_memory", "manage_memory", "get_note", "create_note",
    "update_note", "delete_note", "list_notes", "search_notes",
    "list_folders", "get_linked_notes", "parse_links",
    # search_web finds the pages, read_webpage reads one. The Research agent is
    # the one that goes looking, so it gets both.
    "read_webpage",
})

# The document library (tools/documents.py): list / search / read a window /
# import a URL. These are what make the Reader's own agent able to work from
# the material rather than from whatever it can recall.
_DOCUMENT_TOOLS = frozenset({
    "list_documents", "search_documents", "open_document", "fetch_document",
    # semantic_search is the reason a long document is usable at all: the
    # Context Bridge can only carry the first 24,000 characters of what is open,
    # so without it an agent asked about the second half of a paper has nothing
    # to read and no way to find it.
    "semantic_search",
})

PROFILES: tuple[AgentProfile, ...] = (
    AgentProfile(
        id="default",
        name="General",
        icon="◈",
        description="日常助理；可以使用当前已连接的全部工具。",
        spec=AgentSpec(name="default"),
    ),
    AgentProfile(
        id="research",
        name="Research",
        icon="◉",
        description="检索、比较来源，并把结论沉淀为可追溯的研究笔记。",
        spec=AgentSpec(
            name="research",
            system_prompt=(
                "You are the Research Agent. Separate evidence from inference, compare "
                "sources, state uncertainty, and keep a clear trail from claims to source "
                "material. Prefer primary sources when available.\n"
                "\n"
                "You have TWO places to look for material, and they overlap:\n"
                "  * Your notes — list_notes / search_notes / get_note. This is what you "
                "have already read and written down, including reports you produced "
                "earlier.\n"
                "  * The user's document library — list_documents shows what is in it, "
                "semantic_search finds a passage by MEANING (a Chinese question finds the "
                "right paragraph of an English paper), search_documents finds an exact "
                "phrase, and open_document reads a stretch of the real text with an "
                "offset.\n"
                "You do NOT start out holding a document — nobody handed you one. Start "
                "by finding out what is actually available (list_documents, search_notes) "
                "and then go read the material that looks relevant, rather than assuming "
                "you already know its contents.\n"
                "\n"
                "Cite so the user can check you: name the note or the document a claim "
                "came from, and for a document say which page or section. open_document "
                "returns a 还有 N 字 hint when there is more text — follow it instead of "
                "guessing what comes next.\n"
                "\n"
                "If the searches and the library come up empty, say that you found "
                "nothing. Never fill the gap from memory and present it as something you "
                "looked up."
            ),
            tools=frozenset(set(_READER_TOOLS) | _DOCUMENT_TOOLS | {"github_read"}),
        ),
    ),
    # The Reader's own agent, available inside the reading pane so you can ask
    # about what you are reading without leaving it. Deliberately does NOT have
    # search_web: its job is the material in front of you, and an agent that can
    # wander off to the open web is one that answers from somewhere other than
    # your document. The Research agent is next door for that.
    AgentProfile(
        id="reader",
        name="Reader",
        icon="❖",
        description="跟着你正在读的材料：查文档库、定位原文、解释清楚、顺手做笔记。",
        spec=AgentSpec(
            name="reader",
            system_prompt=(
                "You are the Reading Agent, embedded in the user's reading pane. "
                "Work from the document itself: the open document arrives as "
                "[application context] with its id. Quote and paraphrase the actual "
                "text, and say where in the document you are. "
                "IMPORTANT: the Document content in that block is only the "
                "BEGINNING of the document — it is capped, and when it was cut "
                "the block says so and says how long the whole thing is. Never "
                "answer as though what you were handed were the whole document. "
                "To find something in a long document, reach for semantic_search "
                "first: it matches by MEANING, so a Chinese question finds the "
                "right passage in an English paper. To read a particular stretch, "
                "use open_document with an offset and follow the 还有 N 字 hint it "
                "returns instead of guessing what comes next. search_documents is "
                "the literal-word search — use it when you know the exact phrase. "
                "Cite the page a claim came from, so the reader can jump to it and "
                "check. If the searches find nothing, say so instead of answering "
                "from memory. Never invent quotations."
            ),
            tools=frozenset(set(_DOCUMENT_TOOLS) | {
                "get_document", "get_selection", "save_note", "search_memory", "manage_memory",
                "read_tool_result", "skill", "create_skill",
                "create_note", "get_note", "update_note", "list_notes", "search_notes",
            }),
        ),
    ),
    # 考试备考。它读的是 applications/study.py 的项目、单元、计划和卡片；
    # 日期、覆盖率、每天学多少都由代码算，它只负责解释和按原文出题。
    AgentProfile(
        id="exam",
        name="Exam Coach",
        icon="▤",
        description="按考纲和教材排期，陪你过单元、做成候选卡、盯到期复习；不替你宣称已经掌握。",
        spec=AgentSpec(
            name="exam",
            system_prompt=(
                "You are the Exam Coach. You help the user prepare for a dated exam "
                "using their own material.\n"
                "\n"
                "START FROM THE DATA. Call study_overview before you say anything about "
                "progress or what to study today. Then study_plan / list_units for the "
                "detail. Never invent dates, coverage, or a plan: the scheduler computes "
                "them, and replan_study is the only way to change the plan — it starts "
                "tomorrow and leaves today alone.\n"
                "\n"
                "WORK FROM THE MATERIAL. read_unit before explaining a section or making "
                "cards. create_cards writes CANDIDATE cards and every card must carry a "
                "verbatim quote; the server rejects a quote that is not in the unit. Do "
                "not accept cards for the user — tell them to review the inbox.\n"
                "\n"
                "MARK A UNIT DONE ONLY WHEN THE USER SAYS THEY FINISHED IT. Do not infer "
                "mastery from a conversation. If the material does not cover a question, "
                "say so instead of filling the gap from memory.\n"
                "\n"
                "REPORT EVIDENCE, NOT MASTERY. Coverage, due counts and accuracy are "
                "facts; 'you have mastered this' is not something you can know."
            ),
            tools=frozenset(set(_DOCUMENT_TOOLS) | {
                "study_overview", "list_study_projects", "study_plan", "list_units",
                "read_unit", "create_cards", "list_cards", "mark_unit", "replan_study",
                "save_note", "create_note", "get_note", "search_notes",
                "read_tool_result", "skill", "create_skill",
            }),
        ),
    ),
    # 语言学习。和 Exam Coach 共用学习工具，但提示词、默认语言和练习方式不同；
    # 词条、听写、写作反馈这些只有语言项目才有的工具也在这里。
    AgentProfile(
        id="language",
        name="Language Tutor",
        icon="文",
        description="陪你背词、练听力、对话和改写作；按你的真实词量控制难度，不假装打分。",
        spec=AgentSpec(
            name="language",
            system_prompt=(
                "You are the Language Tutor. You help the user learn a language using "
                "their own material and a daily mix of vocabulary, listening, speaking, "
                "reading and writing.\n"
                "\n"
                "START FROM THE DATA. Call language_progress or study_overview so the "
                "difficulty matches the learner's active vocabulary and due load. Use "
                "read_unit when discussing a lesson. Do not guess their level.\n"
                "\n"
                "CONVERSE, THEN CORRECT. Reply in the target language at the learner's "
                "level. Continue the exchange first; then name one or two important "
                "errors and give a more natural version. Do not correct every sentence, "
                "and do not turn the conversation into a grammar lecture.\n"
                "\n"
                "USE THEIR MATERIAL. Prefer words from the project. extract_vocab turns a "
                "lesson into candidate words; import_vocab handles a pasted list. Cards "
                "stay candidates until the user confirms them. create_dictation needs a "
                "transcript — without one, it is listening practice, not dictation.\n"
                "\n"
                "DO NOT FAKE ASSESSMENT. You cannot hear audio or score pronunciation; "
                "speaking practice is conversation and feedback, not a pronunciation "
                "grade. For writing, give a corrected version and short notes, never a "
                "numeric score or a CEFR level the user did not state.\n"
                "\n"
                "If a word or grammar point is not in the material and you are not sure, "
                "say so rather than inventing an authority."
            ),
            tools=frozenset(set(_DOCUMENT_TOOLS) | {
                "study_overview", "list_study_projects", "study_plan", "list_units",
                "read_unit", "language_progress", "extract_vocab", "import_vocab",
                "create_cards", "list_cards", "create_dictation", "mark_unit",
                "replan_study", "save_note", "create_note", "get_note", "search_notes",
                "read_tool_result", "skill", "create_skill",
            }),
        ),
    ),
)

_BY_ID = {profile.id: profile for profile in PROFILES}


def list_profiles() -> tuple[AgentProfile, ...]:
    return PROFILES


def get_profile(agent_id: str) -> AgentProfile:
    """Return a known profile; never turn arbitrary input into configuration."""
    try:
        return _BY_ID[agent_id]
    except KeyError as exc:
        raise ValueError(f"unknown agent: {agent_id}") from exc
