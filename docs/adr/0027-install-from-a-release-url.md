# 0027: Install from a release URL

Status: superseded by [0029](0029-install-from-pypi-only.md) (2026-10-17); accepted 2026-10-16

## Decision

A tag push builds a release zip, and `bootstrap.py` installs the harness from it, so a machine needs no git checkout of the core.

- **Release.** `.github/workflows/release.yml` runs on a `v*.*.*` tag, after CI passes. `.github/scripts/build_release.py` builds `codex-harness.zip` with `git archive` and refuses a tag that is not `v` plus `harness.VERSION`. `.gitattributes` keeps tests, docs, the Worker and CI files out of the archive (but keeps `bench/requirements.toml`) and sets `* text=auto eol=lf`. The zip gets a `VERSION` file with two words: the tag and its `release_digest`. The release publishes `codex-harness.zip`, `SHA256SUMS` and `bootstrap.py`. The first release with a zip is v0.11.0; older tags have none, and the bootstrap says so.
- **Digest.** `harness.install.release_digest` hashes every file under the folder, sorted by POSIX path string, skipping `__pycache__` and the top-level `VERSION`. It is the same on every OS. It is not `tree_digest`, which orders by `Path` (case-insensitive on Windows) and is what the `.skill-check` stamps hold, so those stamps are untouched.
- **Bootstrap.** `bootstrap.py` is stdlib only and runs on Python 3.11 or newer. In order, it resolves the overlay, reads the pin, picks the tag, gets the version folder, runs `install`, writes the launcher and runs `doctor`.
  - The overlay comes from `--overlay` (a folder, an `https://` or `ssh://` URL, or `user@host:path`), then `CODEX_HARNESS_OVERLAY`, then an existing `~/codex-overlay`. With none of these it creates `~/codex-overlay` from `overlay-examples/release`.
  - The overlay's `core_version` is the pin. `--version` may only change the pin in an overlay the bootstrap created itself: it is marked `# written by codex-harness bootstrap`, has no `.git`, and the bootstrap rewrites the pin after a successful install. Any other overlay, and every overlay with its own `.git`, keeps its pin; `--version` with a different tag is refused and you bump `core_version` yourself. With no overlay and no `--version`, the tag is the latest release, resolved once.
  - The core goes to `<data-dir>/vX.Y.Z/`. A folder that already exists is reused only after its `VERSION` and digest match. A new zip is unpacked into a temporary folder next to the target, checked, then renamed. Old version folders stay, so a rollback needs no download.
  - The launcher (`~/.local/bin/harness`, or `harness.cmd` on Windows) sets `PYTHONPATH` and `CODEX_HARNESS_OVERLAY` and runs `python -P -s -m harness`. It is written only after `install` succeeds. A file at that path that the bootstrap did not write is never replaced.
  - The bootstrap runs `install` and `doctor` itself as `python -P -s -m harness ...`, not through the launcher, because a `.cmd` file re-parses its arguments. It exits with install's status and prints doctor's report; a doctor FAIL does not change the exit status.
  - Options: `--overlay`, `--overlay-dir`, `--version`, `--repo`, `--base-url`, `--codex-bin`, `--codex-home`, `--data-dir`, `--bin-dir`. The data directory is `~/.local/share/codex-harness` (`%LOCALAPPDATA%\codex-harness` on Windows) and the launcher directory `~/.local/bin` (`bin\` inside the data directory on Windows).
- **Doctor.** When the core has a `VERSION` file and no `.git`, `doctor` checks it as a release: a tag that differs from the overlay's `core_version` FAILs when `core_writable = false` and warns otherwise, and the release digest is checked only when `core_writable = false`. This amends ADR 0002, which said `doctor` FAILs every core that is not a git checkout. A core with a `.git` is checked as before.
- **Worker.** A Cloudflare Worker in `worker/` serves https://setup-harness.jeansouza.dev. `/` is a plain-text page with the commands, `/bootstrap.py` is proxied from the latest GitHub release, and `/v/<tag>/<file>` redirects to the release asset. The bootstrap falls back to GitHub directly when the site fails. The Worker is infrastructure, not core, so ADR 0003's rejection of Node for the core stands. The wrangler version is pinned in CONTRIBUTING.md (4.151.1).

## Context

The pack was installable only from a git checkout of the core, which meant git, a clone and several commands on every machine, and a different path on a managed Windows laptop. The user wanted to give a machine one URL. The overlay already pins a core version, so the release zip is what that pin points at.

Piping a script into Python is convenient and cannot protect itself: the script that would verify the download is the script being downloaded. The design keeps the pipe as a shortcut and documents download, read and run as the reliable form.

## Data

Telemetry. Only the Worker counts. The tool sends nothing: `bootstrap.py` and the CLI add no query string, set no user-agent of their own and make no ping. The Worker counts `GET` requests only, in Analytics Engine: `bootstrap` for `/bootstrap.py` and `download` for `/v/<tag>/codex-harness.zip`. Each row holds the event, the tag, a client type (`curl`, `powershell`, `python` or `other`, from the user-agent, which is not stored) and the Cloudflare country. It does not store the IP address, the user-agent string or an install ID, and Workers Logs are off. Cloudflare's own edge processing is outside this promise. The counts are request counts anyone can forge, not installs, and an install, an upgrade and a rollback look the same. There is no `/stats` route and no `/latest` route: the Analytics Engine binding is write-only, and the numbers are read with the SQL API (see `worker/README.md`). CONTRIBUTING's rule that the core never downloads a tool at run time still holds: the bootstrap downloads the core itself and runs before the core exists.

Security. `SHA256SUMS` catches a corrupted download and a swapped zip only while the sums come from GitHub. A compromised release, or a hostile `--base-url`, defeats it. The piped script cannot verify itself. Download-then-run is the reliable form: download only `bootstrap.py`, read it, run it. `bootstrap.py` has no checksum of its own, so reading it is the check; the bootstrap fetches the zip and `SHA256SUMS` itself and verifies the zip. The bootstrap also refuses unsafe zip entries (paths that escape, reserved Windows names, collisions, links), caps sizes, refuses redirects off https, rejects git URLs with credentials, and disables the `ext` and `file` git transports when it clones. Signing the release is future work.

Reviews. The spec was reviewed on 2026-10-16 by Codex `gpt-6-astra`, Claude Fable 5.1 (security) and Claude Opus 5.5 (architecture and fit). The verdicts were rework, approve with fixes and rework. Findings that shaped the design:

- The shipped example pinned v0.8.0, and the pin contract was unclear (all three).
- Import isolation and launcher hardening: `python -P -s`, no current-directory imports (Codex, Fable).
- Git URL injection through the overlay argument (Fable, Codex).
- A reused version folder had to be verified, not trusted, and the design conflicted with ADR 0002 (all three).
- `latest` had to be resolved once (Codex, Opus).
- The Analytics Engine binding is write-only, so the `/stats` route was cut (Opus, Codex).
- `bench/requirements.toml` had to be part of the release (Opus, Codex).
- Doctor in release mode reports instead of blocking (Opus, Codex).
- PowerShell `irm` returns a typed value, so the Windows one-liner needed care (Opus).
- Empty pipes and a dead domain had to fail clearly (Codex).
- The privacy wording had to be scoped (all three).

After the review the user moved all telemetry into the Worker, so the bootstrap's `?from=&os=` parameters and the install, upgrade and rollback events were dropped. Opus warned that the prose gate would refuse these docs; it does not, because this repo sets `codexharness.allowProse true`.

Why not `tree_digest`. It orders files by `Path`, which compares case-insensitively on Windows, so the same tree hashed differently on two systems. Changing it would also have invalidated every `.skill-check` stamp. `release_digest` sorts the POSIX path string and leaves `tree_digest` alone.

Windows CI. Building this on three systems exposed failures that were already in the repository and are fixed on the same branch:

- CRLF checkouts changed the bytes of skills, so their digests no longer matched their stamps. `.gitattributes` now sets `* text=auto eol=lf`.
- `Path` ordering is case-insensitive on Windows, so skill digests differed by system.
- Git output was decoded in the ANSI codepage, not UTF-8.
- An unmatched source pattern raised `EINVAL` on Windows instead of reporting a missing file.

Also, `skills/remove-ai-attribution/SKILL.md` had invalid YAML frontmatter. It is fixed, and the skill needs a Codex restamp (`harness skill-check skills/remove-ai-attribution --review --live`) at release time.

## Cost

One Python file of about 470 lines, one build script, a release workflow, a Worker of about 60 lines with its tests, and a smoke test on three systems in CI.

## Rejected

- Cloning the core in the bootstrap: needs git, and gives no digest to check.
- Telemetry in the tool: the pack sends nothing, and the Worker can count requests without help.
- A `/latest` route in the Worker: the bootstrap resolves the latest tag from GitHub itself, once.
