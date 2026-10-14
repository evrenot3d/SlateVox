"""Yerel HTTP katmanı: arayüz dosyaları, video akışı, parça sesleri ve iş kontrolü."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import engine as E
from jobs import Manager
from youtube import YouTube
from guncelle import Updater

UI = E.BASE / "ui"
for ext, typ in {".js": "text/javascript", ".css": "text/css", ".mkv": "video/x-matroska", ".webm": "video/webm",
                 ".mp4": "video/mp4", ".mov": "video/quicktime", ".wav": "audio/wav"}.items():
    mimetypes.add_type(typ, ext)

MANAGER: Manager | None = None


def manager() -> Manager:
    global MANAGER
    if MANAGER is None:
        MANAGER = Manager()
    return MANAGER


YT: YouTube | None = None


def youtube() -> YouTube:
    global YT
    if YT is None:
        YT = YouTube(manager())
    return YT


UPD = Updater()
LINKS = re.compile(r"^(https://(www\.)?(instagram\.com|github\.com|huggingface\.co)/[\w./?=&%-]*|mailto:[\w.+-]+@[\w.-]+)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DogruMuSamet"

    def log_message(self, *a):
        pass

    # ---- yanıt yardımcıları
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _file(self, f: Path, cache="no-cache"):
        if not f.is_file():
            return self._json({"error": "bulunamadı"}, 404)
        size = f.stat().st_size
        start, end, partial = 0, size - 1, False
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    b = fh.read(min(1 << 20, left))
                    if not b:
                        break
                    self.wfile.write(b)
                    left -= len(b)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # ---- GET
    def do_GET(self):
        u = urlparse(self.path)
        p, q = unquote(u.path), parse_qs(u.query)
        try:
            if p.startswith("/api/status/"):
                return self._json(manager().status(p.rsplit("/", 1)[1], q.get("variant", ["hizli-kadin"])[0]))
            if p.startswith("/api/speakers/"):
                return self._json(manager().speakers(p.rsplit("/", 1)[1], q.get("variant", ["detayli"])[0]))
            if p.startswith("/api/chunk/"):
                _, _, _, vid, k = p.split("/")
                return self._json(manager().chunk_info(vid, q.get("variant", ["hizli-kadin"])[0], int(k)))
            if p == "/api/open_url_status":
                return self._json({"ok": True, **youtube().state})
            if p.startswith("/api/yt_stream/"):
                v = manager().get(p.rsplit("/", 1)[1])
                if not getattr(v, "youtube", None):
                    return self._json({"ok": False, "error": "YouTube videosu değil"}, 400)
                return self._json({"ok": True, "url": youtube().stream_url(v.youtube)})
            if p == "/api/update/check":
                return self._json(UPD.check(force="force" in parse_qs(urlparse(self.path).query)))
            if p == "/api/update/status":
                return self._json({"ok": True, **UPD.state})
            if p == "/api/stats":
                return self._json(manager().stats.summary())
            if p == "/api/export_status":
                return self._json({"ok": True, **manager().export_state})
            if p == "/api/info":
                return self._json({"gpu": E.gpu_name(), "demo": E.DEMO,
                                   "vox": manager().engine.vox_installed(),
                                   "version": E.VERSION})
            if p.startswith("/video/"):
                v = manager().get(p.split("/")[2])
                return self._file(v.path)
            if p.startswith("/media/"):
                parts = p.split("/")            # /media/<vid>/<cXXX>/<file>
                if len(parts) == 5 and re.fullmatch(r"[0-9a-f]{16}", parts[2]) and re.fullmatch(r"c\d{3}", parts[3]) \
                        and re.fullmatch(r"[\w.~-]+\.wav", parts[4]):
                    return self._file(E.WORK / parts[2] / parts[3] / parts[4], cache="max-age=3600")
                return self._json({"error": "bulunamadı"}, 404)
        except KeyError:
            return self._json({"ok": False, "error": "Video bulunamadı"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": str(e)}, 400)
        if p in ("", "/"):
            p = "/index.html"
        f = (UI / p.lstrip("/")).resolve()
        if UI.resolve() not in f.parents:
            return self._json({"error": "bulunamadı"}, 404)
        return self._file(f)

    do_HEAD = do_GET

    # ---- POST
    def do_POST(self):
        p = urlparse(self.path).path
        try:
            if p == "/api/open":
                body = self._body()
                v = manager().open_path(Path(body["path"]))
                return self._json(self._video_json(v))
            if p == "/api/upload":
                return self._upload()
            if p == "/api/open_url":
                return self._json({"ok": True, **youtube().start(self._body().get("url", ""))})
            if p == "/api/update/install":
                self._body()
                return self._json({"ok": True, **UPD.install()})
            if p == "/api/update/restart":
                self._body()
                UPD.restart()
                return self._json({"ok": True})
            if p == "/api/open_external":
                url = self._body().get("url", "")
                if not LINKS.match(url):
                    return self._json({"ok": False, "error": "izin verilmeyen bağlantı"}, 400)
                import webbrowser
                webbrowser.open(url)
                return self._json({"ok": True})
            if p == "/api/start":
                b = self._body()
                manager().start(b["video_id"], b["variant"], float(b.get("t", 0)), bool(b.get("buffering", True)),
                                b.get("range"), bool(b.get("fresh", False)), b.get("chunk_len"),
                                b.get("vox_steps"))
                return self._json(manager().status(b["video_id"], b["variant"]))
            if p == "/api/range":
                b = self._body()
                manager().set_range(b["video_id"], b["variant"], b.get("range"))
                return self._json(manager().status(b["video_id"], b["variant"]))
            if p == "/api/stop":
                b = self._body()
                manager().stop(b["video_id"], b["variant"])
                return self._json(manager().status(b["video_id"], b["variant"]))
            if p == "/api/redo":
                b = self._body()
                manager().redo(b["video_id"], b["variant"], float(b.get("t", 0)), bool(b.get("buffering", True)),
                               b.get("range"), b.get("chunk_len"), b.get("vox_steps"))
                return self._json(manager().status(b["video_id"], b["variant"]))
            if p == "/api/export":
                b = self._body()
                manager().export(b["video_id"], b["variant"], b["kind"], b.get("gains") or {}, b["path"])
                return self._json({"ok": True, **manager().export_state})
            if p == "/api/focus":
                b = self._body()
                manager().set_focus(b["video_id"], b["variant"], float(b.get("t", 0)))
                return self._json({"ok": True})
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            return self._json({"ok": False, "error": str(e)}, 400)
        return self._json({"error": "bulunamadı"}, 404)

    def _video_json(self, v):
        return {"ok": True, "video_id": v.id, "name": v.name, "duration": v.duration, "url": f"/video/{v.id}",
                "youtube": getattr(v, "youtube", None), "embed": getattr(v, "yt_embed", True)}

    def _upload(self):
        # sürükle-bırakta dosya yolu gelmezse: dosya uygulama klasörüne kopyalanır
        n = int(self.headers.get("Content-Length", "0"))
        name = unquote(self.headers.get("X-File-Name", "video.mp4"))
        ext = (Path(name).suffix.lower() or ".mp4")[:8]
        up = E.WORK / "_yuklenen"
        up.mkdir(exist_ok=True)
        dst = up / f"{uuid.uuid4().hex}{ext}"
        with open(dst, "wb") as fh:
            left = n
            while left > 0:
                b = self.rfile.read(min(1 << 20, left))
                if not b:
                    break
                fh.write(b)
                left -= len(b)
        v = manager().open_path(dst, name)
        return self._json(self._video_json(v))


class Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def make_server(port: int) -> Server:
    return Server(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    port = int(os.environ.get("DMS_PORT", "7860"))
    srv = make_server(port)
    print(f"Doğru mu Samet → http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()
