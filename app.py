"""SlateVox — masaüstü uygulaması.

    BASLAT.bat  (ya da: .venv\\Scripts\\pythonw.exe app.py)

Yerel motor + Windows WebView2 penceresi. Arayüz yalnızca uygulamanın kendi penceresinde açılır (tarayıcı kullanılmaz).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
sys.path.insert(0, str(BASE))

if os.name == "nt":                                  # eksik DLL'lerde Windows hata penceresi açılmasın (hata günlüğe düşer)
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass
if sys.stdout is None or sys.stderr is None or os.environ.get("DMS_LOG") == "1":   # pythonw
    _log = open(BASE / "uygulama.log", "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = _log

PORT = int(os.environ.get("DMS_PORT", "7860"))
URL = f"http://127.0.0.1:{PORT}/"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
VIDEO_TYPES = ("Video dosyaları (*.mp4;*.mkv;*.mov;*.webm;*.avi;*.m4v)", "Tüm dosyalar (*.*)")


def stop_old_servers() -> None:
    """Önceki kopyayı (ya da eski sürümü) kapatır; port tek uygulamada kalsın."""
    try:
        with urllib.request.urlopen(URL, timeout=1.5) as r:
            body = r.read(20000).decode("utf-8", "replace")
            if ("SlateVox" not in body and "Doğru mu Samet" not in body          # eski adlı sürüm de kapatılır
                    and not r.headers.get("Server", "").startswith(("SlateVox", "DogruMuSamet"))):
                return
    except Exception:
        return
    if os.name != "nt":
        return
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 5 and p[1].endswith(f":{PORT}") and p[2] in ("0.0.0.0:0", "[::]:0") and p[4].isdigit() \
                and int(p[4]) != os.getpid():
            subprocess.run(["taskkill", "/F", "/PID", p[4]], capture_output=True, creationflags=NO_WINDOW)
    time.sleep(1.0)


def style_titlebar(win) -> None:
    """Windows başlık çubuğunu temaya uydurur: koyu renk, yazı/simge gizli (ad, arayüzde ortada)."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import byref, c_int, c_uint, sizeof

    try:
        hwnd = int(win.native.Handle.ToInt64())
    except Exception as e:  # noqa: BLE001
        print("hwnd alınamadı:", e, flush=True)
        return
    dwm = ctypes.windll.dwmapi
    for attr, val in ((20, c_int(1)),               # koyu mod
                      (35, c_uint(0x00424242)),     # başlık rengi (#424242)
                      (34, c_uint(0x001E1E1E)),     # kenarlık rengi
                      (36, c_uint(0x00E8E8E8))):    # başlık yazısı rengi
        dwm.DwmSetWindowAttribute(hwnd, attr, byref(val), sizeof(val))

    class WTA(ctypes.Structure):
        _fields_ = [("dwFlags", c_uint), ("dwMask", c_uint)]

    o = WTA(3, 3)                                   # WTNCA_NODRAWCAPTION | WTNCA_NODRAWICON
    ctypes.windll.uxtheme.SetWindowThemeAttribute(hwnd, 1, byref(o), sizeof(o))
    try:
        set_icon(hwnd, BASE / "logo.ico")
    except Exception as e:  # noqa: BLE001
        print("simge:", e, flush=True)


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
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("EvrenTug.SlateVox")
        except Exception:  # noqa: BLE001
            pass


class Api:
    """Arayüzden çağrılan masaüstü işlevleri (window.pywebview.api.*)"""
    _window = None          # alt çizgili: pywebview bu alanı JS'e taşımaya çalışmaz

    _fs = False

    def pick_video(self):
        import webview
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=VIDEO_TYPES)
        return res[0] if res else None

    def save_file(self, name, kind):
        """Kaydetme penceresi; seçilen yolu (uzantısı düzeltilmiş) döndürür."""
        import webview
        ext = ".mp4" if kind == "video" else ".wav"
        types = ("MP4 video (*.mp4)",) if ext == ".mp4" else ("WAV ses (*.wav)",)
        res = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=name, file_types=types)
        if isinstance(res, (list, tuple)):
            res = res[0] if res else None
        if not res:
            return None
        p = Path(res)
        return str(p if p.suffix.lower() == ext else p.with_suffix(ext))

    def set_fullscreen(self, on):
        on = bool(on)
        if on != self._fs:
            self._fs = on
            self._window.toggle_fullscreen()
        return self._fs


def main():
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} SlateVox başlıyor =====", flush=True)
    stop_old_servers()
    import server
    srv = server.make_server(PORT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=server.manager, daemon=True).start()   # warm up the engine object
    print(f"Arayüz: {URL}", flush=True)

    if os.environ.get("DMS_NO_WINDOW") == "1":
        srv_thread_wait()
        return
    app_id()
    import webview      # BASLAT.bat eksikse kurar; uygulama yalnızca kendi penceresinde açılır

    api = Api()
    win = webview.create_window("SlateVox", URL, width=1560, height=940, min_size=(1100, 680),
                                background_color="#282828", js_api=api, text_select=False, maximized=True)
    api._window = win

    def on_loaded():
        style_titlebar(win)
        # sürükle-bırakta dosyanın tam yolunu arayüze verir (File.pywebviewFullPath)
        try:
            from webview.dom import DOMEventHandler
            win.dom.document.events.drop += DOMEventHandler(lambda e: None, prevent_default=False)
        except Exception as e:  # noqa: BLE001
            print("drop handler:", e, flush=True)

    win.events.shown += lambda: style_titlebar(win)
    win.events.loaded += on_loaded
    try:
        webview.start(gui="edgechromium" if os.name == "nt" else None, private_mode=False,
                      storage_path=str(BASE / "calisma" / "_webview"))
    finally:
        try:
            server.manager().engine.close()
        except Exception:
            pass
    os._exit(0)


def srv_thread_wait():
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
