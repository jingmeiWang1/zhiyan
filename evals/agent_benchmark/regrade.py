"""Regrade saved answers; never rerun the evaluated Agent."""
import argparse
import json
from pathlib import Path

from evals.agent_benchmark.run import dataset, judge, load_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--errors-only", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    for suite in ("loop_tools", "context_engineering", "agentic_rag"):
        file = args.out / f"{suite}.jsonl"
        if not file.exists():
            continue
        tasks = {t["id"]: t for t in dataset.load_tasks(suite)}
        rows = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            if "answer" not in row or (args.errors_only and row["status"] == "ok"):
                continue
            observed = {}
            if suite == "loop_tools":
                observed = {"state": row["state"], "trace": row["tools"]}
            elif suite == "agentic_rag":
                if row["arm"] == "agent":
                    evidence = [event["output"] for event in row["tools"]]
                else:
                    hits = [h for event in row["retrieval"] if event["kind"] == "search"
                            for h in event["hits"]]
                    evidence = [f"《{h['title']}》\n{h['text']}" for h in hits]
                observed = {"evidence": evidence}
            graded = judge(settings, tasks[row["id"]], row["answer"], observed)
            row.setdefault("previous_judge_calls", []).extend(row.get("grade", {}).get("calls", []))
            row["grade"] = graded
            row["status"] = "ok"
            row.pop("error_type", None)
            row["success"] = graded["answer_success"] and not row["hit_limit"]
            if suite == "loop_tools":
                row["success"] &= not row["state_failures"] and row["policy_ok"]
            file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                            encoding="utf-8")
            print(suite, row["id"], row["arm"], row["success"], flush=True)


if __name__ == "__main__":
    main()
