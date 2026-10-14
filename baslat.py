"""Doğru mu Samet? — başlatıcı ve ilk kurulum ekranı.

Masaüstü kısayolu bunu, kurulumla gelen Python ile (python\\pythonw.exe) çalıştırır; hiç terminal penceresi açılmaz.
  * Bileşenler kuruluysa: asıl programı (.venv\\Scripts\\pythonw.exe app.py) açar ve kapanır.
  * Değilse: programın kendi penceresinde "Gerekli bileşenler" ekranını gösterir. İndirmeler (PyTorch, kütüphaneler,
    yapay zekâ modelleri) bu ekranda ilerleme çubuklarıyla izlenir; bitince program kendiliğinden açılır.
Kurulum yarıda kalırsa bir sonraki açılışta kaldığı yerden sürer (kurulu olanlar atlanır).
Bu dosya yalnız Python'un kendi kütüphanelerini ve pywebview'i kullanır.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
VENV = BASE / ".venv"
VPY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
VPYW = VENV / ("Scripts/pythonw.exe" if os.name == "nt" else "bin/python")
MARK = VENV / ".kurulum_tamam"
LOG = BASE / "kurulum.log"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEMO = os.environ.get("DMS_DEMO") == "1"
TITLE = "Doğru mu Samet?"


def base_python() -> str:
    exe = Path(sys.executable)
    return str(exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe)


def ready() -> bool:
    return VPYW.exists() and MARK.exists()


def launch_app() -> None:
    subprocess.Popen([str(VPYW), str(BASE / "app.py")], cwd=str(BASE),
                     creationflags=(0x00000008 | 0x00000200) if os.name == "nt" else 0)


def has_gpu() -> tuple[bool, str]:
    if DEMO:
        return True, "NVIDIA GeForce RTX (demo)"
    smi = shutil.which("nvidia-smi")
    if not smi:
        return False, ""
    try:
        out = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True,
                             text=True, timeout=20, creationflags=NO_WINDOW).stdout.strip().splitlines()
        return True, out[0].strip() if out else "NVIDIA"
    except Exception:  # noqa: BLE001
        return True, "NVIDIA"


# ------------------------------------------------------------------ adımlar
# (anahtar, başlık, ağırlık) — ağırlık: genel ilerleme çubuğundaki payı (indirilecek boyutla orantılı)
STEPS = [
    ("env", "Python ortamı hazırlanıyor", 3),
    ("torch", "PyTorch indiriliyor (~3 GB)", 34),
    ("sep", "Ses ayırma bileşenleri", 7),
    ("cuda", "Ekran kartı kütüphaneleri (~1 GB)", 9),
    ("libs", "Program kütüphaneleri", 9),
    ("models", "Yapay zekâ modelleri indiriliyor (~6 GB)", 38),
]


class Cancelled(Exception):
    pass


class Setup:
    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._closing = False
        gpu, name = has_gpu()
        self._gpu, self._gpu_name = gpu, name
        self._s = {"phase": "intro", "step": None, "done": [], "skipped": [], "overall": 0.0, "frac": 0.0,
                  "detail": "", "error": None, "log": [], "steps": [{"key": k, "name": n} for k, n, _ in STEPS
                                                                 if k != "cuda" or gpu]}

    # ---- arayüzün çağırdıkları (pywebview js_api)
    def info(self):
        try:
            free = shutil.disk_usage(BASE).free / 2**30
        except OSError:
            free = 0
        return {"gpu": self._gpu, "gpu_name": self._gpu_name, "free_gb": round(free, 1), "path": str(BASE),
                "resume": VENV.exists()}

    def state(self):
        with self._lock:
            return dict(self._s, log=self._s["log"][-120:])

    def start(self):
        with self._lock:
            if self._s["phase"] == "running":
                return False
            self._s.update(phase="running", error=None, done=[], skipped=[], overall=0.0, frac=0.0, detail="")
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def open_app(self):
        launch_app()
        threading.Timer(0.8, lambda: os._exit(0)).start()
        return True

    def open_log(self):
        if os.name == "nt" and LOG.exists():
            os.startfile(str(LOG))                                     # noqa: S606 — günlüğü Not Defteri'nde açar
        return True

    # ---- iç işler
    def _log(self, line: str):
        line = line.rstrip()
        if not line:
            return
        with self._lock:
            self._s["log"].append(line[-300:])
            if len(self._s["log"]) > 400:
                del self._s["log"][:100]
        try:
            with open(LOG, "a", encoding="utf-8", errors="replace") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _set(self, **kw):
        with self._lock:
            self._s.update(kw)
            done_w = sum(w for k, _, w in STEPS if k in self._s["done"] or k in self._s["skipped"])
            cur_w = next((w for k, _, w in STEPS if k == self._s["step"]), 0)
            total = sum(w for k, _, w in STEPS if k != "cuda" or self._gpu)
            val = (done_w + cur_w * min(1.0, self._s["frac"])) * 100.0 / total
            self._s["overall"] = round(max(self._s["overall"], min(val, 100.0)), 1)

    def _run(self):
        self._log(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} kurulum başladı =====")
        try:
            for key, name, _ in STEPS:
                if key == "cuda" and not self._gpu:
                    continue
                self._set(step=key, frac=0.0, detail="")
                self._log(f"--- {name}")
                skipped = getattr(self, "_step_" + key)()
                with self._lock:
                    (self._s["skipped"] if skipped else self._s["done"]).append(key)
                self._set(frac=0.0)
            MARK.parent.mkdir(parents=True, exist_ok=True)
            MARK.write_text("ok", encoding="utf-8")
            self._set(phase="done", step=None, overall=100.0, detail="")
            self._log("KURULUM_TAMAM")
        except Cancelled:
            pass
        except Exception as e:  # noqa: BLE001
            self._log(f"HATA: {e}")
            self._set(phase="error", error=str(e))

    # komut çalıştır; çıktıyı satır satır (\r dahil) okuyup ilerlemeyi yakalar
    def _cmd(self, args, parse=None, check=True) -> int:
        if self._closing:
            raise Cancelled()
        args = [str(a) for a in args]
        self._log("> " + " ".join(args))
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   PIP_DISABLE_PIP_VERSION_CHECK="1", HF_HUB_DISABLE_SYMLINKS_WARNING="1",
                   HF_HOME=str(BASE / "modeller" / "hf"))
        p = subprocess.Popen(args, cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             creationflags=NO_WINDOW)
        self._proc = p
        buf = b""
        last_logged = ""
        while True:
            ch = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(1)
            if not ch:
                break
            buf += ch
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if parse:
                    parse(line)
                # ilerleme satırları günlüğü doldurmasın
                if re.match(r"^Progress \d+ of \d+", line) or re.search(r"\d+%\|", line):
                    continue
                if line != last_logged:
                    self._log(line)
                    last_logged = line
        p.wait()
        self._proc = None
        if self._closing:
            raise Cancelled()
        self._log(f"(çıkış kodu {p.returncode})")
        if check and p.returncode != 0:
            raise RuntimeError("Bir bileşen kurulamadı. İnternet bağlantınızı denetleyip yeniden deneyin.")
        return p.returncode

    def _ok(self, code: str) -> bool:
        return subprocess.run([str(VPY), "-c", code], capture_output=True, creationflags=NO_WINDOW).returncode == 0

    def _pip_parser(self, size_hint_gb: float = 0.0):
        """pip --progress-bar raw çıktısı: 'Progress <n> of <toplam>' satırları + 'Downloading <dosya> (<boyut>)'"""
        cur = {"name": ""}

        def parse(line: str):
            m = re.match(r"^Progress (\d+) of (\d+)", line)
            if m:
                n, t = int(m.group(1)), max(1, int(m.group(2)))
                big = t > 200 * 2**20                                 # büyük dosyalar adımın ilerlemesini belirler
                if big or size_hint_gb == 0:
                    self._set(frac=n / t)
                self._set(detail=f"{cur['name']} · {n / 2**20:,.0f} / {t / 2**20:,.0f} MB".replace(",", "."))
                return
            m = re.match(r"^(?:Downloading|Collecting)\s+(\S+)", line)
            if m:
                nm = m.group(1).rsplit("/", 1)[-1]
                nm = re.sub(r"-(cp\d+|py\d).*$", "", nm.split("#")[0])
                cur["name"] = nm
                self._set(detail=nm)
            elif line.startswith("Installing collected packages"):
                self._set(detail="Kuruluyor…")

        return parse

    def _pip(self, *pkgs, hint=0.0, check=True):
        return self._cmd([VPY, "-m", "pip", "install", "--progress-bar", "raw", *pkgs], self._pip_parser(hint),
                         check=check)

    # ---- 1) Python ortamı
    def _step_env(self):
        if DEMO:
            return self._demo(1.5)
        if not VPY.exists():
            self._set(detail="Sanal ortam oluşturuluyor")
            self._cmd([base_python(), "-m", "venv", VENV])
        self._set(frac=0.5, detail="pip güncelleniyor")
        self._cmd([VPY, "-m", "pip", "install", "-q", "-U", "pip", "wheel", "setuptools"])
        return False

    # ---- 2) PyTorch
    def _step_torch(self):
        if DEMO:
            return self._demo(4, "torch-2.9.1+cu128", 2900)
        if self._ok("import torch,sys;sys.exit(0 if torch.cuda.is_available() or not %s else 1)" % self._gpu):
            return True
        idx = "cu128" if self._gpu else "cpu"
        self._pip("torch", "torchvision", "--index-url", f"https://download.pytorch.org/whl/{idx}", hint=3.0)
        return False

    # ---- 3) ses ayırma
    def _step_sep(self):
        if DEMO:
            return self._demo(1.5, "audio_separator", 120)
        if self._ok("import audio_separator"):
            return True
        self._pip("audio-separator[gpu]" if self._gpu else "audio-separator[cpu]")
        return False

    # ---- 4) CUDA kütüphaneleri (Whisper ekran kartında çalışsın)
    def _step_cuda(self):
        if DEMO:
            return self._demo(2, "nvidia_cudnn_cu12", 700)
        if self._ok("import nvidia.cudnn"):
            return True
        self._pip("nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*", hint=1.0)
        return False

    # ---- 5) program kütüphaneleri + düzeltmeler
    def _step_libs(self):
        if DEMO:
            return self._demo(2, "faster_whisper", 80)
        self._pip("-r", BASE / "requirements.txt")
        self._set(frac=0.7, detail="Uyumluluk denetleniyor")
        if self._ok("import torchaudio"):                           # bazı paketler CPU torchaudio çeker
            self._cmd([VPY, "-m", "pip", "uninstall", "-y", "torchaudio"], check=False)
        if self._gpu:
            if not self._ok("import onnxruntime as o,sys;"
                            "sys.exit(0 if 'CUDAExecutionProvider' in o.get_available_providers() else 1)"):
                self._cmd([VPY, "-m", "pip", "uninstall", "-y", "onnxruntime", "onnxruntime-gpu"], check=False)
                self._pip("--no-deps", "onnxruntime-gpu")
            self._set(frac=0.85, detail="PyTorch ekran kartı uyumu")
            self._cmd([VPY, "-u", BASE / "torch_duzelt.py"])
        return False

    # ---- 6) modeller
    def _step_models(self):
        if DEMO:
            return self._demo(5, "MiLMMT-46-4B-v1.0.Q4_K_M.gguf", 2500)
        part = {"i": 0, "n": 6, "title": ""}

        def parse(line: str):
            m = re.match(r"^==>\s*(\d+)/(\d+)\s+(.*)$", line)
            if m:
                part.update(i=int(m.group(1)) - 1, n=int(m.group(2)), title=m.group(3))
                self._set(frac=part["i"] / part["n"], detail=part["title"])
                return
            m = re.search(r"(\d+)%\|", line)                        # tqdm: "dosya:  45%|████ | 1.1G/2.5G [...]"
            if m:
                pct = int(m.group(1)) / 100.0
                size = re.search(r"\|\s*([\d.]+\s*[kMG]?B?)/([\d.]+\s*[kMG]?B?)", line)
                self._set(frac=(part["i"] + pct) / part["n"],
                          detail=part["title"] + (f" · {size.group(1)} / {size.group(2)}" if size else f" · %{int(pct * 100)}"))

        self._cmd([VPY, "-u", BASE / "modelleri_indir.py"], parse)
        return False

    def _demo(self, sec, name="", mb=0):
        n = int(sec * 10)
        for i in range(n + 1):
            if self._closing:
                raise Cancelled()
            self._set(frac=i / n, detail=f"{name} · {mb * i / n:,.0f} / {mb:,.0f} MB".replace(",", ".") if name else "")
            time.sleep(0.1)
        return False

    def _stop(self):
        self._closing = True
        p = self._proc
        if p and p.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                                   creationflags=NO_WINDOW)
                else:
                    p.kill()
            except Exception:  # noqa: BLE001
                pass


def set_icon(hwnd: int, ico) -> None:
    """Pencere ve görev çubuğu simgesi (logo.ico)."""
    if os.name != "nt" or not ico.exists():
        return
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.LoadImageW.restype = wintypes.HANDLE
    u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    for big, size in ((1, 32), (0, 16)):
        h = u.LoadImageW(None, str(ico), 1, size, size, 0x00000010)          # IMAGE_ICON, LR_LOADFROMFILE
        if h:
            u.SendMessageW(hwnd, 0x0080, big, h)                             # WM_SETICON


def app_id() -> None:
    """Görev çubuğunda Python olarak değil, kendi adı ve simgesiyle görünsün."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("EvrenTug.DogruMuSamet")
        except Exception:  # noqa: BLE001
            pass


