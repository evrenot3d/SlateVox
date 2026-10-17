"""VoxCPM2 yardımcı süreci (kendi Python ortamında çalışır: .venv-vox).

stdin  : her satır bir istek
         {"text": "...", "out": "...wav",
          "prompt_wav": "...wav", "prompt_text": "...",   (devam ettirme: cümlenin kendi sesi + İngilizce metni)
          "ref_wav": "...wav",                             (ses rengi örneği)
          "cfg": 2.0, "steps": 10, "seed": 123}
         Ahenk (Türkçe LoRA, tek kadın sesi):  {"ahenk": true, "text": "...", "emotion": "angry", "out": "...wav",
          "steps": 16, "seed": 123}   (ses örneği verilmez: ses LoRA'nın içinde; duygu talimatla verilir)
stdout : {"ready": true, "sr": 48000} bir kez, sonra her istek için {"ok": true, "dur": sn} ya da {"error": "..."}

Devam ettirme kipi (prompt) cümlenin vurgusunu, hızını ve duygusunu taşır; ayrı ses rengi örneği (ref) sesin
kendisini sabitler. İkisi birlikte VoxCPM2'nin en güçlü kopyalama kipidir.

DMS_VOX_LORA ortam değişkeni Ahenk klasörünü gösteriyorsa (lora_config.json, lora_weights.safetensors,
instruction_pick.py, pause_clean.py) LoRA yüklenir; Ahenk isteklerinde açılır, diğerlerinde kapatılır
(aynı süreç VoxCPM2 ses kopyalamayı da yapmaya devam eder).
"""
import json
import os
import sys
import traceback
from pathlib import Path

PROTO = sys.stdout          # protokol kanalı; başka her çıktı günlüğe gider
sys.stdout = sys.stderr

import faulthandler
faulthandler.enable(file=sys.stderr, all_threads=True)    # çökmede (yerel hata) iz günlüğe yazılsın

import numpy as np
import soundfile as sf
import torch

# torchaudio.load yeni sürümlerde torchcodec + FFmpeg DLL'leri ister; WAV'ları soundfile ile okuyalım
try:
    import torchaudio

    def _load(uri, *a, **k):
        x, sr = sf.read(str(uri), dtype="float32", always_2d=True)
        return torch.from_numpy(np.ascontiguousarray(x.T)), sr

    torchaudio.load = _load
except Exception:  # noqa: BLE001
    pass


