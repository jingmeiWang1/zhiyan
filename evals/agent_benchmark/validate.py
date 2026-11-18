"""Validate the 90-task dataset offline; execute gold calls, never an LLM."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

# Also works as: python evals/agent_benchmark/validate.py
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from knowme import db  # noqa: E402
from knowme.applications import health  # noqa: E402
from knowme.rag.chunk import chunks_for  # noqa: E402
from knowme.tools import calendar, knowledge, messages, notes  # noqa: E402
from knowme.tools.documents import make_document_tools  # noqa: E402
from knowme.tools.health import make_health_tools  # noqa: E402
from knowme.tools.tool_results import make_tool as make_result_tool  # noqa: E402

HERE = Path(__file__).resolve().parent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load(name: str):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def load_tasks(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (HERE / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def resolve(value, aliases: dict):
    if isinstance(value, str) and value.startswith("@notes."):
        require(value in aliases, f"Unknown alias: {value}")
        return aliases[value]
    if isinstance(value, dict):
        return {key: resolve(item, aliases) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, aliases) for item in value]
    return value


def check_type(value, schema: dict) -> None:
    """Only the primitive/array/enum types used by these tool schemas."""
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    tests = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "array": isinstance(value, list),
    }
    require(any(tests.get(kind, False) for kind in kinds), f"Bad type: {value!r}")
    if "enum" in schema:
        require(value in schema["enum"], f"Invalid enum: {value!r}")
    if isinstance(value, list):
        for item in value:
            check_type(item, schema["items"])


def check_state(conn, trial_dir: Path, check: dict, aliases: dict) -> None:
    check = resolve(check, aliases)
    if "file_glob" in check:
        files = list(trial_dir.glob(check["file_glob"]))
        require(len(files) == check["count"], f"Wrong file count: {check}")
        for file in files:
            text = file.read_text(encoding="utf-8")
            require(all(part in text for part in check["contains"]), f"Bad draft: {file}")
        return

    table = check["table"]
    columns = {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    require(columns, f"Unknown table: {table}")
    where = check["where"]
    require(set(where) <= columns, f"Unknown selector column: {where}")
    clause = " AND ".join(f'"{key}" = ?' for key in where) or "1=1"
    rows = conn.execute(
        f'SELECT * FROM "{table}" WHERE {clause}', tuple(where.values())
    ).fetchall()
    require(len(rows) == check["count"], f"Wrong row count: {check}")
    for key, value in check["values"].items():
        if key == "content_semantic_all":
            # Gold wording is exact here; a future agent grader accepts paraphrases.
            require({row["content"] for row in rows} == set(value), "Missing gold facts")
            continue
        for row in rows:
            if key == "items_text":
                actual = [item["text"] for item in json.loads(row["items_json"])]
            elif key == "content_semantic":
                actual = row["content"]
            else:
                require(key in columns, f"Unknown expected column: {key}")
                actual = row[key]
            require(actual == value, f"Bad gold state: {key}: {actual!r} != {value!r}")


def validate_loop(task: dict) -> None:
    """Prove reference calls work against today's tools in an isolated home."""
    with TemporaryDirectory(prefix="knowme-benchmark-") as temp:
        trial_dir = Path(temp)
        home = trial_dir / ".knowme"
        (home / "outbox").mkdir(parents=True)
        conn = db.connect(home)
        try:
            knowledge._ensure_table(conn)
            health._ensure_table(conn)
            tools = {
                **knowledge.make_knowledge_tools(conn),
                **make_health_tools(conn),
                "create_event": calendar.make_tool(conn, home),
                # No home: the calendar reader cannot consult a real Google account.
                "list_events": calendar.make_list_tool(conn),
                "save_note": notes.make_tool(conn),
                "send_message": messages.make_tool(home),
            }
            setup = task["setup"]
            require(set(setup["available_tools"]) <= tools.keys(), "Unknown tool")
            aliases = {}
            for seed in setup.get("notes", []):
                seed = dict(seed)
                alias = seed.pop("alias")
                row = knowledge.create_note(conn, **seed)
                key = f"@notes.{alias}.id"
                require(key not in aliases, f"Duplicate alias: {alias}")
                aliases[key] = row["id"]
            for seed in setup.get("events", []):
                tools["create_event"].fn(**seed)
            for seed in setup.get("foods", []):
                health.add_food(conn, **seed)

            expected = task["expected"]
            calls = expected["tool_calls"]
            policy = expected["call_policy"]
            require(policy["mode"] in {"required_subset", "no_write", "no_tools"}, "Bad mode")
            require(set(policy["allowed_extra"]) <= set(setup["available_tools"]), "Bad extras")
            require(policy["mode"] == "required_subset" or not calls, "Unexpected gold calls")
            for before, after in policy["order"]:
                require(0 <= before < after < len(calls), "Bad call ordering")
            for reference in calls:
                name = reference["name"]
                require(name in setup["available_tools"], f"Unavailable tool: {name}")
                tool = tools[name]
                args = resolve(reference["args"], aliases)
                schema = tool.input_schema
                require(set(schema.get("required", [])) <= args.keys(), f"Missing args: {name}")
                require(args.keys() <= schema["properties"].keys(), f"Unknown args: {name}")
                for key, value in args.items():
                    check_type(value, schema["properties"][key])
                require(reference["match"].keys() <= args.keys(), "Bad matching fields")
                require(set(reference["match"].values()) <= {"semantic", "exact"}, "Bad match mode")
                result = tool.fn(**args)
                require(not isinstance(result, dict) or "error" not in result, str(result))
            for check in expected["state_checks"]:
                check_state(conn, trial_dir, check, aliases)
        finally:
            conn.close()


