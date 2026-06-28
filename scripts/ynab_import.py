#!/usr/bin/env python3
"""
ynab_import.py — bridge between a bank-export CSV and a YNAB budget.

This script is the mechanical half of the `ynab-import` skill. The *intelligence*
(matching merchants to your real payees and categories, and asking you when it's
ambiguous) lives in Claude, which drives this script in two phases:

    1. prepare  — parse the CSV and fetch your LIVE categories + payees, emitting
                  one structured JSON blob for Claude to reason over.
    2. apply    — take the resolved transactions Claude produced and create them
                  in YNAB (deduplicated, landing in the approval queue).

Standard library only (no pip dependencies).

SECURITY: the YNAB access token is a long-lived read/write secret. It is read
ONLY from the YNAB_ACCESS_TOKEN environment variable — never from a file in the
repo, never printed, never written to disk. Budget/account IDs are NOT secrets
and may come from flags, the YNAB_BUDGET_ID / YNAB_ACCOUNT_ID env vars, or the
local config file.
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
# Committed reference of known budget + account IDs (NOT secrets). Lets the
# script resolve a budget/account by name and supply a sensible default.
DEFAULT_ACCOUNTS_REF = (Path(__file__).resolve().parent.parent
                        / "references" / "accounts.json")
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
    """Read the bank export. Returns a list of dicts with raw string fields:
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
    return _request(token, "GET", "/budgets")["data"]["budgets"]


def list_accounts(token, budget_id):
    return _request(token, "GET",
                    f"/budgets/{budget_id}/accounts")["data"]["accounts"]


def list_categories(token, budget_id):
    return _request(token, "GET",
                    f"/budgets/{budget_id}/categories")["data"]["category_groups"]


def list_payees(token, budget_id):
    return _request(token, "GET",
                    f"/budgets/{budget_id}/payees")["data"]["payees"]


def create_transactions(token, budget_id, transactions):
    return _request(
        token, "POST", f"/budgets/{budget_id}/transactions",
        {"transactions": transactions},
    )["data"]


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


def categories_for_output(category_groups):
    """Flat [{id, name, group}] of live, usable categories."""
    out = []
    for group in category_groups:
        if group.get("deleted") or group.get("hidden"):
            continue
        for cat in group.get("categories", []):
            if cat.get("deleted") or cat.get("hidden"):
                continue
            out.append({"id": cat["id"], "name": cat["name"],
                        "group": group["name"]})
    return out


def payees_for_output(payees):
    """Flat [{id, name}] of live merchant payees (excludes deleted/transfer)."""
    out = []
    for p in payees:
        if p.get("deleted"):
            continue
        if p.get("transfer_account_id"):
            continue  # account-transfer payee, not a merchant
        out.append({"id": p["id"], "name": p["name"]})
    return out


def resolve_inflow_category(index):
    """Return (name, category_id) for the budget's Ready-to-Assign category."""
    for name in ("Inflow: Ready to Assign", "Inflow: To be Budgeted"):
        cid = index.get(name.lower())
        if cid:
            return name, cid
    return None, None


