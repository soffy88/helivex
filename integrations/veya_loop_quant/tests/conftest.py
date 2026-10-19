"""使 `python -m pytest integrations/veya_loop_quant/tests` 可直接运行。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # → integrations/veya_loop_quant/
