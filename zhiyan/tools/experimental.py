"""Roadmap tools — the whiteboard boxes beyond the flagship task.

These are SKELETONS on purpose: each shows the *shape* of a capability and
returns an honest "coming soon" (terminal/browser tools need a real sandbox +
safety surface first). Everything here is OFF by default; set
`ZHIYAN_EXPERIMENTAL=1` to register these tools.

`delegate_task` (the Sub-Agents box, which handed coding jobs to local CLIs)
used to live here and be the one ALIVE tool. It was removed together with the
Coding Workspace: its on/off switches were that page's checkboxes and its
configuration file was that module's `coding.json`, so once the workspace was
gone there was no way left to configure or reach it.
"""

from __future__ import annotations

from zhiyan.config import Settings
from zhiyan.core.tools import Tool

# Still-skeleton boxes: name → what it will do, and its box on the whiteboard.
PLANNED = [
    {"name": "run_command", "box": "Terminal tool",
     "description": "Run a shell command in a sandbox and read the output — Hermes's 'Terminal' "
                    "tool. Needs a real sandbox + safety surface first."},
    {"name": "browse_web", "box": "Browser tool",
     "description": "Open a page and read/click it — Hermes's 'Browser' tool. (search_web already "
                    "covers read-only web lookups.)"},
    {"name": "schedule_task", "box": "Cron Job",
     "description": "Let the agent schedule its own recurring runs. Today `make brief` + a system "
                    "cron line already does scheduled runs; this would move it in-app."},
]


def _stub(name: str, description: str, box: str) -> Tool:
    def fn(**kwargs) -> str:
        return (f"'{name}' maps to the '{box}' box on the architecture chart and isn't wired "
                f"in yet — it's on the roadmap (coming soon). Tell the user honestly.")

    return Tool(name=name, description=f"[coming soon] {description}",
                input_schema={"type": "object", "properties": {}}, fn=fn)


def make_tools(settings: Settings) -> list[Tool]:
    """Experimental tools, registered only when ZHIYAN_EXPERIMENTAL=1.

    `settings` is unused today — every box here is a skeleton — but the factory
    keeps it because the callers (build_registry, ops/web/data.py) pass the live
    Settings and the next wired-up box will want it.
    """
    return [_stub(p["name"], p["description"], p["box"]) for p in PLANNED]
