"""Doğru mu Samet — model motoru.

Modeller ilk kullanımda yüklenir ve uygulama açık kaldıkça ekran kartında hazır bekler:
  ses ayırma   UVR-MDX-NET-Voc_FT        (audio-separator, ONNX / CUDA)
               BandIt v2 · DnR, TIGER-DnR (isteğe bağlı, Detaylı mod, yavaş; sepmodels.py)
  metin        Whisper Large V3 Turbo    (faster-whisper, CUDA)
  kadın/erkek  wav2vec2 voice gender     (onnxruntime)
  çeviri       MiLMMT-46 4B (varsayılan, Hızlı ve Detaylı) · Detaylı'da seçilebilir: Hy-MT2 7B
               (GGUF Q4_K_M, llama.cpp llama-server, CUDA; karta sığmayan katmanlar işlemciye kalır)
  dublaj       Hızlı: Ahenk Cep (kadın, duygulu, 16 adım) · Antalia-2 Mini (erkek); Gelişmiş'te EMA Lightning de var
               VoxCPM2 (Detaylı, isteğe bağlı): her cümleyi kendi İngilizce sesinden kopyalar, duygu ve hızı taşır
               (ayrı Python ortamında, .venv-vox; ilk kullanımda kendiliğinden kurulur)
               Ahenk Cep / Ahenk (Gelişmiş, deneysel): Türkçe kadın sesi, duygu talimatıyla; seçilince bütün
               cümleleri okur. Ahenk = VoxCPM2 + Türkçe LoRA (aynı yardımcı süreçte).
  duygu        Emotion English DistilRoBERTa (J. Hartmann; ONNX, işlemcide; İngilizce metinden)
               + orijinal sesin şiddeti / perdesi (Ahenk modellerine duygu talimatı olarak gider)
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
import urllib.request
import wave
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
MODELS = BASE / "modeller"
WORK = BASE / "calisma"
WORK.mkdir(exist_ok=True)
MODELS.mkdir(exist_ok=True)
os.environ.setdefault("HF_HOME", str(MODELS / "hf"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

VERSION = "0.4.5"
DEMO = os.environ.get("DMS_DEMO") == "1"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _hide_child_consoles() -> None:
    """Uygulama penceresiz (pythonw) çalışır; kütüphanelerin başlattığı ffmpeg vb. alt süreçler her seferinde
    kısa süre siyah konsol penceresi açmasın diye tüm alt süreçlere CREATE_NO_WINDOW eklenir."""
    if os.name != "nt" or getattr(subprocess.Popen, "_dms_hidden", False):
        return
    orig = subprocess.Popen.__init__

    def init(self, *args, **kw):
        kw["creationflags"] = kw.get("creationflags", 0) | NO_WINDOW
        orig(self, *args, **kw)

    subprocess.Popen.__init__ = init
    subprocess.Popen._dms_hidden = True


_hide_child_consoles()

UVR_MODEL = "UVR-MDX-NET-Voc_FT.onnx"
WHISPER_MODEL = "large-v3-turbo"
# Çeviri modelleri (tahmini kaliteye göre iyiden kötüye). overhead: bağlam + hesap belleği (MB)
MT_DEFAULT = "milmmt4"
MT_MODELS = {
    "hymt7": {"name": "Hy-MT2 7B", "repo": "tencent/Hy-MT2-7B-GGUF", "file": "Hy-MT2-7B-Q4_K_M.gguf",
              "dir": "hy-mt2", "fmt": "hymt", "layers": 32, "ctx": 2048, "overhead": 900, "demo_s": 1.2, "size_mb": 4700},
    "milmmt4": {"name": "MiLMMT-46 4B", "repo": "mradermacher/MiLMMT-46-4B-v1.0-GGUF",
                "file": "MiLMMT-46-4B-v1.0.Q4_K_M.gguf", "dir": "milmmt", "fmt": "milmmt", "layers": 34,
                "ctx": 2048, "overhead": 700, "demo_s": 0.9, "size_mb": 2500},
}
GENDER_REPO = "Alice-Sabrina-Ivy/voice-gender-classifier-onnx-q8-v2"
SPK_REPO, SPK_FILE = "Wespeaker/wespeaker-voxceleb-resnet34-LM", "voxceleb_resnet34_LM.onnx"   # konuşmacı ayırma
DUB_SR = 48000


# ------------------------------------------------------------------ küçük yardımcılar
def ensure_ffmpeg_on_path() -> None:
    if shutil.which("ffmpeg"):
        return
    try:
        import imageio_ffmpeg
        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return
    bindir = MODELS / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    dst = bindir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not dst.exists():
        shutil.copy2(src, dst)
    os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")


ensure_ffmpeg_on_path()
shutil.rmtree(MODELS / "polarformer", ignore_errors=True)      # kaldırılan BS-PolarFormer'ın indirdiği dosyalar
shutil.rmtree(MODELS / "translategemma", ignore_errors=True)   # kaldırılan TranslateGemma 12B
for _f in list((MODELS / "hy-mt2").rglob("Hy-MT2-1.8B*")) if (MODELS / "hy-mt2").exists() else []:
    try:                                                         # kaldırılan Hy-MT2 1.8B (7B kalır)
        _f.unlink()
    except OSError:
        pass


def ffmpeg(*args) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if r.returncode != 0:
        lines = (r.stderr or "").strip().splitlines()
        raise RuntimeError("ffmpeg: " + (lines[-1] if lines else "bilinmeyen hata"))


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr or "")
    if not m:
        raise RuntimeError("Videonun süresi okunamadı.")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def has_audio(path: Path) -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return "Audio:" in (r.stderr or "")


def energy_profile(path: Path, hop_s: float = 0.1) -> np.ndarray:
    """Whole soundtrack at 8 kHz mono → RMS per 100 ms (for choosing quiet chunk borders)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000",
           "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768
    hop = int(8000 * hop_s)
    n = len(x) // hop
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    return np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)


