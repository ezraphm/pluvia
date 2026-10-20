# Pluvia (gamefocus)

*Less weight. More play.* Pluvia is the menu bar launcher; `gamefocus.py` is its engine. For personal use.

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
python3 gamefocus.py games [--json]         # installed games: Steam, Epic, GOG, App Store
python3 gamefocus.py play <id>              # steam:<appid> | epic:<app> | gog:<id> | mas:<bundle>
python3 gamefocus.py watch [--dry-run]    # auto-run profiles with "watch": true when their game starts
python3 gamefocus.py steamfix run [--dns NAME] [--dry-run] [--auto-stop]  # Steam DNS/proxy fix, foreground, restores on exit
python3 gamefocus.py steamfix status [--json]               # applied? server running?
python3 gamefocus.py steamfix check [--json]                # is Steam blocked/poisoned here? (exit 1 if not ok)
python3 gamefocus.py steamfix restore                        # undo the DNS/PAC settings
python3 -m unittest discover -s tests
```

Config: `game.match` is a regex on the executable **path** (`ps comm=`), not the bundle id
(`find` first). `quit[].restore` = `auto` | `late` | `manual`; a typo is a config error.

## Pluvia Launcher
**Open Pluvia…** in the menu bar opens one window: **Optimize while playing** (same as Start watching), a
**Steam Fix** switch that shows this network's verdict, and a searchable grid of every installed game. Steam
games use their portrait cover from the local `appcache/librarycache` (nothing is downloaded); other stores
get a compact card. **Play** starts the watcher if it is off, then runs `gamefocus.py play`; right-click a
card for **Launch options…** (per game id, saved in `config.json`).

Steam Fix in the window and in **Preferences…** is the same `steamfix` engine as the CLI (both only shell out):
- **Check** (`steamfix check`): resolves `store.steampowered.com` through your resolver and through 8.8.8.8,
  then tries a TLS handshake on each. Verdicts `ok` / `dns_poisoned` / `dpi_blocked` / `offline`; the window
  shows it under the switch and warns when the network blocks Steam. While the fix is on it says "Steam Fix
  is on" instead, because its own DNS change would skew the probe.
- **Auto**: `steamfix.auto` ("Start automatically when I play a Steam game") makes `play` start
  `steamfix run --auto-stop` detached when no server is running; that server stops itself 60 s after Steam
  exits, or 120 s after it started if Steam never appeared.
- **DNS choice**: `steamfix.dns` / the Preferences picker — google, cloudflare, controld, quad9, opendns,
  adguard, nextdns, alidns, dnssb, comodo, or `fastest` (probes every provider and takes the quickest).
- **Non-admin fallback**: when `networksetup` fails with an authorization error, the call is retried once
  through the macOS admin prompt (`osascript … with administrator privileges`). Declining it changes nothing;
  errors that are not about permissions (unknown service, bad value) never prompt.
- **Restore on exit**: Ctrl+C, SIGTERM, SIGHUP and errors all put back the previous DNS servers and PAC/auto-proxy
  setting from `/var/tmp/saf-state.json`; `run`/`watch`/`resume-all` also undo a session whose server died
  (watchdog).
- **What it does NOT do**: no VPN and no tunnel — only Steam goes through it. The PAC file sends just the
  Steam domains (steampowered, steamcommunity, steamstatic, steamusercontent, steam-chat, steamgames,
  steamcontent, steamserver, counter-strike.net, dota2.com, valvesoftware.com) to a loopback proxy and
  everything else DIRECT. It does not hide your IP, bypass store region locks or fix a blocked game server.

What is verified: the mechanics and the restore (applies and undoes the DNS/PAC settings; exits on Ctrl+C,
SIGTERM and error; the watchdog undoes a crashed session) — but only on a network that does **not** block
Steam. The bypass itself has not been verified end to end; that needs a connection that really blocks Steam
(VN ISP), see ROADMAP.

## Steam connection fix (CLI)
`steamfix run` applies the same loopback PAC/CONNECT proxy and DNS set as the launcher's switch, then serves in
the foreground until Ctrl+C. `--dns` overrides the config's `steamfix: {"dns": "google", "auto": false}`
(default `google`). `steamfix status|check|restore` inspect and undo it; DNS choices, auto-stop timing and the
restore rules are described under **Pluvia Launcher** above.

## Actions per app
`quit[].action` = `quit` (default) | `demote` (`taskpolicy -b`: E-cores, low IO) | `pause` (SIGSTOP the app's
process tree, RAM is NOT freed) | `keep`. Undone automatically on restore, and by a watchdog at the next
`run`/`watch` start if a session crashed. `pause`/`demote` are refused for a built-in safelist (Steam, Discord,
Zoom, Teams, FaceTime, Slack, Spotify, Music, OBS, torrent clients, game launchers) and never touch other users'
or system processes (WindowServer, coreaudiod, ...). Only processes in the app's own tree are signalled.

## What can be freed
Preferences lists every running **app**, **menu bar agent** and **background process** (yours only, outside any app,
never `/System`, `/usr/libexec`, the protected list, or gamefocus itself). Apps: keep/demote/pause/quit. Processes
(`{"process": "ollama", "action": "pause"}`): keep/demote/pause only, they cannot be relaunched safely.

## Library auto-detect (all games, no per-game profile)
```json
"library_watch": {"enabled": true, "profile": "library", "sources": ["Steam", "Epic Games", "GOG", "App Store"], "exclude": []}
```
`watch` matches every installed library game by its install path and applies the `quit` list of `profile`
(default off; only apps in that list are touched). `watch` reloads config.json when it changes. Menu bar
**Preferences…** edits all of it (enable, sources, apps + action/restore, excluded games).

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
`menubar/build.sh` builds `menubar/build/GameON.app`, shown as "Pluvia" (needs `swiftc`; the app bundles a copy of `gamefocus.py`,
so rebuild after changing it). It is a thin shell over the CLI: **Open Pluvia…** (the launcher window,
`launcher.swift`), Start/Stop watching (+ dry-run toggle), Restore now, Play game (installed games via
`gamefocus.py games`; starts `watch` if off, then `gamefocus.py play <appid>` — Steam in the background if it
is not running, then the game's `.app` directly, `steam://rungameid/<id>` only for "Always launch via Steam"
or when no single `.app` exists), Preferences (apps + actions, library auto-detect, Steam Fix DNS/auto),
EMERGENCY Resume all, open config/log, Start at login (LaunchAgent). Stop watch CLI instances first: two
watchers race.

## Credits
Epic/GOG/App Store library detection is a Python port of the importers in
[Aether](https://github.com/VibeNoobNotFound/Aether) (MIT, (c) 2026 VibeNoobNotFound). Epic/GOG are untested on
real installs here (none installed); they are covered by fixture tests only. To auto-free RAM for non-Steam games,
add a `watch` profile whose `game.match` matches their install path.

The Steam Fix (`steamfix.py`) is ported from
[SteamAccessFix-macOS](https://github.com/ezraphm/SteamAccessFix-macOS) (MIT, (c) 2026 vuviett47, ezraphm).
Its DNS, PAC/ClientHello-split and IP-failover logic comes from the Windows original,
[vuviett47's SteamAccessFix](https://gitlab.com/vuviett47/SteamAccessFix) (MIT).
