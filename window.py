"""Arcform's windows: the job list and the merge queue editor."""

import ctypes
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

import clipboard
import converters
import menu
import merge_queue
import tasks

# "Copy as" jobs arriving this close together came from one multi-select,
# so their files go on the clipboard together.
COPY_BATCH_SECONDS = 2
# A worker with nothing to do for this long lets Word quit.
IDLE_SECONDS = 3
MUTED = "#5f6368"
ERROR = "#b3261e"


def _make_root(title):
    """Create a Tk window. Returns (root, px, heading font); px scales sizes for the screen's DPI."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
    except (AttributeError, OSError):
        pass  # already set by an earlier window in this process
    root = tk.Tk()
    root.title(title)
    if menu.ICON.exists():
        root.iconbitmap(default=str(menu.ICON))
    scale = root.winfo_fpixels("1i") / 96

    def px(n):
        return int(n * scale)

    ttk.Style(root).configure("Treeview", rowheight=px(24))
    heading = font.nametofont("TkDefaultFont").copy()
    size = heading.cget("size")
    heading.configure(size=size + 3 if size > 0 else size - 4, weight="bold")  # negative = pixels
    return root, px, heading


def _place(root):
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
    y = (root.winfo_screenheight() - root.winfo_reqheight()) // 3
    root.geometry(f"+{x}+{y}")


def _show_in_folder(path: Path) -> None:
    subprocess.Popen(["explorer", "/select,", str(path)])


# ------------------------------------------------------------------ job list

def open_task_window(listener, first_task) -> None:
    window = TaskWindow(listener)
    window.add(first_task)
    window.run()


class TaskWindow:
    """One window listing every job, including ones sent by other Arcform processes."""

    def __init__(self, listener):
        self.listener = listener
        self.rows = []
        self.events = queue.Queue()  # from the worker and pipe threads, handled on the Tk thread
        self.lanes = {name: queue.Queue() for name in tasks.LANE_THREADS}
        self.stopped = threading.Event()
        self.copy_group = []
        self.last_copy_at = 0.0
        self.clipboard_lock = threading.Lock()

        self.root, px, heading = _make_root("Arcform")
        root = self.root
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(root, padding=px(16))
        frame.pack(fill="both", expand=True)

        self.header = ttk.Label(frame, font=heading)
        self.header.pack(anchor="w")

        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True, pady=(px(10), 0))
        self.tree = ttk.Treeview(table, columns=("what", "action", "status"), show="headings",
                                 height=1, selectmode="browse")
        for col, title, width in (("what", "File", 210), ("action", "Action", 170), ("status", "Status", 260)):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=px(width), anchor="w")
        self.tree.tag_configure("failed", foreground=ERROR)
        self.tree.tag_configure("skipped", foreground=MUTED)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_details())
        self.tree.bind("<Double-1>", lambda e: self._open())

        self.bar = ttk.Progressbar(frame, mode="determinate", maximum=1.0)
        self.bar.pack(fill="x", pady=(px(12), 0))
        self.details = ttk.Label(frame, wraplength=px(640), justify="left", foreground=MUTED)
        self.details.pack(anchor="w", fill="x", pady=(px(8), 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(px(12), 0))
        self.open_button = ttk.Button(buttons, text="Open", command=self._open, state="disabled")
        self.open_button.pack(side="left")
        self.folder_button = ttk.Button(buttons, text="Show in folder", command=self._show_folder,
                                        state="disabled")
        self.folder_button.pack(side="left", padx=(px(6), 0))
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")

        root.attributes("-topmost", True)
        self.workers = [threading.Thread(target=self._work, args=(self.lanes[name],), daemon=True)
                        for name, count in tasks.LANE_THREADS.items() for _ in range(count)]
        for worker in self.workers:
            worker.start()
        threading.Thread(target=tasks.serve, daemon=True,
                         args=(listener, lambda task: self.events.put(("add", task)), self.stopped)).start()

    def run(self) -> None:
        _place(self.root)
        self.root.after(100, self._poll)
        self.root.mainloop()

    def add(self, task) -> None:
        what, action = tasks.describe(task)
        row = {"task": task, "state": "waiting", "fraction": 0.0, "dst": None, "note": "", "error": "",
               "iid": self.tree.insert("", "end", values=(what, action, "Waiting"))}
        if task["kind"] == "copy":
            now = time.monotonic()
            if now - self.last_copy_at > COPY_BATCH_SECONDS:
                self.copy_group = []
            self.last_copy_at = now
            row["copy_group"] = self.copy_group
        self.rows.append(row)
        self.lanes[tasks.lane(task)].put(row)
        self.tree.configure(height=min(len(self.rows), 8))
        self.root.attributes("-topmost", True)
        self._refresh()

    # Runs on worker threads: each takes jobs from its lane and reports back through events.
    def _work(self, lane) -> None:
        while True:
            try:
                row = lane.get(timeout=IDLE_SECONDS)
            except queue.Empty:
                converters.close_word()  # nothing to do for now, so don't keep Word running
                continue
            if row is None or self.stopped.is_set():
                converters.close_word()
                return
            self.events.put(("start", row))
            try:
                dst, note = tasks.run(row["task"], lambda f, row=row: self.events.put(("progress", row, f)))
                if dst and "copy_group" in row:
                    with self.clipboard_lock:
                        group = row["copy_group"]
                        group.append(dst)
                        clipboard.copy_files(group)
                        copied = len(group)
                    note = "Copied to the clipboard" if copied == 1 else f"Copied ({copied} files on the clipboard)"
                self.events.put(("done", row, dst, note))
            except Exception as exc:
                self.events.put(("failed", row, tasks.describe_error(exc)))

    def _poll(self) -> None:
        try:
            while True:
                event, *data = self.events.get_nowait()
                if event == "add":
                    self.add(data[0])
                else:
                    self._update(event, *data)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _update(self, event, row, *data) -> None:
        if event == "start":
            row["state"] = "running"
        elif event == "progress":
            row["fraction"] = data[0]
        elif event == "done":
            row["state"], row["fraction"] = "done", 1.0
            row["dst"], row["note"] = data
            # Files copied together share the clipboard, so their rows share the latest count.
            for other in self.rows:
                if other is not row and other.get("copy_group") is row.get("copy_group", ()) and other["dst"]:
                    other["note"] = row["note"]
                    self._show_row(other)
        elif event == "failed":
            row["state"], row["error"] = "failed", data[0]
        self._show_row(row)
        if event in ("done", "failed") and not self.tree.selection():
            self.tree.selection_set(row["iid"])
        self._refresh()

    def _show_row(self, row) -> None:
        failed, skipped = row["state"] == "failed", row["state"] == "done" and not row["dst"]
        what, action = self.tree.item(row["iid"], "values")[:2]
        self.tree.item(row["iid"], values=(what, action, self._status(row)),
                       tags=("failed",) if failed else ("skipped",) if skipped else ())

    @staticmethod
    def _status(row) -> str:
        state = row["state"]
        if state == "waiting":
            return "Waiting"
        if state == "running":
            return f"Working… {row['fraction']:.0%}" if row["fraction"] else "Working…"
        if state == "failed":
            return "✗ " + row["error"].splitlines()[0]
        if not row["dst"]:
            return row["note"]
        if row["task"]["kind"] == "copy":
            return "✓ " + row["note"]
        return "✓ " + row["dst"].name

    def _refresh(self) -> None:
        total = len(self.rows)
        finished = sum(r["state"] in ("done", "failed") for r in self.rows)
        failed = sum(r["state"] == "failed" for r in self.rows)
        self.bar["value"] = sum(r["fraction"] for r in self.rows) / total
        if finished < total:
            self.header.config(text="Working…" if total == 1 else f"Working… {finished} of {total} done")
        else:
            if not failed:
                text = "Done"
            elif total == 1:
                text = "Couldn't finish"
            else:
                text = f"Done, {failed} of {total} failed"
            self.header.config(text=text)
            self.root.attributes("-topmost", False)
        self._show_details()

    def _selected(self):
        selection = self.tree.selection()
        return next((r for r in self.rows if r["iid"] in selection), None)

    def _show_details(self) -> None:
        row = self._selected()
        text = ""
        if row and row["state"] == "failed":
            text = row["error"]
        elif row and row["state"] == "done":
            if row["task"]["kind"] == "copy" and row["dst"]:
                text = f"{row['note']}. Paste it anywhere with Ctrl+V."
            elif row["dst"]:
                text = f"Saved to {row['dst']}" + (f"\n{row['note']}" if row["note"] else "")
            else:
                text = row["note"]
        self.details.config(text=text, foreground=ERROR if row and row["state"] == "failed" else MUTED)
        state = "normal" if row and row["dst"] else "disabled"
        self.open_button.config(state=state)
        self.folder_button.config(state=state)

    def _open(self) -> None:
        row = self._selected()
        if row and row["dst"]:
            os.startfile(row["dst"])
            self._close_if_single()

    def _show_folder(self) -> None:
        row = self._selected()
        if row and row["dst"]:
            _show_in_folder(row["dst"])
            self._close_if_single()

    def _close_if_single(self) -> None:
        if len(self.rows) == 1:
            self.close()

    def close(self) -> None:
        unfinished = sum(r["state"] in ("waiting", "running") for r in self.rows)
        if unfinished and not messagebox.askyesno(
                "Arcform", f"Stop {unfinished} unfinished job{'s' if unfinished > 1 else ''}?", parent=self.root):
            return
        self.stopped.set()
        try:
            self.listener.close()
        except OSError:
            pass
        for proc in list(converters.RUNNING):
            proc.kill()  # the job then fails and removes its partial file
        for name, count in tasks.LANE_THREADS.items():
            for _ in range(count):
                self.lanes[name].put(None)
        deadline = time.monotonic() + 10
        for worker in self.workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        self.root.destroy()


# --------------------------------------------------------- merge queue editor

class MergeQueueWindow:
    """Reorder, remove and add PDFs in the merge queue, then merge them."""

    def __init__(self, where: Path):
        where = Path(where).resolve()
        self.folder = where if where.is_dir() else where.parent
        self.merge_requested = False
        self.paths = []
        self.pages = {}  # path -> (modified time, page count text)
        self.seen = None
        self.drag = None
        self.dragged = False

        self.root, px, heading = _make_root("Arcform - Merge queue")
        root = self.root
        frame = ttk.Frame(root, padding=px(16))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Merge queue", font=heading).pack(anchor="w")
        self.hint = ttk.Label(frame, foreground=MUTED)
        self.hint.pack(anchor="w", pady=(px(2), 0))

        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True, pady=(px(10), 0))
        side = ttk.Frame(body)
        side.pack(side="right", fill="y", padx=(px(10), 0))
        for text, command in (("Move up", lambda: self._shift(-1)), ("Move down", lambda: self._shift(1)),
                              ("Remove", self._remove), ("Add PDFs…", self._add), ("Clear all", self._clear)):
            ttk.Button(side, text=text, command=command).pack(fill="x", pady=(0, px(6)))

        self.tree = ttk.Treeview(body, columns=("n", "file", "pages", "folder"), show="headings",
                                 height=10, selectmode="browse")
        for col, title, width, anchor in (("n", "#", 40, "e"), ("file", "File", 240, "w"),
                                          ("pages", "Pages", 64, "e"), ("folder", "Folder", 280, "w")):
            self.tree.heading(col, text=title, anchor=anchor)
            self.tree.column(col, width=px(width), anchor=anchor, stretch=col in ("file", "folder"))
        self.tree.tag_configure("missing", foreground=ERROR)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<ButtonPress-1>", self._drag_start)
        self.tree.bind("<B1-Motion>", self._drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._drag_end)
        self.tree.bind("<Delete>", lambda e: self._remove())
        self.tree.bind("<Double-1>", lambda e: self._open())

        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", pady=(px(12), 0))
        ttk.Button(bottom, text="Close", command=root.destroy).pack(side="right")
        self.merge_button = ttk.Button(bottom, command=self._merge, default="active")
        self.merge_button.pack(side="right", padx=(0, px(6)))
        ttk.Label(bottom, text=f"Saves Merged.pdf to {self.folder}", foreground=MUTED,
                  wraplength=px(420)).pack(side="left")

        self._reload()
        root.after(1000, self._watch)

    def show(self) -> bool:
        """Show the window. Returns True if the user chose to merge."""
        _place(self.root)
        self.root.mainloop()
        return self.merge_requested

    def _reload(self, select=None) -> None:
        selected = self._selected_path() if select is None else select
        self.paths = merge_queue.paths()
        self.seen = merge_queue.modified()
        self.tree.delete(*self.tree.get_children())
        present = 0
        for i, path in enumerate(self.paths):
            exists = path.exists()
            present += exists
            self.tree.insert("", "end", iid=str(i), tags=() if exists else ("missing",),
                             values=(i + 1, path.name, self._page_count(path), str(path.parent)))
        if selected in self.paths:
            index = str(self.paths.index(selected))
            self.tree.selection_set(index)
            self.tree.see(index)
        self.hint.config(text="PDFs are merged from top to bottom. Drag rows to reorder." if self.paths
                         else "The queue is empty. Use Add PDFs…, or right-click a PDF → Arcform → Add to merge queue.")
        self.merge_button.config(text=f"Merge {present} PDFs", state="normal" if present >= 2 else "disabled")

    def _page_count(self, path: Path) -> str:
        try:
            modified = path.stat().st_mtime
        except OSError:
            return "missing"
        cached = self.pages.get(path)
        if cached and cached[0] == modified:
            return cached[1]
        try:
            import pymupdf
            with pymupdf.open(path) as doc:
                text = "locked" if doc.needs_pass else str(doc.page_count)
        except Exception:
            text = "?"
        self.pages[path] = (modified, text)
        return text

    def _watch(self) -> None:
        # Pick up PDFs added from the right-click menu while this window is open.
        if merge_queue.modified() != self.seen and not self.drag:
            self._reload()
        self.root.after(1000, self._watch)

    def _selected_index(self):
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _selected_path(self):
        index = self._selected_index()
        return self.paths[index] if index is not None else None

    def _shift(self, step: int) -> None:
        index = self._selected_index()
        if index is None or not 0 <= index + step < len(self.paths):
            return
        order = list(self.paths)
        order[index], order[index + step] = order[index + step], order[index]
        merge_queue.reorder(order)
        self._reload(select=order[index + step])

    def _remove(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        merge_queue.remove(self.paths[index])
        remaining = [p for i, p in enumerate(self.paths) if i != index]
        self._reload(select=remaining[min(index, len(remaining) - 1)] if remaining else None)

    def _add(self) -> None:
        chosen = filedialog.askopenfilenames(parent=self.root, title="Add PDFs to the merge queue",
                                             initialdir=self.folder, filetypes=[("PDF files", "*.pdf")])
        for name in chosen:
            merge_queue.add(Path(name))
        self._reload()

    def _clear(self) -> None:
        if self.paths and messagebox.askyesno("Arcform", "Remove every PDF from the merge queue?",
                                              parent=self.root):
            merge_queue.clear()
            self._reload()

    def _open(self) -> None:
        path = self._selected_path()
        if path and path.exists():
            os.startfile(path)

    def _merge(self) -> None:
        self.merge_requested = True
        self.root.destroy()

    def _drag_start(self, event) -> None:
        self.drag = self.tree.identify_row(event.y) or None
        self.dragged = False

    def _drag_motion(self, event) -> None:
        target = self.tree.identify_row(event.y)
        if self.drag and target and target != self.drag:
            self.tree.move(self.drag, "", self.tree.index(target))
            self.dragged = True

    def _drag_end(self, event) -> None:
        if self.drag and self.dragged:
            moved = self.paths[int(self.drag)]
            merge_queue.reorder([self.paths[int(iid)] for iid in self.tree.get_children()])
            self.drag = None
            self._reload(select=moved)
        self.drag = None
