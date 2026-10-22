"""Put files on the Windows clipboard, the same way Explorer's Copy does."""

import io
import struct
import time
from pathlib import Path

import pywintypes
import win32clipboard
import win32con

from converters import ConversionError

PASTEABLE_IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
DROPEFFECT_COPY = 1


def _file_list(paths) -> bytes:
    # DROPFILES header: offset to the file list, drop point x/y, non-client flag, wide-char flag.
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    files = "\0".join(str(p) for p in paths) + "\0\0"
    return header + files.encode("utf-16-le")


def _bitmap(image: Path) -> bytes:
    from PIL import Image

    with Image.open(image) as img:
        buffer = io.BytesIO()
        img.convert("RGB").save(buffer, "BMP")
    return buffer.getvalue()[14:]  # a clipboard DIB is a BMP file without its file header


def copy_files(paths) -> None:
    """Copy files so they paste into Explorer, chat apps, email and upload boxes.

    A single image is also copied as a picture, for apps that only accept images.
    """
    paths = [Path(p) for p in paths]
    data = {
        win32con.CF_HDROP: _file_list(paths),
        win32clipboard.RegisterClipboardFormat("Preferred DropEffect"): struct.pack("<I", DROPEFFECT_COPY),
    }
    if len(paths) == 1 and paths[0].suffix.lower() in PASTEABLE_IMAGES:
        data[win32con.CF_DIB] = _bitmap(paths[0])

    for _ in range(20):
        try:
            win32clipboard.OpenClipboard()
            break
        except pywintypes.error:
            time.sleep(0.1)  # another app has the clipboard open for a moment
    else:
        raise ConversionError("Another app is using the clipboard. Try again.")
    try:
        win32clipboard.EmptyClipboard()
        for fmt, value in data.items():
            win32clipboard.SetClipboardData(fmt, value)
    finally:
        win32clipboard.CloseClipboard()
