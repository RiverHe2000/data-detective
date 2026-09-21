# Two-minute demonstration: inspect, preview, save, restore

This is a rehearsed product walkthrough using a public/synthetic fixture. It is not a user study or a demonstration of autonomous root-cause discovery. The existing video records the initial UI; the current UI adds pagination, direct row-number selection and an explicit **Prepare export files** step.

[Watch the two-minute walkthrough](assets/walkthrough.mp4). The video is an annotated sequence of actual application screenshots, not a real-time screen recording. Its scene timing presents the workflow and does not measure application latency or user task-completion time. The [video receipt](../reports/walkthrough.json) records the source screenshots, durations and hashes. The script below can also be performed live.

## Preparation

Run `scripts/start.ps1 -Port 8503` from the project root and open `http://127.0.0.1:8503`. Use a fresh investigation. Keep **Use the local AI model** off for this short walkthrough; model loading would obscure the core interaction.

Open **Duplicate Dispatch**, backed by `data/cases/dev-duplicate-dispatch/input.csv`. If the sample card is unavailable, upload that file and confirm `InvoiceNo`, `StockCode`, `Quantity`, `UnitPrice`, `InvoiceDate`; date format `%Y-%m-%d %H:%M:%S`, decimal `.`, no thousands separator, GBP.

The case manifest explicitly identifies extra copies at data-record indexes **137, 138 and 139**, copied from indexes 22, 35 and 48. In this demonstration, that manifest serves as the documented correction note. The application itself does not infer that these copies must be deleted.

## Walkthrough

| Time | Action | Suggested narration |
|---|---|---|
| 0:00–0:20 | Show **Investigate**, net amount and monthly chart. | “This file reports GBP 4,701.37. I want to understand which records affect that result and check a proposed change before saving it.” |
| 0:20–0:40 | Expand **Identical source records need review**. Compare original and copied rows. | “Every column matches in these groups. That is a lead, not proof of an error. The correction note confirms three accidental copies; other legitimate repeated records remain untouched.” |
| 0:40–1:05 | Go to **Repair lab**. Choose **Exclude selected rows → Source row numbers**, enter `137-139`, and add to the plan. Enter the reason below. | “I select the three documented copies explicitly. Exclusion preserves their original values and row references.” |
| 1:05–1:25 | Click **Preview combined impact**. Show totals, monthly comparison and changes. | “Nothing is saved yet. The combined change is minus GBP 107.40, taking the amount to GBP 4,593.97. Three rows stop contributing; their evidence remains.” |
| 1:25–1:40 | Check the review confirmation and click **Confirm & save version**. Open **History & export → Prepare export files**. | “Confirmation creates a new version. I can inspect the reason and download the processed data, full change log and report.” |
| 1:40–2:00 | Expand **Restore an earlier version**, select **Original import**, confirm, then **Restore version**. | “Restoring brings the amount back to GBP 4,701.37 as another version. The repair remains in history, and the original file is unchanged.” |

Suggested repair reason: `The case correction note confirms that data rows 137, 138 and 139 are accidental copies; preserve the other repeated records.`

If **Use these rows in repair lab** was selected for one finding, click **Clear lead selection** before selecting all three copied rows: a focused finding can expose only its own pair. Select the suffixes carefully; do not exclude the earlier source rows or the legitimate synthetic pair at indexes 134 and 135.

## Expected checkpoints

| State | Included / excluded | Net transaction amount |
|---|---:|---:|
| Imported fixture | 139 / 0 | GBP 4,701.37 |
| Preview and saved exclusion | 136 / 3 | GBP 4,593.97 |
| Restored original contents | 139 / 0 | GBP 4,701.37 |

The December bucket changes from GBP 4,675.87 to GBP 4,568.47; January remains GBP 25.50. These fixture amounts include clearly labelled synthetic controls. Do not present them as the unmodified UCI retailer's business totals.

For a longer technical discussion, show the preview's exact diff, the saved change log, immutable original download, source manifest and independent case evaluation. A successful walkthrough demonstrates the implemented workflow; it does not establish user time savings.

## Second experiment: change month coverage without changing the amount

Open **Broken Dates**, or upload `data/cases/dev-broken-dates/input.csv` using the same mapping and ISO date format. The initial amount is GBP 3,203.66, including GBP 37.20 without a month. Its correction manifest records the original date for rows 27 and 31 as `2011-03-15 13:23:00`.

In **Repair lab**, choose **Correct a cell value**, select those two rows, choose `InvoiceDate`, and enter that documented value. Add it to the plan, explain the source of the correction, then preview. The amount remains GBP 3,203.66; the unassigned amount becomes zero and March gains GBP 37.20. Clear the plan to demonstrate abandoning an experiment. [Verified preview](assets/12-date-preview.png).

Compare this result with the duplicate exclusion: changing date coverage and changing the total are distinct outcomes. Neither preview establishes the true business cause without external evidence.
