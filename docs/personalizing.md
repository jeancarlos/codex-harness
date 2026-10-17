# Personalizing

## Core vs overlay
Core is shared and pinned. The overlay is a separate directory with fixed slots:
- `overlay.toml`: `core_version`, `core_writable`, and optionally `auto_compact_token_limit`, a positive integer that every tier profile receives as `model_auto_compact_token_limit`. Unset, Codex compacts at 90% of the model's context window. A lower value trades re-reading a long history on every request for a summary that may lose detail; measure with `code-jig tokens` and `code-jig evidence` before and after setting it.
  `git_gate = true` installs the git gate (AI attribution stripped from commit messages, invisible characters from staged files, attributed commits refused at push) and `prose_gate = true` adds the refusal of agent-written prose; both change the global `core.hooksPath`, so they are off unless set. See [commit-attribution](runbooks/commit-attribution.md).
- `profiles.toml`: one model ID per tier (`default`, `deep`, `bulk`, `review`). `doctor` lists the models your account has.
- `AGENTS.overlay.md`: appended to the core rules.
- `guardian-policy.md`: added to the upstream Guardian policy, never replacing it.
- `rules/*.rules`: Codex exec-policy rules. An `allow` rule runs the command outside the sandbox and skips Guardian, and the agent can edit what a test or build command runs, so only allow what you would run unsupervised in that repo. `forbidden` rules are always safe to add.

- `skills/<name>/SKILL.md`: your own skills, installed next to the core ones. A name the core already uses is an error.
- `knowledge/`: one fact per file, plus `knowledge/INDEX.md` with one line per fact. When the index exists, the generated `AGENTS.md` points Codex at it. Keep it at 150 lines or fewer; `doctor` warns past that. The `improve` skill maintains it. Pass it with `--overlay <dir>` or `CODE_JIG_OVERLAY`.

## Starting an overlay
`code-jig init-overlay [PATH]` copies the `personal` example (shipped inside the package) to `PATH`, by default `~/code-jig-overlay`, and prints the next steps. It refuses a folder that already has files in it. Every command finds `~/code-jig-overlay` on its own; for another folder set `CODE_JIG_OVERLAY` or pass `--overlay`. The examples live in [overlay-examples](../overlay-examples): `personal` for your own machine and `enterprise` for a locked-down one.

## Private overlay
Keep your overlay in its own private repo. Never put it inside the core checkout.

## core_writable
`core_writable = false` means nothing may edit the core; `doctor` checks that every installed module and data file still matches the release digest in the package's `VERSION` file (or, in a checkout, that the git tree is clean). Set it to true only if you intend to change the core; the `improve` skill honours it.

## Pinning core_version
Pin `core_version` to the installed version as a tag (for example v0.12.0; `code-jig --version` prints it). `init-overlay` writes the current one. `doctor` reports `ok core pin: v0.12.0`, and reports a mismatch (FAIL with `core_writable = false`, a warning otherwise) after a `pipx upgrade` until you bump the pin. In a checkout the pin is compared with the tag at `HEAD`.

## Clean-room rule
Contribute upstream only what you wrote without copying from private overlays or employer material. No corporate measurements anywhere in the core.

## Simulating a managed laptop
`bench/requirements.toml` is a guess at what an enterprise locks. Install it with `sudo install -D -m 644 bench/requirements.toml /etc/codex/requirements.toml`, remove it with `sudo rm /etc/codex/requirements.toml`. On a managed laptop, `doctor` shows how the real requirements differ from it.
