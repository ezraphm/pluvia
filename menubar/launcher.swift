import AppKit
import QuartzCore

// Game Launcher window (menu bar → "Open Pluvia…"): start/stop the watcher and the Steam Fix, and play
// any installed game in one click. All logic still lives in gamefocus.py; this window only shells out.

// Card metrics. Cards are placed by frame in rebuildGrid (nested stack views mis-sized them), so these are the
// only source of sizes: Steam's 600x900 cover art gets a portrait tile, everything else keeps the compact card.
let cardGap: CGFloat = 12
let compactCardH: CGFloat = 92
let coverCardW: CGFloat = 140
let coverCardH: CGFloat = 210

/// Bottom scrim of a cover card: an NSVisualEffectView masked by a CAGradientLayer that fades it out upward, so
/// the game name and Play stay readable over any artwork without a hard edge.
final class BlurScrim: NSView {
    private let effect = NSVisualEffectView()
    private let fade = CAGradientLayer()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        effect.material = .hudWindow
        effect.blendingMode = .withinWindow
        effect.state = .active
        effect.frame = bounds
        effect.autoresizingMask = [.width, .height]
        addSubview(effect)

        // Opaque at the bottom (layer y = 0), clear at the top; the mask only keeps the blur where text sits.
        fade.colors = [NSColor.black.cgColor, NSColor.black.cgColor, NSColor.black.withAlphaComponent(0).cgColor]
        fade.locations = [0, 0.45, 1]
        fade.startPoint = CGPoint(x: 0.5, y: 0)
        fade.endPoint = CGPoint(x: 0.5, y: 1)
        fade.frame = bounds
        layer?.mask = fade
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true) // resizing the window must not animate the mask
        fade.frame = bounds
        CATransaction.commit()
    }
}

/// Right-click anywhere on a card (its labels and buttons included) opens the same menu.
func attachMenu(_ menu: NSMenu, to view: NSView) {
    view.menu = menu
    for v in view.subviews { attachMenu(menu, to: v) }
}

// Specific to this bundle, so pgrep/pkill can never match another gamefocus session.
let steamfixPattern = "\(scriptPath) steamfix run"

func isSteamfixRunning() -> Bool { run("/usr/bin/pgrep", ["-f", steamfixPattern]).0 == 0 }

/// `steamfix check --json` verdict → the one-line note under the switch ("" means no verdict at all).
func steamCheckText(_ verdict: String) -> String {
    switch verdict {
    case "fix_on": return "Steam Fix is on"
    case "ok": return "Steam reachable"
    case "dns_poisoned", "dpi_blocked": return "Steam blocked by your network - turn on Steam Fix"
    default: return "offline"
    }
}

func steamCheckVerdict(_ json: String) -> String {
    guard let d = json.data(using: .utf8),
          let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
          let v = j["verdict"] as? String else { return "" }
    // With the fix on, our own DNS change skews the probe: say so instead of "blocked - turn on Steam Fix".
    return (j["fix_active"] as? Bool) == true ? "fix_on" : v
}

final class Launcher: NSObject, NSWindowDelegate, NSSearchFieldDelegate {
    var window: NSWindow?
    let statusLabel = NSTextField(labelWithString: "")
    let detailLabel = NSTextField(labelWithString: "")
    let watchSwitch = NSSwitch()
    let steamSwitch = NSSwitch()
    let steamNote = NSTextField(labelWithString: "")
    let steamCheckLabel = NSTextField(labelWithString: "")
    let search = NSSearchField()
    let scroll = NSScrollView()
    let doc = FlippedView()   // scroll document: cards are placed by frame, nested stack views mis-sized them
    var games: [[String: String]] = []
    var columns = 3
    var acts: [Act] = []      // window controls: must outlive every grid rebuild
    var cardActs: [Act] = []  // Play buttons of the current cards only
    var loading = false
    var steamCheckGen = 0
    var steamStoppedAt: Date?
    var timer: Timer?

