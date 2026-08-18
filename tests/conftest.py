import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "instancer-core"))
sys.path.insert(0, str(REPO / "proxy-core"))
