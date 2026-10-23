# My Finance

A local, single-user personal finance app for Python 3.11+.

## Install and run

On Windows, double-click `run.bat`. It creates `.venv`, installs requirements
the first time, and starts the app in your browser.

To run manually from this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.headless false --browser.gatherUsageStats false
```

## Prepare a Render deployment

The app uses local SQLite when `DATABASE_URL` is not set. For Render, configure
`DATABASE_URL` with a Neon PostgreSQL connection URL and set
`FINANCE_APP_PASSWORD` as a Render secret. Never commit either value.

To migrate an existing local database, install the requirements and set
`DATABASE_URL` in the local PowerShell session using the private connection
URL from Neon, then run:

```powershell
python -m core.migrate_to_neon --source finance.db
```

The migration stops if Neon already contains application data. It copies the
existing SQLite records and verifies table row counts. Keep the original
`finance.db` and a backup until you confirm the deployed data is correct.

Render's Streamlit start command is:

```text
streamlit run app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true
```

## Upload and use

In **Upload**, choose a Khan Bank `.xlsx` statement (max 20 MB) and click
**Import**. Files are kept in `imported_statements/` and can be re-imported.
Duplicates are not added. New rows append to the same database; consistent
manual merchant and description classifications become reusable rules.
Review, browse, edit, and report on transactions in their respective pages.

## Add another bank

1. Copy `banks/khan_bank.py` to a new parser module.
2. Implement the four parser methods: `can_parse`, `parse`,
   `clean_merchant`, and `is_fee`.
3. Register and import the parser in `banks/__init__.py`.
4. Add a synthetic workbook fixture and parser tests; run `pytest -q`.

## Back up and restore

The app backs up before imports. **Backup now** is also available in the
sidebar. The `backups/` folder keeps the 20 most recent snapshots.

To restore, stop the app, preserve `finance.db`, copy a chosen backup into the
project folder as `finance.db`, then restart. Keep `imported_statements/` for
statement re-imports.

## Privacy

Statements and backups stay local unless you click the optional AI analysis.
It sends selected-period expense dates, amounts, descriptions, and category
names to OpenAI; account/balance columns and the workbook are excluded, but
details inside descriptions may be sent. Provider data terms apply. Enable AI
in PowerShell with `setx OPENAI_API_KEY "YOUR_API_KEY"` and restart. Never put
the key in code or chat. High-confidence results become local rules; uncertain
results remain for review. **AI шинжилгээ** shows period-specific expense
totals by category and merchant, counts, confidence, and reasons; results are
saved locally. Imported workbooks are excluded from version control.
Streamlit usage statistics are disabled. Files and backups are not encrypted.
