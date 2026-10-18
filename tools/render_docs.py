"""Render docs/TOOLS.md from the server's own tool registry.

The tool reference is generated rather than typed, because a hand-written list of
25 tools and 120 arguments drifts from the code within one commit and then lies to
whoever reads it.

    python tools/render_docs.py           # writes docs/TOOLS.md
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mt5trader.server import Server          # noqa: E402
from mt5trader.config import Settings        # noqa: E402

HEADER = """# Tool reference

Generated from the server's own registry by `python tools/render_docs.py`. Do not
edit by hand: the descriptions and the argument list come from the code, which is
the point of generating it.

`read` tools only look at the terminal. `WRITE` tools change something on it, and
every one of them runs the risk kernel first or refuses to act on a position that
carries another magic number.
"""

FOOTER = """
## The two gates every write tool sits behind

1. `MT5_AUTONOMY` in the server's environment. `propose` (the default) makes the
   order tools refuse outright. `auto` additionally needs `confirm=true` and a
   `plan_id` whose fingerprint still matches.
2. The risk kernel. A `BLOCK` is final; no argument overrides it. Read the
   `failed_checks` array, change the plan, and check again.

`MT5_ALLOW_LIVE=1` is required for any real account, `MT5_ALLOW_FLATTEN=1` to
close positions this kernel does not own, and `MT5_ALLOW_KILL_RELEASE=1` to clear
the kill switch.
"""

TYPE_NAMES = {
    "string": "string", "number": "number", "integer": "int",
    "boolean": "bool", "array": "string[]",
}


def render():
    server = Server(settings=Settings(root=str(ROOT), config_path=None, environ={}))
    lines = [HEADER]
    reads = [t for t in server.tools.values() if not t.mutating]
    writes = [t for t in server.tools.values() if t.mutating]
    lines.append("\n%d tools: %d read, %d write.\n" % (len(server.tools), len(reads), len(writes)))

    for group, tools in (("Read", reads), ("Write", writes)):
        lines.append("\n## %s\n" % group)
        for tool in tools:
            lines.append("\n### `%s`\n" % tool.name)
            lines.append(tool.description.strip() + "\n")
            properties = (tool.input_schema or {}).get("properties") or {}
            required = set((tool.input_schema or {}).get("required") or [])
            if not properties:
                lines.append("\nNo arguments.\n")
                continue
            lines.append("\n| argument | type | required | meaning |")
            lines.append("\n| --- | --- | --- | --- |")
            for name, spec in properties.items():
                kind = spec.get("type")
                if kind == "array":
                    kind = TYPE_NAMES["array"]
                lines.append("\n| `%s` | %s | %s | %s |" % (
                    name, TYPE_NAMES.get(spec.get("type"), spec.get("type", "")),
                    "yes" if name in required else "",
                    (spec.get("description") or "").replace("|", "\\|")))
            lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    target = ROOT / "docs" / "TOOLS.md"
    target.write_text(render(), encoding="utf-8")
    print("wrote %s (%d bytes)" % (target, target.stat().st_size))
