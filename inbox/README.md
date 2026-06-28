# inbox/

Drop your monthly bank-export CSV(s) here to have GitHub Actions import them
into YNAB.

**How:** on GitHub, open this `inbox/` folder → **Add file** → **Upload files**,
drag your export in, and commit to `main`. The **YNAB Import** workflow imports
everything in here (manually via the Actions tab, or automatically on the 1st of
each month). Re-running is safe — imports are deduplicated by YNAB's native
`import_id`.

**Privacy note:** files committed here are stored in the GitHub repo (and its
history). Keep the repo private. After a successful import you can delete the CSV
from this folder; the imported transactions already live in YNAB.

> Note: the repo's `.gitignore` ignores `*.csv`, so the GitHub web **Upload
> files** flow is the intended way to add exports here (it commits regardless of
> `.gitignore`). To add one from the command line instead, force it:
> `git add -f inbox/your-export.csv`.
