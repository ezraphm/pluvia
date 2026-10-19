#!/usr/bin/env python3
"""
gamefocus - free up RAM on a low-memory Mac (e.g. M1 8GB) while gaming,
then bring your apps back gently once the game exits.

Standard library only. macOS only for real use (uses osascript, ps, top, open, sysctl).

Typical workflow
    python3 gamefocus.py init                 # create ~/.gamefocus/config.json
    python3 gamefocus.py list                 # see running apps + approx RAM
    python3 gamefocus.py find "minecraft"     # test your game-process regex
    # edit ~/.gamefocus/config.json  (profiles -> quit list, game match)
    python3 gamefocus.py run mygame --dry-run # preview, changes nothing
    python3 gamefocus.py run mygame           # quit apps, play, restore after exit
    python3 gamefocus.py restore              # crash recovery: restore last snapshot

Safety rules built in
  * Apps are asked to quit politely (AppleScript "quit"). Never force-killed.
    If an app refuses (e.g. unsaved-document dialog) it is left running and reported.
  * Only apps listed in the profile are touched (opt-in, not "quit everything").
  * Finder, terminals and the terminal running this script are never quit.
  * A snapshot is written BEFORE quitting, so `restore` works even after a crash.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

HOME_DIR = os.path.expanduser("~/.gamefocus")
CONFIG = os.path.join(HOME_DIR, "config.json")
SNAPSHOT = os.path.join(HOME_DIR, "snapshot.json")
LOGFILE = os.path.join(HOME_DIR, "gamefocus.log")

NEVER_QUIT = set(b.lower() for b in [
    "com.apple.finder",
    "com.apple.dock",
    "com.apple.systemuiserver",
    "com.apple.controlcenter",
    "com.apple.loginwindow",
    "com.apple.Terminal",
    "com.googlecode.iterm2",
    "dev.warp.Warp-Stable",
    "com.mitchellh.ghostty",
    "net.kovidgoyal.kitty",
    "org.alacritty",
])

DEFAULT_SETTINGS = {
    "quit_timeout": 20,             # seconds to wait for apps to quit politely
    "start_timeout": 180,           # seconds to wait for the game process to appear
    "restore_grace_after_game": 3,  # seconds to wait after game exits before restoring
    "restore_min_delay": 5,         # min seconds between launching "auto" apps
    "restore_late_min_delay": 15,   # min seconds after launching "late" (heavy) apps
    "restore_max_wait": 90,         # max seconds to wait for the Mac to calm down per app
    "restore_cpu_idle_pct": 60,     # CPU must be at least this idle before next launch
    "steam_start_timeout": 30,      # seconds to wait for the Steam client to appear when `play` has to start it
    "steam_settle": 5,              # seconds to let Steam finish starting before launching the game
    "restore_min_free_pct": 20,     # `memory_pressure` free % must be at least this (8GB Macs idle at pressure level 2)
}

RESTORE_MODES = ("auto", "late", "manual")
ACTIONS = ("keep", "demote", "pause", "quit")

# Apps that must keep running at full speed while gaming: voice chat, meetings, music, downloads,
# and the game launcher. pause/demote on these is refused (an explicit `quit` is still the user's call).
FREEZE_SAFE = set(b.lower() for b in [
    "com.valvesoftware.steam", "com.hnc.Discord", "us.zoom.xos", "com.microsoft.teams",
    "com.microsoft.teams2", "com.apple.FaceTime", "com.tinyspeck.slackmacgap",
    "com.spotify.client", "com.apple.Music", "com.apple.podcasts", "com.obsproject.obs-studio",
    "org.m0k.transmission", "org.qbittorrent.qBittorrent", "com.epicgames.EpicGamesLauncher",
    "net.battle.app", "com.apple.TimeMachine",
])

# Never signal or re-prioritize these, even when they appear inside an app's process tree.
SYSTEM_PROCS = set(n.lower() for n in [
    "WindowServer", "coreaudiod", "audiomxd", "launchd", "loginwindow", "Dock", "Finder",
    "kernel_task", "SystemUIServer", "ControlCenter", "WindowManager", "bluetoothd", "hidd",
    "powerd", "logd", "notifyd", "cfprefsd", "distnoted", "usernoted", "coreservicesd",
    "tccd", "securityd", "trustd", "mds", "mds_stores", "UserEventAgent", "universalaccessd",
])

DEFAULT_CONFIG = {
    "_help": [
        "keep: apps (name or bundle id) that must never be quit, in any profile.",
        "profiles.<name>.game.match: regex matched against the game's executable path (ps comm).",
        "  set match_args=true to match against full command line instead (useful for Wine/GPTK).",
        "profiles.<name>.game.launch: app name to open with `open -a`, or launch_cmd: [\"cmd\", \"arg\"].",
        "  Leave both out to just wait until you start the game yourself.",
        "profiles.<name>.quit: apps to quit. restore = auto | late | manual.",
        "  auto = relaunch first, late = relaunch last (heavy apps), manual = never relaunch, just list.",
        "Use `python3 gamefocus.py list` to see exact app names and bundle ids.",
    ],
    "settings": DEFAULT_SETTINGS,
    "keep": [],
    "profiles": {
        "example": {
            "game": {"launch": "Minecraft", "match": "minecraft", "match_args": False},
            "quit": [
                {"app": "Slack", "restore": "auto"},
                {"app": "Google Chrome", "restore": "late"},
                {"app": "Notion", "restore": "manual"},
            ],
        }
    },
}

JXA_LIST = r'''
ObjC.import("AppKit");
var apps = $.NSWorkspace.sharedWorkspace.runningApplications;
var out = [];
for (var i = 0; i < apps.count; i++) {
  var a = apps.objectAtIndex(i);
  if (a.activationPolicy != 0) continue;
  out.push({name: ObjC.unwrap(a.localizedName),
            bundle: ObjC.unwrap(a.bundleIdentifier),
            pid: a.processIdentifier});
}
JSON.stringify(out);
'''

AS_LIST = '''
tell application "System Events"
    set out to ""
    repeat with p in (every process whose background only is false)
        set b to bundle identifier of p
        if b is missing value then set b to ""
        set out to out & (name of p) & "||" & b & "||" & (unix id of p) & linefeed
    end repeat
    return out
end tell
'''


# ----------------------------------------------------------------- helpers
def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(HOME_DIR, exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def list_apps():
    """Running regular (Dock) apps: [{name, bundle, pid}]."""
    rc, out, _ = sh(["osascript", "-l", "JavaScript", "-e", JXA_LIST])
    if rc == 0 and out:
        try:
            return [
                {"name": a.get("name") or "?", "bundle": a.get("bundle") or None, "pid": int(a["pid"])}
                for a in json.loads(out) if "pid" in a
            ]
        except (ValueError, KeyError, TypeError):
            pass
    rc, out, err = sh(["osascript", "-e", AS_LIST])
    if rc != 0:
        sys.exit("Could not list apps (osascript failed: %s). Are you on macOS?" % err[:200])
    apps = []
    for line in out.splitlines():
        parts = line.split("||")
        if len(parts) == 3 and parts[2].strip().isdigit():
            apps.append({"name": parts[0], "bundle": parts[1] or None, "pid": int(parts[2])})
    return apps


def ram_by_pid():
    """Approx RAM (MB) per pid including all descendant processes (helpers)."""
    rc, out, _ = sh(["ps", "-axo", "pid=,ppid=,rss="])
    rss, kids = {}, {}
    for line in out.splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        try:
            pid, ppid, r = int(p[0]), int(p[1]), int(p[2])
        except ValueError:
            continue
        rss[pid] = r
        kids.setdefault(ppid, []).append(pid)
    totals = {}
    for pid in rss:
        total, stack, seen = 0, [pid], set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            total += rss.get(cur, 0)
            stack.extend(kids.get(cur, []))
        totals[pid] = total / 1024.0
    return totals


def proc_table():
    """{pid: (ppid, uid, comm)} for every process."""
    rc, out, _ = sh(["ps", "-axo", "pid=,ppid=,uid=,comm="])
    table = {}
    for line in out.splitlines():
        p = line.split(None, 3)
        if len(p) == 4 and p[0].isdigit() and p[1].isdigit() and p[2].isdigit():
            table[int(p[0])] = (int(p[1]), int(p[2]), p[3])
    return table


def process_tree(pid, table, skip=()):
    """The app's pid plus descendants, minus other users' and system processes, minus `skip`."""
    kids = {}
    for p, (pp, _, _) in table.items():
        kids.setdefault(pp, []).append(p)
    out, stack, seen = [], [pid], set()
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in table:
            continue
        seen.add(cur)
        stack.extend(kids.get(cur, []))
        _, uid, comm = table[cur]
        if uid == os.getuid() and os.path.basename(comm).lower() not in SYSTEM_PROCS \
                and cur != os.getpid() and cur not in skip:
            out.append(cur)
    return sorted(out)


def ancestor_pids():
    pids, pid = set(), os.getpid()
    for _ in range(30):
        rc, out, _ = sh(["ps", "-o", "ppid=", "-p", str(pid)])
        try:
            pid = int(out.strip())
        except ValueError:
            break
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
    return pids


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def send_quit(app):
    if app.get("bundle"):
        target = 'application id "%s"' % app["bundle"]
    else:
        target = 'application "%s"' % app["name"].replace('"', '\\"')
    rc, _, err = sh(["osascript", "-e", "tell %s to quit" % target], timeout=15)
    if rc != 0:
        log("  could not ask %s to quit: %s" % (app["name"], err[:120]))
    return rc == 0


def open_app(entry):
    cmd = ["open", "-g"]  # -g = do not steal focus
    cmd += ["-b", entry["bundle"]] if entry.get("bundle") else ["-a", entry["name"]]
    rc, _, err = sh(cmd, timeout=20)
    if rc != 0:
        log("  could not open %s: %s" % (entry["name"], err[:120]))
    return rc == 0


def mem_pressure():
    """1 = normal, 2 = warning, 4 = critical (kern.memorystatus_vm_pressure_level)."""
    rc, out, _ = sh(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    try:
        return int(out)
    except ValueError:
        return 1


def mem_free_pct():
    """System-wide free memory % from `memory_pressure`, or None if unreadable."""
    rc, out, _ = sh(["memory_pressure"])
    m = re.search(r"free percentage:\s*(\d+)%", out) if rc == 0 else None
    return int(m.group(1)) if m else None


def swap_used_mb():
    """Swap in use (MB) from `sysctl vm.swapusage`, or None if unreadable."""
    rc, out, _ = sh(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used = ([\d.]+)([MG])", out) if rc == 0 else None
    return round(float(m.group(1)) * (1024 if m.group(2) == "G" else 1)) if m else None


def mem_line(label):
    """One measured (not estimated) memory reading for the log."""
    free, swap = mem_free_pct(), swap_used_mb()
    return "memory %s: free %s%%, swap used %s MB, pressure %d" % (
        label, "?" if free is None else free, "?" if swap is None else swap, mem_pressure())


def cpu_idle_pct():
    rc, out, _ = sh(["top", "-l", "2", "-n", "0", "-s", "1"], timeout=25)
    lines = [l for l in out.splitlines() if l.startswith("CPU usage")]
    if lines:
        m = re.search(r"([\d.]+)% idle", lines[-1])
        if m:
            return float(m.group(1))
    return 100.0


def is_calm(st):
    """Calm = pressure not critical, enough free memory, CPU mostly idle.
    Pressure level 2 (warning) is the idle baseline on 8GB Macs, so it must not block."""
    if mem_pressure() >= 4:
        return False
    free = mem_free_pct()
    if free is not None and free < st["restore_min_free_pct"]:
        return False
    return cpu_idle_pct() >= st["restore_cpu_idle_pct"]


def wait_until_calm(st):
    start = time.time()
    while True:
        if is_calm(st):
            return True
        if time.time() - start > st["restore_max_wait"]:
            return False
        time.sleep(2)


def find_game_pids(match, match_args=False):
    field = "command=" if match_args else "comm="
    rc, out, _ = sh(["ps", "-axo", "pid=," + field])
    rx = re.compile(match, re.I)
    me = {os.getpid(), os.getppid()}
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid not in me and rx.search(parts[1]):
            pids.append(pid)
    return pids


# ------------------------------------------------------------ config / state
def validate_config(cfg):
    """Return a list of human-readable problems (empty = OK)."""
    errs = []
    if not isinstance(cfg, dict):
        return ["config.json must be a JSON object"]
    st = cfg.get("settings", {})
    if not isinstance(st, dict):
        errs.append("settings must be an object")
        st = {}
    for k, v in st.items():
        if k in DEFAULT_SETTINGS and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            errs.append("settings.%s must be a non-negative number, got %r" % (k, v))
    if not isinstance(cfg.get("keep", []), list):
        errs.append("keep must be a list of app names / bundle ids")
    profiles = cfg.get("profiles", {})
    if not isinstance(profiles, dict):
        return errs + ["profiles must be an object"]
    for name, prof in profiles.items():
        where = "profiles.%s" % name
        if not isinstance(prof, dict):
            errs.append("%s must be an object" % where)
            continue
        game = prof.get("game", {})
        match = game.get("match") if isinstance(game, dict) else None
        if match is not None:
            try:
                re.compile(match)
            except (re.error, TypeError) as e:
                errs.append("%s.game.match is not a valid regex: %s" % (where, e))
        if prof.get("watch") and not match:
            errs.append("%s: watch=true needs game.match" % where)
        quit_list = prof.get("quit", [])
        if not isinstance(quit_list, list):
            errs.append("%s.quit must be a list" % where)
            continue
        for i, e in enumerate(quit_list):
            if isinstance(e, str):
                continue
            if not isinstance(e, dict) or not isinstance(e.get("app"), str) or not e["app"]:
                errs.append("%s.quit[%d] needs an \"app\" name or bundle id" % (where, i))
            elif e.get("action", "quit") not in ACTIONS:
                errs.append("%s.quit[%d] (%s): action=%r, must be one of %s"
                            % (where, i, e["app"], e["action"], "/".join(ACTIONS)))
            elif e.get("restore", "auto") not in RESTORE_MODES:
                errs.append("%s.quit[%d] (%s): restore=%r, must be one of %s"
                            % (where, i, e["app"], e["restore"], "/".join(RESTORE_MODES)))
    return errs


def load_config():
    if not os.path.exists(CONFIG):
        sys.exit("No config yet. Run:  python3 gamefocus.py init")
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except ValueError as e:
        sys.exit("%s is not valid JSON: %s" % (CONFIG, e))
    errs = validate_config(cfg)
    if errs:
        sys.exit("Config problems in %s:\n  - %s" % (CONFIG, "\n  - ".join(errs)))
    st = cfg.setdefault("settings", {})
    for k, v in DEFAULT_SETTINGS.items():
        st.setdefault(k, v)
    cfg.setdefault("keep", [])
    cfg.setdefault("profiles", {})
    return cfg


def norm_entries(raw):
    out = []
    for e in raw:
        if isinstance(e, str):
            e = {"app": e}
        out.append({"app": e["app"], "restore": e.get("restore", "auto"), "action": e.get("action", "quit")})
    return out


def app_matches(app, token):
    t = token.lower()
    return t == (app.get("bundle") or "").lower() or t == app["name"].lower()


def save_snapshot(profile, entries):
    os.makedirs(HOME_DIR, exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump({"profile": profile, "created": datetime.now().isoformat(timespec="seconds"),
                   "apps": entries}, f, indent=2)


def load_snapshot():
    if not os.path.exists(SNAPSHOT):
        return None
    with open(SNAPSHOT, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------- core
def select_targets(apps, quit_entries, keep):
    protected = set(NEVER_QUIT) | set(k.lower() for k in keep)
    ancestors = ancestor_pids()
    ram = ram_by_pid()
    table = None
    targets, notes = [], []
    for e in quit_entries:
        found = [a for a in apps if app_matches(a, e["app"])]
        if not found:
            notes.append("%s: not running, skipped" % e["app"])
            continue
        a = found[0]
        bundle = (a.get("bundle") or "").lower()
        if bundle in protected or a["name"].lower() in protected or a["pid"] in ancestors:
            notes.append("%s: protected, will NOT be touched" % a["name"])
            continue
        action = e["action"]
        if action == "keep":
            notes.append("%s: keep, left alone" % a["name"])
            continue
        t = {"name": a["name"], "bundle": a.get("bundle"), "pid": a["pid"], "action": action,
             "restore": e["restore"], "ram_mb": round(ram.get(a["pid"], 0))}
        if action in ("pause", "demote"):
            if bundle in FREEZE_SAFE:
                notes.append("%s: on the safelist (voice/meeting/music/download/launcher), %s refused"
                             % (a["name"], action))
                continue
            table = table or proc_table()
            t["pids"] = process_tree(a["pid"], table, skip=ancestors)
            if not t["pids"]:
                notes.append("%s: no eligible processes to %s" % (a["name"], action))
                continue
        targets.append(t)
    return targets, notes


def apply_action(t, undo=False):
    """pause: SIGSTOP/SIGCONT. demote: taskpolicy -b/-B (E-cores, low IO). quit: nothing to do here."""
    act = t.get("action", "quit")
    for pid in t.get("pids", []):
        if act == "pause":
            try:
                os.kill(pid, signal.SIGCONT if undo else signal.SIGSTOP)
            except (ProcessLookupError, PermissionError):
                pass
        elif act == "demote":
            sh(["taskpolicy", "-B" if undo else "-b", "-p", str(pid)])


def undo_action(t):
    apply_action(t, undo=True)


def is_freeze(e):
    return e.get("action", "quit") in ("pause", "demote")  # old snapshots have no action == quit


def resume_frozen():
    """Undo every pause/demote recorded in the snapshot; keep quit entries for `restore`. Returns count."""
    snap = load_snapshot()
    if not snap:
        return 0
    frozen = [e for e in snap["apps"] if is_freeze(e)]
    for e in frozen:
        undo_action(e)
    if frozen:
        rest = [e for e in snap["apps"] if not is_freeze(e)]
        if rest:
            save_snapshot(snap.get("profile"), rest)
        elif os.path.exists(SNAPSHOT):
            os.remove(SNAPSHOT)
    return len(frozen)


def quit_targets(targets, timeout):
    for t in targets:
        log("asking %s to quit..." % t["name"])
        send_quit(t)
    deadline = time.time() + timeout
    while time.time() < deadline and any(pid_alive(t["pid"]) for t in targets):
        time.sleep(1)
    done = [t for t in targets if not pid_alive(t["pid"])]
    stuck = [t for t in targets if pid_alive(t["pid"])]
    for t in stuck:
        log("  %s is still running (unsaved document dialog?). Left as-is." % t["name"])
    return done, stuck


def restore(cfg, snap):
    st = cfg["settings"]
    entries = snap.get("apps", [])
    frozen = [e for e in entries if is_freeze(e)]
    if frozen:
        for e in frozen:
            log("resuming %s (%s)" % (e["name"], e["action"]))
            undo_action(e)
        entries = [e for e in entries if not is_freeze(e)]
        save_snapshot(snap.get("profile"), entries)
    running = list_apps()
    order = [e for e in entries if e["restore"] == "auto"] + [e for e in entries if e["restore"] == "late"]
    manual = [e for e in entries if e["restore"] == "manual"]
    remaining = list(entries)
    log("restoring %d app(s), %d left for you to open manually" % (len(order), len(manual)))
    try:
        for i, e in enumerate(order):
            if any(app_matches(a, e["bundle"] or e["name"]) for a in running):
                log("%s already running, skipping" % e["name"])
            else:
                log("opening %s..." % e["name"])
                open_app(e)
            remaining = [r for r in remaining if r is not e]
            save_snapshot(snap.get("profile"), remaining)
            if i == len(order) - 1:
                break  # nothing left to launch, no reason to wait for the Mac to settle
            time.sleep(st["restore_late_min_delay"] if e["restore"] == "late" else st["restore_min_delay"])
            if not wait_until_calm(st):
                log("  system still busy after %ss, moving on" % st["restore_max_wait"])
    except KeyboardInterrupt:
        log("restore interrupted; %d app(s) still pending. Run `gamefocus.py restore` to continue" % len(remaining))
        return False
    if os.path.exists(SNAPSHOT):
        os.remove(SNAPSHOT)
    if manual:
        log("open these yourself when you want: " + ", ".join(e["name"] for e in manual))
    log("restore finished")
    return True


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt()


def _install_signals():
    signal.signal(signal.SIGTERM, _raise_interrupt)
    signal.signal(signal.SIGHUP, _raise_interrupt)


# ----------------------------------------------------------------- commands
def cmd_init(args):
    os.makedirs(HOME_DIR, exist_ok=True)
    if os.path.exists(CONFIG):
        print("Config already exists: %s (not overwritten)" % CONFIG)
        return
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
    print("Created %s\nEdit it, then try:  python3 gamefocus.py run example --dry-run" % CONFIG)


def cmd_list(args):
    apps = list_apps()
    ram = ram_by_pid()
    rows = sorted(((ram.get(a["pid"], 0), a) for a in apps), key=lambda r: -r[0])
    print("%8s  %-28s %s" % ("~RAM MB", "NAME", "BUNDLE ID"))
    for mb, a in rows:
        print("%8.0f  %-28s %s" % (mb, a["name"][:28], a.get("bundle") or "-"))
    print("\nRAM is approximate (includes helper processes, ignores compressed memory).")


def cmd_find(args):
    pids = find_game_pids(args.regex, args.args)
    print("matches: %s" % (pids or "none"))


STEAM_ROOT = os.path.expanduser("~/Library/Application Support/Steam")
STEAM_NOT_GAMES = {"228980"}  # Steamworks Common Redistributables


def steam_games(root=STEAM_ROOT):
    """Fully installed Steam games across all library folders: [{appid, name}] sorted by name."""
    libs = [root]
    try:
        with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8") as f:
            libs += re.findall(r'"path"\s+"([^"]+)"', f.read())
    except OSError:
        pass
    games, seen = [], set()
    for lib in dict.fromkeys(libs):
        apps_dir = os.path.join(lib, "steamapps")
        try:
            names = os.listdir(apps_dir)
        except OSError:
            continue
        for fn in names:
            if not (fn.startswith("appmanifest_") and fn.endswith(".acf")):
                continue
            try:
                with open(os.path.join(apps_dir, fn), encoding="utf-8", errors="replace") as f:
                    txt = f.read()
            except OSError:
                continue
            kv = dict(re.findall(r'"(appid|name|StateFlags|installdir)"\s+"([^"]*)"', txt))
            appid = kv.get("appid", "")
            if appid.isdigit() and appid not in seen and appid not in STEAM_NOT_GAMES \
                    and kv.get("StateFlags", "").isdigit() and int(kv["StateFlags"]) & 4 and kv.get("name"):
                seen.add(appid)
                g = {"appid": appid, "name": kv["name"]}
                install = os.path.join(apps_dir, "common", kv.get("installdir", ""))
                try:  # direct launch only when the choice is unambiguous
                    bundles = [d for d in os.listdir(install) if d.endswith(".app")]
                except OSError:
                    bundles = []
                if len(bundles) == 1:
                    g["app"] = os.path.join(install, bundles[0])
                games.append(g)
    return sorted(games, key=lambda g: g["name"].lower())


STEAM_BUNDLE = "com.valvesoftware.steam"


def steam_running():
    return any((a.get("bundle") or "").lower() == STEAM_BUNDLE for a in list_apps())


def ensure_steam(st):
    """Start the Steam client in the background if needed (games with Steamworks want it running)."""
    if steam_running():
        return True
    log("Steam is not running, starting it in the background...")
    sh(["open", "-g", "-b", STEAM_BUNDLE], timeout=20)
    deadline = time.time() + st["steam_start_timeout"]
    while time.time() < deadline:
        if steam_running():
            time.sleep(st["steam_settle"])
            return True
        time.sleep(1)
    log("Steam did not appear within %ss, launching the game anyway" % st["steam_start_timeout"])
    return False


def play_game(appid, via_steam, st):
    """Launch an installed Steam game: its .app directly (Steam started first if needed), else steam:// URL."""
    game = next((g for g in steam_games() if g["appid"] == str(appid)), None)
    if not game:
        return False
    ensure_steam(st)
    if game.get("app") and not via_steam:
        log("launching %s directly" % game["name"])
        sh(["open", "-a", game["app"]], timeout=20)
    else:
        log("launching %s via Steam" % game["name"])
        sh(["open", "steam://rungameid/%s" % game["appid"]], timeout=20)
    return True


