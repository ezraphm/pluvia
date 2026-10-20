import argparse
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


class LaunchOptions(IsolatedHome):
    def test_steam_syntax_env_before_command_args_after(self):
        self.assertEqual(gf.parse_launch_options('FOO=1 BAR="a b" %command% -windowed -name "x y"'),
                         ({"FOO": "1", "BAR": "a b"}, ["-windowed", "-name", "x y"]))

    def test_zombiebuddy_style_option_keeps_the_double_dash(self):
        self.assertEqual(gf.parse_launch_options("-javaagent:ZombieBuddy.jar --"), ({}, ["-javaagent:ZombieBuddy.jar", "--"]))
        calls = self.play_cmd({"id": "steam:108600", "source": "Steam", "name": "PZ", "app": "/p/PZ.app"},
                              "-javaagent:ZombieBuddy.jar --")
        self.assertEqual(calls, [["open", "-a", "/p/PZ.app", "--args", "-javaagent:ZombieBuddy.jar", "--"]])

    def test_dry_run_prints_the_command_and_launches_nothing(self):
        game = {"id": "gog:5", "source": "GOG", "name": "X", "app": "/g/X dir/X.app"}
        out = []
        with mock.patch.object(gf, "library_games", return_value=[game]), mock.patch.object(gf, "sh") as sh, \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertTrue(gf.play_game("gog:5", False, dict(ST), "A=1 %command% -x", dry=True))
        sh.assert_not_called()
        self.assertEqual(out[-1], "open -a '/g/X dir/X.app' --env A=1 --args -x")
        self.assertTrue(all(o.startswith(("#", "open")) for o in out))  # nothing else, in particular no log lines

    def test_without_command_placeholder_everything_is_args(self):
        self.assertEqual(gf.parse_launch_options("-novid -console"), ({}, ["-novid", "-console"]))
        self.assertEqual(gf.parse_launch_options("  "), ({}, []))

    def test_wrappers_and_bad_names_are_rejected_with_a_clear_error(self):
        for bad in ("gamemoderun %command%", "1BAD=x %command%", 'unclosed "quote'):
            with self.assertRaises(ValueError):
                gf.parse_launch_options(bad)

    def play_cmd(self, game, opts, via_steam=False):
        calls = []
        with mock.patch.object(gf, "library_games", return_value=[game]), mock.patch.object(gf, "ensure_steam"), \
             mock.patch.object(gf, "sh", side_effect=lambda c, timeout=30: calls.append(c) or (0, "", "")):
            gf.play_game(game["id"], via_steam, dict(ST), opts)
        return calls

    def test_direct_launch_passes_env_and_args_to_open(self):
        calls = self.play_cmd({"id": "gog:5", "source": "GOG", "name": "X", "app": "/g/X.app"}, "FOO=1 %command% -w 800")
        self.assertEqual(calls, [["open", "-a", "/g/X.app", "--env", "FOO=1", "--args", "-w", "800"]])

    def test_steam_url_gets_args_urlencoded_env_is_not_supported(self):
        game = {"id": "steam:9", "source": "Steam", "name": "G"}
        calls = self.play_cmd(game, "FOO=1 %command% -name \"a b\"")
        self.assertEqual(calls, [["open", "steam://rungameid/9//-name%20%22a%20b%22"]])

    def test_epic_uri_ignores_options(self):
        game = {"id": "epic:a", "source": "Epic Games", "name": "E", "uri": "com.epicgames.launcher://apps/a"}
        self.assertEqual(self.play_cmd(game, "-x"), [["open", "com.epicgames.launcher://apps/a"]])

    def test_bad_options_do_not_launch_anything(self):
        game = {"id": "gog:5", "source": "GOG", "name": "X", "app": "/g/X.app"}
        self.assertEqual(self.play_cmd(game, "gamemoderun %command%"), [])

    def test_config_overrides_steam_local_options(self):
        cfg = {"launch_options": {"steam:9": "-mine"}}
        with mock.patch.object(gf, "steam_launch_option", return_value="-theirs"):
            self.assertEqual(gf.launch_options_for("steam:9", cfg), "-mine")
            self.assertEqual(gf.launch_options_for("steam:8", cfg), "-theirs")
            self.assertEqual(gf.launch_options_for("gog:1", cfg), "")

    def test_steam_localconfig_launch_options_are_read(self):
        vdf = '''"UserLocalConfigStore"
{
\t"Software"
\t{
\t\t"valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"apps"
\t\t\t\t{
\t\t\t\t\t"108600"
\t\t\t\t\t{
\t\t\t\t\t\t"LaunchOptions"\t\t"-Xmx4g %command%"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}'''
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "userdata", "123", "config")
            os.makedirs(d)
            with open(os.path.join(d, "localconfig.vdf"), "w") as f:
                f.write(vdf)
            self.assertEqual(gf.steam_launch_option("108600", root), "-Xmx4g %command%")
            self.assertEqual(gf.steam_launch_option("1", root), "")

    def test_validate_launch_options(self):
        ok = {"profiles": {}, "launch_options": {"steam:1": "A=1 %command% -x"}}
        self.assertEqual(gf.validate_config(ok), [])
        bad = {"profiles": {}, "launch_options": {"steam:1": "gamemoderun %command%", "x": 5}}
        self.assertEqual(len(gf.validate_config(bad)), 2)


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


