"""Shrink images and videos to fit under a size limit (web forms, Discord, email)."""

import io
import os
import tempfile
from pathlib import Path

from converters import (IMAGE_EXTS, VIDEO_EXTS, ConversionError, flatten, has_alpha,
                        media_duration, open_image, output_path, partial_output, run_ffmpeg)

# size limit in MB -> label in the right-click menu
SIZES = {
    1: "Under 1 MB (web forms)",
    10: "Under 10 MB (Discord)",
    25: "Under 25 MB (email)",
}
SHRINKABLE = IMAGE_EXTS | VIDEO_EXTS

# Decimal megabytes, so the result fits whichever way a site counts.
MB = 1_000_000

MIN_QUALITY, MAX_QUALITY = 40, 90

# Below these video bitrates (kbps), cap the height so the picture stays clear.
MAX_HEIGHT_FOR_BITRATE = [(2500, 2160), (1200, 1080), (600, 720), (300, 480), (0, 360)]


def shrink(src: Path, size_mb: int, progress=None):
    """Make a copy of `src` under `size_mb`. Returns (new file or None, note)."""
    src = Path(src).resolve()
    limit = size_mb * MB
    original = src.stat().st_size
    if original <= limit:
        return None, f"Already under {size_mb} MB, nothing to do."

    ext = src.suffix.lower()
    if ext in IMAGE_EXTS:
        dst = _shrink_image(src, size_mb, limit, progress)
    elif ext in VIDEO_EXTS:
        dst = _shrink_video(src, size_mb, limit, progress)
    else:
        raise ConversionError(f"Arcform can't shrink {ext} files.")
    return dst, f"Now {dst.stat().st_size / MB:.1f} MB (was {original / MB:.1f} MB)"


# ------------------------------------------------------------------- images

def _encode(img, fmt: str, quality: int) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, fmt, quality=quality)
    return buffer.getvalue()


def _best_quality(img, fmt: str, limit: int):
    """The highest-quality encoding that fits under the limit, or None."""
    low, high, best = MIN_QUALITY, MAX_QUALITY, None
    while low <= high:
        quality = (low + high) // 2
        data = _encode(img, fmt, quality)
        if len(data) <= limit:
            best, low = data, quality + 1
        else:
            high = quality - 1
    return best


def _shrink_image(src: Path, size_mb: int, limit: int, progress) -> Path:
    from PIL import Image

    img = open_image(src)
    # JPEG for photos; WEBP keeps transparency where JPEG can't.
    if has_alpha(img):
        fmt, ext, img = "WEBP", "webp", img.convert("RGBA")
    else:
        fmt, ext, img = "JPEG", "jpg", flatten(img)

    attempts = 8
    for attempt in range(attempts):
        if progress:
            progress(attempt / attempts)
        data = _best_quality(img, fmt, limit)
        if data:
            dst = output_path(src, ext, tag=f" (under {size_mb} MB)")
            dst.write_bytes(data)
            return dst
        # Even the lowest quality is too big, so make the picture smaller too.
        smallest = len(_encode(img, fmt, MIN_QUALITY))
        factor = max(0.5, (limit / smallest) ** 0.5 * 0.95)
        size = (max(1, round(img.width * factor)), max(1, round(img.height * factor)))
        img = img.resize(size, Image.Resampling.LANCZOS)
    raise ConversionError(f"Couldn't get {src.name} under {size_mb} MB.")


# -------------------------------------------------------------------- video

def _shrink_video(src: Path, size_mb: int, limit: int, progress) -> Path:
    duration = media_duration(src)
    if not duration:
        raise ConversionError(f"Couldn't read the length of {src.name}.")

    dst = output_path(src, "mp4", tag=f" (under {size_mb} MB)")
    # Total bitrate the file can afford, minus a little for the MP4 container.
    budget_kbps = limit * 8 / 1000 / duration * 0.96
    # Every attempt stays under a temporary name until one fits.
    with partial_output(dst) as part:
        for _ in range(3):
            audio_kbps = 128 if budget_kbps > 1000 else 96 if budget_kbps > 400 else 64
            video_kbps = budget_kbps - audio_kbps
            if video_kbps < 60:
                raise ConversionError(
                    f"{src.name} is too long to fit under {size_mb} MB and still be watchable."
                )
            max_height = next(h for kbps, h in MAX_HEIGHT_FOR_BITRATE if video_kbps >= kbps)
            _two_pass(src, part, video_kbps, audio_kbps, max_height, duration, progress)

            size = part.stat().st_size
            if size <= limit:
                break
            budget_kbps *= limit / size * 0.95  # overshot: aim lower and try again
        else:
            raise ConversionError(f"Couldn't get {src.name} under {size_mb} MB.")
    return dst


def _two_pass(src, out, video_kbps, audio_kbps, max_height, duration, progress) -> None:
    """Encode at an exact average bitrate: pass 1 studies the video, pass 2 encodes it."""
    video = ["-i", str(src),
             "-vf", f"scale=-2:trunc(min(ih\\,{max_height})/2)*2,setsar=1",
             "-c:v", "libx264", "-preset", "medium", "-b:v", f"{int(video_kbps)}k",
             "-pix_fmt", "yuv420p"]
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "pass")
        run_ffmpeg([*video, "-pass", "1", "-passlogfile", log, "-an", "-f", "null", os.devnull],
                   duration, progress, (0.0, 0.5))
        run_ffmpeg([*video, "-pass", "2", "-passlogfile", log,
                    "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-movflags", "+faststart", str(out)],
                   duration, progress, (0.5, 1.0))
