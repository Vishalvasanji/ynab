# ynab

Home for the **`ynab-import`** Claude Code skill — load a bank-export CSV into a
YNAB budget via the YNAB API.

See [`SKILL.md`](./SKILL.md) for setup, the monthly workflow, and troubleshooting.

## Automated imports (GitHub Actions)

This repo runs the import for you — no local setup needed.

- **CI** (`.github/workflows/ci.yml`) auto-tests the skill on every push/PR. No
  secrets, no writes to your budget.
- **YNAB Import** (`.github/workflows/import.yml`) imports every CSV in
  [`inbox/`](./inbox/) — on the **1st of each month** automatically, or any time
  via the **Actions → YNAB Import → Run workflow** button. Idempotent (dedupes
  via YNAB's `import_id`); a no-op when `inbox/` is empty.

### One-time setup (the only things that need you)

Under **Settings → Secrets and variables → Actions**:

| Kind | Name | Value |
|---|---|---|
| Secret | `YNAB_ACCESS_TOKEN` | your token from <https://app.ynab.com/settings/developer> |
| Variable | `YNAB_BUDGET_ID` | target budget UUID (run `--list-budgets` locally once to find it) |
| Variable | `YNAB_ACCOUNT_ID` | target account UUID (run `--list-accounts` locally once to find it) |

After that, each month just upload your export into `inbox/` (see
[`inbox/README.md`](./inbox/README.md)) and the import runs itself.

> Keep this repo **private** — uploaded CSVs contain real financial data.
