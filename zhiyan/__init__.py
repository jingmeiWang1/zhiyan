"""zhiyan-agent — a minimal, transparent, local-first Zhiyan.

The implementation lives under ``zhiyan.core``.  The aliases below are kept
in one place for callers written before the core layout was introduced; new
code should import ``zhiyan.core`` directly.
"""

import sys

from zhiyan.core import context as _context
from zhiyan.core import loop as _loop
from zhiyan.core import models as _models
from zhiyan.core import session as _session
from zhiyan.core import tools as _tools
from zhiyan.core.context import (
    micro_compact as _micro_compact,
)
from zhiyan.core.context import (
    snip_compact as _snip_compact,
)
from zhiyan.core.context import (
    state_summary as _state_summary,
)
from zhiyan.core.context import (
    tool_budget as _tool_budget,
)
from zhiyan.core.context import (
    tool_entries as _tool_entries,
)

# Legacy imports remain valid without keeping a second source tree.  This is a
# compatibility map, not another implementation: every name points at the
# canonical module in ``zhiyan.core``.
for _legacy, _canonical in {
    "zhiyan.loop": "zhiyan.core",
    "zhiyan.loop.agent": "zhiyan.core.loop",
    "zhiyan.loop.models": "zhiyan.core.models",
    "zhiyan.runtime": "zhiyan.core",
    "zhiyan.runtime.session": "zhiyan.core.session",
    "zhiyan.runtime.micro_compact": "zhiyan.core.context.micro_compact",
    "zhiyan.runtime.snip_compact": "zhiyan.core.context.snip_compact",
    "zhiyan.runtime.state_summary": "zhiyan.core.context.state_summary",
    "zhiyan.runtime.tool_budget": "zhiyan.core.context.tool_budget",
    "zhiyan.runtime.tool_entries": "zhiyan.core.context.tool_entries",
}.items():
    sys.modules.setdefault(_legacy, sys.modules[_canonical])

# Package-style imports such as ``from zhiyan.runtime import tool_entries``
# also need attributes on the aliased package object.
sys.modules["zhiyan.loop"].agent = _loop
sys.modules["zhiyan.loop"].models = _models
sys.modules["zhiyan.runtime"].session = _session
for _name, _module in {
    "micro_compact": _micro_compact,
    "snip_compact": _snip_compact,
    "state_summary": _state_summary,
    "tool_budget": _tool_budget,
    "tool_entries": _tool_entries,
}.items():
    sys.modules["zhiyan.runtime"].__dict__[_name] = _module

del _legacy, _canonical, _loop, _models, _session, _tools, _context, _name, _module
del _micro_compact, _snip_compact, _state_summary, _tool_budget, _tool_entries

__version__ = "0.1.0"
