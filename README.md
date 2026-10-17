# code-jig

[![CI](https://github.com/jeancarlos/code-jig/actions/workflows/ci.yml/badge.svg)](https://github.com/jeancarlos/code-jig/actions/workflows/ci.yml)
[![security](https://github.com/jeancarlos/code-jig/actions/workflows/security.yml/badge.svg)](https://github.com/jeancarlos/code-jig/actions/workflows/security.yml)
[![coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/jeancarlos/code-jig/badges/coverage.json)](https://github.com/jeancarlos/code-jig/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/jeancarlos/code-jig)](https://github.com/jeancarlos/code-jig/releases/latest)
[![license](https://img.shields.io/github/license/jeancarlos/code-jig)](LICENSE)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/jeancarlos/code-jig/badge)](https://scorecard.dev/viewer/?uri=github.com/jeancarlos/code-jig)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)
![dependencies: none (stdlib)](https://img.shields.io/badge/dependencies-none%20(stdlib)-brightgreen)

**Make the official Codex CLI cheaper, safer and smarter, by configuration alone.**

code-jig is an open-source config pack for [OpenAI Codex](https://github.com/openai/codex). One command turns a stock install into a tuned setup: model tiers, automatic command review, a local token tracker, a set of battle-tested skills and a knowledge base that grows with you. No proxy, no daemon, no fork, no extra binaries. Just Python's standard library and the settings Codex already ships.

## Why I built it

I use coding agents every day, and I kept running into the same four problems.

**1. Tokens disappear and nobody can say where.** Input, not output, is the bill: every request re-sends the whole conversation. I wanted to see exactly where it goes (which sessions, which commands, how much the safety reviews cost) without sending a byte to anyone. So code-jig reads Codex's own session files into a local SQLite database after every turn. Its totals match Codex's own per-thread count and [ccusage](https://github.com/ryoppippi/ccusage) to the token.

**2. Safety that asks you a hundred times a day gets switched off.** Codex ships Guardian, a reviewer model that approves or denies commands leaving the sandbox. code-jig turns it on in every tier and lets you *add* local policy on top of the upstream rules instead of replacing them. You stay protected, and you stop clicking "approve".

**3. Tips are cheap, measurements are not.** Every choice here was tested on a real bench before it shipped, and some popular ideas did not survive. An output-compression layer I used to swear by turned out to save little; Codex's own `tool_output_token_limit` already covered what mattered, so it went. One Guardian review costs about 7,000 input tokens on the bench, 84% of them cached, so I know what safety costs instead of guessing.

**4. A setup should get better with use, and travel.** Lessons learned on a laptop usually die in that laptop's shell history. Here they become rules, Guardian policy, skills or knowledge facts in a small **overlay**, reviewed like code. The shared **core** stays clean and versioned; every machine gets its own overlay; nothing private ever flows back into the core.

## What you get

- **Tiers**: `default`, `deep`, `bulk`, `review`, each a generated Codex profile with its own model and reasoning effort. Pick the right brain for the job; [daily use](docs/runbooks/daily-use.md#which-model-for-which-job) maps jobs and skills to models for a public and an enterprise catalog.
- **Cheap subagents**: an `explorer` role on the `bulk` model answers lookups with line ranges instead of file dumps, a `reviewer` role on the `review` model attacks plans and findings, and a `judge` role on the `deep` model decides disputes. Code-writing subagents keep the session's model.
- **Guardian everywhere**, with your additions layered on the upstream policy.
- **Token tracker**: `code-jig tokens` shows input, cache hit, Guardian share, escalated commands, the heaviest sessions and per-model usage. All local.
- **Evidence**: `code-jig evidence` compares commits made in Codex sessions with the same repos' other commits (rework, `analyze` findings) and reports test runs, skill use, Guardian decisions and cost. Local data only.
- **Cross-family review**: `code-jig cross-review` sends your diff to a model from another family, read-only, before you push.
- **Sonar-style analysis without a server**: `code-jig analyze` runs the analyzers you already have (lizard, ruff, vulture, mypy, eslint with sonarjs, knip, tsc, dotnet build with SonarAnalyzer and NuGet audit, semgrep, gitleaks, osv-scanner) and groups the findings like Sonar: security, secrets, dependencies, bugs, types, unused code, complexity, duplication.
- **Requirement tracing**: `code-jig trace` checks which requirement IDs (from Azure DevOps, GitHub issues or a Markdown design) have tests, and which tests cite IDs that no longer exist.
- **Skills**: `cross-review`, `grilling` (stress-test a decision), `web-consensus` (settle facts against live sources), `improve` (turn a lesson into a durable change), and the deterministic toolbox: `code-analysis`, `regression-tests`, `hld-trace`, `bug-hunt` (property tests, fuzzing, mutation testing), `codegen` (OpenAPI clients, codemods) and `skill-validate`. Plus [superpowers](https://github.com/obra/superpowers) in one command.
- **Knowledge base**: one fact per file, indexed, pointed at by the generated AGENTS.md.
- **Sourced research**: `web-consensus` runs researchers, challenger and judge on the model each job needs, and `code-jig check-sources` confirms every quoted sentence is really on the cited page.
- **No AI attribution, no unreviewed prose**: an opt-in git gate strips AI co-author lines from commit messages, removes invisible characters from staged files, refuses agent-written docs until you commit them yourself, cleans attribution from the commits you are about to push (then asks you to push again), and refuses pushes that fail your test command.
- **Skills proven on Codex**: `code-jig lint-skill` and the `skill-validate` skill check a skill's text; `code-jig skill-check --review --live` has Codex review it and run its test cases, and CI refuses a skill whose stamp is not current.
- **Edit feedback**: after every agent write, syntax errors and undefined names in Python, JSON and TOML, and a notice when a test is deleted or skipped, go straight back to the model. Nothing blocks.
- **Doctor**: checks the binary, the pins, every tier's model against your account's catalog, the subagent roles, managed requirements, the AGENTS.md budget and the size of the skills list sent with every request, and probes each tier for free.
- **Works on locked-down machines**: native Windows support, no scripts required, respects administrator-managed Codex config and tells you how it differs from what was tested.

## Quick start

Requires Python 3.11+, git and [pipx](https://pipx.pypa.io/).

```sh
pipx install code-jig
code-jig init-overlay
code-jig install
code-jig doctor
codex --profile default
```

`init-overlay` writes a starter overlay to `~/code-jig-overlay` (pass a folder to put it elsewhere, then point `CODE_JIG_OVERLAY` or `--overlay` at it). Edit its `profiles.toml` to pick your models; `doctor` lists the ones your account can use. See [personalizing](docs/personalizing.md). On Windows, run the same commands in PowerShell after `py -3 -m pip install --user pipx` and `py -3 -m pipx ensurepath`.

The wheel is published to PyPI from CI with Trusted Publishing, and it is the same file as the one on the GitHub release (CI checks the attestation and the sha256 before uploading).

### Upgrade and pin

`pipx upgrade code-jig` moves to the latest release; `pipx install --force code-jig==X.Y.Z` installs one version. The overlay's `core_version` is a pin: `doctor` compares it with the installed version, so bump it with every upgrade or rollback, then run `code-jig install` and `code-jig doctor`. See [upgrade and rollback](docs/runbooks/upgrade-and-rollback.md).

### Development build

To try a change before it is released, build a wheel from your checkout (it packs the committed `HEAD`):

```sh
python3 -m pip install --require-hashes -r ci/build.txt   # in a venv
python3 .github/scripts/build_wheel.py                    # writes dist/code_jig-<version>-py3-none-any.whl
pipx install --force dist/code_jig-*.whl
```

Or bump `VERSION` and the overlay examples' `core_version` to `X.Y.Z-rcN` and push a `vX.Y.Z-rcN` tag. That makes a signed GitHub pre-release with the wheel and never publishes to PyPI; install it with `pipx install --force <wheel URL from the pre-release>`.

To change the core itself, `pip install -e .` in a venv runs the checkout directly: with no packaged data next to the code, code-jig uses the repository root as its core.

## Documentation

| Read | When |
|---|---|
| [Daily use](docs/runbooks/daily-use.md) | Tiers, skills, reviews, the repo's own AGENTS.md |
| [Doctor](docs/runbooks/doctor.md) | What every check means and how to fix it |
| [Tokens](docs/runbooks/tokens.md) | Reading the usage report |
| [Evidence](docs/runbooks/evidence.md) | Whether code-jig pays off: rework, tests, skills, cost |
| [Code quality](docs/runbooks/code-quality.md) | `analyze`, `trace`, the toolbox skills, installing the tools |
| [Managed machines](docs/runbooks/managed-machine.md) | Installing where someone else administers the machine |
| [Network and certificates](docs/runbooks/network-and-certificates.md) | TLS-inspecting networks, git and CLI tools in the sandbox |
| [Circuit breaker](docs/runbooks/circuit-breaker.md) | When Guardian stops a turn |
| [Commit attribution](docs/runbooks/commit-attribution.md) | Codex's co-author trailer and how to turn it off |
| [Watermarks, humanizing and edit feedback](docs/runbooks/watermark-and-humanize.md) | What the write hook strips from agent output, what it reports back to the model, and how to run or disable it |
| [Upgrade and rollback](docs/runbooks/upgrade-and-rollback.md) | Updates, rollbacks, uninstalling |
| [Personalizing](docs/personalizing.md) | Overlay slots, pinning, the clean-room rule |
| [Decisions](docs/adr/) | Why each design choice was made, and what was rejected |
| [Contributing](CONTRIBUTING.md) | Code rules and tests |
| [Editing the deck](docs/deck/README.md) | The presentation and its data files |

## Principles

- **Configuration, not interception.** code-jig writes files Codex reads. It never sits between you and the model.
- **Measure, then decide.** Bench data in the repo, never guesses.
- **Additive safety.** Local policy extends upstream policy; it never replaces it.
- **Clean-room overlays.** Private context stays in private overlays. The core carries only what anyone could use.

## Verify a release

Each GitHub release carries the wheel, `SHA256SUMS`, an SPDX SBOM (`code-jig.spdx.json`), a Sigstore bundle per file (`<file>.sigstore.json`) and the SLSA build provenance (`code-jig.intoto.jsonl`). Download the wheel and its bundle from the release page (`pip download code-jig==X.Y.Z --no-deps` gets the same file from PyPI), then:

```
gh attestation verify code_jig-X.Y.Z-py3-none-any.whl --repo jeancarlos/code-jig
cosign verify-blob code_jig-X.Y.Z-py3-none-any.whl --bundle code_jig-X.Y.Z-py3-none-any.whl.sigstore.json --certificate-identity-regexp '^https://github.com/jeancarlos/code-jig/' --certificate-oidc-issuer https://token.actions.githubusercontent.com
pipx install ./code_jig-X.Y.Z-py3-none-any.whl
```

After install, `doctor` with `core_writable = false` checks every packaged module and data file against the digest in the wheel's `VERSION` file. That digest catches edits after install; the signature and the attestation are what prove where the wheel came from.

## License

Apache-2.0.
