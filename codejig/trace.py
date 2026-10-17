from __future__ import annotations

import codecs
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from codejig.analyze import printable, source_files, utf8_stdout

TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|specs?)/"
    r"|\.(test|spec)\.[cm]?[jt]sx?$"
    r"|(^|/)test_[^/]*\.py$"
    r"|_test\.py$"
    r"|Tests?\.cs$"
    r"|(^|/)[^/]*\.Tests?/"
)
ID_CHARS = r"[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)*"
MAX_BYTES = 2_000_000
LINE_ID = re.compile(rf"^\s*(?:[-*+]\s+|\d+[.)]\s+(?=#|REQ-|[\w.-]*\d))?#?(?:REQ-)?({ID_CHARS})", re.IGNORECASE)


@dataclass(frozen=True)
class Result:
    covered: dict[str, list[str]]
    missing: list[str]
    unknown: dict[str, list[str]]


def _norm(value) -> str:
    text = str(value).strip().lstrip("#")
    return text[4:] if text.lower().startswith("req-") else text


def _json_ids(data) -> list:
    if isinstance(data, dict):
        data = data.get("workItems") or data.get("value") or ([data] if "id" in data else [])
    if isinstance(data, (int, str)) and not isinstance(data, bool):
        return [data]
    if not isinstance(data, list):
        return []
    return [item.get("id") if isinstance(item, dict) else item for item in data if not isinstance(item, (list, bool))]


def read_ids(text: str) -> list[str]:
    text = text.lstrip("\ufeff")
    try:
        raw = _json_ids(json.loads(text))
    except ValueError:
        if text.lstrip().startswith(("[", "{")):
            raise ValueError("looks like JSON but does not parse") from None
        raw = [m.group(1) for m in map(LINE_ID.match, text.splitlines()) if m]
    ids = [_norm(v) for v in raw if v is not None and _norm(v)]
    return list(dict.fromkeys(ids))


def _regex(pattern: str, id_part: str) -> re.Pattern:
    if "{id}" not in pattern:
        raise ValueError("pattern must contain {id}")
    head, _, tail = pattern.partition("{id}")
    return re.compile(rf"(?<![\w-]){re.escape(head)}{id_part}{re.escape(tail)}(?!\w|[.-]\w)", re.IGNORECASE)


def trace(root: Path, ids: list[str], pattern: str = "REQ-{id}") -> Result:
    wanted: dict[str, str] = {}
    for i in ids:
        wanted.setdefault(i.lower(), i)
    ids = list(wanted.values())
    finder = _regex(pattern, f"({ID_CHARS})")
    covered: dict[str, list[str]] = {}
    unknown: dict[str, list[str]] = {}
    for name in source_files(root):
        if not TEST_PATH.search(name):
            continue
        f = root / name
        if f.stat().st_size > MAX_BYTES:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for found in dict.fromkeys(m.group(1) for m in finder.finditer(text)):
            target = covered if found.lower() in wanted else unknown
            key = wanted.get(found.lower(), found)
            if name not in target.setdefault(key, []):
                target[key].append(name)
    ordered = {i: covered[i] for i in ids if i in covered}
    return Result(ordered, [i for i in ids if i not in covered], dict(sorted(unknown.items())))


def _files(names: list[str], limit: int = 3) -> str:
    extra = f" and {len(names) - limit} more" if len(names) > limit else ""
    return printable(", ".join(names[:limit]) + extra)


def render(result: Result) -> list[str]:
    total = len(result.covered) + len(result.missing)
    out = [f"trace: {len(result.covered)} of {total} requirements have tests"]
    out += [f"  {printable(i)}  {_files(names)}" for i, names in result.covered.items()]
    if result.missing:
        out.append(f"missing: {printable(', '.join(result.missing))}")
    out += [f"unknown in tests: {printable(i)} ({_files(names)})" for i, names in result.unknown.items()]
    return out


def decode(data: bytes) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _read(name: str) -> str:
    if name != "-":
        return decode(Path(name).read_bytes())
    return decode(sys.stdin.buffer.read()) if hasattr(sys.stdin, "buffer") else sys.stdin.read()


def cli(args) -> int:
    utf8_stdout()
    try:
        ids = read_ids(_read(args.ids))
    except OSError as e:
        print(f"code-jig: {args.ids}: {e.strerror or e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"code-jig: {args.ids}: {e}", file=sys.stderr)
        return 2
    if not ids:
        print(f"code-jig: no requirement IDs in {args.ids}", file=sys.stderr)
        return 2
    try:
        result = trace(Path(args.path).resolve(), ids, args.pattern)
    except (ValueError, re.error) as e:
        print(f"code-jig: --pattern: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"covered": result.covered, "missing": result.missing, "unknown": result.unknown}, ensure_ascii=False, indent=1))
    else:
        for line in render(result):
            print(line)
    return 1 if result.missing or result.unknown else 0
