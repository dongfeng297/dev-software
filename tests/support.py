"""Test helpers that avoid `tempfile` directories on Windows sandboxes.

The DSH/Windows sandbox used during development rejects writes inside
directories created with mode 0o700 (which is what `tempfile.TemporaryDirectory`
uses).  Test-only directories are therefore created with the default mode under
the git-ignored `.backnest/test-tmp` directory.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / ".backnest" / "test-tmp"


class WorkspaceTemporaryDirectory:
    def __init__(self, prefix: str = "test-"):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.path = TEST_ROOT / (prefix + uuid.uuid4().hex)
        self.path.mkdir()
        self.name = str(self.path)

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *_exc) -> None:
        self.cleanup()
