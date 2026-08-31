---
name: daily-financial-health-brief
description: Freshly read the transaction, budget, and revenue Google Sheets and generate normalized CSVs plus a read-only draft Daily Financial Health Brief. Use for the Kiddy Academy daily finance and operations reporting workflow.
---

# Daily Financial Health Brief

Generate a traceable, read-only management draft from three live Google Sheets. Never use committed deliverables, manually downloaded files, or a cache as input.

## Runtime

- Python 3.11 or newer; the implementation uses only the standard library.
- Network access to public viewer-only Google Sheets export endpoints.
- No Google credentials, cookies, API keys, or write access.

Read [references/reporting-rules.md](references/reporting-rules.md) before changing calculations, validation, escalation, or approval behavior.

## Run

From the repository root:

```bash
python daily-financial-health-brief/scripts/generate_report.py \
  --transactions-url "https://docs.google.com/spreadsheets/d/16HhjfR9uG1oUwSFNjQvAvU9Q9gVjzxL0ufBzTJe82v8/edit" \
  --budget-url "https://docs.google.com/spreadsheets/d/1pnHBrxWvZBDIQItyxhmaSUZBxF8VMYqo_fyN7JtgyA4/edit" \
  --revenue-url "https://docs.google.com/spreadsheets/d/1DToTpZtuwtVIdCPethZRe4T-y6mxGpWuivWSmR2XZt4/edit" \
  --reporting-date 2026-08-11 \
  --prior-business-day 2026-08-10 \
  --output-dir deliverables
```

The three source roles are explicit command arguments. URLs must be HTTPS Google Sheets URLs; the script extracts each spreadsheet ID and any supplied `gid`, performs a fresh CSV export request, and rejects login/error/HTML responses.

## Outputs

- `deliverables/normalized/transactions.csv`
- `deliverables/normalized/budget.csv`
- `deliverables/normalized/revenue.csv`
- `deliverables/report.md`

Before atomically replacing these files, the program prints one fetch-evidence line per source with its URL, spreadsheet ID, sheet export identity, UTC fetch time, source version, and fetched data-row count. The same evidence appears in `report.md`.

## Safe failure

All three live sources must fetch and validate in the same run. Missing columns, malformed or inconsistent business fields, duplicate keys, unsupported currency, missing required reporting snapshots, login/error responses, or any failed reconciliation cause a nonzero exit before new deliverables are published. The script never falls back to local CSVs, edits a source, changes a transaction state, approves a dispute, or creates payment instructions.

