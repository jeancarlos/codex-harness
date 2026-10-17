# Runbook: setting up a managed machine

A managed machine (a work laptop, a shared build box, anything someone else administers) runs the core read-only and keeps everything local in its own overlay. Nothing from that overlay flows back to the core.

## Before you start

- Check that the machine's rules allow installing a small stdlib-only Python tool from a public repo.
- Find out whether an administrator applies managed Codex config: `/etc/codex/requirements.toml`, or `%ProgramData%\OpenAI\Codex\requirements.toml` on Windows. `doctor` reports how it differs from `bench/requirements.toml`.
- Install Codex and log in the way that machine expects.
- Python 3.11 or newer, git, and pipx (`python3 -m pip install --user pipx`, or `py -3 -m pip install --user pipx` on Windows, then `pipx ensurepath`).

## 1. Get the core, read-only

Install a released version from PyPI, never a branch or a development wheel:

```sh
pipx install code-jig==X.Y.Z
```

On a machine where PyPI is blocked, download the wheel from the [release page](https://github.com/jeancarlos/code-jig/releases/latest) in a browser, verify it as the README's "Verify a release" section shows, and `pipx install ./code_jig-X.Y.Z-py3-none-any.whl`. Either way, pipx puts `code-jig` on `PATH` in its own venv; nothing runs from a pipe and no script needs `-ExecutionPolicy Bypass`.

Do not sign in to a personal account with write access on a machine someone else administers.

## 2. Create the overlay from the example

```sh
code-jig init-overlay ~/code-jig-overlay
```

`init-overlay` copies the `personal` example. For a managed machine, copy the four files of [overlay-examples/enterprise](../../overlay-examples/enterprise) over it (`core_writable = false`, a company-facing Guardian policy, its own models) and delete `knowledge/`, which describes a personal bench machine. Then make it a repository:

```sh
cd ~/code-jig-overlay && git init && git add -A && git commit -m "chore: local overlay"
```

Keep it local, or push it to a git host that belongs to the same organisation as the machine. Never push it to a personal account.

## 3. Fill the slots

| File | What goes in it |
|---|---|
| `overlay.toml` | `core_version` matching the installed version (`code-jig --version`), as `vX.Y.Z`. Keep `core_writable = false`. |
| `profiles.toml` | One model ID per tier (`default`, `deep`, `bulk`, `review`), from the models this account may use. `doctor` checks each against Codex's model catalog. |
| `guardian-policy.md` | What Guardian should treat as riskier or safer here: internal hosts, deploy commands, approved package sources. It is added to the upstream policy, never replaces it. |
| `AGENTS.overlay.md` | Rules every session follows. |
| `rules/*.rules` | Optional. Prefer `forbidden`. An `allow` rule runs the command outside the sandbox with no review; read ADR 0009 first. |
| `knowledge/` | Optional. `INDEX.md` plus one fact per file, 150 index lines at most. |
| `skills/` | Optional. One folder per skill with a `SKILL.md`. |

## 4. Install and check

```sh
code-jig install
code-jig doctor
code-jig doctor --live
```

`~/code-jig-overlay` is found without flags. For another folder, set `CODE_JIG_OVERLAY` (`$env:CODE_JIG_OVERLAY = "$HOME\enterprise-overlay"` in PowerShell) or pass `--overlay`. With `core_writable = false`, `doctor` FAILs if any installed module or data file differs from the release.

Fix every FAIL; [doctor](doctor.md) explains each line. `--live` runs one short request per tier, so it spends a few tokens; skip it if usage is strictly metered.

## 5. First run

- If plugins are allowed, install superpowers: `codex plugin add superpowers@openai-curated-remote`.
- The first interactive session asks once to trust the `Stop` hook. Accept, or the tracker never collects.
- Behind a TLS-inspecting network, read [network-and-certificates](network-and-certificates.md) first.

## Day to day

- `code-jig tokens` shows where tokens go; see [tokens](tokens.md). Numbers for the deck go in `docs/deck/data.local.js` on that machine only; it is gitignored.
- Improvements go into overlay slots through the `improve` skill, then `install` and `doctor`.
- Upgrades: [upgrade-and-rollback](upgrade-and-rollback.md).

## What never leaves the machine

The overlay, numbers measured there, internal names, hosts, people and code. The `improve` skill ends with upstream candidates written in generic terms; restate them by hand on the bench if they help anyone.
