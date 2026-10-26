# Project structure

Start the application with `Foldit Monitor.pyw` in the project root. The launcher
keeps its original name and location, so existing shortcuts continue to work.
Other Python files are implementation modules and do not need to be launched.

```text
Foldit Monitor.pyw       Application launcher
requirements.txt        Python dependencies
modules/                Application code
modules/resources/      Bundled default settings and alert sound
data/                   Local settings and generated files; created when needed
docs/                   Instructions and screenshots
tests/                  Automated tests
tools/                  Optional build and source-backup utilities
```

Keep `modules/` and its resources together with the launcher. Resource and data
paths do not depend on the terminal's current working directory. A packaged EXE
keeps writable data in `data/` beside the executable, outside its extracted
application resources.

## Local data

`data/` contains the local `Foldit Monitor.json`, settings backups, `puzzle_logs/`,
`ligand_viewer_reports/`, and diagnostic `logs/`. Puzzle logs also contain notes,
local snapshots, received files, and the save index. Back up this folder
separately from the source code.

Relative puzzle-log folders in settings are resolved inside `data/`. Absolute
paths allow data to be stored elsewhere. Bundled defaults in
`modules/resources/Foldit Monitor.defaults.json` are separate from local settings.

When upgrading from the older flat layout, the application moves recognized
settings and local data folders during startup, before opening logs or the save
index. Existing custom paths retain their intended locations. If different files
already exist at both locations, migration stops instead of overwriting either
file. Save backups beside Foldit installations keep their existing location.

## Development utilities

Run the automated tests from the project root:

```text
python -m unittest discover -s tests -q
```

`tools/build_windows_exe.ps1` builds the Windows application with its bundled
resources. `tools/backup_sources.pyw` creates a source ZIP in `archive/` and
excludes personal settings and runtime data.

The repository's `.gitignore` excludes `data/` and optional local research and
archive folders. Public updates should include application code, bundled
resources, dependencies, documentation, and tests; personal profiles, generated
reports, and local research stay in the local workspace.
