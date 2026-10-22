"""Conversion functions used by Arcform.

Each converter takes a source path, a destination path and an optional
progress callback (called with a fraction from 0 to 1), and writes the
converted file to the destination.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path

# Hide console windows for any child processes (ffmpeg, LibreOffice).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}

# ffmpeg processes in progress, so they can be stopped when the window closes.
RUNNING = set()


class ConversionError(Exception):
    pass


@contextmanager
def partial_output(dst: Path):
    """Write to a temporary name first, so a cancelled job leaves no half-written file."""
    part = dst.with_name(f"{dst.stem}.part{dst.suffix}")  # keep the extension for ffmpeg
    try:
        yield part
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)


# ---------------------------------------------------------------- documents

def word_to_pdf(src: Path, dst: Path, progress=None) -> None:
    """Convert .docx/.doc to PDF using Microsoft Word, or LibreOffice as a fallback."""
    try:
        _word_to_pdf_msword(src, dst)
        return
    except ImportError:
        pass  # pywin32 not installed, try LibreOffice
    except Exception as word_error:
        if not _find_libreoffice():
            raise ConversionError(f"Microsoft Word could not convert the file:\n{word_error}")
    _word_to_pdf_libreoffice(src, dst)


# Word takes seconds to start, so each thread keeps its own copy open between
# documents; close_word() quits it once the thread runs out of work.
_word = threading.local()


def _word_app():
    import pythoncom
    import win32com.client

    app = getattr(_word, "app", None)
    if app is not None:
        try:
            app.Visible  # fails if Word was closed or crashed since the last document
            return app
        except Exception:
            _word.app = None
    if not getattr(_word, "com", False):
        pythoncom.CoInitialize()
        _word.com = True
    app = win32com.client.DispatchEx("Word.Application")
    app.Visible = False
    app.DisplayAlerts = 0
    _word.app = app
    return app


def close_word() -> None:
    """Quit the copy of Word this thread started, if any."""
    app = getattr(_word, "app", None)
    _word.app = None
    if app is not None:
        try:
            app.Quit()
        except Exception:
            pass  # already gone
    if getattr(_word, "com", False):
        import pythoncom
        pythoncom.CoUninitialize()
        _word.com = False


def _word_to_pdf_msword(src: Path, dst: Path) -> None:
    WD_FORMAT_PDF = 17
    doc = _word_app().Documents.Open(str(src), ReadOnly=True, AddToRecentFiles=False)
    try:
        doc.SaveAs2(str(dst), FileFormat=WD_FORMAT_PDF)
    finally:
        doc.Close(SaveChanges=False)


def _find_libreoffice():
    found = shutil.which("soffice")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidate = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if candidate.exists():
                return str(candidate)
    return None


def _word_to_pdf_libreoffice(src: Path, dst: Path) -> None:
    soffice = _find_libreoffice()
    if not soffice:
        raise ConversionError(
            "Word to PDF needs Microsoft Word (with pywin32) or LibreOffice installed."
        )
    out_dir = dst.parent / ".arcform_tmp"
    out_dir.mkdir(exist_ok=True)
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(src)],
            check=True, capture_output=True, creationflags=_NO_WINDOW,
        )
        shutil.move(str(out_dir / (src.stem + ".pdf")), str(dst))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def pdf_to_word(src: Path, dst: Path, progress=None) -> None:
    from pdf2docx import Converter

    cv = Converter(str(src))
    try:
        cv.convert(str(dst))
    finally:
        cv.close()


def merge_pdfs(sources, dst: Path) -> None:
    import pymupdf

    merged = pymupdf.open()
    try:
        for src in sources:
            with pymupdf.open(src) as doc:
                if doc.needs_pass:
                    raise ConversionError(f"{Path(src).name} is password-protected.")
                merged.insert_pdf(doc)
        merged.save(dst)
    finally:
        merged.close()


# ------------------------------------------------------------------- images

def open_image(src: Path):
    """Open an image the right way up, including iPhone HEIC photos."""
    from PIL import Image, ImageOps

    if Path(src).suffix.lower() in (".heic", ".heif"):
        from pillow_heif import register_heif_opener
        register_heif_opener()
    with Image.open(src) as img:
        # Phones save photos sideways plus a "rotate me" tag; apply it so
        # formats and apps that ignore the tag still show it upright.
        return ImageOps.exif_transpose(img)


def has_alpha(img) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)


def flatten(img):
    """Remove transparency by placing the image on a white background."""
    from PIL import Image

    if not has_alpha(img):
        return img.convert("RGB")
    rgba = img.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def convert_image(src: Path, dst: Path, progress=None) -> None:
    img = open_image(src)
    exif = {"exif": img.info["exif"]} if img.info.get("exif") else {}
    fmt = dst.suffix.lower()
    if fmt in (".jpg", ".jpeg"):
        # JPEG has no transparency, so flatten onto a white background.
        flatten(img).save(dst, "JPEG", quality=95, **exif)
    elif fmt == ".webp":
        img.convert("RGBA" if has_alpha(img) else "RGB").save(dst, "WEBP", quality=90, **exif)
    else:
        if img.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"):
            img = img.convert("RGBA" if has_alpha(img) else "RGB")
        img.save(dst, "PNG", **exif)


# -------------------------------------------------------------- audio/video

def find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise ConversionError("Video conversions need ffmpeg. Run: pip install imageio-ffmpeg")


MediaInfo = namedtuple("MediaInfo", "duration video video_details audio")


def media_info(src: Path) -> MediaInfo:
    """Length in seconds plus the first video and audio codec; None where ffmpeg can't tell."""
    result = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", str(src)],
                            capture_output=True, text=True, errors="replace", creationflags=_NO_WINDOW)
    text = result.stderr
    duration = None
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds) or None
    # Skip album-art "videos" inside audio files.
    video = next((m for m in re.finditer(r"Video: (\w+)(.*)", text) if "attached pic" not in m.group(2)), None)
    audio = re.search(r"Audio: (\w+)", text)
    return MediaInfo(duration, video and video.group(1), video and video.group(2),
                     audio and audio.group(1))


