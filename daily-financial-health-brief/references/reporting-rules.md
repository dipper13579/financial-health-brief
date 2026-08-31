# Reporting rules

Use these stakeholder-confirmed rules for the Daily Financial Health Brief.

## Reporting context

- The Aug. 12, 2026 operator meeting uses Aug. 11 as the reporting date and Aug. 10 as the prior business day.
- Business time zone is Eastern Time. Source cutoff is midnight ET; deliver the clearly labeled draft to the Operations owner by 8:30 a.m. ET.
- A prior business day skips weekends and bank holidays. A rerun refreshes all three sources together.

## Transactions and status

- Daily posted totals include only `posted` rows dated exactly on the reporting date.
- Month-to-date posted totals include `posted` rows from the first calendar day of the reporting month through the reporting date, inclusive.
- Confirmed negative posted amounts are source-authorized credits/corrections and reduce daily and month-to-date totals. Distinct transaction IDs are not duplicates merely because descriptions repeat.
- Pending and disputed activity never enters posted cash or budget totals. The unresolved queue contains all and only `pending` and `disputed` rows from month start through reporting date.
- Total known unresolved amounts separately by status. Keep an unknown amount blank, outside numeric totals, and explicitly escalated. Escalate any unrecognized status instead of guessing or silently excluding it.

## Comparisons and materiality

- Compare reporting-day versus prior-business-day posted totals both overall and by category.
- A monetary change is material only when its absolute amount is strictly greater than both USD 500 and 10% of the relevant nonzero baseline. Use unrounded values for tests and display money to cents.
- A zero or missing prior-day baseline has no inferred percentage. Label it new/previously inactive and escalate if unexpected.
- Compare month-to-date posted spend to the full, unprorated monthly budget. Show utilization, remaining budget, and variance (`MTD posted - budget`). Flag both favorable and unfavorable material variance using the same strict dual threshold with the monthly target as denominator.
- Treat zero, negative, or missing budget targets as invalid/unmapped and escalate to Finance; never divide by zero.

## Revenue and receivables

- Compare reporting-date and prior-business-day `collected_revenue` and `outstanding_balance`.
- Show `payment_plan_balance` as accounts-receivable context.
- Show `enrolled_students` and `past_due_accounts` as operational context. A count change is material only when absolute percentage change is strictly above 10% and absolute change is at least 5 units.
- Missing, duplicated, stale, mixed-version, unexpected-currency, or unexpected-unit revenue snapshots are not guessed or silently converted; route them to Finance.

## Validation, lineage, and approval

- Require unique transaction IDs; valid dates; recognized status/amount-state combinations; and complete reporting-date snapshots.
- Reconcile daily category sums to daily total and month-to-date category sums to month-to-date total. Label unmapped posted categories and escalate them.
- Record source names, viewer URLs, owners, source versions, fetch timestamps, and row counts.
- Operations owns the transaction ledger. Finance owns budget targets and the revenue snapshot. Route source discrepancies to the source owner before Operations reviews the draft.
- The Operations owner reviews and approves the final brief, spending changes, disputed-item treatment, and escalation decisions. The USD 700 duplicate-charge dispute and unknown-amount plumbing estimate remain human decisions.
- Automation is read-only: it may fetch, validate, calculate, and draft. It may not edit source systems, change statuses, approve/refuse refunds or disputes, initiate payments, add payment instructions, or make operational decisions.

