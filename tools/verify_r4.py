"""Compare the reconstructed source against the R4 executable's code objects.

Usage: python tools/verify_r4.py [module ...]

The R4 PYZ was extracted with pyinstxtractor; set R4_PYC_DIR to the
``.../PYZ.pyz_<id>_extracted/groupreader`` directory.
"""

from __future__ import annotations

import hashlib
import marshal
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "groupreader"
R4_PYC_DIR = Path(
    os.environ.get(
        "R4_PYC_DIR",
        "/tmp/gcr_extract/GroupChatReader-WinR4.exe_extracted/"
        "PYZ.pyz_6ca878ad-f722-47b2-97e7-defec8ffa77b_extracted/groupreader",
    )
)
DEFAULT_MODULES = [
    "__init__",
    "deepseek",
    "gui",
    "merge",
    "model",
    "native",
    "parser",
    "scrolling",
    "storage",
    "windows_accessibility",
    "worker",
]


def load_pyc(path: Path) -> types.CodeType:
    return marshal.loads(path.read_bytes()[16:])


def digest(code: types.CodeType) -> str:
    parts = [
        code.co_code,
        repr(code.co_names),
        repr(code.co_varnames),
        repr(code.co_freevars),
        repr(code.co_cellvars),
        repr((code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount, code.co_flags)),
    ]
    firstlineno = "__firstlineno__" in code.co_names
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            parts.append(digest(const))
        elif isinstance(const, (set, frozenset)):
            parts.append("set:" + repr(sorted(map(repr, const))))
        elif firstlineno and type(const) is int and const == code.co_firstlineno:
            # Python 3.13 stores the class's own line number in __firstlineno__.
            parts.append("<firstlineno>")
        else:
            parts.append(repr(const))
    return hashlib.sha256(b"\x00".join(p.encode() if isinstance(p, str) else p for p in parts)).hexdigest()


def flatten(code: types.CodeType, prefix: str = "", out: dict[str, types.CodeType] | None = None):
    if out is None:
        out = {}
    out[prefix + code.co_name] = code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            flatten(const, prefix + code.co_name + ".", out)
    return out


def compare(module: str, verbose: bool) -> int:
    binary = flatten(load_pyc(R4_PYC_DIR / f"{module}.pyc"))
    source = compile(
        (SRC / f"{module}.py").read_text(encoding="utf-8"),
        f"{module}.py",
        "exec",
        dont_inherit=True,
    )
    built = flatten(source)
    diff = [name for name in binary if name in built and digest(binary[name]) != digest(built[name])]
    missing = [name for name in binary if name not in built]
    extra = [name for name in built if name not in binary]
    state = "OK" if not (diff or missing or extra) else "DIFF"
    print(f"{module:24} {state:4} same={len(binary) - len(diff) - len(missing):3} diff={len(diff):3} missing={len(missing):3} extra={len(extra):3}")
    if verbose:
        for name in diff:
            print(f"    diff   {name}")
        for name in missing:
            print(f"    missing {name}")
        for name in extra:
            print(f"    extra  {name}")
    return len(diff) + len(missing) + len(extra)


def main() -> int:
    modules = sys.argv[1:] or DEFAULT_MODULES
    verbose = os.environ.get("VERBOSE", "") not in ("", "0")
    failures = sum(compare(module, verbose) for module in modules)
    print("all code objects match" if not failures else f"{failures} mismatched/missing code object(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