def dark_titlebar(win) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import byref, c_int, c_uint, sizeof
        hwnd = int(win.native.Handle.ToInt64())
        dwm = ctypes.windll.dwmapi
        for attr, val in ((20, c_int(1)), (35, c_uint(0x00424242)), (34, c_uint(0x001E1E1E)), (36, c_uint(0x00E8E8E8))):
            dwm.DwmSetWindowAttribute(hwnd, attr, byref(val), sizeof(val))
        set_icon(hwnd, BASE / "logo.ico")
    except Exception:  # noqa: BLE001
        pass


def message(text: str) -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)
    else:
        print(text)


def main():
    os.chdir(BASE)
    if ready() and "--kurulum" not in sys.argv:
        launch_app()
        return
    app_id()
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        message(f"Kurulum ekranı açılamadı ({e}).\nProgram klasöründeki KUR.bat ile kurabilirsiniz.")
        return
    api = Setup()
    win = webview.create_window(TITLE, str(BASE / "ui" / "kurulum.html"), js_api=api, width=900, height=840,
                                min_size=(780, 720), background_color="#282828", text_select=False)
    win.events.shown += lambda: dark_titlebar(win)
    win.events.closed += api._stop
    try:
        webview.start(gui="edgechromium" if os.name == "nt" else None, private_mode=True)
    except Exception as e:  # noqa: BLE001
        message(f"Kurulum ekranı açılamadı ({e}).\nWindows'ta Microsoft Edge WebView2 gerekir.")
    api._stop()
    os._exit(0)


if __name__ == "__main__":
    main()
