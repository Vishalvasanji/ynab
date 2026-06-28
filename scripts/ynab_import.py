#!/usr/bin/env python3
"""
ynab_import.py — load a bank-export CSV into a YNAB budget via the YNAB API.

Runs once a month against a fresh bank export. Safe to re-run: overlapping
exports never create duplicates because every transaction carries a
deterministic YNAB `import_id`.

Standard library only (no pip dependencies) for portability.

SECURITY: the YNAB access token is a long-lived read/write secret. This script
reads it ONLY from the YNAB_ACCESS_TOKEN environment variable. It never reads a
token from a file in the repo, never prints it, and never writes it to disk.
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API_BASE = "https://api.ynab.com/v1"
DEFAULT_CONFIG = Path.home() / ".config" / "ynab-import" / "config.json"
PAYEE_MAX = 50
MEMO_MAX = 200
IMPORT_ID_MAX = 36

# US state codes, for trimming a single trailing " XX" off a cleaned payee.
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

TRANSFER_RE = re.compile(r"\bTRANSFER\s+From\s+account#", re.IGNORECASE)

# Leading payee-prefix patterns to strip (case-insensitive), in order.
PAYEE_PREFIXES = [
    re.compile(r"^DEBIT CARD DEBIT\s+", re.IGNORECASE),
    re.compile(r"^WITHDRAWAL POS\s+\d+\s+\d+\s+\d+\s+", re.IGNORECASE),
    re.compile(r"^ELECTRONIC WITHDRAWAL\s+", re.IGNORECASE),
    re.compile(r"^MISCELLANEOUS DEBIT\s+", re.IGNORECASE),
    re.compile(r"^WITHDRAWAL\s+POS\s+", re.IGNORECASE),
]


# --------------------------------------------------------------------------- #
# CSV reader — isolated so that a different bank's columns only touch this fn.
# --------------------------------------------------------------------------- #
def read_bank_csv(path):
    """Read the bank export. Yields dicts with raw string fields:
    {date, type, description, debit, credit, check_number}.

    Columns expected (header row present), in order:
        Date | Type | Description | Debit | Credit | CheckNumber
    """
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = None
        for raw in reader:
            if not raw or all((c or "").strip() == "" for c in raw):
                continue  # skip blank lines
            if header is None:
                header = raw  # consume the header row
                continue
            # Pad short rows so trailing empty columns don't IndexError.
            cells = list(raw) + [""] * (6 - len(raw))
            rows.append({
                "date": cells[0].strip(),
                "type": cells[1].strip(),
                "description": cells[2].strip(),
                "debit": cells[3].strip(),
                "credit": cells[4].strip(),
                "check_number": cells[5].strip(),
            })
    return rows


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def parse_date(raw):
    """M/D/YYYY (non-zero-padded) -> ISO YYYY-MM-DD. None if unparseable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%m/%d/%Y")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d")


def parse_money(raw):
    """Strip everything but digits, dot, minus. Return float or None if empty."""
    raw = (raw or "").strip()
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", raw)
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def to_milliunits(dollars):
    return int(round(dollars * 1000))


def clean_payee(description, check_number):
    """Strip bank boilerplate, leaving a recognizable merchant name."""
    desc = (description or "").strip()
    upper = desc.upper()

    # Special cases first.
    if upper == "WITHDRAWAL-CASH":
        return "Cash Withdrawal"
    if upper == "SHARE DRAFT":
        num = (check_number or "").strip()
        return f"Check #{num}" if num else "Check"
    if upper.startswith("DEPOSIT"):
        return "Deposit"
    if upper == "DIVIDEND":
        return "Dividend"

    # Strip a leading boilerplate prefix (first match wins).
    cleaned = desc
    for pat in PAYEE_PREFIXES:
        new = pat.sub("", cleaned)
        if new != cleaned:
            cleaned = new
            break

    # Collapse whitespace.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Trim one trailing 2-letter US state code.
    parts = cleaned.split(" ")
    if len(parts) > 1 and parts[-1].upper() in US_STATES:
        cleaned = " ".join(parts[:-1]).strip()

    return cleaned or desc


# --------------------------------------------------------------------------- #
# import_id (dedup)
# --------------------------------------------------------------------------- #
def build_import_ids(txns):
    """Assign YNAB-native import_ids. txns must already be in stable order."""
    seen = {}
    for t in txns:
        key = (t["amount_milli"], t["date"])
        seen[key] = seen.get(key, 0) + 1
        t["import_id"] = f"YNAB:{t['amount_milli']}:{t['date']}:{seen[key]}"


