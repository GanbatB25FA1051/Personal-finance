# My Finance

A local, single-user personal finance app for Python 3.11+.

## Install and run

On Windows, double-click `run.bat`. It creates `.venv`, installs requirements
on first run, and starts the app in your browser. If `FINANCE_APP_PASSWORD` is
not already set in the process environment, the launcher securely prompts for
a local password without echoing it. The same required password check applies
to local and hosted use; there is no password bypass. Do not store passwords in
source control or CI. The launcher accepts `FINANCE_APP_HEADLESS=true` and
`FINANCE_APP_PORT` for smoke checks; normal use opens a browser on port 8501.
If Windows Application Control blocks a native package inside `.venv`, the
launcher tries the installed Python environment instead. If that is also blocked,
ask your administrator to approve the package location; do not disable the policy.

For the hosted My Finance application on Windows, double-click `My Finance.bat`.
It opens the Render app in an Edge or Chrome app-style window and uses the
existing cloud database. Internet access and the app password are required.
This launcher is not an offline or standalone `.exe` application.

To run manually from this folder:

```powershell
python -m venv --system-site-packages .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.headless false --browser.gatherUsageStats false
```

## Prepare a Render deployment

The app uses local SQLite when `DATABASE_URL` is not set. It refuses to start on Render without `DATABASE_URL` so it cannot silently create a blank database on Render's temporary disk. For Render, configure
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

Render Free web services sleep when idle and have temporary local storage; local SQLite, backups, and workbook files would be lost on a restart or spin-down. Keep the existing Neon PostgreSQL `DATABASE_URL` on the Render service. Imported workbooks are stored in that database. Do not create a new Neon database or replace the existing service variables when deploying this branch.

Render service settings:

- Runtime: Python 3 (the repository uses Python 3.11 in CI)
- Build command: `pip install -r requirements.txt`
- Start command: `streamlit run app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true`
- Plan: Free
- Required secrets in the Render Environment page: `DATABASE_URL` (existing Neon URL) and `FINANCE_APP_PASSWORD` (login password)

Render's Streamlit start command is:

```text
streamlit run app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true
```

## Upload and use

In **Upload**, choose a Khan Bank `.xlsx` statement (max 20 MB) and click
**Import**. The workbook is saved in the database so it remains available on
Render and can be re-imported. Older Render imports from before workbook
storage was added need the original file selected once to reconnect it. New
statements append to the shared database; duplicate transactions are not added.
Each import records which transactions appeared in that statement. In
**Upload**, choose an existing statement and add another statement for the same
account to it. **Reports** then analyzes the selected statement together with
its appended statements, or the full combined history. Older imports created
before statement-link tracking can only be analyzed using the transactions
first added by each import.

The spending report shows the amount for each category and its percentage of
total income in the selected period. Clothing and footwear is available as a
default expense category. Uncategorized transactions remain visible in the
breakdown; assigning categories is needed for a meaningful category report.

## Monthly budgets

Open **Budget** to plan a month (defaults to next month). Enter category budgets
in MNT and save. The page compares each target with actual categorized spending
for that month, including remaining amounts, execution percentages, and
over-budget warnings. Transfers and income categories are excluded; uncategorized
spending is shown separately.

## Long-term goals

Open **Long-term goals** to plan a purchase using current savings, a loan, and
family or other funding. Enter the target price and date; the app calculates
the monthly savings needed and a fixed monthly loan installment from the annual
interest rate and term. Goal savings and loan payments are also summarized in
the selected month's **Budget** page as separate planned commitments, not as
actual transaction expenses. Loan calculations are estimates, not lender quotes.

## Add another bank

1. Copy `banks/khan_bank.py` to a new parser module.
2. Implement the four parser methods: `can_parse`, `parse`,
   `clean_merchant`, and `is_fee`.
3. Register and import the parser in `banks/__init__.py`.
4. Add a synthetic workbook fixture and parser tests; run `pytest -q`.

## Back up and restore

SQLite databases are snapshotted before imports. **Backup now** is also
available in the sidebar. Each snapshot is created consistently, checked with
SQLite integrity validation, and the `backups/` folder retains the 20 newest
snapshots. The app does not claim a local backup succeeded when `DATABASE_URL`
selects PostgreSQL.

To restore SQLite, stop the app first, then run this from the project folder:

```powershell
python -m core.restore_sqlite --backup backups\finance_YYYYMMDD_HHMMSS.db --database finance.db
```

The restore tool asks for an explicit `RESTORE` confirmation, validates the
snapshot, saves the current database as a safety snapshot, validates a staged
copy, then replaces the database. Keep `imported_statements/` for statement
re-imports. The automated test restores a synthetic SQLite backup and checks
transaction counts and summed amounts against the saved values.

### Neon PostgreSQL backup and restore

The application does not create local backup files for Neon and does not manage
Neon backups. Check the project's Neon console for the backup/PITR window and
plan-specific options before relying on them. Neon provides snapshot restore
that creates a branch; restore can be previewed there before finalizing it to
replace the original branch. Configurable scheduled snapshots require a paid
plan according to Neon API documentation. This repository has no Neon project
credentials, so the live project's plan, retention, and restore permissions
have not been verified. Use Neon Console/API for production recovery; do not
point the SQLite restore tool at a Neon database.

References: [Neon restore snapshot API](https://api-docs.neon.tech/reference/restoresnapshot),
[Neon backup schedule API](https://api-docs.neon.tech/reference/setsnapshotschedule).

## Continuous integration

GitHub Actions runs the full synthetic SQLite suite, starts a clean Windows
launcher smoke check, and runs PostgreSQL integration tests against a temporary
Postgres 16 service. The PostgreSQL fixture creates and removes an isolated
schema. CI uses synthetic credentials and data only; it does not use production
secrets or deploy the app.

## Privacy

Statements, including the original uploaded Excel workbooks, are stored in the
configured database and are not encrypted. SQLite backups are local; Neon backups are managed separately. Unless you click
the optional AI analysis, no transaction data is sent to OpenAI.
It sends selected-period expense dates, amounts, descriptions, and category
names to OpenAI; account/balance columns and the workbook are excluded, but
details inside descriptions may be sent. Provider data terms apply. Enable AI
in PowerShell with `$env:OPENAI_API_KEY = "YOUR_API_KEY"` for the current
session. Never put the key in code or chat. AI suggestions are reviewed and
approved by default; separate auto-apply is optional. **AI шинжилгээ** shows
period-specific expense
totals by category and merchant, counts, confidence, and reasons; results are
saved locally. Imported workbooks are excluded from version control.
Streamlit usage statistics are disabled. Files and backups are not encrypted.
