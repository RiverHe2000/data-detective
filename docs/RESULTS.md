# Measured results and limits

The initial measurements below are retained as historical evidence. The later [optimization record](OPTIMIZATION.md) links current verification, a controlled serialization comparison and the v4 model smoke subset. Old full-corpus scores must not be presented as a fresh measurement of the changed model prompt.

The completed evidence covers deterministic checks, explicit repair and restoration, a real browser workflow, local performance, isolated installation, and optional Qwen calls. It does not establish autonomous diagnosis or user productivity gains.

## Checks and repair

The frozen corpus contains six development and six test cases with non-overlapping UCI source rows, deliberate mutations and labelled synthetic controls. The baseline is a reference, not a declaration that the original data are clean.

| Measurement | Result |
|---|---:|
| Known injected events detected with expected severity | 32 / 32 |
| Injected target rows detected | 40 / 40 |
| Error flags on stipulated normal control rows | 0 / 192 |
| Legitimate repeated control rows receiving a review flag | 24 / 24 |
| Oracle repairs restoring baseline included row values exactly | 12 / 12 |
| Independent Decimal/ISO financial checks | 36 / 36 |

The development and test halves each detected 16 events across 20 target rows. Exact duplicates and price spikes are **review** candidates, not proven business errors. Native baseline findings remain unlabelled; they are not counted as false positives. Oracle repairs use known injection answers to test the repair mechanism, not autonomous repair judgment. These deliberately small cases do not estimate production precision or recall. Sources: [full case report](../reports/evaluation.md), [machine-readable evidence](../reports/evaluation.json), [data provenance](../data/README.md).

## Performance and installation

On Windows 11 / Python 3.12.0, three runs over a **50,000-row, 1,534,440-byte synthetic fixture** produced:

| Operation | Median | Observed range |
|---|---:|---:|
| CSV parsing plus full analysis | 0.339 s | 0.310–0.351 s |
| Complete repair preview, excluding one selected row | 0.629 s | 0.593–0.664 s |

These are core execution measurements, excluding browser rendering, disk persistence and model inference. The fixture is not a real-user workload or a worst-case 20 MiB input, and three runs do not establish tail latency. [Raw performance results](../reports/performance.json).

An independently created Windows x64 / CPython 3.12 environment, with system and user site packages disabled, installed the development lock and passed **133 tests, zero skips**, `pip check`, and Ruff. The report confirms the complete installed dependency closure; the separate runtime lock also passed an independent resolver check. No GPU packages or real model calls were included in that clean environment. Locks pin package versions, not wheel hashes or isolated build-backend versions. [Clean-install receipt](../reports/clean-install.json).

The receipt retains an initial failed verification: UTF-8 mode exposed a locale-dependent Windows process-list decoding assumption in a timeout regression test. The test now inspects ASCII PID bytes; the final verification retained UTF-8 mode and skipped no checks.

After the final UI and packaging changes, the same isolated environment again passed all 133 tests, installation checks, Ruff and the twelve-case evaluation. The refreshed dependency locks contain the same package versions. The [final verification receipt](../reports/final-verification.json) binds this run to source and lock hashes; it records reuse of the isolated environment rather than claiming a second fresh installation. The configured GitHub Actions workflow has not been executed remotely.

## Optional Qwen: fallback remains necessary

Twelve real local calls used Qwen3-4B-Instruct-2507 through a separate Python 3.12 environment with PyTorch 2.11.0+cu128 and Transformers 5.16.1. Prompt version: `investigation-ranking-v3`. The scoring rubric concerns which existing deterministic rule to inspect first, not whether a free-text business explanation is correct.

| Measurement | Result |
|---|---:|
| Model attempts | 12 |
| Responses accepted by the output protocol | 6 / 12 |
| Conservative numeric-prose rejections, followed by rules fallback | 6 / 12 |
| Rules-only first-priority matches | 11 / 12 |
| Delivered product answers, including fallback: first-priority matches | 11 / 12 |
| Cold-call latency, median / maximum | 21.497 s / 23.022 s |

The six accepted model responses matched the first-priority rubric, but that selected subset is not an all-attempt success rate. Some rejected responses repeated actual raw evidence such as malformed numbers or invalid dates; the conservative rule forbids numeric details in model prose even when they came from the input. Rejection therefore does not establish a fabricated fact. No human semantic ratings were collected.

The final product ranking score equals the rules baseline, while model calls add substantial cold-start time. This run provides **no demonstrated AI benefit**. Latency includes process startup, model loading, generation and validation. Prompt v3 incorporates corrections made after earlier evaluations, and the test questions were reused; this is a diagnostic rerun, **not a fresh untouched holdout**. [Full responses, failures, environment and scores](../reports/advisor-evaluation.json).

## Browser workflow and presentation

A real-browser walkthrough exercised CSV upload and contract confirmation, evidence inspection, exclusion preview, save, download and restore. In the duplicate fixture, excluding documented extra copies at rows 137–139 changes GBP 4,701.37 to GBP 4,593.97; restoring the original contents returns GBP 4,701.37 while retaining the repair in history.

A second browser experiment restored the documented dates at rows 27 and 31 of Broken Dates. Its preview kept the net amount at GBP 3,203.66 while moving GBP 37.20 from the unassigned bucket into March. Clearing the plan left the saved version unchanged. [Date-only preview](assets/12-date-preview.png).

The live UI also completed a separate Qwen request in 16.6 seconds. It returned existing duplicate leads and cautious review advice, but placed a stipulated legitimate repeated pair first. Passing the output protocol establishes neither error diagnosis nor useful within-rule prioritization. This browser smoke check is separate from the twelve-question benchmark. [Actual UI response](assets/11-local-ai.png).

The [two-minute walkthrough video](assets/walkthrough.mp4) is an **annotated sequence of actual UI screenshots**, not a real-time screen recording. Its scene durations are editorial choices, not task-completion or application-latency measurements. [Scene and video receipt](../reports/walkthrough.json) · [demonstration script](DEMO_SCRIPT.md).

**User-study participants: 0.** Browser verification and developer demonstrations are not a user study. The [3–5 participant protocol](USER_STUDY.md) is planned work; no measured user time saving, improved comprehension, or real-world deployment outcome is claimed.
