"""Moved to zhiyan.core.tools.

This alias keeps old imports AND monkeypatches working through Phase 1 of the
platform refactor: after the line below, `import zhiyan.tools.registry` and
`import zhiyan.core.tools` are the SAME module object, so a test that patches
an attribute on one is patching the other.
"""

import sys

from zhiyan.core import tools as _target

sys.modules[__name__] = _target