# --------------------------------------------------------------------------- #
# YNAB API client (urllib)
# --------------------------------------------------------------------------- #
class YNABError(Exception):
    pass


def _request(token, method, path, body=None):
    url = f"{API_BASE}{path}"
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise YNABError(
                "YNAB token invalid/expired (HTTP 401). Regenerate it at "
                "https://app.ynab.com/settings/developer"
            )
        if e.code == 429:
            raise YNABError(
                "Rate limited by YNAB (HTTP 429, ~200 requests/hr). "
                "Wait a bit and retry."
            )
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise YNABError(f"YNAB API error (HTTP {e.code}). {detail}".strip())
    except urllib.error.URLError as e:
        raise YNABError(f"Network failure contacting YNAB: {e.reason}")


def list_budgets(token):
    data = _request(token, "GET", "/budgets")
    return data["data"]["budgets"]


def list_accounts(token, budget_id):
    data = _request(token, "GET", f"/budgets/{budget_id}/accounts")
    return data["data"]["accounts"]


def list_categories(token, budget_id):
    data = _request(token, "GET", f"/budgets/{budget_id}/categories")
    return data["data"]["category_groups"]


def create_transactions(token, budget_id, transactions):
    data = _request(
        token, "POST", f"/budgets/{budget_id}/transactions",
        {"transactions": transactions},
    )
    return data["data"]


def build_category_index(category_groups):
    """case-insensitive {name -> category_id} over non-deleted/non-hidden."""
    index = {}
    for group in category_groups:
        if group.get("deleted") or group.get("hidden"):
            continue
        for cat in group.get("categories", []):
            if cat.get("deleted") or cat.get("hidden"):
                continue
            index[cat["name"].strip().lower()] = cat["id"]
    return index


def resolve_inflow_category(index):
    """Return (name, category_id) for the budget's Ready-to-Assign category."""
    for name in ("Inflow: Ready to Assign", "Inflow: To be Budgeted"):
        cid = index.get(name.lower())
        if cid:
            return name, cid
    return None, None


