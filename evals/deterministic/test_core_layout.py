"""The core layout — two rules the platform refactor depends on.

1. Every OLD path is an alias of its NEW path: the same module object, not a
   copy. A copy would mean a monkeypatch on `zhiyan.runtime.session` misses
   the code `zhiyan.core.session` actually runs — and every existing test that
   patches through the old path would pass while testing nothing.

2. zhiyan.core imports nothing from zhiyan.ops / zhiyan.graph / zhiyan.gateway.
   Those are consumers of the core; a core that reaches back into its consumers
   cannot be reused by a second agent, a second app, or a second gateway.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

ALIASES = {
    "zhiyan.loop.agent": "zhiyan.core.loop",
    "zhiyan.loop.models": "zhiyan.core.models",
    "zhiyan.tools.registry": "zhiyan.core.tools",
    "zhiyan.runtime.session": "zhiyan.core.session",
    "zhiyan.runtime.tool_entries": "zhiyan.core.context.tool_entries",
    "zhiyan.runtime.tool_budget": "zhiyan.core.context.tool_budget",
    "zhiyan.runtime.snip_compact": "zhiyan.core.context.snip_compact",
    "zhiyan.runtime.micro_compact": "zhiyan.core.context.micro_compact",
    "zhiyan.runtime.state_summary": "zhiyan.core.context.state_summary",
}


@pytest.mark.parametrize(("old", "new"), sorted(ALIASES.items()))
def test_old_path_is_the_new_module(old: str, new: str) -> None:
    assert importlib.import_module(old) is importlib.import_module(new)


def test_core_does_not_import_its_consumers() -> None:
    # A fresh interpreter, so nothing another test imported can mask a leak.
    code = (
        "import sys, zhiyan.core.loop, zhiyan.core.models, zhiyan.core.tools, "
        "zhiyan.core.session, zhiyan.core.events, zhiyan.core.context.tool_budget, "
        "zhiyan.core.context.snip_compact, zhiyan.core.context.micro_compact, "
        "zhiyan.core.context.state_summary; "
        "print(sorted(m for m in sys.modules if m.startswith(('zhiyan.ops', "
        "'zhiyan.graph', 'zhiyan.gateway'))))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "[]", out.stdout
