# Foldit Monitor

Foldit Monitor is a desktop helper for watching local Foldit clients, tracking scores and logs, viewing puzzle statistics, and connecting monitors over a local network.

## Install and run

You need at least one local Foldit client and Python 3.11 or newer. Running the Python version is recommended: it is easier to update and shows useful error messages if something needs attention.

1. Install [Python](https://www.python.org/downloads/).
2. Download this repository with **Code → Download ZIP**, then extract the archive.
3. Open a terminal in the extracted project folder and install the packages:

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. Start the monitor:

   ```powershell
   python "Foldit Monitor.pyw"
   ```

If your system calls Python `python3`, use that command instead of `python`.

### Alternative: Windows EXE

If you do not want to install Python, open [the latest release](https://github.com/sercatp/FolditMonitor/releases/latest), download `FolditMonitor-windows-x64.zip`, extract the entire ZIP, and run `FolditMonitor.exe`. The ZIP already includes everything required.

## Screenshots

[![Main window](https://github.com/sercatp/FolditMonitor/raw/main/images/main-window.png)](https://github.com/sercatp/FolditMonitor/blob/main/images/main-window.png)

[![Stats window](https://github.com/sercatp/FolditMonitor/raw/main/images/stats-window.jpg)](https://github.com/sercatp/FolditMonitor/blob/main/images/stats-window.jpg)

## First launch

Foldit Monitor starts with the bundled [default profile](Foldit%20Monitor.defaults.json), then creates a separate local `Foldit Monitor.json`. That local file keeps window positions, paths, last-used puzzle data, and connections; it is intentionally not part of the repository.

The app also creates `logs/`, `puzzle_logs/`, and `foldit_backup/` when needed. These are local working folders and can be kept or removed without changing the source code.

## Main files

- `Foldit Monitor.pyw` — main application.
- `settings.py` — default and local settings.
- `network.py` — local-network synchronisation and artifact transfer.
- `stats_*.py`, `logger.py`, `log_lookup.py` — score/log parsing and the statistics UI.
- `foldit_speed_boost*.py` — optional Frida-based speed-boost integration.
- `alert.wav` — default alert sound.
