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
python3 gamefocus.py status | restore      # pending snapshot / crash recovery
python3 -m unittest discover -s tests
```

Config: `game.match` is a regex on the executable **path** (`ps comm=`), not the bundle id
(`find` first). `quit[].restore` = `auto` | `late` | `manual`; a typo is a config error.

## macOS notes
- First quit of each app triggers an Automation permission prompt for your terminal.
- Apps are asked to quit politely; one with an unsaved-document dialog stays open and is not restored.
- Restore waits until pressure is not critical, `memory_pressure` free >= `restore_min_free_pct` (20),
  CPU idle >= 60. Pressure level 2 is normal idle on 8GB, so it does not block.

## Verified on a real Mac mini (M1, 8GB)
Verified: list/find/dry-run, quit via osascript, game detection and exit, `open -g` restore of Aside,
`sysctl kern.memorystatus_vm_pressure_level` readable, JXA frontmost app readable.
Not verified: RAM actually freed (only `ps` RSS estimates), FPS effect, Automation prompt wording.
