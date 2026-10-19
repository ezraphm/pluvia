# GameON (gamefocus)

Free RAM on a Mac mini M1 8GB while gaming, then reopen apps gently afterwards.
Python 3 standard library only, macOS only. No sudo, no `kill -9`.

## Usage
```
python3 gamefocus.py init                  # create ~/.gamefocus/config.json
python3 gamefocus.py list                  # running apps + approx RAM
python3 gamefocus.py find "regex"          # test game-process match (ps comm)
python3 gamefocus.py run <profile> --dry-run
python3 gamefocus.py run <profile>         # quit apps, wait for game, restore
python3 gamefocus.py resume-all            # EMERGENCY: undo every pause/demote (SIGCONT, taskpolicy -B)
python3 gamefocus.py status | restore      # pending snapshot / crash recovery
python3 gamefocus.py games [--json]         # installed Steam games (appmanifest_*.acf)
python3 gamefocus.py watch [--dry-run]    # auto-run profiles with "watch": true when their game starts
python3 -m unittest discover -s tests
```

Config: `game.match` is a regex on the executable **path** (`ps comm=`), not the bundle id
(`find` first). `quit[].restore` = `auto` | `late` | `manual`; a typo is a config error.

## Actions per app
`quit[].action` = `quit` (default) | `demote` (`taskpolicy -b`: E-cores, low IO) | `pause` (SIGSTOP the app's
process tree, RAM is NOT freed) | `keep`. Undone automatically on restore, and by a watchdog at the next
`run`/`watch` start if a session crashed. `pause`/`demote` are refused for a built-in safelist (Steam, Discord,
Zoom, Teams, FaceTime, Slack, Spotify, Music, OBS, torrent clients, game launchers) and never touch other users'
or system processes (WindowServer, coreaudiod, ...). Only processes in the app's own tree are signalled.

## Watch (auto trigger)
Opt-in per profile: `"watch": true` + `game.match`. Fires after 2 consecutive polls (3s apart) with a match,
then quits apps, waits for the game to exit, restores. Launcher style: one profile for all Steam games:
`"game": {"match": "steamapps/common/", "match_args": true}`. No fullscreen/Game Mode detection (not readable).

## macOS notes
- First quit of each app triggers an Automation permission prompt for your terminal.
- Apps are asked to quit politely; one with an unsaved-document dialog stays open and is not restored.
- Restore waits until pressure is not critical, `memory_pressure` free >= `restore_min_free_pct` (20),
  CPU idle >= 60. Pressure level 2 is normal idle on 8GB, so it does not block.

## Verified on a real Mac mini (M1, 8GB)
Verified: list/find/dry-run, quit via osascript, game detection and exit, `open -g` restore of Aside,
`sysctl kern.memorystatus_vm_pressure_level` readable, JXA frontmost app readable.
Also verified (on a throwaway `sleep`): SIGSTOP/SIGCONT, `taskpolicy -b/-B` without sudo, `resume-all` after a simulated crash.
Not verified: RAM actually freed (only `ps` RSS estimates), FPS effect, Automation prompt wording.

## Menu bar app
`menubar/build.sh` builds `menubar/build/GameON.app` (needs `swiftc`; the app bundles a copy of `gamefocus.py`,
so rebuild after changing it). It is a thin shell: Start/Stop watching (+ dry-run toggle), Restore now,
Play Steam game (lists installed games via `gamefocus.py games`, starts `watch` if off, then opens `steam://rungameid/<id>`), EMERGENCY Resume all, open config/log, Start at login (LaunchAgent). Stop watch CLI instances first: two watchers race.
