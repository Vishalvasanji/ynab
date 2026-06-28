---
name: ynab-import
description: Load bank-export CSV transactions into a YNAB budget via the YNAB API. Use this whenever the user wants to import, upload, or push bank/credit-union transactions into YNAB, reconcile a monthly statement export into YNAB, or mentions a bank CSV that needs to get into their budget — even if they just say "get these into YNAB" or "do my YNAB import for this month." Handles payee cleanup, skips internal transfers, deduplicates via YNAB's native import_id, and optionally categorizes by merchant.
---

# ynab-import

Loads a bank-export CSV into a YNAB budget through the YNAB API (v1). Built to be
run **once a month** against a fresh bank export and to be **safe to re-run** —
overlapping exports never create duplicates, because every transaction carries a
deterministic YNAB `import_id`.

## What it does

- **Skips internal transfers** (`TRANSFER From account# X To account# Y`) by
  default — importing one leg as a plain inflow would misreport it as income.
  Use `--keep-transfers` when the other account is *not* tracked in YNAB.
- **Cleans payee boilerplate** (e.g. `DEBIT CARD DEBIT TACO BELL 034632 BATON
  ROUGE LA` → `TACO BELL 034632 BATON ROUGE`), leaving YNAB's own rename rules to
  finish normalization. The full raw description is preserved in the memo.
- **Categorizes by merchant**, but only ever assigns a category that already
  exists in your budget — a wrong map entry never creates a garbage category, it
  just falls through to uncategorized.
- **Skips $0.00 rows.**
- **Deduplicates** via YNAB's native `import_id`, so re-running an overlapping
  export is a no-op for the repeated rows.
- Imports transactions as **`approved: false`** and **`cleared: "cleared"`** so
  they land in YNAB's approval queue for a human glance before affecting the
  budget.
- **Inflows (deposits)** route to `Inflow: Ready to Assign` (falling back to
  `Inflow: To be Budgeted`).

## Security — the token is a secret

The YNAB access token is a long-lived read/write credential for your entire
budget. **It must never be committed to git.** The script reads it **only** from
the `YNAB_ACCESS_TOKEN` environment variable — never from a file in the repo, and
it is never printed or written to disk.

Create a Personal Access Token at <https://app.ynab.com/settings/developer>, then
provide it one of two safe ways:

```bash
# Option A — export in your shell
export YNAB_ACCESS_TOKEN=your-token-here

# Option B — a gitignored .env (copy .env.example first)
cp .env.example .env        # then paste your token into .env
set -a; source .env; set +a
```

`.env`, `*.token`, `*.secret`, and `config.json` are all gitignored. Budget ID
and Account ID are **not** secrets (they're plain UUIDs) and are saved to a local
config file (see below).

## One-time setup

Pick your budget and account once; the IDs are saved to
`~/.config/ynab-import/config.json` so later runs need only the CSV.

```bash
# 1. find your budget id
python3 scripts/ynab_import.py --list-budgets

# 2. find your account id (saves the budget id to config)
python3 scripts/ynab_import.py --budget-id <BUDGET_ID> --list-accounts

# 3. save the account id to config
python3 scripts/ynab_import.py --budget-id <BUDGET_ID> --account-id <ACCOUNT_ID> --list-accounts
```

## Monthly workflow (two commands)

```bash
# 1. preview — parses + categorizes, writes NOTHING to YNAB
python3 scripts/ynab_import.py export.csv --dry-run

# 2. import for real
python3 scripts/ynab_import.py export.csv
```

The preview prints a `DATE | AMOUNT | CATEGORY | PAYEE` table and a summary with
counts (to import, categorized, uncategorized, transfers skipped, zero rows
skipped). The real run prints `DONE: N created, M skipped as duplicates.`

## Aligning the category map to your budget

`references/category_map.json` maps **a YNAB category name → a list of UPPERCASE
keyword substrings** matched against the raw bank description (first hit wins,
top-to-bottom). The shipped keys are common/standard names and **almost
certainly need renaming to match your actual budget**:

```bash
# print your real category names, then edit the JSON keys to match
python3 scripts/ynab_import.py --budget-id <BUDGET_ID> --list-categories
```

Keys starting with `_` are ignored (use them for comments). Inflows are routed
automatically — no map entry needed. A mapped name that doesn't exist in your
budget is harmless: that transaction simply lands uncategorized.

## Expected CSV format

Header row present; columns in this order:

| Date | Type | Description | Debit | Credit | CheckNumber |
|---|---|---|---|---|---|
| `6/27/2026` | `Withdrawal` | `DEBIT CARD DEBIT TACO BELL 034632 BATON ROUGE LA` | `-$24.88` | | |
| `6/22/2026` | `Deposit` | `DEPOSIT 1024521` | | `$8,500.00` | |
| `6/26/2026` | `Withdrawal` | `SHARE DRAFT` | `-$1000.00` | | `2077` |

- Date is `M/D/YYYY` (non-zero-padded). Debit is already negative, Credit
  positive; exactly one is populated per row. `$` and commas are stripped. The
  file is read as `utf-8-sig` (BOM tolerated); blank lines and rows with an
  unparseable date are skipped with a warning to stderr.
- A different bank's export only requires changing the CSV reader — it's isolated
  in the `read_bank_csv()` function in `scripts/ynab_import.py`.

See `references/sample.csv` for a committed fixture you can dry-run against.

## Options

```
python3 scripts/ynab_import.py [CSV] [options]
  --budget-id ID        target budget (saved to config when supplied)
  --account-id ID       target account (saved to config when supplied)
  --config PATH         config file (default: ~/.config/ynab-import/config.json)
  --category-map PATH   default: ../references/category_map.json
  --dry-run             preview only; NO writes to YNAB
  --keep-transfers      import internal transfer rows instead of skipping
  --list-budgets        print budget ids + names, then exit
  --list-accounts       print account ids + names (needs --budget-id)
  --list-categories     print category groups + names (needs --budget-id)
```

## Troubleshooting

- **`YNAB_ACCESS_TOKEN is not set`** — export the token or source a `.env` (see
  Security). Get a token at <https://app.ynab.com/settings/developer>.
- **HTTP 401** — the token is invalid or expired; regenerate it at the developer
  settings page.
- **HTTP 429** — rate limited (~200 requests/hour); wait and retry. A normal
  monthly import is a single bulk request, so this is rare.
- **Network failure** — the reason is reported and the script exits non-zero.
- **CSV not found / no CSV and no `--list-*`** — argparse will tell you what's
  missing.
- **No budget/account configured on an import run** — run the `--list-*` setup
  steps above to populate the config.
- **Everything uncategorized** — your `category_map.json` keys don't match your
  real category names; run `--list-categories` and edit them.
