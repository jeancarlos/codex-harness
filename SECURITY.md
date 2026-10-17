# Security policy

## Reporting a vulnerability

Do not open a public issue for a vulnerability. Report it privately through GitHub: <https://github.com/jeancarlos/code-jig/security/advisories/new>.

Include the affected version (`code-jig --version`), the platform, and the smallest steps that reproduce the problem. A proof of concept helps but is not required.

## What counts

code-jig writes configuration for Codex and runs hooks on the user's machine. In scope:

- a way to make `install`, `init-overlay` or a hook write outside the paths it manages, run a command it should not, or skip a check it should run;
- a way to bypass the git gate, the watermark gate or the scrub of AI attribution;
- a secret or private overlay content that reaches the public core or a release asset;
- a weakness in how release assets are verified (`SHA256SUMS`, signatures, provenance), or a PyPI wheel that differs from the attested GitHub release asset.

Out of scope: vulnerabilities in Codex, Python or GitHub themselves, and issues that need an attacker who already controls the user's account or overlay.

## Response and disclosure

- Acknowledgement within 7 days.
- A status update at least every 14 days until the report is closed.
- A fix or a documented mitigation within 90 days of the report. Disclosure is coordinated with the reporter: the advisory is published once a fixed release exists, or after 90 days, whichever comes first.

## Supported versions

Only the latest release receives security fixes. Releases are published to PyPI and as GitHub releases, signed with Sigstore and carrying SLSA provenance; verify the wheel before installing, as described in the README. `-rc` pre-releases are for testing and receive no fixes.
