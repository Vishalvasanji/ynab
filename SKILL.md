---
name: ynab-import
description: Load bank-export CSV transactions into a YNAB budget via the YNAB API, with Claude doing the categorization and payee matching. Use this whenever the user attaches or points to a bank/credit-union CSV and wants it imported into YNAB, says "import these into YNAB", "do my YNAB import", "get this statement into my budget", or similar. Claude parses the CSV, pulls the budget's live categories and payees, matches each transaction as best it can, asks the user about anything ambiguous, then imports — deduplicated via YNAB's native import_id, landing in the approval queue.
---

# ynab-import

Imports a bank-export CSV into a YNAB budget. The script is the mechanical half
(parse the CSV, talk to the YNAB API); **Claude is the brain** — it matches each
transaction to the budget's real payees and categories and asks the user
whenever something is ambiguous.

Designed to be run interactively: the user attaches/points to a CSV in a Claude
session and Claude drives the two-phase flow below.

## Prerequisites

1. **Token in the session environment.** The script reads the YNAB token **only**
   from the `YNAB_ACCESS_TOKEN` environment variable — never from a file in the
   repo. In Claude Code on the web, set it in the environment's variables; locally,
   `export YNAB_ACCESS_TOKEN=...` or use a gitignored `.env`
   (`set -a; source .env; set +a`). Get a token at
   <https://app.ynab.com/settings/developer>.
2. **Budget + account IDs** (not secrets). Provide via `--budget-id`/`--account-id`,
   the `YNAB_BUDGET_ID`/`YNAB_ACCOUNT_ID` env vars, or let the script save them to
   `~/.config/ynab-import/config.json` on first use. Find them with
   `list-budgets` and `list-accounts` (below).

If the token is missing, the script exits with a message pointing to the YNAB
developer-settings page. Surface that to the user rather than guessing a token.

## The flow Claude should follow

When the user gives you a CSV to import, do this:

### 1. Confirm setup
Make sure a budget and account are resolvable (config, env, or ask the user). If
not, run `list-budgets`, then `list-accounts`, and ask which to use:

```bash
python3 scripts/ynab_import.py list-budgets
python3 scripts/ynab_import.py list-accounts --budget-id <BUDGET_ID>
```

### 2. prepare — parse + fetch live data
```bash
python3 scripts/ynab_import.py prepare <CSV> > prepared.json
```
This emits one JSON object:
```jsonc
{
  "budget_id": "...", "account_id": "...",
  "inflow_category": { "id": "...", "name": "Inflow: Ready to Assign" },
  "categories": [ { "id": "...", "name": "Groceries", "group": "Everyday" }, ... ],
  "payees":     [ { "id": "...", "name": "Taco Bell" }, ... ],
  "transactions": [
    {
      "date": "2026-06-27", "amount_milli": -24880, "inflow": false,
      "suggested_payee": "TACO BELL 034632 BATON ROUGE",
      "memo": "DEBIT CARD DEBIT TACO BELL 034632 BATON ROUGE LA",
      "import_id": "YNAB:-24880:2026-06-27:1",
      "hint_category_id": null, "hint_category_name": null
    }, ...
  ],
  "skipped": { "transfers": 1, "zero": 1, "bad_date": 0 }
}
```
(Internal transfers and $0 rows are already dropped and counted in `skipped`.)

### 3. Match each transaction — this is your job, not the script's
For every transaction in `transactions`, decide a **payee** and a **category**
using the live `payees` and `categories` lists plus your own knowledge of
merchants:

- **Payee.** Try to match `suggested_payee` to an existing entry in `payees`
  (handle abbreviations, store numbers, city/state tails, AMZN→Amazon, etc.).
  - Confident match → set `payee_id` to that payee's id.
  - **No confident match → ASK the user** which existing payee to use (or whether
    to create a new one). Do not silently invent a new payee. (Only set
    `payee_name` to create a new payee once the user has approved it.)