def cmd_play(args):
    if not play_game(args.appid, args.via_steam, load_config()["settings"]):
        sys.exit("No installed Steam game with appid %s (see `gamefocus.py games`)." % args.appid)


def cmd_games(args):
    games = steam_games()
    if args.json:
        print(json.dumps(games))
    else:
        for g in games:
            print("%10s  %s" % (g["appid"], g["name"]))
        if not games:
            print("No installed Steam games found.")


def cmd_resume_all(args):
    n = resume_frozen()
    print("Resumed %d paused/demoted app(s)." % n)
    if load_snapshot():
        print("Apps that were quit are still pending. Run:  python3 gamefocus.py restore")


def cmd_status(args):
    snap = load_snapshot()
    if not snap:
        print("No pending snapshot. Nothing to restore.")
        return
    print("Pending snapshot (profile %s, %s):" % (snap.get("profile"), snap.get("created")))
    for e in snap["apps"]:
        print("  - %s [%s, restore=%s]" % (e["name"], e.get("action", "quit"), e["restore"]))
    print("Run:  python3 gamefocus.py restore")


def cmd_restore(args):
    cfg = load_config()
    snap = load_snapshot()
    if not snap:
        print("No snapshot found. Nothing to restore.")
        return
    _install_signals()
    restore(cfg, snap)


