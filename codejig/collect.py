import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

from codejig import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, offset INTEGER NOT NULL, size INTEGER NOT NULL, thread_id TEXT, model TEXT, turn_id TEXT, line INTEGER NOT NULL, last_total INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, parent_id TEXT, kind TEXT NOT NULL, started TEXT, cwd TEXT);
CREATE TABLE IF NOT EXISTS usage (thread_id TEXT NOT NULL, key TEXT NOT NULL, turn_id TEXT, ts TEXT, model TEXT, input INTEGER, cached INTEGER, output INTEGER, reasoning INTEGER, PRIMARY KEY (thread_id, key));
CREATE TABLE IF NOT EXISTS calls (thread_id TEXT NOT NULL, call_id TEXT NOT NULL, turn_id TEXT, ts TEXT, cmd TEXT, escalated INTEGER NOT NULL, PRIMARY KEY (thread_id, call_id));
CREATE TABLE IF NOT EXISTS compactions (thread_id TEXT NOT NULL, line INTEGER NOT NULL, ts TEXT, PRIMARY KEY (thread_id, line));
CREATE TABLE IF NOT EXISTS quarantine (path TEXT NOT NULL, line INTEGER NOT NULL, reason TEXT, PRIMARY KEY (path, line));
CREATE TABLE IF NOT EXISTS call_results (thread_id TEXT NOT NULL, call_id TEXT NOT NULL, exit_code INTEGER, skill TEXT, PRIMARY KEY (thread_id, call_id));
CREATE TABLE IF NOT EXISTS skill_reads (thread_id TEXT NOT NULL, call_id TEXT NOT NULL, skill TEXT NOT NULL, PRIMARY KEY (thread_id, call_id, skill));
CREATE TABLE IF NOT EXISTS guardian (thread_id TEXT NOT NULL, line INTEGER NOT NULL, ts TEXT, outcome TEXT NOT NULL, PRIMARY KEY (thread_id, line));
CREATE TABLE IF NOT EXISTS file_marks (path TEXT PRIMARY KEY, offset INTEGER NOT NULL, mark TEXT NOT NULL);
DROP VIEW IF EXISTS turn_usage;
CREATE VIEW turn_usage AS
  SELECT u.*, COALESCE(t.kind, 'main') AS kind, t.parent_id AS parent_id
  FROM usage u LEFT JOIN threads t ON t.id = u.thread_id
  WHERE u.key NOT LIKE 'tc:%' OR NOT EXISTS (
    SELECT 1 FROM usage r WHERE r.thread_id = u.thread_id AND r.key NOT LIKE 'tc:%' AND r.turn_id IS u.turn_id);
DROP VIEW IF EXISTS call_view;
CREATE VIEW call_view AS
  SELECT c.*, r.exit_code AS exit_code
  FROM calls c LEFT JOIN call_results r ON r.thread_id = c.thread_id AND r.call_id = c.call_id;