- **Category.** Pick the best-fitting category from `categories` by merchant type.
  `hint_category_id` is a keyword-map suggestion you may use as one signal, not a
  rule. For **inflows** (`"inflow": true`), use `inflow_category.id` unless the
  user wants otherwise.
  - Confident → set `category_id`.
  - Ambiguous or no good fit → **ask the user**, or leave it uncategorized
    (omit `category_id`) so YNAB's approval queue catches it. Prefer asking when
    the amount is large or the merchant is unclear.
- **Batch your questions.** Collect all the ambiguous payees/categories and ask
  them together (e.g. a short numbered list), rather than one prompt per
  transaction.

Keep `date`, `amount_milli`, `import_id`, and `memo` exactly as `prepare` emitted
them — those drive dedup and must not change.

### 4. Write the resolved file
Produce `resolved.json` (you can keep the whole prepared object and just add
`payee_id`/`payee_name`/`category_id` to each transaction, or emit a slimmer
`{"account_id": "...", "transactions": [...]}`). Required per transaction:
`date`, `amount_milli`, `import_id`. Optional: `payee_id` **or** `payee_name`,
`category_id`, `memo`.

### 5. Preview, confirm, apply
```bash
python3 scripts/ynab_import.py apply resolved.json --dry-run   # exact payload, sends nothing
# show the user the preview, get a yes, then:
python3 scripts/ynab_import.py apply resolved.json
```
`apply` creates everything as `approved: false` / `cleared: "cleared"` (so it
lands in YNAB's approval queue) and deduplicates via `import_id`. It prints
`DONE: N created, M skipped as duplicates.` Re-running the same export is safe —
duplicates are dropped by YNAB.

## Settled behavior (don't change without asking the user)

- **Internal transfers** (`TRANSFER From account# X To account# Y`) are skipped by
  default. Pass `--keep-transfers` to `prepare` only when the other account isn't
  tracked in YNAB.
- **$0.00 rows** are skipped.
- **Payee cleanup** strips bank boilerplate but isn't aggressive — store numbers
  and fragments may remain; that's fine, you (and YNAB rename rules) finish the
  job. The full raw description is preserved in `memo`.
- **Dedup** via the native `YNAB:[milliunits]:[date]:[occurrence]` import_id.
- **Inflows** route to `Inflow: Ready to Assign` (fallback `Inflow: To be
  Budgeted`).
- Amounts are milliunits ($1.00 = 1000; −$24.88 = −24880).

## Other commands

```bash
python3 scripts/ynab_import.py list-categories --budget-id <ID>   # real category names
python3 scripts/ynab_import.py list-payees     --budget-id <ID>   # real payee names
```

`references/category_map.json` is an **optional** keyword hint map (merchant
substrings → category name) that pre-fills `hint_category_id`. It only ever
accelerates your matching; you make the real call. Keys starting with `_` are
ignored.

## Expected CSV format

Header row present; columns in order: `Date | Type | Description | Debit | Credit
| CheckNumber`. Date is `M/D/YYYY`; Debit is negative, Credit positive (exactly
one populated per row); `$` and commas are stripped; the file is read as
`utf-8-sig`. A different bank's export only requires changing `read_bank_csv()` in
`scripts/ynab_import.py`. See `references/sample.csv` for a fixture.

## Troubleshooting

- **`YNAB_ACCESS_TOKEN is not set`** — set the env var / source a `.env`. Token
  from <https://app.ynab.com/settings/developer>.
- **HTTP 401** — token invalid/expired; regenerate it.
- **HTTP 429** — rate limited (~200/hr); wait and retry (a normal import is one
  bulk request).
- **Network failure** — the reason is reported; the script exits non-zero.
- **No budget/account configured** — run `list-budgets` / `list-accounts` and pass
  or save the IDs.
- **`prepare` shows empty `categories`/`payees`** — no token or no budget set, so
  it degraded to parse-only; fix the token/budget and re-run before matching.
