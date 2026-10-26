"""Create a source-only snapshot; personal settings and runtime data stay separate."""
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
import zipfile


def main():
    root = Path(__file__).resolve().parent.parent
    destination = root / "archive" / f"sources-{datetime.now():%Y%m%d-%H%M%S-%f}.zip"
    destination.parent.mkdir(exist_ok=True)
    files = [root / "Foldit Monitor.pyw", root / "requirements.txt", root / "README.md", root / ".gitignore"]
    for directory in ("modules", "tests", "docs", "tools"):
        files.extend(path for path in (root / directory).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    window = tk.Tk()
    window.withdraw()
    try:
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as snapshot:
            for path in sorted(set(files)):
                snapshot.write(path, path.relative_to(root))
        messagebox.showinfo("Source backup", f"Saved {len(set(files))} files:\n{destination}")
    except Exception as error:
        messagebox.showerror("Source backup", str(error))
    finally:
        window.destroy()


if __name__ == "__main__":
    main()
