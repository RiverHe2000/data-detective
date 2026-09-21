# Planned formative user study

**Status: not conducted.** This document is a future protocol, not evidence of usability gains. Automated browser runs, the scripted demonstration, benchmark cases and developer self-testing are not participant sessions.

## Question and participants

Can someone inspect the evidence, make one justified change and recover the original state without confusing a review candidate with a proven error?

Recruit **3–5 volunteers** who work with sales spreadsheets or are comfortable checking CSV data. Record prior spreadsheet/programming experience. Use public/synthetic fixtures only; request permission before recording a session and allow stopping without explanation. Each session should take approximately 20–30 minutes.

## Procedure

1. Give a two-minute introduction to the purpose and the three tabs, without demonstrating the task solution.
2. Ask the participant to open the duplicate case, identify what evidence they would need before removing records, and explain why two identical rows are not automatically a proven error.
3. Provide a written correction note confirming that data rows 137, 138 and 139 were accidentally copied. Ask them to preview their exclusion, state the exact amount change, save a version and find the original file.
4. Ask them to restore the pre-repair contents while keeping the saved repair in history, then export a report.
5. Open a date-error case and ask why the total and monthly amounts differ. If using the optional assistant, ask the participant to distinguish computed evidence from a suggested explanation.
6. Ask what felt unclear, what they would trust, and which action they would hesitate to take on their own data.

Rotate the duplicate/date task order when feasible to reduce familiarity effects. Keep the model mode consistent within each participant session and record it. Use fresh workspaces so earlier saved versions do not reveal solutions.

## What to record

| Measure | Definition |
|---|---|
| Task completion | Completed independently, completed with a hint, or not completed. |
| Time | Start of task instruction to verified outcome; separately note reading/thinking and technical waits when observable. |
| Incorrect action | Wrong row excluded, unreviewed save, wrong version restored, or failure to preserve required evidence. |
| Understanding | Participant can explain review versus error, preview versus save, and unassigned-month amount. |
| Assistance | Number and content of moderator hints; model enabled/disabled and any fallback. |
| Comments | Short participant quotes linked to observed behavior, with consent. |

Use a simple session sheet: participant pseudonym, experience, task order, app revision, model mode, outcome, elapsed time, hints, mistakes and comments. Do not store names or contact details with recordings/results.

## Interpretation and publication

Report each participant's outcomes and observed problems. With 3–5 participants, do not claim a population-level productivity improvement, a statistically established advantage over Excel, or model effectiveness from preference alone. There is no speedup baseline in this protocol.

Prioritize fixes when participants cannot recover from an error, mistake suggestions for confirmed facts, or save a change they did not understand. Preserve failures in the report. A later controlled comparison with spreadsheets needs a separate balanced-task protocol and additional participants.

Until sessions have happened, the results entry must remain: **0 participants; not yet run; no measured user benefit.**
