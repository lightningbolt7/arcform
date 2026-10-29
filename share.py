"""Send files to a phone over the home network.

A QR code opens a download page in the phone's browser, so it works the same on
iPhone and Android with no app to install. The PC serves the files only while the
Send to phone window is open, and every link carries a random code so other
devices on the network can't browse them.
"""

import getpass
import html
import json
import mimetypes
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import unicodedata
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from converters import IMAGE_EXTS, VIDEO_EXTS, _NO_WINDOW, convert_image, find_ffmpeg, flatten, open_image

APP_DIR = Path(__file__).resolve().parent
HEIC = {".heic", ".heif"}
for _type, _ext in (("image/heic", ".heic"), ("image/heif", ".heif"), ("image/webp", ".webp"),
                    ("video/x-matroska", ".mkv"), ("video/webm", ".webm")):
    mimetypes.add_type(_type, _ext)
ICONS = {".pdf": "📄", ".doc": "📝", ".docx": "📝", ".txt": "📝", ".zip": "📦", ".rar": "📦", ".7z": "📦",
         ".mp3": "🎵", ".wav": "🎵", ".m4a": "🎵", ".apk": "🤖", ".xlsx": "📊", ".pptx": "📽️"}


def device_name(user_agent: str) -> str:
    if "iPhone" in user_agent:
        return "iPhone"
    if "iPad" in user_agent:
        return "iPad"
    if "Macintosh" in user_agent:
        return "iPad or Mac"  # iPads ask for the desktop site, so they look like Macs
    if "Android" in user_agent:
        return "Android phone" if "Mobile" in user_agent else "Android tablet"
    return "computer"


def is_apple(user_agent: str) -> bool:
    # iPads ask for the desktop site, so they look like Macs; both open HEIC fine.
    return bool(re.search(r"iPhone|iPad|iPod|Macintosh", user_agent))


def lan_addresses():
    """This PC's IPv4 addresses, the one most likely to reach a phone first."""
    primary = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # sends nothing; just picks the network the PC uses
            primary = probe.getsockname()[0]
    except OSError:
        pass
    found = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    usable = [ip for ip in dict.fromkeys([primary, *sorted(found)])
              if ip and not ip.startswith(("127.", "169.254."))]
    # VPN addresses such as Tailscale's 100.x only work if the phone is on that VPN too.
    return sorted(usable, key=lambda ip: (ip != primary, ip.startswith("100."), ip))


def human_size(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000


class Share:
    """The files being shared, and the little web server that hands them out."""

    def __init__(self, on_event):
        self.files = []
        self.token = secrets.token_urlsafe(9)
        self.on_event = on_event  # called from server threads as on_event(kind, data)
        self.lock = threading.Lock()  # guards the file list
        self.work_lock = threading.Lock()  # guards the slower cache work (JPGs, previews, zip)
        self.cache = Path(tempfile.mkdtemp(prefix="arcform-share-"))
        self.computer = f"{getpass.getuser()}'s PC"  # friendlier than names like DESKTOP-1AGI469
        handler = type("Handler", (_Handler,), {"share": self})
        self.server = ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, address: str) -> str:
        return f"http://{address}:{self.port}/{self.token}/"

    def add(self, path: Path) -> bool:
        path = Path(path).resolve()
        with self.lock:
            if path in self.files or not path.is_file():
                return False
            self.files.append(path)
        return True

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.cache, ignore_errors=True)

    # ---- versions of files made on request, kept in the cache folder

    def for_device(self, index: int, user_agent: str) -> Path:
        """The file to send. iPhone photos become JPGs for phones that can't open HEIC."""
        path = self.files[index]
        if path.suffix.lower() not in HEIC or is_apple(user_agent):
            return path
        jpg = self.cache / f"jpg{index}" / f"{path.stem}.jpg"
        with self.work_lock:
            if not jpg.exists():
                jpg.parent.mkdir(exist_ok=True)
                convert_image(path, jpg)
        return jpg

    def thumbnail(self, index: int):
        path = self.files[index]
        thumb = self.cache / f"thumb{index}.jpg"
        ext = path.suffix.lower()
        with self.work_lock:
            if not thumb.exists():
                try:
                    if ext in IMAGE_EXTS:
                        img = open_image(path)
                        img.thumbnail((360, 360))
                        flatten(img).save(thumb, "JPEG", quality=80)
                    elif ext in VIDEO_EXTS:
                        subprocess.run([find_ffmpeg(), "-y", "-loglevel", "error", "-ss", "1", "-i", str(path),
                                        "-frames:v", "1", "-vf", "scale=360:-2", str(thumb)],
                                       timeout=20, creationflags=_NO_WINDOW)
                except Exception:
                    pass  # no preview, the page shows an icon instead
        return thumb if thumb.exists() else None

    def zip_all(self) -> Path:
        with self.lock:
            files = list(self.files)
        archive = self.cache / f"all-{len(files)}.zip"
        with self.work_lock:
            if not archive.exists():
                used = set()
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:  # photos and videos are already compressed
                    for path in files:
                        name, n = path.name, 2
                        while name.lower() in used:
                            name = f"{path.stem} ({n}){path.suffix}"
                            n += 1
                        used.add(name.lower())
                        zf.write(path, name)
        return archive


