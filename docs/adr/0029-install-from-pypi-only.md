# 0029: Install from PyPI only

Status: accepted (2026-10-17). Supersedes [0027](0027-install-from-a-release-url.md).

## Decision

`pipx install code-jig` is the only install path. The release zip, `bootstrap.py`, `install.sh`, `install.ps1`, the download Worker and every CI job for them are removed.

- **One wheel per release.** `release.yml` runs on a `v*.*.*` tag after CI passes. Its `publish` job builds the wheel once with `.github/scripts/build_wheel.py` and the hash-locked tools in `ci/build.txt`, writes an SPDX SBOM and `SHA256SUMS` (wheel and SBOM), signs those three files with `cosign sign-blob` and attests them with build provenance. The GitHub release carries the wheel, `SHA256SUMS`, the SBOM, one `.sigstore.json` bundle per signed file and `code-jig.intoto.jsonl`.
- **PyPI gets that same file.** The `pypi` job (environment `pypi`, `id-token: write`, read-only otherwise) downloads the wheel from the GitHub release, runs `gh attestation verify` against this repository, checks its sha256 against `SHA256SUMS`, and only then uploads it with Trusted Publishing. It never builds.
- **Release candidates.** A `vX.Y.Z-rcN` tag needs `VERSION = "X.Y.Z-rcN"` and makes a GitHub pre-release with the wheel. The `pypi` job is skipped for it.
- **Development builds.** `python3 .github/scripts/build_wheel.py` with no arguments builds the current version from `HEAD` into `dist/`; `pipx install` takes the file. `pip install -e .` runs the checkout itself: with no `_core` folder next to the code, `paths.locate_core` uses the repository root.
- **Digest covers the code.** `doctor.package_digest` hashes every file of the installed `codejig` package (the modules and `_core`: `core`, `skills`, `bench`, `overlay-examples`), skipping `__pycache__` and `_core/VERSION`, sorted by POSIX path, each path and content length-prefixed. `build_wheel.py` writes it into `_core/VERSION` next to the tag. With `core_writable = false`, `doctor` FAILs when any module or data file differs. It replaces the data-only digest the wheel had and the zip's `install.release_digest`.
- **Starter overlay.** The wheel ships `overlay-examples` in `_core`. `code-jig init-overlay [PATH]` copies the `personal` example to `PATH` (default `~/code-jig-overlay`), refuses a folder with files in it, and prints the next steps. Every command uses `~/code-jig-overlay` when neither `--overlay` nor `CODE_JIG_OVERLAY` is given and it has an `overlay.toml`. The `release` example, which only the bootstrap used, is gone.
- **The pin stays.** The overlay's `core_version` is compared by `doctor` with the installed version (the tag in `_core/VERSION`). A release bump changes `VERSION` and the examples' `core_version`, and a test keeps them equal. `pyproject.toml` installs both `code-jig` and `cjig`.

## Context

PyPI with pipx already installed the same files as the zip, from a signed and attested build, with no script piped into Python and no launcher of our own. Keeping two install paths meant two digests, two tag checks, a bootstrap that refused rc tags, a Worker to host, and CI on three systems for the zip alone. The owner chose one path.

The audits of 2026-10-17 found that the wheel's digest covered only the data, so `doctor` reported a clean core while the code that runs could differ, and that the `pypi` job rebuilt the wheel instead of uploading the one that was signed. Both are fixed here.

## Consequences

- Releases up to v0.12.0 keep their zips on the release pages; nothing installs them any more. v0.12.0 is the first version on PyPI.
- A machine that used the bootstrap keeps its old launchers in `~/.local/bin` until they are deleted; the upgrade runbook says how.
- The digest detects edits after install. Where the wheel came from is proven by the Sigstore signature and the attestation, not by the digest, which lives inside the tree it covers.
- `install.release_digest` has no caller left in `codejig` and can be deleted with the next change to `install.py`.

## Rejected

- Keeping the zip for machines without PyPI: the wheel on the GitHub release installs with `pipx install ./<file>` after the same verification.
- Uploading a fresh build to PyPI: it would not be the attested file.
