# Foldit Monitor

Foldit Monitor is a Windows desktop companion for people who run one or more local Foldit clients. It watches scores and logs, keeps puzzle statistics, and can connect monitors on a local network.

## Start here

You need Windows and at least one local Foldit client. The recommended way to use Foldit Monitor is the Python version; a packaged Windows EXE is also available when a release includes one.

### Recommended: run with Python

This option receives updates directly from the source and is the easiest way to see an error message if something needs attention.

1. Install [Python 3.11 or newer](https://www.python.org/downloads/windows/). During installation, select **Add Python to PATH**.
2. Download this repository with **Code → Download ZIP**, then extract the ZIP.
3. Open a terminal in the extracted folder.
4. Install the required packages:

   ```powershell
   python -m pip install -r requirements.txt
   ```

5. Start Foldit Monitor:

   ```powershell
   python "Foldit Monitor.pyw"
   ```

If `python` is not recognized, reinstall Python and make sure **Add Python to PATH** is selected.

### Alternative: Windows EXE

When a release provides `FolditMonitor-windows-x64.zip`, download it from the repository's **Releases** page, extract the entire ZIP, and run `FolditMonitor.exe`. The ZIP already includes everything required, so Python does not need to be installed.

## First launch

Foldit Monitor starts with the bundled [default profile](Foldit%20Monitor.defaults.json). It then creates a separate `Foldit Monitor.json` for your own machine. That local file stores window positions, the last puzzle, paths, and connections; it is intentionally not part of the repository.

The app creates `puzzle_logs/`, `logs/`, and `foldit_backup/` when it needs them. You can keep or remove those local files without affecting the source code.

## For maintainers: build the EXE

From a Python-enabled PowerShell in the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1
```

The script installs PyInstaller if required, creates a folder distribution in `dist/FolditMonitor/`, and packages it as `release/FolditMonitor-windows-x64.zip`. Build outputs are intentionally ignored by Git.
