"""Show a unified diff between R4 bytecode and the reconstructed source.

Usage: python tools/disdiff.py module function [function ...]
       python tools/disdiff.py model markdown Message
"""

from __future__ import annotations

import difflib
import dis
import marshal
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_r4 import R4_PYC_DIR, SRC, flatten, load_pyc  # noqa: E402


def instructions(code: types.CodeType) -> list[str]:
    result = []
    for ins in dis.get_instructions(code):
        arg = "" if ins.arg is None else f" {ins.argrepr}"
        result.append(f"{ins.opname}{arg}")
    return result


def main() -> int:
    module, names = sys.argv[1], sys.argv[2:]
    binary = flatten(load_pyc(R4_PYC_DIR / f"{module}.pyc"))
    source = flatten(
        compile(
            (SRC / f"{module}.py").read_text(encoding="utf-8"),
            f"{module}.py",
            "exec",
            dont_inherit=True,
        )
    )
    for name in names:
        key = next((k for k in binary if k == name or k.endswith("." + name) or k.split(".")[-1] == name), None)
        if key is None:
            print(f"## {name}: not in R4")
            continue
        print(f"## {key}")
        left = instructions(binary[key])
        right = instructions(source[key]) if key in source else ["<missing>"]
        for line in difflib.unified_diff(left, right, fromfile="R4", tofile="source", lineterm="", n=2):
            print(line)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
