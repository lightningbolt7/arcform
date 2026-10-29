"""Arcform's windows: the job list and the merge queue editor."""

import ctypes
import os
import queue
import re
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
import share
import split
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


def show_error(text: str) -> None:
    """A plain error box, for problems before any Arcform window is open."""
    root, _, _ = _make_root("Arcform")
    root.withdraw()
    messagebox.showerror("Arcform", text, parent=root)
    root.destroy()


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


# ---------------------------------------------------------------- PDF cutter

class SplitWindow:
    """Choose where to split a PDF: click between pages to cut, click a page to leave it out.

    Each resulting file gets its own colour, so you can see what goes where before saving.
    """

    COLORS = ["#4F7DF3", "#16A394", "#F2994A", "#9B51E0", "#E5484D", "#2FA84F", "#C99A06", "#2D9CDB"]
    CUT = "#E5484D"
    BACKGROUND = "#F3F3F6"
    BATCH = 6  # thumbnails drawn per step, so the window stays responsive

    def __init__(self, pdf: Path):
        self.pdf = Path(pdf).resolve()
        self.doc = split.open_pdf(self.pdf)
        self.count = self.doc.page_count
        self.chapters = split.chapters(self.doc)
        self.cuts = set()  # page indexes with a cut after them
        self.skipped = set()  # pages left out
        self.thumbs = [None] * self.count
        self.image_items = {}
        self.columns = 0
        self.result = None

        self.root, px, heading = _make_root("Arcform - Split PDF")
        self.px = px
        self.tile_w, self.tile_h, self.label_h = px(112), px(146), px(30)
        self.gap, self.margin, self.row_gap = px(34), px(16), px(14)
        root = self.root
        root.protocol("WM_DELETE_WINDOW", self._close)
        frame = ttk.Frame(root, padding=px(16))
        frame.pack(fill="both", expand=True)
        pages = f"{self.count} page{'s' if self.count != 1 else ''}"
        ttk.Label(frame, text=f"Split {self.pdf.name}", font=heading).pack(anchor="w")
        ttk.Label(frame, text=f"{pages}. Click the ✂ between two pages to cut there. Click a page to leave it out.",
                  foreground=MUTED).pack(anchor="w", pady=(px(2), 0))

        tools = ttk.Frame(frame)
        tools.pack(fill="x", pady=(px(10), px(8)))
        ttk.Label(tools, text="Quick cuts:").pack(side="left", padx=(0, px(6)))
        ttk.Button(tools, text="Every page", command=self._every_page).pack(side="left")
        ttk.Button(tools, text="Every", command=self._every_n).pack(side="left", padx=(px(6), 0))
        self.every = tk.IntVar(value=2)
        ttk.Spinbox(tools, from_=2, to=max(2, self.count), width=4,
                    textvariable=self.every).pack(side="left", padx=px(4))
        ttk.Label(tools, text="pages").pack(side="left")
        ttk.Button(tools, text="Chapters", command=self._chapters,
                   state="normal" if self.chapters else "disabled").pack(side="left", padx=(px(12), 0))
        ttk.Button(tools, text="Blank pages", command=self._blank_pages).pack(side="left", padx=(px(6), 0))
        ttk.Button(tools, text="Clear", command=self._clear).pack(side="right")

        area = ttk.Frame(frame)
        area.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(area, width=px(820), height=px(470), background=self.BACKGROUND,
                                highlightthickness=0, yscrollincrement=px(40))
        scroll = ttk.Scrollbar(area, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._layout())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._hover)
        root.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", pady=(px(12), 0))
        ttk.Button(bottom, text="Close", command=self._close).pack(side="right")
        self.split_button = ttk.Button(bottom, command=self._split, default="active")
        self.split_button.pack(side="right", padx=(0, px(6)))
        self.summary = ttk.Label(bottom, foreground=MUTED, wraplength=px(560), justify="left")
        self.summary.pack(side="left", fill="x", expand=True)

        self._summarize()
        root.after(50, self._render, 0)

    def show(self):
        """Show the window. Returns the chosen parts (lists of page indexes), or None."""
        _place(self.root)
        self.root.mainloop()
        return self.result

    # ---- the plan

    def parts(self):
        groups, current = [], []
        for page in range(self.count):
            if page not in self.skipped:
                current.append(page)
            if page in self.cuts and current:
                groups.append(current)
                current = []
        if current:
            groups.append(current)
        return groups

    def _summarize(self) -> None:
        groups = self.parts()
        if not groups:
            text, button, state = "Every page is left out.", "Split", "disabled"
        elif len(groups) == 1 and not self.skipped:
            text, button, state = "No cuts yet.", "Split", "disabled"
        elif len(groups) == 1:
            text = f"1 file: pages {split.page_ranges(groups[0])} ({len(self.skipped)} left out)"
            button, state = "Save without left-out pages", "normal"
        else:
            shown = " · ".join(split.page_ranges(g) for g in groups[:8]) + (" · …" if len(groups) > 8 else "")
            text = f"{len(groups)} files: pages {shown}"
            button, state = f"Split into {len(groups)} files", "normal"
        self.summary.config(text=text)
        self.split_button.config(text=button, state=state)

    # ---- drawing

    def _layout(self) -> None:
        width = self.canvas.winfo_width()
        columns = max(1, (width - 2 * self.margin + self.gap) // (self.tile_w + self.gap))
        if columns != self.columns:
            self.columns = columns
            self._draw()

    def _position(self, page):
        row, col = divmod(page, self.columns or 1)
        return (self.margin + col * (self.tile_w + self.gap),
                self.margin + row * (self.tile_h + self.label_h + self.row_gap))

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self.image_items.clear()
        part_of = {page: k for k, group in enumerate(self.parts()) for page in group}
        family = font.nametofont("TkDefaultFont").cget("family")
        for page in range(self.count):
            x, y = self._position(page)
            tag = f"p{page}"
            skipped = page in self.skipped
            color = "#B8B8C2" if skipped else self.COLORS[part_of[page] % len(self.COLORS)]
            c.create_rectangle(x - 4, y - 4, x + self.tile_w + 4, y + self.tile_h + 4,
                               fill="white", outline=color, width=self.px(3), tags=tag)
            cx, cy = x + self.tile_w / 2, y + self.tile_h / 2
            self.image_items[page] = c.create_image(cx, cy, image=self.thumbs[page] or "", tags=tag)
            if self.thumbs[page] is None:
                c.create_text(cx, cy, text="…", fill=MUTED, tags=(tag, f"wait{page}"))
            if skipped:
                c.create_rectangle(x, y, x + self.tile_w, y + self.tile_h, fill="white", stipple="gray50",
                                   outline="", tags=tag)
                c.create_text(cx, cy, text="✕", fill=ERROR, font=(family, 28, "bold"), tags=tag)
            label = f"Page {page + 1}  ·  " + ("left out" if skipped else f"file {part_of[page] + 1}")
            c.create_text(cx, y + self.tile_h + self.label_h / 2 + 2, text=label,
                          fill=MUTED if skipped else color, tags=tag)
            if page < self.count - 1:
                self._draw_gap(page, x + self.tile_w, y)
        rows = -(-self.count // max(self.columns, 1))
        height = 2 * self.margin + rows * (self.tile_h + self.label_h + self.row_gap)
        c.configure(scrollregion=(0, 0, c.winfo_width(), height))
        self._summarize()

    def _draw_gap(self, page, x, y) -> None:
        c, tag = self.canvas, f"g{page}"
        middle = x + self.gap / 2
        # A clickable strip between the two pages, the same colour as the background,
        # kept clear of both pages' coloured frames.
        frame = 4 + self.px(3)
        c.create_rectangle(x + frame, y - 4, x + self.gap - frame, y + self.tile_h + 4,
                           fill=self.BACKGROUND, outline="", tags=tag)
        if page in self.cuts:
            c.create_line(middle, y - 6, middle, y + self.tile_h + 6, fill=self.CUT, width=self.px(2),
                          dash=(6, 4), tags=tag)
            c.create_text(middle, y + self.tile_h / 2, text="✂", fill=self.CUT,
                          font=("Segoe UI Symbol", 18), tags=tag)
        else:
            c.create_text(middle, y + self.tile_h / 2, text="✂", fill="#C9C9D2",
                          font=("Segoe UI Symbol", 12), tags=tag)

    def _render(self, start) -> None:
        """Make page thumbnails a few at a time, so even long PDFs open instantly."""
        import pymupdf
        from PIL import Image, ImageTk

        for page in range(start, min(start + self.BATCH, self.count)):
            rect = self.doc[page].rect
            zoom = min(self.tile_w / rect.width, self.tile_h / rect.height)
            pix = self.doc[page].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            self.thumbs[page] = ImageTk.PhotoImage(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            if page in self.image_items:
                self.canvas.itemconfig(self.image_items[page], image=self.thumbs[page])
                self.canvas.delete(f"wait{page}")
        if start + self.BATCH < self.count:
            self.root.after(1, self._render, start + self.BATCH)

    # ---- clicks

    def _target(self):
        """("p", page) over a page, ("g", page) over the gap after a page, else (None, None)."""
        for tag in self.canvas.gettags("current"):
            match = re.fullmatch(r"([pg])(\d+)", tag)
            if match:
                return match.group(1), int(match.group(2))
        return None, None

    def _click(self, event) -> None:
        kind, page = self._target()
        if kind == "g":
            self.cuts ^= {page}
        elif kind == "p":
            self.skipped ^= {page}
        else:
            return
        self._draw()

    def _hover(self, event) -> None:
        kind, _ = self._target()
        self.canvas.configure(cursor="hand2" if kind else "")

    # ---- quick cuts

    def _every_page(self) -> None:
        self.cuts = set(range(self.count - 1))
        self._draw()

    def _every_n(self) -> None:
        try:
            n = max(1, int(self.every.get()))
        except (tk.TclError, ValueError):
            return
        self.cuts = {page for page in range(self.count - 1) if (page + 1) % n == 0}
        self._draw()

    def _chapters(self) -> None:
        self.cuts = {first - 1 for first, _ in self.chapters if first > 0}
        self._draw()

    def _blank_pages(self) -> None:
        self.root.configure(cursor="watch")
        self.root.update()
        blanks = split.blank_pages(self.doc)
        self.root.configure(cursor="")
        if not blanks:
            messagebox.showinfo("Arcform", "No blank pages found.", parent=self.root)
            return
        self.skipped |= set(blanks)
        for page in blanks:  # cut on both sides, so each document becomes its own file
            self.cuts |= {page - 1, page} - {-1, self.count - 1}
        self._draw()

    def _clear(self) -> None:
        self.cuts.clear()
        self.skipped.clear()
        self._draw()

    def _split(self) -> None:
        self.result = self.parts()
        self._close()

    def _close(self) -> None:
        self.doc.close()
        self.root.destroy()


# ------------------------------------------------------------- send to phone

def open_share_window(listener, first_task) -> None:
    window = ShareWindow(listener)
    window.events.put(("add", Path(first_task["path"])))
    window.run()


class ShareWindow:
    """A QR code for the phone to scan, and live news of what it downloaded."""

    def __init__(self, listener):
        self.listener = listener
        self.events = queue.Queue()  # from the web server and pipe threads, handled on the Tk thread
        self.stopped = threading.Event()
        self.share = share.Share(lambda kind, data: self.events.put((kind, data)))
        self.addresses = share.lan_addresses() or ["127.0.0.1"]
        self.rows = []  # tree item for each shared file, in order
        self.devices = []

        self.root, px, heading = _make_root("Arcform - Send to phone")
        self.px = px
        root = self.root
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(root, padding=px(18))
        frame.pack(fill="both", expand=True)

        left = ttk.Frame(frame)
        left.pack(side="left", fill="y", padx=(0, px(20)))
        self.code = tk.Label(left, background="white", borderwidth=0)
        self.code.pack()
        self.link = ttk.Entry(left, justify="center")
        self.link.pack(fill="x", pady=(px(10), 0))
        self.address = tk.StringVar(value=self.addresses[0])
        if len(self.addresses) > 1:
            pick = ttk.Frame(left)
            pick.pack(fill="x", pady=(px(6), 0))
            ttk.Label(pick, text="Network:", foreground=MUTED).pack(side="left")
            box = ttk.Combobox(pick, textvariable=self.address, values=self.addresses, state="readonly", width=16)
            box.pack(side="left", padx=(px(6), 0))
            box.bind("<<ComboboxSelected>>", lambda e: self._show_code())

        right = ttk.Frame(frame)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="Scan with your phone's camera", font=heading).pack(anchor="w")
        ttk.Label(right, text="Works on iPhone and Android, no app needed. Your phone has to be on the same "
                              "Wi-Fi or home network as this PC.",
                  foreground=MUTED, wraplength=px(400), justify="left").pack(anchor="w", pady=(px(4), px(12)))
        self.tree = ttk.Treeview(right, columns=("file", "size", "status"), show="headings", height=6,
                                 selectmode="none")
        for col, title, width in (("file", "File", 190), ("size", "Size", 70), ("status", "Status", 150)):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=px(width), anchor="w", stretch=col == "file")
        self.tree.tag_configure("sent", foreground="#1e7d32")
        self.tree.pack(fill="both", expand=True)
        self.status = ttk.Label(right, text="Waiting for your phone…", wraplength=px(400), justify="left")
        self.status.pack(anchor="w", pady=(px(10), 0))
        ttk.Label(right, text="Page won't open on the phone? When Windows asks, allow Python on private "
                              "networks. Also check the phone is on Wi-Fi, not mobile data.",
                  foreground=MUTED, wraplength=px(400), justify="left").pack(anchor="w", pady=(px(6), 0))
        buttons = ttk.Frame(right)
        buttons.pack(fill="x", pady=(px(14), 0))
        ttk.Button(buttons, text="Stop sharing", command=self.close).pack(side="right")

        self._show_code()
        threading.Thread(target=tasks.serve, daemon=True,
                         args=(listener, lambda task: self.events.put(("add", Path(task["path"]))),
                               self.stopped)).start()

    def run(self) -> None:
        _place(self.root)
        self.root.after(100, self._poll)
        self.root.mainloop()

    def url(self) -> str:
        return self.share.url(self.address.get())

    def _show_code(self) -> None:
        import qrcode
        from PIL import Image, ImageDraw, ImageTk

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, border=2)
        qr.add_data(self.url())
        qr.make(fit=True)
        side = self.px(250)
        qr.box_size = max(1, side // (qr.modules_count + 2 * qr.border))  # whole pixels per square scan best
        img = qr.make_image(fill_color="#14161f", back_color="white").get_image().convert("RGB")
        # The Arcform logo in the middle; strong error correction lets the code work around it.
        if menu.LOGO.exists():
            badge = img.width // 5
            logo = Image.open(menu.LOGO).convert("RGBA").resize((badge, badge), Image.Resampling.LANCZOS)
            spot = ((img.width - badge) // 2, (img.height - badge) // 2)
            pad = badge // 10
            ImageDraw.Draw(img).rounded_rectangle((spot[0] - pad, spot[1] - pad, spot[0] + badge + pad, spot[1] + badge + pad),
                                                  radius=pad * 2, fill="white")
            img.paste(logo, spot, logo)
        self.code_image = ImageTk.PhotoImage(img)
        self.code.config(image=self.code_image)
        self.link.config(state="normal")
        self.link.delete(0, "end")
        self.link.insert(0, self.url())
        self.link.config(state="readonly")

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "add" and self.share.add(data):
                    size = share.human_size(data.stat().st_size)
                    self.rows.append(self.tree.insert("", "end", values=(data.name, size, "Ready")))
                    self.tree.configure(height=min(max(len(self.rows), 3), 10))
                    self.root.lift()
                elif kind == "visit":
                    if data not in self.devices:
                        self.devices.append(data)
                    self.status.config(text=f"{' and '.join(self.devices)} opened the page. Tap a file to download it.")
                elif kind == "sent":
                    index, device = data
                    for row in self.rows if index == "all" else [self.rows[index]]:
                        self.tree.set(row, "status", f"✓ Sent to {device}")
                        self.tree.item(row, tags=("sent",))
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def close(self) -> None:
        self.stopped.set()
        try:
            self.listener.close()
        except OSError:
            pass
        self.share.stop()
        self.root.destroy()
