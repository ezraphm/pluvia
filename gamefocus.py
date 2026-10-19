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
    "restore_min_free_pct": 20,     # `memory_pressure` free % must be at least this (8GB Macs idle at pressure level 2)
}

RESTORE_MODES = ("auto", "late", "manual")

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
        quit_list = prof.get("quit", [])
        if not isinstance(quit_list, list):
            errs.append("%s.quit must be a list" % where)
            continue
        for i, e in enumerate(quit_list):
            if isinstance(e, str):
                continue
            if not isinstance(e, dict) or not isinstance(e.get("app"), str) or not e["app"]:
                errs.append("%s.quit[%d] needs an \"app\" name or bundle id" % (where, i))
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
        out.append({"app": e["app"], "restore": e.get("restore", "auto")})
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
    targets, notes = [], []
    for e in quit_entries:
        found = [a for a in apps if app_matches(a, e["app"])]
        if not found:
            notes.append("%s: not running, skipped" % e["app"])
            continue
        a = found[0]
        if (a.get("bundle") or "").lower() in protected or a["name"].lower() in protected \
                or a["pid"] in ancestors:
            notes.append("%s: protected, will NOT be quit" % a["name"])
            continue
        targets.append({"name": a["name"], "bundle": a.get("bundle"), "pid": a["pid"],
                        "restore": e["restore"], "ram_mb": round(ram.get(a["pid"], 0))})
    return targets, notes


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


def cmd_status(args):
    snap = load_snapshot()
    if not snap:
        print("No pending snapshot. Nothing to restore.")
        return
    print("Pending snapshot (profile %s, %s):" % (snap.get("profile"), snap.get("created")))
    for e in snap["apps"]:
        print("  - %s [%s]" % (e["name"], e["restore"]))
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
    if load_snapshot() and not args.dry_run:
        sys.exit("A previous snapshot is still pending. Run `python3 gamefocus.py restore` first.")
    st = cfg["settings"]
    game = prof.get("game", {})
    targets, notes = select_targets(list_apps(), norm_entries(prof.get("quit", [])), cfg["keep"])

    print("Profile: %s" % args.profile)
    total = 0
    for t in targets:
        total += t["ram_mb"]
        print("  quit  %-26s ~%5d MB   restore=%s" % (t["name"], t["ram_mb"], t["restore"]))
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

    _install_signals()
    snap = None
    try:
        if targets:
            save_snapshot(args.profile, targets)  # BEFORE quitting
            done, stuck = quit_targets(targets, st["quit_timeout"])
            save_snapshot(args.profile, done)     # only restore what we really quit
            snap = {"profile": args.profile, "apps": done}
            if done:
                time.sleep(2)
        log("%d app(s) closed" % len(snap["apps"]) if snap else "no apps closed")

        match, match_args = game.get("match"), game.get("match_args", False)
        if game.get("launch_cmd"):
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
        else:
            input("No game.match set. Press Enter when you are done playing... ")
    except KeyboardInterrupt:
        log("interrupted, restoring apps")
    finally:
        snap = load_snapshot()
        if snap:
            try:
                time.sleep(st["restore_grace_after_game"])
            except KeyboardInterrupt:
                log("grace wait skipped")
            restore(cfg, snap)


def main():
    p = argparse.ArgumentParser(description="Free RAM while gaming, restore apps gently afterwards.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create default config").set_defaults(fn=cmd_init)
    sub.add_parser("list", help="show running apps with approx RAM").set_defaults(fn=cmd_list)
    f = sub.add_parser("find", help="test a game-process regex")
    f.add_argument("regex")
    f.add_argument("--args", action="store_true", help="match against full command line")
    f.set_defaults(fn=cmd_find)
    sub.add_parser("status", help="show pending snapshot").set_defaults(fn=cmd_status)
    sub.add_parser("restore", help="restore apps from last snapshot").set_defaults(fn=cmd_restore)
    r = sub.add_parser("run", help="quit apps, wait for game, restore")
    r.add_argument("profile")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    r.set_defaults(fn=cmd_run)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