# --------------------------------------------------------------------------- #
# Category map
# --------------------------------------------------------------------------- #
def load_category_map(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        sys.stderr.write(f"WARNING: category map not found at {path}; "
                         "everything will be uncategorized.\n")
        return []
    except (json.JSONDecodeError, OSError) as e:
        sys.stderr.write(f"WARNING: could not read category map ({e}); "
                         "everything will be uncategorized.\n")
        return []
    # Preserve order; skip meta keys and non-list values.
    pairs = []
    for key, val in raw.items():
        if key.startswith("_"):
            continue
        if not isinstance(val, list):
            continue
        pairs.append((key, [str(s).upper() for s in val]))
    return pairs


def categorize_outflow(description, category_map):
    """Return the intended category NAME for an outflow, or None."""
    upper = (description or "").upper()
    for name, keywords in category_map:
        for kw in keywords:
            if kw in upper:
                return name
    return None


# --------------------------------------------------------------------------- #
# Config persistence (budget_id / account_id — NOT secrets)
# --------------------------------------------------------------------------- #
def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(path, cfg):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def process_rows(rows, category_map, keep_transfers):
    """Turn raw CSV rows into transaction dicts + collect skip counts.

    Returns (txns, stats). Each txn dict has:
        date, amount_milli, is_inflow, payee, memo, category_name (intended),
        raw_description
    """
    txns = []
    stats = {"transfers": 0, "zero": 0, "bad_date": 0}

    for row in rows:
        iso = parse_date(row["date"])
        if iso is None:
            stats["bad_date"] += 1
            sys.stderr.write(
                f"WARNING: skipping row with unparseable date: {row['date']!r} "
                f"({row['description']!r})\n"
            )
            continue

        debit = parse_money(row["debit"])
        credit = parse_money(row["credit"])
        is_inflow = credit is not None and debit is None

        amount = credit if is_inflow else debit
        if amount is None:
            # Neither column populated — treat as a zero/empty row.
            stats["zero"] += 1
            continue

        # 1. Skip zero rows.
        if abs(amount) < 0.005:
            stats["zero"] += 1
            continue

        # 2. Skip internal transfers (unless overridden).
        if not keep_transfers and TRANSFER_RE.search(row["description"] or ""):
            stats["transfers"] += 1
            continue

        # 3. milliunits (outflow stays negative).
        amount_milli = to_milliunits(amount)

        # 4. payee (capped to 50).
        payee = clean_payee(row["description"], row["check_number"])[:PAYEE_MAX]

        # 5. memo = raw description, capped to 200.
        memo = (row["description"] or "")[:MEMO_MAX]

        # 6. categorize (intended name; resolved against live budget later).
        if is_inflow:
            category_name = "__INFLOW__"
        else:
            category_name = categorize_outflow(row["description"], category_map)

        txns.append({
            "date": iso,
            "amount_milli": amount_milli,
            "is_inflow": is_inflow,
            "payee": payee,
            "memo": memo,
            "category_name": category_name,
            "raw_description": row["description"],
        })

    # 7. import_ids (stable order = order encountered).
    build_import_ids(txns)
    return txns, stats


def assign_category_ids(txns, cat_index):
    """Resolve intended category names to live category_ids.

    Mutates each txn: adds category_id (or None) and category_display.
    """
    inflow_name, inflow_id = resolve_inflow_category(cat_index)
    for t in txns:
        if t["category_name"] == "__INFLOW__":
            t["category_id"] = inflow_id
            t["category_display"] = inflow_name or "(uncategorized)"
        elif t["category_name"]:
            cid = cat_index.get(t["category_name"].lower())
            t["category_id"] = cid
            if cid:
                t["category_display"] = t["category_name"]
            else:
                # Mapped name not in budget -> uncategorized, but note intent.
                t["category_display"] = f"{t['category_name']} (not in budget)"
        else:
            t["category_id"] = None
            t["category_display"] = "(uncategorized)"


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def fmt_amount(milli):
    return f"{milli / 1000:,.2f}"


def print_preview(txns, stats):
    if not txns:
        print("No transactions to import.")
    else:
        date_w, amt_w, cat_w = 10, 12, 24
        print(f"{'DATE':<{date_w}}  {'AMOUNT':>{amt_w}}  "
              f"{'CATEGORY':<{cat_w}}  PAYEE")
        print("-" * (date_w + amt_w + cat_w + 4 + 20))
        for t in txns:
            print(f"{t['date']:<{date_w}}  {fmt_amount(t['amount_milli']):>{amt_w}}  "
                  f"{t['category_display'][:cat_w]:<{cat_w}}  {t['payee']}")

    categorized = sum(1 for t in txns if t.get("category_id"))
    uncategorized = len(txns) - categorized
    print()
    print(f"Summary: {len(txns)} to import "
          f"({categorized} categorized, {uncategorized} uncategorized); "
          f"{stats['transfers']} transfers skipped, "
          f"{stats['zero']} zero rows skipped"
          + (f", {stats['bad_date']} bad-date rows skipped"
             if stats["bad_date"] else "")
          + ".")


def build_api_transactions(txns, account_id):
    out = []
    for t in txns:
        obj = {
            "account_id": account_id,
            "date": t["date"],
            "amount": t["amount_milli"],
            "payee_name": t["payee"],
            "memo": t["memo"],
            "cleared": "cleared",
            "approved": False,
            "import_id": t["import_id"][:IMPORT_ID_MAX],
        }
        if t.get("category_id"):
            obj["category_id"] = t["category_id"]
        out.append(obj)
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def get_token():
    return os.environ.get("YNAB_ACCESS_TOKEN", "").strip()


def require_token():
    token = get_token()
    if not token:
        sys.stderr.write(
            "ERROR: YNAB_ACCESS_TOKEN is not set.\n"
            "Create a Personal Access Token at "
            "https://app.ynab.com/settings/developer then either:\n"
            "  export YNAB_ACCESS_TOKEN=...        (in your shell), or\n"
            "  set -a; source .env; set +a         (from a gitignored .env)\n"
        )
        sys.exit(1)
    return token


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ynab_import.py",
        description="Load a bank-export CSV into a YNAB budget via the YNAB API.",
    )
    parser.add_argument("csv", nargs="?", help="path to the bank export CSV")
    parser.add_argument("--budget-id", help="target budget (saved to config)")
    parser.add_argument("--account-id", help="target account (saved to config)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help=f"config file (default: {DEFAULT_CONFIG})")
    script_dir = Path(__file__).resolve().parent
    default_map = script_dir.parent / "references" / "category_map.json"
    parser.add_argument("--category-map", default=str(default_map),
                        help="category map JSON "
                             "(default: ../references/category_map.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview only; NO writes to YNAB")
    parser.add_argument("--keep-transfers", action="store_true",
                        help="import internal transfer rows instead of skipping")
    parser.add_argument("--list-budgets", action="store_true",
                        help="print budget ids + names, then exit")
    parser.add_argument("--list-accounts", action="store_true",
                        help="print account ids + names (needs --budget-id)")
    parser.add_argument("--list-categories", action="store_true",
                        help="print category groups + names (needs --budget-id)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    budget_id = args.budget_id or cfg.get("budget_id")
    account_id = args.account_id or cfg.get("account_id")

    # Persist supplied ids (not secrets).
    if args.budget_id or args.account_id:
        if args.budget_id:
            cfg["budget_id"] = args.budget_id
        if args.account_id:
            cfg["account_id"] = args.account_id
        save_config(args.config, cfg)

    # --- list commands ---
    if args.list_budgets:
        token = require_token()
        try:
            for b in list_budgets(token):
                print(f"{b['id']}  {b['name']}")
        except YNABError as e:
            sys.stderr.write(f"ERROR: {e}\n")
            sys.exit(1)
        return

    if args.list_accounts:
        token = require_token()
        if not budget_id:
            parser.error("--list-accounts needs --budget-id (or a saved one)")
        try:
            for a in list_accounts(token, budget_id):
                if a.get("deleted"):
                    continue
                flag = " [closed]" if a.get("closed") else ""
                print(f"{a['id']}  {a['name']} ({a['type']}){flag}")
        except YNABError as e:
            sys.stderr.write(f"ERROR: {e}\n")
            sys.exit(1)
        return

    if args.list_categories:
        token = require_token()
        if not budget_id:
            parser.error("--list-categories needs --budget-id (or a saved one)")
        try:
            for g in list_categories(token, budget_id):
                if g.get("deleted") or g.get("hidden"):
                    continue
                print(f"# {g['name']}")
                for c in g.get("categories", []):
                    if c.get("deleted") or c.get("hidden"):
                        continue
                    print(f"  {c['name']}")
        except YNABError as e:
            sys.stderr.write(f"ERROR: {e}\n")
            sys.exit(1)
        return

    # --- import / dry-run path ---
    if not args.csv:
        parser.error("a CSV path is required (or use a --list-* command)")
    if not Path(args.csv).is_file():
        parser.error(f"CSV not found: {args.csv}")

    category_map = load_category_map(args.category_map)
    rows = read_bank_csv(args.csv)
    txns, stats = process_rows(rows, category_map, args.keep_transfers)

    # Fetch live categories so the preview reflects real assignment.
    cat_index = {}
    token = get_token()
    if token:
        if not budget_id and not args.dry_run:
            parser.error(
                "no budget configured. Run with --list-budgets, then "
                "--budget-id <ID> --list-accounts, then --account-id <ID>."
            )
        if budget_id:
            try:
                cat_index = build_category_index(
                    list_categories(token, budget_id))
            except YNABError as e:
                if args.dry_run:
                    sys.stderr.write(
                        f"WARNING: could not fetch categories ({e}); "
                        "preview will show uncategorized.\n")
                else:
                    sys.stderr.write(f"ERROR: {e}\n")
                    sys.exit(1)
    elif not args.dry_run:
        require_token()  # exits with guidance

    assign_category_ids(txns, cat_index)
    print_preview(txns, stats)

    if args.dry_run:
        print("\nDRY RUN: nothing was sent to YNAB.")
        return

    # Real import requires budget + account.
    if not budget_id or not account_id:
        parser.error(
            "no budget/account configured. Run setup first:\n"
            "  --list-budgets\n"
            "  --budget-id <ID> --list-accounts\n"
            "  --account-id <ID>"
        )
    if not txns:
        print("Nothing to import.")
        return

    api_txns = build_api_transactions(txns, account_id)
    try:
        result = create_transactions(token, budget_id, api_txns)
    except YNABError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)

    created = len(result.get("transaction_ids", []))
    dupes = len(result.get("duplicate_import_ids", []))
    print(f"DONE: {created} created, {dupes} skipped as duplicates.")


if __name__ == "__main__":
    main()
