# Case data and provenance

These small, redistributable cases derive from **Chen, D. (2015). Online Retail [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C5BW33.**

Source: [UCI dataset page](https://archive.ics.uci.edu/dataset/352/online+retail). License: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/). This project adapts and redistributes attributed excerpts; the original author does not endorse the modifications.

The official archive and full workbook are downloaded only to the ignored `data/private/` directory. Its `source_receipt.json` records the download URL, timestamp, byte sizes and SHA-256 of both files. Each distributed case repeats the attribution, license and source hashes, so provenance does not require the private download.

## Reproduction

From the repository root, using its installed Python environment:

```text
python scripts/fetch_corpus.py
python scripts/build_cases.py
python scripts/evaluate.py
```

Fetching verifies an existing receipt instead of silently updating data. Building reads twelve predetermined, non-overlapping windows of 120 original data rows, beginning at rows 1, 40001, 80001, …, 440001. Source numbering is one-based, excluding the header; Excel worksheet rows are one higher. The first six windows form development cases, and the last six form held-out cases. Datetimes are serialized as `YYYY-MM-DD HH:MM:SS`; numeric values remain decimal strings. No detector is imported by the case builder.

Every case contains `baseline.csv`, `input.csv` and `manifest.json`. The manifest records every original row's source location, all synthetic additions, each injected cell edit or copied row, known answers and inverse repair operations. `corpus.lock.json` pins every artifact. Regeneration fails if any existing case would change, including development cases. Create a separately versioned corpus for future changes instead of quietly retuning the held-out cases.

## What the labels mean

**Baseline is not ground truth and is not declared clean.** It is a small excerpt of naturally messy retail data plus explicitly described controls. Native duplicates, unusual prices and missing values can already exist. Evaluation therefore reports baseline findings and separates newly detected edited rows from pre-existing findings. It does not infer full-dataset precision from unlabelled original rows.

Known injections cover copied records, missing required values, numeric corruption, invalid dates and price spikes. Price spikes and exact duplicates are **review** cases, not necessarily business errors. The inverse operations are an oracle for checking repair mechanics; they are not available to the detector and do not measure autonomous repair intelligence.

Each baseline adds 16 labelled synthetic rows: 10 reference prices for the selected real product, one cancellation with negative quantity, two different products in one order, two stipulated legitimate identical sale events, and one premium product. The ten reference prices provide enough comparison observations for the outlier rule; they are not claimed to be historical UCI sales. The duplicate normal control intentionally cannot be distinguished from an accidental duplicate using CSV values alone. A duplicate review is appropriate; an automatic error verdict or automatic deletion is not. Reviews and errors are reported separately.

The benchmark is small and deliberately constructed, so its score demonstrates regression behavior on known edits rather than estimating production accuracy. Original data rows do not overlap across either cases or development/test splits. Synthetic controls share a construction method and are not independent real-world samples.

## Featured demonstrations

- `dev-duplicate-dispatch`: duplicated records change totals; preview exclusion and see the exact amount delta.
- `dev-broken-dates`: invalid dates move otherwise valid amounts out of monthly totals.
- `dev-price-spike`: a large intentional price edit asks for review without automatically changing data.

All fields preserve the UCI names. Required mapping: `InvoiceNo`, `StockCode`, `Quantity`, `UnitPrice`, `InvoiceDate`; currency GBP. The full workbook remains private to the checkout; the 12 small derived cases may be redistributed under the attribution above.
