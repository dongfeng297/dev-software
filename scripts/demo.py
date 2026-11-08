"""Create disposable sample files and demonstrate real backup and restore.

Uses the C++17 executable when a compiler is available, otherwise the
compatible Python engine.  Both write the same BACKNEST 1 snapshot format.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

from build import build, ROOT

sys.path.insert(0, str(ROOT))
from backnest import engine as python_engine


def fingerprint(directory):
    return {str(path.relative_to(directory)): ("directory" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest())
            for path in directory.rglob("*")}


def main():
    try:
        binary = build()
        engine_name = "C++17"
    except RuntimeError as error:
        binary = None
        engine_name = "Python"
        print("C++ engine unavailable:", error)

    run = ROOT / ".backnest" / ("demo-" + uuid.uuid4().hex[:10])
    source = run / "source"
    (source / "中文目录" / "空目录").mkdir(parents=True)
    (source / "README.txt").write_text("BackNest basic demo\nThese are generated sample files, not user documents.\n", encoding="utf-8")
    (source / "中文目录" / "说明.txt").write_text("基础要求：目录树备份与还原。\n", encoding="utf-8")
    (source / "empty.txt").touch()
    (source / "bytes.bin").write_bytes(bytes(range(256)) * 1024)
    original = fingerprint(source)

    if binary:
        def execute(*args):
            process = subprocess.run([str(binary)] + list(map(str, args)), capture_output=True, text=True,
                                     encoding="utf-8", check=False)
            events = [json.loads(line) for line in process.stdout.splitlines()]
            if process.returncode or not events or events[-1]["event"] != "result":
                raise RuntimeError(process.stdout + process.stderr)
            return events[-1]

        backup = execute("backup", source, run / "backups")
        restored = execute("restore", backup["path"], run / "restored")
    else:
        backup = python_engine.backup(source, run / "backups")
        restored = python_engine.restore(backup["path"], run / "restored")

    assert original == fingerprint(run / "restored"), "Restored bytes or directory structure do not match"
    assert original == fingerprint(source), "Source was modified"
    report = {
        "result": "PASSED",
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "engine": engine_name,
        "source": str(source),
        "snapshot": backup["path"],
        "restored": restored["path"],
        "files": backup["files"],
        "directories": backup["directories"],
        "bytes": backup["bytes"],
        "checks": ["all relative paths match", "all file SHA-256 hashes match", "empty directories preserved", "source unchanged"],
    }
    run.joinpath("report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    artifact = ROOT / "artifacts" / "basic-demo-report.json"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
