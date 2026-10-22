"""Arcform - convert files from the Windows right-click menu.

Usage (add --no-gui to print the result instead of showing a window):
    arcform.py convert <format> <file>
    arcform.py copy <format> <file>          convert, then put the result on the clipboard
    arcform.py shrink <MB> <file>            make a copy under the size limit

PDF merge queue:
    arcform.py merge-add <pdf>
    arcform.py merge-view <folder-or-file>
    arcform.py merge-generate <folder-or-file>     (saves Merged.pdf there)
    arcform.py merge-clear
"""

import os
import sys
from pathlib import Path

# pythonw has no console, so libraries that print would crash without this.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import converters  # noqa: E402
import merge_queue  # noqa: E402
import tasks  # noqa: E402


def parse_task(command, args):
    if command in ("convert", "copy") and len(args) == 2:
        return {"kind": command, "target": args[0].lower(), "path": args[1]}
    if command == "shrink" and len(args) == 2 and args[0].isdigit():
        return {"kind": "shrink", "size": int(args[0]), "path": args[1]}
    if command == "merge-generate" and len(args) == 1:
        return {"kind": "merge", "path": args[0]}
    return None


def run_in_console(task) -> int:
    try:
        dst, note = tasks.run(task)
        if dst and task["kind"] == "copy":
            import clipboard
            clipboard.copy_files([dst])
        print("\n".join(line for line in (f"Saved: {dst}" if dst else "", note) if line))
        return 0
    except Exception as exc:
        print(f"Error: {tasks.describe_error(exc)}", file=sys.stderr)
        return 1
    finally:
        converters.close_word()


def run_quietly(action) -> int:
    """For queue changes: no window, the menu label shows the new count."""
    try:
        action()
        return 0
    except Exception as exc:
        print(f"Error: {tasks.describe_error(exc)}", file=sys.stderr)
        return 1


def main(argv) -> int:
    gui = "--no-gui" not in argv
    args = [a for a in argv if a != "--no-gui"]
    command, rest = (args[0], args[1:]) if args else ("", [])

    if command == "merge-add" and len(rest) == 1:
        return run_quietly(lambda: merge_queue.add(Path(rest[0])))
    if command == "merge-clear" and not rest:
        return run_quietly(merge_queue.clear)

    if command == "merge-view" and len(rest) == 1:
        import window
        if not window.MergeQueueWindow(Path(rest[0])).show():
            return 0
        task = {"kind": "merge", "path": rest[0]}
    else:
        task = parse_task(command, rest)
    if task is None:
        print(__doc__)
        return 2

    if not gui:
        return run_in_console(task)
    import window
    tasks.submit(task, window.open_task_window)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
