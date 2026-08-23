from __future__ import annotations

import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = SERVICE_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

AKASHIC_ROOT = SERVICE_ROOT.parents[1] / "akashic-agent"
for path in (AKASHIC_ROOT, AKASHIC_ROOT / "sdk" / "python" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
