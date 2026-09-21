# Frozen case evaluation

Baseline is a reference, not ground truth. Scores cover only explicit injections and stipulated synthetic controls.

**Scope:** Six development cases and six held-out cases. All twelve were frozen before detector evaluation. Known injected edits are labels; naturally occurring baseline findings remain unlabelled. Synthetic legitimate repeated rows and premium prices may warrant review, which is counted separately from erroneous error classification.

## dev (6 cases)

| Injection | Detected events | Detected target rows | Already flagged before edit | New target detections |
|---|---:|---:|---:|---:|
| date_corruption | 4/4 | 4/4 | 0 | 4 |
| numeric_corruption | 4/4 | 4/4 | 0 | 4 |
| copied_records | 2/2 | 6/6 | 0 | 6 |
| missing_value | 4/4 | 4/4 | 0 | 4 |
| price_spike | 2/2 | 2/2 | 0 | 2 |

| Stipulated normal control | Rows | Error flags | Review flags | Unexpected review flags |
|---|---:|---:|---:|---:|
| price_reference | 60 | 0 | 0 | 0 |
| legitimate_cancellation | 6 | 0 | 0 | 0 |
| multi_product_order | 12 | 0 | 0 | 0 |
| legitimate_repeat | 12 | 0 | 12 | 0 |
| legitimate_premium_price | 6 | 0 | 0 | 0 |

Oracle restoration: **6/6** cases restored baseline included row values exactly. Independent Decimal/ISO metric checks: **18/18**.

## test (6 cases)

| Injection | Detected events | Detected target rows | Already flagged before edit | New target detections |
|---|---:|---:|---:|---:|
| date_corruption | 4/4 | 4/4 | 0 | 4 |
| numeric_corruption | 4/4 | 4/4 | 0 | 4 |
| copied_records | 2/2 | 6/6 | 0 | 6 |
| missing_value | 4/4 | 4/4 | 0 | 4 |
| price_spike | 2/2 | 2/2 | 0 | 2 |

| Stipulated normal control | Rows | Error flags | Review flags | Unexpected review flags |
|---|---:|---:|---:|---:|
| price_reference | 60 | 0 | 0 | 0 |
| legitimate_cancellation | 6 | 0 | 0 | 0 |
| multi_product_order | 12 | 0 | 0 | 0 |
| legitimate_repeat | 12 | 0 | 12 | 0 |
| legitimate_premium_price | 6 | 0 | 0 | 0 |

Oracle restoration: **6/6** cases restored baseline included row values exactly. Independent Decimal/ISO metric checks: **18/18**.

## all (12 cases)

| Injection | Detected events | Detected target rows | Already flagged before edit | New target detections |
|---|---:|---:|---:|---:|
| date_corruption | 8/8 | 8/8 | 0 | 8 |
| numeric_corruption | 8/8 | 8/8 | 0 | 8 |
| copied_records | 4/4 | 12/12 | 0 | 12 |
| missing_value | 8/8 | 8/8 | 0 | 8 |
| price_spike | 4/4 | 4/4 | 0 | 4 |

| Stipulated normal control | Rows | Error flags | Review flags | Unexpected review flags |
|---|---:|---:|---:|---:|
| price_reference | 120 | 0 | 0 | 0 |
| legitimate_cancellation | 12 | 0 | 0 | 0 |
| multi_product_order | 24 | 0 | 0 | 0 |
| legitimate_repeat | 24 | 0 | 24 | 0 |
| legitimate_premium_price | 12 | 0 | 0 | 0 |

Oracle restoration: **12/12** cases restored baseline included row values exactly. Independent Decimal/ISO metric checks: **36/36**.

## Limits

- These small, purposely constructed cases are an engineering regression benchmark, not an estimate of real-world precision or recall.
- Original source rows are disjoint across cases and splits. Synthetic rows are labelled and are not historical UCI records.
- Duplicate rows and price spikes require review; the detector is not expected to infer business intent from values alone.
- Oracle repairs use hidden injection answers to validate repair mechanics; this does not measure autonomous repair accuracy.
- Source workbook, case hashes, row provenance and cell changes are recorded in the case manifests and corpus.lock.json.
