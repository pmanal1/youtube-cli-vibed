import os
import select
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import ytcli

if ytcli._RAW_KEY_MODULES:
    import pty
    import termios


# Exercise the actual menu and terminal input without touching YouTube or files.
MENU_SCRIPT = """
import ytcli

video = {"id": "abcdefghijk", "title": "Test video", "item_id": "item"}
plan = {
    "items": [video], "fresh": [video], "already_saved": 0, "invalid": 0,
}
ytcli.make_oauth_session = lambda: None
ytcli.build_grab_bag_plan = lambda session: plan

def execute(session, plan):
    print("SYNC_EXECUTED", flush=True)
    return {"imported": 1, "removed": 1, "failed": []}

ytcli.execute_grab_bag_plan = execute
ytcli.main([])
"""


@unittest.skipUnless(ytcli._RAW_KEY_MODULES, "Requires a POSIX terminal")
class GrabBagTerminalTests(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.original_state = termios.tcgetattr(self.slave)
        self.pending = b""
        self.output = b""
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", MENU_SCRIPT],
            cwd=Path(ytcli.__file__).resolve().parent,
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
        )
        self.addCleanup(self.cleanup_terminal)
        self.read_until(b"=== ytcli ===")
        os.write(self.master, b"\x1b[B\x1b[B\r")
        self.read_until(b"q=quit): ")

    def cleanup_terminal(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)
        os.close(self.master)
        os.close(self.slave)

    def read_until(self, marker):
        deadline = time.monotonic() + 5
        while marker not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.master], [], [], remaining)[0]:
                self.fail(f"Timed out waiting for {marker!r}: {self.output!r}")
            data = os.read(self.master, 65536)
            self.pending += data
            self.output += data
        _, _, self.pending = self.pending.partition(marker)

    def quit_menu(self):
        os.write(self.master, b"q")
        self.read_until(b"Bye!")
        self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertEqual(termios.tcgetattr(self.slave), self.original_state)

    def test_y_confirms_without_enter_and_returns_to_menu(self):
        os.write(self.master, b"y")
        self.read_until(b"Press Enter/Esc to go back")
        self.assertEqual(self.output.count(b"SYNC_EXECUTED"), 1)
        self.assertIn(b"Imported 1 video(s)", self.output)
        os.write(self.master, b"\r")
        self.read_until(b"=== ytcli ===")
        self.quit_menu()

    def test_uppercase_y_confirms_without_enter(self):
        os.write(self.master, b"Y")
        self.read_until(b"Press Enter/Esc to go back")
        self.assertEqual(self.output.count(b"SYNC_EXECUTED"), 1)
        os.write(self.master, b"q")
        self.read_until(b"Bye!")
        self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertEqual(termios.tcgetattr(self.slave), self.original_state)

    def test_n_cancels_and_escape_returns(self):
        os.write(self.master, b"n")
        self.read_until(b"Press Enter/Esc to go back")
        self.assertIn(b"Cancelled.", self.output)
        self.assertNotIn(b"SYNC_EXECUTED", self.output)
        os.write(self.master, b"\x1b")
        self.read_until(b"=== ytcli ===")
        self.quit_menu()

    def test_enter_defaults_to_cancel(self):
        os.write(self.master, b"\r")
        self.read_until(b"Press Enter/Esc to go back")
        self.assertIn(b"Cancelled.", self.output)
        self.assertNotIn(b"SYNC_EXECUTED", self.output)
        os.write(self.master, b"\r")
        self.read_until(b"=== ytcli ===")
        self.quit_menu()

    def test_escape_cancels(self):
        os.write(self.master, b"\x1b")
        self.read_until(b"Press Enter/Esc to go back")
        self.assertIn(b"Cancelled.", self.output)
        self.assertNotIn(b"SYNC_EXECUTED", self.output)
        os.write(self.master, b"\r")
        self.read_until(b"=== ytcli ===")
        self.quit_menu()

    def test_q_quits_without_sync_and_restores_terminal(self):
        self.quit_menu()
        self.assertNotIn(b"SYNC_EXECUTED", self.output)


class GrabBagPipedInputTests(unittest.TestCase):
    def test_line_confirmation(self):
        for answer, expected in (("y", True), (" YES ", True), ("n", False), ("", False)):
            with self.subTest(answer=answer), patch.object(
                ytcli, "raw_keys_supported", return_value=False
            ), patch("builtins.input", return_value=answer):
                self.assertEqual(ytcli.read_confirmation("Sync?"), expected)


if __name__ == "__main__":
    unittest.main()
