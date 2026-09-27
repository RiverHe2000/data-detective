# Independent assistant task results

**18/18 checkpoints passed; 0 participants; 0 model calls.** Executed September 27,
2026 through the real application services in a fresh temporary workspace. This
was not a browser run or usability study. It tests known development fixtures.

The [protocol](../../docs/INDEPENDENT_TASK_CHECK.md) was committed in `5c9149b` before
execution. The [receipt](receipt.json) records that application revision, the exact
new runner's byte hash, frozen fixture hashes, all checkpoints and export hashes.
The runner was added after the protocol commit; application code was unchanged.

| Task | Observed result |
|---|---|
| Preview documented duplicate removal | GBP 4,701.37 → GBP 4,593.97; GBP −107.40; no persisted change before save |
| Save and retry | One new version; same action identifier returned the same version; fresh stale action refused |
| Reopen and export | 136 included rows and every cell matched an independent standard-CSV selection from the original source |
| Audit retained | Exactly the three documented exclusions, their reason, original digest and selected version |
| Restore | All 139 original rows and GBP 4,701.37 restored; repair retained in three-entry history; original bytes unchanged |
| Abandon date correction | Preview kept GBP 3,203.66, moved GBP 37.20 to March; abandoning it left saved state unchanged |

Actual artifacts: [repaired data](repaired.csv), [repair audit](repair-audit.csv),
[investigation report](repair-report.md), [restored data](restored.csv),
[restoration audit](restored-audit.csv). Version identifiers and timestamps vary on
replay; the task invariants and source fixture hashes must remain the same.

Reproduce from the repository root, choosing a new output directory:

```powershell
.venv\Scripts\python.exe scripts/check_independent_tasks.py --output reports/task-check-replay
```

The runner refuses to overwrite prior outputs and writes a failed receipt on a
failed checkpoint. This check does not establish browser usability, AI benefit,
real-business correctness or user time savings. The documented case correction
note supplies the justification; duplicates alone do not justify deletion.
