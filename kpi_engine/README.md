# QBO KPI Dashboard

Local-first Python tool that parses QuickBooks Online report exports
(CSV/XLSX) for small-business clients, stores them in per-client SQLite
databases, computes KPIs and a 13-week cash forecast, and generates a
self-contained offline HTML dashboard. No internet dependencies at runtime —
every artifact opens offline and can be emailed as a single file.

## Requirements

- Python 3.11+
- `pip install -r requirements.txt` (openpyxl for .xlsx reading; pytest for
  the test suite)

## Running the app

```
python run.py            # starts the localhost app and opens a browser
python run.py --version
```

The app binds `127.0.0.1` on the first free port in 8400–8499, creates a
hidden `.appdata/` folder on first run, and shuts itself down after 30
minutes of inactivity. From the home page you can add a client (which opens
the onboarding wizard), drop in QBO exports, review account categories,
and click **Update Dashboard**.

## Where client data lives

All data stays on the machine running the app — nothing is uploaded. Each
client gets one self-contained folder under the data directory:

```
<data root>/
  clients/<client>/
    client.db        # the client's database
    exports/         # uploaded QBO reports (and processed/)
    dashboards/      # generated dashboard HTML
    proposals/       # COA standardization proposals
  aliases.db         # shared learned account mappings
  registry.json      # the client list
  logs/
```

The data root is `./.appdata` when running from source, or a visible
`~/QBO KPI Dashboard` folder when running as a packaged app (override with
`QBO_APPDATA_DIR`). To back up or move a client, copy its folder.

## Packaging for a client

**Option A — true double-click app (recommended).** Bundles Python and every
dependency into one application; the client needs nothing pre-installed and
runs it with no terminal:

```
pip install pyinstaller
python scripts/build_app.py
```

Output is `dist/QBO KPI Dashboard.app` (macOS) or
`dist/QBO KPI Dashboard/` with an `.exe` (Windows). Build on the **same OS**
you're shipping to — PyInstaller does not cross-compile. The app is unsigned,
so on first launch the client right-clicks → **Open** on macOS (or **More
info → Run anyway** on Windows SmartScreen); signing/notarizing to remove that
prompt requires an Apple Developer or Windows code-signing certificate.

**Option B — venv launcher (needs Python + internet on first run).**

```
python scripts/make_dist.py [--out dist]
```

Produces a `dist/` folder with the app code, a `README_FIRST.html` guide, and
`Start.command` (macOS) / `Start.bat` (Windows) launchers that create a
private virtualenv and install dependencies on first run.

## Generating a dashboard from the CLI

```
python -m dashboard.generate <client_slug> <out_dir> [--scenario BASE|STRETCH|CRUNCH] [--name "Display Name"]
```

Writes one `{Client}_Dashboard_{YYYY-MM}.html` into `<out_dir>`. All three
forecast scenarios are embedded and switchable in the page; print falls back
to BASE.

## Layout

- `run.py` — entry point (`--version`, first-run setup, launches the server)
- `core/` — detection, parsers, importer, classifier, KPI + forecast engine,
  COA proposal, DB layer
- `dashboard/` — offline HTML dashboard generator
- `app/` — localhost server, JSON API, and static pages (home, wizard,
  mapping, settings)
- `data/` — taxonomy, keyword rules, factory account aliases
- `scripts/` — `make_dist.py` distribution builder
- `tests/` — pytest suite

## Testing

```
python -m pytest -q
```
