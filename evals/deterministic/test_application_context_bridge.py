"""The Application Context Bridge is bounded, isolated and easy to inspect."""

from zhiyan.applications import ApplicationContextBridge


def test_reader_snapshot_renders_for_the_matching_agent_and_session():
    bridge = ApplicationContextBridge()
    bridge.publish(
        agent_id="learning",
        session_id="s-1",
        application="reader",
        resource="essay.md",
        content="The whole essay",
        selection="a useful sentence",
        metadata={"page": 3},
    )

    rendered = bridge.render("learning", "s-1")
    assert "Application: reader" in rendered
    assert "Current resource: essay.md" in rendered
    assert "The whole essay" in rendered
    assert "Current selection:\na useful sentence" in rendered
    assert "page=3" in rendered
    assert bridge.render("research", "s-1") == ""
    assert bridge.render("learning", "s-2") == ""


def test_snapshot_is_bounded_before_it_reaches_the_model():
    bridge = ApplicationContextBridge(max_content_chars=5, max_selection_chars=3)
    state = bridge.publish(
        agent_id="default",
        session_id="default",
        application="reader",
        content="abcdefgh",
        selection="12345",
    )

    assert state.content == "abcde"
    assert state.selection == "123"
    rendered = bridge.render("default", "default")
    assert "fgh" not in rendered
    # …and the model is TOLD that what it got is not all of it. Without this line
    # a 110,000-character paper is answered from its first 22% and nothing in the
    # prompt says a word about the rest existing.
    assert state.content_chars == 8
    assert "8" in rendered and "5" in rendered, rendered


def test_the_truncation_note_appears_only_when_something_was_cut():
    """A document that fits must render byte-for-byte as it always did — the
    note is news, and there is no news to report."""
    bridge = ApplicationContextBridge(max_content_chars=50)
    bridge.publish(agent_id="a", session_id="s", application="reader",
                   resource="short.md", content="1234567890")
    assert bridge.render("a", "s") == (
        "[application context]\n"
        "Application: reader\n"
        "Current resource: short.md\n"
        "Document content:\n"
        "1234567890"
    )

    bridge.publish(agent_id="a", session_id="s", application="reader",
                   resource="long.md", content="x" * 80)
    cut = bridge.render("a", "s")
    assert cut.startswith("[application context]\nApplication: reader")
    assert "80" in cut and "50" in cut, cut
    # The note states the numbers and nothing else: which tool reads the rest
    # depends on the Application, so that instruction lives in the agent prompt.
    assert "open_document" not in cut


def test_clear_removes_only_one_conversations_transient_state():
    bridge = ApplicationContextBridge()
    for session_id in ("one", "two"):
        bridge.publish(
            agent_id="default",
            session_id=session_id,
            application="reader",
            selection=session_id,
        )

    assert bridge.clear("default", "one") is True
    assert bridge.render("default", "one") == ""
    assert "two" in bridge.render("default", "two")
    assert bridge.clear("default", "one") is False
