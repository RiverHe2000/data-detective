# Core verification

Status: **pass**.

This run inspected the current Python environment. It did not perform a clean install, create a virtual environment, download data, or run real model/GPU inference.

Python: `3.12.0` · `Windows-11-10.0.26200-SP0`.
System site packages inherited: `False`.

| Check | Result | Seconds |
|---|---|---:|
| installed_core | PASS | 0.775 |
| dependencies | PASS | 0.627 |
| lint | PASS | 0.145 |
| tests | PASS | 9.564 |
| case_evaluation | PASS | 0.231 |
| frozen_case_gate | PASS | — |

Pytest counts: `{"tests": 182, "failures": 0, "errors": 0, "skipped": 0}`.

The JSON receipt records source/case hashes, commands, environment, output hashes and test counts. Full per-command logs, JUnit XML and case evaluation files are retained beside it.