    func show() {
        if window == nil { window = makeWindow() }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil) // isVisible must be true before refreshStatus reads it
        refreshStatus()
        reloadGames()
        refreshSteamCheck()
        startTimer()
    }

    func startTimer() {
        timer?.invalidate()
        // Status comes from pgrep + snapshot.json, so polling is the only way to notice a change made elsewhere.
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.refreshStatus() }
    }

    func windowWillClose(_ notification: Notification) {
        timer?.invalidate()
        timer = nil
    }

    func windowDidResize(_ notification: Notification) {
        let c = columnCount()
        if c != columns { columns = c; rebuildGrid() }
    }

    func refreshIn(_ delay: TimeInterval) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in self?.refreshStatus() }
    }

    func refreshStatus() {
        guard window?.isVisible == true else { return } // nothing to update while the window is closed
        let watching = isWatching()
        watchSwitch.state = watching ? .on : .off
        statusLabel.stringValue = watching ? "Watching" : "Not watching"
        detailLabel.stringValue = snapshotSummary() ?? "No active session"

        // After a stop the server needs a moment to restore the network settings, so hold the switch off.
        let restoring = steamStoppedAt.map { Date().timeIntervalSince($0) < 2.5 } ?? false
        let fixing = !restoring && isSteamfixRunning()
        steamSwitch.state = fixing ? .on : .off
        steamNote.stringValue = restoring ? "restoring..." : ""
    }

    /// Asks `steamfix check --json` whether this network blocks Steam. It resolves DNS and does two TLS
    /// handshakes, so it runs off the main thread; only the finished verdict touches the label, and a newer
    /// check always wins over a slower older one.
    func refreshSteamCheck() {
        steamCheckGen += 1
        let gen = steamCheckGen
        steamCheckLabel.stringValue = "checking…"
        steamCheckLabel.textColor = .secondaryLabelColor
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let (_, out) = run(python, [scriptPath, "steamfix", "check", "--json"])
            let verdict = steamCheckVerdict(out)
            DispatchQueue.main.async {
                guard let self = self, gen == self.steamCheckGen else { return }
                self.steamCheckLabel.stringValue = steamCheckText(verdict)
                let blocked = verdict == "dns_poisoned" || verdict == "dpi_blocked"
                self.steamCheckLabel.textColor = blocked ? .systemOrange : .secondaryLabelColor
            }
        }
    }

    @objc func toggleWatchClicked() {
        if isWatching() {
            // Never kill `watch` mid-session: it treats SIGTERM like Ctrl+C and restores every app right away.
            guard confirmStopIfSessionActive("Stopping") else { refreshStatus(); return }
            stopWatching()
        } else {
            spawnDetached(["watch"] + (UserDefaults.standard.bool(forKey: "dryRun") ? ["--dry-run"] : []))
        }
        refreshIn(0.6)
    }

    @objc func toggleSteamfixClicked() {
        if isSteamfixRunning() {
            // steamfix.py restores the saved DNS/proxy settings on SIGTERM, which takes a moment.
            steamStoppedAt = Date()
            steamNote.stringValue = "restoring..."
            run("/usr/bin/pkill", ["-TERM", "-f", steamfixPattern])
            refreshIn(1.5)
        } else {
            steamStoppedAt = nil
            steamNote.stringValue = ""
            spawnDetached(["steamfix", "run"])
            refreshIn(1.0)
        }
        // The network change is applied/restored asynchronously, so re-probe once it has settled.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in self?.refreshSteamCheck() }
    }

    // Same sequence as the menu bar's Play: start `watch` first (if off) so the profile frees RAM when the
    // game process appears, then let `gamefocus.py play` handle Steam/`.app`/URI details.
    func play(_ id: String) {
        if !isWatching() { spawnDetached(["watch"] + (UserDefaults.standard.bool(forKey: "dryRun") ? ["--dry-run"] : [])) }
        spawnDetached(["play", id] + (UserDefaults.standard.bool(forKey: "viaSteam") ? ["--via-steam"] : []))
        refreshIn(0.6)
    }

    func reloadGames() {
        if loading { return }
        loading = true
        showMessage("Loading games…")
        // `games --json` walks the Steam/Epic/GOG/App Store manifests: keep it off the main thread.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let list = libraryGames()
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.loading = false
                self.games = list
                if self.columns != self.columnCount() { self.columns = self.columnCount() }
                self.rebuildGrid()
            }
        }
    }

    func columnCount() -> Int {
        // ~180pt per column keeps the 140pt portrait covers from touching each other at any window width.
        max(1, min(6, Int(max(scroll.contentView.bounds.width, 320) / 180)))
    }

    func act(_ f: @escaping () -> Void) -> Act { let a = Act(f); acts.append(a); return a }

    func cardAct(_ f: @escaping () -> Void) -> Act { let a = Act(f); cardActs.append(a); return a }

    func label(_ t: String, small: Bool = false) -> NSTextField {
        let l = NSTextField(labelWithString: t)
        l.font = .systemFont(ofSize: small ? 10 : 12)
        if small { l.textColor = .secondaryLabelColor }
        return l
    }

    func controlTextDidChange(_ obj: Notification) { rebuildGrid() }

    func showMessage(_ text: String) {
        doc.subviews.forEach { $0.removeFromSuperview() }
        let l = label(text, small: true)
        l.frame = NSRect(x: 4, y: 4, width: max(scroll.contentView.bounds.width - 8, 100), height: 16)
        doc.addSubview(l)
        doc.frame = NSRect(x: 0, y: 0, width: scroll.contentView.bounds.width, height: 24)
    }

    func rebuildGrid() {
        doc.subviews.forEach { $0.removeFromSuperview() }
        cardActs = []
        let query = search.stringValue.trimmingCharacters(in: .whitespaces).lowercased()
        let shown = query.isEmpty ? games : games.filter { ($0["name"] ?? "").lowercased().contains(query) }
        if shown.isEmpty {
            let text = games.isEmpty ? "No installed games found." : "No game matches “\(search.stringValue)”."
            showMessage(text)
            return
        }
        let width = scroll.contentView.bounds.width
        let cardW = max((width - cardGap * CGFloat(columns - 1)) / CGFloat(columns), coverCardW)
        // One row height for the whole grid: a row with covers is as tall as a cover, and compact cards are
        // centered in that slot, so columns stay aligned and filtering by search never shifts the layout.
        let cardH = shown.contains(where: hasCover) ? coverCardH : compactCardH
        for (i, g) in shown.enumerated() {
            let cover = hasCover(g)
            let c = cover ? coverCard(g) : card(g)
            let w = cover ? min(coverCardW, cardW) : cardW
            let h = cover ? coverCardH : compactCardH
            c.frame = NSRect(x: CGFloat(i % columns) * (cardW + cardGap) + (cardW - w) / 2,
                             y: CGFloat(i / columns) * (cardH + cardGap) + (cardH - h) / 2,
                             width: w, height: h)
            doc.addSubview(c)
        }
        let rows = (shown.count + columns - 1) / columns
        doc.frame = NSRect(x: 0, y: 0, width: width, height: CGFloat(rows) * (cardH + cardGap) + 16)
    }

    func card(_ g: [String: String]) -> NSView {
        let box = NSBox()
        box.boxType = .custom
        box.titlePosition = .noTitle
        box.borderWidth = 1
        box.borderColor = .separatorColor
        box.fillColor = .controlBackgroundColor
        box.cornerRadius = 8
        guard let content = box.contentView else { return box }

        let icon = NSImageView()
        icon.image = gameIcon(g)
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.translatesAutoresizingMaskIntoConstraints = false

        let name = NSTextField(wrappingLabelWithString: g["name"] ?? "?")
        name.font = .boldSystemFont(ofSize: 12)
        name.maximumNumberOfLines = 2
        name.lineBreakMode = .byTruncatingTail
        name.translatesAutoresizingMaskIntoConstraints = false

        let badge = label(g["source"] ?? "?", small: true)
        badge.translatesAutoresizingMaskIntoConstraints = false

        let a = cardAct { [weak self] in
            guard let id = g["id"], !id.isEmpty else { return }
            self?.play(id)
        }
        let optionsAct = cardAct { [weak self] in self?.editLaunchOptions(g) }
        let playButton = NSButton(title: "Play", target: a, action: #selector(Act.go))
        playButton.bezelStyle = .rounded
        playButton.controlSize = .small
        playButton.translatesAutoresizingMaskIntoConstraints = false

        content.addSubview(icon)
        content.addSubview(name)
        content.addSubview(badge)
        content.addSubview(playButton)
        NSLayoutConstraint.activate([
            icon.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 10),
            icon.centerYAnchor.constraint(equalTo: content.centerYAnchor),
            icon.widthAnchor.constraint(equalToConstant: 52),
            icon.heightAnchor.constraint(equalToConstant: 52),
            name.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 10),
            name.topAnchor.constraint(equalTo: content.topAnchor, constant: 10),
            name.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -10),
            badge.leadingAnchor.constraint(equalTo: name.leadingAnchor),
            badge.topAnchor.constraint(equalTo: name.bottomAnchor, constant: 2),
            badge.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -10),
            playButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -10),
            playButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -10),
        ])
        attachMenu(cardMenu(g, play: a, options: optionsAct), to: box)
        return box
    }

    /// Portrait cover card: the Steam librarycache 600x900 art fills the tile (aspect fill, clipped to the rounded
    /// corners), with the name and Play on the bottom blur. Right-click (or "…") opens launch options.
    func coverCard(_ g: [String: String]) -> NSView {
        let box = NSView(frame: NSRect(x: 0, y: 0, width: coverCardW, height: coverCardH))
        box.setAccessibilityLabel(g["name"] ?? "Game")
        box.wantsLayer = true
        box.layer?.cornerRadius = 10
        box.layer?.masksToBounds = true
        box.layer?.borderWidth = 1
        box.layer?.borderColor = NSColor.separatorColor.cgColor
        box.layer?.backgroundColor = NSColor.black.cgColor
        if let img = coverImage(g), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
            box.layer?.contentsGravity = .resizeAspectFill
            box.layer?.contents = cg
        }

        let scrim = BlurScrim(frame: NSRect(x: 0, y: 0, width: coverCardW, height: 78))
        box.addSubview(scrim)

        let a = cardAct { [weak self] in
            guard let id = g["id"], !id.isEmpty else { return }
            self?.play(id)
        }
        let optionsAct = cardAct { [weak self] in self?.editLaunchOptions(g) }

        let name = NSTextField(wrappingLabelWithString: g["name"] ?? "?")
        name.font = .boldSystemFont(ofSize: 11)
        name.maximumNumberOfLines = 2
        name.lineBreakMode = .byTruncatingTail
        name.textColor = .labelColor
        name.frame = NSRect(x: 10, y: 30, width: coverCardW - 20, height: 32)

        let playButton = NSButton(title: "Play", target: a, action: #selector(Act.go))
        playButton.bezelStyle = .rounded
        playButton.controlSize = .small
        playButton.frame = NSRect(x: 10, y: 6, width: 56, height: 20)

        let more = NSButton(title: "…", target: optionsAct, action: #selector(Act.go))
        more.isBordered = false
        more.toolTip = "Launch options…"
        more.font = .boldSystemFont(ofSize: 13)
        more.frame = NSRect(x: 68, y: 6, width: 26, height: 20)

        box.addSubview(name)
        box.addSubview(playButton)
        box.addSubview(more)
        attachMenu(cardMenu(g, play: a, options: optionsAct), to: box)
        return box
    }

    /// Right-click menu shared by both card styles: the game as a disabled header, then Play and launch options.
    func cardMenu(_ g: [String: String], play: Act, options: Act) -> NSMenu {
        let m = NSMenu()
        let head = NSMenuItem(title: g["name"] ?? "?", action: nil, keyEquivalent: "")
        head.isEnabled = false
        m.addItem(head)
        m.addItem(.separator())
        let p = m.addItem(withTitle: "Play", action: #selector(Act.go), keyEquivalent: "")
        p.target = play
        let o = m.addItem(withTitle: "Launch options…", action: #selector(Act.go), keyEquivalent: "")
        o.target = options
        return m
    }

    /// Per-game launch options: the value is read from disk when the sheet opens, and saved through
    /// `Prefs.setLaunchOption`, which re-reads config.json and replaces just that one key (no stale write-back).
    func editLaunchOptions(_ g: [String: String]) {
        guard let id = g["id"], !id.isEmpty, let win = window else { return }
        let field = NSTextField(string: launchOptionValue(id))
        field.frame = NSRect(x: 0, y: 0, width: 380, height: 24)
        field.font = .systemFont(ofSize: 12)
        field.placeholderString = (g["steam_options"]).map { "Steam: \($0)" }
            ?? "e.g. FOO=1 %command% -windowed"

        let alert = NSAlert()
        alert.messageText = "Launch options — \(g["name"] ?? id)"
        alert.informativeText = "Steam syntax (VAR=x %command% -arg). Leave it empty to clear this game's options."
        alert.accessoryView = field
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        let save: (NSApplication.ModalResponse) -> Void = { response in
            guard response == .alertFirstButtonReturn else { return }
            prefs.setLaunchOption(id, field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        if win.isVisible {
            alert.beginSheetModal(for: win, completionHandler: save)
            DispatchQueue.main.async {
                win.makeFirstResponder(field)
                field.currentEditor()?.selectAll(nil)
            }
        } else {
            save(alert.runModal())
        }
    }

    /// Current value of launch_options[<id>] straight from the file, so a window that has been open for a while
    /// still shows what is really saved.
    func launchOptionValue(_ id: String) -> String {
        guard let d = FileManager.default.contents(atPath: home + "/config.json"),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let o = j["launch_options"] as? [String: Any] else { return "" }
        return o[id] as? String ?? ""
    }

    /// Steam keeps portrait library art at appcache/librarycache/<appid>/library_600x900.jpg; `games --json` only
    /// reports `icon` when that file exists, so the key alone decides the card style (no JPEG decode per rebuild).
    func hasCover(_ g: [String: String]) -> Bool { !(g["icon"] ?? "").isEmpty }

    func coverImage(_ g: [String: String]) -> NSImage? {
        guard let path = g["icon"], !path.isEmpty else { return nil }
        return NSImage(contentsOfFile: path) // local Steam cache only, nothing is downloaded
    }

    func gameIcon(_ g: [String: String]) -> NSImage? {
        if let path = g["icon"], let img = NSImage(contentsOfFile: path) { return img } // local Steam cache only
        if let app = g["app"] { return NSWorkspace.shared.icon(forFile: app) }
        return NSImage(systemSymbolName: "gamecontroller", accessibilityDescription: "Game")
    }

    func makeWindow() -> NSWindow {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 560),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "Pluvia"
        w.isReleasedWhenClosed = false
        w.minSize = NSSize(width: 520, height: 380)
        w.delegate = self

        statusLabel.font = .boldSystemFont(ofSize: 15)
        detailLabel.font = .systemFont(ofSize: 11)
        detailLabel.textColor = .secondaryLabelColor
        steamNote.font = .systemFont(ofSize: 10)
        steamNote.textColor = .secondaryLabelColor
        steamCheckLabel.font = .systemFont(ofSize: 10)
        steamCheckLabel.textColor = .secondaryLabelColor
        steamCheckLabel.preferredMaxLayoutWidth = 320

        let status = NSStackView(views: [statusLabel, detailLabel])
        status.orientation = .vertical
        status.alignment = .leading
        status.spacing = 2

        let watchRow = NSStackView(views: [label("Optimize while playing"), watchSwitch])
        watchRow.orientation = .horizontal
        watchRow.spacing = 8
        let steamRow = NSStackView(views: [label("Steam Fix"), steamSwitch, steamNote])
        steamRow.orientation = .horizontal
        steamRow.spacing = 8
        let subtitle = NSTextField(wrappingLabelWithString: "Fixes Steam Store/Community/images blocked by your ISP")
        subtitle.font = .systemFont(ofSize: 10)
        subtitle.textColor = .secondaryLabelColor
        subtitle.preferredMaxLayoutWidth = 300

        let toggles = NSStackView(views: [watchRow, steamRow, steamCheckLabel, subtitle])
        toggles.orientation = .vertical
        toggles.alignment = .leading
        toggles.spacing = 4

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        spacer.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let header = NSStackView(views: [status, spacer, toggles])
        header.orientation = .horizontal
        header.alignment = .top
        header.spacing = 12

        watchSwitch.target = act { [weak self] in self?.toggleWatchClicked() }
        watchSwitch.action = #selector(Act.go)
        steamSwitch.target = act { [weak self] in self?.toggleSteamfixClicked() }
        steamSwitch.action = #selector(Act.go)

        search.placeholderString = "Search games"
        search.delegate = self
        search.target = act { [weak self] in self?.rebuildGrid() }
        search.action = #selector(Act.go)

        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        scroll.documentView = doc

        let root = NSStackView(views: [header, search, scroll])
        root.orientation = .vertical
        root.alignment = .width
        root.spacing = 12
        root.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        root.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: content.topAnchor),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
        ])
        w.contentView = content
        w.center()
        return w
    }
}

let launcher = Launcher()
