# dev-duplicate-dispatch

## Confirmed calculation contract

- Dataset: `09a46ee083e74fde80abb841d565a65a`; exported version: `db44f191075c462f8f4c171986f0f43e`.
- Original SHA-256: `996cd1e2fec58b58407b344eac0afb7b04c12cee0564cffdb207f1db8bd71f7b`.
- Field mapping: order_id → `InvoiceNo`; product_id → `StockCode`; quantity → `Quantity`; unit_price → `UnitPrice`; order_time → `InvoiceDate`.
- Currency: GBP; dates: `%Y-%m-%d %H:%M:%S`.
- Decimal separator: `.`; thousands separator: `(none)`.
- Net transaction amount is the signed sum of quantity × unit price; it is not accounting revenue.
- Cancellations are not negated again. Excluded rows remain in the version history.

## Calculated results

- Net transaction amount: **GBP 4593.97**.
- Included rows: 136; excluded rows: 3.
- Rows contributing to amount: 136; unparseable amounts: 0.
- Unassigned-month amount: GBP 0.00; invalid dates: 0.

| Month | Net transaction amount |
|---|---:|
| 2010-12 | 4568.47 |
| 2011-01 | 25.50 |

## Unresolved findings

Review candidates are not confirmed business errors. AI recommendations do not establish causality.

### Identical source records need review

`f_82ecb3c78b0aa7358b600289` · review · 2 affected rows.
Every source column matches. Repeated records may still be legitimate; select specific rows before exclusion. An order ID alone is never a duplicate key.

Row references: `996cd1e2fec58b58407b344eac0afb7b04c12cee0564cffdb207f1db8bd71f7b:134`, `996cd1e2fec58b58407b344eac0afb7b04c12cee0564cffdb207f1db8bd71f7b:135`

## Version history

| Version | Action | Reason |
|---|---|---|
| `45834974bff44b5787387f8dd0e7ccb9` | import | Original import |
| `db44f191075c462f8f4c171986f0f43e` | repair | Case correction note confirms only source rows 137-139 are accidental copies. |

The exported change log records exact cell edits, row exclusions and setting changes.
Processed CSV preserves source text. Reimport using the mapping and parse settings in this report.
The optional row-reference column is named _detective_row_id, with extra leading underscores if that name already exists.
Restores reference the immutable source version; they do not perform reverse arithmetic.
