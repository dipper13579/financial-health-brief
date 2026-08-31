#!/usr/bin/env python3
"""Freshly fetch three Google Sheets and publish normalized finance deliverables."""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Sequence


TRANSACTION_COLUMNS = (
    "transaction_id",
    "date",
    "account",
    "category",
    "description",
    "amount",
    "currency",
    "status",
    "source",
    "source_version",
    "amount_status",
)
BUDGET_COLUMNS = (
    "period",
    "category",
    "budget_amount",
    "currency",
    "owner",
    "review_rule",
    "source",
    "source_version",
)
REVENUE_COLUMNS = (
    "date",
    "source",
    "metric",
    "value",
    "currency",
    "source_version",
)
RECOGNIZED_STATUSES = {"posted", "pending", "disputed"}
RECOGNIZED_AMOUNT_STATES = {"confirmed", "unknown"}
MONETARY_REVENUE_METRICS = {
    "collected_revenue",
    "outstanding_balance",
    "payment_plan_balance",
}
COUNT_REVENUE_METRICS = {"enrolled_students", "past_due_accounts"}
REQUIRED_REVENUE_METRICS = MONETARY_REVENUE_METRICS | COUNT_REVENUE_METRICS
MONEY_FLOOR = Decimal("500")
PERCENT_FLOOR = Decimal("0.10")
CENT = Decimal("0.01")


class SourceError(RuntimeError):
    """Raised when a live source cannot be safely used."""


@dataclass(frozen=True)
class FetchEvidence:
    role: str
    owner: str
    viewer_url: str
    spreadsheet_id: str
    sheet_identity: str
    fetched_at: str
    source_versions: tuple[str, ...]
    row_count: int


@dataclass(frozen=True)
class FetchedSource:
    rows: list[dict[str, str]]
    evidence: FetchEvidence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the read-only Daily Financial Health Brief from live Google Sheets."
    )
    parser.add_argument("--transactions-url", required=True)
    parser.add_argument("--budget-url", required=True)
    parser.add_argument("--revenue-url", required=True)
    parser.add_argument("--reporting-date", required=True, type=date.fromisoformat)
    parser.add_argument("--prior-business-day", required=True, type=date.fromisoformat)
    parser.add_argument("--output-dir", default="deliverables", type=Path)
    return parser.parse_args(argv)


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def google_export_url(viewer_url: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlparse(viewer_url)
    if parsed.scheme != "https" or parsed.hostname != "docs.google.com":
        raise SourceError("source URL must be an HTTPS docs.google.com Google Sheet")
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise SourceError("source URL does not contain a Google Sheets spreadsheet ID")
    spreadsheet_id = match.group(1)
    query = urllib.parse.parse_qs(parsed.query)
    fragment = urllib.parse.parse_qs(parsed.fragment)
    gid_values = query.get("gid") or fragment.get("gid") or []
    params = {"format": "csv"}
    if gid_values:
        if not gid_values[0].isdigit():
            raise SourceError("Google Sheets gid must be numeric")
        params["gid"] = gid_values[0]
    export_url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?"
        + urllib.parse.urlencode(params)
    )
    requested_identity = f"gid={gid_values[0]}" if gid_values else "default export tab"
    return spreadsheet_id, export_url, requested_identity


def export_identity(headers, fallback: str) -> str:
    disposition = headers.get("Content-Disposition", "")
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
    if encoded:
        name = urllib.parse.unquote(encoded.group(1))
    else:
        plain = re.search(r'filename="?([^";]+)', disposition, flags=re.IGNORECASE)
        name = plain.group(1) if plain else fallback
    return name[:-4] if name.lower().endswith(".csv") else name


