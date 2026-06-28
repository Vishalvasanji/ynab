# ynab

Home for the **`ynab-import`** Claude Code skill — load a bank-export CSV into a
YNAB budget via the YNAB API, with **Claude doing the categorization and payee
matching**.

See [`SKILL.md`](./SKILL.md) for the full flow and troubleshooting.

## How it works

You attach (or point Claude at) a bank-export CSV in a Claude Code session and
ask it to import. Claude then:

1. **Parses** the CSV (skipping internal transfers and $0 rows) and **pulls your
   live categories and payees** from YNAB.
2. **Matches** each transaction to an existing payee and the best-fitting
   category, using your real lists plus its knowledge of merchants.
3. **Asks you** whenever a payee can't be confidently matched (or a category is
   ambiguous) — batched into a short list, not one prompt per row.
4. **Previews**, then **imports** as `approved: false` so everything lands in
   YNAB's approval queue for a final glance. Deduplicated via YNAB's native
   `import_id`, so re-running an overlapping export never creates duplicates.

Under the hood the script runs in two phases — `prepare` (parse + fetch live
data → JSON) and `apply` (create the resolved transactions) — with Claude doing
the matching in between. See [`SKILL.md`](./SKILL.md).

## Setup (one time)

Claude runs this skill inside a session, so the session needs your credentials:

| What | Where | Notes |
|---|---|---|
| `YNAB_ACCESS_TOKEN` | the **session environment** (Claude Code web env vars, or `export`/`.env` locally) | **Secret.** Never committed; read only from this env var. Get it at <https://app.ynab.com/settings/developer>. |
| `YNAB_BUDGET_ID` | env var, `--budget-id`, or saved config | Not a secret. Find via `list-budgets`. |
| `YNAB_ACCOUNT_ID` | env var, `--account-id`, or saved config | Not a secret. Find via `list-accounts`. |

```bash
python3 scripts/ynab_import.py list-budgets
python3 scripts/ynab_import.py list-accounts --budget-id <BUDGET_ID>
```

After that, just hand Claude a CSV and say "import this into YNAB."

## Install it as a plugin (Claude Code & Cowork)

This repo is also a one-plugin **marketplace** (`.claude-plugin/marketplace.json`
+ `.claude-plugin/plugin.json`), so the same skill installs cleanly into Claude
Code and Cowork — no copying files around.

**Claude Code (CLI):**
```bash
/plugin marketplace add Vishalvasanji/ynab
/plugin install ynab-import@ynab-tools
```

**Cowork:**
- *Just you:* **Customize → Plugins → Browse / Add**, point it at this GitHub repo.
- *Whole org:* **Organization settings → Plugins → Add plugin → GitHub**, select
  `Vishalvasanji/ynab`. Cowork auto-syncs new commits.

After installing, set the same credentials in that environment:

| What | How | Secret? |
|---|---|---|
| `YNAB_ACCESS_TOKEN` | environment variable | **Yes** — never in chat or git |
| `YNAB_BUDGET_ID` / `YNAB_ACCOUNT_ID` | environment variables | No |

The skill's commands use `${CLAUDE_PLUGIN_ROOT}` so they resolve whether it's run
from this repo or from the installed plugin. The environment must allow outbound
HTTPS to `api.ynab.com`.

> Note: claude.ai **chat** is intentionally not supported — its sandbox has no
> secret store and its connectors require a hosted OAuth MCP server. Use Claude
> Code or Cowork.

## CI

`.github/workflows/ci.yml` auto-tests the skill on every push/PR (offline
`prepare` against the committed fixture, plus the import_id / milliunit / payee
invariants). No secrets, no writes to your budget.
