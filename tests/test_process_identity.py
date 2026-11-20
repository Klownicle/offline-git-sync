"""Windows process liveness using harmless isolated Python children only."""

import os
import subprocess
import sys
import unittest

import github_dashboard as dashboard


@unittest.skipUnless(os.name == "nt", "Windows process handles are platform-specific")
class ProcessIdentityTests(unittest.TestCase):
    def test_current_process_has_stable_live_identity(self):
        identity = dashboard.process_identity(os.getpid())
        self.assertIsInstance(identity, str)
        self.assertTrue(identity)
        self.assertEqual(dashboard.process_identity(os.getpid()), identity)

    def test_exited_child_is_not_live_while_popen_retains_its_handle(self):
        # 259 is STILL_ACTIVE in GetExitCodeProcess, but it is also a possible
        # real exit code. Waiting on the process handle distinguishes the two.
        for exit_code in (0, 259):
            with self.subTest(exit_code=exit_code):
                with subprocess.Popen(
                    [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); sys.exit(int(sys.argv[1]))",
                     str(exit_code)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ) as child:
                    try:
                        self.assertIsNone(child.poll())
                        identity = dashboard.process_identity(child.pid)
                        self.assertIsInstance(identity, str)
                        self.assertEqual(dashboard.process_identity(child.pid), identity)
                        child.stdin.close()
                        self.assertEqual(child.wait(timeout=10), exit_code)
                        self.assertIsNotNone(child._handle)
                        self.assertIsNone(dashboard.process_identity(child.pid))
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
