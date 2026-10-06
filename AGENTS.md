# Agent Notes for jevclip

## Project overview
- `jevclip` is a small Python CLI package. The console entry point is `jevclip = jevclip.cli:main`.
- Main commands:
  - `jevclip run ...` judges transcript segments, writes reports, and can cut highlight/full videos.
  - `jevclip snu ...` derives Semantic Narrative Units by judging boundaries, writes `snus.json` and `snu-report.md`.
- Keep release asset names neutral: `jevclip-linux-x86_64`, `jevclip-macos-arm64`, plus `.sha256` and `.json` metadata files. Do not use Korsakow branding in this repo or release assets.

## Development and verification
- Run the full test suite before release:
  ```bash
  python -m unittest tests.test_jevclip -v
  ```
- `pytest` is not required; the repository test suite is standard-library `unittest`.
- The binary release workflow also runs `python -m unittest discover -s tests` on GitHub Actions before building assets.
- Failure/partial-success semantics matter for downstream adapters: CLI commands must exit nonzero if any input was skipped/failed or any Jev judgment remains undecided/error. Do not let fault states look like successful runs.

## Versioning and release process
1. Choose the next semver-ish tag (`v0.1.2`, etc.).
2. Update both version declarations:
   - `pyproject.toml` -> `[project].version`
   - `jevclip/__init__.py` -> `__version__`
3. Run the full test suite locally.
4. Commit changes on the release branch and push to `fork`.
5. Create and push an annotated tag:
   ```bash
   git tag -a vX.Y.Z -m "jevclip vX.Y.Z"
   git push fork vX.Y.Z
   ```
6. Pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds Linux x86_64 and macOS arm64 binaries and publishes/updates the GitHub release.
7. Verify the workflow run completes successfully, then read back the GitHub release.
8. Download release assets into a clean directory, verify checksums, mark the Linux binary executable, and smoke-test:
   ```bash
   gh release download vX.Y.Z --repo al-munazzim/jevclip --dir /home/ubuntu/tmp/jevclip-release-vX.Y.Z-verify
   cd /home/ubuntu/tmp/jevclip-release-vX.Y.Z-verify
   sha256sum -c jevclip-linux-x86_64.sha256
   chmod +x jevclip-linux-x86_64
   ./jevclip-linux-x86_64 --help
   ./jevclip-linux-x86_64 snu --help
   ```
9. Report the release URL, tag target commit, asset list, checksum verification, and smoke-test result.

## GitHub/repository notes
- Preferred writable remote in this environment: `fork` (`git@github.com:al-munazzim/jevclip.git`).
- Upstream remote: `origin` (`https://github.com/cclank/jevclip.git`).
- Use `gh release view/list/download --repo al-munazzim/jevclip` for release verification.
