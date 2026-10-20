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

func libraryGames() -> [[String: String]] {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = [scriptPath, "games", "--json"]
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else { return [] }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (try? JSONSerialization.jsonObject(with: data)) as? [[String: String]] ?? []
}

func isWatching() -> Bool { run("/usr/bin/pgrep", ["-f", watchPattern]).0 == 0 }
func stopWatching() { run("/usr/bin/pkill", ["-TERM", "-f", watchPattern]) }

/// Stopping `watch` during a game session makes it restore your apps right away (it treats SIGTERM like Ctrl+C).
func confirmStopIfSessionActive(_ what: String) -> Bool {
    guard isWatching(), let summary = snapshotSummary() else { return true }
    let a = NSAlert()
    a.messageText = "A game session is active"
    a.informativeText = "\(summary).\n\n\(what) will restore your apps immediately, while the game may still be running."
    a.addButton(withTitle: "Keep watching")
    a.addButton(withTitle: "Stop and restore apps")
    return a.runModal() == .alertSecondButtonReturn
}

func snapshotSummary() -> String? {
    guard let d = FileManager.default.contents(atPath: snapshotPath),
          let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
          let apps = j["apps"] as? [[String: Any]] else { return nil }
    let frozen = apps.filter { ["pause", "demote"].contains($0["action"] as? String ?? "quit") }.count
    return "Session: \(apps.count - frozen) closed, \(frozen) frozen (\(j["profile"] as? String ?? "?"))"
}


// MARK: - Preferences window (edits ~/.gamefocus/config.json; `watch` reloads it live)

final class Act: NSObject {
    let f: () -> Void
    init(_ f: @escaping () -> Void) { self.f = f }
    @objc func go() { f() }
}

final class FlippedView: NSView { override var isFlipped: Bool { true } }

let allSources = ["Steam", "Epic Games", "GOG", "App Store"]
let actionNames = ["keep", "demote", "pause", "quit"]
let restoreNames = ["auto", "late", "manual"]
// Same list as gamefocus.py's STEAMFIX_DNS_NAMES, so a value picked here always validates.
let steamfixDNSNames = ["google", "cloudflare", "controld", "quad9", "opendns", "adguard", "nextdns",
                        "alidns", "dnssb", "comodo", "fastest"]

func pythonJSON(_ args: [String]) -> [[String: Any]] {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = [scriptPath] + args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else { return [] }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]] ?? []
}

final class Prefs: NSObject, NSTextFieldDelegate {
    var window: NSWindow?
    let stack = NSStackView()
    var acts: [Act] = []
    var fieldActs: [ObjectIdentifier: Act] = [:]
    var cfg: [String: Any] = [:]
    var games: [[String: Any]] = []
    var running: [[String: Any]] = []
    var processes: [[String: Any]] = []
    let configPath = home + "/config.json"

    var lw: [String: Any] {
        get { cfg["library_watch"] as? [String: Any] ?? [:] }
        set { cfg["library_watch"] = newValue }
    }
    var profileName: String { lw["profile"] as? String ?? "library" }
    /// `steamfix: {"dns": "...", "auto": false}`; defaults match gamefocus.py (google, off). An unknown dns
    /// value falls back to google, so the popup always has a selection to write back.
    var steamfixDNS: String {
        let d = (cfg["steamfix"] as? [String: Any])?["dns"] as? String ?? "google"
        return steamfixDNSNames.contains(d) ? d : "google"
    }
    var steamfixAuto: Bool { (cfg["steamfix"] as? [String: Any])?["auto"] as? Bool ?? false }
    var quitList: [[String: Any]] {
        get { ((cfg["profiles"] as? [String: Any])?[profileName] as? [String: Any])?["quit"] as? [[String: Any]] ?? [] }
        set {
            var profiles = cfg["profiles"] as? [String: Any] ?? [:]
            var prof = profiles[profileName] as? [String: Any] ?? [:]
            prof["quit"] = newValue
            profiles[profileName] = prof
            cfg["profiles"] = profiles
        }
    }

