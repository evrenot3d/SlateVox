"""Eksik modelleri indirir. Var olanlar atlanır (her seferinde indirmez).
İndirmeler 'modeller/' klasörüne yapılır; sonrasında uygulama internetsiz çalışır.
"""
import io
import json
import os
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(BASE / "modeller" / "hf"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
sys.path.insert(0, str(BASE))

if os.name == "nt":                                  # eksik DLL'lerde Windows hata penceresi açılmasın (hata günlüğe düşer)
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


def ensure_ffmpeg_on_path() -> None:
    """audio-separator 'ffmpeg' komutunu PATH'te arar; imageio-ffmpeg'i oraya kopyalar."""
    if shutil.which("ffmpeg"):
        return
    try:
        import imageio_ffmpeg
        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return
    bindir = Path(__file__).resolve().parent / "modeller" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    dst = bindir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not dst.exists():
        shutil.copy2(src, dst)
    os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")


ensure_ffmpeg_on_path()
MODELS = BASE / "modeller"
LLAMA_DIR = MODELS / "llama.cpp"
UVR_MODEL = "UVR-MDX-NET-Voc_FT.onnx"
WHISPER_MODEL = "large-v3-turbo"
MT_REPO = "mradermacher/MiLMMT-46-4B-v1.0-GGUF"     # varsayılan çeviri modeli (engine.MT_MODELS ile aynı)
MT_FILE = "MiLMMT-46-4B-v1.0.Q4_K_M.gguf"


def has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None


def driver_major() -> int:
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=20).stdout
        return int(out.strip().split(".")[0])
    except Exception:
        return 0


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "slatevox"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def llama_cpp() -> None:
    """llama.cpp resmi Windows sürümünü (llama-server.exe) indirir."""
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    if LLAMA_DIR.exists() and any(LLAMA_DIR.rglob(exe)):
        print("    zaten var")
        return
    if os.name != "nt":
        raise RuntimeError("Linux/macOS: llama.cpp'yi kurun (llama-server PATH'te olmalı).")
    rels = json.loads(_get("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15"))
    drv = driver_major()
    cuda_pref = [r"13\.\d+", r"12\.\d+"] if drv >= 580 else [r"12\.\d+"]
    rel, wanted = None, []
    for r in rels:
        assets = {a["name"]: a["browser_download_url"] for a in r.get("assets", [])}
        names = list(assets)
        wanted = []
        if has_nvidia():
            for cv in cuda_pref:
                main = next((n for n in names if re.match(rf"^llama-.*bin-win-cuda-{cv}-x64\.zip$", n)), None)
                if main:
                    ver = re.search(r"cuda-([\d.]+)-x64", main).group(1)
                    cud = next((n for n in names if n == f"cudart-llama-bin-win-cuda-{ver}-x64.zip"), None)
                    wanted = [main] + ([cud] if cud else [])
                    break
        if not wanted:
            cpu = next((n for n in names if re.match(r"^llama-.*bin-win-cpu-x64\.zip$", n)), None)
            if cpu:
                wanted = [cpu]
        if wanted:
            rel = r
            break
    if not wanted:
        raise RuntimeError("Uygun llama.cpp Windows sürümü bulunamadı")
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    LLAMA_DIR.mkdir(parents=True, exist_ok=True)
    for n in wanted:
        print(f"    indiriliyor: {n}")
        zipfile.ZipFile(io.BytesIO(_get(assets[n]))).extractall(LLAMA_DIR)
    print(f"    llama.cpp {rel.get('tag_name')} hazır")


def uvr():
    target = MODELS / "uvr" / UVR_MODEL
    if target.exists():
        print("    zaten var")
        return
    from audio_separator.separator import Separator
    (BASE / "calisma").mkdir(exist_ok=True)
    sep = Separator(output_dir=str(BASE / "calisma"), model_file_dir=str(MODELS / "uvr"))
    sep.load_model(model_filename=UVR_MODEL)


def whisper():
    from faster_whisper.utils import download_model
    download_model(WHISPER_MODEL, cache_dir=str(MODELS / "whisper"))


def mt():
    if (MODELS / "milmmt" / MT_FILE).exists():
        print("    zaten var")
        return
    from huggingface_hub import hf_hub_download
    hf_hub_download(MT_REPO, MT_FILE, local_dir=str(MODELS / "milmmt"))


def ema():
    from ema_lightning import EMA
    EMA().say("Merhaba.")


def antalia():
    from antalia_mini import Antalia
    Antalia().say("Merhaba.")


def step(title, fn) -> bool:
    print(f"\n==> {title}", flush=True)
    try:
        fn()
        print("    tamam", flush=True)
        return True
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"    HATA: {e}", flush=True)
        return False


if __name__ == "__main__":
    ok = all([
        step("1/6 UVR-MDX-NET-Voc_FT (ses ayırma)", uvr),
        step("2/6 Whisper Large V3 Turbo (~1.6 GB)", whisper),
        step("3/6 llama.cpp (çeviri motoru)", llama_cpp),
        step("4/6 MiLMMT-46 4B Q4_K_M (~2.5 GB, çeviri)", mt),
        step("5/6 EMA Lightning (kadın sesi)", ema),
        step("6/6 Antalia-2 Mini (erkek sesi)", antalia),
    ])
    print("\nMODELLER_TAMAM" if ok else "\nMODELLER_EKSIK", flush=True)
    sys.exit(0 if ok else 1)