def validate() -> None:
    histories = load("fixtures/context_histories.json")
    documents = load("fixtures/rag_documents.json")
    history_map = {item["id"]: item for item in histories}
    doc_map = {item["id"]: item for item in documents}
    require(len(history_map) == len(histories) == 6, "Duplicate/missing histories")
    require(len(doc_map) == len(documents) == 9, "Duplicate/missing documents")
    for history in histories:
        turns = history["messages"]
        require(len(turns) == 48, "Bad history length")
        require(len({turn["id"] for turn in turns}) == len(turns), "Duplicate turn")
        require(sum(len(turn["content"]) for turn in turns) >= 40000, "History too short")
        require(len(turns[23]["content"]) > 4000, "Tool result must exceed default budget")
        for index, turn in enumerate(turns):
            require(turn["role"] == ("user" if index % 2 == 0 else "assistant"), "Bad role")

    for doc in documents:
        require(doc["synthetic"] is True and doc["kind"] == "markdown", "Bad fixture type")
        require(len({s["id"] for s in doc["sections"]}) == len(doc["sections"]), "Duplicate section")
        for section in doc["sections"]:
            require(doc["content"][section["start"]:section["end"]] == section["text"],
                    f"Broken section offsets: {doc['id']}/{section['id']}")

    chunks = {doc["id"]: chunks_for(doc["kind"], [doc["content"]]) for doc in documents}
    require(all(chunks.values()), "Document cannot be indexed by the production chunker")

    suites = {name: load_tasks(name) for name in
              ("loop_tools", "context_engineering", "agentic_rag")}
    all_tasks = [task for suite in suites.values() for task in suite]
    require(len({task["id"] for task in all_tasks}) == 90, "Duplicate IDs / not 90 tasks")
    require(len({task["input"] for task in all_tasks}) == 90, "Duplicate inputs")
    for category, tasks in suites.items():
        require(len(tasks) == 30, f"{category}: expected 30 tasks")
        for task in tasks:
            require(task["category"] == category, "Wrong category")
            require(task["split"] in {"dev", "test"}, "Invalid split")
            require(task["input"].strip() and task["tags"], "Empty task")
            expected = task["expected"]
            require(bool(expected["answer_facts"]), "Missing answer criteria")
            if category == "loop_tools":
                validate_loop(task)
                continue
            require(type(expected["answerable"]) is bool, "Bad answerable label")
            require(bool(expected["evidence"]) == expected["answerable"], "Bad evidence label")
            if category == "context_engineering":
                history = history_map[task["setup"]["history_fixture"]]
                turns = {turn["id"]: turn for turn in history["messages"]}
                require(task["setup"]["available_tools"] == ["read_tool_result"], "Bad context tools")
                require(task["setup"]["memory_enabled"] is False, "Memory must be disabled")
                for item in expected["evidence"]:
                    require(item["quote"] in turns[item["turn_id"]]["content"], "Bad context quote")
            else:
                require(task["setup"]["corpus_fixture"] == "rag_documents.json", "Bad corpus")
                for item in expected["evidence"]:
                    doc = doc_map[item["doc_id"]]
                    section = next(s for s in doc["sections"] if s["id"] == item["section_id"])
                    require(section["start"] <= item["start"] < item["end"] <= section["end"],
                            "Evidence outside section")
                    require(doc["content"][item["start"]:item["end"]] == item["quote"],
                            f"Broken evidence: {task['id']}")
                    require(any(item["quote"] in chunk.text for chunk in chunks[doc["id"]]),
                            f"Evidence not indexable: {task['id']}")
        print(f"{category}: {len(tasks)} tasks OK")

    # Check non-loop tool names against actual factories as well.
    with TemporaryDirectory(prefix="knowme-benchmark-") as temp:
        conn = db.connect(Path(temp))
        try:
            names = make_document_tools(conn, Path(temp)).keys()
            for task in suites["agentic_rag"]:
                require(set(task["setup"]["available_tools"]) <= names, "Unknown RAG tool")
            require(make_result_tool(Path(temp)).name == "read_tool_result", "Unknown context tool")
        finally:
            conn.close()

    # No context scenario may appear in both development and held-out tasks.
    dev = {t["setup"]["history_fixture"] for t in suites["context_engineering"]
           if t["split"] == "dev"}
    test = {t["setup"]["history_fixture"] for t in suites["context_engineering"]
            if t["split"] == "test"}
    require(dev.isdisjoint(test), "Context fixture leaks between splits")
    print("Total: 90 tasks; gold tool calls, fixtures and evidence validated. No LLM calls.")


if __name__ == "__main__":
    validate()