def cmd_run(args):
    cfg = load_config()
    prof = cfg["profiles"].get(args.profile)
    if not prof:
        sys.exit("Unknown profile '%s'. Available: %s" % (args.profile, ", ".join(cfg["profiles"]) or "none"))
    if not args.dry_run:
        n = resume_frozen()  # watchdog: a crashed session may have left apps paused/demoted
        if n:
            log("watchdog: resumed %d frozen app(s) from an earlier session" % n)
    if load_snapshot() and not args.dry_run:
        sys.exit("A previous snapshot is still pending. Run `python3 gamefocus.py restore` first.")
    st = cfg["settings"]
    game = prof.get("game", {})
    targets, notes = select_targets(list_apps(), norm_entries(prof.get("quit", [])), cfg["keep"])

    print("Profile: %s" % args.profile)
    total = 0
    for t in targets:
        total += t["ram_mb"]
        print("  %-6s%-26s ~%5d MB   restore=%s" % (t["action"], t["name"], t["ram_mb"], t["restore"]))
    for n in notes:
        print("  note  " + n)
    print("  ~%d MB should be freed (rough estimate)" % total)
    if args.dry_run:
        print("Dry run: nothing changed.")
        return
    if not targets:
        print("Nothing to quit.")
    if not args.yes:
        if input("Proceed? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return

    run_session(cfg, args.profile, prof, targets)


def run_session(cfg, name, prof, targets, launch=True):
    """Quit targets, wait for the game, restore. Returns True if interrupted (Ctrl+C/SIGTERM/SIGHUP)."""
    st = cfg["settings"]
    game = prof.get("game", {})
    interrupted = False
    _install_signals()
    snap = None
    try:
        if targets:
            log(mem_line("before"))
            save_snapshot(name, targets)  # BEFORE touching anything
            frozen = [t for t in targets if t["action"] != "quit"]
            quits = [t for t in targets if t["action"] == "quit"]
            for t in frozen:
                log("%s %s (%d process(es))" % (t["action"], t["name"], len(t["pids"])))
                apply_action(t)
            done = quit_targets(quits, st["quit_timeout"])[0] if quits else []
            save_snapshot(name, frozen + done)  # only undo what we really changed
            snap = {"profile": name, "apps": frozen + done}
            if done:
                time.sleep(2)
            log(mem_line("after quit/freeze"))
        log("%d app(s) closed/frozen" % len(snap["apps"]) if snap else "no apps changed")

        match, match_args = game.get("match"), game.get("match_args", False)
        if not launch:
            pass
        elif game.get("launch_cmd"):
            sh(game["launch_cmd"], timeout=20)
        elif game.get("launch"):
            sh(["open", "-a", game["launch"]], timeout=20)
        else:
            log("start your game now...")

        if match:
            deadline = time.time() + st["start_timeout"]
            while not find_game_pids(match, match_args):
                if time.time() > deadline:
                    log("game process not found within %ss, restoring apps" % st["start_timeout"])
                    break
                time.sleep(3)
            else:
                log("game detected, waiting for it to exit (Ctrl+C = stop waiting and restore)")
                gone = 0
                while gone < 2:  # require 2 consecutive misses (launchers can flicker)
                    time.sleep(3)
                    gone = gone + 1 if not find_game_pids(match, match_args) else 0
                log("game exited")
                log(mem_line("at game exit"))
        else:
            input("No game.match set. Press Enter when you are done playing... ")
    except KeyboardInterrupt:
        interrupted = True
        log("interrupted, restoring apps")
    finally:
        snap = load_snapshot()
        if snap:
            try:
                time.sleep(st["restore_grace_after_game"])
            except KeyboardInterrupt:
                log("grace wait skipped")
            restore(cfg, snap)
    return interrupted


def watched_hits(cfg):
    """Names of opt-in (watch=true) profiles whose game.match currently matches a process."""
    hits = set()
    for name, prof in cfg["profiles"].items():
        g = prof.get("game", {})
        if prof.get("watch") and g.get("match") and find_game_pids(g["match"], g.get("match_args", False)):
            hits.add(name)
    return hits


def watch_tick(counts, hits, need=2):
    """Hysteresis: a profile fires only after `need` consecutive polls with a hit; any gap resets it."""
    for name in list(counts):
        if name not in hits:
            del counts[name]
    for name in sorted(hits):
        counts[name] = counts.get(name, 0) + 1
        if counts[name] >= need:
            return name
    return None


def wait_gone(cfg, name):
    """Block until the profile's game is absent for 2 consecutive polls (launchers can flicker)."""
    gone = 0
    while gone < 2:
        time.sleep(3)
        gone = gone + 1 if name not in watched_hits(cfg) else 0


def cmd_watch(args):
    cfg = load_config()
    names = [n for n, p in cfg["profiles"].items() if p.get("watch")]
    if not names:
        sys.exit('No profile has "watch": true (and a game.match). Add it to a profile first.')
    _install_signals()
    if not args.dry_run:
        n = resume_frozen()
        if n:
            log("watchdog: resumed %d frozen app(s) from an earlier session" % n)
    log("watching for: %s%s (Ctrl+C to stop)" % (", ".join(names), " [dry-run]" if args.dry_run else ""))
    counts = {}
    try:
        while True:
            fire = watch_tick(counts, watched_hits(cfg))
            if fire:
                counts.clear()
                log("game detected for profile '%s'" % fire)
                if args.dry_run:
                    log("dry-run: would quit apps now. Waiting for the game to exit.")
                    wait_gone(cfg, fire)
                elif load_snapshot():
                    log("previous snapshot still pending, not touching apps. Run `restore`.")
                    wait_gone(cfg, fire)
                else:
                    prof = cfg["profiles"][fire]
                    targets, _ = select_targets(list_apps(), norm_entries(prof.get("quit", [])), cfg["keep"])
                    if run_session(cfg, fire, prof, targets, launch=False):
                        break
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    log("watch stopped")


def main():
    p = argparse.ArgumentParser(description="Free RAM while gaming, restore apps gently afterwards.")
    sub =p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create default config").set_defaults(fn=cmd_init)
    sub.add_parser("list", help="show running apps with approx RAM").set_defaults(fn=cmd_list)
    f = sub.add_parser("find", help="test a game-process regex")
    f.add_argument("regex")
    f.add_argument("--args", action="store_true", help="match against full command line")
    f.set_defaults(fn=cmd_find)
    sub.add_parser("status", help="show pending snapshot").set_defaults(fn=cmd_status)
    g = sub.add_parser("games", help="list installed Steam games")
    g.add_argument("--json", action="store_true")
    g.set_defaults(fn=cmd_games)
    pl = sub.add_parser("play", help="launch an installed Steam game (starts Steam first if needed)")
    pl.add_argument("appid")
    pl.add_argument("--via-steam", action="store_true", help="use steam://rungameid instead of the game's .app")
    pl.set_defaults(fn=cmd_play)
    sub.add_parser("resume-all", help="EMERGENCY: undo every pause/demote now").set_defaults(fn=cmd_resume_all)
    sub.add_parser("restore", help="restore apps from last snapshot").set_defaults(fn=cmd_restore)
    r = sub.add_parser("run", help="quit apps, wait for game, restore")
    r.add_argument("profile")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    r.set_defaults(fn=cmd_run)
    w = sub.add_parser("watch", help="auto-run profiles marked watch=true when their game starts")
    w.add_argument("--dry-run", action="store_true", help="only log detections, change nothing")
    w.set_defaults(fn=cmd_watch)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
