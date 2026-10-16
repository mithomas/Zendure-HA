# AGENTS.md

## Purpose
This is a HomeAssistant custom integration for Zendure devices.
Prefer small, behavior-focused changes and keep history clean.

## Environment
Use the local venv when present:
- `. .venv/bin/activate`
- `scripts/check`

## Editing Rules
- Preserve existing entity naming, translation, and Home Assistant patterns unless the change is explicitly about renaming or cleanup.
- Keep test helpers in `tests/components/zendure_ha/common.py`.
- Do not introduce a dependency from `device.py` to `manager.py`.
- Keep device-derived discharge and recovery state owned by the device.
- Let the manager consume device state rather than recompute it from back-references.
- For manager routing changes, preserve the pipeline approach: poll and classify device state, build routing snapshot facts once, shape the setpoint, build an intent, then execute from those prepared facts without recomputing policy from raw back-references.
- Avoid adding user-visible entities or convenience sensors unless the task explicitly requires them.
- Do not change user-visible entity names or semantics as part of test-only work.
- Leave `examples/` alone unless the task is explicitly about examples or docs.
- Do not create analysis scripts for each specific day; instead, re-use and potentially adjust the existing ones.
- **ASCII Typographic Characters**: Always write comments, documentation, and docstrings using standard ASCII hyphen-minus characters (`-`) instead of non-ASCII typographic symbols (like the en-dash `–` or Unicode minus sign `−`). This prevents linter warnings (such as `RUF002`/`RUF003`) and terminal rendering issues.
- **Arrows**: Always construct arrows using standard hyphens and the greater-than symbol (e.g., `->`) instead of Unicode characters (like `→`).

### Manager Routing Fixes
- For manager routing fixes, identify the specific source of surplus, deficit, blocking, or eligibility before changing allocation behavior. Do not generalize a taper, SoC, priority, or availability fix beyond the observed source unless tests cover the broader behavior.
- Prefer local routing facts and local variables inside the existing pipeline phase over new manager-level helpers when the calculation is used only for one narrow fix. Add a helper only when the logic is reused, meaningfully named as a domain concept, or needed to keep the phase readable.
- Keep shaping decisions in the setpoint-shaping phase and execution eligibility/capacity decisions in the execution phase. Do not move policy decisions across phases just to share code.
- When a fix admits a new allocation target, use the same eligibility predicate for target selection and capacity calculation, or precompute a named eligible set, so selection and capacity cannot drift.
- State the root cause narrowly in tests and commit messages: distinguish the observed source, such as selected-primary taper overflow, from the broader class, such as secondary eligibility.

## Analysis Scripts
The `analysis/` directory contains tools to process and interpret telemetry exports.
- `analyze_all_days.py`: The primary, comprehensive script for identifying patterns (e.g., unexpected secondary AC charging, primary output blockages, mode switches) across one or multiple CSV exports. By default, it scans for `export*.csv` in the current directory or a provided path.
- `run_analysis.py`: Contains core analysis logic, interval calculations, and metric extractions.
- `generate_markdown_table.py` & `generate_report.py`: Utilities for converting parsed metrics and episodes into structured markdown reports.


## Test Expectations
Do not weaken, remove, or substantially rewrite existing tests without prior confirmation.
For bug fixes, add or adjust a focused regression first and run it to confirm it fails before implementing the fix.

Prefer:
- parametrized tests for threshold and state-table logic
- explicit scenario tests for manager routing behavior
- expectation data or split tests instead of branchy assertions in test bodies

- If tests require Home Assistant fixtures, use the repo test harness rather than ad hoc scripts.
- Use patched setup smoke tests instead of real Bluetooth or Recorder setup.
- Prefer direct manager and device tests for algorithmic behavior.
- Keep manifest runtime requirements and `requirements.txt` aligned.
- Keep developer tooling in `requirements_dev.txt` and test-only dependencies in `requirements_test.txt`.
- Run tests through `scripts/test`; do not call `pytest` directly.
- Always run `scripts/test` and `scripts/check` outside the sandbox with escalated permissions. Sandboxed runs can stall during HomeAssistant fixture setup and eventually time out.
- Do not retry a sandboxed test run with a longer timeout. Stop the stalled run, then rerun the wrapper outside the sandbox.

Run at least:
- `scripts/check`


## Upstream Synchronization
- Prefer selective porting and surgical cherry-picking of upstream hardware models, bug fixes, translations, and dependency updates over rebasing or blind merging.
- Do not rebase `custom` onto `upstream/master`: the fork's extensive routing pipeline and history will cause cascading intermediate conflicts and require force-pushing.
- Preserve custom manager routing pipeline, routing snapshot facts, dwell times, and P1 spike coalescing; do not overwrite custom manager logic with upstream's simpler dispatch loops.
- Retain required fork runtime dependencies in `manifest.json` and `requirements.txt` (specifically `stringcase==1.2.0`).
- Align dependency bumps (`bleak-retry-connector`, `paho-mqtt>=2.1.0`, `ruff`, test collection pins) with upstream to maintain compatibility with newer HomeAssistant core releases.

### Tracking Upstream Changes Across Syncs
Because selective porting does not create a git merge commit, the git merge-base remains at the original fork point. To track what has already been reviewed and isolate newly added upstream commits in future syncs:
1. Place a git tag on the audited upstream commit:
   `git tag sync/upstream-YYYY-MM-DD <upstream-commit-sha>`
2. In subsequent syncs, fetch upstream and review only new commits:
   `git fetch upstream`
   `git log --oneline sync/upstream-YYYY-MM-DD..upstream/master`
3. Baseline ledger:
   - 2026-10-16: Upstream audited up to `8d59bac4` (PR #1701 / tag 1.4.4+). Tag: `sync/upstream-2026-10-16`.


## Commit Message Style
- Use lowercase commit subjects.
- Prefer the format `<type>[/scope]: <summary>`.
- Spell `HomeAssistant` as one word in commit messages.
- Use the existing prefixes already present in this repo, such as `feature/<area>`, `stability`, `fix`, `test`, and `code quality`.
- Keep the summary short and behavior-focused.
- Prefer a concise verb phrase that matches the existing history, for example `adding ...`, `converting ...`, `adjusting ...`, `gating ...`, `refactoring ...`, or `removing ...` for simple fixes.


## History Rules
- Before making any commit or history rewrite, check whether the change logically belongs in one of the existing commits on the current branch.
- Before creating any commit or performing any history rewrite, explicitly state which existing commit the change belongs in, or explicitly state that it belongs in a new commit.
- If it belongs in an existing commit, explicitly propose folding it into that commit before changing history or defaulting to a new commit.
- When adding tests, check whether they belong in the commit that introduced the tested behavior and propose that fold when appropriate.
- On every history rewrite, check `custom_components/zendure_ha/manifest.json` and restamp the `version` `-MTH-N-YYYYMMDDHHMMSS` marker if the rewritten commit sequence requires it.
- Add changes as new commits unless explicitly instructed otherwise.
