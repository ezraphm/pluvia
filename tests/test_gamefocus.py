import json
import os
import plistlib
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


class MemLine(unittest.TestCase):
    def test_swap_parse_units(self):
        with mock.patch.object(gf, "sh", return_value=(0, "total = 2048.00M  used = 585.25M  free = 1462.75M  (encrypted)", "")):
            self.assertEqual(gf.swap_used_mb(), 585)
        with mock.patch.object(gf, "sh", return_value=(0, "total = 3.00G  used = 1.50G  free = 1.50G", "")):
            self.assertEqual(gf.swap_used_mb(), 1536)
        with mock.patch.object(gf, "sh", return_value=(1, "", "x")):
            self.assertIsNone(gf.swap_used_mb())

    def test_mem_line_shows_measured_values_and_unknowns(self):
        with mock.patch.object(gf, "mem_free_pct", return_value=27), \
             mock.patch.object(gf, "swap_used_mb", return_value=585), \
             mock.patch.object(gf, "mem_pressure", return_value=2):
            self.assertEqual(gf.mem_line("before"), "memory before: free 27%, swap used 585 MB, pressure 2")
        with mock.patch.object(gf, "mem_free_pct", return_value=None), \
             mock.patch.object(gf, "swap_used_mb", return_value=None), \
             mock.patch.object(gf, "mem_pressure", return_value=1):
            self.assertEqual(gf.mem_line("x"), "memory x: free ?%, swap used ? MB, pressure 1")


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


ACF = '''"AppState"
{
\t"appid"\t\t"%s"
\t"name"\t\t"%s"
\t"StateFlags"\t\t"%s"
\t"installdir"\t\t"x"
}
'''