def pcm16k(path: Path, start: float, dur: float) -> np.ndarray:
    """Videonun [start, start+dur] aralığı: 16 kHz mono float32."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}",
           "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768


def find_pause(path: Path, lo: float, hi: float, target: float) -> float:
    """[lo, hi] aralığında cümleyi bölmeyecek en iyi kesim noktası.
    Konuşma algılayıcı (Silero VAD) ile konuşmasız boşluklar bulunur; hedefe en yakın belirgin boşluk seçilir,
    kesim o boşluğun en sessiz anına konur. Konuşma bulunamazsa en düşük ses enerjili an seçilir."""
    if hi - lo < 1.0:
        return round(target, 3)
    x = pcm16k(path, lo, hi - lo)
    if len(x) < 16000:
        return round(target, 3)
    sr = 16000
    gaps = []
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        sp = get_speech_timestamps(x, VadOptions(min_silence_duration_ms=120, speech_pad_ms=40))
        prev = 0
        for seg in sp:
            if seg["start"] > prev:
                gaps.append((prev, seg["start"]))
            prev = max(prev, seg["end"])
        if prev < len(x):
            gaps.append((prev, len(x)))
    except Exception as e:  # noqa: BLE001
        print("[vad]", e, flush=True)
    hop = sr // 20                                                        # 50 ms
    n = len(x) // hop
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    best, best_score = None, -1e9
    for a, b in gaps:
        ln = (b - a) / sr
        if ln < 0.15:
            continue
        mid = lo + (a + b) / 2 / sr
        score = min(ln, 0.8) - 0.12 * abs(mid - target)              # hedef süreye yakın, gerçek bir cümle arası
        if score > best_score:
            best, best_score = (a, b), score
    if best:
        fa, fb = best[0] // hop, max(best[0] // hop + 1, best[1] // hop)
        k = fa + int(np.argmin(rms[fa:fb]))
    else:
        sm = np.convolve(rms, np.ones(8) / 8, mode="same")
        dist = np.abs(np.arange(n) * hop / sr + lo - target)
        k = int(np.argmin(sm * (1 + 0.3 * dist)))
    return round(lo + (k + 0.5) * hop / sr, 3)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """16-bit / float WAV → float32 [n, ch]"""
    try:
        import soundfile as sf
        x, sr = sf.read(str(path), dtype="float32", always_2d=True)
        return x, sr
    except ImportError:
        with wave.open(str(path), "rb") as w:
            sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
            x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768
        return x.reshape(-1, ch), sr


def write_wav(path: Path, x: np.ndarray, sr: int) -> None:
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    os.replace(tmp, path)


def resample(x: np.ndarray, sr: int, to: int) -> np.ndarray:
    if sr == to:
        return x
    try:
        import soxr
        return soxr.resample(x, sr, to).astype(np.float32)
    except ImportError:
        pass
    try:
        import librosa
    except ImportError:                     # son çare: doğrusal ara değerleme
        n = int(round(len(x) * to / sr))
        t = np.linspace(0, len(x) - 1, n)
        if x.ndim == 2:
            return np.stack([np.interp(t, np.arange(len(x)), x[:, c]) for c in range(x.shape[1])], 1).astype(np.float32)
        return np.interp(t, np.arange(len(x)), x).astype(np.float32)
    if x.ndim == 2:
        return librosa.resample(x.T, orig_sr=sr, target_sr=to).T.astype(np.float32)
    return librosa.resample(x, orig_sr=sr, target_sr=to).astype(np.float32)


def active_rms(x: np.ndarray, sr: int) -> float:
    hop = max(1, int(sr * 0.05))
    n = len(x) // hop
    if n == 0:
        return 0.0
    r = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    act = r[r > max(r.max() * 0.06, 1e-4)]
    return float(np.sqrt((act ** 2).mean())) if len(act) else 0.0


def trim_silence(a: np.ndarray, sr: int, pad: float = 0.03) -> np.ndarray:
    if len(a) == 0:
        return a
    thr = max(float(np.abs(a).max()) * 0.02, 1e-4)
    idx = np.flatnonzero(np.abs(a) > thr)
    if len(idx) == 0:
        return a[:0]
    p = int(pad * sr)
    return a[max(0, idx[0] - p): min(len(a), idx[-1] + p)]


_MEL = {}


def kaldi_fbank(x16: np.ndarray, num_mel: int = 80) -> np.ndarray:
    """torchaudio.compliance.kaldi.fbank ile aynı hesap (16 kHz · 25/10 ms · hamming · dither yok) + ortalama çıkarma.
    WeSpeaker konuşmacı modelinin beklediği giriş."""
    L, S, NFFT = 400, 160, 512
    x = np.asarray(x16, dtype=np.float64) * 32768.0
    if len(x) < L:
        x = np.pad(x, (0, L - len(x)))
    n = 1 + (len(x) - L) // S
    fr = x[np.arange(L)[None, :] + S * np.arange(n)[:, None]]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = np.concatenate([fr[:, :1] * 0.03, fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1)       # ön vurgu (ilk örnek: x0-0.97x0)
    win = 0.54 - 0.46 * np.cos(2 * np.pi * np.arange(L) / (L - 1))
    spec = np.abs(np.fft.rfft(fr * win, n=NFFT)) ** 2
    if num_mel not in _MEL:
        mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)                                  # noqa: E731
        lo, hi = mel(20.0), mel(8000.0)
        d = (hi - lo) / (num_mel + 1)
        fm = mel(16000.0 / NFFT * np.arange(NFFT // 2))
        bank = np.zeros((num_mel, NFFT // 2 + 1))
        for b in range(num_mel):
            l, c, r = lo + b * d, lo + (b + 1) * d, lo + (b + 2) * d
            bank[b, : NFFT // 2] = np.maximum(0.0, np.minimum((fm - l) / (c - l), (r - fm) / (r - c)))
        _MEL[num_mel] = bank
    m = np.log(np.maximum(spec @ _MEL[num_mel].T, np.finfo(np.float32).eps))
    return (m - m.mean(axis=0, keepdims=True)).astype(np.float32)


def free_gpu() -> None:
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def vram_free_mb() -> int:
    """Kartın gerçek boş belleği. Windows (WDDM) altında torch'un bildirdiği boş bellek, sürücünün sistem
    belleğine taşıyabildiği miktarı da sayar; bu yüzden nvidia-smi'nin gördüğü kullanılan bellek esas alınır."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
        total, used = (int(float(v)) for v in r.stdout.strip().splitlines()[0].split(","))
        return total - used
    except Exception:
        try:
            import torch
            return torch.cuda.mem_get_info()[0] >> 20
        except Exception:
            return 1 << 20


def gpu_name() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return ""


def _dir_mb(paths) -> float:
    tot = 0
    for d in paths:
        try:
            for root, _, files in os.walk(d):
                for f in files:
                    try:
                        tot += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
        except OSError:
            pass
    return tot / 2 ** 20


