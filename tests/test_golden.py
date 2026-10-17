import json
import re
import shutil
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from codejig import install, paths
from codejig.overlay import load
from codejig.paths import CORE
from tests.util import tree

RENAME: list[tuple[str, str | Callable[[re.Match], str]]] = [
    (re.escape("setup-harness.jeansouza.dev"), "code-jig.jeansouza.dev"),
    (re.escape("codex-harness-overlay"), "code-jig-overlay"),
    (re.escape("codex-overlay"), "code-jig-overlay"),
    (re.escape("codex-harness"), "code-jig"),
    (re.escape("codex_harness"), "code_jig"),
    (re.escape("codexharness"), "codejig"),
    (re.escape("CODEX_HARNESS_"), "CODE_JIG_"),
    (re.escape("-m harness"), "-m codejig"),
    (re.escape("from harness"), "from codejig"),
    (re.escape("import harness"), "import codejig"),
    (re.escape('"harness.'), '"codejig.'),
    ((r"(?<![\w./-])harness (?=(install|doctor|collect|hooks|humanize|tokens|cross-review|analyze|trace|scrub|skill-check"
      r"|lint-skill|check-sources|evidence|git-hook)\b)"), "code-jig "),
    (re.escape("`harness`"), "`code-jig`"),
    (re.escape("the harness core"), "the code-jig core"),
    (re.escape("contains `harness/`"), "contains `codejig/`"),
    (re.escape("improve the harness,"), "improve code-jig,"),
    (re.escape('"teach the harness"'), '"teach code-jig", "teach the harness"'),
    (re.escape("The harness git gate"), "The code-jig git gate"),
    (r'(?m)^description: (Use when AI attribution leaked.*)$',
     lambda m: 'description: "' + m[1].replace('"', '\\"') + '"'),
    (re.escape("<HOME>/code-jig/lib"), "<STATE>/lib"),
    (r"(?m)^python -m codejig cross-review", "code-jig cross-review"),
    (re.escape("A tag on a test that checks something else is worse than no tag.\n"),
     "A tag on a test that checks something else is worse than no tag.\n- After changing any test or tag, rerun step 3 so the report uses current results.\n"),
    (re.escape("Run it from the repository under review, with the code-jig core on `PYTHONPATH` (POSIX: `PYTHONPATH=<core> python -m codejig cross-review`; "
               'Windows PowerShell: `$env:PYTHONPATH = "<core>"; py -3 -m codejig cross-review`).'),
     "Run it from the repository under review with the installed launcher: `code-jig cross-review` (alias `cjig cross-review`; the URL bootstrap installs both)."),
    (re.escape("# Remove AI attribution\n\n`code-jig scrub` decides everything; do not re-derive its work from git logs. It rewrites commit messages only, only on commits by the user (`git config user.email`, every `codejig.email`, and each `--author`), checks that trees, authors, committers and dates are unchanged, restores every ref if that check fails, and is deterministic: the same history gives the same new SHAs on every clone.\n\n## Procedure\n\n1. `code-jig scrub --dry-run` in the repo lists the commits that carry attribution. Nothing listed: stop.\n2. Show the list and get a yes for this repo.\n3. `code-jig scrub`. It writes a backup bundle into the git directory, rewrites local branches and tags, and prints one `git push --force-with-lease=...` command per branch. It never pushes.\n4. Pushing rewrites shared history. Get a yes for each push command, then run it exactly as printed: the lease refuses the push if someone pushed in the meantime.\n5. Every other clone of the repo, with a yes for each clone: `git fetch`, then for each branch the push rewrote run `git cherry origin/<branch> <branch>`. Rewritten commits show `-` (only their message changed); a `+` is a commit that exists only in that clone: stop and ask the user how to carry it over. No `origin/<branch>`: leave the branch alone. Otherwise `git switch <branch>` and `git reset --keep origin/<branch>`. `--keep` refuses to discard uncommitted work.\n6. Old commits stay reachable by SHA on the server (and in pull request refs) until the host garbage-collects them. Say so; removing them needs the hosting admin or recreating the repository, which only the user decides.\n\n## Prevention\n\nThe code-jig git gate (`git_gate = true` in `overlay.toml`) strips these lines in `commit-msg` and refuses pushes that still carry them. See `docs/runbooks/commit-attribution.md` in the code-jig repository.\n\n## Common mistakes\n\n| Mistake | Instead |\n|---|---|\n| Rewriting other people's commits | Only the user's identities; pass `--author` for an old address of the user, never for someone else. |\n| `git push --force` | Only the printed `--force-with-lease` commands. |\n| `--no-verify` to get past the gate | Scrub, then push normally. |\n| `git reset --hard` on other clones | `--keep`, so uncommitted work is never lost. |\n"),
     '# Remove AI attribution\n\nA safety net, not the rule: the rule (no AI attribution anywhere) is in the base instructions, and the git gate strips trailers before they land. Use this only when attribution already reached commits.\n\n`code-jig scrub` decides everything; do not re-derive its work from git logs. It rewrites commit messages only, only on commits by the user (`git config user.email`, every `codejig.email`, and each `--author`), checks that trees, authors, committers and dates are unchanged, restores every ref if that check fails, and is deterministic: the same history gives the same new SHAs on every clone.\n\n## Procedure\n\n1. `code-jig scrub --dry-run` in the repo lists the commits that carry attribution. Nothing listed: stop.\n2. Show the list and get a yes for this repo.\n3. `code-jig scrub`. It saves a backup bundle in the git directory first and prints its path: that is the safe point. Keep the path in the reply. It rewrites local branches and tags, prints one `git push --force-with-lease=...` command per branch, and never pushes.\n4. Pushing rewrites shared history. Get a yes for each push command, then run it exactly as printed: the lease refuses the push if someone pushed in the meantime.\n5. Other clones: never repair them by hand. In each one, with a yes, run this procedure from step 1. Scrub gives the same SHAs there, so after `git fetch`, `git status -sb` shows the branch even with its upstream and there is nothing to push. Anything else (ahead, behind or diverged) means that clone has its own commits: stop and ask the user.\n6. Old commits stay reachable by SHA on the server (and in pull request refs) until the host garbage-collects them. Say so; removing them needs the hosting admin or recreating the repository, which only the user decides.\n\n## Undo\n\nBefore pushing, the backup restores every branch and tag: `git fetch --update-head-ok <bundle> "+refs/heads/*:refs/heads/*" "+refs/tags/*:refs/tags/*"`. After pushing, restoring needs another lease-protected push, which only the user decides.\n\n## Common mistakes\n\n| Mistake | Instead |\n|---|---|\n| Rewriting other people\'s commits | Only the user\'s identities; pass `--author` for an old address of the user, never for someone else. |\n| `git push --force` | Only the printed `--force-with-lease` commands. |\n| `--no-verify` to get past the gate | Scrub, then push normally. |\n| Resetting other clones to the remote | Run this procedure in each clone (step 5); a clone that differs is the user\'s call. |\n| Deleting the backup bundle | Keep it until every clone matches the remote. |\n'),
    (re.escape('- Root cause over symptom. Fix the shared function, not each caller.\n'),
     '- Root cause over symptom. Fix the shared function, not each caller.\n- No AI attribution. Never write `Co-authored-by` or `Generated with` lines for an AI, `AI-assisted` notes or robot emoji in commits, pull requests, comments or files. If one already reached history, use the `remove-ai-attribution` skill.\n- Subagent check. Before each `spawn_agent`, decide whether the job needs one and which `agent_type` fits. Skip it for one grep, one file read or a few commands: do those yourself. `explorer` (bulk tier) for lookups, `worker` for one bounded change with a clear file scope, checked before the next, `reviewer` (review tier) to attack a plan, diff or claim before acting on it, `judge` (deep tier) to decide between gathered conflicting claims, `default` for any other self-contained task. Keep architecture, subtle multi-file logic, security and root-cause debugging in the main thread, and say so when they need the deep tier. At most three in parallel unless asked. Name the agent you picked and why in one line.\n'),
    (re.escape('3. **Challenge.** A `reviewer` reads the evidence files and the check report, and is told not to search. It rules on every claim with one line each, `OK`, `DOUBT` or `MISMATCH`, then lists the questions that would settle each dispute.\n'),
     '3. **Challenge.** A `reviewer` reads the evidence files and the check report, and is told not to search. Its brief asks for a sweep before anything else: one line per claim, in order, none skipped or merged. `MISMATCH` when the quote does not contain what the claim asserts, `DOUBT` when the quote alone cannot tell (a number, a name, a baseline or a causal link it never states), `OK` only when the quote plainly carries the whole claim, each claim judged against its own quote. The sweep comes first because a reviewer that picks its findings first stops at the few defects that caught its eye. Tell it that a wrong flag costs one rebuttal while a fabrication it lets pass reaches the user as fact, so it flags every claim its quote does not plainly establish, and never invents a mismatch. Then it lists the questions that would settle each dispute.\n'),
]
EXAMPLES = ("enterprise", "personal")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "golden"
KEPT = ("AGENTS.md", "hooks.json")