    func load() {
        try? FileManager.default.createDirectory(atPath: home, withIntermediateDirectories: true)
        if let d = FileManager.default.contents(atPath: configPath),
           let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { cfg = j }
        else { cfg = ["settings": [String: Any](), "keep": [Any](), "profiles": [String: Any]()] }
        var profiles = cfg["profiles"] as? [String: Any] ?? [:]
        var l = lw
        if l["profile"] == nil { l["profile"] = "library" }
        if l["enabled"] == nil { l["enabled"] = false }
        if l["sources"] == nil { l["sources"] = allSources }
        if l["exclude"] == nil { l["exclude"] = [String]() }
        if profiles[l["profile"] as! String] == nil { profiles[l["profile"] as! String] = ["quit": [Any]()] }
        cfg["profiles"] = profiles
        lw = l
        games = pythonJSON(["games", "--json"])
        running = pythonJSON(["list", "--json"])
        processes = pythonJSON(["list", "--json", "--processes"])
    }

    func readDisk() -> [String: Any]? {
        guard let d = FileManager.default.contents(atPath: configPath) else { return nil }
        return (try? JSONSerialization.jsonObject(with: d)) as? [String: Any]
    }

    /// Never writes a stale in-memory copy over the file: the file is re-read first and only what this window
    /// owns is replaced (library_watch + the profile's quit list); everything else, launch_options included, stays.
    func save() {
        var disk = readDisk() ?? cfg
        disk["library_watch"] = cfg["library_watch"]
        var profiles = disk["profiles"] as? [String: Any] ?? [:]
        var prof = profiles[profileName] as? [String: Any] ?? [:]
        prof["quit"] = quitList
        profiles[profileName] = prof
        disk["profiles"] = profiles
        write(disk)
    }

    /// One launch option changes exactly one key on disk, so other games' options can never be lost.
    func setLaunchOption(_ id: String, _ value: String) {
        var disk = readDisk() ?? cfg
        var o = disk["launch_options"] as? [String: Any] ?? [:]
        if value.isEmpty { o.removeValue(forKey: id) } else { o[id] = value }
        disk["launch_options"] = o
        write(disk)
        cfg["launch_options"] = o
    }

    /// One Steam Fix setting changes exactly one key on disk (`steamfix.dns` / `steamfix.auto`), re-reading the
    /// file first, so the other key, launch_options and everything else in the config survive a stale window.
    func setSteamfix(_ key: String, _ value: Any) {
        var disk = readDisk() ?? cfg
        var sfx = disk["steamfix"] as? [String: Any] ?? [:]
        sfx[key] = value
        disk["steamfix"] = sfx
        write(disk)
        var mine = cfg["steamfix"] as? [String: Any] ?? [:]
        mine[key] = value
        cfg["steamfix"] = mine
    }

