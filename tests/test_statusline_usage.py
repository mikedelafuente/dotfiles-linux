"""The status line keeps each limit's freshest reading in ~/.claude/usage.json."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "config/claude/statusline-command.sh"


def limit(used, resets):
    return {"used_percentage": used, "resets_at": resets}


@unittest.skipUnless(shutil.which("jq"), "jq required")
class StatuslineUsage(unittest.TestCase):
    def saved(self, readings):
        """Feeds each session's limits to the script in turn and returns what the file holds."""
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".claude").mkdir()
            for limits in readings:
                subprocess.run(["bash", str(SCRIPT)], env=dict(os.environ, HOME=home), check=True,
                               input=json.dumps({"cwd": home, "rate_limits": limits}),
                               capture_output=True, text=True, timeout=15)
            saved = json.loads((Path(home) / ".claude/usage.json").read_text())
        self.assertIsInstance(saved.pop("at"), int)
        return saved

    def test_a_missing_figure_keeps_the_saved_one(self):
        self.assertEqual(self.saved([
            {"five_hour": limit(77, 2000), "seven_day": limit(53, 9000)},
            {"seven_day": limit(31, 9000)},
        ]), {"five_hour": 77, "five_hour_resets_at": 2000, "seven_day": 53, "seven_day_resets_at": 9000})

    def test_an_older_window_loses_and_a_later_one_wins(self):
        self.assertEqual(self.saved([
            {"five_hour": limit(77, 2000), "seven_day": limit(53, 9000)},
            {"five_hour": limit(95, 1000), "seven_day": limit(5, 9500)},
        ]), {"five_hour": 77, "five_hour_resets_at": 2000, "seven_day": 5, "seven_day_resets_at": 9500})

    def test_an_unreadable_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as home:
            usage = Path(home) / ".claude/usage.json"
            usage.parent.mkdir()
            usage.write_text("not json")
            subprocess.run(["bash", str(SCRIPT)], env=dict(os.environ, HOME=home), check=True,
                           input=json.dumps({"cwd": home, "rate_limits": {"five_hour": limit(12, 2000)}}),
                           capture_output=True, text=True, timeout=15)
            self.assertEqual(json.loads(usage.read_text())["five_hour"], 12)


if __name__ == "__main__":
    unittest.main()