def rename(text: str) -> str:
    for old, new in RENAME:
        text = re.sub(old, new, text)
    return text


def _kept(rel: str) -> bool:
    return rel in KEPT or rel.endswith(".config.toml") or rel.split("/")[0] in ("agents", "rules", "skills")


def _normalize(text: str, home: Path) -> str:
    py = install.interpreter()
    for raw in (py, Path(py).as_posix()):
        text = text.replace(raw, "<PY>")
    state = paths.state_dir()
    for raw in (str(state), state.as_posix()):
        text = text.replace(raw, "<STATE>")
    for raw in (str(home), home.as_posix()):
        text = text.replace(raw, "<HOME>")
    for raw in (str(CORE), CORE.as_posix()):
        text = text.replace(raw, "<CORE>")
    text = re.sub(r"(<(?:CORE|HOME|STATE|PY)>)([^\s`\"']*)", lambda m: m[1] + m[2].replace("\\", "/"), text)
    return re.sub(r"\b[0-9a-f]{64}\b", "<sha>", text)


def _normalize_hooks(text: str, home: Path) -> str:
    def walk(v):
        if isinstance(v, str):
            return re.sub(r"'(<(?:CORE|HOME|STATE|PY)>[^'\s]*)'", r"\1", _normalize(v, home))
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    return json.dumps(walk(json.loads(text)), indent=2, sort_keys=True) + "\n"


