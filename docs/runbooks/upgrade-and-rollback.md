# Runbook: upgrading, rolling back, uninstalling

`install` copies what the hooks need into `~/.local/share/code-jig/lib/` (`%LOCALAPPDATA%\code-jig\lib\` on Windows) and writes the rest into `$CODEX_HOME`. The hooks run that copy, not the installed package, so an upgrade does nothing until the next `install`.

## Upgrade the core

With pipx (the install path):

1. `pipx upgrade code-jig` for the latest release, or `pipx install --force code-jig==X.Y.Z` for one version. `code-jig --version` prints the installed version. Coming from the old `bootstrap.py` installer, delete its launchers `~/.local/bin/code-jig` and `cjig` first (step 7 of [Uninstall](#uninstall)), or pipx leaves them in place in front of its own.
2. Set `core_version = "vX.Y.Z"` in the overlay's `overlay.toml` to the version you installed. `doctor` compares the pin with the installed package.
3. `code-jig install`, then `code-jig doctor`. Fix every FAIL before the next session.

From a checkout (`pip install -e .` in a venv):

1. Get the new tag (`git fetch --tags && git checkout vX.Y.Z`).
2. Set `core_version = "vX.Y.Z"` in the overlay's `overlay.toml`.
3. `code-jig install`, then `code-jig doctor`. Fix every FAIL before the next session.

## When Codex updates

Codex updates on its own schedule. After an update:

1. Run `doctor`. `codex keys` FAIL means the new Codex dropped or renamed a setting the pack writes; the rest of the pack still works, but that setting is ignored.
2. Run `code-jig tokens`. A sudden `rollout lines quarantined` count means the session file format changed and the tracker is missing turns.
3. Report either one as an issue with both versions (`codex --version`, the core tag), then check for a newer core tag.

## Roll back the core

`pipx install --force code-jig==X.Y.Z` with the older version, set `core_version` back to `vX.Y.Z`, then `code-jig install` and `code-jig doctor`. From a checkout, check out the previous tag instead. Generated files are rewritten in place. Core skills that the older version does not ship are removed, unless you edited them by hand, in which case `install` leaves them and warns. Rules come from the overlay, so a core rollback does not change them.

Versions up to v0.12.0 were also released as `code-jig.zip` for the old `bootstrap.py` installer. Those zips stay on their release pages, but the installer is gone; install those versions from PyPI too (v0.12.0 is the first one there).

The tracker database survives a rollback: newer versions only add tables, so an older `Stop` hook keeps writing to it. Those tables (exit codes, skill reads, Guardian outcomes) stop updating until you upgrade again and run `collect --rescan`.

## What install touches

| Path | Notes |
|---|---|
| `$CODEX_HOME/AGENTS.md` | Generated. A hand-written file there is moved to `AGENTS.md.bak-<timestamp>` first. |
| `$CODEX_HOME/<tier>.config.toml` | One per tier. Same backup rule. |
| `$CODEX_HOME/rules/code-jig-*.rules` | From the overlay's `rules/`. `default.rules` is never touched. |
| `$CODEX_HOME/hooks.json` | The `Stop` entry with status `code-jig collect` is added or replaced; other hooks are kept. The previous file is copied to `hooks.json.bak-<timestamp>`. |
| `~/.local/share/code-jig/lib/` | The copy of code-jig the hooks run. |
| `~/.local/share/code-jig/usage.sqlite3` | Tracker data. Delete it to start counting from scratch; `collect --rescan` rebuilds it from the session files. |
| `~/code-jig-overlay` | Only `code-jig init-overlay` writes it, and only into a missing or empty folder. `install` never does. |
| `~/.agents/skills/<name>/` | Core and overlay skills, each with a `.code-jig` marker. A skill already there under the same name is moved to `~/.agents/skills-backup/`. |

`config.toml` is never edited.

## Uninstall

There is no uninstall command. To remove the pack:

1. Delete `$CODEX_HOME/AGENTS.md`, the four `<tier>.config.toml` files, `$CODEX_HOME/rules/code-jig-*.rules` and `~/.local/share/code-jig/`.
2. Remove the `Stop` entry whose `statusMessage` is `code-jig collect` from `hooks.json`, or restore the oldest `hooks.json.bak-*`.
3. Delete each skill directory in `~/.agents/skills/` that holds a `.code-jig` file.
4. Move back anything you want from `~/.agents/skills-backup/` and from `*.bak-*` files.
5. `pipx uninstall code-jig`. Delete `~/code-jig-overlay` only if you want to lose your overlay.
6. `codex plugin remove superpowers@openai-curated-remote` if you added it.
7. A machine that used the old `bootstrap.py` installer also has version folders `~/.local/share/code-jig/vX.Y.Z/` (`%LOCALAPPDATA%\code-jig\vX.Y.Z\`) and launchers `~/.local/bin/code-jig` and `cjig` (`%LOCALAPPDATA%\code-jig\bin\*.cmd`). Delete them; an old launcher earlier on `PATH` hides the pipx one.
