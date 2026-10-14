"""YouTube bağlantısından yalnız SES indirme (yt-dlp). Görüntü indirilmez: uygulamada YouTube oynatıcısıyla izlenir.

İlk kullanımda gerekenler kendiliğinden kurulur:
  yt-dlp[default]  (pip, ana ortama)
  Deno             (YouTube'un JavaScript doğrulamasını çözmek için yt-dlp'nin önerdiği çalıştırıcı; modeller/bin)
Bir indirme başarısız olursa yt-dlp bir kez güncellenip yeniden denenir (YouTube sık değişir).
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from importlib.util import find_spec
from pathlib import Path

import engine as E

YT_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/|v/)|youtu\.be/|"
                   r"youtube-nocookie\.com/embed/)([A-Za-z0-9_-]{11})")
DIR = E.WORK / "_youtube"
BIN = E.MODELS / "bin"
LOG = E.BASE / "youtube.log"
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
AUDIO_FMT = "bestaudio[ext=m4a]/bestaudio/best"
STREAM_FMT = "18/best[vcodec!=none][acodec!=none][height<=720]/best"


def video_id(url: str) -> str | None:
    m = YT_RE.search(url or "")
    return m.group(1) if m else None


def _py() -> Path:
    exe = Path(sys.executable)                       # pythonw yerine konsollu python (çıktı okunabilsin)
    return exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe


def _env() -> dict:
    env = dict(os.environ)
    env["PATH"] = str(BIN) + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _log(text: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


def _run(args, timeout=600) -> subprocess.CompletedProcess:
    _log("$ " + " ".join(map(str, args)))
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=_env(), creationflags=E.NO_WINDOW, timeout=timeout)
    _log((r.stdout or "")[-3000:] + (r.stderr or "")[-3000:])
    return r


class YouTube:
    def __init__(self, manager):
        self.m = manager
        self.lock = threading.Lock()
        self.state = {"running": False}
        self.updated = False
        self.streams = {}                            # video_id → (url, zaman)

    # ---- araçlar
    def ensure_tools(self, upd) -> None:
        if find_spec("yt_dlp") is None:
            upd("YouTube indirici kuruluyor (bir kerelik)")
            r = _run([_py(), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-U", "yt-dlp[default]"])
            if r.returncode:
                raise RuntimeError("YouTube indirici (yt-dlp) kurulamadı — youtube.log")
            self.updated = True
        if os.name == "nt" and not (BIN / "deno.exe").exists() and not shutil.which("deno"):
            upd("YouTube doğrulama bileşeni (Deno) indiriliyor (bir kerelik)")
            BIN.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(DENO_URL, headers={"User-Agent": "dogru-mu-samet"})
            with urllib.request.urlopen(req, timeout=600) as r:
                data = r.read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.extract("deno.exe", BIN)

    def _update(self, upd) -> bool:
        if self.updated:
            return False
        upd("YouTube indirici güncelleniyor")
        self.updated = True
        return _run([_py(), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-U", "yt-dlp[default]"]).returncode == 0

    def _ytdlp(self, *args, timeout=600):
        return _run([_py(), "-m", "yt_dlp", "--no-playlist", "--no-warnings", *args], timeout=timeout)

    # ---- bilgi + ses indirme
    def info(self, url: str, upd) -> dict:
        for attempt in (0, 1):
            r = self._ytdlp("-J", url, timeout=180)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
            if attempt == 0 and self._update(upd):
                continue
            last = (r.stderr or "").strip().splitlines()
            raise RuntimeError(_why(last[-1] if last else "bilinmeyen hata"))

    def download_audio(self, url: str, vid: str, upd) -> Path:
        DIR.mkdir(parents=True, exist_ok=True)
        done = [p for p in DIR.glob(f"{vid}.*") if p.suffix not in (".part", ".ytdl", ".json", ".tmp")]
        if done:                                     # aynı video bu oturumda zaten indirildi
            return done[0]
        for attempt in (0, 1):
            args = [_py(), "-m", "yt_dlp", "--no-playlist", "--no-warnings", "-f", AUDIO_FMT, "--newline",
                    "--progress-template", "download:DMS %(progress._percent_str)s",
                    "-o", str(DIR / f"{vid}.%(ext)s"), url]
            _log("$ " + " ".join(map(str, args)))
            p = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", env=_env(), creationflags=E.NO_WINDOW)
            tail = []
            for line in p.stdout:
                line = line.strip()
                m = re.search(r"DMS\s+([\d.]+)%", line)
                if m:
                    upd("YouTube sesi indiriliyor", float(m.group(1)))
                elif line:
                    tail = (tail + [line])[-20:]
            p.wait()
            _log("\n".join(tail))
            done = [q for q in DIR.glob(f"{vid}.*") if q.suffix not in (".part", ".ytdl", ".json", ".tmp")]
            if p.returncode == 0 and done:
                return done[0]
            if attempt == 0 and self._update(upd):
                continue
            err = next((t for t in reversed(tail) if "ERROR" in t), tail[-1] if tail else "bilinmeyen hata")
            raise RuntimeError(_why(err))

    # ---- arka planda: bağlantı → ses → video kaydı
    def start(self, url: str) -> dict:
        vid = video_id(url)
        if not vid:
            raise ValueError("Bu bir YouTube video bağlantısı değil.")
        with self.lock:
            if self.state.get("running"):
                raise RuntimeError("Önceki YouTube indirmesi sürüyor.")
            self.state = {"running": True, "stage": "YouTube bağlantısı açılıyor", "progress": 0.0, "error": None,
                          "video": None, "id": vid}
        threading.Thread(target=self._work, args=(url.strip(), vid), daemon=True).start()
        return dict(self.state)

    def _work(self, url: str, vid: str):
        st = self.state

        def upd(stage, progress=None):
            st["stage"] = stage
            if progress is not None:
                st["progress"] = round(float(progress), 1)

        try:
            if E.DEMO:
                info, path = self._demo(vid, upd)
            else:
                self.ensure_tools(upd)
                upd("Video bilgisi alınıyor")
                info = self.info(url, upd)
                if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
                    raise RuntimeError("Canlı yayınlar desteklenmiyor.")
                upd("YouTube sesi indiriliyor", 0)
                path = self.download_audio(url, info.get("id") or vid, upd)
            upd("Hazırlanıyor", 100)
            v = self.m.open_path(path, info.get("title") or vid)
            v.youtube = info.get("id") or vid
            v.yt_embed = info.get("playable_in_embed", True) is not False
            st["video"] = {"ok": True, "video_id": v.id, "name": v.name, "duration": v.duration,
                           "url": f"/video/{v.id}", "youtube": v.youtube, "embed": v.yt_embed}
        except Exception as e:  # noqa: BLE001
            _log(f"HATA: {e}")
            st["error"] = str(e)
        finally:
            st["running"] = False

    def _demo(self, vid, upd):
        src = Path(os.environ.get("DMS_DEMO_YT", ""))
        if not src.is_file():
            raise RuntimeError("Demo: DMS_DEMO_YT dosyası yok")
        DIR.mkdir(parents=True, exist_ok=True)
        for k in range(5):
            upd("YouTube sesi indiriliyor", k * 25)
            time.sleep(0.3)
        dst = DIR / f"{vid}{src.suffix}"
        shutil.copy(src, dst)
        return {"id": vid, "title": "Demo YouTube videosu", "playable_in_embed": True}, dst

    # ---- yerleştirme kapalı videolar için doğrudan akış adresi (düşük kalite, görüntü + ses birlikte)
    def stream_url(self, vid: str) -> str:
        url, t = self.streams.get(vid, (None, 0))
        if url and time.time() - t < 3 * 3600:
            return url
        if E.DEMO:
            return ""
        r = self._ytdlp("-g", "-f", STREAM_FMT, f"https://www.youtube.com/watch?v={vid}", timeout=120)
        lines = [x for x in (r.stdout or "").splitlines() if x.startswith("http")]
        if r.returncode or not lines:
            raise RuntimeError("Doğrudan akış adresi alınamadı.")
        self.streams[vid] = (lines[0], time.time())
        return lines[0]


def _why(msg: str) -> str:
    """yt-dlp hatasını anlaşılır Türkçeye çevir"""
    m = msg.lower()
    if "private video" in m:
        return "Bu video gizli."
    if "sign in to confirm your age" in m or "age-restricted" in m:
        return "Bu video yaş sınırlı; oturum açmadan indirilemiyor."
    if "video unavailable" in m or "not available" in m:
        return "Video bulunamadı ya da bu ülkede kullanılamıyor."
    if "sign in to confirm" in m and "bot" in m:
        return "YouTube geçici olarak doğrulama istiyor; biraz sonra yeniden deneyin."
    if "unable to download" in m or "http error" in m:
        return "YouTube'a bağlanılamadı: " + msg[-160:]
    return "YouTube: " + msg[-200:]
