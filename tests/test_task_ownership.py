"""Task XML ownership regression tests; no Scheduler, config, or process writes."""

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import github_management as management
import github_sync as sync


NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class TaskOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.repository = "chasem-dev/sms-launcher"
        self.script = str(Path(sync.__file__).resolve())
        self.python = str(Path(sync.sys.executable).with_name("pythonw.exe"))
        self.saved = [self.script, "run-saved", self.repository]
        patcher = patch.object(management, "_task_command",
                               side_effect=AssertionError("Ownership tests must not call Task Scheduler"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def owns(self, command, words=None, arguments=None):
        if arguments is None:
            arguments = subprocess.list2cmdline(self.saved if words is None else words)
        return management._owns_action(sync, self.repository, command, arguments)

    def task_xml(self, command, words=None, additional_actions=None):
        root = ET.fromstring(f'''<Task xmlns="{NS}" version="1.2">
          <RegistrationInfo><Description>Preserve this exact task configuration</Description></RegistrationInfo>
          <Triggers><TimeTrigger><StartBoundary>2026-11-10T08:43:00-05:00</StartBoundary>
          <Repetition><Interval>PT37M</Interval><Duration>P1D</Duration></Repetition></TimeTrigger></Triggers>
          <Principals><Principal id="Author"><UserId>LocalUser</UserId><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
          <Settings><Enabled>true</Enabled><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy></Settings>
          <Actions Context="Author"><Exec><Command/><Arguments/><WorkingDirectory>C:\\Git</WorkingDirectory></Exec></Actions>
        </Task>''')
        actions = root.find(f"{{{NS}}}Actions")
        action = actions.find(f"{{{NS}}}Exec")
        action.find(f"{{{NS}}}Command").text = command
        action.find(f"{{{NS}}}Arguments").text = subprocess.list2cmdline(self.saved if words is None else words)
        for extra in additional_actions or []:
            actions.append(extra)
        return ET.tostring(root, encoding="unicode")

    def migrate(self, xml):
        with patch.object(management, "_saved_command", return_value=[self.python, *self.saved]):
            return management._saved_action_xml(sync, self.repository, xml)

    def test_accepts_quoted_command_for_existing_legacy_run(self):
        words = [self.script, "run", self.repository, "--asset-pattern", "*win*.zip",
                 "--asset-pattern", "*.exe", "--last", "5", "--branch", "main"]
        self.assertTrue(self.owns(f'"{self.python}"', words))

    def test_accepts_quoted_command_for_run_saved(self):
        self.assertTrue(self.owns(f'"{self.python}"'))

    def test_accepts_unquoted_and_case_normalized_known_python_commands(self):
        for executable in (self.python, self.python.upper(),
                           str(Path(sync.sys.executable).with_name("python.exe"))):
            with self.subTest(executable=executable):
                self.assertTrue(self.owns(executable))

    def test_rejects_unbalanced_nested_and_non_windows_command_quotes(self):
        cases = (f'"{self.python}', f'{self.python}"', f'""{self.python}""',
                 f"'{self.python}'", f'"{self.python}""', f'"{self.python}\\"')
        for executable in cases:
            with self.subTest(executable=executable):
                self.assertFalse(self.owns(executable))

    def test_rejects_command_containing_extra_arguments_or_shell_text(self):
        cases = (f'"{self.python}" -u', f'"{self.python}" --version',
                 f'{self.python} -u', f'"{self.python}" & calc.exe',
                 f'"{self.python}" "{self.script}"', f'"{self.python} --version"')
        for executable in cases:
            with self.subTest(executable=executable):
                self.assertFalse(self.owns(executable))

    def test_rejects_other_python_installations_relative_paths_and_wrappers(self):
        for executable in (r"C:\OtherPython\pythonw.exe", r'"C:\OtherPython\pythonw.exe"',
                           "pythonw.exe", "python.exe", "cmd.exe", "powershell.exe", ""):
            with self.subTest(executable=executable):
                self.assertFalse(self.owns(executable))

    def test_quoted_command_does_not_relax_script_repository_or_argument_checks(self):
        cases = ([self.script, "run-saved", "another/repository"],
                 ["github_sync.py", "run-saved", self.repository],
                 [str(Path(self.script).with_name("other.py")), "run-saved", self.repository],
                 [self.script, "run-saved", self.repository, "extra"],
                 [self.script, "run", self.repository, "--unknown", "value"],
                 [self.script, "run", self.repository, "--asset-pattern"],
                 [self.script, "run-all"], [self.script, "setup", self.repository])
        for words in cases:
            with self.subTest(words=words):
                self.assertFalse(self.owns(f'"{self.python}"', words))
        self.assertFalse(self.owns(f'"{self.python}"', arguments=f'"{self.script} run-saved {self.repository}'))

    def test_migrates_quoted_legacy_and_saved_actions_preserving_task_configuration(self):
        for command_name in ("run", "run-saved"):
            words = [self.script, command_name, self.repository]
            if command_name == "run":
                words += ["--asset-pattern", "old*.zip", "--last", "3"]
            with self.subTest(command_name=command_name):
                old = self.task_xml(f'"{self.python}"', words)
                new = self.migrate(old)
                old_root, new_root = ET.fromstring(old), ET.fromstring(new)
                self.assertEqual(old_root.attrib, new_root.attrib)
                for element in ("RegistrationInfo", "Triggers", "Principals", "Settings"):
                    self.assertEqual(ET.tostring(old_root.find(f"{{{NS}}}{element}")),
                                     ET.tostring(new_root.find(f"{{{NS}}}{element}")))
                action = new_root.find(f"{{{NS}}}Actions/{{{NS}}}Exec")
                self.assertEqual(action.findtext(f"{{{NS}}}Command"), self.python)
                self.assertEqual(management._split_windows_arguments(action.findtext(f"{{{NS}}}Arguments")),
                                 self.saved)
                self.assertEqual(action.findtext(f"{{{NS}}}WorkingDirectory"), r"C:\Git")

    def test_rejects_multiple_owned_actions_even_with_mixed_quoting(self):
        extra = ET.fromstring(self.task_xml(self.python)).find(f"{{{NS}}}Actions/{{{NS}}}Exec")
        xml = self.task_xml(f'"{self.python}"', additional_actions=[extra])
        with self.assertRaisesRegex(ValueError, "not uniquely owned"):
            self.migrate(xml)

    def test_preserves_an_unrelated_action_when_exactly_one_action_is_owned(self):
        extra = ET.fromstring(self.task_xml(r"C:\OtherPython\pythonw.exe")).find(
            f"{{{NS}}}Actions/{{{NS}}}Exec")
        original = ET.tostring(extra)
        xml = self.task_xml(f'"{self.python}"', additional_actions=[extra])
        migrated = ET.fromstring(self.migrate(xml)).find(f"{{{NS}}}Actions")
        self.assertEqual(len(migrated), 2)
        self.assertEqual(ET.tostring(list(migrated)[1]), original)

    def test_unknown_action_diagnostics_name_expected_and_actual_identity(self):
        external = r"C:\OtherPython\pythonw.exe"
        with self.assertRaises(management.TaskActionConflict) as caught:
            self.migrate(self.task_xml(external))
        details = caught.exception.diagnostics
        self.assertEqual(details["task_name"], "Git Sync - chasem-dev-sms-launcher")
        self.assertEqual(details["repository"], self.repository)
        self.assertEqual(details["expected"]["command"], self.python)
        self.assertEqual(management._split_windows_arguments(details["expected"]["arguments"]), self.saved)
        self.assertEqual(len(details["actions"]), 1)
        self.assertEqual(details["actions"][0]["command"], external)
        self.assertEqual(management._split_windows_arguments(details["actions"][0]["arguments"]), self.saved)
        self.assertIn("not this tool's Python", details["actions"][0]["reason"])
        self.assertIn("No task or settings were changed", details["help"])

    def test_multiple_owned_action_diagnostics_explain_ambiguity(self):
        extra = ET.fromstring(self.task_xml(self.python)).find(f"{{{NS}}}Actions/{{{NS}}}Exec")
        with self.assertRaises(management.TaskActionConflict) as caught:
            self.migrate(self.task_xml(f'"{self.python}"', additional_actions=[extra]))
        actions = caught.exception.diagnostics["actions"]
        self.assertEqual(len(actions), 2)
        self.assertTrue(all("ambiguous" in action["reason"] for action in actions))
        self.assertTrue(all("More than one action" in action["reason"] for action in actions))

    def test_zero_action_diagnostics_are_available_without_inventing_an_action(self):
        for element in ("<Actions/>", ""):
            with self.subTest(element=element), self.assertRaises(management.TaskActionConflict) as caught:
                self.migrate(f'<Task xmlns="{NS}" version="1.2">{element}</Task>')
            self.assertEqual(caught.exception.diagnostics["actions"], [])
            self.assertEqual(caught.exception.diagnostics["repository"], self.repository)
            self.assertEqual(caught.exception.diagnostics["expected"]["command"], self.python)

    def test_distinct_argument_mismatches_have_specific_diagnostics(self):
        cases = (([self.script, "run-saved", "other/repo"], "different repository"),
                 ([str(Path(self.script).with_name("other.py")), "run-saved", self.repository], "github_sync.py"),
                 ([self.script, "run-saved", self.repository, "extra"], "unexpected extra arguments"),
                 ([self.script, "run", self.repository, "--unknown"], "unsupported option"),
                 ([self.script, "setup", self.repository], "not a run or run-saved"))
        for words, reason in cases:
            with self.subTest(words=words), self.assertRaises(management.TaskActionConflict) as caught:
                self.migrate(self.task_xml(f'"{self.python}"', words))
            self.assertIn(reason, caught.exception.diagnostics["actions"][0]["reason"])

    def test_non_exec_action_diagnostics_do_not_attempt_to_claim_it(self):
        xml = f'<Task xmlns="{NS}"><Actions><ComHandler><ClassId>unrelated</ClassId></ComHandler></Actions></Task>'
        with self.assertRaises(management.TaskActionConflict) as caught:
            self.migrate(xml)
        actions = caught.exception.diagnostics["actions"]
        self.assertEqual(len(actions), 1)
        self.assertIn("not a program execution", actions[0]["reason"])


if __name__ == "__main__":
    unittest.main()
