"""Arcform jobs, and handing them to a single Arcform window.

When several files are selected, Explorer starts one Arcform per file. The
first one opens the window; the rest send it their job over a named pipe and
exit, so everything shows up in one list.

A job is a small dict, e.g. {"kind": "convert", "target": "jpg", "path": "..."}.
"""

import getpass
import json
import os
import shutil
import time
import traceback
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from pathlib import Path

import merge_queue
import shrink
import split
from converters import FORMAT_NAMES, VIDEO_EXTS, ConversionError, convert

PIPE = rf"\\.\pipe\arcform-{getpass.getuser()}"
SHARE_PIPE = PIPE + "-share"  # the Send to phone window gathers its files the same way
AUTHKEY = b"arcform"
LOG_FILE = Path(__file__).resolve().parent / "arcform_error.log"
# "Copy as" results live here until pasted; old ones are cleaned up.
CLIPBOARD_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Arcform" / "Clipboard"

# Jobs wait in lanes, each with its own number of worker threads. Word handles one
# document at a time, ffmpeg already uses every core, and the PDF library isn't safe
# to use from several threads; images run side by side.
LANE_THREADS = {"word": 1, "video": 1, "pdf": 1, "images": min(4, os.cpu_count() or 1)}


def lane(task) -> str:
    ext = Path(task["path"]).suffix.lower()
    if task["kind"] == "merge" or ext == ".pdf":
        return "pdf"
    if ext in (".doc", ".docx"):
        return "word"
    if ext in VIDEO_EXTS:
        return "video"
    return "images"


def describe(task):
    """(what, action) for showing a job in the window."""
    kind = task["kind"]
    if kind == "merge":
        return f"{merge_queue.count()} PDFs", "Merge into Merged.pdf"
    name = Path(task["path"]).name
    if kind == "convert":
        return name, f"Convert to {FORMAT_NAMES[task['target']]}"
    if kind == "copy":
        return name, f"Copy as {FORMAT_NAMES[task['target']]}"
    if kind == "shrink":
        return name, f"Shrink under {task['size']} MB"
    if kind == "split":
        if task["mode"] == "custom":
            files = len(task["parts"])
            return name, f"Split into {files} file{'s' if files > 1 else ''}"
        return name, split.ACTIONS[task["mode"]]
    raise ValueError(f"Unknown job: {kind}")


def run(task, progress=None):
    """Do the job. Returns (output file or None, note)."""
    kind = task["kind"]
    path = Path(task["path"])
    if kind == "convert":
        return convert(path, task["target"], progress=progress), ""
    if kind == "copy":
        return convert(path, task["target"], folder=_clipboard_folder(), progress=progress), ""
    if kind == "shrink":
        return shrink.shrink(path, task["size"], progress)
    if kind == "merge":
        return merge_queue.generate(path)
    if kind == "split":
        return split.split(path, task["mode"], task.get("parts"), progress)
    raise ValueError(f"Unknown job: {kind}")


def _clipboard_folder() -> Path:
    CLIPBOARD_DIR.mkdir(parents=True, exist_ok=True)
    day_ago = time.time() - 24 * 3600
    for old in CLIPBOARD_DIR.iterdir():
        if old.is_dir() and old.stat().st_mtime < day_ago:
            shutil.rmtree(old, ignore_errors=True)
    # One folder per copy keeps the original file name for pasting.
    folder = CLIPBOARD_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"
    folder.mkdir()
    return folder


def describe_error(exc: Exception) -> str:
    if isinstance(exc, ConversionError):
        return str(exc)
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write(traceback.format_exc() + "\n")
    return f"{type(exc).__name__}: {exc}\n\nDetails saved to {LOG_FILE.name}"


# --------------------------------------------------------- one shared window

def submit(task, open_window, pipe=PIPE) -> None:
    """Send the job to the open Arcform window, or become that window."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            # Only one process can create the pipe, and that one shows the window.
            listener = Listener(pipe, family="AF_PIPE", authkey=AUTHKEY)
        except OSError:
            pass
        else:
            open_window(listener, task)
            return
        try:
            with Client(pipe, family="AF_PIPE", authkey=AUTHKEY) as conn:
                conn.send_bytes(json.dumps(task).encode())
                if conn.recv_bytes() == b"ok":
                    return
        except (OSError, EOFError, AuthenticationError):
            pass  # the window was closing; try again
        time.sleep(0.05)
    raise RuntimeError("Couldn't reach the Arcform window.")


def serve(listener, deliver, stopped) -> None:
    """Pass jobs sent by other Arcform processes to `deliver` until `stopped` is set.

    Runs in a thread.
    """
    while not stopped.is_set():
        try:
            with listener.accept() as conn:
                deliver(json.loads(conn.recv_bytes()))
                conn.send_bytes(b"ok")
        except (OSError, EOFError, ValueError, AuthenticationError):
            pass
