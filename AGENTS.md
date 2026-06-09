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
- Avoid adding user-visible entities or convenience sensors unless the task explicitly requires them.
- Do not change user-visible entity names or semantics as part of test-only work.
- Leave `examples/` alone unless the task is explicitly about examples or docs.
- Do not create analysis scripts for each specific day; instead, re-use and potentially adjust the existing ones.

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

Run at least:
- `scripts/check`


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
