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

    def test_no_wait_after_last_app(self):
        waits = []
        self.entries = lambda: [{"name": "B", "bundle": "b", "restore": "auto", "pid": 2, "ram_mb": 1}]
        opened, _ = self.run_restore(calm_side=lambda *a: waits.append(a) or True)
        self.assertEqual(opened, ["B"])
        self.assertEqual(waits, [])

    def test_interrupt_keeps_remaining_in_snapshot(self):
        def boom(*a, **k):
            raise KeyboardInterrupt()
        opened, result = self.run_restore(calm_side=boom)
        self.assertEqual(opened, ["B"])
        self.assertFalse(result)
        left = [e["name"] for e in gf.load_snapshot()["apps"]]
        self.assertEqual(left, ["A", "M"])


# pid: (ppid, uid, comm)
ME = os.getuid()
TABLE = {
    200: (1, ME, "/Applications/Heavy.app/Contents/MacOS/Heavy"),
    201: (200, ME, "/Applications/Heavy.app/Contents/Helper"),
    202: (201, ME, "/System/Library/PrivateFrameworks/WindowServer"),   # system name under the tree
    203: (200, 0, "/usr/sbin/rootthing"),                               # other uid
    300: (1, ME, "/Applications/Discord.app/Contents/MacOS/Discord"),
}
FAPPS = [{"name": "Heavy", "bundle": "x.heavy", "pid": 200},
         {"name": "Discord", "bundle": "com.hnc.Discord", "pid": 300},
         {"name": "Steam", "bundle": "com.valvesoftware.steam", "pid": 400}]


class Actions(IsolatedHome):
    def select(self, entries):
        with mock.patch.object(gf, "ancestor_pids", return_value=set()), \
             mock.patch.object(gf, "ram_by_pid", return_value={}), \
             mock.patch.object(gf, "proc_table", return_value=TABLE):
            return gf.select_targets(FAPPS, gf.norm_entries(entries), [])

    def test_action_defaults_to_quit_and_bad_action_is_config_error(self):
        self.assertEqual(gf.norm_entries(["A"])[0]["action"], "quit")
        c = {"profiles": {"p": {"quit": [{"app": "A", "action": "freeze"}]}}}
        self.assertEqual(len(gf.validate_config(c)), 1)

    def test_pause_tree_excludes_system_and_foreign_processes(self):
        t, _ = self.select([{"app": "Heavy", "action": "pause"}])
        self.assertEqual(t[0]["action"], "pause")
        self.assertEqual(t[0]["pids"], [200, 201])

    def test_safelist_refuses_pause_and_demote_but_not_keep_semantics(self):
        t, notes = self.select([{"app": "Discord", "action": "pause"},
                                {"app": "Steam", "action": "demote"}])
        self.assertEqual(t, [])
        self.assertEqual(len(notes), 2)
        self.assertIn("safelist", notes[0])

    def test_keep_action_is_never_a_target(self):
        t, _ = self.select([{"app": "Heavy", "action": "keep"}])
        self.assertEqual(t, [])

    def test_apply_and_undo_pause_and_demote(self):
        pause = {"action": "pause", "pids": [1, 2]}
        demote = {"action": "demote", "pids": [3]}
        with mock.patch.object(gf.os, "kill") as kill, mock.patch.object(gf, "sh", return_value=(0, "", "")) as sh:
            gf.apply_action(pause)
            gf.apply_action(demote)
            gf.undo_action(pause)
            gf.undo_action(demote)
        self.assertEqual(kill.call_args_list, [mock.call(1, gf.signal.SIGSTOP), mock.call(2, gf.signal.SIGSTOP),
                                                mock.call(1, gf.signal.SIGCONT), mock.call(2, gf.signal.SIGCONT)])
        self.assertEqual([c.args[0] for c in sh.call_args_list],
                         [["taskpolicy", "-b", "-p", "3"], ["taskpolicy", "-B", "-p", "3"]])

    def test_undo_ignores_already_dead_pid(self):
        with mock.patch.object(gf.os, "kill", side_effect=ProcessLookupError):
            gf.undo_action({"action": "pause", "pids": [9]})

    def test_watchdog_undoes_freezes_keeps_quit_entries(self):
        gf.save_snapshot("p", [
            {"name": "A", "action": "pause", "pids": [5], "restore": "auto"},
            {"name": "B", "action": "demote", "pids": [6], "restore": "auto"},
            {"name": "C", "bundle": "c", "restore": "auto"}])      # old-format entry == quit
        with mock.patch.object(gf, "undo_action") as undo:
            n = gf.resume_frozen()
        self.assertEqual(n, 2)
        self.assertEqual(undo.call_count, 2)
        self.assertEqual([e["name"] for e in gf.load_snapshot()["apps"]], ["C"])

    def test_resume_frozen_removes_snapshot_when_only_freezes(self):
        gf.save_snapshot("p", [{"name": "A", "action": "pause", "pids": [5], "restore": "auto"}])
        with mock.patch.object(gf, "undo_action"):
            gf.resume_frozen()
        self.assertIsNone(gf.load_snapshot())

    def test_restore_undoes_freezes_before_opening_apps(self):
        order = []
        entries = [{"name": "A", "bundle": "a", "action": "pause", "pids": [5], "restore": "auto"},
                   {"name": "B", "bundle": "b", "restore": "auto"}]
        gf.save_snapshot("p", entries)
        with mock.patch.object(gf, "list_apps", return_value=[]), \
             mock.patch.object(gf, "undo_action", side_effect=lambda e: order.append("undo " + e["name"])), \
             mock.patch.object(gf, "open_app", side_effect=lambda e: order.append("open " + e["name"]) or True), \
             mock.patch.object(gf, "wait_until_calm", return_value=True), \
             mock.patch.object(gf.time, "sleep"):
            gf.restore({"settings": dict(ST)}, {"profile": "p", "apps": entries})
        self.assertEqual(order, ["undo A", "open B"])


class Watch(unittest.TestCase):
    def test_trigger_needs_consecutive_hits_and_resets_on_gap(self):
        counts = {}
        self.assertIsNone(gf.watch_tick(counts, {"g"}))          # 1st hit
        self.assertIsNone(gf.watch_tick(counts, set()))          # gap (alt-tab, flicker) resets
        self.assertIsNone(gf.watch_tick(counts, {"g"}))
        self.assertEqual(gf.watch_tick(counts, {"g"}), "g")      # 2 in a row

    def test_only_watch_true_profiles_with_match_are_polled(self):
        cfg = {"profiles": {
            "a": {"watch": True, "game": {"match": "steamapps/common/", "match_args": True}},
            "b": {"game": {"match": "x"}},
            "c": {"watch": True, "game": {}}}}
        seen = []
        with mock.patch.object(gf, "find_game_pids", side_effect=lambda m, a: seen.append((m, a)) or [5]):
            self.assertEqual(gf.watched_hits(cfg), {"a"})
        self.assertEqual(seen, [("steamapps/common/", True)])

    def test_wait_gone_ignores_single_flicker(self):
        seq = iter([{"g"}, set(), {"g"}, set(), set()])  # one-poll gap must not count as exit
        calls = []
        with mock.patch.object(gf, "watched_hits", side_effect=lambda c: calls.append(1) or next(seq)), \
             mock.patch.object(gf.time, "sleep"):
            gf.wait_gone({}, "g")
        self.assertEqual(len(calls), 5)

    def test_validate_rejects_watch_without_match(self):
        c = {"profiles": {"w": {"watch": True, "game": {}}}}
        self.assertEqual(len(gf.validate_config(c)), 1)


if __name__ == "__main__":
    unittest.main()
