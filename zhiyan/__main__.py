"""Entrypoints — installed as the `zhiyan` command (and `python -m zhiyan`):

  zhiyan                       the local Web client (default) → localhost:8888 on Windows
  zhiyan web                   the same door — `zhiyan` alone already does this
  zhiyan connections           list configured integrations and their health
  zhiyan brief                 morning briefing (calendar + mail + memory) — as a LOOP
  zhiyan gather                same job as a GRAPH: github, web, calendar and
                             memory fetched together, then one digest
  zhiyan skill install <url>   install a community skill
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "web":
        # Bare `zhiyan` and `zhiyan web` are the same door, so they share a branch.
        from zhiyan.ops.web import main as web_main

        web_main()
    elif args[0] == "connections":
        from zhiyan.integrations import cli_main

        sys.exit(cli_main())
    elif args[0] == "brief":
        from zhiyan.ops.brief import main as brief_main

        brief_main()
    elif args[0] == "gather":
        from zhiyan.ops.gather import main as gather_main

        gather_main()
    elif args[0] == "deep_research":
        from zhiyan.ops.deep_research import main as research_main

        # The topic is every remaining word, so quoting is optional but allowed:
        # python -m zhiyan deep_research 固态电池的产业化进度
        research_main(" ".join(args[1:]))
    elif args[0] == "skill" and len(args) >= 3 and args[1] == "install":
        from zhiyan.memory.procedural.installer import install

        install(args[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
