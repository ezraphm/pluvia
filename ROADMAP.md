# GameON Launcher roadmap

Goal: a game launcher for weak Macs (8 GB class) that frees RAM while playing and keeps Steam reachable where
ISPs break it. Python stdlib core (`gamefocus.py`, `steamfix.py`), thin Swift shell (`menubar/`).

## Done
- M1 `steamfix.py`: Steam DNS/PAC proxy ported from SteamAccessFix-macOS, validated `networksetup` calls.
- M2 `gamefocus.py steamfix run|status|restore`, crash watchdog, `steamfix.dns` config. User level, no root.
- M3 Launcher window: Optimize and Steam Fix switches, game grid, search, Play.

## Next (worker tasks, in order)
| # | Task | Files | Needs |
|---|------|-------|-------|
| T1 | `steamfix check [--json]`: detect whether Steam is blocked | gamefocus.py, tests | - |
| T2 | Auto Steam Fix: `steamfix.auto` starts/stops it around `play steam:*` | gamefocus.py, tests | T1 |
| T3 | Preferences + launcher: Steam Fix DNS/auto settings, "blocked" indicator | menubar/*.swift | T2 |
| T4 | Launcher polish: portrait covers, per-game launch options entry | menubar/launcher.swift | T3 |
| T5 | steamfix fallback for non-admin accounts (osascript admin prompt) | steamfix.py, tests | - |
| T6 | README + credits for the Launcher and Steam Fix | README.md | T4 |

## Needs the user's Mac (not for workers)
- Steam Fix on a network that really blocks Steam (VN ISP): verify the bypass end to end.
- RAM measurement before/after quit (log has the lines; no numbers claimed until measured).
- Restore bug `system still busy after 90s` (PastePal).
- Epic/GOG detection on real installs (fixture tests only so far).

## Later
Packaging (zip/dmg, install to /Applications, login item), privileged helper (M0 spike works with ad-hoc signing).
