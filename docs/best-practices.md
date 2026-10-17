# OpenSSF Best Practices answers

Answers for <https://www.bestpractices.dev> (passing level). Project URL: <https://github.com/jeancarlos/code-jig>.

| Criterion | Answer | Evidence |
|---|---|---|
| description_good, interact, contribution, contribution_requirements | Met | README.md, CONTRIBUTING.md |
| floss_license, floss_license_osi, license_location | Met | Apache-2.0, LICENSE, LICENSES/Apache-2.0.txt |
| documentation_basics, documentation_interface | Met | README.md, docs/ |
| sites_https, delivery_mitm | Met | PyPI and github.com only, TLS; pip checks the PyPI sha256, and the wheel is signed and attested |
| discussion, english | Met | GitHub issues and pull requests, English |
| maintained | Met | commits every week |
| repo_public, repo_track, repo_interim, repo_distributed | Met | public git repository on GitHub |
| version_unique, version_semver, version_tags | Met | tags vX.Y.Z, VERSION |
| release_notes | Met | GitHub release notes, generated per tag |
| release_notes_vulns | N/A | no publicly known vulnerability fixed in a release so far |
| report_process, report_tracker, report_responses, enhancement_responses, report_archive | Met | GitHub issues, CONTRIBUTING.md |
| vulnerability_report_process, vulnerability_report_private, vulnerability_report_response | Met | SECURITY.md, GitHub private vulnerability reporting, 7 day acknowledgement |
| build, build_common_tools, build_floss_tools | N/A | pure Python; the wheel is built from `git archive` of the tag by `.github/scripts/build_wheel.py` with hash-locked build tools |
| test, test_invocation, test_most, test_continuous_integration | Met | `python -m unittest discover -s tests`, coverage floor 93 percent, .github/workflows/ci.yml |
| test_policy, tests_are_added, tests_documented_added | Met | CONTRIBUTING.md |
| warnings, warnings_fixed, warnings_strict | Met | ruff in CI, `code-jig analyze` |
| know_secure_design, know_common_errors | Met | CONTRIBUTING.md secure design paragraph |
| crypto_* | N/A | no cryptography implemented; sha256 from hashlib only |
| delivery_unsigned | Met | wheel, SBOM and SHA256SUMS signed with Sigstore, SLSA provenance; PyPI gets the attested wheel |
| vulnerabilities_fixed_60_days, vulnerabilities_critical_fixed | Met | none known open; osv-scanner in CI |
| no_leaked_credentials | Met | gitleaks over full history in CI |
| static_analysis, static_analysis_fixed, static_analysis_often | Met | CodeQL, ruff, zizmor on every push |
| dynamic_analysis, dynamic_analysis_enable_assertions | Met | ClusterFuzzLite with atheris, .github/workflows/fuzz.yml |
| dynamic_analysis_unsafe, dynamic_analysis_fixed | N/A | memory-safe language |
