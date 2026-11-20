"""Prepare this folder for Offline Git Sync without adding tasks or syncing data."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent


def validate_environment():
    if os.name != "nt":
        raise RuntimeError("Offline Git Sync requires Windows and Windows Task Scheduler.")
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required. Install Python from python.org.")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError(f"Windowless Python is missing: {pythonw}. Use a full Windows Python installation.")
    from github_sync import git_executable
    result = subprocess.run([str(git_executable()), "--version"], check=True,
                            capture_output=True, text=True, timeout=15)
    return pythonw, result.stdout.strip()


def install_dependencies(root):
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
        "--upgrade", "--no-deps", "--target", str(root / ".tools" / "python"),
        "--requirement", str(root / "requirements.txt"),
    ], check=True)
    # Verify through the same loader used by downloads. This checks that the
    # selected interpreter can load the native certificate validation package.
    subprocess.run([sys.executable, "-c", "import github_https; github_https.ssl_context()"],
                   cwd=str(root), check=True)


def write_launcher(root, pythonw):
    # Escape a VBScript string literal; the command itself quotes both paths.
    python_literal = str(pythonw).replace('"', '""')
    launcher = '''Option Explicit
Dim shell, files, root, python, script, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
python = "PYTHON_LITERAL"
If Not files.FileExists(python) Then
    MsgBox "Python could not be found. Run python install.py again from this application folder.", 16, "Offline Git Sync"
    WScript.Quit 1
End If
script = files.BuildPath(root, "github_sync.py")
command = Chr(34) & python & Chr(34) & " " & Chr(34) & script & Chr(34) & " dashboard --open"
shell.CurrentDirectory = root
shell.Run command, 0, False
'''.replace("PYTHON_LITERAL", python_literal)
    # WSH recognizes UTF-16 with its BOM, including non-ASCII account/folder names.
    (root / "Open dashboard.vbs").write_text(launcher, encoding="utf-16")


def initialize_config(root):
    # Exclusive creation is intentional: reinstalling never resets saved syncs.
    try:
        with (root / "sync-config.json").open("x", encoding="utf-8") as config:
            config.write("{}\n")
    except FileExistsError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Run from the permanent application folder with Python 3.11 or newer. "
        "Installs truststore locally, preserves existing settings and creates "
        "Open dashboard.vbs. Does not register scheduled tasks or run a sync."))
    parser.parse_args()
    pythonw, git_version = validate_environment()
    # Fail before installing if an existing configuration needs attention.
    from github_sync import load_config
    if (ROOT / "sync-config.json").exists():
        load_config()
    print(f"Preparing {ROOT}")
    print(f"Python {sys.version.split()[0]}; {git_version}")
    install_dependencies(ROOT)
    initialize_config(ROOT)
    write_launcher(ROOT, pythonw)
    print("Ready. Double-click Open dashboard.vbs to open the dashboard.")
    print("No sync tasks were created and no repositories were downloaded.")
    print("Keep this folder and Python installation in place after adding scheduled syncs.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        sys.exit(1)
