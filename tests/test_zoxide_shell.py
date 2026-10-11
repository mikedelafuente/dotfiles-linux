"""Check real shell integration with an isolated home and zoxide database."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("zoxide") and shutil.which("starship"),
                     "zoxide and Starship required")
class ZoxideShell(unittest.TestCase):
    def test_prompt_hook_and_directory_learning(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "trel-project").mkdir()
            env = dict(os.environ, HOME=temp, XDG_DATA_HOME=temp,
                       XDG_CONFIG_HOME=temp, XDG_CACHE_HOME=temp,
                       XDG_STATE_HOME=temp, TERM="xterm",
                       DFA_TEST_SOURCE=str(ROOT))
            env.pop("_ZO_DATA_DIR", None)
            env.pop("_ZO_DOCTOR", None)
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic", r'''
source "$DFA_TEST_SOURCE/home/.bashrc"
unset _ZO_DOCTOR
__zoxide_doctor
cd "$HOME/trel-project" || exit 1
__zoxide_hook
cd "$HOME" || exit 1
z trel || exit 1
[[ $PWD == "$HOME/trel-project" ]] || exit 1
source "$DFA_TEST_SOURCE/home/.bashrc"
__zoxide_doctor
'''], env=env, cwd=temp, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("possible configuration issue", result.stderr)


if __name__ == "__main__":
    unittest.main()
