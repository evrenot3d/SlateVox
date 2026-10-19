"""Güncelleme: GitHub'daki son sürümü denetler, istenirse indirip kurar.

Ayar: guncelleme.json  →  {"repo": "kullanici/depo"}
Sürüm: GitHub'da "v0.3.4" gibi etiketlenmiş son yayın (Release). Yayın notları "Yenilikler" olarak gösterilir.

Kurulumda yalnızca program dosyaları değişir. Şunlara hiç dokunulmaz:
  modeller/  calisma/  .venv*  yedek/  günlük dosyaları (*.log)  model_hizlari*.json
Değişen her dosyanın eski hali yedek/<eski sürüm>/ klasörüne alınır.
requirements.txt değiştiyse yeni paketler kurulur. Sonra program kendini yeniden başlatır.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import engine as E

CONF = E.BASE / "guncelleme.json"
BACKUP = E.BASE / "yedek"
PROTECT_DIRS = {"modeller", "calisma", "yedek", "python", ".venv", ".venv-vox", ".venv-vf", ".git", "__pycache__", "_test"}
PROTECT_RE = re.compile(r"(\.log$|^model_hizlari.*\.json$)", re.I)
UA = {"User-Agent": "slatevox-guncelleme", "Accept": "application/vnd.github+json"}


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:4]) or (0,)


def repo() -> str:
    try:
        return (json.loads(CONF.read_text(encoding="utf-8")).get("repo") or "").strip().strip("/")
    except Exception:
        return ""


def _get(url: str, timeout=30) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _protected(rel: Path) -> bool:
    return (rel.parts and rel.parts[0] in PROTECT_DIRS) or bool(PROTECT_RE.search(rel.name))


class Updater:
    def __init__(self):
        self.lock = threading.Lock()
        self.latest = None                 # son denetimin sonucu
        self.state = {"running": False}

    # ---- denetle
    def check(self, force: bool = False) -> dict:
        cur = E.VERSION
        r = repo()
        if not r:
            return {"ok": True, "current": cur, "configured": False}
        if self.latest and not force and time.time() - self.latest["_t"] < 600:
            return {k: v for k, v in self.latest.items() if not k.startswith("_")}
        if E.DEMO and os.environ.get("DMS_DEMO_UPDATE"):
            rel = json.loads(os.environ["DMS_DEMO_UPDATE"])
        else:
            try:
                rel = json.loads(_get(f"https://api.github.com/repos/{r}/releases/latest"))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {"ok": True, "current": cur, "configured": True, "repo": r, "latest": None, "newer": False}
                return {"ok": False, "current": cur, "error": f"GitHub'a ulaşılamadı ({e.code})"}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "current": cur, "error": "İnternet bağlantısı yok ya da GitHub'a ulaşılamadı"}
        tag = rel.get("tag_name") or ""
        zip_url = next((a["browser_download_url"] for a in rel.get("assets", [])
                        if a.get("name", "").lower().endswith(".zip")), None) or rel.get("zipball_url")
        res = {"ok": True, "current": cur, "configured": True, "repo": r, "latest": tag.lstrip("vV"),
               "newer": _ver(tag) > _ver(cur), "notes": (rel.get("body") or "").strip()[:4000],
               "date": (rel.get("published_at") or "")[:10], "url": rel.get("html_url"), "_zip": zip_url}
        self.latest = {**res, "_t": time.time()}
        return {k: v for k, v in res.items() if not k.startswith("_")}

    # ---- kur
    def install(self) -> dict:
        with self.lock:
            if self.state.get("running"):
                raise RuntimeError("Güncelleme zaten sürüyor.")
            info = self.check(force=True)
            if not info.get("ok"):
                raise RuntimeError(info.get("error") or "Güncelleme denetlenemedi")
            if not info.get("newer"):
                raise RuntimeError("Program zaten güncel.")
            self.state = {"running": True, "stage": "İndiriliyor", "progress": 0.0, "error": None, "done": False,
                          "version": info["latest"]}
        threading.Thread(target=self._work, args=(self.latest["_zip"], info["latest"]), daemon=True).start()
        return dict(self.state)

    def _work(self, url: str, newver: str):
        st = self.state
        try:
            data = self._download(url, st) if not (E.DEMO and url.startswith("demo:")) else self._demo_zip()
            st.update(stage="Dosyalar hazırlanıyor", progress=100.0)
            z = zipfile.ZipFile(io.BytesIO(data))
            names = [n for n in z.namelist() if not n.endswith("/")]
            # GitHub zip'leri tek bir üst klasör içinde gelir
            top = os.path.commonprefix([n.split("/")[0] + "/" for n in names]) if names else ""
            top = top if top.endswith("/") and all(n.startswith(top) for n in names) else ""
            files = {Path(n[len(top):]): n for n in names}
            if Path("app.py") not in files or Path("server.py") not in files:
                raise RuntimeError("İndirilen paket bu programa ait görünmüyor.")
            old_req = self._hash(E.BASE / "requirements.txt")
            bdir = BACKUP / E.VERSION
            st.update(stage="Kuruluyor")
            for rel, n in files.items():
                if _protected(rel):
                    continue
                dst = E.BASE / rel
                new = z.read(n)
                if dst.exists():
                    if dst.read_bytes() == new:
                        continue
                    (bdir / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, bdir / rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".yeni")
                tmp.write_bytes(new)
                os.replace(tmp, dst)
            if self._hash(E.BASE / "requirements.txt") != old_req and not E.DEMO:
                st.update(stage="Yeni bileşenler kuruluyor")
                py = Path(sys.executable)
                py = py.with_name("python.exe") if py.name.lower() == "pythonw.exe" else py
                subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
                                str(E.BASE / "requirements.txt")], creationflags=E.NO_WINDOW, cwd=str(E.BASE))
            st.update(stage=f"Sürüm {newver} kuruldu", done=True)
        except Exception as e:  # noqa: BLE001
            st["error"] = str(e)
        finally:
            st["running"] = False

    @staticmethod
    def _hash(p: Path) -> str:
        try:
            return hashlib.sha1(p.read_bytes()).hexdigest()
        except OSError:
            return ""

    @staticmethod
    def _download(url: str, st: dict) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=120) as r:
            total = int(r.headers.get("Content-Length") or 0)
            buf, n = io.BytesIO(), 0
            while chunk := r.read(1 << 16):
                buf.write(chunk)
                n += len(chunk)
                if total:
                    st["progress"] = round(n * 100.0 / total, 1)
        return buf.getvalue()

    @staticmethod
    def _demo_zip() -> bytes:
        time.sleep(1.0)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in ("app.py", "server.py"):
                z.writestr(f"demo-repo-abc/{f}", (E.BASE / f).read_bytes())
        return buf.getvalue()

    # ---- yeniden başlat
    def restart(self) -> None:
        """Yeni sürümü açar: yeni kopya eskisini kapatır (app.py stop_old_servers)."""
        def go():
            time.sleep(0.6)
            if E.DEMO or os.name != "nt":
                return
            flags = 0x00000008 | 0x00000200                          # DETACHED_PROCESS | NEW_PROCESS_GROUP
            launcher = E.BASE / "python" / "pythonw.exe"
            if launcher.exists() and (E.BASE / "baslat.py").exists():
                # EXE kurulumu: başlatıcı üzerinden aç (yeni sürüm ek bileşen istiyorsa kurulum ekranı onu tamamlar)
                subprocess.Popen([str(launcher), str(E.BASE / "baslat.py")], cwd=str(E.BASE), creationflags=flags)
                time.sleep(1.0)
                try:
                    import server
                    server.manager().engine.close()
                except Exception:  # noqa: BLE001
                    pass
                os._exit(0)
            exe = Path(sys.executable)
            pyw = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
            subprocess.Popen([str(pyw), str(E.BASE / "app.py")], cwd=str(E.BASE), creationflags=flags)
        threading.Thread(target=go, daemon=True).start()
