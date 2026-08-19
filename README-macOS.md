# Foldit Monitor on macOS

Python 3.11-3.13 is supported; Python 3.13 is recommended. Use a Python build
with Tk support:

```bash
python3.13 -m pip install -r requirements.txt
python3.13 "Foldit Monitor.pyw"
```

Homebrew Python may require the matching Tk formula, for example
`brew install python-tk@3.13`.

Foldit Monitor discovers the executable inside `Foldit.app`, but reads script
logs and puzzle data from `Foldit.app/Contents/Resources`. **New Client** first
launches an installation that is not already running. Only when every
discovered installation is in use does it start another instance of a running
bundle with `open -n`.

Window titles can be unavailable until Screen Recording access is granted to
the terminal or Python host that runs the monitor. This permission is optional:
process discovery, log ownership detection, score/script monitoring, and
application activation continue to work without a title. Puzzle statistics
require a puzzle id from either the window title or the optional JSON fallback.

Accessibility permission is not used by this version.

## Possible issues

### No alert sound on Python 3.14

Foldit Monitor can notify you about events with a sound. Python 3.14 is outside
the supported range, and pygame currently has no macOS package for that Python
version. `requirements.txt` therefore skips pygame automatically on Python
3.14. The monitor can still run, but sound alerts are unavailable; use Python
3.13 for full functionality.

### One Foldit installation with multiple Tracks

Several Foldit processes can run from the same `Foldit.app` while using
different Tracks. Foldit Monitor normally detects this automatically: it
inspects the script-log files opened by each process and associates every PID
with its exact `scriptlog.<track>.xml` file.

When several processes use different Tracks in one bundle, save copying
addresses the exact `puzzles/<puzzle>/<user>/<track>` directory. Copying between
two rows therefore replaces and backs up only the target Track; sibling Tracks
remain untouched.

If automatic ownership detection cannot identify a log, add an explicit entry
to the existing `Foldit Monitor.json` file:

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

Only `log_path` is required. `name`, `title_suffix`, and `puzzle_id` provide
better row names, PID matching, and statistics when that information is known.
A configured log remains visible as a logical client when it cannot be
associated with a PID; CPU and window actions are then unavailable for that
row.
