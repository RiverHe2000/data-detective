# Independent assistant task check

This is a reproducible application-service check, **not a participant session or
usability study**. The owner could not recruit participants and requested independent
assistant testing. Human participants: 0. The planned human study remains unrun.

The tasks below are fixed before execution. Use an empty temporary workspace and
the two existing development fixtures; they are known functional cases, not an
unseen accuracy benchmark. Use no model, credential or external service. The case
correction manifests supply the documented repair instruction; the application is
not expected to infer whether a repeated business transaction is erroneous.

1. Duplicate Dispatch: check the imported total GBP 4,701.37. Preview exclusion of
   documented accidental copies at rows 137–139. The expected total is GBP 4,593.97,
   a decrease of GBP 107.40. Preview must not save. Legitimate repeated rows 134–135
   must remain included.
2. Save the reviewed plan. Retry the same action identifier and require the same
   version, with no duplicate history entry. A fresh action against the stale
   original preview must be refused. Reopen the persisted workspace before export.
3. Export the repaired CSV, audit trail and report. Verify all 136 included source
   rows and all their cells independently with the standard CSV reader, not just
   the application's own totals. Require exactly the three intended exclusions,
   an intact reason, the original-file digest and selected-version identity.
4. Restore original contents as a new version. Require the original total, all
   original rows, three history entries, an explicit restore target in the audit
   trail, and byte-identical original download.
5. Broken Dates: correct only rows 27 and 31 to the manifest's recorded date
   `2011-03-15 13:23:00`. Preview must leave the total GBP 3,203.66 unchanged while
   moving GBP 37.20 from the unassigned bucket into March. Abandon the preview;
   persisted data and history must remain unchanged.

Record the source revision, fixture byte hashes, every checkpoint and export hashes.
On any unexpected exception or failed checkpoint, retain a failed receipt and exit
nonzero. Never count a repeated run as an additional participant or change expected
amounts to match application output. Runtime is not user task-completion time.

The check calls the application's storage, preview and export services directly.
It does not validate browser layout, user understanding, AI advice, adoption or time
savings. See the separate existing browser verification and planned [user study](USER_STUDY.md).
