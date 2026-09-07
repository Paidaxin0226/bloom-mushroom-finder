from __future__ import annotations

import sys

import av
import PIL
import torch
import ultralytics
import yaml


print(f"Python: {sys.version.split()[0]}")
print(f"Pillow: {PIL.__version__}")
print(f"PyYAML: {yaml.__version__}")
print(f"PyAV: {av.__version__}")
print(f"Ultralytics: {ultralytics.__version__}")
print(f"PyTorch: {torch.__version__}")
print(f"CUDA available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"GPU: {torch.cuda.get_device_name(0)}")
