"""Split PDFs: every page, by chapters, at blank pages, by size, or at hand-picked cuts.

The parts go into a new folder next to the PDF, e.g. "report (split)".
"""

import re
import shutil
from pathlib import Path

from converters import ConversionError

EMAIL_LIMIT_MB = 25
MB = 1_000_000

# mode -> label in the right-click menu
MODES = {
    "pages": "Every page",
    "chapters": "By chapters (bookmarks)",
    "blank": "At blank pages (scanned stacks)",
    "size": f"Into email-sized parts (under {EMAIL_LIMIT_MB} MB)",
}
# mode -> action shown in the Arcform window
ACTIONS = {
    "pages": "Split into single pages",
    "chapters": "Split by chapters",
    "blank": "Split at blank pages",
    "size": f"Split into parts under {EMAIL_LIMIT_MB} MB",
}
NOTHING_TO_DO = {
    "blank": "No blank pages found, so there's nothing to split.",
    "chapters": "This PDF has only one chapter, so there's nothing to split.",
}


def open_pdf(src):
    import pymupdf

    doc = pymupdf.open(src)
    if doc.needs_pass:
        doc.close()
        raise ConversionError(f"{Path(src).name} is password-protected.")
    return doc


def _runs(pages):
    """Consecutive stretches of page indexes, as (first, last)."""
    runs = []
    for page in pages:
        if runs and page == runs[-1][1] + 1:
            runs[-1][1] = page
        else:
            runs.append([page, page])
    return runs


def page_ranges(pages) -> str:
    """[0, 1, 2, 4] -> "1-3, 5", in page numbers as people count them."""
    return ", ".join(f"{a + 1}" if a == b else f"{a + 1}-{b + 1}" for a, b in _runs(pages))


def safe_name(title: str) -> str:
    """A bookmark title made safe to use as a file name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name[:80] or "Untitled"


def chapters(doc):
    """(first page index, title) for each top-level bookmark, in page order."""
    starts = {}
    for level, title, page in doc.get_toc():
        if level == 1 and 1 <= page <= doc.page_count:
            starts.setdefault(page - 1, title.strip() or f"Chapter at page {page}")
    return sorted(starts.items())


def is_blank(page) -> bool:
    import pymupdf
    from PIL import Image

    if page.get_text().strip():
        return False
    pix = page.get_pixmap(matrix=pymupdf.Matrix(0.3, 0.3), colorspace=pymupdf.csGRAY)
    histogram = Image.frombytes("L", (pix.width, pix.height), pix.samples).histogram()
    # Scans of blank paper still have specks of dust, so allow a tiny amount of ink.
    return sum(histogram[:200]) < 0.003 * pix.width * pix.height


def blank_pages(doc, progress=None):
    blanks = []
    for i, page in enumerate(doc):
        if is_blank(page):
            blanks.append(i)
        if progress:
            progress((i + 1) / doc.page_count)
    return blanks


def name_parts(groups, stem):
    """Give plain parts names like "report - part 2 (pages 4-7)"."""
    named = []
    for k, pages in enumerate(groups, 1):
        which = "page" if len(pages) == 1 else "pages"
        named.append((pages, f"{stem} - part {k} ({which} {page_ranges(pages)})"))
    return named


def _pdf_size(doc, first, last) -> int:
    import pymupdf

    with pymupdf.open() as part:
        part.insert_pdf(doc, from_page=first, to_page=last)
        return len(part.tobytes(garbage=3, deflate=True))


def _by_size(doc, limit, progress=None):
    """Fewest parts under `limit`: for each part, binary-search the last page that still fits."""
    groups, start, count = [], 0, doc.page_count
    while start < count:
        low, high, best = start, count - 1, start  # a single page always gets a part
        while low <= high:
            middle = (low + high) // 2
            if _pdf_size(doc, start, middle) <= limit:
                best, low = middle, middle + 1
            else:
                high = middle - 1
        groups.append(list(range(start, best + 1)))
        start = best + 1
        if progress:
            progress(start / count)
    return groups


def plan(doc, mode, stem, progress=None):
    """The parts to write, as (page indexes, file name without .pdf)."""
    count = doc.page_count
    if mode == "pages":
        width = len(str(count))
        return [([i], f"{stem} - page {i + 1:0{width}d}") for i in range(count)]

    if mode == "chapters":
        starts = chapters(doc)
        if not starts:
            raise ConversionError("This PDF has no chapters (bookmarks) to split by.\n"
                                  "Try Split PDF → Choose where to cut… instead.")
        parts = []
        if starts[0][0] > 0:
            parts.append((list(range(starts[0][0])), "00 - Front matter"))
        for k, (first, title) in enumerate(starts):
            end = starts[k + 1][0] if k + 1 < len(starts) else count
            parts.append((list(range(first, end)), f"{k + 1:02d} - {safe_name(title)}"))
        return parts

    if mode == "blank":
        blanks = set(blank_pages(doc, progress))
        groups, current = [], []
        for i in range(count):
            if i in blanks:
                if current:
                    groups.append(current)
                current = []
            else:
                current.append(i)
        if current:
            groups.append(current)
        return name_parts(groups, stem)

    if mode == "size":
        return name_parts(_by_size(doc, EMAIL_LIMIT_MB * MB, progress), stem)
    raise ValueError(f"Unknown split mode: {mode}")


def _new_folder(src: Path) -> Path:
    folder = src.with_name(f"{src.stem} (split)")
    n = 2
    while folder.exists():
        folder = src.with_name(f"{src.stem} (split {n})")
        n += 1
    return folder


def write_parts(doc, src: Path, parts, progress=None) -> Path:
    import pymupdf

    folder = _new_folder(src)
    # Fill a temporary folder and rename it at the end, so a failed or
    # cancelled split never leaves a half-finished folder behind.
    tmp = folder.with_name(folder.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        for k, (pages, name) in enumerate(parts, 1):
            with pymupdf.open() as part:
                for first, last in _runs(pages):
                    part.insert_pdf(doc, from_page=first, to_page=last)
                part.save(tmp / f"{name}.pdf", garbage=3, deflate=True)
            if progress:
                progress(k / len(parts))
        tmp.rename(folder)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return folder


def split(src: Path, mode: str, parts=None, progress=None):
    """Split `src` into a new folder next to it. Returns (folder or None, note).

    `parts` (lists of page indexes) is used for mode "custom", from the cutting window.
    """
    src = Path(src).resolve()
    if mode == "size" and src.stat().st_size <= EMAIL_LIMIT_MB * MB:
        return None, f"Already under {EMAIL_LIMIT_MB} MB, so there's nothing to split."

    with open_pdf(src) as doc:
        # Finding blank pages or part sizes is the first half of the work; writing is the second.
        slow_plan = mode in ("blank", "size")
        planning = (lambda f: progress(f / 2)) if progress and slow_plan else None
        writing = (lambda f: progress(0.5 + f / 2)) if progress and slow_plan else progress

        if mode == "custom":
            planned = name_parts([list(p) for p in parts if p], src.stem)
        else:
            planned = plan(doc, mode, src.stem, planning)
        if not planned:
            raise ConversionError("Every page was left out, so there's nothing to save.")
        if len(planned) == 1 and planned[0][0] == list(range(doc.page_count)):
            return None, NOTHING_TO_DO.get(mode, "Nothing to split: the result would be the same as the original.")
        folder = write_parts(doc, src, planned, writing)

    files = len(planned)
    return folder, f'{files} file{"s" if files > 1 else ""} in "{folder.name}"'
