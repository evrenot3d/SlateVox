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
