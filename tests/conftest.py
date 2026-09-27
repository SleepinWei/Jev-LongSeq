import os
from pathlib import Path

local_browsers = Path(__file__).resolve().parents[1] / ".browsers"
if local_browsers.exists():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(local_browsers))
