"""GPU varsa torch + torchvision'ı birbiriyle uyumlu CUDA sürümüne getirir."""
import re
import subprocess
import sys
from importlib.metadata import requires, version


def ok() -> bool:
    code = "import torch,torchvision,sys;sys.exit(0 if torch.cuda.is_available() else 1)"
    return subprocess.run([sys.executable, "-c", code]).returncode == 0


def main() -> int:
    if ok():
        print("torch/torchvision CUDA zaten uyumlu")
        return 0
    vv = version("torchvision").split("+")[0]
    tv = None
    for r in requires("torchvision") or []:
        m = re.match(r"^torch\s*==\s*([0-9.]+)", r)
        if m:
            tv = m.group(1)
    pkgs = [f"torch=={tv}" if tv else "torch", f"torchvision=={vv}"]
    print("hedef:", pkgs, flush=True)
    for idx in ("cu128", "cu129", "cu130"):
        cmd = [sys.executable, "-m", "pip", "install", "--progress-bar", "off", "--force-reinstall", "--no-deps",
               *pkgs, "--index-url", f"https://download.pytorch.org/whl/{idx}"]
        print(">", " ".join(cmd), flush=True)
        if subprocess.run(cmd).returncode == 0 and ok():
            print(f"tamam ({idx})")
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
