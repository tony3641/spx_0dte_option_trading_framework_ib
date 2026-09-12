"""Repository-relative resource locations.

The single place that derives paths from the repository root. Every other
module imports from here rather than computing ``Path(__file__).parent``
itself, so that relocating a module cannot silently change which file it
reads.

Resolution is anchored to this file's location, never to the current working
directory.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

CONFIG_DIR = REPO_ROOT / "config"
STATIC_DIR = REPO_ROOT / "static"
DOCS_DIR = REPO_ROOT / "docs"
EXPERIMENTS_DIR = DOCS_DIR / "experiments"

# An explicit DOTENV_PATH override keeps working, matching the behaviour of the
# former config.py. If it is set to a relative path it stays cwd-relative.
ENV_PATH = Path(os.getenv("DOTENV_PATH", REPO_ROOT / ".env"))