class SteamGames(unittest.TestCase):
    def make(self, root, games):
        os.makedirs(os.path.join(root, "steamapps"))
        for appid, name, flags in games:
            with open(os.path.join(root, "steamapps", "appmanifest_%s.acf" % appid), "w") as f:
                f.write(ACF % (appid, name, flags))

    def test_lists_installed_games_across_libraries_skips_tools_and_partial(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.make(a, [("108600", "Project Zomboid", "4"), ("228980", "Steamworks Common Redistributables", "4"),
                          ("5", "Half Downloaded", "1026")])
            self.make(b, [("909080", "Cube Escape: Paradox", "4")])
            with open(os.path.join(a, "steamapps", "libraryfolders.vdf"), "w") as f:
                f.write('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % (a, b))
            games = gf.steam_games(a)
        self.assertEqual([(g["appid"], g["name"]) for g in games],
                         [("909080", "Cube Escape: Paradox"), ("108600", "Project Zomboid")])

    def test_direct_app_path_only_when_exactly_one_app_in_installdir(self):
        with tempfile.TemporaryDirectory() as a:
            self.make(a, [("1", "One App", "4"), ("2", "Two Apps", "4"), ("3", "No App", "4")])
            for appid, dirs in (("1", ["G.app"]), ("2", ["A.app", "B.app"]), ("3", [])):
                m = os.path.join(a, "steamapps", "appmanifest_%s.acf" % appid)
                with open(m, "a") as f:
                    f.write('"installdir" "d%s"\n' % appid)
                os.makedirs(os.path.join(a, "steamapps", "common", "d" + appid))
                for d in dirs:
                    os.makedirs(os.path.join(a, "steamapps", "common", "d" + appid, d))
            games = {g["appid"]: g for g in gf.steam_games(a)}
            self.assertEqual(games["1"]["app"], os.path.join(a, "steamapps", "common", "d1", "G.app"))
            self.assertNotIn("app", games["2"])
            self.assertNotIn("app", games["3"])

    def test_missing_steam_is_empty_not_error(self):
        self.assertEqual(gf.steam_games("/nonexistent/steam"), [])


STEAM_APP = {"name": "Steam", "bundle": "com.valvesoftware.steam", "pid": 7}
GAME = {"id": "steam:9", "source": "Steam", "name": "G", "app": "/x/G.app"}


class Play(IsolatedHome):
    def play(self, running_seq, game=GAME, ident="steam:9", via_steam=False):
        calls = []
        seq = iter(running_seq)
        with mock.patch.object(gf, "library_games", return_value=[game]), \
             mock.patch.object(gf, "list_apps", side_effect=lambda: next(seq)), \
             mock.patch.object(gf, "sh", side_effect=lambda c, timeout=30: calls.append(c) or (0, "", "")), \
             mock.patch.object(gf.time, "sleep", side_effect=lambda s: calls.append(["sleep", s])):
            ok = gf.play_game(ident, via_steam, dict(ST))
        return ok, calls

    def test_steam_running_launches_app_directly_no_steam_open(self):
        ok, calls = self.play([[STEAM_APP]])
        self.assertTrue(ok)
        self.assertEqual(calls, [["open", "-a", "/x/G.app"]])

    def test_bare_appid_means_steam(self):
        ok, calls = self.play([[STEAM_APP]], ident="9")
        self.assertTrue(ok)
        self.assertEqual(calls, [["open", "-a", "/x/G.app"]])

    def test_steam_not_running_opens_steam_in_background_waits_then_direct(self):
        ok, calls = self.play([[], [], [STEAM_APP]])
        self.assertEqual(calls[0], ["open", "-g", "-b", "com.valvesoftware.steam"])
        self.assertEqual(calls[-1], ["open", "-a", "/x/G.app"])
        self.assertIn(["sleep", ST["steam_settle"]], calls)
        self.assertNotIn(["open", "steam://rungameid/9"], calls)

    def test_via_steam_or_no_app_uses_url(self):
        _, calls = self.play([[STEAM_APP]], via_steam=True)
        self.assertEqual(calls, [["open", "steam://rungameid/9"]])
        _, calls = self.play([[STEAM_APP]], game={"id": "steam:9", "source": "Steam", "name": "G"})
        self.assertEqual(calls, [["open", "steam://rungameid/9"]])

    def test_non_steam_games_never_start_steam(self):
        gog = {"id": "gog:5", "source": "GOG", "name": "X", "app": "/g/X.app"}
        ok, calls = self.play([], game=gog, ident="gog:5")  # list_apps must not be called
        self.assertTrue(ok)
        self.assertEqual(calls, [["open", "-a", "/g/X.app"]])
        epic = {"id": "epic:Sugar", "source": "Epic Games", "name": "E",
                "uri": "com.epicgames.launcher://apps/Sugar?action=launch&silent=true"}
        _, calls = self.play([], game=epic, ident="epic:Sugar")
        self.assertEqual(calls, [["open", epic["uri"]]])

    def test_unknown_game_is_false(self):
        with mock.patch.object(gf, "library_games", return_value=[]):
            self.assertFalse(gf.play_game("steam:1", False, dict(ST)))


class Importers(unittest.TestCase):
    def test_epic_manifests(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.item"), "w") as f:
                json.dump({"DisplayName": "Fortnite-ish", "InstallLocation": "/e/F", "AppName": "Sugar",
                           "LaunchExecutable": "F.app"}, f)
            with open(os.path.join(d, "bad.item"), "w") as f:
                f.write("{not json")
            with open(os.path.join(d, "noname.item"), "w") as f:
                json.dump({"InstallLocation": "/x"}, f)
            games = gf.epic_games(d)
        self.assertEqual(len(games), 1)
        self.assertEqual((games[0]["id"], games[0]["source"], games[0]["name"]), ("epic:Sugar", "Epic Games", "Fortnite-ish"))
        self.assertEqual(games[0]["uri"], "com.epicgames.launcher://apps/Sugar?action=launch&silent=true")

    def test_gog_info_inside_app_bundle_and_plain_dir(self):
        with tempfile.TemporaryDirectory() as d:
            res = os.path.join(d, "Thing.app", "Contents", "Resources")
            os.makedirs(res)
            with open(os.path.join(res, "goggame-123.info"), "w") as f:
                json.dump({"gameId": "123", "name": "Thing"}, f)
            os.makedirs(os.path.join(d, "Plain"))
            with open(os.path.join(d, "Plain", "goggame-9.info"), "w") as f:
                json.dump({"gameId": "9", "name": "Plain Game"}, f)
            games = {g["id"]: g for g in gf.gog_games([d])}
        self.assertEqual(games["gog:123"]["app"], os.path.join(d, "Thing.app"))
        self.assertNotIn("app", games["gog:9"])

    def test_appstore_needs_game_category_and_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            def mk(name, cat, receipt):
                c = os.path.join(d, name + ".app", "Contents")
                os.makedirs(c)
                with open(os.path.join(c, "Info.plist"), "wb") as f:
                    plistlib.dump({"CFBundleName": name, "CFBundleIdentifier": "id." + name,
                                   **({"LSApplicationCategoryType": cat} if cat else {})}, f)
                if receipt:
                    os.makedirs(os.path.join(c, "_MASReceipt"))
                    open(os.path.join(c, "_MASReceipt", "receipt"), "w").close()
            mk("Good", "public.app-category.arcade-games", True)
            mk("NoReceipt", "public.app-category.games", False)
            mk("NotGame", "public.app-category.productivity", True)
            mk("NoCategory", None, True)
            games = gf.appstore_games([d])
        self.assertEqual([g["name"] for g in games], ["Good"])
        self.assertEqual(games[0]["app"], os.path.join(d, "Good.app"))

    def test_library_merges_sources_sorted_and_steam_gets_local_icon(self):
        steam = [{"appid": "1", "name": "Zed", "app": "/s/Zed.app"}]
        with mock.patch.object(gf, "steam_games", return_value=steam), \
             mock.patch.object(gf, "epic_games", return_value=[{"id": "epic:a", "source": "Epic Games", "name": "Alpha"}]), \
             mock.patch.object(gf, "gog_games", return_value=[]), \
             mock.patch.object(gf, "appstore_games", return_value=[]), \
             mock.patch.object(gf.os.path, "isfile", side_effect=lambda p: p.endswith("1/library_600x900.jpg")):
            lib = gf.library_games()
        self.assertEqual([g["id"] for g in lib], ["epic:a", "steam:1"])
        self.assertTrue(lib[1]["icon"].endswith("1/library_600x900.jpg"))
        self.assertNotIn("icon", lib[0])


class Watch(unittest.TestCase):
    def test_trigger_needs_consecutive_hits_and_resets_on_gap(self):
        counts = {}
        self.assertIsNone(gf.watch_tick(counts, {"g"}))          # 1st hit
        self.assertIsNone(gf.watch_tick(counts, set()))          # gap (alt-tab, flicker) resets
        self.assertIsNone(gf.watch_tick(counts, {"g"}))
        self.assertEqual(gf.watch_tick(counts, {"g"}), "g")      # 2 in a row

    def test_validate_rejects_watch_without_match(self):
        c = {"profiles": {"w": {"watch": True, "game": {}}}}
        self.assertEqual(len(gf.validate_config(c)), 1)

    def test_hits_use_one_ps_call_per_field_and_ignore_self(self):
        profiles = {"a": {"game": {"match": "steamapps/common/", "match_args": True}},
                    "b": {"game": {"match": "nothing-here", "match_args": True}},
                    "c": {"game": {"match": "cube"}}}
        out = {"pid=,command=": "10 /S/steamapps/common/G/G\n99 /x steamapps/common/self\n",
               "pid=,comm=": "11 /Apps/CubeThing\n"}
        calls = []
        def fake(cmd, timeout=30):
            calls.append(cmd[-1])
            return 0, out[cmd[-1]], ""
        with mock.patch.object(gf, "sh", side_effect=fake), mock.patch.object(gf.os, "getpid", return_value=99), \
             mock.patch.object(gf.os, "getppid", return_value=1):
            self.assertEqual(gf.watched_hits(profiles), {"a", "c"})
        self.assertEqual(sorted(calls), ["pid=,comm=", "pid=,command="])

    def test_wait_gone_ignores_single_flicker(self):
        seq = iter([{"g"}, set(), {"g"}, set(), set()])  # one-poll gap must not count as exit
        calls = []
        with mock.patch.object(gf, "watched_hits", side_effect=lambda p: calls.append(1) or next(seq)), \
             mock.patch.object(gf.time, "sleep"):
            gf.wait_gone({}, "g")
        self.assertEqual(len(calls), 5)


LIB = [{"id": "steam:1", "source": "Steam", "name": "One", "path": "/S/steamapps/common/One"},
       {"id": "epic:a", "source": "Epic Games", "name": "Alpha", "path": "/E/Alpha"},
       {"id": "gog:9", "source": "GOG", "name": "Nine"},                       # no path: cannot be matched
       {"id": "mas:x", "source": "App Store", "name": "X", "path": "/Applications/X.app"}]


class LibraryWatch(unittest.TestCase):
    def cfg(self, **lw):
        base = {"enabled": True, "profile": "lib", "sources": ["Steam", "Epic Games", "GOG", "App Store"], "exclude": []}
        base.update(lw)
        return {"keep": [], "settings": dict(ST), "library_watch": base, "profiles": {
            "lib": {"quit": [{"app": "Slack", "action": "pause"}]},
            "explicit": {"watch": True, "game": {"match": "x"}, "quit": []}}}

    def test_disabled_or_missing_profile_adds_nothing(self):
        self.assertEqual(list(gf.watch_profiles(self.cfg(enabled=False), LIB)), ["explicit"])
        self.assertEqual(list(gf.watch_profiles(self.cfg(profile="nope"), LIB)), ["explicit"])

    def test_every_matchable_library_game_gets_a_synthetic_profile_sharing_the_quit_list(self):
        p = gf.watch_profiles(self.cfg(), LIB)
        self.assertEqual(sorted(p), ["explicit", "lib:epic:a", "lib:mas:x", "lib:steam:1"])
        g = p["lib:steam:1"]
        self.assertEqual(g["game"], {"match": r"/S/steamapps/common/One", "match_args": True})
        self.assertEqual(g["quit"], [{"app": "Slack", "action": "pause"}])

    def test_sources_and_exclude_filter(self):
        p = gf.watch_profiles(self.cfg(sources=["Steam", "GOG"], exclude=["steam:1"]), LIB)
        self.assertEqual(list(p), ["explicit"])

    def test_path_with_regex_chars_is_escaped(self):
        lib = [{"id": "steam:2", "source": "Steam", "name": "T", "path": "/S/Game (2)+/x.app"}]
        p = gf.watch_profiles(self.cfg(), lib)["lib:steam:2"]
        self.assertTrue(gf.re.search(p["game"]["match"], "/S/Game (2)+/x.app/Contents/MacOS/x"))

    def test_validate_library_watch(self):
        c = {"profiles": {"lib": {"quit": []}}, "library_watch": {"enabled": True, "profile": "lib"}}
        self.assertEqual(gf.validate_config(c), [])
        c["library_watch"] = {"enabled": "yes", "profile": "nope", "sources": ["Epik"], "exclude": "x"}
        self.assertEqual(len(gf.validate_config(c)), 4)

    def test_library_games_carry_install_path(self):
        steam = [{"appid": "1", "name": "One", "app": "/S/steamapps/common/One/One.app", "path": "/S/steamapps/common/One"}]
        with mock.patch.object(gf, "steam_games", return_value=steam), \
             mock.patch.object(gf, "epic_games", return_value=[]), mock.patch.object(gf, "gog_games", return_value=[]), \
             mock.patch.object(gf, "appstore_games", return_value=[]):
            self.assertEqual(gf.library_games()[0]["path"], "/S/steamapps/common/One")

    def test_list_json_hides_protected_apps_and_our_own_ancestors(self):
        apps = [{"name": "Finder", "bundle": "com.apple.finder", "pid": 1},
                {"name": "Term", "bundle": "x.term", "pid": 2},
                {"name": "Slack", "bundle": "s.l", "pid": 3}]
        with mock.patch.object(gf, "list_apps", return_value=apps), mock.patch.object(gf, "ram_by_pid", return_value={}), \
             mock.patch.object(gf, "ancestor_pids", return_value={2}):
            self.assertEqual([a["name"] for a in gf.running_apps_json()], ["Slack"])

    def test_list_json_shape(self):
        apps = [{"name": "A", "bundle": "a.b", "pid": 5}]
        with mock.patch.object(gf, "list_apps", return_value=apps), mock.patch.object(gf, "ram_by_pid", return_value={5: 12.4}), \
             mock.patch.object(gf, "ancestor_pids", return_value=set()):
            self.assertEqual(gf.running_apps_json(), [{"name": "A", "bundle": "a.b", "ram_mb": 12}])


if __name__ == "__main__":
    unittest.main()
