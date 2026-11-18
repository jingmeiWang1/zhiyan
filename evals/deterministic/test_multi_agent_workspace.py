"""MVP guarantees, and where the line between Agents sits.

Agents share MEMORY — facts, episodes and notes are one pool, so any Agent
recalls all of it and any Agent may correct it (the rows keep an agent_id, but
only as a "who wrote this down" label). They do NOT share CONVERSATIONS: a
session belongs to the Agent you had it with. Both halves are pinned here."""

from knowme.agents import get_profile, list_profiles
from knowme.db import connect
from knowme.memory.semantic.store import SqliteFactStore
from knowme.ops.browser_agent import resume_or_new_session
from knowme.ops.web import _thread_history, session_list


def test_builtin_profiles_are_small_explicit_agent_specs():
    profiles = list_profiles()
    assert [profile.id for profile in profiles] == [
        "default", "research", "reader", "health"
    ]
    assert all(profile.spec.name == profile.id for profile in profiles)
    assert get_profile("default").spec.tools is None
    # Research carries the reading/note toolset, so it can work from a document
    assert "get_document" in get_profile("research").spec.tools
    # ...and the WHOLE document library, not just the note side. This is what
    # makes it a research agent rather than a note-taker: nobody hands it a
    # document up front, so it has to be able to find one and go read it.
    research_tools = get_profile("research").spec.tools
    assert {"list_documents", "search_documents", "semantic_search",
            "open_document", "fetch_document"} <= research_tools
    # the reader agent lives in the reading pane and works from the document
    # library; it deliberately has no search_web (Research is next door for that)
    reader_tools = get_profile("reader").spec.tools
    assert {"open_document", "search_documents", "list_documents"} <= reader_tools
    assert "search_web" not in reader_tools
    # The health agent reads and writes the six health tables (including the
    # food library it needs to convert "150g of chicken" into a number), and it
    # keeps search_web/read_webpage because "give me a plan with evidence" means
    # looking things up rather than reciting them.
    health_tools = get_profile("health").spec.tools
    assert {"health_overview", "query_metrics", "query_workouts", "query_meals",
            "log_metric", "log_workout", "log_meal", "search_food", "set_target",
            "save_plan", "list_plans"} <= health_tools
    assert {"search_web", "read_webpage"} <= health_tools
    # ...but it does not read documents — that is the reader's and research's job.
    assert "open_document" not in health_tools


def test_sessions_and_history_are_isolated_by_agent(tmp_path):
    conn = connect(tmp_path)
    conn.executemany(
        "INSERT INTO chat_log(role, content, session_id, source, agent_id) "
        "VALUES (?,?,?,?,?)",
        [
            ("user", "general question", "same-id", "dashboard", "default"),
            ("assistant", "general answer", "same-id", "dashboard", "default"),
            ("user", "research question", "same-id", "dashboard", "research"),
            ("assistant", "research answer", "same-id", "dashboard", "research"),
        ],
    )
    conn.commit()

    assert session_list(conn, "default")[0]["title"] == "general question"
    assert session_list(conn, "research")[0]["title"] == "research question"
    assert [row["content"] for row in _thread_history(conn, "same-id", "research")] == [
        "research question", "research answer"
    ]
    assert resume_or_new_session(conn, "research") == "same-id"


def test_any_agent_can_correct_a_fact_another_agent_recorded(tmp_path):
    """The inverse of what this test used to pin, and the reason the user asked
    for it: the memory is theirs, not the General agent's, so telling the
    Research agent "that's wrong" has to actually fix it."""
    conn = connect(tmp_path)
    general = SqliteFactStore(conn, agent_id="default")
    research = SqliteFactStore(conn, agent_id="research")
    general.add("project", "keep this")
    fact_id = general.list()[0]["id"]

    assert research.update(fact_id, "corrected by research") is True
    assert general.list()[0]["content"] == "corrected by research"
    # ...and one Agent forgetting something removes it for everyone.
    assert research.delete(fact_id) is True
    assert general.list() == []


def test_a_fact_written_by_one_agent_is_found_by_another(tmp_path):
    """Reads too, not just writes: the point of sharing is that the agent you
    happen to be talking to knows what you already told a different one."""
    conn = connect(tmp_path)
    research = SqliteFactStore(conn, agent_id="research")
    research.add("user", "lives in Dalian")

    assert SqliteFactStore(conn, agent_id="learning").search("Dalian") == [
        "[user] lives in Dalian"
    ]
