import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gamefocus as gf  # noqa: E402

ST = dict(gf.DEFAULT_SETTINGS)


class IsolatedHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name, fname in (("HOME_DIR", ""), ("SNAPSHOT", "snapshot.json"), ("LOGFILE", "log")):
            p = mock.patch.object(gf, name, os.path.join(self.tmp.name, fname) if fname else self.tmp.name)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)


class Calm(unittest.TestCase):
    """Regression: pressure level 2 is the idle baseline on 8GB Macs; must not block restore."""

    def calm(self, pressure, free, idle):
        with mock.patch.object(gf, "mem_pressure", return_value=pressure), \
             mock.patch.object(gf, "mem_free_pct", return_value=free), \
             mock.patch.object(gf, "cpu_idle_pct", return_value=idle):
            return gf.is_calm(ST)

    def test_warning_level_with_enough_free_is_calm(self):
        self.assertTrue(self.calm(2, 27, 84))

    def test_critical_is_not_calm(self):
        self.assertFalse(self.calm(4, 50, 99))

    def test_low_free_is_not_calm(self):
        self.assertFalse(self.calm(2, 8, 99))

    def test_busy_cpu_is_not_calm(self):
        self.assertFalse(self.calm(1, 50, 20))

    def test_unknown_free_falls_back_to_other_signals(self):
        self.assertTrue(self.calm(2, None, 90))

    def test_parse_memory_pressure_output(self):
        out = "The system has 8589934592 (524288 pages)\nSystem-wide memory free percentage: 27%\n"
        with mock.patch.object(gf, "sh", return_value=(0, out, "")):
            self.assertEqual(gf.mem_free_pct(), 27)
        with mock.patch.object(gf, "sh", return_value=(1, "", "boom")):
            self.assertIsNone(gf.mem_free_pct())


class Validate(unittest.TestCase):
    def good(self):
        return {"settings": dict(ST), "keep": [],
                "profiles": {"g": {"game": {"match": "cube"}, "quit": [{"app": "Slack", "restore": "late"}]}}}

    def test_good_config_has_no_errors(self):
        self.assertEqual(gf.validate_config(self.good()), [])

    def test_bad_restore_mode_is_error_not_silent_auto(self):
        c = self.good()
        c["profiles"]["g"]["quit"][0]["restore"] = "lates"
        errs = gf.validate_config(c)
        self.assertEqual(len(errs), 1)
        self.assertIn("lates", errs[0])

    def test_bad_regex_and_missing_app_and_bad_setting(self):
        c = self.good()
        c["profiles"]["g"]["game"]["match"] = "(unclosed"
        c["profiles"]["g"]["quit"].append({"restore": "auto"})
        c["settings"]["quit_timeout"] = "20"
        self.assertEqual(len(gf.validate_config(c)), 3)

    def test_profile_must_be_object(self):
        self.assertEqual(len(gf.validate_config({"profiles": {"g": []}})), 1)


APPS = [
    {"name": "Slack", "bundle": "com.tinyspeck.slackmacgap", "pid": 100},
    {"name": "Finder", "bundle": "com.apple.finder", "pid": 101},
    {"name": "Notion", "bundle": "notion.id", "pid": 102},
    {"name": "Terminal", "bundle": "com.apple.Terminal", "pid": 103},
]


class SelectTargets(unittest.TestCase):
    def select(self, entries, keep=(), ancestors=()):
        with mock.patch.object(gf, "ancestor_pids", return_value=set(ancestors)), \
             mock.patch.object(gf, "ram_by_pid", return_value={100: 300.4}):
            return gf.select_targets(APPS, gf.norm_entries(entries), list(keep))

    def test_picks_running_app_by_name_or_bundle(self):
        t, _ = self.select(["Slack", {"app": "notion.id", "restore": "late"}])
        self.assertEqual([x["name"] for x in t], ["Slack", "Notion"])
        self.assertEqual(t[0]["ram_mb"], 300)
        self.assertEqual(t[1]["restore"], "late")

    def test_hard_protected_never_selected(self):
        t, notes = self.select(["Finder", "com.apple.Terminal"])
        self.assertEqual(t, [])
        self.assertEqual(len(notes), 2)

    def test_keep_list_and_ancestors_protected(self):
        t, _ = self.select(["Slack", "Notion"], keep=["slack"], ancestors=[102])
        self.assertEqual(t, [])

    def test_not_running_is_noted(self):
        t, notes = self.select(["Nope"])
        self.assertEqual(t, [])
        self.assertIn("not running", notes[0])


class QuitTargets(IsolatedHome):
    def test_separates_done_from_stuck(self):
        ts = [{"name": "A", "bundle": "a", "pid": 1}, {"name": "B", "bundle": "b", "pid": 2}]
        with mock.patch.object(gf, "send_quit", return_value=True), \
             mock.patch.object(gf, "pid_alive", side_effect=lambda p: p == 2), \
             mock.patch.object(gf.time, "sleep"):
            done, stuck = gf.quit_targets(ts, timeout=0)
        self.assertEqual([t["name"] for t in done], ["A"])
        self.assertEqual([t["name"] for t in stuck], ["B"])


class FindGame(unittest.TestCase):
    PS = "  10 /Applications/Game.app/Contents/MacOS/Game\n  11 /usr/bin/other\n  12 /Applications/Game.app/Contents/Helper\n  junk\n"

    def test_matches_all_launcher_processes_excluding_self(self):
        with mock.patch.object(gf, "sh", return_value=(0, self.PS, "")), \
             mock.patch.object(gf.os, "getpid", return_value=12), \
             mock.patch.object(gf.os, "getppid", return_value=1):
            self.assertEqual(gf.find_game_pids("game"), [10])


class Restore(IsolatedHome):
    def entries(self):
        return [{"name": "A", "bundle": "a", "restore": "late", "pid": 1, "ram_mb": 1},
                {"name": "B", "bundle": "b", "restore": "auto", "pid": 2, "ram_mb": 1},
                {"name": "M", "bundle": "m", "restore": "manual", "pid": 3, "ram_mb": 1}]

    def cfg(self):
        return {"settings": dict(ST)}

    def run_restore(self, running=(), calm_side=None):
        opened = []
        with mock.patch.object(gf, "list_apps", return_value=list(running)), \
             mock.patch.object(gf, "open_app", side_effect=lambda e: opened.append(e["name"]) or True), \
             mock.patch.object(gf, "wait_until_calm", return_value=True, side_effect=calm_side), \
             mock.patch.object(gf.time, "sleep"):
            snap = {"profile": "p", "apps": self.entries()}
            gf.save_snapshot("p", snap["apps"])
            result = gf.restore(self.cfg(), snap)
        return opened, result

    def test_auto_before_late_manual_never_opened_snapshot_removed(self):
        opened, result = self.run_restore()
        self.assertEqual(opened, ["B", "A"])
        self.assertTrue(result)
        self.assertFalse(os.path.exists(gf.SNAPSHOT))

    def test_already_running_is_skipped(self):
        opened, _ = self.run_restore(running=[{"name": "B", "bundle": "b", "pid": 9}])
        self.assertEqual(opened, ["A"])

    def test_interrupt_keeps_remaining_in_snapshot(self):
        def boom(*a, **k):
            raise KeyboardInterrupt()
        opened, result = self.run_restore(calm_side=boom)
        self.assertEqual(opened, ["B"])
        self.assertFalse(result)
        left = [e["name"] for e in gf.load_snapshot()["apps"]]
        self.assertEqual(left, ["A", "M"])


if __name__ == "__main__":
    unittest.main()
