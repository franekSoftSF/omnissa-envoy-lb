import shutil
import subprocess
import unittest

from tests.helpers import ROOT

SCRIPTS = ["install.sh", "uninstall.sh", "update.sh", "scripts/verify-omnissa-lb.sh"]


class ScriptsTest(unittest.TestCase):
    def test_bash_syntax(self):
        for s in SCRIPTS:
            r = subprocess.run(["bash", "-n", str(ROOT / s)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{s}: {r.stderr}")

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck niedostępny")
    def test_shellcheck(self):
        r = subprocess.run(["shellcheck", "-S", "warning", *[str(ROOT / s) for s in SCRIPTS]],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_install_strict_mode_and_trap(self):
        text = (ROOT / "install.sh").read_text()
        self.assertIn("set -Eeuo pipefail", text)
        self.assertIn("trap 'on_err", text)
        self.assertNotIn("--break-system-packages", text)
        self.assertNotRegex(text, r"curl[^\n]*\|\s*(sudo\s+)?(ba)?sh")

    def test_help_and_bad_option(self):
        r = subprocess.run(["bash", str(ROOT / "install.sh"), "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--unattended", r.stdout)
        r = subprocess.run(["bash", str(ROOT / "install.sh"), "--bogus"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_requirements_pinned(self):
        for line in (ROOT / "requirements.txt").read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==\d")


if __name__ == "__main__":
    unittest.main()
