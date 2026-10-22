"""The PDF merge queue.

PDFs are added one at a time from the right-click menu, then merged together
with "Generate merge". The queue is a small JSON file, kept in merge order; a
lock file keeps it safe when several PDFs are added at once (Explorer starts
one process per file).
"""

import json
import msvcrt
import os
import time
from contextlib import contextmanager
from pathlib import Path

import menu
from converters import ConversionError, merge_pdfs, output_path

QUEUE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Arcform"
QUEUE_FILE = QUEUE_DIR / "merge_queue.json"
LOCK_FILE = QUEUE_DIR / "merge_queue.lock"

# Files added within this many seconds of each other were selected together,
# so they are put in name order rather than whichever process ran first.
BATCH_SECONDS = 2
LOCK_TIMEOUT = 30


@contextmanager
def _locked():
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "a+") as lock:
        lock.seek(0)
        # Retry quickly ourselves: msvcrt's waiting lock sleeps a whole second per try
        # and gives up after 10, too slow and fragile for a big multi-select.
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise ConversionError("The merge queue is busy. Try again.")
                time.sleep(0.01)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _load():
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))["files"]
    except (FileNotFoundError, KeyError, ValueError):
        return []


def _save(entries) -> None:
    # Write a new file and swap it in, so readers never see a half-written queue.
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"files": entries}, indent=2), encoding="utf-8")
    os.replace(tmp, QUEUE_FILE)
    menu.refresh_merge_menu(len(entries))


def paths():
    return [Path(e["path"]) for e in _load()]


def count() -> int:
    return len(_load())


def modified():
    """Changes whenever the queue does, so a window can notice outside edits."""
    try:
        return QUEUE_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def add(pdf: Path) -> None:
    pdf = Path(pdf).resolve()
    with _locked():
        entries = _load()
        if any(Path(e["path"]) == pdf for e in entries):
            return
        entries.append({"path": str(pdf), "added": time.time()})
        # Find the files added along with this one and keep them in name order.
        start = len(entries) - 1
        while start > 0 and abs(entries[start]["added"] - entries[start - 1]["added"]) <= BATCH_SECONDS:
            start -= 1
        entries[start:] = sorted(entries[start:], key=lambda e: Path(e["path"]).name.lower())
        _save(entries)


def remove(pdf: Path) -> None:
    with _locked():
        _save([e for e in _load() if Path(e["path"]) != Path(pdf)])


def reorder(order) -> None:
    """Put the queue in the given order. Files added meanwhile stay at the end."""
    rank = {Path(p): i for i, p in enumerate(order)}
    with _locked():
        entries = _load()
        entries.sort(key=lambda e: rank.get(Path(e["path"]), len(rank)))
        _save(entries)


def clear() -> None:
    with _locked():
        _save([])


def generate(where: Path):
    """Merge the queued PDFs into the folder `where` (or the folder holding it).

    Returns (output path, note about skipped files).
    """
    where = Path(where)
    folder = where if where.is_dir() else where.parent

    with _locked():
        entries = _load()
    present = [Path(e["path"]) for e in entries if Path(e["path"]).exists()]
    missing = len(entries) - len(present)
    if len(present) < 2:
        raise ConversionError(
            f"The merge queue needs at least 2 PDFs (it has {len(present)})."
            + (f"\n{missing} queued file(s) were moved or deleted." if missing else "")
        )

    dst = output_path(folder / "Merged.pdf", "pdf")
    merge_pdfs(present, dst)

    # Remove only what was merged, in case more PDFs were queued meanwhile.
    done = {e["path"] for e in entries}
    with _locked():
        _save([e for e in _load() if e["path"] not in done])

    note = f"Skipped {missing} file(s) that were moved or deleted." if missing else ""
    return dst, note
