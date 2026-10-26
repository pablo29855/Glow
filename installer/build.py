"""Build the GLOW Windows installer.

    python installer/build.py

Needs PyInstaller and the packages in requirements.txt. Freezes the app into
dist/GLOW/ and, when Inno Setup 6 is installed, packs it into dist/GLOW-Setup.exe.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
# read without importing glow.py, which loads the win32 APIs
VERSION = re.search(r'^VERSION = "([^"]+)"', (ROOT / "glow.py").read_text(encoding="utf-8"), re.M).group(1)


def run(*cmd):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT)


def main():
    run(sys.executable, "glow.py", "check")
    # onedir, not onefile: starts at once and is not unpacked to %TEMP% on every launch
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noconsole",
        "--name", "GLOW", "--icon", ROOT / "glow.ico", "--add-data", f"{ROOT / 'glow.ico'};.",
        "--distpath", DIST, "--workpath", ROOT / "build", "--specpath", ROOT / "build",
        "glow_app.py")
    iscc = shutil.which("iscc") or next((p for p in (
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6/ISCC.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Inno Setup 6/ISCC.exe",
    ) if p.is_file()), None)
    if not iscc:
        sys.exit(f"App ready in {DIST / 'GLOW'}. Install Inno Setup 6 to build GLOW-Setup.exe.")
    run(iscc, f"/DAppVer={VERSION}", ROOT / "installer" / "glow.iss")
    print("Done:", DIST / "GLOW-Setup.exe")


if __name__ == "__main__":
    main()