def render(example: str, base: Path) -> dict[str, str]:
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    ov = load(CORE / "overlay-examples" / example)
    out = {}
    for path, text in install.plan(CORE, ov, home):
        if not path.is_relative_to(home):
            continue
        rel = path.relative_to(home).as_posix()
        if _kept(rel):
            out[rel] = _normalize_hooks(text, home) if rel == "hooks.json" else _normalize(text, home).replace("\r\n", "\n")
    skills = base / "skills"
    install.install_skills(install.skill_sources(CORE, ov.root), skills)
    for f in sorted(p for p in skills.rglob("*") if p.is_file()):
        out["skills/" + f.relative_to(skills).as_posix()] = _normalize(f.read_text(encoding="utf-8"), home).replace("\r\n", "\n")
    return out


def _fixture(example: str) -> dict[str, str]:
    root = FIXTURES / example
    return {rename(p.relative_to(root).as_posix()): rename(p.read_text(encoding="utf-8"))
            for p in sorted(root.rglob("*")) if p.is_file()}


def capture() -> None:
    for example in EXAMPLES:
        root = FIXTURES / example
        shutil.rmtree(root, True)
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "dir with space" / "ação"
            base.mkdir(parents=True)
            for rel, text in render(example, base).items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8", newline="\n")


class GoldenTest(unittest.TestCase):
    def test_codex_output_matches_fixtures_apart_from_the_rename(self):
        for example in EXAMPLES:
            with self.subTest(example=example):
                got = render(example, tree(self))
                want = _fixture(example)
                self.assertEqual(sorted(got), sorted(want))
                for rel in want:
                    self.assertEqual(got[rel], want[rel], rel)


if __name__ == "__main__":
    if "--capture" in sys.argv:
        capture()
    else:
        unittest.main()
