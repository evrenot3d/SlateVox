"""VoxCPM2 yardımcı süreci (kendi Python ortamında çalışır: .venv-vox).

stdin  : her satır bir istek
         {"text": "...", "out": "...wav",
          "prompt_wav": "...wav", "prompt_text": "...",   (devam ettirme: cümlenin kendi sesi + İngilizce metni)
          "ref_wav": "...wav",                             (ses rengi örneği)
          "cfg": 2.0, "steps": 10, "seed": 123}
stdout : {"ready": true, "sr": 48000} bir kez, sonra her istek için {"ok": true, "dur": sn} ya da {"error": "..."}

Devam ettirme kipi (prompt) cümlenin vurgusunu, hızını ve duygusunu taşır; ayrı ses rengi örneği (ref) sesin
kendisini sabitler. İkisi birlikte VoxCPM2'nin en güçlü kopyalama kipidir.
"""
import json
import os
import sys
import traceback

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
    # optimize=False: torch.compile Windows'ta Triton ister; denoiser kapalı (ses zaten ayrılmış geliyor)
    model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False, optimize=False, cache_dir=cache)
    sr = int(getattr(model.tts_model, "sample_rate", 48000))
    print(f"VoxCPM2 hazır (cuda={torch.cuda.is_available()}, sr={sr})", flush=True)
    PROTO.write(json.dumps({"ready": True, "sr": sr}) + "\n")
    PROTO.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            kw = dict(text=req["text"], cfg_value=float(req.get("cfg", 2.0)),
                      inference_timesteps=int(req.get("steps", 10)))
            if req.get("prompt_wav") and req.get("prompt_text"):
                kw.update(prompt_wav_path=req["prompt_wav"], prompt_text=req["prompt_text"])
            if req.get("ref_wav"):
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
            sf.write(req["out"], wav, sr, subtype="PCM_16")
            res = {"ok": True, "dur": round(len(wav) / sr, 3)}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            res = {"error": str(e)[:500]}
        PROTO.write(json.dumps(res) + "\n")
        PROTO.flush()


if __name__ == "__main__":
    main()