PT = {  # pid: (ppid, uid, comm)
    10: (1, ME, "/Applications/Big.app/Contents/MacOS/Big"),
    11: (10, ME, "/Applications/Big.app/Contents/Helper"),
    20: (1, ME, "/opt/homebrew/bin/ollama"),
    21: (1, ME, "/opt/homebrew/bin/ollama"),
    22: (1, ME, "/opt/homebrew/bin/tinything"),
    23: (1, ME, "/usr/libexec/someagent"),
    24: (1, ME, "/System/Library/CoreServices/sys"),
    25: (1, 0, "/opt/homebrew/bin/rootthing"),
    26: (1, ME, "/Users/me/bin/mine"),
    27: (1, ME, "/Library/Frameworks/WindowServer"),
}
RSS = {10: 500, 11: 100, 20: 300, 21: 100, 22: 3, 23: 90, 24: 90, 25: 90, 26: 80, 27: 90}


class Processes(unittest.TestCase):
    def test_groups_background_user_processes_outside_apps(self):
        with mock.patch.object(gf, "proc_table", return_value=PT), mock.patch.object(gf, "rss_mb_by_pid", return_value=RSS), \
             mock.patch.object(gf, "ancestor_pids", return_value={26}), mock.patch.object(gf, "_self_pids", return_value={21}):
            apps = [{"name": "Big", "bundle": "b", "pid": 10}]
            groups = gf.process_groups(apps)
        # pid 21 is a gamefocus process (e.g. a running `watch`): never offered, so it can never be frozen
        self.assertEqual([(g["name"], g["ram_mb"], g["count"]) for g in groups], [("ollama", 300, 1)])
        self.assertEqual(groups[0]["kind"], "process")

    def test_apps_json_marks_kind_and_hides_apple_agents(self):
        apps = [{"name": "Safari", "bundle": "x.a", "pid": 1, "policy": 0},
                {"name": "SideNotes", "bundle": "x.sn", "pid": 2, "policy": 1},
                {"name": "ControlStrip", "bundle": "com.apple.cs", "pid": 3, "policy": 1}]
        with mock.patch.object(gf, "list_apps", return_value=apps), mock.patch.object(gf, "ram_by_pid", return_value={}), \
             mock.patch.object(gf, "ancestor_pids", return_value=set()), \
             mock.patch.object(gf, "proc_table", return_value={}):
            self.assertEqual([(a["name"], a["kind"]) for a in gf.running_apps_json()], [("Safari", "app"), ("SideNotes", "agent")])

    def test_agents_inside_another_app_and_our_own_app_are_hidden(self):
        apps = [{"name": "Aside", "bundle": "a.s", "pid": 10, "policy": 0},
                {"name": "Aside Daemon", "bundle": "a.d", "pid": 11, "policy": 1},
                {"name": "GameON", "bundle": "dev.gameon.menubar", "pid": 30, "policy": 1},
                {"name": "SideNotes", "bundle": "x.sn", "pid": 31, "policy": 1}]
        with mock.patch.object(gf, "list_apps", return_value=apps), mock.patch.object(gf, "ram_by_pid", return_value={}), \
             mock.patch.object(gf, "ancestor_pids", return_value=set()), mock.patch.object(gf, "proc_table", return_value=PT):
            self.assertEqual([a["name"] for a in gf.running_apps_json()], ["Aside", "SideNotes"])

    def test_list_apps_keeps_policy(self):
        out = '[{"name":"A","bundle":"a","pid":1,"policy":1}]'
        with mock.patch.object(gf, "sh", return_value=(0, out, "")):
            self.assertEqual(gf.list_apps()[0]["policy"], 1)

    def test_process_entries_normalize_and_validate(self):
        e = gf.norm_entries([{"process": "ollama", "action": "pause"}])[0]
        self.assertEqual((e["process"], e["action"]), ("ollama", "pause"))
        ok = {"profiles": {"p": {"quit": [{"process": "ollama", "action": "demote"}]}}}
        self.assertEqual(gf.validate_config(ok), [])
        bad = {"profiles": {"p": {"quit": [{"process": "ollama", "action": "quit"}, {"process": "x"}]}}}
        self.assertEqual(len(gf.validate_config(bad)), 2)   # processes cannot be quit; default action is quit

    def test_select_targets_handles_process_entries_by_name(self):
        with mock.patch.object(gf, "ancestor_pids", return_value=set()), mock.patch.object(gf, "ram_by_pid", return_value={}), \
             mock.patch.object(gf, "proc_table", return_value=PT), mock.patch.object(gf, "rss_mb_by_pid", return_value=RSS), \
             mock.patch.object(gf, "_self_pids", return_value=set()):
            t, notes = gf.select_targets([], gf.norm_entries([{"process": "ollama", "action": "pause"},
                                                                {"process": "ghost", "action": "pause"}]), [])
        self.assertEqual([(x["name"], x["action"], x["pids"], x["ram_mb"]) for x in t], [("ollama", "pause", [20, 21], 400)])
        self.assertIn("not running", notes[0])

    def test_system_looking_agent_is_protected_from_quit(self):
        apps = [{"name": "ControlStrip", "bundle": "com.apple.cs", "pid": 3, "policy": 1}]
        with mock.patch.object(gf, "ancestor_pids", return_value=set()), mock.patch.object(gf, "ram_by_pid", return_value={}):
            t, notes = gf.select_targets(apps, gf.norm_entries(["ControlStrip"]), [])
        self.assertEqual(t, [])
        self.assertIn("protected", notes[0])


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
             mock.patch.object(gf, "ancestor_pids", return_value={2}), \
             mock.patch.object(gf, "proc_table", return_value={}):
            self.assertEqual([a["name"] for a in gf.running_apps_json()], ["Slack"])

    def test_list_json_shape(self):
        apps = [{"name": "A", "bundle": "a.b", "pid": 5}]
        with mock.patch.object(gf, "list_apps", return_value=apps), mock.patch.object(gf, "ram_by_pid", return_value={5: 12.4}), \
             mock.patch.object(gf, "ancestor_pids", return_value=set()), \
             mock.patch.object(gf, "proc_table", return_value={}):
            self.assertEqual(gf.running_apps_json(), [{"name": "A", "bundle": "a.b", "ram_mb": 12, "kind": "app"}])