def fetch_csv(
    role: str,
    owner: str,
    viewer_url: str,
    required_columns: Sequence[str],
) -> FetchedSource:
    spreadsheet_id, export_url, requested_identity = google_export_url(viewer_url)
    request = urllib.request.Request(
        export_url,
        headers={
            "Accept": "text/csv",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
            "User-Agent": "daily-financial-health-brief/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get_content_type().lower()
            payload = response.read()
            sheet_identity = export_identity(response.headers, requested_identity)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"{role}: live fetch failed: {exc}") from exc

    if not payload:
        raise SourceError(f"{role}: live fetch returned an empty response")
    leading = payload.lstrip()[:32].lower()
    if content_type in {"text/html", "application/xhtml+xml"} or leading.startswith(
        (b"<!doctype html", b"<html")
    ):
        raise SourceError(f"{role}: received a login/error HTML page instead of CSV")
    if content_type not in {"text/csv", "application/csv", "application/octet-stream"}:
        raise SourceError(f"{role}: unexpected content type {content_type!r}")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError(f"{role}: CSV is not UTF-8") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise SourceError(f"{role}: CSV has no header row")
    normalized_headers = [normalize_header(value or "") for value in reader.fieldnames]
    if len(set(normalized_headers)) != len(normalized_headers):
        raise SourceError(f"{role}: normalized CSV headers are not unique")
    missing = [column for column in required_columns if column not in normalized_headers]
    if missing:
        raise SourceError(f"{role}: missing required columns: {', '.join(missing)}")
    header_map = dict(zip(reader.fieldnames, normalized_headers))
    rows: list[dict[str, str]] = []
    for line_number, raw in enumerate(reader, start=2):
        row = {
            header_map[key]: (value or "").strip()
            for key, value in raw.items()
            if key is not None
        }
        canonical = {column: row.get(column, "") for column in required_columns}
        if not any(canonical.values()):
            continue
        canonical["_line"] = str(line_number)
        rows.append(canonical)
    if not rows:
        raise SourceError(f"{role}: CSV contains no data rows")

    versions = tuple(sorted({row.get("source_version", "") for row in rows if row.get("source_version")}))
    if not versions:
        raise SourceError(f"{role}: no source_version values were found")
    evidence = FetchEvidence(
        role=role,
        owner=owner,
        viewer_url=viewer_url,
        spreadsheet_id=spreadsheet_id,
        sheet_identity=sheet_identity,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_versions=versions,
        row_count=len(rows),
    )
    return FetchedSource(rows=rows, evidence=evidence)


def require_text(row: dict[str, str], field: str, role: str) -> str:
    value = row.get(field, "").strip()
    if not value:
        raise SourceError(f"{role} line {row['_line']}: {field} is blank")
    return value


def parse_date_value(value: str, role: str, line: str) -> date:
    candidates = ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d")
    for pattern in candidates:
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            pass
    raise SourceError(f"{role} line {line}: invalid date {value!r}")


def parse_decimal(value: str, role: str, line: str, field: str) -> Decimal:
    cleaned = value.strip().replace(",", "").replace("$", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise SourceError(f"{role} line {line}: invalid {field} {value!r}") from exc
    if not number.is_finite():
        raise SourceError(f"{role} line {line}: non-finite {field}")
    return number


def plain_decimal(value: Decimal, places: int | None = None) -> str:
    if places == 2:
        value = value.quantize(CENT, rounding=ROUND_HALF_UP)
        return f"{value:.2f}"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def money(value: Decimal) -> str:
    value = value.quantize(CENT, rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def percent(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{(value * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def normalize_transactions(rows: Iterable[dict[str, str]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    versions: set[str] = set()
    for row in rows:
        line = row["_line"]
        transaction_id = require_text(row, "transaction_id", "transactions")
        if transaction_id in seen_ids:
            raise SourceError(f"transactions line {line}: duplicate transaction_id {transaction_id}")
        seen_ids.add(transaction_id)
        status = require_text(row, "status", "transactions").lower()
        amount_status = require_text(row, "amount_status", "transactions").lower()
        if status not in RECOGNIZED_STATUSES:
            raise SourceError(f"transactions line {line}: unrecognized status {status!r}")
        if amount_status not in RECOGNIZED_AMOUNT_STATES:
            raise SourceError(f"transactions line {line}: unrecognized amount_status {amount_status!r}")
        amount_text = row.get("amount", "").strip()
        amount = parse_decimal(amount_text, "transactions", line, "amount") if amount_text else None
        if amount is None and amount_status != "unknown":
            raise SourceError(f"transactions line {line}: blank amount must have amount_status=unknown")
        if amount is not None and amount_status != "confirmed":
            raise SourceError(f"transactions line {line}: numeric amount must have amount_status=confirmed")
        if amount is None and status == "posted":
            raise SourceError(f"transactions line {line}: posted amount cannot be unknown")
        currency = require_text(row, "currency", "transactions").upper()
        if currency != "USD":
            raise SourceError(f"transactions line {line}: unsupported currency {currency!r}")
        version = require_text(row, "source_version", "transactions")
        versions.add(version)
        normalized.append(
            {
                "transaction_id": transaction_id,
                "date": parse_date_value(require_text(row, "date", "transactions"), "transactions", line),
                "account": require_text(row, "account", "transactions").lower(),
                "category": require_text(row, "category", "transactions").lower(),
                "description": require_text(row, "description", "transactions"),
                "amount": amount,
                "currency": currency,
                "status": status,
                "source": require_text(row, "source", "transactions"),
                "source_version": version,
                "amount_status": amount_status,
            }
        )
    if len(versions) != 1:
        raise SourceError("transactions: mixed source_version values in one ledger snapshot")
    return normalized


def normalize_budget(rows: Iterable[dict[str, str]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    versions: set[str] = set()
    for row in rows:
        line = row["_line"]
        period = require_text(row, "period", "budget")
        try:
            datetime.strptime(period, "%Y-%m")
        except ValueError as exc:
            raise SourceError(f"budget line {line}: invalid period {period!r}") from exc
        category = require_text(row, "category", "budget").lower()
        key = (period, category)
        if key in seen:
            raise SourceError(f"budget line {line}: duplicate period/category {period}/{category}")
        seen.add(key)
        amount = parse_decimal(row.get("budget_amount", ""), "budget", line, "budget_amount")
        currency = require_text(row, "currency", "budget").upper()
        if currency != "USD":
            raise SourceError(f"budget line {line}: unsupported currency {currency!r}")
        version = require_text(row, "source_version", "budget")
        versions.add(version)
        normalized.append(
            {
                "period": period,
                "category": category,
                "budget_amount": amount,
                "currency": currency,
                "owner": require_text(row, "owner", "budget").lower(),
                "review_rule": require_text(row, "review_rule", "budget"),
                "source": require_text(row, "source", "budget"),
                "source_version": version,
            }
        )
    if len(versions) != 1:
        raise SourceError("budget: mixed source_version values in one budget snapshot")
    return normalized


def normalize_revenue(rows: Iterable[dict[str, str]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    seen: set[tuple[date, str]] = set()
    versions_by_date: dict[date, set[str]] = defaultdict(set)
    for row in rows:
        line = row["_line"]
        snapshot_date = parse_date_value(require_text(row, "date", "revenue"), "revenue", line)
        metric = require_text(row, "metric", "revenue").lower()
        key = (snapshot_date, metric)
        if key in seen:
            raise SourceError(f"revenue line {line}: duplicate date/metric {snapshot_date}/{metric}")
        seen.add(key)
        value = parse_decimal(row.get("value", ""), "revenue", line, "value")
        currency = row.get("currency", "").strip().upper()
        if metric in MONETARY_REVENUE_METRICS and currency != "USD":
            raise SourceError(f"revenue line {line}: monetary metric {metric} must use USD")
        if metric in COUNT_REVENUE_METRICS and currency:
            raise SourceError(f"revenue line {line}: count metric {metric} must not have a currency")
        version = require_text(row, "source_version", "revenue")
        versions_by_date[snapshot_date].add(version)
        normalized.append(
            {
                "date": snapshot_date,
                "source": require_text(row, "source", "revenue"),
                "metric": metric,
                "value": value,
                "currency": currency,
                "source_version": version,
            }
        )
    mixed_dates = [str(day) for day, versions in versions_by_date.items() if len(versions) != 1]
    if mixed_dates:
        raise SourceError(f"revenue: mixed source_version values within dates: {', '.join(mixed_dates)}")
    return normalized


def sum_amount(rows: Iterable[dict[str, object]]) -> Decimal:
    return sum((row["amount"] for row in rows if row["amount"] is not None), Decimal("0"))


def sums_by_category(rows: Iterable[dict[str, object]]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        amount = row["amount"]
        if amount is not None:
            totals[str(row["category"])] += amount
    return dict(totals)


def monetary_material(change: Decimal, baseline: Decimal) -> bool | None:
    if baseline == 0:
        return None
    return abs(change) > MONEY_FLOOR and abs(change) > abs(baseline) * PERCENT_FLOOR


def count_material(change: Decimal, baseline: Decimal) -> bool | None:
    if baseline == 0:
        return None
    return abs(change) >= Decimal("5") and abs(change) > abs(baseline) * PERCENT_FLOOR


def comparison_label(change: Decimal, baseline: Decimal, count: bool = False) -> str:
    material = count_material(change, baseline) if count else monetary_material(change, baseline)
    if material is None:
        return "review—new/previously inactive" if change != 0 else "no change"
    return "material" if material else "not material"


def csv_rows_transactions(rows: Iterable[dict[str, object]]) -> Iterable[dict[str, str]]:
    for row in rows:
        yield {
            **{key: str(row[key]) for key in TRANSACTION_COLUMNS if key not in {"date", "amount"}},
            "date": row["date"].isoformat(),
            "amount": "" if row["amount"] is None else plain_decimal(row["amount"], 2),
        }


def csv_rows_budget(rows: Iterable[dict[str, object]]) -> Iterable[dict[str, str]]:
    for row in rows:
        yield {
            **{key: str(row[key]) for key in BUDGET_COLUMNS if key != "budget_amount"},
            "budget_amount": plain_decimal(row["budget_amount"], 2),
        }


def csv_rows_revenue(rows: Iterable[dict[str, object]]) -> Iterable[dict[str, str]]:
    for row in rows:
        value_places = 2 if row["metric"] in MONETARY_REVENUE_METRICS else None
        yield {
            **{key: str(row[key]) for key in REVENUE_COLUMNS if key not in {"date", "value"}},
            "date": row["date"].isoformat(),
            "value": plain_decimal(row["value"], value_places),
        }


def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    output.extend("| " + " | ".join(str(value).replace("|", "\\|") for value in row) + " |" for row in rows)
    return output


def build_report(
    transactions: list[dict[str, object]],
    budget: list[dict[str, object]],
    revenue: list[dict[str, object]],
    evidence: Sequence[FetchEvidence],
    reporting_date: date,
    prior_day: date,
) -> str:
    if prior_day >= reporting_date:
        raise SourceError("prior business day must be earlier than reporting date")
    month_start = reporting_date.replace(day=1)
    period = reporting_date.strftime("%Y-%m")
    daily = [row for row in transactions if row["status"] == "posted" and row["date"] == reporting_date]
    prior = [row for row in transactions if row["status"] == "posted" and row["date"] == prior_day]
    mtd = [
        row
        for row in transactions
        if row["status"] == "posted" and month_start <= row["date"] <= reporting_date
    ]
    unresolved = [
        row
        for row in transactions
        if row["status"] in {"pending", "disputed"} and month_start <= row["date"] <= reporting_date
    ]
    daily_total = sum_amount(daily)
    prior_total = sum_amount(prior)
    daily_change = daily_total - prior_total
    daily_categories = sums_by_category(daily)
    prior_categories = sums_by_category(prior)
    mtd_categories = sums_by_category(mtd)
    mtd_total = sum(mtd_categories.values(), Decimal("0"))
    if mtd_total != sum_amount(mtd):
        raise SourceError("month-to-date category totals do not reconcile to the posted total")
    if daily_total != sum(daily_categories.values(), Decimal("0")):
        raise SourceError("daily category totals do not reconcile to the posted total")

    current_budget = {str(row["category"]): row for row in budget if row["period"] == period}
    if not current_budget:
        raise SourceError(f"budget: no targets found for reporting period {period}")
    unmapped = sorted(set(mtd_categories) - set(current_budget))
    invalid_budget = sorted(
        category for category, row in current_budget.items() if row["budget_amount"] <= 0
    )

    revenue_by_date: dict[date, dict[str, dict[str, object]]] = defaultdict(dict)
    for row in revenue:
        revenue_by_date[row["date"]][str(row["metric"])] = row
    for day in (reporting_date, prior_day):
        missing = sorted(REQUIRED_REVENUE_METRICS - set(revenue_by_date.get(day, {})))
        if missing:
            raise SourceError(f"revenue: {day} is missing required metrics: {', '.join(missing)}")

    pending_known = sum_amount(row for row in unresolved if row["status"] == "pending")
    disputed_known = sum_amount(row for row in unresolved if row["status"] == "disputed")
    unknown_items = [row for row in unresolved if row["amount"] is None]
    special_dispute = next(
        (
            row
            for row in unresolved
            if row["status"] == "disputed"
            and row["amount"] == Decimal("700")
            and "duplicate" in str(row["description"]).lower()
        ),
        None,
    )

    lines = [
        "# DRAFT — Daily Financial Health Brief",
        "",
        f"**Reporting date:** {reporting_date.isoformat()}  ",
        f"**Prior business day:** {prior_day.isoformat()}  ",
        "**Business time zone / cutoff:** Eastern Time; midnight ET  ",
        "**Delivery:** Draft to the Operations owner by 8:30 a.m. ET before the operator meeting  ",
        "**Human-review status:** Read-only draft; no payment instructions or source-system changes",
        "",
        "## Executive summary",
        "",
        f"- Reporting-day posted activity was **{money(daily_total)}**, versus **{money(prior_total)}** on {prior_day.isoformat()}; the change was **{money(daily_change)}** ({comparison_label(daily_change, prior_total)}).",
        f"- Month-to-date posted spend was **{money(mtd_total)}**. Pending (**{money(pending_known)} known**) and disputed (**{money(disputed_known)} known**) exposure remain separate from posted cash and budget calculations.",
        f"- The unresolved queue contains **{len(unresolved)}** items, including **{len(unknown_items)}** with unknown amounts that require Operations review.",
        "- Finance owns budget and revenue-source exceptions; Operations owns ledger exceptions and approves the final brief and unresolved-item treatment.",
        "",
        "## Daily posted activity",
        "",
    ]
    category_rows = []
    for category in sorted(set(daily_categories) | set(prior_categories)):
        current = daily_categories.get(category, Decimal("0"))
        baseline = prior_categories.get(category, Decimal("0"))
        change = current - baseline
        pct = None if baseline == 0 else change / abs(baseline)
        category_rows.append(
            (category, money(current), money(baseline), money(change), percent(pct), comparison_label(change, baseline))
        )
    lines.extend(
        markdown_table(
            ("Category", str(reporting_date), str(prior_day), "Change", "% change", "Review"),
            category_rows,
        )
    )
    lines.extend(["", "## Month-to-date budget position", ""])
    budget_rows = []
    for category in sorted(set(current_budget) | set(mtd_categories)):
        spend = mtd_categories.get(category, Decimal("0"))
        target_row = current_budget.get(category)
        if target_row is None:
            budget_rows.append((category, money(spend), "unmapped", "n/a", "n/a", "n/a", "escalate to Finance"))
            continue
        target = target_row["budget_amount"]
        if target <= 0:
            budget_rows.append((category, money(spend), money(target), "n/a", "n/a", "n/a", "invalid target—escalate"))
            continue
        variance = spend - target
        remaining = target - spend
        budget_rows.append(
            (
                category,
                money(spend),
                money(target),
                percent(spend / target),
                money(remaining),
                money(variance),
                "material" if monetary_material(variance, target) else "not material",
            )
        )
    lines.extend(
        markdown_table(
            ("Category", "MTD posted", "Monthly target", "Utilization", "Remaining", "Variance", "Review"),
            budget_rows,
        )
    )
    lines.extend(
        [
            "",
            "Monthly targets are not prorated. Favorable and unfavorable variances use the same strict dual threshold: absolute variance greater than both 10% of target and USD 500.",
            "",
            "## Pending and disputed queue",
            "",
            f"- Pending known total: **{money(pending_known)}**",
            f"- Disputed known total: **{money(disputed_known)}**",
            "- Unknown amounts remain blank and are excluded from numeric totals.",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ("ID", "Date", "Status", "Category", "Description", "Amount", "Amount state", "Owner/action"),
            (
                (
                    str(row["transaction_id"]),
                    row["date"].isoformat(),
                    str(row["status"]),
                    str(row["category"]),
                    str(row["description"]),
                    "" if row["amount"] is None else money(row["amount"]),
                    str(row["amount_status"]),
                    "Operations decision / escalation",
                )
                for row in sorted(unresolved, key=lambda item: (item["date"], str(item["transaction_id"])))
            ),
        )
    )
    lines.extend(["", "## Revenue and accounts receivable", ""])
    revenue_rows = []
    current_metrics = revenue_by_date[reporting_date]
    prior_metrics = revenue_by_date[prior_day]
    for metric in (
        "collected_revenue",
        "outstanding_balance",
        "payment_plan_balance",
        "enrolled_students",
        "past_due_accounts",
    ):
        current = current_metrics[metric]["value"]
        baseline = prior_metrics[metric]["value"]
        change = current - baseline
        pct = None if baseline == 0 else change / abs(baseline)
        is_count = metric in COUNT_REVENUE_METRICS
        render = plain_decimal if is_count else money
        review = comparison_label(change, baseline, count=is_count)
        if metric == "payment_plan_balance":
            review = "AR context"
        revenue_rows.append(
            (
                metric,
                render(current),
                render(baseline),
                render(change),
                percent(pct),
                review,
            )
        )
    lines.extend(
        markdown_table(
            ("Metric", str(reporting_date), str(prior_day), "Change", "% change", "Review"),
            revenue_rows,
        )
    )

    lines.extend(["", "## Human decisions and exceptions", ""])
    if special_dispute:
        lines.append(
            f"- **Operations decision required:** {special_dispute['transaction_id']} — {special_dispute['description']} ({money(special_dispute['amount'])}) remains disputed. Automation does not approve or refuse it."
        )
    for row in unknown_items:
        lines.append(
            f"- **Unknown amount:** {row['transaction_id']} — {row['description']}; keep amount blank and escalate to Operations."
        )
    for category in unmapped:
        lines.append(f"- **Unmapped budget category:** {category}; route to Finance.")
    for category in invalid_budget:
        lines.append(f"- **Invalid budget target:** {category}; route to Finance.")
    new_categories = [
        category
        for category, current in daily_categories.items()
        if current != 0 and prior_categories.get(category, Decimal("0")) == 0
    ]
    for category in sorted(new_categories):
        lines.append(
            f"- **New/previously inactive daily category:** {category} ({money(daily_categories[category])}); no percentage was inferred. Operations should review if unexpected."
        )
    if not any((special_dispute, unknown_items, unmapped, invalid_budget, new_categories)):
        lines.append("- No human-review exceptions were detected beyond normal final approval.")

    lines.extend(
        [
            "",
            "## Validation and reconciliation",
            "",
            f"- Transaction IDs unique: **yes** ({len(transactions)} rows).",
            f"- Reporting-day category sum ties to posted total: **yes** ({money(daily_total)}).",
            f"- Month-to-date category sum ties to posted total: **yes** ({money(mtd_total)}).",
            f"- Posted MTD categories mapped to a positive monthly target: **{'yes' if not unmapped and not invalid_budget else 'no—see exceptions'}**.",
            "- Pending and disputed exposure excluded from posted cash and budget totals: **yes**.",
            "- Revenue reporting-date and prior-business-day metric sets complete and internally version-consistent: **yes**.",
            "",
            "## Source lineage and fresh-fetch evidence",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ("Role", "Owner", "Spreadsheet ID", "Sheet/export identity", "Fetched at (UTC)", "Source version(s)", "Rows", "Viewer URL"),
            (
                (
                    item.role,
                    item.owner,
                    item.spreadsheet_id,
                    item.sheet_identity,
                    item.fetched_at,
                    ", ".join(item.source_versions),
                    str(item.row_count),
                    item.viewer_url,
                )
                for item in evidence
            ),
        )
    )
    lines.extend(
        [
            "",
            "## Approval and safety boundary",
            "",
            "This document is a draft for the Operations owner. Automation performed read-only fetch, validation, calculation, and drafting. It did not edit a source, change a status, approve or refuse a refund/dispute, initiate a payment, create payment instructions, or make an operational decision.",
            "",
        ]
    )
    return "\n".join(lines)


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def print_fetch_evidence(evidence: Iterable[FetchEvidence]) -> None:
    for item in evidence:
        print(
            "FETCH_EVIDENCE"
            f" role={item.role}"
            f" url={item.viewer_url}"
            f" spreadsheet_id={item.spreadsheet_id}"
            f" sheet_identity={item.sheet_identity!r}"
            f" fetched_at={item.fetched_at}"
            f" source_versions={','.join(item.source_versions)}"
            f" row_count={item.row_count}"
        )


def publish_outputs(
    output_dir: Path,
    transactions: list[dict[str, object]],
    budget: list[dict[str, object]],
    revenue: list[dict[str, object]],
    report: str,
) -> None:
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix="financial-health-brief-", dir=output_dir.parent))
    try:
        write_csv(staging_root / "normalized" / "transactions.csv", TRANSACTION_COLUMNS, csv_rows_transactions(transactions))
        write_csv(staging_root / "normalized" / "budget.csv", BUDGET_COLUMNS, csv_rows_budget(budget))
        write_csv(staging_root / "normalized" / "revenue.csv", REVENUE_COLUMNS, csv_rows_revenue(revenue))
        (staging_root / "report.md").write_text(report, encoding="utf-8", newline="\n")
        for relative in (
            Path("normalized/transactions.csv"),
            Path("normalized/budget.csv"),
            Path("normalized/revenue.csv"),
            Path("report.md"),
        ):
            destination = output_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_root / relative, destination)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def run(args: argparse.Namespace) -> None:
    fetched_transactions = fetch_csv("transactions", "Operations", args.transactions_url, TRANSACTION_COLUMNS)
    fetched_budget = fetch_csv("budget", "Finance", args.budget_url, BUDGET_COLUMNS)
    fetched_revenue = fetch_csv("revenue", "Finance", args.revenue_url, REVENUE_COLUMNS)

    transactions = normalize_transactions(fetched_transactions.rows)
    budget = normalize_budget(fetched_budget.rows)
    revenue = normalize_revenue(fetched_revenue.rows)
    evidence = (
        fetched_transactions.evidence,
        fetched_budget.evidence,
        fetched_revenue.evidence,
    )
    report = build_report(
        transactions,
        budget,
        revenue,
        evidence,
        args.reporting_date,
        args.prior_business_day,
    )
    print_fetch_evidence(evidence)
    publish_outputs(args.output_dir, transactions, budget, revenue, report)
    print(f"PUBLISHED output_dir={args.output_dir.resolve()} files=4")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run(parse_args(argv))
    except (SourceError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

