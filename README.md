# Foldit Monitor

Foldit Monitor is a small desktop helper for watching local Foldit clients, tracking scores/log output, viewing puzzle stats, and optionally connecting monitors over the local network.

## Contents

- `Foldit Monitor.pyw` - main Tk application.
- `settings.py` - defaults and runtime settings handling.
- `network.py` - local network sync and artifact transfer.
- `stats_*.py`, `logger.py`, `log_lookup.py` - score/log parsing and stats UI.
- `foldit_speed_boost*.py` - optional Frida-based speed boost integration.
- `alert.wav` - default alert sound.
- `tests/` - unit tests.

Runtime folders such as `logs/`, `puzzle_logs/`, `foldit_backup/`, `__pycache__/`, and the generated `Foldit Monitor.json` are intentionally not tracked.

## Requirements

- Windows with Python 3.11+.
- Foldit clients running locally.
- Python packages from `requirements.txt`.

Optional features:

- Install `PySide6` to use the Qt stats UI (`display.stats_ui_backend = "pyside6"`).
- Install `frida` to use the speed boost menu items.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Optional dependencies:

```powershell
.\.venv\Scripts\python -m pip install -r requirements-optional.txt
```

## Run

```powershell
.\.venv\Scripts\pythonw "Foldit Monitor.pyw"
```

For console output while debugging:

```powershell
.\.venv\Scripts\python "Foldit Monitor.pyw"
```

On first start the app creates `Foldit Monitor.json` from defaults. That file stores local window positions, last selected puzzle, network address, and other user preferences, so it is ignored by git.

`Foldit Monitor.example.json` is a clean default settings example.

## Test

```powershell
.\.venv\Scripts\python -m unittest discover -s tests
```

Some Qt tests are skipped unless `PySide6` is installed.