class download_watch:
    """İlk kullanımda model indirilirken klasör boyutunu izleyip ilerlemeyi aşamaya yazar.
    Model zaten varsa yalnız "yükleniyor" der."""

    def __init__(self, on, label: str, dirs, approx_mb: float, present: bool):
        self.on, self.label, self.dirs, self.mb, self.present = on, label, [Path(d) for d in dirs], approx_mb, present
        self.stop = threading.Event()

    def __enter__(self):
        if self.present or DEMO:
            self.on("indiriliyor", 0, f"{self.label} yükleniyor")
            return self
        size = f"~{self.mb / 1024:.1f} GB".replace(".", ",") if self.mb >= 1024 else f"~{self.mb:.0f} MB"
        self.on("indiriliyor", 0, f"{self.label} indiriliyor ({size}, yalnızca bir kez)")
        base = _dir_mb(self.dirs)

        def run():
            while not self.stop.wait(1.0):
                got = max(0.0, _dir_mb(self.dirs) - base)
                pct = min(99.0, got * 100.0 / max(1.0, self.mb))
                self.on("indiriliyor", pct, f"{self.label} indiriliyor · {got:,.0f} / ~{self.mb:,.0f} MB · yalnızca bir kez".replace(",", "."))
        threading.Thread(target=run, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        return False


def _has_files(d: Path, pattern: str = "*") -> bool:
    try:
        return any(p.is_file() and p.stat().st_size > 1_000_000 for p in Path(d).rglob(pattern))
    except OSError:
        return False


def _add_nvidia_dll_dirs() -> None:
    if os.name != "nt":
        return
    try:
        import nvidia  # type: ignore
        for root in getattr(nvidia, "__path__", []):
            for b in Path(root).glob("*/bin"):
                os.add_dll_directory(str(b))
                os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass


# ------------------------------------------------------------------ llama-server (çeviri)
class LlamaServer:
    PORT = int(os.environ.get("DMS_LLAMA_PORT", "8089"))

    def __init__(self):
        self.proc = None
        self.log = None
        self.model = None

    @staticmethod
    def exe() -> Path | None:
        name = "llama-server.exe" if os.name == "nt" else "llama-server"
        d = MODELS / "llama.cpp"
        found = sorted(d.rglob(name)) if d.exists() else []
        if found:
            return found[0]
        w = shutil.which("llama-server")
        return Path(w) if w else None

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def start(self, model: Path, on=None, ngl: int = 99, ctx: int = 4096, jinja: bool = True):
        if self.running() and self.model == Path(model):
            return
        self.stop()
        exe = self.exe()
        if not exe:
            sys.path.insert(0, str(BASE))
            import modelleri_indir
            with download_watch(on or (lambda *a: None), "Çeviri motoru (llama.cpp)", [MODELS / "llama.cpp"], 700, False):
                modelleri_indir.llama_cpp()
            exe = self.exe()
        if not exe:
            raise RuntimeError("llama-server bulunamadı.")
        self.log = open(BASE / "llama-server.log", "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [str(exe), "-m", str(model), "--host", "127.0.0.1", "--port", str(self.PORT),
             "-ngl", str(ngl), "-c", str(ctx), "-np", "1"] + (["--jinja"] if jinja else []),
            cwd=str(exe.parent), stdout=self.log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        self.model = Path(model)
        for _ in range(240):
            if self.proc.poll() is not None:
                raise RuntimeError("llama-server kapandı (llama-server.log)")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.PORT}/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("llama-server açılmadı (llama-server.log)")

    def chat(self, prompt: str, max_tokens: int) -> str:
        body = json.dumps({
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1, "top_p": 0.6, "top_k": 20, "repeat_penalty": 1.05,
            "max_tokens": max_tokens, "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{self.PORT}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8"))["choices"][0]["message"].get("content") or ""

    def complete(self, prompt: str, max_tokens: int, stop=None) -> str:
        """Hazır biçimlenmiş istem ile ham tamamlama (MiLMMT)."""
        body = {"prompt": prompt, "n_predict": max_tokens, "temperature": 0.0, "top_k": 1,
                "repeat_penalty": 1.0, "cache_prompt": True, "stream": False}
        if stop:
            body["stop"] = stop
        req = urllib.request.Request(f"http://127.0.0.1:{self.PORT}/completion", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8")).get("content") or ""

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except Exception:
                self.proc.kill()
        self.proc = None
        self.model = None


MT_PROMPT = ("Translate the following text into Turkish. Note that you should only output the translated "
             "result without any additional explanation:\n\n{text}")
MILMMT_PROMPT = "Translate this from English to Turkish:\nEnglish: {text}\nTurkish:"

def clean_translation(out: str, fallback: str) -> str:
    out = re.sub(r"<think>.*?</think>", "", out or "", flags=re.S).strip()
    out = out.strip().strip('"“”«»').strip()
    out = re.sub(r"^(Türkçe|Turkish)\s*:\s*", "", out, flags=re.I)
    return out or fallback


# ------------------------------------------------------------------ VoxCPM2 yardımcı süreci
VOX_VENV = BASE / ".venv-vox"
VOX_CFG, VOX_STEPS = 2.0, 10            # yönlendirme gücü, difüzyon adımı (VoxCPM2 önerilen değerler)

# Ahenk (Özgür Güler · ağırlıklar CC BY 4.0, kod Apache-2.0): tek tasarlanmış kadın sesi (f022), duygu talimatlı
AHENK_REPO, AHENK_DIR = "ozgurguler/ahenk-tts-tr", MODELS / "ahenk"          # VoxCPM2 + Türkçe LoRA (~140 MB)
CEP_REPO, CEP_DIR = "ozgurguler/ahenk-cep-tr", MODELS / "ahenk-cep"          # küçük model (EMA Lightning, ~35 MB)
AHENK_STEPS = 16                        # iki modelin de ölçülmüş / varsayılan adım sayısı
EMOTIONS = ("neutral", "angry", "cheerful", "sad", "calm", "surprised", "empathetic", "laughing")
EMO_TR = {"neutral": "düz", "angry": "kızgın", "cheerful": "neşeli", "sad": "üzgün", "calm": "sakin",
          "surprised": "şaşkın", "empathetic": "şefkatli", "laughing": "gülerek"}
# duygu algılama: Emotion English DistilRoBERTa-base (J. Hartmann, 2022; onnx-community ONNX kopyası, ~83 MB)
# 7 etiket (anger, disgust, fear, joy, neutral, sadness, surprise) → Ahenk'in duyguları
EMO_REPO, EMO_DIR = "onnx-community/emotion-english-distilroberta-base-ONNX", MODELS / "emotion-jh"
EMO_MAP = {"anger": "angry", "disgust": "angry", "joy": "cheerful", "sadness": "sad", "surprise": "surprised",
           "neutral": "neutral", "fear": "fear"}
EMO_MIN = 0.40                          # en olası duygu bundan zayıfsa düz okunur
LAUGH_RE = re.compile(r"\b(ha(ha)+|he(he)+|lol)\b|\((laugh|chuckl)|\[(laugh|chuckl)", re.I)
COMFORT_RE = re.compile(r"\b(don'?t worry|it'?s (ok(ay)?|alright|all right)|calm down|deep breath|"
                        r"(it|everything)('?ll| will) be (ok(ay)?|fine|alright)|i'?m here for you|"
                        r"sorry for your|not your fault|i understand)\b", re.I)


def vox_python() -> Path | None:
    for c in (VOX_VENV / "Scripts" / "python.exe", VOX_VENV / "bin" / "python"):
        if c.exists():
            return c
    return None


def vox_ready() -> bool:
    return vox_python() is not None and (VOX_VENV / ".tamam").exists()


class VoxProc:
    """VoxCPM2'yi ayrı ortamda çalıştırır (kütüphaneleri ana ortamla çakışmasın). İlk kullanımda ortamı kurar."""

    def __init__(self):
        self.proc = None
        self.sr = 48000
        self.ahenk = False                           # süreç Ahenk LoRA'sıyla mı açıldı
        self.lock = threading.Lock()

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def install(self, on):
        log = open(BASE / "voxcpm_kurulum.log", "a", encoding="utf-8", errors="replace")

        def run(cmd, msg) -> bool:
            on("indiriliyor", 0, msg)
            log.write("\n$ " + " ".join(map(str, cmd)) + "\n")
            log.flush()
            r = subprocess.run([str(c) for c in cmd], stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            return r.returncode == 0

        try:
            exe = Path(sys.executable)
            base_py = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
            if vox_python() is None and not run([base_py, "-m", "venv", VOX_VENV], "VoxCPM2 kuruluyor · Python ortamı"):
                raise RuntimeError("VoxCPM2 ortamı oluşturulamadı (voxcpm_kurulum.log)")
            py = vox_python()
            run([py, "-m", "pip", "install", "--upgrade", "pip"], "VoxCPM2 kuruluyor · pip")
            tv, cu = "", "cu128"                     # ana ortamla aynı PyTorch / CUDA (RTX 50 serisi cu128+ ister)
            try:
                import torch
                tv = torch.__version__.split("+")[0]
                if "+" in torch.__version__:
                    cu = torch.__version__.split("+", 1)[1]
                elif torch.version.cuda:
                    cu = "cu" + torch.version.cuda.replace(".", "")
            except Exception:  # noqa: BLE001
                pass
            idx = ["--index-url", f"https://download.pytorch.org/whl/{cu}"]
            tries = ([[f"torch=={tv}", f"torchaudio=={tv}"], [f"torch=={tv}", "torchaudio"]] if tv else []) \
                + [["torch", "torchaudio"]]
            if not any(run([py, "-m", "pip", "install", *pk, *idx], "VoxCPM2 kuruluyor · PyTorch indiriliyor (~3 GB)")
                       for pk in tries):
                raise RuntimeError("VoxCPM2 için PyTorch kurulamadı (voxcpm_kurulum.log)")
            r = subprocess.run([str(py), "-c", "import torch;print(torch.__version__)"], capture_output=True, text=True,
                               creationflags=NO_WINDOW)
            cons = VOX_VENV / "kisit.txt"           # voxcpm kurulurken CUDA'lı torch değiştirilmesin
            cons.write_text(f"torch=={r.stdout.strip()}\n" if r.returncode == 0 and r.stdout.strip() else "",
                            encoding="utf-8")
            if not run([py, "-m", "pip", "install", "voxcpm", "soundfile", "-c", cons],
                       "VoxCPM2 kuruluyor · kütüphaneler indiriliyor"):
                # tam kurulum olmazsa (ör. derleme isteyen bir yan paket): yalnız seslendirme için gerekenler
                core = ["transformers>=4.36.2", "einops", "inflect", "addict", "huggingface-hub", "pydantic", "tqdm",
                        "simplejson", "sortedcontainers", "soundfile", "librosa", "safetensors", "argbind"]
                if not (run([py, "-m", "pip", "install", "--no-deps", "voxcpm"], "VoxCPM2 kuruluyor · yedek kurulum")
                        and run([py, "-m", "pip", "install", *core, "-c", cons], "VoxCPM2 kuruluyor · temel kütüphaneler")):
                    raise RuntimeError("VoxCPM2 kütüphanesi kurulamadı (voxcpm_kurulum.log)")
            (VOX_VENV / ".tamam").write_text("ok", encoding="utf-8")
        finally:
            log.close()

    def _read(self) -> dict:
        while True:                                  # kütüphanelerin araya yazdığı satırları atla
            line = self.proc.stdout.readline()
            if not line:
                try:
                    rc = self.proc.wait(10)
                except Exception:  # noqa: BLE001
                    rc = None
                why = {3221225477: "bellek erişim hatası", 3221226505: "iç hata", 3221225501: "işlemci komutu desteklenmiyor",
                       3221225725: "yığın taşması", 3221225495: "bellek yetersiz", 3221226356: "yığın bozulması"}
                code = f" · çıkış kodu {rc:#x} {why.get(rc & 0xFFFFFFFF, '')}".rstrip() if isinstance(rc, int) else ""
                with open(BASE / "voxcpm.log", "a", encoding="utf-8", errors="replace") as f:
                    f.write(f"\n[VoxCPM2 süreci kapandı{code}]\n")
                raise RuntimeError(f"VoxCPM2 kapandı{code} (voxcpm.log)")
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if isinstance(j, dict):
                return j

    def ensure(self, on, lora: Path | None = None):
        """lora: Ahenk klasörü — süreç LoRA'sız açıksa LoRA'yla yeniden başlatılır (LoRA'lı süreç ikisini de yapar)."""
        if self.running() and (lora is None or self.ahenk):
            return
        self.stop()
        if not vox_ready():
            self.install(on)
        have = _has_files(MODELS / "voxcpm", "*.safetensors")
        name = "VoxCPM2 + Ahenk" if lora else "VoxCPM2"
        for attempt in (1, 2):                       # ilk yükleme kapanırsa bir kez daha denenir
            msg = f"{name} yükleniyor" if have or attempt == 2 else \
                f"{name} indiriliyor (~5 GB, yalnızca bir kez) ve yükleniyor"
            on("indiriliyor", 0, msg + (" · yeniden deneniyor" if attempt == 2 else ""))
            env = dict(os.environ, DMS_VOX_CACHE=str(MODELS / "voxcpm"), PYTHONIOENCODING="utf-8",
                       PYTHONFAULTHANDLER="1")
            env.pop("DMS_VOX_LORA", None)
            if lora:
                env["DMS_VOX_LORA"] = str(lora)
            err = open(BASE / "voxcpm.log", "a", encoding="utf-8", errors="replace")
            err.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {name} başlatılıyor (deneme {attempt}) =====\n")
            err.flush()
            self.proc = subprocess.Popen([str(vox_python()), "-u", str(BASE / "vox_worker.py")], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8",
                                         creationflags=NO_WINDOW, cwd=str(BASE), env=env)
            try:
                j = self._read()
                break
            except RuntimeError:
                self.proc = None
                if attempt == 2:
                    raise
                time.sleep(2)
        if not j.get("ready"):
            raise RuntimeError("VoxCPM2 başlatılamadı (voxcpm.log)")
        self.sr = int(j.get("sr", 48000))
        self.ahenk = bool(j.get("ahenk"))

    def run(self, req: dict) -> dict:
        with self.lock:
            self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            j = self._read()
            if j.get("error"):
                raise RuntimeError("VoxCPM2: " + j["error"])
            return j

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(8)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.proc = None
        self.ahenk = False


def atempo(a: np.ndarray, sr: int, f: float, tmpdir: Path) -> np.ndarray:
    """Perdeyi bozmadan hızlandırır (ffmpeg atempo)."""
    if f <= 1.01:
        return a
    i, o = tmpdir / "_t_in.wav", tmpdir / "_t_out.wav"
    write_wav(i, a, sr)
    ffmpeg("-i", i, "-filter:a", f"atempo={f:.3f}", o)
    y, _ = read_wav(o)
    return y.mean(axis=1) if y.ndim > 1 else y


# ------------------------------------------------------------------ motor
class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self._sep = None
        self._sep2 = {}
        self._whisper = None
        self._gender = None
        self._spk = None
        self._llama = LlamaServer()
        self._tts = {}                 # "kadin" EMA · "erkek" Antalia · "ahenkcep" Ahenk Cep
        self._vox = VoxProc()
        self._emo = None               # go_emotions (işlemcide)

    # ---- ekran kartı belleği (8 GB kartlarda modeller sırayla yer açar)
    def make_room(self, need_mb: int, keep: tuple = ()) -> None:
        """Boş VRAM need_mb'den azsa o an gerekmeyen modelleri sırayla bellekten çıkarır (gerekince yeniden yüklenir).
        Bellek taşarsa Windows sistem belleğine taşar ve her şey çok yavaşlar; bunu önler."""
        if DEMO:
            return
        try:
            import torch
            if not torch.cuda.is_available():
                return
        except Exception:
            return
        import gc

        def free_mb():
            torch.cuda.empty_cache()
            return vram_free_mb()

        if free_mb() >= need_mb:
            return
        victims = [("whisper", "_whisper"), ("uvr", "_sep"), ("gender", "_gender")]
        for name, attr in victims:
            if name in keep or getattr(self, attr, None) is None:
                continue
            setattr(self, attr, None)
            gc.collect()
            print(f"[bellek] {name} boşaltıldı, boş: {free_mb()} MB", flush=True)
            if free_mb() >= need_mb:
                return
        for name in [k for k in list(self._sep2) if k not in keep]:
            self._sep2.pop(name, None)
            gc.collect()
            print(f"[bellek] {name} boşaltıldı, boş: {free_mb()} MB", flush=True)
            if free_mb() >= need_mb:
                return
        if "llama" not in keep and getattr(self._llama, "proc", None) is not None:
            self._llama.stop()
            time.sleep(0.5)
            print(f"[bellek] çeviri sunucusu kapatıldı, boş: {free_mb()} MB", flush=True)
            if free_mb() >= need_mb:
                return
        for g in [g for g in list(self._tts) if g not in keep]:
            self._tts.pop(g, None)
            gc.collect()
            print(f"[bellek] tts-{g} boşaltıldı, boş: {free_mb()} MB", flush=True)
            if free_mb() >= need_mb:
                return
        if "vox" not in keep and self._vox.running():
            self._vox.stop()
            time.sleep(1.0)
            print(f"[bellek] VoxCPM2 kapatıldı, boş: {free_mb()} MB", flush=True)
            if free_mb() >= need_mb:
                return

    # ---- 1) ses ayırma
    def separate(self, mix: Path, voc: Path, inst: Path, on, model: str = "uvr") -> tuple[Path, Path]:
        self.make_room(1200 if model == "uvr" else 2500, keep=(model,))
        if model != "uvr" and not DEMO:
            return self._separate2(model, mix, voc, inst, on)
        if DEMO:
            x, sr = read_wav(mix)
            write_wav(voc, x.mean(axis=1), sr)
            write_wav(inst, x * 0.3, sr)
            time.sleep(1.0)
            return voc, inst
        with self.lock:
            if self._sep is None:
                from audio_separator.separator import Separator
                tmp = WORK / "_sep"
                tmp.mkdir(exist_ok=True)
                with download_watch(on, "Ses ayırma modeli", [MODELS / "uvr"], 67,
                                    (MODELS / "uvr" / UVR_MODEL).exists()):
                    self._sep = Separator(output_dir=str(tmp), output_format="WAV", model_file_dir=str(MODELS / "uvr"))
                    self._sep.load_model(model_filename=UVR_MODEL)
            on("calisiyor", 10, "Konuşma ve arka plan ayrılıyor")
            tag = f"p{os.getpid()}_{int(time.time() * 1000)}"
            names = {"Vocals": f"{tag}_v", "Instrumental": f"{tag}_i"}
            outs = self._sep.separate(str(mix), custom_output_names=names)
            tmp = WORK / "_sep"
            fv, fi = tmp / f"{tag}_v.wav", tmp / f"{tag}_i.wav"
            for o in outs or []:      # older versions return other names
                p = Path(o) if Path(o).is_absolute() else tmp / o
                if "(Vocals)" in p.name and not fv.exists():
                    p.rename(fv)
                if "(Instrumental)" in p.name and not fi.exists():
                    p.rename(fi)
            if not fv.exists() or not fi.exists():
                raise RuntimeError("Ses ayırma çıktı üretmedi.")
            os.replace(fv, voc)
            os.replace(fi, inst)
            return voc, inst


    # ---- 1b) ek (yavaş) ayırma modelleri: BandIt v2 · DnR, TIGER-DnR  (sepmodels.py)
    def _sep2_model(self, name: str, on):
        import torch
        import sepmodels as SM
        m = self._sep2.get(name)
        if m is None:
            on("indiriliyor", 0, f"{SM.INFO[name]['name']} hazırlanıyor (ilk seferde indirilir)")
            dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            for other in [k for k in self._sep2 if k != name]:     # aynı anda tek ek model bellekte
                self._sep2.pop(other, None)
            free_gpu()
            m = (SM.load(name, MODELS / "sepcode", dev, on), dev)
            self._sep2[name] = m
        return m

    def _separate2(self, name: str, mix: Path, voc: Path, inst: Path, on) -> tuple[Path, Path]:
        import sepmodels as SM
        with self.lock:
            model, dev = self._sep2_model(name, on)
            x, sr = read_wav(mix)
            a = np.ascontiguousarray(x.T, dtype=np.float32)
            if a.shape[0] == 1:
                a = np.repeat(a, 2, axis=0)
            a = a[:2]
            msr = SM.INFO[name]["sr"]
            a_m = resample(a.T, sr, msr).T if sr != msr else a
            on("calisiyor", 3, f"{SM.INFO[name]['name']} çalışıyor")
            nm = SM.INFO[name]["name"]
            T = a_m.shape[1]
            blk, ov = 120 * msr, 2 * msr                 # uzun ses: 2 dk'lık bloklar, 2 sn çapraz geçiş (bellek sabit)
            if T <= blk + ov * 2:
                sp = SM.separate(name, model, np.ascontiguousarray(a_m, dtype=np.float32), msr, dev,
                                 lambda p: on("calisiyor", 3 + 94 * p, f"{nm} %{p * 100:.0f}"))
            else:
                sp = np.zeros_like(a_m, dtype=np.float32)
                wsum = np.zeros(T, dtype=np.float32)
                starts = list(range(0, T - ov, blk - ov))
                for bi, s0 in enumerate(starts):
                    e0 = min(T, s0 + blk)
                    y = SM.separate(name, model, np.ascontiguousarray(a_m[:, s0:e0], dtype=np.float32), msr, dev,
                                    lambda p, bi=bi: on("calisiyor", 3 + 94 * (bi + p) / len(starts),
                                                        f"{nm} %{(bi + p) / len(starts) * 100:.0f}"))
                    w = np.ones(e0 - s0, dtype=np.float32)
                    r = min(ov, (e0 - s0) // 2)
                    if s0 > 0:
                        w[:r] = np.linspace(0, 1, r, dtype=np.float32)
                    if e0 < T:
                        w[-r:] = np.linspace(1, 0, r, dtype=np.float32)
                    sp[:, s0:e0] += y[:, : e0 - s0] * w
                    wsum[s0:e0] += w
                sp = sp / np.maximum(wsum, 1e-6)
            if msr != sr:
                sp = resample(sp.T, msr, sr).T
            n = min(a.shape[1], sp.shape[1])
            sp = np.pad(sp[:, :n], ((0, 0), (0, a.shape[1] - n)))
            write_wav(voc, sp.T, sr)
            write_wav(inst, (a - sp).T, sr)
            free_gpu()
            return voc, inst

    # ---- 2) konuşma → metin
    def transcribe(self, vocals: Path, dur: float, on) -> list:
        if DEMO:
            demo = ["Hello and welcome to this short video.", "Today we are testing the dubbing app.",
                    "Every sentence lights up while it is spoken.", "The background music keeps playing."]
            words, t, k = [], 0.5, 0
            while t + 3 < dur:
                ws = demo[k % len(demo)].split(" ")
                step = 2.6 / len(ws)
                for j, w in enumerate(ws):
                    words.append((t + j * step, t + (j + 1) * step - 0.05, " " + w))
                t += 3.6
                k += 1
            time.sleep(0.8)
            return words
        self.make_room(2400 if self._whisper is None else 700, keep=("whisper",))
        with self.lock:
            if self._whisper is None:
                _add_nvidia_dll_dirs()
                from faster_whisper import WhisperModel
                with download_watch(on, "Whisper", [MODELS / "whisper"], 1550,
                                    _has_files(MODELS / "whisper", "model.bin")):
                    try:
                        self._whisper = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16",
                                                     download_root=str(MODELS / "whisper"))
                    except Exception as e:
                        print("[whisper] GPU açılamadı, CPU:", e, flush=True)
                        self._whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                                     download_root=str(MODELS / "whisper"))
            on("calisiyor", 0, "Yazıya dökülüyor")
            # ses dosyası yerine 16 kHz dizi verilir: faster-whisper'ın dosya okuyucusu (PyAV) bazı sürümlerde bozuk
            x, sr = read_wav(vocals)
            audio = x.mean(axis=1).astype(np.float32)
            if sr != 16000:
                audio = resample(audio, sr, 16000).astype(np.float32)
            segs, _ = self._whisper.transcribe(
                audio, language="en", task="transcribe", beam_size=5, word_timestamps=True,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 500}, condition_on_previous_text=False)
            words = []
            for s in segs:
                for w in s.words or []:
                    words.append((float(w.start), float(w.end), w.word))
                on("calisiyor", min(99, s.end / max(dur, 0.1) * 100), "Yazıya dökülüyor")
            return words

    # ---- 2b) kadın / erkek
    def _gender_model(self):
        if self._gender is None:
            from huggingface_hub import hf_hub_download
            import onnxruntime as ort
            onnx = hf_hub_download(GENDER_REPO, "onnx/model_quantized.onnx")
            cfg = json.loads(Path(hf_hub_download(GENDER_REPO, "config.json")).read_text(encoding="utf-8"))
            try:
                pre = json.loads(Path(hf_hub_download(GENDER_REPO, "preprocessor_config.json")).read_text(encoding="utf-8"))
            except Exception:
                pre = {}
            sess = ort.InferenceSession(onnx, providers=["CPUExecutionProvider"])
            labels = {int(k): v for k, v in cfg.get("id2label", {}).items()}
            fem = next((k for k, v in labels.items() if str(v).lower().startswith("f")), 1)
            self._gender = (sess, sess.get_inputs()[0].name, fem, bool(pre.get("do_normalize", True)))
        return self._gender

    def female_prob(self, x16: np.ndarray) -> float:
        sess, name, fem, norm = self._gender_model()
        x = x16.astype(np.float32)
        if norm:
            x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
        logits = sess.run(None, {name: x[None, :]})[0][0]
        e = np.exp(logits - logits.max())
        return float(e[fem] / e.sum())

    # ---- 2c) konuşmacı ses izi (kim konuşuyor) — WeSpeaker ResNet34-LM, işlemcide (ekran kartını meşgul etmez)
    def speaker_embeddings(self, vocals: Path, segs: list) -> list:
        """Her cümle için 256 boyutlu, birim uzunlukta ses izi (0,4 sn'den kısa cümlelerde None)."""
        if DEMO:                                       # demo: 2 kadın, 3 erkek gibi davranan sahte izler
            rng = np.random.default_rng(len(segs))
            out = []
            for i in range(len(segs)):
                who = (i // 3) % 2 if i % 3 == 1 else 2 + (i % 3 + i // 7) % 3
                e = np.zeros(256)
                e[who * 20:(who + 1) * 20] = 1.0
                e = e + rng.normal(0, 0.08, 256)
                out.append([round(float(v), 4) for v in e / np.linalg.norm(e)])
            return out
        if self._spk is None:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            p = hf_hub_download(SPK_REPO, SPK_FILE, local_dir=str(MODELS / "wespeaker"))
            so = ort.SessionOptions()
            so.intra_op_num_threads = 4
            self._spk = ort.InferenceSession(p, sess_options=so, providers=["CPUExecutionProvider"])
        x, sr = read_wav(vocals)
        x16 = resample(x.mean(axis=1), sr, 16000)
        out = []
        for s in segs:
            a = x16[max(0, int(s["start"] * 16000)): int(s["end"] * 16000)]
            if len(a) < int(0.4 * 16000):
                out.append(None)
                continue
            e = self._spk.run(["embs"], {"feats": kaldi_fbank(a)[None]})[0][0].astype(np.float64)
            e = e / (np.linalg.norm(e) + 1e-9)
            out.append([round(float(v), 4) for v in e])
        return out

    def classify(self, vocals: Path, segs: list, prior: dict, on) -> tuple[list, list]:
        """segs: [{start,end}] chunk-local. prior: {'kadin': w, 'erkek': w} from earlier chunks."""
        if DEMO:
            time.sleep(0.4)
            return [("kadin" if i % 3 == 1 else "erkek") for i in range(len(segs))], [0.9 if i % 3 == 1 else 0.1 for i in range(len(segs))]
        with self.lock:
            if self._gender is None:
                on("indiriliyor", 0, "Kadın/erkek modeli yükleniyor")
                self._gender_model()
            x, sr = read_wav(vocals)
            x16 = resample(x.mean(axis=1), sr, 16000)
            out = []
            n = len(segs)
            for i, s in enumerate(segs):
                a = x16[max(0, int(s["start"] * 16000)): int(s["end"] * 16000)]
                dur = len(a) / 16000
                pf, conf = 0.5, 0.0
                if dur >= 0.3:
                    W = 4 * 16000
                    wins = [a] if len(a) <= W * 1.5 else [a[k:k + W] for k in range(0, len(a) - 16000, W)][:6]
                    acc = ws = 0.0
                    for w in wins:
                        wt = float(np.sqrt((w ** 2).mean() + 1e-12)) * np.sqrt(len(w) / 16000)
                        acc += self.female_prob(w) * wt
                        ws += wt
                    pf = acc / ws if ws > 0 else 0.5
                    conf = abs(pf - 0.5) * 2 * min(1.0, dur)
                out.append((pf, conf))
                on("calisiyor", (i + 1) / max(1, n) * 100, f"{i + 1}/{n} cümle")
        fw = prior.get("kadin", 0.0) + sum(c for p, c in out if p >= 0.5)
        mw = prior.get("erkek", 0.0) + sum(c for p, c in out if p < 0.5)
        major = "kadin" if fw > mw else "erkek"
        labels = [major if c < 0.15 else ("kadin" if p >= 0.5 else "erkek") for p, c in out]
        for i in range(n):
            if out[i][1] >= 0.5:
                continue
            best, gap_best = None, 1e9
            for j in (i - 1, i + 1, i - 2, i + 2):
                if j < 0 or j >= n or out[j][1] < 0.6:
                    continue
                gap = segs[i]["start"] - segs[j]["end"] if j < i else segs[j]["start"] - segs[i]["end"]
                if gap < 2.5 and gap < gap_best:
                    best, gap_best = j, gap
            if best is not None:
                labels[i] = "kadin" if out[best][0] >= 0.5 else "erkek"
        return labels, [round(p, 3) for p, _ in out]

    # ---- 3) çeviri
    def _mt_path(self, key: str, on) -> Path:
        m = MT_MODELS[key]
        path = MODELS / m["dir"] / m["file"]
        if not path.exists():
            from huggingface_hub import hf_hub_download
            with download_watch(on, m["name"], [MODELS / m["dir"]], m.get("size_mb", 2500), False):
                path = Path(hf_hub_download(m["repo"], m["file"], local_dir=str(MODELS / m["dir"])))
        return path

    @staticmethod
    def _fit_layers(size_mb: float, m: dict) -> int:
        """Model karta sığmıyorsa sığan katman sayısı (gerisi işlemcide çalışır; bellek taşmasından hızlıdır)."""
        if DEMO:
            return 99
        free = vram_free_mb() - 250
        if free >= size_mb + m["overhead"]:
            return 99
        n = max(0, int((free - m["overhead"]) / (size_mb / m["layers"])))
        print(f"[çeviri] {m['name']}: {n}/{m['layers']} katman ekran kartında (boş {free + 250} MB)", flush=True)
        return n

    def translate(self, texts: list, on, model: str = MT_DEFAULT) -> list:
        key = model if model in MT_MODELS else MT_DEFAULT
        m = MT_MODELS[key]
        if DEMO:
            time.sleep(m["demo_s"])
            return [f"(demo {m['name']}) " + t for t in texts]
        with self.lock:
            path = self._mt_path(key, on)
            if not (self._llama.running() and self._llama.model == path):
                self._llama.stop()
                size = path.stat().st_size / 2 ** 20
                need = int(size + m["overhead"] + 300)
                self.make_room(need, keep=("llama",))    # yalnız yetmezse diğer modeller sırayla boşaltılır
                on("indiriliyor", 0, f"Çeviri modeli yükleniyor ({m['name']})")
                # ham istemle çalışan modelde (MiLMMT) sunucunun sohbet şablonu kullanılmaz
                self._llama.start(path, on, ngl=self._fit_layers(size, m), ctx=m["ctx"], jinja=m["fmt"] == "hymt")
            on("calisiyor", 0, f"0/{len(texts)} cümle")
            out = []
            for i, t in enumerate(texts):
                n = min(1024, len(t) * 3 + 64)
                if m["fmt"] == "hymt":
                    r = self._llama.chat(MT_PROMPT.format(text=t), n)
                else:
                    r = self._llama.complete(MILMMT_PROMPT.format(text=t), n,
                                             stop=["\nEnglish:", "\nTranslate this", "\n\n"])
                    r = next((ln for ln in r.splitlines() if ln.strip()), "")     # ilk dolu satır = çeviri
                out.append(clean_translation(r, t))
                on("calisiyor", (i + 1) / max(1, len(texts)) * 100, f"{i + 1}/{len(texts)} cümle")
            return out

    # ---- 4) dublaj
    @staticmethod
    def _tts_seen(gender: str) -> bool:
        return (MODELS / f".tts_{gender}").exists()

    def _tts_model(self, gender: str):
        if gender not in self._tts:
            if gender == "kadin":
                from ema_lightning import EMA
                ema = EMA()
                # İlk say() çağrısında EMA 1..128 toplu iş boyutlarını ölçer; bellek dolu dizüstü GPU'da bu
                # ölçüm bitmeyecek kadar yavaşlar. Cümleleri tek tek okuttuğumuz için küçük sabit boyut yeter.
                ema._batch_size = 4
                self._tts[gender] = (ema, 1.0)
            else:
                from antalia_mini import Antalia
                self._tts[gender] = (Antalia(), 0.95)
        return self._tts[gender]

    def say(self, gender: str, text: str, speed_mul: float = 1.0, seed: int = 0) -> np.ndarray:
        if DEMO:
            d = max(0.5, len(text) * 0.055 / speed_mul)
            t = np.arange(int(d * DUB_SR)) / DUB_SR
            f0 = 210 if gender == "kadin" else 120
            return (0.2 * (0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t)) * np.sin(2 * np.pi * f0 * t)).astype(np.float32)
        tts, base = self._tts_model(gender)
        sp = tts.say(text, speed=float(base * speed_mul), seed=int(seed))
        a = np.asarray(sp.audio, dtype=np.float32).reshape(-1)
        sr = int(getattr(sp, "sample_rate", DUB_SR))
        return resample(a, sr, DUB_SR) if sr != DUB_SR else a

    def dub(self, segs: list, voices: list, chunk_dur: float, target_rms: float, on, seed: int = 0) -> tuple[np.ndarray, list]:
        """segs chunk-local [{start,end,tr}], voices per seg ('kadin'/'erkek') → (48 kHz track, timings)."""
        self.make_room(2500, keep=tuple(set(voices)))
        with self.lock:
            for g in set(voices):
                if g not in self._tts and not DEMO:
                    with download_watch(on, "EMA Lightning" if g == "kadin" else "Antalia-2 Mini", [MODELS / "hf"], 350,
                                        self._tts_seen(g)):
                        self._tts_model(g)
                    (MODELS / f".tts_{g}").write_text("ok", encoding="utf-8")
            n = len(segs)
            track = np.zeros(int((chunk_dur + 6) * DUB_SR), dtype=np.float32)
            cursor, timings = 0.0, []
            for i, s in enumerate(segs):
                text = (s.get("tr") or "").strip()
                if not text:
                    timings.append(None)
                    continue
                nxt = segs[i + 1]["start"] if i + 1 < n else chunk_dur + 1.0
                start = max(float(s["start"]), cursor)
                avail = max(0.4, nxt - start - 0.05)
                a = trim_silence(self.say(voices[i], text, 1.0, seed + i), DUB_SR)
                d = len(a) / DUB_SR
                if d > avail * 1.03:
                    a = trim_silence(self.say(voices[i], text, min(d / avail, 1.35), seed + i), DUB_SR)
                    d = len(a) / DUB_SR
                i0 = int(start * DUB_SR)
                if i0 + len(a) > len(track):
                    track = np.concatenate([track, np.zeros(i0 + len(a) - len(track), dtype=np.float32)])
                track[i0:i0 + len(a)] += a
                timings.append([round(start, 3), round(start + d, 3)])
                cursor = start + d + 0.05
                on("calisiyor", (i + 1) / max(1, n) * 100, f"{i + 1}/{n} cümle")
            cur = active_rms(track, DUB_SR)
            if target_rms > 0 and cur > 0:
                track *= float(np.clip(target_rms / cur, 0.25, 4.0))
            peak = float(np.abs(track).max() or 1.0)
            if peak > 0.98:
                track *= 0.98 / peak
            return track, timings

    # ---- 4b) dublaj · VoxCPM2 (sesi kopyalar, duyguyu taşır)
    def dub_vox(self, segs: list, voc_path: Path, voices: list, chunk_dur: float, target_rms: float, on,
                seed: int = 0, work: Path | None = None, steps: int = VOX_STEPS) -> tuple[np.ndarray, list]:
        """Her cümle, ses ayırmadan çıkan kendi İngilizce sesi + metniyle (devam ettirme) okunur: vurgu, hız ve duygu
        taşınır. Ses rengi için ayrıca ≥3 sn örnek verilir (kısa cümlede aynı sesin komşu cümleleri eklenir).
        Her cümlenin ses gücü özgününe eşlenir (bağırma yüksek, fısıltı alçak kalır)."""
        x, sr = read_wav(voc_path)
        mono = x.mean(axis=1) if x.ndim > 1 else x
        n = len(segs)

        def clip(a, b):
            return mono[max(0, int(a * sr)): max(0, min(len(mono), int(b * sr)))]

        if not DEMO:
            self.make_room(7200, keep=("vox",))
        with self.lock:
            if not DEMO and not self._vox.running():
                self._vox.ensure(on)
            on("calisiyor", 0, f"0/{n} cümle")
            tmp = (work or voc_path.parent) / "_vox"
            tmp.mkdir(exist_ok=True)
            track = np.zeros(int((chunk_dur + 6) * DUB_SR), dtype=np.float32)
            cursor, timings = 0.0, []
            try:
                for i, s in enumerate(segs):
                    text = (s.get("tr") or "").strip()
                    if not text:
                        timings.append(None)
                        continue
                    nxt = segs[i + 1]["start"] if i + 1 < n else chunk_dur + 1.0
                    start = max(float(s["start"]), cursor)
                    avail = max(0.4, nxt - start - 0.05)
                    own = clip(s["start"] - 0.06, s["end"] + 0.12)
                    parts, tot, j, k = [own], len(own) / sr, i - 1, i + 1
                    while tot < 3.0:                        # kısa cümle: aynı sesin bitişik cümleleriyle örnek uzat
                        if k < n and voices[k] == voices[i] and segs[k]["start"] - segs[k - 1]["end"] < 1.5:
                            c = clip(segs[k]["start"] - 0.05, segs[k]["end"] + 0.1)
                            parts.append(c)
                            k += 1
                        elif j >= 0 and voices[j] == voices[i] and segs[j + 1]["start"] - segs[j]["end"] < 1.5:
                            c = clip(segs[j]["start"] - 0.05, segs[j]["end"] + 0.1)
                            parts.insert(0, c)
                            j -= 1
                        else:
                            break
                        tot += len(c) / sr
                    gap = np.zeros(int(0.15 * sr), dtype=np.float32)
                    ref = np.concatenate([p for c in parts for p in (c, gap)][:-1]) if len(parts) > 1 else own
                    if DEMO:
                        time.sleep(0.01 * steps)
                        a = self.say(voices[i], text, 1.0, seed + i)
                    else:
                        pw, rw, ow = tmp / f"p{i}.wav", tmp / f"r{i}.wav", tmp / f"o{i}.wav"
                        write_wav(pw, resample(own, sr, 16000), 16000)
                        write_wav(rw, resample(ref, sr, 16000), 16000)
                        en = (s.get("en") or "").strip()
                        req = {"text": text, "out": str(ow), "ref_wav": str(rw), "cfg": VOX_CFG, "steps": int(steps),
                               "seed": int(seed) * 1000 + i}
                        if en and len(own) / sr >= 0.5:
                            req.update(prompt_wav=str(pw), prompt_text=en)
                        self._vox.run(req)
                        y, ysr = read_wav(ow)
                        a = resample(y.mean(axis=1) if y.ndim > 1 else y, ysr, DUB_SR)
                    a = trim_silence(np.asarray(a, dtype=np.float32), DUB_SR)
                    ro, rd = active_rms(own, sr), active_rms(a, DUB_SR)
                    if ro > 0 and rd > 0:
                        a = a * float(np.clip(ro / rd, 0.2, 5.0))
                    d = len(a) / DUB_SR
                    if d > avail * 1.03:
                        a = atempo(a, DUB_SR, min(d / avail, 1.35), tmp)
                        d = len(a) / DUB_SR
                    i0 = int(start * DUB_SR)
                    if i0 + len(a) > len(track):
                        track = np.concatenate([track, np.zeros(i0 + len(a) - len(track), dtype=np.float32)])
                    track[i0:i0 + len(a)] += a
                    timings.append([round(start, 3), round(start + d, 3)])
                    cursor = start + d + 0.05
                    on("calisiyor", (i + 1) / max(1, n) * 100, f"{i + 1}/{n} cümle")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            cur = active_rms(track, DUB_SR)
            if target_rms > 0 and cur > 0:
                track *= float(np.clip(target_rms / cur, 0.25, 4.0))
            peak = float(np.abs(track).max() or 1.0)
            if peak > 0.98:
                track *= 0.98 / peak
            return track, timings

    # ---- 2d) duygu: İngilizce metin + orijinal sesin şiddeti / perdesi → Ahenk'in duyguları
    def _emo_model(self, on):
        if self._emo is None:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
            with download_watch(on, "Duygu modeli", [EMO_DIR], 85,
                                (EMO_DIR / "onnx" / "model_quantized.onnx").exists()):
                p = {f: hf_hub_download(EMO_REPO, f, local_dir=str(EMO_DIR))
                     for f in ("onnx/model_quantized.onnx", "tokenizer.json", "config.json")}
            cfg = json.loads(Path(p["config.json"]).read_text(encoding="utf-8"))
            labels = [cfg["id2label"][str(i)] for i in range(len(cfg["id2label"]))]
            tok = Tokenizer.from_file(p["tokenizer.json"])
            tok.enable_truncation(128)
            so = ort.SessionOptions()
            so.intra_op_num_threads = 4
            sess = ort.InferenceSession(p["onnx/model_quantized.onnx"], sess_options=so,
                                        providers=["CPUExecutionProvider"])
            self._emo = (sess, tok, labels)
        return self._emo

    def _emo_text(self, texts: list, on) -> tuple[np.ndarray, list]:
        """Her cümle için 7 duygunun olasılığı (toplamı 1)."""
        sess, tok, labels = self._emo_model(on)
        out = []
        for t in texts:                                  # tek tek: dolgu (padding) sonucu etkilemesin
            e = tok.encode(t or ".")
            lg = sess.run(None, {"input_ids": np.array([e.ids], dtype=np.int64),
                                 "attention_mask": np.array([e.attention_mask], dtype=np.int64)})[0][0]
            p = np.exp(lg - lg.max())
            out.append(p / p.sum())
        return np.array(out), labels

    @staticmethod
    def _emo_audio(mono: np.ndarray, sr: int, segs: list) -> list:
        """Her cümlenin ses şiddeti (dB) ve perdesi (yarım ton): (db, st) ya da None."""
        import librosa
        x16 = resample(mono, sr, 16000)
        out = []
        for s in segs:
            a = x16[max(0, int(s["start"] * 16000)): int(s["end"] * 16000)]
            if len(a) < 0.3 * 16000:
                out.append(None)
                continue
            db = 20 * np.log10(active_rms(a, 16000) + 1e-6)
            f0 = librosa.yin(a, fmin=65, fmax=500, sr=16000, frame_length=1024, hop_length=320)
            n = min(len(f0), len(a) // 320)
            fr = np.sqrt(np.array([np.mean(a[j * 320:(j + 1) * 320] ** 2) for j in range(n)]) + 1e-12)
            voiced = f0[:n][fr > fr.max() * 0.15] if n else f0
            st = float(np.median(12 * np.log2(voiced / 100.0))) if len(voiced) >= 5 else None
            out.append((float(db), st))
        return out

    def emotions(self, vocals: Path, segs: list, voices: list, on) -> tuple[list, list]:
        """Her cümleye Ahenk duygusu (EMOTIONS) ve güven puanı. Metin modeli duyguyu, orijinal ses ne kadar
        güçlü söylendiğini verir: aynı cümle bağırarak söylendiyse 'kızgın', çok alçak ve sakinse 'sakin' olur."""
        if DEMO:
            time.sleep(0.3)
            cyc = ("neutral", "cheerful", "angry", "neutral", "sad", "surprised", "calm", "empathetic")
            return [cyc[i % len(cyc)] for i in range(len(segs))], [0.8] * len(segs)
        if not segs:
            return [], []
        on("calisiyor", 5, "Cümlelerin duygusu okunuyor")
        P, labels = self._emo_text([s.get("en", "") for s in segs], on)
        on("calisiyor", 50, "Ses şiddeti ve perdesi ölçülüyor")
        x, sr = read_wav(vocals)
        feats = self._emo_audio(x.mean(axis=1), sr, segs)
        # uyarılma (arousal): aynı cinsiyetteki cümlelere göre ne kadar yüksek sesle / tiz söylendi (z puanı)
        arousal = [0.0] * len(segs)
        for g in set(voices):
            idx = [i for i, f in enumerate(feats) if f is not None and voices[i] == g]
            if len(idx) < 4:
                continue
            db = np.array([feats[i][0] for i in idx])
            st = np.array([feats[i][1] if feats[i][1] is not None else np.nan for i in idx])
            zd = (db - np.median(db)) / max(3.0, float(np.std(db)))
            sv = st[~np.isnan(st)]
            zs = (st - np.median(sv)) / max(1.5, float(np.std(sv))) if len(sv) >= 4 else np.zeros_like(st)
            for j, i in enumerate(idx):
                arousal[i] = float(0.6 * zd[j] + 0.4 * (0.0 if np.isnan(zs[j]) else zs[j]))
        out, conf = [], []
        for i, s in enumerate(segs):
            p = {n: float(P[i][j]) for j, n in enumerate(labels)}
            en = s.get("en", "")
            a = arousal[i]
            top = max(p, key=p.get)
            lab, c = EMO_MAP.get(top, "neutral"), p[top]
            if c < EMO_MIN:
                lab = "neutral"
            if lab == "fear":                        # Ahenk'te korku yok: heyecanlıysa şaşkın, değilse üzgün
                lab = "surprised" if a > 0.5 else "sad"
            if lab == "angry" and c < 0.6 and a < -0.5:
                lab = "neutral"                      # alçak sesle söylenen hafif kızgınlık: düz
            if LAUGH_RE.search(en):
                lab, c = "laughing", max(c, 0.6)
            elif COMFORT_RE.search(en) and lab in ("neutral", "sad"):
                lab, c = "empathetic", max(c, 0.5)   # teselli / yatıştırma
            elif lab == "neutral":                   # metin düz: sesin kendisine bak
                neg = p.get("anger", 0) + p.get("disgust", 0)
                if a > 1.5 and max(neg, p.get("joy", 0), p.get("surprise", 0)) >= 0.15:
                    lab = "angry" if neg >= max(p.get("joy", 0), p.get("surprise", 0)) else \
                        "cheerful" if p.get("joy", 0) >= p.get("surprise", 0) else "surprised"
                elif a < -1.3 and len(en.split()) >= 3:
                    lab = "calm"
            out.append(lab)
            conf.append(round(c, 3))
            on("calisiyor", 50 + (i + 1) / len(segs) * 50, f"{i + 1}/{len(segs)} cümle")
        return out, conf

    # ---- 4c) dublaj · Ahenk Cep / Ahenk (duygulu; çiftte erkekleri Antalia-2 Mini okur)
    def _ahenk_files(self, on) -> Path:
        if DEMO:
            return AHENK_DIR
        from huggingface_hub import snapshot_download
        with download_watch(on, "Ahenk", [AHENK_DIR], 145, (AHENK_DIR / "lora_weights.safetensors").exists()):
            snapshot_download(AHENK_REPO, allow_patterns=["lora_*", "*.py", "instructions_tr.json"],
                              local_dir=str(AHENK_DIR))
        return AHENK_DIR

    def _cep_model(self, on):
        if "ahenkcep" not in self._tts:
            import importlib.util
            import torch
            from huggingface_hub import snapshot_download
            if str(BASE) not in sys.path:                # trnorm proje klasöründe
                sys.path.insert(0, str(BASE))
            with download_watch(on, "Ahenk Cep", [CEP_DIR], 35, (CEP_DIR / "student.pt").exists()):
                snapshot_download(CEP_REPO, allow_patterns=["student.pt", "decoder.pt", "ahenk_cep.py"],
                                  local_dir=str(CEP_DIR))
            spec = importlib.util.spec_from_file_location("ahenk_cep", CEP_DIR / "ahenk_cep.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            cep = mod.AhenkCep(str(CEP_DIR / "student.pt"), str(CEP_DIR / "decoder.pt"), device=dev)
            cep.engine._batch_size = 4          # EMA'daki gibi: toplu iş boyutu ölçümü dolu kartta çok yavaşlar
            self._tts["ahenkcep"] = (cep, tuple(mod.STYLES))
        return self._tts["ahenkcep"]

    def dub_ahenk(self, segs: list, voc_path: Path, emotions: list, chunk_dur: float, target_rms: float, on,
                  seed: int = 0, work: Path | None = None, steps: int = AHENK_STEPS,
                  big: bool = False, voices: list | None = None) -> tuple[np.ndarray, list]:
        """Cümleler Ahenk Cep (big=False) ya da Ahenk (big=True) ile, algılanan duyguyla okunur. voices verilirse
        (kadın / erkek çifti) erkeklerin cümlelerini Antalia-2 Mini okur. Her cümlenin ses gücü özgününe eşlenir
        (bağırma yüksek, fısıltı alçak)."""
        x, sr = read_wav(voc_path)
        mono = x.mean(axis=1) if x.ndim > 1 else x
        n = len(segs)
        name = "Ahenk" if big else "Ahenk Cep"
        male = [bool(voices) and i < len(voices) and voices[i] == "erkek" for i in range(n)]
        fem = not all(male)
        if not DEMO:
            keep = ("vox",) if big else ("ahenkcep",)
            self.make_room(7200 if big and fem else 2500, keep=keep + (("erkek",) if any(male) else ()))
        with self.lock:
            if any(male) and "erkek" not in self._tts and not DEMO:
                with download_watch(on, "Antalia-2 Mini", [MODELS / "hf"], 350, self._tts_seen("erkek")):
                    self._tts_model("erkek")
                (MODELS / ".tts_erkek").write_text("ok", encoding="utf-8")
            cep = None
            if fem and not DEMO:
                if big:
                    self._vox.ensure(on, lora=self._ahenk_files(on))
                else:
                    cep = self._cep_model(on)
            on("calisiyor", 0, f"0/{n} cümle")
            tmp = (work or voc_path.parent) / "_ahenk"
            tmp.mkdir(exist_ok=True)
            track = np.zeros(int((chunk_dur + 6) * DUB_SR), dtype=np.float32)
            cursor, timings = 0.0, []
            try:
                for i, s in enumerate(segs):
                    text = (s.get("tr") or "").strip()
                    if not text:
                        timings.append(None)
                        continue
                    nxt = segs[i + 1]["start"] if i + 1 < n else chunk_dur + 1.0
                    start = max(float(s["start"]), cursor)
                    avail = max(0.4, nxt - start - 0.05)
                    own = mono[max(0, int((s["start"] - 0.06) * sr)): max(0, min(len(mono), int((s["end"] + 0.12) * sr)))]
                    emo = emotions[i] if i < len(emotions) else "neutral"
                    sd = int(seed) * 1000 + i
                    if male[i] or DEMO:                  # erkek (çift): Antalia · demo: sahte ses
                        g = "erkek" if male[i] else "kadin"
                        if DEMO and not male[i]:
                            time.sleep(0.004 * steps * (8 if big else 1))
                        a = trim_silence(self.say(g, text, 1.0, sd), DUB_SR)
                        if len(a) / DUB_SR > avail * 1.03:
                            a = trim_silence(self.say(g, text, min(len(a) / DUB_SR / avail, 1.35), sd), DUB_SR)
                    elif big:
                        ow = tmp / f"o{i}.wav"
                        self._vox.run({"ahenk": True, "text": text, "emotion": "" if emo == "neutral" else emo,
                                       "out": str(ow), "cfg": VOX_CFG, "steps": int(steps), "seed": sd})
                        y, ysr = read_wav(ow)
                        a = trim_silence(resample(y.mean(axis=1) if y.ndim > 1 else y, ysr, DUB_SR), DUB_SR)
                        if len(a) / DUB_SR > avail * 1.03:
                            a = atempo(a, DUB_SR, min(len(a) / DUB_SR / avail, 1.35), tmp)
                    else:
                        model, styles = cep
                        style = {"laughing": "cheerful"}.get(emo, emo)     # Cep'te gülme eğitilmedi
                        style = style if style in styles else "neutral"

                        def cep_say(speed):
                            au, asr = model.say(text, style=style, steps=int(steps), speed=float(speed), seed=sd)
                            au = np.asarray(au, dtype=np.float32).reshape(-1)
                            return trim_silence(resample(au, int(asr), DUB_SR) if int(asr) != DUB_SR else au, DUB_SR)
                        a = cep_say(1.0)
                        if len(a) / DUB_SR > avail * 1.03:
                            a = cep_say(min(len(a) / DUB_SR / avail, 1.35))
                    a = np.asarray(a, dtype=np.float32)
                    ro, rd = active_rms(own, sr), active_rms(a, DUB_SR)
                    if ro > 0 and rd > 0:
                        a = a * float(np.clip(ro / rd, 0.2, 5.0))
                    d = len(a) / DUB_SR
                    i0 = int(start * DUB_SR)
                    if i0 + len(a) > len(track):
                        track = np.concatenate([track, np.zeros(i0 + len(a) - len(track), dtype=np.float32)])
                    track[i0:i0 + len(a)] += a
                    timings.append([round(start, 3), round(start + d, 3)])
                    cursor = start + d + 0.05
                    on("calisiyor", (i + 1) / max(1, n) * 100, f"{i + 1}/{n} cümle · {name}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            cur = active_rms(track, DUB_SR)
            if target_rms > 0 and cur > 0:
                track *= float(np.clip(target_rms / cur, 0.25, 4.0))
            peak = float(np.abs(track).max() or 1.0)
            if peak > 0.98:
                track *= 0.98 / peak
            return track, timings

    def vox_installed(self) -> bool:
        return DEMO or vox_ready()

    def close(self):
        self._llama.stop()
        self._vox.stop()
