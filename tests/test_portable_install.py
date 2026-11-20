"""Portable setup checks; no downloads, real tasks, or live configuration changes."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import github_sync as sync
import install


class PortableInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="offline git sync ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_storage_root_is_script_folder_not_current_directory(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "import github_sync; print(github_sync.BASE)")
        result = subprocess.run([sys.executable, "-c", code, str(install.ROOT)],
                                cwd=str(self.root), capture_output=True, text=True,
                                check=True)
        self.assertEqual(Path(result.stdout.strip()), install.ROOT)

    def test_initial_config_is_empty_and_reinstall_preserves_existing_settings(self):
        install.initialize_config(self.root)
        path = self.root / "sync-config.json"
        self.assertEqual(path.read_text(encoding="utf-8"), "{}\n")
        original = '{"owner/repo": {"asset_patterns": ["*"], "last": 5}}\n'
        path.write_text(original, encoding="utf-8")
        install.initialize_config(self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_launcher_binds_selected_python_and_resolves_its_own_folder(self):
        selected = Path(r"C:\Users\André\Python with spaces\pythonw.exe")
        install.write_launcher(self.root, selected)
        launcher = (self.root / "Open dashboard.vbs").read_text(encoding="utf-16")
        self.assertIn(f'python = "{selected}"', launcher)
        self.assertIn("files.GetParentFolderName(WScript.ScriptFullName)", launcher)
        self.assertIn('" dashboard --open"', launcher)
        self.assertNotIn("C:\\Git", launcher)

    def test_dependencies_install_only_into_package_folder(self):
        with mock.patch.object(install.subprocess, "run") as run:
            install.install_dependencies(self.root)
        self.assertEqual(run.call_args_list[0].args[0], [
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--upgrade", "--no-deps", "--target", str(self.root / ".tools" / "python"),
            "--requirement", str(self.root / "requirements.txt"),
        ])
        self.assertEqual(run.call_args_list[1].kwargs["cwd"], str(self.root))

    def test_git_standard_location_used_when_path_is_stale(self):
        git = self.root / "Programs" / "Git" / "cmd" / "git.exe"
        git.parent.mkdir(parents=True)
        git.write_bytes(b"")
        with mock.patch.object(sync.shutil, "which", return_value=None), \
                mock.patch.dict(sync.os.environ, {
                    "ProgramFiles": str(self.root / "not-installed"),
                    "ProgramFiles(x86)": str(self.root / "not-installed-x86"),
                    "LOCALAPPDATA": str(self.root),
                }):
            self.assertEqual(sync.git_executable(), git)

    def test_missing_git_has_actionable_error(self):
        with mock.patch.object(sync.shutil, "which", return_value=None), \
                mock.patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(FileNotFoundError, "Install Git for Windows"):
                sync.git_executable()


if __name__ == "__main__":
    unittest.main()
