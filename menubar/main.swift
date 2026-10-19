import AppKit

// Thin menu bar shell around gamefocus.py (bundled in Resources). All logic stays in Python;
// this app only starts/stops `watch`, runs `restore` / `resume-all`, and shows snapshot.json state.

let home = NSHomeDirectory() + "/.gamefocus"
let snapshotPath = home + "/snapshot.json"
let agentLabel = "dev.gameon.menubar"
let agentPath = NSHomeDirectory() + "/Library/LaunchAgents/\(agentLabel).plist"

let scriptPath: String = Bundle.main.path(forResource: "gamefocus", ofType: "py") ?? ""
let python: String = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
    .first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
// Specific to this bundle, so pgrep/pkill can never match anything else.
let watchPattern = "\(scriptPath) watch"

@discardableResult
func run(_ exe: String, _ args: [String], wait: Bool = true) -> (Int32, String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: exe)
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return (127, "") }
    if !wait { return (0, "") }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

func spawnDetached(_ args: [String]) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = [scriptPath] + args
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    try? p.run()
}

func steamGames() -> [(id: String, name: String)] {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = [scriptPath, "games", "--json"]
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else { return [] }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    let arr = (try? JSONSerialization.jsonObject(with: data)) as? [[String: String]] ?? []
    return arr.compactMap { g in g["appid"].flatMap { id in g["name"].map { (id, $0) } } }
}

func isWatching() -> Bool { run("/usr/bin/pgrep", ["-f", watchPattern]).0 == 0 }
func stopWatching() { run("/usr/bin/pkill", ["-TERM", "-f", watchPattern]) }

func snapshotSummary() -> String? {
    guard let d = FileManager.default.contents(atPath: snapshotPath),
          let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
          let apps = j["apps"] as? [[String: Any]] else { return nil }
    let frozen = apps.filter { ["pause", "demote"].contains($0["action"] as? String ?? "quit") }.count
    return "Session: \(apps.count - frozen) closed, \(frozen) frozen (\(j["profile"] as? String ?? "?"))"
}

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var timer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        item.menu = { let m = NSMenu(); m.delegate = self; return m }()
        stopWatching() // stale watch from an earlier run of this app
        refreshIcon()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.refreshIcon() }
        let c = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            c.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in self?.refreshIcon() }
        }
    }

    func refreshIcon() {
        let active = snapshotSummary() != nil
        let name = active ? "gamecontroller.fill" : "gamecontroller"
        item.button?.image = NSImage(systemSymbolName: name, accessibilityDescription: "GameON")
        item.button?.title = isWatching() ? "" : " off"
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let watching = isWatching()
        let dry = UserDefaults.standard.bool(forKey: "dryRun")
        menu.addItem(withTitle: watching ? "Watching\(dry ? " (dry-run)" : "")" : "Not watching", action: nil, keyEquivalent: "")
        menu.addItem(withTitle: snapshotSummary() ?? "No active session", action: nil, keyEquivalent: "")
        menu.addItem(.separator())
        let playItem = menu.addItem(withTitle: "Play Steam game", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        let games = steamGames()
        for g in games {
            let gi = sub.addItem(withTitle: g.name, action: #selector(play(_:)), keyEquivalent: "")
            gi.target = self
            gi.representedObject = g.id
        }
        if games.isEmpty { sub.addItem(withTitle: "No installed Steam games found", action: nil, keyEquivalent: "") }
        playItem.submenu = sub
        menu.addItem(.separator())
        add(menu, watching ? "Stop watching" : "Start watching", #selector(toggleWatch))
        let d = add(menu, "Dry-run (log only, change nothing)", #selector(toggleDry))
        d.state = dry ? .on : .off
        menu.addItem(.separator())
        add(menu, "Restore apps now", #selector(restoreNow))
        add(menu, "EMERGENCY: Resume all (unpause/undemote)", #selector(resumeAll))
        menu.addItem(.separator())
        add(menu, "Open config", #selector(openConfig))
        add(menu, "Open log", #selector(openLog))
        let l = add(menu, "Start at login", #selector(toggleLogin))
        l.state = FileManager.default.fileExists(atPath: agentPath) ? .on : .off
        menu.addItem(.separator())
        add(menu, "Quit GameON (stops watching)", #selector(quit))
    }

    @discardableResult
    func add(_ m: NSMenu, _ t: String, _ a: Selector) -> NSMenuItem {
        let i = m.addItem(withTitle: t, action: a, keyEquivalent: "")
        i.target = self
        return i
    }

    @objc func toggleWatch() {
        if isWatching() { stopWatching() }
        else { spawnDetached(["watch"] + (UserDefaults.standard.bool(forKey: "dryRun") ? ["--dry-run"] : [])) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.refreshIcon() }
    }

    // Playing from here starts `watch` first (if off), so the profile frees RAM when the game process appears.
    @objc func play(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, id.allSatisfy({ $0.isNumber }) else { return }
        if !isWatching() { toggleWatch() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            if let url = URL(string: "steam://rungameid/\(id)") { NSWorkspace.shared.open(url) }
        }
    }

    @objc func toggleDry() {
        let d = UserDefaults.standard
        d.set(!d.bool(forKey: "dryRun"), forKey: "dryRun")
        if isWatching() { stopWatching(); DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.toggleWatch() } }
    }

    @objc func restoreNow() { spawnDetached(["restore"]) }
    @objc func resumeAll() { spawnDetached(["resume-all"]) }
    @objc func openConfig() { NSWorkspace.shared.open(URL(fileURLWithPath: home + "/config.json")) }
    @objc func openLog() { NSWorkspace.shared.open(URL(fileURLWithPath: home + "/gamefocus.log")) }

    @objc func toggleLogin() {
        if FileManager.default.fileExists(atPath: agentPath) {
            run("/bin/launchctl", ["bootout", "gui/\(getuid())/\(agentLabel)"])
            try? FileManager.default.removeItem(atPath: agentPath)
        } else {
            let plist: [String: Any] = ["Label": agentLabel, "RunAtLoad": true,
                                        "ProgramArguments": [Bundle.main.executablePath ?? ""]]
            (plist as NSDictionary).write(toFile: agentPath, atomically: true)
            run("/bin/launchctl", ["bootstrap", "gui/\(getuid())", agentPath])
        }
    }

    @objc func quit() {
        stopWatching()
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
