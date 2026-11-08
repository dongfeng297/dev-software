"""Build the dependency-free C++17 engine with the installed native compiler."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "build" / ("backnest.exe" if os.name == "nt" else "backnest")


def build(force=False):
    source = ROOT / "core" / "main.cpp"
    if not force and BINARY.exists() and BINARY.stat().st_mtime >= max(source.stat().st_mtime, (ROOT / "core" / "backend_interfaces.hpp").stat().st_mtime):
        return BINARY
    BINARY.parent.mkdir(exist_ok=True)
    if os.name == "nt":
        compiler = shutil.which("cl")
        if compiler:
            command = [compiler, "/nologo", "/std:c++17", "/EHsc", "/utf-8", "/W4", str(source),
                       "/Fe:" + str(BINARY), "/Fo:" + str(BINARY.parent / "backnest.obj")]
        else:
            compiler = shutil.which("g++") or shutil.which("clang++")
            if not compiler:
                raise RuntimeError("Run from a Visual Studio Developer PowerShell with the C++ build tools installed, "
                                   "or put g++/clang++ on PATH.")
            command = [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Wpedantic", str(source), "-o", str(BINARY)]
    else:
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            raise RuntimeError("Install a C++17 compiler (Xcode Command Line Tools on macOS).")
        command = [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Wpedantic", str(source), "-o", str(BINARY)]
    subprocess.run(command, check=True, cwd=ROOT)
    return BINARY


if __name__ == "__main__":
    print(build(force=True))