class Steamfix(IsolatedHome):
    """Every steamfix / networksetup / subprocess call is mocked: these tests must never touch the real DNS."""

    def setUp(self):
        super().setUp()
        import steamfix  # noqa: E402  (imported here on purpose: only this class needs it)
        self.sf = steamfix
        self.steam_state = gf.STEAMFIX_STATE  # before the patch, to compare with steamfix.STATE
        for name, val in (("CONFIG", os.path.join(self.tmp.name, "config.json")),
                          ("STEAMFIX_STATE", os.path.join(self.tmp.name, "saf-state.json"))):
            p = mock.patch.object(gf, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.state = gf.STEAMFIX_STATE
        p = mock.patch.object(gf, "STEAMFIX_AUTO_READY_WAIT", 0)  # tests never wait for a real apply
        p.start()
        self.addCleanup(p.stop)

    def write_config(self, **extra):
        cfg = {"settings": dict(ST), "keep": [], "profiles": {}}
        cfg.update(extra)
        os.makedirs(os.path.dirname(gf.CONFIG), exist_ok=True)
        with open(gf.CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def run_cmd(self, dns=None, dry=False, serve=None, apply_side=None, restore_side=None, auto_stop=False):
        """Run cmd_steamfix_run with the network side fully mocked; return (calls, printed lines)."""
        calls, out = [], []
        with mock.patch.object(self.sf, "active_service", return_value="Wi-Fi"), \
             mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch.object(self.sf, "pac_url", return_value="http://127.0.0.1:31627/proxy.pac"), \
             mock.patch.object(self.sf, "apply_network",
                               side_effect=apply_side or (lambda *a: calls.append(("apply",) + a))), \
             mock.patch.object(self.sf, "serve",
                               side_effect=serve or (lambda **k: calls.append(("serve", k)))), \
             mock.patch.object(self.sf, "restore_network",
                               side_effect=restore_side or (lambda: calls.append(("restore",)))), \
             mock.patch.object(gf.threading, "Thread"), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            gf.cmd_steamfix_run(argparse.Namespace(dns=dns, dry_run=dry, auto_stop=auto_stop))
        return calls, out

    def test_apply_then_serve_then_always_restore(self):
        calls, _ = self.run_cmd(dns="cloudflare")
        self.assertEqual(calls, [("apply", "Wi-Fi", "cloudflare", "http://127.0.0.1:31627/proxy.pac"),
                                 ("serve", {"dns": "cloudflare"}),
                                 ("restore",)])

    def test_auto_waits_for_the_state_file_before_returning(self):
        self.write_config(steamfix={"auto": True})
        with mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch.object(gf, "STEAMFIX_AUTO_READY_WAIT", 5), \
             mock.patch.object(gf.subprocess, "Popen",
                               side_effect=lambda *a, **k: open(gf.STEAMFIX_STATE, "w").close()), \
             mock.patch.object(gf.time, "sleep") as sl:
            self.assertIsNotNone(gf.start_steamfix_auto(gf.load_config(), logger=lambda m: None))
        sl.assert_not_called()  # state file already there: no polling

    def test_auto_gives_up_waiting_and_still_returns(self):
        self.write_config(steamfix={"auto": True})
        msgs = []
        with mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch.object(gf.subprocess, "Popen"), \
             mock.patch.object(gf.time, "sleep"):
            self.assertIsNotNone(gf.start_steamfix_auto(gf.load_config(), logger=msgs.append))
        self.assertTrue(any("starting Steam anyway" in m for m in msgs))

    def test_run_refuses_while_another_server_runs(self):
        with mock.patch.object(gf, "steamfix_running", return_value=[4242]):
            with self.assertRaises(SystemExit):
                self.run_cmd()

    def test_restore_runs_even_when_serve_raises(self):
        restored = []
        with mock.patch.object(self.sf, "active_service", return_value="Wi-Fi"), \
             mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch.object(self.sf, "pac_url", return_value="http://127.0.0.1:31627/proxy.pac"), \
             mock.patch.object(self.sf, "apply_network") as apply_n, \
             mock.patch.object(self.sf, "serve", side_effect=RuntimeError("proxy died")), \
             mock.patch.object(self.sf, "restore_network", side_effect=lambda: restored.append(1)):
            with self.assertRaises(RuntimeError):
                gf.cmd_steamfix_run(argparse.Namespace(dns="google", dry_run=False))
        apply_n.assert_called_once_with("Wi-Fi", "google", "http://127.0.0.1:31627/proxy.pac")
        self.assertEqual(restored, [1])

    def test_ctrl_c_during_serve_is_quiet_and_restores(self):
        calls, _ = self.run_cmd(dns="google", serve=lambda **k: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual([c[0] for c in calls], ["apply", "restore"])

    def test_dry_run_prints_the_plan_and_changes_nothing(self):
        calls, out = self.run_cmd(dns="google", dry=True)
        text = "\n".join(out)
        self.assertEqual(calls, [])
        self.assertIn("Wi-Fi", text)
        self.assertIn("8.8.8.8, 8.8.4.4 (google)", text)
        self.assertIn("http://127.0.0.1:31627/proxy.pac", text)
        self.assertIn("nothing changed", text)

    def test_config_dns_is_the_default_and_flag_wins(self):
        self.write_config(steamfix={"dns": "cloudflare"})
        calls, out = self.run_cmd(dns=None, dry=True)
        self.assertEqual(calls, [])
        self.assertIn("1.1.1.1, 1.0.0.1 (cloudflare)", "\n".join(out))
        calls, _ = self.run_cmd(dns="quad9")
        self.assertEqual(calls[0][2], "quad9")

    def test_fastest_dns_is_resolved_before_applying(self):
        self.write_config(steamfix={"dns": "fastest"})
        calls = []
        with mock.patch.object(self.sf, "fastest_dns", return_value=("quad9", [(1.0, "quad9")])), \
             mock.patch.object(self.sf, "active_service", return_value="Wi-Fi"), \
             mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch.object(self.sf, "pac_url", return_value="http://127.0.0.1:31627/proxy.pac"), \
             mock.patch.object(self.sf, "apply_network", side_effect=lambda *a: calls.append(a)), \
             mock.patch.object(self.sf, "serve"), mock.patch.object(self.sf, "restore_network"), \
             mock.patch("builtins.print"):
            gf.cmd_steamfix_run(argparse.Namespace(dns=None, dry_run=False))
        self.assertEqual(calls, [("Wi-Fi", "quad9", "http://127.0.0.1:31627/proxy.pac")])

    def test_unknown_dns_exits_before_touching_anything(self):
        with mock.patch.object(self.sf, "active_service", return_value="Wi-Fi"), \
             mock.patch.object(self.sf, "apply_network") as apply_n, mock.patch.object(self.sf, "serve") as serve:
            with self.assertRaises(SystemExit):
                gf.cmd_steamfix_run(argparse.Namespace(dns="nope", dry_run=False))
        apply_n.assert_not_called()
        serve.assert_not_called()

    def test_watchdog_restores_only_with_state_and_no_server(self):
        with mock.patch.object(self.sf, "restore_network", return_value="Wi-Fi") as restore, \
             mock.patch.object(gf, "steamfix_running", return_value=[]):
            self.assertIsNone(gf.steamfix_watchdog())          # no state file: nothing to do
            restore.assert_not_called()
            open(self.state, "w").close()
            self.assertEqual(gf.steamfix_watchdog(), "Wi-Fi")   # stale state, no server: undo it
            self.assertEqual(restore.call_count, 1)
        with mock.patch.object(self.sf, "restore_network") as restore, \
             mock.patch.object(gf, "steamfix_running", return_value=[4242]):
            self.assertIsNone(gf.steamfix_watchdog())          # server alive: leave it alone
            restore.assert_not_called()

    def test_watchdog_tolerates_a_failing_restore(self):
        open(self.state, "w").close()
        with mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch.object(self.sf, "restore_network", side_effect=ValueError("bad state")):
            self.assertIsNone(gf.steamfix_watchdog())

    def test_watchdogs_resume_freezes_then_steamfix(self):
        order = []
        with mock.patch.object(gf, "resume_frozen", side_effect=lambda: order.append("frozen") or 0), \
             mock.patch.object(gf, "steamfix_watchdog", side_effect=lambda: order.append("steamfix")):
            gf._watchdogs()
        self.assertEqual(order, ["frozen", "steamfix"])

    def test_running_uses_pgrep_and_never_matches_self(self):
        with mock.patch.object(gf, "sh", return_value=(0, "111\n222\n333\n", "")) as sh, \
             mock.patch.object(gf.os, "getpid", return_value=111), mock.patch.object(gf.os, "getppid", return_value=222):
            self.assertEqual(gf.steamfix_running(), [333])
        self.assertEqual(sh.call_args.args[0], ["pgrep", "-f", gf.STEAMFIX_PROC])

    def test_status_json_reports_whether_a_server_is_serving(self):
        out, st = [], {"active": True, "svc": "Wi-Fi", "dns": ["8.8.8.8", "8.8.4.4"]}
        with mock.patch.object(self.sf, "status", return_value=dict(st)), \
             mock.patch.object(gf, "steamfix_running", return_value=[42]), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            gf.cmd_steamfix_status(argparse.Namespace(json=True))
        self.assertEqual(json.loads(out[-1]), dict(st, serving=True))
        out = []
        with mock.patch.object(self.sf, "status", return_value={"active": False, "svc": None, "dns": []}), \
             mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            gf.cmd_steamfix_status(argparse.Namespace(json=False))
        self.assertIn("not applied", "\n".join(out))
        self.assertIn("serving: no", out[-1])

    def test_restore_command_is_safe_when_nothing_was_applied(self):
        out = []
        with mock.patch.object(self.sf, "restore_network", return_value=None) as restore, \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            gf.cmd_steamfix_restore(argparse.Namespace())
        restore.assert_called_once()
        self.assertIn("Nothing to restore", out[-1])

    def test_check_json_prints_the_verdict_and_exits_zero_when_ok(self):
        res = {"verdict": "ok", "system_ip": "203.0.113.5", "google_ip": "203.0.113.5", "detail": "fine"}
        out = []
        with mock.patch.object(self.sf, "check_steam", return_value=dict(res)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertIsNone(gf.cmd_steamfix_check(argparse.Namespace(json=True)))
        self.assertEqual(json.loads(out[-1]), dict(res, fix_active=False))

    def test_check_flags_an_active_fix_because_its_dns_skews_the_verdict(self):
        open(gf.STEAMFIX_STATE, "w").close()
        res = {"verdict": "dpi_blocked", "system_ip": "1.2.3.4", "google_ip": "1.2.3.4", "detail": "x"}
        out = []
        with mock.patch.object(self.sf, "check_steam", return_value=dict(res)), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            with self.assertRaises(SystemExit):
                gf.cmd_steamfix_check(argparse.Namespace(json=True))
        got = json.loads(out[-1])
        self.assertTrue(got["fix_active"])
        self.assertIn("Steam Fix is on", got["detail"])

    def test_check_exits_one_for_every_problem_verdict(self):
        for verdict in ("dns_poisoned", "dpi_blocked", "offline"):
            with self.subTest(verdict=verdict):
                res = {"verdict": verdict, "system_ip": "10.0.0.1", "google_ip": "203.0.113.5",
                       "detail": "%s because" % verdict}
                out = []
                with mock.patch.object(self.sf, "check_steam", return_value=dict(res)), \
                     mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                    with self.assertRaises(SystemExit) as cm:
                        gf.cmd_steamfix_check(argparse.Namespace(json=False))
                self.assertEqual(cm.exception.code, 1)
                text = "\n".join(out)
                self.assertIn("verdict: %s" % verdict, text)
                self.assertIn("10.0.0.1", text)
                self.assertIn("203.0.113.5 (8.8.8.8)", text)
                self.assertEqual("steamfix run" in text, verdict != "offline")  # no fix hint when offline

    def test_check_never_touches_the_network_settings(self):
        res = {"verdict": "ok", "system_ip": "203.0.113.5", "google_ip": "203.0.113.5", "detail": "fine"}
        with mock.patch.object(self.sf, "check_steam", return_value=dict(res)), \
             mock.patch.object(self.sf, "apply_network") as apply_n, \
             mock.patch.object(self.sf, "restore_network") as restore_n, \
             mock.patch.object(self.sf, "serve") as serve, mock.patch("builtins.print"):
            gf.cmd_steamfix_check(argparse.Namespace(json=True))
        apply_n.assert_not_called()
        restore_n.assert_not_called()
        serve.assert_not_called()

    def test_resume_all_runs_the_steamfix_watchdog(self):
        with mock.patch.object(gf, "resume_frozen", return_value=0), \
             mock.patch.object(gf, "steamfix_watchdog") as wd, \
             mock.patch.object(gf, "load_snapshot", return_value=None), mock.patch("builtins.print"):
            gf.cmd_resume_all(argparse.Namespace())
        wd.assert_called_once()

    def test_validate_steamfix_key(self):
        base = {"profiles": {}}
        self.assertEqual(gf.validate_config(dict(base, steamfix={"dns": "google"})), [])
        self.assertEqual(gf.validate_config(dict(base, steamfix={"dns": "fastest"})), [])
        self.assertEqual(gf.validate_config(dict(base, steamfix={})), [])
        self.assertEqual(len(gf.validate_config(dict(base, steamfix={"dns": "nope"}))), 1)
        self.assertEqual(len(gf.validate_config(dict(base, steamfix={"dns": 5}))), 1)
        self.assertEqual(len(gf.validate_config(dict(base, steamfix="google"))), 1)
        self.assertIn("steamfix.dns", gf.validate_config(dict(base, steamfix={"dns": "nope"}))[0])

    def test_validate_steamfix_auto_is_a_bool(self):
        base = {"profiles": {}}
        for auto in (True, False):
            self.assertEqual(gf.validate_config(dict(base, steamfix={"auto": auto})), [])
        self.assertEqual(gf.validate_config(dict(base, steamfix={"dns": "google", "auto": True})), [])
        for bad in (1, 0, "true", [], {}):
            errs = gf.validate_config(dict(base, steamfix={"auto": bad}))
            self.assertEqual(len(errs), 1, bad)
            self.assertIn("steamfix.auto", errs[0])

    def test_mirrored_constants_match_steamfix(self):
        self.assertEqual(self.steam_state, self.sf.STATE)
        self.assertEqual(set(gf.STEAMFIX_DNS_SETS), set(self.sf.DNS_SETS))

    def test_cli_parses_the_steamfix_subcommands(self):
        for argv in (["steamfix", "run", "--dry-run"], ["steamfix", "run", "--dns", "quad9"],
                     ["steamfix", "status", "--json"], ["steamfix", "restore"],
                     ["steamfix", "check", "--json"], ["steamfix", "check"]):
            with mock.patch.object(sys, "argv", ["gamefocus.py"] + argv), \
                 mock.patch.object(gf, "cmd_steamfix_run") as run, \
                 mock.patch.object(gf, "cmd_steamfix_status") as status, \
                 mock.patch.object(gf, "cmd_steamfix_check") as check, \
                 mock.patch.object(gf, "cmd_steamfix_restore") as restore:
                gf.main()
            self.assertEqual(run.call_count + status.call_count + restore.call_count + check.call_count, 1)
        with mock.patch.object(sys, "argv", ["gamefocus.py", "steamfix", "check", "--json"]), \
             mock.patch.object(gf, "cmd_steamfix_check") as check:
            gf.main()
        self.assertTrue(check.call_args.args[0].json)

    def test_cli_run_accepts_auto_stop(self):
        with mock.patch.object(sys, "argv", ["gamefocus.py", "steamfix", "run", "--auto-stop"]), \
             mock.patch.object(gf, "cmd_steamfix_run") as run:
            gf.main()
        self.assertTrue(run.call_args.args[0].auto_stop)
        with mock.patch.object(sys, "argv", ["gamefocus.py", "steamfix", "run"]), \
             mock.patch.object(gf, "cmd_steamfix_run") as run:
            gf.main()
        self.assertFalse(run.call_args.args[0].auto_stop)

    def test_auto_stop_starts_the_watcher_thread_before_serving(self):
        calls = []
        with mock.patch.object(self.sf, "active_service", return_value="Wi-Fi"), \
             mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch.object(self.sf, "pac_url", return_value="http://127.0.0.1:31627/proxy.pac"), \
             mock.patch.object(self.sf, "apply_network", side_effect=lambda *a: calls.append("apply")), \
             mock.patch.object(self.sf, "serve", side_effect=lambda **k: calls.append("serve")), \
             mock.patch.object(self.sf, "restore_network", side_effect=lambda: calls.append("restore")), \
             mock.patch.object(gf.threading, "Thread") as thread, \
             mock.patch("builtins.print"):
            gf.cmd_steamfix_run(argparse.Namespace(dns="google", dry_run=False, auto_stop=True))
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs.get("target"), gf._auto_stop_thread)
        self.assertTrue(thread.return_value.start.called)
        self.assertEqual(calls, ["apply", "serve", "restore"])

    def test_without_auto_stop_no_watcher_thread_runs(self):
        with mock.patch.object(self.sf, "active_service", return_value="Wi-Fi"), \
             mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch.object(self.sf, "pac_url", return_value="http://127.0.0.1:31627/proxy.pac"), \
             mock.patch.object(self.sf, "apply_network"), mock.patch.object(self.sf, "serve"), \
             mock.patch.object(self.sf, "restore_network"), \
             mock.patch.object(gf.threading, "Thread") as thread, mock.patch("builtins.print"):
            gf.cmd_steamfix_run(argparse.Namespace(dns="google", dry_run=False, auto_stop=False))
            gf.cmd_steamfix_run(argparse.Namespace(dns="google", dry_run=False))  # old namespaces keep working
        thread.assert_not_called()

    def test_watcher_thread_swallows_errors_so_the_restore_still_happens(self):
        sf = type("FakeSteamfix", (), {"stop": lambda self: None})()
        with mock.patch.object(gf, "steamfix_auto_stop", side_effect=RuntimeError("pgrep vanished")), \
             mock.patch.object(gf, "log") as log:
            self.assertIsNone(gf._auto_stop_thread(sf))
        self.assertIn("pgrep vanished", log.call_args.args[0])


class FakeClock:
    """Monotonic clock whose sleep() advances time: the auto-stop loop runs in microseconds in tests."""

    def __init__(self, limit=None):
        self.t = 0.0
        self.limit = limit

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        if self.limit is not None and self.t > self.limit:
            raise AssertionError("auto-stop never stopped (t=%s)" % self.t)


class SteamfixAutoStop(IsolatedHome):
    """`steamfix run --auto-stop`: the server stops itself once Steam is gone, so DNS/proxy never outlive it."""

    def watched(self, running_until, running_again=None, **kw):
        clock = FakeClock(limit=kw.pop("limit", 1000))
        stopped = []

        def running():
            if running_again is not None and clock.t >= running_again:
                return True
            return clock.t < running_until

        with mock.patch.object(gf, "log"):
            reason = gf.steamfix_auto_stop(lambda: stopped.append(clock.t), running=running,
                                           now=clock.now, sleep=clock.sleep, **kw)
        return reason, stopped

    def test_stops_60s_after_steam_exits(self):
        reason, stopped = self.watched(running_until=10)
        self.assertEqual(reason, "steam-exited")
        self.assertEqual(stopped, [70])  # 10s of Steam, gone at the first poll, +60s grace

    def test_stops_after_120s_when_steam_never_appears(self):
        reason, stopped = self.watched(running_until=0)
        self.assertEqual(reason, "no-steam")
        self.assertEqual(stopped, [120])

    def test_never_stops_while_steam_keeps_running(self):
        clock = FakeClock(limit=500)
        with mock.patch.object(gf, "log"), self.assertRaises(AssertionError):
            gf.steamfix_auto_stop(lambda: self.fail("stopped while Steam was running"), running=lambda: True,
                                  now=clock.now, sleep=clock.sleep)
        self.assertGreater(clock.t, 120)  # it really kept polling past both deadlines

    def test_steam_restarting_resets_the_grace_period(self):
        clock = FakeClock(limit=1000)
        stopped = []
        with mock.patch.object(gf, "log"):
            gf.steamfix_auto_stop(lambda: stopped.append(clock.t), gone_after=25, start_timeout=999,
                                  poll=5, now=clock.now, sleep=clock.sleep,
                                  running=lambda: (clock.t < 10) or (30 <= clock.t < 40))
        # gone 10..30 (20s, under the grace) -> Steam is back at 30 -> gone again at 40 -> stop at 65
        self.assertEqual(stopped, [65])

    def test_constants_are_the_documented_ones(self):
        self.assertEqual(gf.STEAMFIX_AUTO_STOP_GONE, 60)
        self.assertEqual(gf.STEAMFIX_AUTO_STOP_START, 120)
        self.assertEqual(gf.STEAM_PROC, "steam_osx")


class SteamfixAutoStart(IsolatedHome):
    """`play` with steamfix.auto: one detached `steamfix run --auto-stop`, and never a real Popen here."""

    CFG = {"steamfix": {"auto": True}}

    def setUp(self):
        super().setUp()
        # Never read the real /var/tmp state file (it exists while Steam Fix runs) and never wait for it.
        for name, val in (("STEAMFIX_STATE", os.path.join(self.tmp.name, "saf-state.json")),
                          ("STEAMFIX_AUTO_READY_WAIT", 0)):
            p = mock.patch.object(gf, name, val)
            p.start()
            self.addCleanup(p.stop)

    def start(self, cfg=None, dry=False, running=(), popen=None):
        log = mock.Mock()
        with mock.patch.object(gf, "steamfix_running", return_value=list(running)), \
             mock.patch.object(gf.subprocess, "Popen", side_effect=popen) as popen_mock:
            cmd = gf.start_steamfix_auto(self.CFG if cfg is None else cfg, dry=dry, logger=log)
        return cmd, popen_mock, log

    def test_auto_off_never_starts_anything(self):
        for cfg in ({}, {"steamfix": {}}, {"steamfix": {"auto": False}}, {"steamfix": {"dns": "google"}}):
            cmd, popen, _ = self.start(cfg=cfg)
            self.assertIsNone(cmd)
            popen.assert_not_called()

    def test_auto_on_starts_a_detached_auto_stop_server(self):
        cmd, popen, log = self.start()
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:], [os.path.abspath(gf.__file__), "steamfix", "run", "--auto-stop"])
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(cmd, argv)
        self.assertTrue(any("starting Steam Fix" in c.args[0] for c in log.call_args_list))

    def test_existing_server_is_reused_not_duplicated(self):
        cmd, popen, log = self.start(running=[4242])
        self.assertIsNone(cmd)
        popen.assert_not_called()
        self.assertIn("4242", log.call_args.args[0])

    def test_dry_run_prints_the_command_and_starts_nothing(self):
        out = []
        with mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch.object(gf.subprocess, "Popen") as popen, \
             mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            gf.start_steamfix_auto(self.CFG, dry=True)
        popen.assert_not_called()
        self.assertIn("steamfix run --auto-stop", out[-1])

    def test_popen_failure_is_logged_and_swallowed(self):
        cmd, popen, log = self.start(popen=OSError("no such file"))
        self.assertIsNone(cmd)
        self.assertIn("could not start Steam Fix", log.call_args.args[0])

    def test_play_starts_the_fix_before_steam_and_the_game(self):
        calls = []
        with mock.patch.object(gf, "library_games", return_value=[dict(GAME)]), \
             mock.patch.object(gf, "steamfix_running", return_value=[]), \
             mock.patch.object(gf, "list_apps", return_value=[STEAM_APP]), \
             mock.patch.object(gf.subprocess, "Popen", side_effect=lambda *a, **k: calls.append("steamfix")), \
             mock.patch.object(gf, "sh", side_effect=lambda c, timeout=30: calls.append(c) or (0, "", "")):
            self.assertTrue(gf.play_game("steam:9", False, dict(ST), cfg=self.CFG))
        self.assertEqual(calls[0], "steamfix")
        self.assertEqual(calls[-1], ["open", "-a", "/x/G.app"])

    def test_non_steam_games_do_not_start_the_fix(self):
        gog = {"id": "gog:5", "source": "GOG", "name": "X", "app": "/g/X.app"}
        with mock.patch.object(gf, "library_games", return_value=[gog]), \
             mock.patch.object(gf.subprocess, "Popen") as popen, \
             mock.patch.object(gf, "sh", return_value=(0, "", "")):
            self.assertTrue(gf.play_game("gog:5", False, dict(ST), cfg=self.CFG))
        popen.assert_not_called()

    def test_cmd_play_hands_the_config_to_play_game(self):
        with mock.patch.object(gf, "load_config", return_value=dict(self.CFG, settings=dict(ST), profiles={})), \
             mock.patch.object(gf, "play_game", return_value=True) as play:
            gf.cmd_play(argparse.Namespace(game="9", via_steam=False, dry_run=True))
        self.assertEqual(play.call_args.kwargs["cfg"]["steamfix"], {"auto": True})


if __name__ == "__main__":
    unittest.main()
