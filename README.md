# Foldit Monitor

Foldit Monitor is a desktop helper for watching local Foldit clients, tracking scores and logs, viewing puzzle statistics, and connecting monitors over a local network.

[![Main window](https://github.com/sercatp/FolditMonitor/raw/main/images/main-window.png)](https://github.com/sercatp/FolditMonitor/blob/main/images/main-window.png)

[![Stats window](https://github.com/sercatp/FolditMonitor/raw/main/images/stats-window.jpg)](https://github.com/sercatp/FolditMonitor/blob/main/images/stats-window.jpg)


## Install and run

You need at least one local Foldit client and Python 3.11-3.13. Python 3.13
is recommended. Running the Python version is recommended: it is easier to
update and shows useful error messages if something needs attention.

1. Install [Python](https://www.python.org/downloads/).
2. Download this repository with **Code → Download ZIP**, then extract the archive.
3. Open a terminal in the extracted project folder and create a virtual
   environment:

   ```bash
   python -m venv .venv
   ```

   Activate it with `.venv\Scripts\activate` on Windows or
   `source .venv/bin/activate` on Linux and macOS.

4. Install the packages:

   ```bash
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

5. Start the monitor:

   ```bash
   python "Foldit Monitor.pyw"
   ```

### Alternative: Windows EXE

If you do not want to install Python, open [the latest release](https://github.com/sercatp/FolditMonitor/releases/latest), download `FolditMonitor-windows-x64.zip`, extract the entire ZIP, and run `FolditMonitor.exe`. The ZIP already includes everything required.

## Platform notes

`requirements.txt` installs the appropriate Python window-integration package for the current operating system.

- **Windows:** fully supported. The Python installation receives `pywin32` and the shortcut helper; the ZIP release is also available for Windows x64.
- **Linux:** install the system Tk package as well as the Python requirements (for example, `python3-tk` on Debian/Ubuntu). Client-window detection and activation require an X11 session or XWayland; a pure Wayland session is not supported by the current window manager.
- **macOS:** install a Python build with Tk support; the requirements install
  the Cocoa and Quartz bridges automatically. Client-window integration remains
  experimental and there is no packaged macOS release. See the complete
  [macOS setup and multi-Track notes](README-macOS.md).

## First launch

Foldit Monitor starts with the bundled [default profile](Foldit%20Monitor.defaults.json), then creates a separate local `Foldit Monitor.json`. That local file keeps window positions, paths, last-used puzzle data, and connections; it is intentionally not part of the repository.

The app also creates `logs/`, `puzzle_logs/`, and `foldit_backup/` when needed. These are local working folders and can be kept or removed without changing the source code.

## Main files

- `Foldit Monitor.pyw` — main application.
- `settings.py` — default and local settings.
- `network.py` — local-network synchronisation and artifact transfer.
- `stats_*.py`, `logger.py`, `log_lookup.py` — score/log parsing and the statistics UI.
- `save_catalog.py`, `save_manager_qt.py`, `savefile_api.py` — indexed save-file browsing and export.
- `foldit_speed_boost*.py` — optional Frida-based speed-boost integration.
- `alert.wav` — default alert sound.

Speed Boost is disabled by default. To expose its menu, set
`speed_boost.enabled` to `true` in the generated `Foldit Monitor.json` and
restart the monitor. The same section contains the configured return-address
`offsets`; invalid or empty lists keep the feature disabled.

## Possible issues

### No alert sound on Python 3.14

Foldit Monitor can notify you about events with a sound. Python 3.14 is outside
the supported range, and pygame currently has no macOS package for that Python
version. `requirements.txt` therefore skips pygame automatically on Python
3.14. The monitor can still run, but sound alerts are unavailable; use Python
3.13 for full functionality.

Do not remove `psutil` from `requirements.txt`: it is required for process
discovery and is not the package causing this Python 3.14 installation issue.

### One installation with multiple Tracks

Several Foldit processes can use different Tracks from the same installation.
Foldit Monitor normally detects this automatically by inspecting which
`scriptlog.<track>.xml` file each process has open.

If a log remains unidentified, add a fallback to the existing
`Foldit Monitor.json` file:

```json
{
  "monitoring": {
    "log_fallbacks": [
      {
        "log_path": "/Applications/Foldit.app/Contents/Resources/scriptlog.Solo1.xml",
        "name": "Foldit Solo1",
        "title_suffix": " - Solo1",
        "puzzle_id": "2802"
      }
    ]
  }
}
```

Only `log_path` is required. The other fields improve display names, PID
matching, and statistics. If the configured log cannot be associated with a
PID, it still appears as a logical client without CPU and window actions.