def _ahenk_helpers(lora_dir: str):
    """Ahenk metin hazırlığı: trnorm (okunuş biçimi) → instruction_pick (duygu talimatı, dili kendisi seçer).
    Dönen işlev (metin, duygu) → modele giden "(talimat)metin"; .clean: pause_clean (yoksa None)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))      # trnorm proje klasöründe
    sys.path.insert(0, lora_dir)
    from trnorm import normalize
    from instruction_pick import compose, pick

    def prep(text: str, emotion: str) -> str:
        t = normalize(text).strip() or text
        try:
            return compose(*pick(emotion, t)[:2])
        except ValueError:                           # ör. metin "(" ile başlıyor: talimatsız oku
            return t.lstrip("([").strip() or t

    try:
        from pause_clean import clean
        prep.clean = clean
    except Exception:  # noqa: BLE001 — scipy yoksa nefes temizliği atlanır
        traceback.print_exc()
        prep.clean = None
    return prep


def main():
    from voxcpm import VoxCPM
    try:
        import importlib.metadata as md
        print(f"python {sys.version.split()[0]} · torch {torch.__version__} · cuda {torch.version.cuda} · "
              f"voxcpm {md.version('voxcpm')} · transformers {md.version('transformers')} · numpy {np.__version__}",
              flush=True)
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            print(f"GPU {torch.cuda.get_device_name(0)} · boş {free / 2**20:.0f} / {total / 2**20:.0f} MB", flush=True)
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tot", ctypes.c_ulonglong),
                            ("free", ctypes.c_ulonglong), ("ptot", ctypes.c_ulonglong), ("pfree", ctypes.c_ulonglong),
                            ("vtot", ctypes.c_ulonglong), ("vfree", ctypes.c_ulonglong), ("x", ctypes.c_ulonglong)]
            m = MS()
            m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            print(f"RAM boş {m.free / 2**30:.1f} / {m.tot / 2**30:.1f} GB · sanal bellek boş {m.pfree / 2**30:.1f} GB",
                  flush=True)
    except Exception:  # noqa: BLE001
        pass
    cache = os.environ.get("DMS_VOX_CACHE") or None
    lora_dir = os.environ.get("DMS_VOX_LORA") or None
    lora_kw, ahenk = {}, None
    if lora_dir:                                     # Ahenk: VoxCPM2'ye Türkçe LoRA (+ yanındaki yardımcı dosyalar)
        try:
            from voxcpm.model.voxcpm2 import LoRAConfig
        except ImportError:
            from voxcpm.model.voxcpm import LoRAConfig
        cfg = json.loads(Path(lora_dir, "lora_config.json").read_text(encoding="utf-8"))["lora_config"]
        lora_kw = dict(lora_config=LoRAConfig(**cfg), lora_weights_path=lora_dir)
        ahenk = _ahenk_helpers(lora_dir)
    # optimize=False: torch.compile Windows'ta Triton ister; denoiser kapalı (ses zaten ayrılmış geliyor)
    model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False, optimize=False, cache_dir=cache, **lora_kw)
    sr = int(getattr(model.tts_model, "sample_rate", 48000))
    lora_on = bool(lora_dir)
    if lora_dir:
        n = sum(1 for k, _ in model.tts_model.named_parameters() if "lora_" in k)
        print(f"Ahenk LoRA yüklendi ({n} LoRA parametresi)", flush=True)
    print(f"VoxCPM2 hazır (cuda={torch.cuda.is_available()}, sr={sr}, ahenk={bool(lora_dir)})", flush=True)
    PROTO.write(json.dumps({"ready": True, "sr": sr, "ahenk": bool(lora_dir)}) + "\n")
    PROTO.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            want = bool(req.get("ahenk"))
            if want and not lora_dir:
                raise RuntimeError("Ahenk yüklenmeden başlatıldı")
            if lora_dir and want != lora_on:         # LoRA yalnız Ahenk cümlelerinde açık
                model.set_lora_enabled(want)
                lora_on = want
            text = req["text"]
            if want:
                text = ahenk(text, req.get("emotion") or "")
                print(f"[ahenk] {text}", flush=True)
            kw = dict(text=text, cfg_value=float(req.get("cfg", 2.0)),
                      inference_timesteps=int(req.get("steps", 10)))
            if want:
                kw["normalize"] = False              # metin trnorm'dan geçti (model onunla eğitildi)
            if req.get("prompt_wav") and req.get("prompt_text") and not want:
                kw.update(prompt_wav_path=req["prompt_wav"], prompt_text=req["prompt_text"])
            if req.get("ref_wav") and not want:
                kw["reference_wav_path"] = req["ref_wav"]
            if req.get("seed") is not None:
                kw["seed"] = int(req["seed"])
            try:
                wav = model.generate(**kw)
            except TypeError:                      # eski sürümde seed yoksa
                kw.pop("seed", None)
                wav = model.generate(**kw)
            except Exception:                      # devam ettirme olmazsa yalnız ses rengiyle dene
                if "prompt_wav_path" not in kw:
                    raise
                traceback.print_exc()
                kw.pop("prompt_wav_path", None)
                kw.pop("prompt_text", None)
                kw.setdefault("reference_wav_path", req.get("prompt_wav"))
                wav = model.generate(**kw)
            wav = np.asarray(wav, dtype=np.float32).reshape(-1)
            if want and getattr(ahenk, "clean", None):
                try:                                 # duraklardaki nefes seslerini kısar (Ahenk'in kendi aracı)
                    wav = np.asarray(ahenk.clean(wav, sr)[0], dtype=np.float32).reshape(-1)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
            sf.write(req["out"], wav, sr, subtype="PCM_16")
            res = {"ok": True, "dur": round(len(wav) / sr, 3)}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            res = {"error": str(e)[:500]}
        PROTO.write(json.dumps(res) + "\n")
        PROTO.flush()


if __name__ == "__main__":
    main()