def media_duration(src: Path):
    return media_info(src).duration


def run_ffmpeg(args, duration=None, progress=None, span=(0.0, 1.0)) -> None:
    """Run ffmpeg, reporting its progress as the `span` part of the whole job."""
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(
            [find_ffmpeg(), "-hide_banner", "-nostats", "-progress", "pipe:1", "-y", *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
            text=True, creationflags=_NO_WINDOW,
        )
        RUNNING.add(proc)
        try:
            for line in proc.stdout:
                if progress and duration and line.startswith("out_time_us="):
                    try:
                        done = int(line.split("=", 1)[1]) / 1_000_000 / duration
                    except ValueError:
                        continue  # "N/A" before the first frame
                    progress(span[0] + (span[1] - span[0]) * min(max(done, 0.0), 1.0))
            proc.wait()
        finally:
            RUNNING.discard(proc)
        if proc.returncode != 0:
            log.seek(0)
            last_lines = log.read().decode(errors="replace").strip().splitlines()[-5:]
            raise ConversionError("ffmpeg failed:\n" + "\n".join(last_lines))


def video_to_mp3(src: Path, dst: Path, progress=None) -> None:
    with partial_output(dst) as part:
        run_ffmpeg(["-i", str(src), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(part)],
                   media_duration(src), progress)


def video_to_mp4(src: Path, dst: Path, progress=None) -> None:
    info = media_info(src)
    # Most cameras and screen recorders already use H.264 (8-bit 4:2:0), which MP4 can
    # hold as-is: copying it takes seconds and loses nothing. Anything else is re-encoded.
    if info.video == "h264" and re.search(r"\byuvj?420p\b", info.video_details or ""):
        video = ["-c:v", "copy"]
    else:
        video = ["-c:v", "libx264", "-crf", "23", "-preset", "medium",
                 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p"]
    audio = ["-c:a", "copy"] if info.audio in ("aac", "mp3", None) else ["-c:a", "aac", "-b:a", "160k"]
    with partial_output(dst) as part:
        # Only the main video and audio track; subtitles and data tracks often can't go in MP4.
        run_ffmpeg(["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", *video, *audio,
                    "-movflags", "+faststart", str(part)],
                   info.duration, progress)


# ------------------------------------------------------------------ registry

# source extension -> {target format: converter}
_PHOTO = {"jpg": convert_image, "png": convert_image}
_VIDEO = {"mp4": video_to_mp4, "mp3": video_to_mp3}
CONVERSIONS = {
    ".docx": {"pdf": word_to_pdf},
    ".doc":  {"pdf": word_to_pdf},
    ".pdf":  {"docx": pdf_to_word},
    ".png":  {"jpg": convert_image, "jpeg": convert_image, "webp": convert_image},
    ".jpg":  {"png": convert_image, "jpeg": convert_image, "webp": convert_image},
    ".jpeg": {"png": convert_image, "jpg": convert_image, "webp": convert_image},
    ".webp": {"png": convert_image, "jpg": convert_image},
    ".heic": _PHOTO,
    ".heif": _PHOTO,
    ".mp4":  {"mp3": video_to_mp3},
    ".mov":  _VIDEO,
    ".mkv":  _VIDEO,
    ".webm": _VIDEO,
    ".avi":  _VIDEO,
}

# Names shown in the right-click menu and the Arcform window.
FORMAT_NAMES = {
    "pdf": "PDF",
    "docx": "Word (.docx)",
    "png": "PNG",
    "jpg": "JPG",
    "jpeg": "JPEG",
    "webp": "WEBP",
    "mp3": "MP3 (audio)",
    "mp4": "MP4",
}


def copy_targets(ext: str):
    """Formats offered under "Copy as" (JPG and JPEG are the same format, so skip that pair)."""
    jpeg = {".jpg", ".jpeg"}
    return [t for t in CONVERSIONS.get(ext, {}) if not (ext in jpeg and f".{t}" in jpeg)]


def output_path(src: Path, target: str, tag: str = "", folder: Path = None) -> Path:
    """Pick a destination that doesn't overwrite anything, next to the source by default."""
    folder = folder or src.parent
    stem = src.stem + tag
    dst = folder / f"{stem}.{target}"
    n = 1
    while dst.exists():
        dst = folder / f"{stem} ({n}).{target}"
        n += 1
    return dst


def convert(src: Path, target: str, folder: Path = None, progress=None) -> Path:
    src = Path(src).resolve()
    target = target.lower().lstrip(".")
    options = CONVERSIONS.get(src.suffix.lower())
    if not options or target not in options:
        raise ConversionError(f"Arcform can't convert {src.suffix or 'this file'} to {target}.")
    dst = output_path(src, target, folder=folder)
    options[target](src, dst, progress)
    if not dst.exists():
        raise ConversionError("The conversion finished but no output file was created.")
    return dst
