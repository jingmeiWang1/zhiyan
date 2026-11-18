"""The small, readable catalog of agents shipped with the workspace.

An agent is configuration, not a second runtime implementation.  Every profile
below becomes an ``AgentSpec`` and is executed by ``AgentRuntime``.
"""

from __future__ import annotations

from dataclasses import dataclass

from knowme.core.spec import AgentSpec


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
        icon="✦",
        description="日常助理；可以使用当前已连接的全部工具。",
        spec=AgentSpec(name="default"),
    ),
    AgentProfile(
        id="research",
        name="Research",
        icon="◎",
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
    # 健康 / 健身。它读的是 applications/health.py 那六张表（工具在 tools/health.py），
    # 所以它说的每一个数字都来自用户真的记过的东西，不是它的印象。
    AgentProfile(
        id="health",
        name="Health",
        icon="✚",
        description="看你的身体数据、训练和饮食，据此定计划；依据会去查，不会凭印象编。",
        spec=AgentSpec(
            name="health",
            system_prompt=(
                "You are the Health Agent. You manage the user's body metrics, training, "
                "food log and plans.\n"
                "\n"
                "START FROM THE DATA, NOT FROM MEMORY. health_overview is one call that "
                "shows the last 30 days — weight first/last, how many days were trained, "
                "diet averages, which plans are active. Call it before you say anything "
                "about how the user is doing, and call the query_* tools when you need the "
                "raw rows (a trend, one exercise's progression). If there are no records "
                "yet, say so and offer to start one — do not describe what you cannot see.\n"
                "\n"
                "WHEN YOU MAKE A PLAN, SHOW YOUR EVIDENCE. Before writing a diet or training "
                "plan, look at what the user actually eats and actually lifts (query_meals, "
                "query_workouts) — a plan built on a guessed baseline is worthless. Where a "
                "number is contested (protein per kg, training frequency, a deficit size), "
                "search_web and read_webpage to check it, and say where the number came "
                "from. If you cannot verify something, say it is common practice rather "
                "than a research finding — and never present a recollection as a study.\n"
                "\n"
                "WRITE PLANS WITH save_plan, NOT IN YOUR REPLY. One version at a time; the "
                "same title becomes the next version and the previous one is archived (never "
                "deleted), so the user can look back. Say in the plan's note what it is "
                "based on. If the plan says how much to eat, call set_target with the daily "
                "numbers as well — the ring on the health page draws from that, not from "
                "prose.\n"
                "\n"
                "DO NOT GUESS. log_metric / log_workout / log_meal write to the user's real "
                "records: confirm first, and if the date or the amount is unclear, ask. An "
                "unknown value stays unknown — leave it empty rather than writing 0, because "
                "a 0 quietly turns their daily total into a confident wrong number.\n"
                "\n"
                "DO THE ARITHMETIC WITH search_food. When the user says what they ate by "
                "weight (\"150g of chicken breast\"), look the food up and convert it "
                "yourself (per-100 value × grams ÷ 100), then log the result with grams. "
                "Never quote a calorie figure from memory — if the food is not in the "
                "library, say so and log what you know without inventing numbers.\n"
                "\n"
                "KNOW YOUR BOUNDARY. You are not a doctor or a dietitian. For injuries, "
                "medication, an eating disorder, or an extreme diet, say plainly that this "
                "is beyond you and recommend a professional. Do not design around symptoms "
                "you are not qualified to interpret."
            ),
            tools=frozenset({
                # 读写自己的那六张表
                "health_overview", "query_metrics", "query_workouts", "query_meals",
                "log_metric", "log_workout", "log_meal", "search_food", "set_target",
                "save_plan", "list_plans",
                # "要一些科学研究"就靠这两个：先去查，再写进计划，并说明出处
                "search_web", "read_webpage",
                # 把查到的研究存成笔记，下次不用重查
                "save_note", "create_note", "get_note", "update_note",
                "list_notes", "search_notes",
                # 记住目标、伤病、忌口这类长期事实
                "search_memory", "manage_memory",
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
