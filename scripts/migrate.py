from __future__ import annotations

import os
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()

key = "TEST_DATABASE_URL" if "--test" in sys.argv else "DATABASE_URL"
url = os.environ.get(key)
if not url:
    raise SystemExit(f"{key} is not configured")
if key == "TEST_DATABASE_URL" and "_test" not in url:
    raise SystemExit("refusing to migrate a test target whose database name lacks _test")

command = [sys.executable, "-m", "alembic", "-x", f"db_url={url}", "upgrade", "head"]
raise SystemExit(subprocess.call(command))