class _Handler(BaseHTTPRequestHandler):
    share: Share

    def log_message(self, *args):
        pass  # no console to print to

    def do_HEAD(self):
        self._route(head=True)

    def do_GET(self):
        self._route(head=False)

    def _route(self, head):
        try:
            parts = self.path.split("?")[0].strip("/").split("/")
            if not parts or not secrets.compare_digest(parts[0], self.share.token):
                return self.send_error(404)
            agent = self.headers.get("User-Agent", "")
            action, *rest = parts[1:] or [""]
            index = int(rest[0]) if rest and rest[0].isdigit() else None
            if index is not None and not 0 <= index < len(self.share.files):
                return self.send_error(404)

            if action == "" and not rest:
                if not head:
                    self.share.on_event("visit", device_name(agent))
                self._send_bytes(self._page(agent).encode(), "text/html; charset=utf-8", head)
            elif action == "files.json":
                self._send_bytes(json.dumps(len(self.share.files)).encode(), "application/json", head)
            elif action == "logo.png" and (APP_DIR / "arcform.png").exists():
                self._send_file(APP_DIR / "arcform.png", "logo.png", inline=True, head=head)
            elif action in ("download", "open") and index is not None:
                path = self.share.for_device(index, agent)
                self._send_file(path, path.name, inline=action == "open", head=head,
                                done=(index, device_name(agent)))
            elif action == "thumb" and index is not None:
                thumb = self.share.thumbnail(index)
                if not thumb:
                    return self.send_error(404)
                self._send_file(thumb, thumb.name, inline=True, head=head)
            elif action == "all.zip":
                self._send_file(self.share.zip_all(), "Arcform files.zip", inline=False, head=head,
                                done=("all", device_name(agent)))
            else:
                self.send_error(404)
        except (ConnectionError, TimeoutError):
            pass  # the phone cancelled or lost Wi-Fi

    def _send_bytes(self, data, content_type, head):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _send_file(self, path, name, inline, head, done=None):
        """Send a file, supporting partial requests (iPhones use them to play videos)."""
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if match and size and any(match.groups()):
            first, last = match.groups()
            if first:
                start, end = int(first), min(int(last), size - 1) if last else size - 1
            else:
                start = max(0, size - int(last))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        # Phones use the UTF-8 name; the plain one ("café" -> "cafe") is a fallback for old browsers.
        plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().replace('"', "'")
        self.send_header("Content-Disposition",
                         f'{"inline" if inline else "attachment"}; filename="{plain}"; filename*=UTF-8\'\'{quote(name)}')
        self.end_headers()
        if head:
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        if done and end == size - 1:
            self.share.on_event("sent", done)

    def _page(self, agent):
        share = self.share
        with share.lock:
            files = list(share.files)
        apple = is_apple(agent)
        android = "Android" in agent
        cards = []
        for i, path in enumerate(files):
            ext = path.suffix.lower()
            media = ext in IMAGE_EXTS or ext in VIDEO_EXTS
            as_jpg = ext in HEIC and not apple
            name = f"{path.stem}.jpg" if as_jpg else path.name
            preview = (f'<img class="thumb" src="thumb/{i}" alt="" loading="lazy">' if media
                       else f'<div class="thumb icon">{ICONS.get(ext, "📁")}</div>')
            note = '<div class="note">Sent as JPG so your phone can open it</div>' if as_jpg else ""
            open_button = f'<a class="button light" href="open/{i}">Open</a>' if media else ""
            cards.append(f"""
      <div class="card">{preview}
        <div class="info"><div class="name">{html.escape(name)}</div>
          <div class="size">{human_size(path.stat().st_size)}</div>{note}
          <div class="buttons"><a class="button" href="download/{i}">Download</a>{open_button}</div></div>
      </div>""")
        if apple:
            tip = ("<b>Photos and videos:</b> tap <b>Open</b>, then press and hold a photo and choose "
                   "<b>Save to Photos</b>. For a video, tap the Share button, then <b>Save Video</b>.<br>"
                   "<b>Other files:</b> tap <b>Download</b>. They go to the Files app, in Downloads.")
        elif android:
            tip = ("Tap <b>Download</b>. Files go to your Downloads folder, and photos and videos "
                   "also show up in your gallery.")
        else:
            tip = "Click <b>Download</b> to save a file."
        everything = (f'<a class="button all" href="all.zip">Download all {len(files)} files (.zip)</a>'
                      if len(files) > 1 else "")
        count = f"{len(files)} file{'s' if len(files) != 1 else ''}"
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arcform - {count} from {html.escape(share.computer)}</title>
<style>
  :root {{ --bg:#f4f5f8; --card:#fff; --text:#1c1d22; --muted:#6b6e78; --accent:#2563eb; --soft:#e8eefc; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#111216; --card:#1c1d23; --text:#f2f3f5; --muted:#9a9ca6; --soft:#1f2a44; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:16px/1.45 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif; background:var(--bg); color:var(--text); }}
  main {{ max-width:560px; margin:0 auto; padding:20px 16px 40px; }}
  header {{ display:flex; align-items:center; gap:10px; font-weight:700; font-size:18px; }}
  header img {{ width:34px; height:34px; border-radius:8px; }}
  h1 {{ font-size:22px; margin:18px 0 6px; }}
  .tip {{ background:var(--soft); border-radius:12px; padding:12px 14px; font-size:14px; margin:12px 0 18px; }}
  .card {{ display:flex; gap:14px; background:var(--card); border-radius:14px; padding:12px; margin-bottom:12px;
          box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  .thumb {{ width:84px; height:84px; border-radius:10px; object-fit:cover; flex:none; background:var(--bg); }}
  .icon {{ display:flex; align-items:center; justify-content:center; font-size:38px; }}
  .info {{ min-width:0; flex:1; }}
  .name {{ font-weight:600; overflow-wrap:anywhere; }}
  .size, .note {{ color:var(--muted); font-size:13px; }}
  .buttons {{ display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; }}
  .button {{ display:inline-block; background:var(--accent); color:#fff; text-decoration:none; font-weight:600;
            padding:9px 16px; border-radius:10px; font-size:15px; }}
  .light {{ background:var(--soft); color:var(--accent); }}
  .all {{ display:block; text-align:center; padding:14px; margin-top:6px; }}
  footer {{ color:var(--muted); font-size:13px; text-align:center; margin-top:22px; }}
</style></head>
<body><main>
  <header><img src="logo.png" alt="">Arcform</header>
  <h1>{count} from {html.escape(share.computer)}</h1>
  <div class="tip">{tip}</div>
  {"".join(cards)}
  {everything}
  <footer>This page works while the Send to phone window is open on the PC.</footer>
</main>
<script>
  // Files added on the PC (say, a second selection) appear without a manual refresh.
  setInterval(() => fetch("files.json").then(r => r.json())
    .then(n => {{ if (n !== {len(files)}) location.reload(); }}).catch(() => {{}}), 3000);
</script>
</body></html>"""