# --------------------------------------------------------------------------- #
# Category map (optional hint to reduce questions; the real call is Claude's)
# --------------------------------------------------------------------------- #
def load_category_map(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        sys.stderr.write(f"WARNING: could not read category map ({e}).\n")
        return []
    pairs = []
    for key, val in raw.items():
        if key.startswith("_") or not isinstance(val, list):
            continue
        pairs.append((key, [str(s).upper() for s in val]))
    return pairs


def categorize_outflow(description, category_map):
    """Return the hinted category NAME for an outflow, or None."""
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
# Known accounts reference (committed; budget + all account IDs, NOT secrets)
# --------------------------------------------------------------------------- #
def load_accounts_ref(path):
    """Load references/accounts.json. Returns {} if absent/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            ref = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return ref if isinstance(ref, dict) else {}


def ref_budget_id(ref):
    budget = ref.get("budget") if isinstance(ref, dict) else None
    return budget.get("id") if isinstance(budget, dict) else None


def resolve_account_name(ref, name):
    """Case-insensitive account-name -> id over the reference. None if no match."""
    if not name:
        return None
    target = name.strip().lower()
    for acct in ref.get("accounts", []) or []:
        if str(acct.get("name", "")).strip().lower() == target:
            return acct.get("id")
    return None


def ref_default_account_id(ref):
    """The reference's default account id (by default_account name)."""
    return resolve_account_name(ref, ref.get("default_account")) if ref else None


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def process_rows(rows, category_map, keep_transfers):
    """Turn raw CSV rows into transaction dicts + collect skip counts."""
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
            stats["zero"] += 1
            continue
        if abs(amount) < 0.005:
            stats["zero"] += 1
            continue
        if not keep_transfers and TRANSFER_RE.search(row["description"] or ""):
            stats["transfers"] += 1
            continue

        amount_milli = to_milliunits(amount)
        payee = clean_payee(row["description"], row["check_number"])[:PAYEE_MAX]
        memo = (row["description"] or "")[:MEMO_MAX]
        category_name = ("__INFLOW__" if is_inflow
                         else categorize_outflow(row["description"], category_map))

        txns.append({
            "date": iso,
            "amount_milli": amount_milli,
            "is_inflow": is_inflow,
            "suggested_payee": payee,
            "memo": memo,
            "hint_category_name": category_name,
            "raw_description": row["description"],
        })

    build_import_ids(txns)
    return txns, stats


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #
def cmd_prepare(args, token, budget_id, account_id):
    category_map = load_category_map(args.category_map)
    rows = read_bank_csv(args.csv)
    txns, stats = process_rows(rows, category_map, args.keep_transfers)

    categories = []
    payees = []
    cat_index = {}
    inflow_name = inflow_id = None
    if token and budget_id:
        try:
            groups = list_categories(token, budget_id)
            categories = categories_for_output(groups)
            cat_index = build_category_index(groups)
            inflow_name, inflow_id = resolve_inflow_category(cat_index)
            payees = payees_for_output(list_payees(token, budget_id))
        except YNABError as e:
            sys.stderr.write(
                f"WARNING: could not fetch live categories/payees ({e}). "
                "Emitting transactions only.\n")
    elif not token:
        sys.stderr.write(
            "WARNING: no YNAB_ACCESS_TOKEN; emitting transactions without "
            "live categories/payees.\n")

    # Attach the keyword-map hint as a category_id when the name exists live.
    for t in txns:
        hint = t.pop("hint_category_name")
        if hint == "__INFLOW__":
            t["inflow"] = True
            t["hint_category_id"] = inflow_id
            t["hint_category_name"] = inflow_name
        else:
            t["inflow"] = False
            cid = cat_index.get(hint.lower()) if hint else None
            t["hint_category_id"] = cid
            t["hint_category_name"] = hint if cid else None

    out = {
        "budget_id": budget_id,
        "account_id": account_id,
        "inflow_category": ({"id": inflow_id, "name": inflow_name}
                            if inflow_id else None),
        "categories": categories,
        "payees": payees,
        "transactions": txns,
        "skipped": stats,
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def build_api_transactions(resolved, account_id):
    """Turn Claude's resolved transactions into YNAB API objects."""
    out = []
    for i, t in enumerate(resolved):
        date = t.get("date")
        amount = t.get("amount_milli", t.get("amount"))
        import_id = t.get("import_id")
        if date is None or amount is None or not import_id:
            raise YNABError(
                f"resolved transaction #{i} is missing date/amount_milli/"
                f"import_id: {t!r}")
        obj = {
            "account_id": account_id,
            "date": date,
            "amount": int(amount),
            "cleared": "cleared",
            "approved": False,
            "import_id": str(import_id)[:IMPORT_ID_MAX],
        }
        if t.get("payee_id"):
            obj["payee_id"] = t["payee_id"]
        elif t.get("payee_name"):
            obj["payee_name"] = str(t["payee_name"])[:PAYEE_MAX]
        if t.get("category_id"):
            obj["category_id"] = t["category_id"]
        if t.get("memo"):
            obj["memo"] = str(t["memo"])[:MEMO_MAX]
        out.append(obj)
    return out


def cmd_apply(args, token, budget_id, account_id):
    try:
        with open(args.resolved, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        sys.stderr.write(f"ERROR: could not read resolved file: {e}\n")
        sys.exit(1)

    resolved = doc.get("transactions", doc) if isinstance(doc, dict) else doc
    if not isinstance(resolved, list):
        sys.stderr.write("ERROR: resolved file must be a list of transactions "
                         "or an object with a 'transactions' list.\n")
        sys.exit(1)

    # account_id may be carried in the resolved doc.
    if isinstance(doc, dict) and doc.get("account_id"):
        account_id = account_id or doc["account_id"]
    if isinstance(doc, dict) and doc.get("budget_id"):
        budget_id = budget_id or doc["budget_id"]

    if not budget_id or not account_id:
        sys.stderr.write(
            "ERROR: no budget/account configured. Pass --budget-id/--account-id, "
            "set YNAB_BUDGET_ID/YNAB_ACCOUNT_ID, or include them in the resolved "
            "file.\n")
        sys.exit(1)

    try:
        api_txns = build_api_transactions(resolved, account_id)
    except YNABError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)

    if not api_txns:
        print("Nothing to import.")
        return

    if args.dry_run:
        print(json.dumps({"would_create": api_txns}, indent=2))
        print(f"\nDRY RUN: {len(api_txns)} transactions ready; "
              "nothing was sent to YNAB.")
        return

    token = token or require_token()
    try:
        result = create_transactions(token, budget_id, api_txns)
    except YNABError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)

    created = len(result.get("transaction_ids", []))
    dupes = len(result.get("duplicate_import_ids", []))
    print(f"DONE: {created} created, {dupes} skipped as duplicates.")


# --------------------------------------------------------------------------- #
# list-* commands
# --------------------------------------------------------------------------- #
def cmd_list_budgets(token):
    for b in list_budgets(token):
        print(f"{b['id']}  {b['name']}")


def cmd_list_accounts(token, budget_id):
    for a in list_accounts(token, budget_id):
        if a.get("deleted"):
            continue
        flag = " [closed]" if a.get("closed") else ""
        print(f"{a['id']}  {a['name']} ({a['type']}){flag}")


def cmd_list_categories(token, budget_id):
    for g in list_categories(token, budget_id):
        if g.get("deleted") or g.get("hidden"):
            continue
        print(f"# {g['name']}")
        for c in g.get("categories", []):
            if c.get("deleted") or c.get("hidden"):
                continue
            print(f"  {c['name']}")


def cmd_list_payees(token, budget_id):
    for p in payees_for_output(list_payees(token, budget_id)):
        print(f"{p['id']}  {p['name']}")


def cmd_list_known_accounts(ref):
    """Print budget + accounts from the committed reference (no API call)."""
    if not ref:
        sys.stderr.write(
            "No accounts reference found. Expected references/accounts.json "
            "(or pass --accounts-ref). Run `list-budgets` / `list-accounts` "
            "to fetch IDs and populate it.\n")
        sys.exit(1)
    budget = ref.get("budget") or {}
    if budget:
        print(f"Budget: {budget.get('name')}  {budget.get('id')}")
    default = ref.get("default_account")
    for acct in ref.get("accounts", []) or []:
        star = " *default" if acct.get("name") == default else ""
        typ = f" ({acct['type']})" if acct.get("type") else ""
        print(f"  {acct.get('id')}  {acct.get('name')}{typ}{star}")


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


def resolve_ids(args, cfg, ref):
    """Resolve (budget_id, account_id) by precedence.

    budget:  --budget-id > YNAB_BUDGET_ID > config.json > accounts.json
    account: --account-id > --account NAME > YNAB_ACCOUNT_ID > config.json
             > accounts.json default_account
    The committed accounts.json reference is the lowest-precedence fallback, so
    explicit flags/env/config always win.
    """
    budget_id = (args.budget_id or os.environ.get("YNAB_BUDGET_ID")
                 or cfg.get("budget_id") or ref_budget_id(ref))
    account_id = (args.account_id
                  or resolve_account_name(ref, getattr(args, "account", None))
                  or os.environ.get("YNAB_ACCOUNT_ID")
                  or cfg.get("account_id") or ref_default_account_id(ref))
    return budget_id, account_id


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ynab_import.py",
        description="Bridge a bank-export CSV into a YNAB budget (driven by "
                    "the ynab-import skill).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, need_budget=False, need_account=False):
        p.add_argument("--budget-id", help="target budget UUID "
                       "(or set YNAB_BUDGET_ID)")
        p.add_argument("--account-id", help="target account UUID "
                       "(or set YNAB_ACCOUNT_ID)")
        p.add_argument("--account", help="target account by NAME, resolved via "
                       "the known-accounts reference (e.g. 'LESFCU Checking')")
        p.add_argument("--config", default=str(DEFAULT_CONFIG),
                       help=f"config file (default: {DEFAULT_CONFIG})")
        p.add_argument("--accounts-ref", default=str(DEFAULT_ACCOUNTS_REF),
                       help="known budget/account IDs reference "
                       f"(default: {DEFAULT_ACCOUNTS_REF})")

    script_dir = Path(__file__).resolve().parent
    default_map = script_dir.parent / "references" / "category_map.json"

    p_prep = sub.add_parser("prepare",
                            help="parse CSV + fetch live categories/payees -> JSON")
    p_prep.add_argument("csv", help="path to the bank export CSV")
    p_prep.add_argument("--category-map", default=str(default_map),
                        help="optional keyword hint map")
    p_prep.add_argument("--keep-transfers", action="store_true",
                        help="include internal transfer rows instead of skipping")
    add_common(p_prep)

    p_apply = sub.add_parser("apply",
                             help="create resolved transactions in YNAB")
    p_apply.add_argument("resolved",
                         help="JSON file of resolved transactions")
    p_apply.add_argument("--dry-run", action="store_true",
                         help="print the exact payload; send nothing")
    add_common(p_apply)

    p_lb = sub.add_parser("list-budgets", help="print budget ids + names")
    add_common(p_lb)
    p_la = sub.add_parser("list-accounts", help="print account ids + names")
    add_common(p_la)
    p_lc = sub.add_parser("list-categories", help="print category groups + names")
    add_common(p_lc)
    p_lp = sub.add_parser("list-payees", help="print payee ids + names")
    add_common(p_lp)
    p_lk = sub.add_parser("list-known-accounts",
                          help="print budget + account IDs from the committed "
                               "reference (offline; no token needed)")
    add_common(p_lk)

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    ref = load_accounts_ref(args.accounts_ref)
    budget_id, account_id = resolve_ids(args, cfg, ref)

    # Persist supplied ids (not secrets) for later runs.
    if args.budget_id or args.account_id:
        if args.budget_id:
            cfg["budget_id"] = args.budget_id
        if args.account_id:
            cfg["account_id"] = args.account_id
        save_config(args.config, cfg)

    try:
        if args.command == "list-known-accounts":
            cmd_list_known_accounts(ref)
            return
        if args.command == "list-budgets":
            cmd_list_budgets(require_token())
            return
        if args.command == "list-accounts":
            if not budget_id:
                parser.error("list-accounts needs --budget-id / YNAB_BUDGET_ID")
            cmd_list_accounts(require_token(), budget_id)
            return
        if args.command == "list-categories":
            if not budget_id:
                parser.error("list-categories needs --budget-id / YNAB_BUDGET_ID")
            cmd_list_categories(require_token(), budget_id)
            return
        if args.command == "list-payees":
            if not budget_id:
                parser.error("list-payees needs --budget-id / YNAB_BUDGET_ID")
            cmd_list_payees(require_token(), budget_id)
            return

        if args.command == "prepare":
            if not Path(args.csv).is_file():
                parser.error(f"CSV not found: {args.csv}")
            cmd_prepare(args, get_token(), budget_id, account_id)
            return

        if args.command == "apply":
            cmd_apply(args, get_token(), budget_id, account_id)
            return
    except YNABError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