    func write(_ obj: [String: Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]) else { return }
        try? FileManager.default.removeItem(atPath: configPath + ".bak")
        try? FileManager.default.copyItem(atPath: configPath, toPath: configPath + ".bak") // last good copy
        try? d.write(to: URL(fileURLWithPath: configPath), options: .atomic)
    }

    func show() {
        load()
        if window == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 640),
                             styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            w.title = "Pluvia Preferences"
            w.isReleasedWhenClosed = false
            let scroll = NSScrollView()
            scroll.hasVerticalScroller = true
            scroll.drawsBackground = false
            let doc = FlippedView()
            doc.translatesAutoresizingMaskIntoConstraints = false
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 8
            stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
            stack.translatesAutoresizingMaskIntoConstraints = false
            doc.addSubview(stack)
            scroll.documentView = doc
            NSLayoutConstraint.activate([
                stack.topAnchor.constraint(equalTo: doc.topAnchor), stack.bottomAnchor.constraint(equalTo: doc.bottomAnchor),
                stack.leadingAnchor.constraint(equalTo: doc.leadingAnchor), stack.trailingAnchor.constraint(equalTo: doc.trailingAnchor),
                doc.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            ])
            w.contentView = scroll
            w.center()
            window = w
        }
        rebuild()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func controlTextDidChange(_ obj: Notification) {
        if let f = obj.object as? NSTextField { fieldActs[ObjectIdentifier(f)]?.go() }
    }

    func act(_ f: @escaping () -> Void) -> Act { let a = Act(f); acts.append(a); return a }

    func label(_ t: String, bold: Bool = false, small: Bool = false) -> NSTextField {
        let l = NSTextField(wrappingLabelWithString: t)
        l.font = bold ? .boldSystemFont(ofSize: 13) : .systemFont(ofSize: small ? 11 : 13)
        if small { l.textColor = .secondaryLabelColor }
        l.preferredMaxLayoutWidth = 540
        return l
    }

    func check(_ title: String, _ on: Bool, _ f: @escaping (Bool) -> Void) -> NSButton {
        var btn: NSButton!
        let a = act { f(btn.state == .on) }
        btn = NSButton(checkboxWithTitle: title, target: a, action: #selector(Act.go))
        btn.state = on ? .on : .off
        return btn
    }

    func popup(_ items: [String], _ selected: String, _ f: @escaping (String) -> Void) -> NSPopUpButton {
        let p = NSPopUpButton(frame: .zero, pullsDown: false)
        p.addItems(withTitles: items)
        p.selectItem(withTitle: selected)
        let a = act { f(p.titleOfSelectedItem ?? selected) }
        p.target = a
        p.action = #selector(Act.go)
        return p
    }

    func button(_ title: String, _ f: @escaping () -> Void) -> NSButton {
        NSButton(title: title, target: act(f), action: #selector(Act.go))
    }

    func rebuild() {
        stack.arrangedSubviews.forEach { stack.removeArrangedSubview($0); $0.removeFromSuperview() }
        acts = []
        fieldActs = [:]

        stack.addArrangedSubview(label("Auto-detect games", bold: true))
        stack.addArrangedSubview(check("Free RAM automatically when a game from my library starts", lw["enabled"] as? Bool ?? false) {
            var l = self.lw; l["enabled"] = $0; self.lw = l; self.save()
        })
        stack.addArrangedSubview(label("Needs “Start watching” (menu bar) to be running. Only the apps listed below are touched.", small: true))

        stack.addArrangedSubview(label("Sources", bold: true))
        let srcRow = NSStackView()
        srcRow.spacing = 12
        let enabledSources = lw["sources"] as? [String] ?? allSources
        for s in allSources {
            srcRow.addArrangedSubview(check(s, enabledSources.contains(s)) { on in
                var cur = (self.lw["sources"] as? [String]) ?? allSources
                cur.removeAll { $0 == s }
                if on { cur.append(s) }
                var l = self.lw; l["sources"] = cur; self.lw = l; self.save()
            })
        }
        stack.addArrangedSubview(srcRow)

        stack.addArrangedSubview(label("Steam Fix", bold: true))
        stack.addArrangedSubview(label("Sends Steam through a local proxy and a DNS provider your ISP does not poison. “fastest” probes them all and picks the quickest.", small: true))
        let dnsRow = NSStackView()
        dnsRow.spacing = 8
        dnsRow.addArrangedSubview(label("DNS provider"))
        dnsRow.addArrangedSubview(popup(steamfixDNSNames, steamfixDNS) { self.setSteamfix("dns", $0) })
        stack.addArrangedSubview(dnsRow)
        stack.addArrangedSubview(check("Start automatically when I play a Steam game", steamfixAuto) { on in
            self.setSteamfix("auto", on)
        })

        stack.addArrangedSubview(label("Apps to free while a game runs", bold: true))
        stack.addArrangedSubview(label("quit = closes the app politely (frees RAM) · pause = freezes it (no RAM freed) · demote = low priority · keep = leave alone. Voice/meeting/music apps and launchers are protected from pause/demote.", small: true))
        var list = quitList
        for (i, e) in list.enumerated() {
            let row = NSStackView()
            row.spacing = 8
            let isProc = e["process"] != nil
            let name = ((e["label"] as? String) ?? (e["app"] as? String) ?? (e["process"] as? String) ?? "?") + (isProc ? "  (process)" : "")
            let nameLabel = label(name)
            nameLabel.widthAnchor.constraint(equalToConstant: 200).isActive = true
            row.addArrangedSubview(nameLabel)
            row.addArrangedSubview(popup(isProc ? ["keep", "demote", "pause"] : actionNames, e["action"] as? String ?? (isProc ? "keep" : "quit")) { v in
                list[i]["action"] = v; self.quitList = list; self.save(); self.rebuild()
            })
            if (e["action"] as? String ?? "quit") == "quit" {
                row.addArrangedSubview(popup(restoreNames, e["restore"] as? String ?? "auto") { v in
                    list[i]["restore"] = v; self.quitList = list; self.save()
                })
            }
            row.addArrangedSubview(button("Remove") {
                list.remove(at: i); self.quitList = list; self.save(); self.rebuild()
            })
            stack.addArrangedSubview(row)
        }
        if list.isEmpty { stack.addArrangedSubview(label("No apps yet: auto-detect will not close anything.", small: true)) }

        let have = Set(list.compactMap { ($0["app"] as? String) ?? ($0["process"] as? String) })
        // Every running app, menu bar agent and background process (protected ones are already filtered out by Python).
        var choices: [[String: Any]] = []
        let add = NSPopUpButton(frame: .zero, pullsDown: true)
        add.addItem(withTitle: "Add a running app or process…")
        func section(_ title: String, _ items: [[String: Any]], process: Bool) {
            let fresh = items.filter { r in
                let id = process ? (r["name"] as? String) : ((r["bundle"] as? String) ?? (r["name"] as? String))
                return id != nil && !id!.isEmpty && !have.contains(id!)
            }.sorted { ($0["ram_mb"] as? Int ?? 0) > ($1["ram_mb"] as? Int ?? 0) }
            if fresh.isEmpty { return }
            let h = NSMenuItem.sectionHeader(title: title)
            add.menu?.addItem(h)
            for r in fresh {
                var c = r
                c["isProcess"] = process
                let item = NSMenuItem(title: "\(r["name"] as? String ?? "?")  (~\(r["ram_mb"] as? Int ?? 0) MB)", action: nil, keyEquivalent: "")
                item.tag = choices.count
                choices.append(c)
                add.menu?.addItem(item)
            }
        }
        section("Apps", running.filter { ($0["kind"] as? String) == "app" }, process: false)
        section("Menu bar apps & agents", running.filter { ($0["kind"] as? String) == "agent" }, process: false)
        section("Background processes (pause/demote only)", processes, process: true)
        let addAct = act {
            guard let tag = add.selectedItem?.tag, tag >= 0, tag < choices.count else { return }
            let r = choices[tag]
            if r["isProcess"] as? Bool == true {
                list.append(["process": r["name"] as? String ?? "", "label": r["name"] as? String ?? "", "action": "keep"])
            } else {
                list.append(["app": (r["bundle"] as? String) ?? (r["name"] as? String) ?? "", "label": r["name"] as? String ?? "",
                             "action": "quit", "restore": "auto"])
            }
            self.quitList = list; self.save(); self.rebuild()
        }
        add.target = addAct
        add.action = #selector(Act.go)
        stack.addArrangedSubview(add)

        stack.addArrangedSubview(label("Games (uncheck to exclude · text field = launch options, Steam syntax)", bold: true))
        let excluded = lw["exclude"] as? [String] ?? []
        if games.isEmpty { stack.addArrangedSubview(label("No installed games found.", small: true)) }
        let opts = cfg["launch_options"] as? [String: String] ?? [:]
        for g in games {
            guard let id = g["id"] as? String else { continue }
            let title = "\(g["name"] as? String ?? id)   (\(g["source"] as? String ?? "?"))"
            stack.addArrangedSubview(check(title, !excluded.contains(id)) { on in
                var ex = (self.lw["exclude"] as? [String]) ?? []
                ex.removeAll { $0 == id }
                if !on { ex.append(id) }
                var l = self.lw; l["exclude"] = ex; self.lw = l; self.save()
            })
            if g["source"] as? String == "Epic Games" { continue } // launched through the Epic launcher: no options
            let field = NSTextField(string: opts[id] ?? "")
            field.placeholderString = (g["steam_options"] as? String).map { "Steam: \($0)" } ?? "Launch options, e.g. FOO=1 %command% -windowed"
            field.cell?.sendsActionOnEndEditing = true
            field.widthAnchor.constraint(equalToConstant: 440).isActive = true
            let a = act { self.setLaunchOption(id, field.stringValue.trimmingCharacters(in: .whitespaces)) }
            field.target = a
            field.action = #selector(Act.go)
            field.delegate = self // save on every keystroke too: Enter / focus loss is easy to miss
            fieldActs[ObjectIdentifier(field)] = a
            let row = NSStackView(views: [NSTextField(labelWithString: "    "), field])
            stack.addArrangedSubview(row)
        }
    }
}

let prefs = Prefs()

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var timer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        item.menu = { let m = NSMenu(); m.delegate = self; return m }()
        // A running `watch` is adopted, never killed: status comes from pgrep, so restarting this app is harmless.
        refreshIcon()
        if CommandLine.arguments.contains("--prefs") { prefs.show() } // handy for testing/screenshots
        if CommandLine.arguments.contains("--launcher") { launcher.show() }
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.refreshIcon() }
        let c = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            c.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in self?.refreshIcon() }
        }
    }

    func refreshIcon() {
        let active = snapshotSummary() != nil
        let name = active ? "gamecontroller.fill" : "gamecontroller"
        item.button?.image = NSImage(systemSymbolName: name, accessibilityDescription: "Pluvia")
        item.button?.title = isWatching() ? "" : " off"
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        add(menu, "Open Pluvia…", #selector(openLauncher))
        menu.addItem(.separator())
        let watching = isWatching()
        let dry = UserDefaults.standard.bool(forKey: "dryRun")
        menu.addItem(withTitle: watching ? "Watching\(dry ? " (dry-run)" : "")" : "Not watching", action: nil, keyEquivalent: "")
        menu.addItem(withTitle: snapshotSummary() ?? "No active session", action: nil, keyEquivalent: "")
        menu.addItem(.separator())
        let playItem = menu.addItem(withTitle: "Play game", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        let games = libraryGames()
        for g in games {
            let suffix = g["source"] == "Steam" ? "" : "  (\(g["source"] ?? "?"))"
            let gi = sub.addItem(withTitle: (g["name"] ?? "?") + suffix, action: #selector(play(_:)), keyEquivalent: "")
            if let path = g["icon"], let img = NSImage(contentsOfFile: path) { // local Steam library cache only
                img.size = NSSize(width: 16, height: 24)
                gi.image = img
            }
            gi.target = self
            gi.representedObject = g
        }
        if games.isEmpty { sub.addItem(withTitle: "No installed games found", action: nil, keyEquivalent: "") }
        playItem.submenu = sub
        menu.addItem(.separator())
        let v = add(menu, "Always launch via Steam (for DRM games)", #selector(toggleViaSteam))
        v.state = UserDefaults.standard.bool(forKey: "viaSteam") ? .on : .off
        add(menu, watching ? "Stop watching" : "Start watching", #selector(toggleWatch))
        let d = add(menu, "Dry-run (log only, change nothing)", #selector(toggleDry))
        d.state = dry ? .on : .off
        menu.addItem(.separator())
        add(menu, "Restore apps now", #selector(restoreNow))
        add(menu, "EMERGENCY: Resume all (unpause/undemote)", #selector(resumeAll))
        menu.addItem(.separator())
        let pi = add(menu, "Preferences…", #selector(openPrefs))
        pi.keyEquivalent = ","
        add(menu, "Open config", #selector(openConfig))
        add(menu, "Open log", #selector(openLog))
        let l = add(menu, "Start at login", #selector(toggleLogin))
        l.state = FileManager.default.fileExists(atPath: agentPath) ? .on : .off
        menu.addItem(.separator())
        add(menu, "Quit Pluvia (stops watching)", #selector(quit))
    }

    @discardableResult
    func add(_ m: NSMenu, _ t: String, _ a: Selector) -> NSMenuItem {
        let i = m.addItem(withTitle: t, action: a, keyEquivalent: "")
        i.target = self
        return i
    }

    @objc func toggleWatch() {
        if isWatching() { if confirmStopIfSessionActive("Stopping") { stopWatching() } }
        else { spawnDetached(["watch"] + (UserDefaults.standard.bool(forKey: "dryRun") ? ["--dry-run"] : [])) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.refreshIcon() }
    }

    // Playing from here starts `watch` first (if off), so the profile frees RAM when the game process appears.
    @objc func play(_ sender: NSMenuItem) {
        guard let g = sender.representedObject as? [String: String], let id = g["id"], !id.isEmpty else { return }
        if !isWatching() { toggleWatch() }
        // All launch logic (Steam first if needed, then the .app or URI) lives in `gamefocus.py play`.
        spawnDetached(["play", id] + (UserDefaults.standard.bool(forKey: "viaSteam") ? ["--via-steam"] : []))
    }

    @objc func toggleViaSteam() {
        let d = UserDefaults.standard
        d.set(!d.bool(forKey: "viaSteam"), forKey: "viaSteam")
    }

    @objc func toggleDry() {
        let d = UserDefaults.standard
        d.set(!d.bool(forKey: "dryRun"), forKey: "dryRun")
        if isWatching() && confirmStopIfSessionActive("Restarting the watcher") {
            stopWatching()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.toggleWatch() }
        }
    }

    @objc func openLauncher() { launcher.show() }
    @objc func openPrefs() { prefs.show() }
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
        guard confirmStopIfSessionActive("Quitting") else { return }
        stopWatching()
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
