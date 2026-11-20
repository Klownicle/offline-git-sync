"""Dashboard add/pattern workflows; temporary files and mocked scheduler/processes."""

from contextlib import ExitStack
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import github_dashboard as dashboard
import github_management as management
import github_sync as sync


NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class AddSyncTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(patch.object(management, "_render"))
        self.query = self.stack.enter_context(patch.object(management, "_task_xml", return_value=None))
        self.real_install = management._install_new
        self.real_launch = management._launch_saved
        self.install = self.stack.enter_context(patch.object(management, "_install_new"))
        self.launch = self.stack.enter_context(patch.object(management, "_launch_saved"))
        self.restore = self.stack.enter_context(patch.object(management, "_restore_task"))
        self.repository = "Owner/Repo"
        self.job = {"asset_patterns": ["*.zip"], "last": 2, "branch": "main",
                    "source_only": False, "branch_redirects": {"old": "main"},
                    "custom": {"keep": "unchanged"}}

    def write_jobs(self, jobs):
        sync.CONFIG.write_text(json.dumps(jobs), encoding="utf-8")

    def add(self, **changes):
        values = dict(repository=self.repository, asset_patterns=["*.zip"], every_minutes=30,
                      last=2, branch=None, source_only=False, start_time=None, run_now=False)
        values.update(changes)
        return management.add_sync(**values)

    def action_xml(self, *, repository=None, command=None, words=None):
        root = ET.fromstring(f'''<Task xmlns="{NS}" version="1.2">
          <RegistrationInfo><Description>Keep this</Description></RegistrationInfo>
          <Triggers><CalendarTrigger><StartBoundary>2022-01-01T08:00:00-05:00</StartBoundary>
          <Repetition><Interval>PT37M</Interval><Duration>P1D</Duration><StopAtDurationEnd>true</StopAtDurationEnd></Repetition>
          <ScheduleByWeek><WeeksInterval>2</WeeksInterval><DaysOfWeek><Tuesday/></DaysOfWeek></ScheduleByWeek>
          </CalendarTrigger><LogonTrigger><Enabled>false</Enabled></LogonTrigger></Triggers>
          <Principals><Principal id="Author"><UserId>user</UserId><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
          <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><Enabled>true</Enabled></Settings>
          <Actions Context="Author"><Exec><Command/><Arguments/><WorkingDirectory>C:\\Git</WorkingDirectory></Exec></Actions>
        </Task>''')
        root.find(f"{{{NS}}}Actions/{{{NS}}}Exec/{{{NS}}}Command").text = command or str(
            Path(sync.sys.executable).with_name("pythonw.exe"))
        root.find(f"{{{NS}}}Actions/{{{NS}}}Exec/{{{NS}}}Arguments").text = subprocess.list2cmdline(words or [
            str(Path(sync.__file__).resolve()), "run", repository or self.repository,
            "--asset-pattern", "old*.zip", "--last", "2", "--branch", "main"])
        return ET.tostring(root, encoding="unicode")

    def test_add_normalizes_url_preserves_case_and_existing_history(self):
        history = self.base / ".sync-state" / "existing-history.json"
        history.parent.mkdir()
        history.write_text("preserve", encoding="utf-8")
        result = self.add(repository=" https://github.com/Owner/Repo.git/ ",
                          asset_patterns=[" *.Zip ", "*.ZIP", "*.exe"], start_time="08:15")
        self.assertEqual(result["repository"], self.repository)
        self.assertEqual(result["job"]["asset_patterns"], ["*.Zip", "*.exe"])
        self.assertEqual(sync.load_config()[self.repository], result["job"])
        self.assertFalse(result["started"])
        self.assertEqual(history.read_text(), "preserve")
        self.launch.assert_not_called()
        self.install.assert_called_once()

    def test_add_source_only_preserves_selected_patterns_and_uses_daily_settings(self):
        result = self.add(source_only=True, every_minutes=1440, branch="feature/new-build", last=100)
        self.assertTrue(result["job"]["source_only"])
        self.assertEqual(result["job"]["asset_patterns"], ["*.zip"])
        self.assertEqual(result["job"]["branch"], "feature/new-build")

    def test_duplicate_active_archived_or_case_variant_never_touches_scheduler(self):
        for archived in (False, True):
            self.write_jobs({"owner/repo": dict(self.job, archived=archived)})
            with self.subTest(archived=archived), self.assertRaisesRegex(ValueError, "already has"):
                self.add()
            self.query.assert_not_called()
            self.install.assert_not_called()

    def test_task_name_collision_never_touches_scheduler(self):
        self.write_jobs({"a-b/c": self.job})
        with self.assertRaisesRegex(ValueError, "task name conflicts"):
            self.add(repository="a/b-c")
        self.query.assert_not_called()

    def test_local_folder_collision_with_other_sync_is_rejected(self):
        self.write_jobs({"Owner/Repo": self.job})
        for repository in ("Owner/Repo-releases", "owner/repo-releases"):
            with self.subTest(repository=repository), self.assertRaisesRegex(ValueError, "overlap"):
                self.add(repository=repository)
        self.query.assert_not_called()
        self.write_jobs({"owner/repo-releases": self.job})
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.add()

    def test_orphan_task_is_not_overwritten(self):
        self.query.return_value = self.action_xml()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.add()
        self.install.assert_not_called()
        self.assertFalse(sync.CONFIG.exists())

    def test_invalid_inputs_are_rejected_before_files_or_scheduler(self):
        bad = [dict(repository=value) for value in (
            None, "owner/..", "../repo", "owner/repo.", "CON/repo", "owner/NUL.zip",
            "owner/COM1", "owner/LPT9.txt", "https://github.com/owner/repo/issues",
            "http://github.com/owner/repo", "https://github.com/owner/repo?x=1",
            "git@github.com:owner/repo.git", "owner/repo/", "owner/repo\\other",
            "https://github.com.local/owner/repo", "owner/" + "x" * 101,
            ".sync-state/repo", ".tools/repo", "__pycache__/repo", "-owner/repo", "owner-/repo",
            "x" * 40 + "/repo")]
        bad += [dict(asset_patterns=value) for value in (
            None, "*.zip", [], [None], [""], [" "], ["foo/bar"], ["[x].zip"], ["x"] * 51, ["x"] * 0, ["x" * 201])]
        bad += [dict(branch=value) for value in (
            "", "HEAD", "-branch", "a..b", "a//b", "a/.hidden", "a.lock/b", "a/", "a.", "a b", 2)]
        bad += [dict(every_minutes=4), dict(every_minutes=True), dict(last=0), dict(last=101),
                dict(last=True), dict(start_time="9:00"), dict(source_only="false"), dict(run_now=None)]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.add(**values)
        self.assertFalse(sync.CONFIG.exists())
        self.assertFalse((self.base / ".sync-state").exists())
        self.query.assert_not_called()
        self.install.assert_not_called()
        self.launch.assert_not_called()

    def test_unmanaged_existing_source_and_reparse_path_are_rejected(self):
        source = self.base / "Owner" / "Repo"
        source.mkdir(parents=True)
        (source / "notes.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not a Git checkout"):
            self.add()
        self.assertEqual((source / "notes.txt").read_text(), "keep")
        with patch.object(management, "_no_links", side_effect=ValueError("reparse")):
            with self.assertRaisesRegex(ValueError, "reparse"):
                self.add()
        self.install.assert_not_called()

    def test_existing_checkout_and_releases_can_be_readded_without_touching_data(self):
        source = self.base / "Owner" / "Repo"
        (source / ".git").mkdir(parents=True)
        (source / "notes.txt").write_text("keep", encoding="utf-8")
        releases = self.base / "Owner" / "Repo-releases"
        releases.mkdir()
        (releases / "asset.zip").write_bytes(b"old data")
        self.add()
        self.assertEqual((source / "notes.txt").read_text(), "keep")
        self.assertEqual((releases / "asset.zip").read_bytes(), b"old data")

    def test_task_query_or_install_denied_does_not_save_or_launch(self):
        for failing in (self.query, self.install):
            failing.side_effect = RuntimeError("Access denied")
            with self.subTest(failing=failing), self.assertRaisesRegex(RuntimeError, "denied"):
                self.add(run_now=True)
            failing.side_effect = None
        self.assertFalse(sync.CONFIG.exists())
        self.launch.assert_not_called()

    def test_config_failure_rolls_back_new_task(self):
        with patch.object(management, "_write_config", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.add(run_now=True)
        self.restore.assert_called_once_with(self.repository, None)
        self.launch.assert_not_called()

    def test_first_run_starts_after_both_locks_and_config_saved(self):
        def launch(module, repository):
            self.assertIn(repository, sync.load_config())
            with management._locked(repository):
                pass
        self.launch.side_effect = launch
        result = self.add(run_now=True)
        self.assertTrue(result["started"])
        self.launch.assert_called_once_with(sync, self.repository)

    def test_launch_failure_keeps_saved_sync_and_returns_warning(self):
        self.launch.side_effect = OSError("cannot start process")
        result = self.add(run_now=True)
        self.assertFalse(result["started"])
        self.assertIn("cannot start process", result["warning"])
        self.assertIn(self.repository, sync.load_config())
        self.restore.assert_not_called()

    def test_install_new_never_forces_overwrite_and_uses_saved_settings(self):
        # Call the unmocked helper with a completely isolated module and command.
        module = SimpleNamespace(sys=sync.sys, __file__=sync.__file__)
        job = dict(every_minutes=1440, start_time="20:15")
        with patch.object(management, "_task_command", return_value=SimpleNamespace(returncode=0)) as command, \
                patch.object(management, "_saved_command", return_value=["pythonw.exe", "tool.py", "run-saved", self.repository]):
            self.real_install(module, self.repository, job)
        args = command.call_args.args
        self.assertNotIn("/F", args)
        self.assertIn("DAILY", args)
        self.assertIn("20:15", args)
        self.assertIn("run-saved", args[args.index("/TR") + 1])

    def test_launch_uses_hidden_window_python_with_no_shell(self):
        module = SimpleNamespace(BASE=self.base)
        command = ["pythonw.exe", "tool.py", "run-saved", self.repository]
        with patch.object(management, "_saved_command", return_value=command), \
                patch.object(management.subprocess, "Popen") as process:
            self.real_launch(module, self.repository)
        self.assertEqual(process.call_args.args[0], command)
        self.assertFalse(process.call_args.kwargs.get("shell", False))
        self.assertEqual(process.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(process.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_archived_patterns_preserve_every_other_field_without_scheduler_or_files(self):
        self.job["archived"] = True
        self.write_jobs({self.repository: self.job, "another/repo": self.job})
        before = copy.deepcopy(sync.load_config())
        result = management.update_asset_patterns(self.repository, ["*.exe", "*.msi"])
        expected = dict(self.job, asset_patterns=["*.exe", "*.msi"])
        self.assertEqual(result, expected)
        self.assertEqual(sync.load_config()["another/repo"], before["another/repo"])
        self.query.assert_not_called()
        self.restore.assert_not_called()
        self.install.assert_not_called()
        self.launch.assert_not_called()

    def test_active_patterns_migrate_only_owned_action_preserving_schedule_and_all_settings(self):
        self.write_jobs({self.repository: self.job})
        old = self.action_xml()
        self.query.return_value = old
        with patch.object(Path, "is_file", return_value=True):
            result = management.update_asset_patterns(self.repository, ["*.exe"])
        new = self.restore.call_args.args[1]
        old_root, new_root = ET.fromstring(old), ET.fromstring(new)
        self.assertEqual(old_root.attrib, new_root.attrib)
        for name in ("RegistrationInfo", "Triggers", "Principals", "Settings"):
            self.assertEqual(ET.tostring(old_root.find(f"{{{NS}}}{name}")),
                             ET.tostring(new_root.find(f"{{{NS}}}{name}")))
        action = new_root.find(f"{{{NS}}}Actions/{{{NS}}}Exec")
        words = management._split_windows_arguments(action.findtext(f"{{{NS}}}Arguments"))
        self.assertEqual(words, [str(Path(sync.__file__).resolve()), "run-saved", self.repository])
        self.assertEqual(action.findtext(f"{{{NS}}}WorkingDirectory"), "C:\\Git")
        self.assertEqual(result, dict(self.job, asset_patterns=["*.exe"]))
        self.install.assert_not_called()
        self.launch.assert_not_called()

    def test_absent_task_patterns_save_without_adding_schedule(self):
        self.write_jobs({self.repository: self.job})
        management.update_asset_patterns(self.repository, ["*.exe"])
        self.assertEqual(sync.load_config()[self.repository]["asset_patterns"], ["*.exe"])
        self.install.assert_not_called()
        self.restore.assert_not_called()

    def test_unknown_or_denied_action_does_not_update_config(self):
        self.write_jobs({self.repository: self.job})
        bad_xml = [self.action_xml(repository="Other/Repo"), self.action_xml(command="cmd.exe"),
                   self.action_xml(words=["other.py", "run", self.repository]),
                   self.action_xml(words=[str(Path(sync.__file__).resolve()), "run-saved", self.repository, "extra"])]
        for xml in bad_xml:
            self.query.return_value = xml
            with self.subTest(xml=xml), self.assertRaisesRegex(ValueError, "not uniquely owned"):
                management.update_asset_patterns(self.repository, ["*.exe"])
        self.query.side_effect = RuntimeError("Access denied")
        with self.assertRaisesRegex(RuntimeError, "denied"):
            management.update_asset_patterns(self.repository, ["*.exe"])
        self.assertEqual(sync.load_config()[self.repository], self.job)
        self.restore.assert_not_called()

    def test_pattern_config_failure_restores_original_task(self):
        self.write_jobs({self.repository: self.job})
        old = self.action_xml()
        self.query.return_value = old
        with patch.object(Path, "is_file", return_value=True), \
                patch.object(management, "_write_config", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                management.update_asset_patterns(self.repository, ["*.exe"])
        self.assertEqual(self.restore.call_count, 2)
        self.assertEqual(self.restore.call_args.args, (self.repository, old))
        self.assertEqual(sync.load_config()[self.repository], self.job)

    def test_pattern_import_denied_keeps_config(self):
        self.write_jobs({self.repository: self.job})
        self.query.return_value = self.action_xml()
        self.restore.side_effect = RuntimeError("Access denied")
        with patch.object(Path, "is_file", return_value=True), self.assertRaisesRegex(RuntimeError, "denied"):
            management.update_asset_patterns(self.repository, ["*.exe"])
        self.assertEqual(sync.load_config()[self.repository], self.job)

    def test_windows_argument_parser_round_trips_python_quoting(self):
        cases = [["C:\\Program Files\\Python311\\pythonw.exe", "C:\\tools with space\\tool.py", "run", "owner/repo"],
                 ["", 'a"b', "a\\", 'C:\\path with space\\', "foo", "a\\\\\"b"]]
        for words in cases:
            with self.subTest(words=words):
                self.assertEqual(management._split_windows_arguments(subprocess.list2cmdline(words)), words)


if __name__ == "__main__":
    unittest.main()
