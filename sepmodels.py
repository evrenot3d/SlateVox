"""Ek (yavaş) ses ayırma modelleri — konuşma / arka plan.

    bandit2  BandIt v2 · DnR Multi   (Watcharasupat vd., 48 kHz, konuşma · müzik · efekt)
    tiger    TIGER-DnR               (Xu, Li vd., ICLR 2025, 44.1 kHz, diyalog · efekt · müzik)

Model kodları ilk kullanımda özgün depolardan (sabit sürüm) modeller/sepcode altına indirilir; torchaudio ve
pytorch-lightning gerektirmesin diye birkaç satır değiştirilir. Ağırlıklar Hugging Face'ten indirilir.
Her model konuşmayı verir; arka plan = karışım − konuşma.
"""
from __future__ import annotations

import importlib
import sys
import urllib.request
from pathlib import Path

import numpy as np

MSST_SHA = "84b1eac0887756b4f1a9d7a1ff49105939749ed2"     # ZFTurbo/Music-Source-Separation-Training
TIGER_SHA = "9f18d4a10a7137e1ce8052cfb62215179f1287b6"    # JusperLee/TIGER

INFO = {
    "bandit2": {"name": "BandIt v2 · DnR Multi", "sr": 48000},
    "tiger": {"name": "TIGER-DnR", "sr": 44100},
}

_TA_SHIM = '''"""torchaudio.transforms.Spectrogram / InverseSpectrogram yerine (aynı hesap, aynı 'window' tamponu)."""
import torch
from torch import nn


class _Spec(nn.Module):
    def __init__(self, n_fft=400, win_length=None, hop_length=None, pad=0, window_fn=torch.hann_window, power=None,
                 normalized=False, wkwargs=None, center=True, pad_mode="reflect", onesided=True, **_):
        super().__init__()
        self.n_fft = n_fft
        self.win_length = win_length if win_length is not None else n_fft
        self.hop_length = hop_length if hop_length is not None else self.win_length // 2
        w = window_fn(self.win_length) if wkwargs is None else window_fn(self.win_length, **wkwargs)
        self.register_buffer("window", w)
        self.normalized, self.center, self.pad_mode, self.onesided = bool(normalized), center, pad_mode, onesided


class Spectrogram(_Spec):
    def forward(self, x):
        shape = x.size()
        x = x.reshape(-1, shape[-1])
        s = torch.stft(x, n_fft=self.n_fft, hop_length=self.hop_length, win_length=self.win_length,
                       window=self.window, center=self.center, pad_mode=self.pad_mode, normalized=False,
                       onesided=self.onesided, return_complex=True)
        s = s.reshape(shape[:-1] + s.shape[-2:])
        if self.normalized:
            s = s / self.window.pow(2.0).sum().sqrt()
        return s


class InverseSpectrogram(_Spec):
    def forward(self, s, length=None):
        if self.normalized:
            s = s * self.window.pow(2.0).sum().sqrt()
        shape = s.size()
        s = s.reshape(-1, shape[-2], shape[-1])
        w = torch.istft(s, n_fft=self.n_fft, hop_length=self.hop_length, win_length=self.win_length,
                        window=self.window, center=self.center, normalized=False, onesided=self.onesided,
                        length=length, return_complex=False)
        return w.reshape(shape[:-2] + w.shape[-1:])


class transforms:
    Spectrogram = Spectrogram
    InverseSpectrogram = InverseSpectrogram


class functional:
    @staticmethod
    def melscale_fbanks(*a, **k):
        raise NotImplementedError("mel bant türü bu uygulamada kullanılmıyor")
'''

CODE = {
    "bandit2": {
        "pkg": "dms_bandit2",
        "base": f"https://raw.githubusercontent.com/ZFTurbo/Music-Source-Separation-Training/{MSST_SHA}/models/bandit_v2/",
        "files": ["bandit.py", "bandsplit.py", "film.py", "maskestim.py", "tfmodel.py", "utils.py"],
        "patch": {
            "bandit.py": [("import torchaudio as ta\n", "from . import _ta as ta\n"),
                          ("import pytorch_lightning as pl\n", ""),
                          ("(pl.LightningModule)", "(nn.Module)")],
            "utils.py": [("from torchaudio import functional as taF\n", "from ._ta import functional as taF\n")],
        },
        "extra": {"_ta.py": _TA_SHIM},
    },
    "tiger": {
        "pkg": "dms_tiger",
        "base": f"https://raw.githubusercontent.com/JusperLee/TIGER/{TIGER_SHA}/look2hear/",
        "files": ["models/tiger_dnr.py", "models/base_model.py", "layers/activations.py", "layers/normalizations.py"],
        "patch": {"tiger_dnr.py": [("from ..layers import activations, normalizations",
                                    "from . import activations, normalizations")]},
        "extra": {},
    },
}


def ensure_code(name: str, root: Path) -> str:
    """Model kodunu indirip uyarlar; içe aktarılacak paket adını döndürür."""
    spec = CODE[name]
    pdir = root / spec["pkg"]
    done = pdir / ".tamam"
    if not done.exists():
        pdir.mkdir(parents=True, exist_ok=True)
        for rel in spec["files"]:
            with urllib.request.urlopen(spec["base"] + rel, timeout=60) as r:
                txt = r.read().decode("utf-8")
            fn = rel.rsplit("/", 1)[-1]
            for old, new in spec["patch"].get(fn, []):
                if old not in txt:
                    raise RuntimeError(f"{name}: {fn} beklenen biçimde değil (kod sürümü değişmiş)")
                txt = txt.replace(old, new)
            (pdir / fn).write_text(txt, encoding="utf-8")
        for fn, txt in spec["extra"].items():
            (pdir / fn).write_text(txt, encoding="utf-8")
        (pdir / "__init__.py").write_text("", encoding="utf-8")
        done.write_text("ok", encoding="utf-8")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return spec["pkg"]