"""
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
CMD = re.compile(r'\bcmd:\s*"((?:[^"\\]|\\.)*)"')
EXIT = re.compile(r'"exit_code"\s*:\s*(-?\d+)|Exit code:\s*(-?\d+)|exited with code\s+(-?\d+)')
SKILL = re.compile(r"skills[/\\]+([A-Za-z0-9_.-]+)[/\\]+SKILL\.md")
OUTPUTS = ("function_call_output", "custom_tool_call_output")
MARK_BYTES = 4096


def db_path() -> Path:
    return paths.state_dir() / "usage.sqlite3"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.executescript(SCHEMA)
    return con


def rollout_files(home: Path) -> list[Path]:
    return sorted([*home.glob("sessions/**/rollout-*.jsonl"), *home.glob("archived_sessions/rollout-*.jsonl")])


def kind_of(meta: dict) -> str:
    src = meta.get("source")
    if meta.get("thread_source") == "guardian_review" or (isinstance(src, dict) and src.get("subagent") == {"other": "guardian"}):
        return "guardian"
    if isinstance(src, dict) and "subagent" in src:
        return "subagent"
    return "main"


def _s(v) -> str | None:
    return v if isinstance(v, str) else None


def _usage(u) -> tuple[int, ...] | None:
    if not isinstance(u, dict) or not all(isinstance(u.get(k), int) and not isinstance(u.get(k), bool) for k in USAGE_KEYS):
        return None
    return tuple(u[k] for k in USAGE_KEYS)


def _custom_call(call_id: str, payload: dict) -> tuple[str, str, bool] | None:
    cells = cell_commands(payload)
    return (call_id, *cells[0]) if cells else None


def _function_call(call_id: str, payload: dict) -> tuple[str, str, bool] | None:
    try:
        args = json.loads(payload.get("arguments") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(args, dict):
        return None
    cmd = args.get("cmd", args.get("command"))
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    if not isinstance(cmd, str):
        return None
    return call_id, cmd, args.get("sandbox_permissions") == "require_escalated"


def tool_call(payload: dict) -> tuple[str, str, bool] | None:
    call_id = payload.get("call_id")
    if not isinstance(call_id, str):
        return None
    build = {"custom_tool_call": _custom_call, "function_call": _function_call}.get(payload.get("type"))
    return build(call_id, payload) if build else None


def _decode(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw


def cell_commands(payload: dict) -> list[tuple[str, bool]]:
    text = payload.get("input") if payload.get("type") == "custom_tool_call" else None
    found = list(CMD.finditer(text)) if isinstance(text, str) else []
    starts, end = [], 0
    for m in found:
        starts.append(max(text.rfind("{", 0, m.start()), end))
        end = m.end()
    ends = [*starts[1:], len(text)] if found else []
    return [(_decode(m.group(1)), "require_escalated" in text[a:b]) for m, a, b in zip(found, starts, ends, strict=True)]


def _output_text(payload: dict) -> str:
    out = payload.get("output")
    if isinstance(out, list):
        return "\n".join(o["text"] for o in out if isinstance(o, dict) and isinstance(o.get("text"), str))
    return out if isinstance(out, str) else ""


def exit_code(payload: dict) -> int | None:
    text = _output_text(payload)
    try:
        meta = json.loads(text).get("metadata")
    except (ValueError, AttributeError):
        meta = None
    if isinstance(meta, dict) and isinstance(meta.get("exit_code"), int):
        return meta["exit_code"]
    m = EXIT.search(text)
    return int(next(g for g in m.groups() if g is not None)) if m else None


def skills_of(cmd: str) -> list[str]:
    return list(dict.fromkeys(SKILL.findall(cmd)))


def guardian_outcome(payload: dict) -> str | None:
    try:
        msg = json.loads(payload.get("last_agent_message") or "")
    except (TypeError, ValueError):
        return None
    return msg["outcome"] if isinstance(msg, dict) and isinstance(msg.get("outcome"), str) else None


def _response_item(con: sqlite3.Connection, thread: str, turn: str | None, ts: str | None, p: dict) -> None:
    call = tool_call(p)
    if call:
        rows = [(call[0], call[1], int(call[2]))]
        rows += [(f"{call[0]}#{i}", cmd, int(esc)) for i, (cmd, esc) in enumerate(cell_commands(p)[1:], start=2)]
        for call_id, cmd, escalated in rows:
            con.execute("INSERT OR IGNORE INTO calls (thread_id, call_id, turn_id, ts, cmd, escalated) VALUES (?,?,?,?,?,?)",
                        (thread, call_id, turn, ts, cmd, escalated))
            for skill in skills_of(cmd):
                con.execute("INSERT OR IGNORE INTO skill_reads VALUES (?,?,?)", (thread, call_id, skill))
    elif p.get("type") in OUTPUTS and isinstance(p.get("call_id"), str):
        code = exit_code(p)
        if code is not None:
            con.execute("INSERT INTO call_results (thread_id, call_id, exit_code) SELECT ?, ?, ? "
                        "WHERE EXISTS (SELECT 1 FROM calls WHERE thread_id = ? AND call_id = ?) "
                        "AND NOT EXISTS (SELECT 1 FROM calls WHERE thread_id = ? AND call_id = ? || '#2') "
                        "ON CONFLICT(thread_id, call_id) DO UPDATE SET exit_code = excluded.exit_code WHERE call_results.exit_code IS NULL",
                        (thread, p["call_id"], code, thread, p["call_id"], thread, p["call_id"]))


class _Cursor:
    def __init__(self, thread=None, model=None, turn=None, line=0, last_total=-1):
        self.thread, self.model, self.turn, self.line, self.last_total = thread, model, turn, line, last_total


def _session_meta(con, st, p, ts, quarantine) -> None:
    if st.thread is None and isinstance(p.get("id"), str):
        st.thread = p["id"]
        con.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?)",
                    (st.thread, _s(p.get("parent_thread_id")), kind_of(p), _s(p.get("timestamp")) or ts, _s(p.get("cwd"))))


def _turn_context(con, st, p, ts, quarantine) -> None:
    st.model = _s(p.get("model")) or st.model
    st.turn = _s(p.get("turn_id")) or st.turn


def _token_usage_record(con, st, p, ts, quarantine) -> None:
    u = _usage(p.get("usage"))
    if u is None or not isinstance(p.get("response_id"), str):
        quarantine("token_usage_record shape")
        return
    con.execute("INSERT OR IGNORE INTO usage VALUES (?,?,?,?,?,?,?,?,?)",
                (st.thread, p["response_id"], _s(p.get("turn_id")) or st.turn, ts, st.model, *u))


def _token_count(con, st, p, ts, quarantine) -> None:
    info = p.get("info")
    if info is None:
        return
    total = (info.get("total_token_usage") or {}).get("total_tokens") if isinstance(info, dict) else None
    u = _usage(info.get("last_token_usage")) if isinstance(info, dict) else None
    if u is None or not isinstance(total, int):
        quarantine("token_count shape")
        return
    if total == st.last_total:
        return
    st.last_total = total
    con.execute("INSERT OR IGNORE INTO usage VALUES (?,?,?,?,?,?,?,?,?)",
                (st.thread, f"tc:{st.line}", st.turn, ts, st.model, *u))


def _compacted(con, st, p, ts, quarantine) -> None:
    con.execute("INSERT OR IGNORE INTO compactions VALUES (?,?,?)", (st.thread, st.line, ts))


def _response(con, st, p, ts, quarantine) -> None:
    _response_item(con, st.thread, st.turn, ts, p)


def _task_complete(con, st, p, ts, quarantine) -> None:
    outcome = guardian_outcome(p)
    if outcome and con.execute("SELECT 1 FROM threads WHERE id=? AND kind='guardian'", (st.thread,)).fetchone():
        con.execute("INSERT OR IGNORE INTO guardian VALUES (?,?,?,?)", (st.thread, st.line, ts, outcome))


_RECORDS = {"turn_context": _turn_context, "token_usage_record": _token_usage_record,
            "compacted": _compacted, "response_item": _response}
_EVENTS = {"token_count": _token_count, "task_complete": _task_complete}


def _handler(t, p):
    if t == "event_msg":
        kind = p.get("type")
        return _EVENTS.get(kind) if isinstance(kind, str) else None
    return _RECORDS.get(t) if isinstance(t, str) else None


def _record(con, st, raw, quarantine) -> None:
    try:
        o = json.loads(raw)
    except ValueError:
        quarantine("not JSON")
        return
    p = o.get("payload") if isinstance(o, dict) else None
    if not isinstance(p, dict):
        quarantine("no payload")
        return
    t, ts = o.get("type"), _s(o.get("timestamp"))
    if t == "session_meta":
        _session_meta(con, st, p, ts, quarantine)
    elif st.thread is None:
        quarantine("before session_meta")
    else:
        handler = _handler(t, p)
        if handler:
            handler(con, st, p, ts, quarantine)


def _mark(f, offset: int) -> str:
    start = max(0, offset - MARK_BYTES)
    f.seek(start)
    return hashlib.sha256(f.read(offset - start)).hexdigest()


def _scan_file(con: sqlite3.Connection, path: Path) -> tuple[int, int]:
    size = path.stat().st_size
    row = con.execute("SELECT offset, thread_id, model, turn_id, line, last_total FROM files WHERE path=?", (str(path),)).fetchone()
    offset, *state = row or (0, None, None, None, 0, -1)
    mark = con.execute("SELECT offset, mark FROM file_marks WHERE path=?", (str(path),)).fetchone()
    with open(path, "rb") as f:
        if size < offset or (mark and (mark[0] > size or _mark(f, mark[0]) != mark[1])):
            offset, state = 0, [None, None, None, 0, -1]
        if size == offset:
            return 0, 0
        f.seek(offset)
        data = f.read()
        end = data.rfind(b"\n") + 1
        con.execute("INSERT OR REPLACE INTO file_marks VALUES (?,?,?)", (str(path), offset + end, _mark(f, offset + end)))
    st = _Cursor(*state)
    count = bad = 0
    for raw in data[:end].split(b"\n")[:-1]:
        st.line += 1
        count += 1

        def quarantine(reason: str, line: int = st.line) -> None:
            nonlocal bad
            bad += 1
            con.execute("INSERT OR IGNORE INTO quarantine VALUES (?,?,?)", (str(path), line, reason))

        try:
            _record(con, st, raw, quarantine)
        except Exception as e:
            quarantine(type(e).__name__)
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)",
                (str(path), offset + end, size, st.thread, st.model, st.turn, st.line, st.last_total))
    return count, bad


def scan(con: sqlite3.Connection, home: Path, rescan: bool = False) -> dict[str, int]:
    if rescan:
        with con:
            for table in ("files", "file_marks", "threads", "usage", "calls", "compactions", "quarantine", "guardian", "call_results", "skill_reads"):
                con.execute(f"DELETE FROM {table}")
    summary = {"files": 0, "lines": 0, "quarantined": 0}
    for path in rollout_files(home):
        with con:
            lines, bad = _scan_file(con, path)
        summary["files"] += 1
        summary["lines"] += lines
        summary["quarantined"] += bad
    return summary


def cli(args) -> int:
    try:
        home = paths.codex_home(args.codex_home)
        con = connect(db_path())
        try:
            summary = scan(con, home, rescan=args.rescan)
        finally:
            con.close()
        print(f"code-jig collect: {summary['files']} files, {summary['lines']} new lines, {summary['quarantined']} quarantined", file=sys.stderr)
    except Exception as e:
        print(f"code-jig collect: {type(e).__name__}: {e}", file=sys.stderr)
    print("{}")
    return 0