# ------------------------------------------------------------------ yükleme
def _load_flexible(model, sd: dict) -> None:
    """Ağırlık adlarındaki 'model.', 'module.', '_orig_mod.' farklarını tolere ederek sıkı yükleme."""
    norm = lambda k: k.replace("_orig_mod.", "")                          # noqa: E731
    for p in ("model.", "module."):
        if all(k.startswith(p) for k in sd):
            sd = {k[len(p):]: v for k, v in sd.items()}
    target = {norm(k): k for k in model.state_dict()}
    mapped = {target[norm(k)]: v for k, v in sd.items() if norm(k) in target}
    missing = set(model.state_dict()) - set(mapped)
    if missing:
        raise RuntimeError(f"ağırlıklar uyuşmadı ({len(missing)} eksik, ör. {sorted(missing)[:3]})")
    model.load_state_dict(mapped, strict=True)


def load(name: str, root: Path, device, on=None):
    import torch
    pkg = ensure_code(name, root)
    if name == "bandit2":
        from huggingface_hub import hf_hub_download
        try:
            ck = hf_hub_download("zerochocobo/bandit-v2", "checkpoint-multi.slim.pt")
        except Exception:
            ck = hf_hub_download("jsoncx/bandit-v2", "checkpoint-multi.ckpt")
        sd = torch.load(ck, map_location="cpu", weights_only=False)
        for key in ("state_dict", "model_state_dict", "model"):
            if isinstance(sd, dict) and isinstance(sd.get(key), dict):
                sd = sd[key]
        stems = []
        for k in sd:
            if ".mask_estim." in "." + k:
                s = k.split("mask_estim.", 1)[1].split(".", 1)[0]
                if s not in stems:
                    stems.append(s)
        Bandit = importlib.import_module(f"{pkg}.bandit").Bandit
        kw = dict(in_channels=1, stems=stems or ["speech", "music", "sfx"], band_type="musical", n_bands=64,
                  normalize_channel_independently=False, treat_channel_as_feature=True, n_sqm_modules=8,
                  emb_dim=128, rnn_dim=256, bidirectional=True, rnn_type="GRU", mlp_dim=512,
                  hidden_activation="Tanh", hidden_activation_kwargs=None, complex_mask=True, use_freq_weights=True,
                  n_fft=2048, win_length=2048, hop_length=512, window_fn="hann_window", wkwargs=None, power=None,
                  center=True, normalized=True, pad_mode="reflect", onesided=True)
        err = None
        for fs in (48000, 44100):
            try:
                model = Bandit(fs=fs, **kw)
                _load_flexible(model, sd)
                model.dms_speech = next((i for i, s in enumerate(model.stems) if "speech" in s or "dialog" in s), 0)
                return model.to(device).eval()
            except Exception as e:  # noqa: BLE001
                err = e
        raise RuntimeError(f"BandIt v2 yüklenemedi: {err}")
    if name == "tiger":
        TIGERDNR = importlib.import_module(f"{pkg}.tiger_dnr").TIGERDNR
        model = TIGERDNR.from_pretrained("JusperLee/TIGER-DnR")
        return model.to(device).eval()
    raise ValueError(name)


# ------------------------------------------------------------------ ayırma
def _fade_window(n: int, fade: int):
    import torch
    w = torch.ones(n)
    w[:fade] = torch.linspace(0, 1, fade)
    w[-fade:] = torch.linspace(1, 0, fade)
    return w


def separate(name: str, model, audio, sr: int, device, on=None):
    """audio: (2, T) float32 (sr Hz) → konuşma (2, T) aynı örnekleme hızında."""
    import torch
    if name == "tiger":
        out = []
        for c in range(audio.shape[0]):
            x = torch.from_numpy(np.ascontiguousarray(audio[c])).float().to(device).view(1, 1, -1)
            with torch.no_grad():
                d = model.wav_chunk_inference(model.dialog, x, batch_size=2)[2]     # (1, T)
            out.append(d.reshape(-1)[: audio.shape[1]].float().cpu().numpy())
            if on:
                on((c + 1) / audio.shape[0])
        return np.stack(out)

    if name == "bandit2":
        C = 384000                        # 8 sn (48 kHz)
        step = C // 4
        fade = C // 10
        T = audio.shape[1]
        x = torch.from_numpy(np.ascontiguousarray(audio)).float()
        border = C - step
        x = torch.nn.functional.pad(x, (border, border + C))
        win = _fade_window(C, fade).to(device)
        res = torch.zeros(2, x.shape[1], device=device)
        cnt = torch.zeros(x.shape[1], device=device)
        starts = list(range(0, T + border, step))
        B = 1                             # 8 GB kartta bellek taşmasın (her parça 2 kanal)
        for i in range(0, len(starts), B):
            idx = starts[i:i + B]
            batch = torch.stack([x[:, s:s + C] for s in idx]).to(device)            # (b, 2, C)
            with torch.no_grad():
                y = model(batch)                                                    # (b, stems, 2, C)
            sp = y[:, model.dms_speech].float()
            for j, s in enumerate(idx):
                w = win.clone()
                if s == 0:
                    w[:fade] = 1
                res[:, s:s + C] += sp[j] * w
                cnt[s:s + C] += w
            if on:
                on(min(1.0, (i + len(idx)) / len(starts)))
        out = (res / cnt.clamp_min(1e-6))[:, border:border + T]
        return out.cpu().numpy()
    raise ValueError(name)
