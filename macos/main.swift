// InfoGrep launcher — a Spotlight-style search bar for the InfoGrep index.
//
// A menu-bar (agent) app: press the global hotkey (default ⌘⇧-Space) to pop a search
// panel, type to search the local InfoGrep web API, ↑/↓ to navigate, ↵ to reveal the
// file in Finder, Esc to dismiss. The backend is `infogrep serve` (http://127.0.0.1:7421).
//
// Standalone builds (build.sh --standalone) ship the backend inside the app bundle
// (Resources/python + backend + jre); if nothing answers on the port at launch, the
// app starts that bundled server itself and stops it on quit. Thin builds have no
// bundled backend and rely on an externally managed `infogrep serve` as before.

import AppKit
import Carbon.HIToolbox

let kPort = "7421"
let kAPIBase = "http://127.0.0.1:\(kPort)"
let kSearchMode = "hybrid"
let kMaxResults = 25

// Bridge so the C hotkey callback can reach the controller (single, app-lifetime instance).
nonisolated(unsafe) weak var gController: AppController?

func fourCharCode(_ s: String) -> FourCharCode {
    var code: FourCharCode = 0
    for ch in s.utf8.prefix(4) { code = (code << 8) + FourCharCode(ch) }
    return code
}

struct SearchResult {
    let path: String
    let absPath: String?
    let snippet: String
    let score: Double
    let page: Int?
    let ext: String?
    let retriever: String
    var filename: String { (path as NSString).lastPathComponent }
    var displayPath: String { absPath ?? path }
}

// Calls back with the results plus an error message when the search failed (server
// down, backend error), so the panel can say what went wrong instead of showing nothing.
// `notes` adds the Obsidian knowledge base to the search; it is left out by default
// because it answers through the Obsidian app, far slower than the local indexes.
func apiSearch(_ query: String, dir: String?, allFolders: Bool, notes: Bool,
               completion: @escaping ([SearchResult], String?) -> Void) {
    guard var comp = URLComponents(string: "\(kAPIBase)/api/search") else { completion([], nil); return }
    comp.queryItems = [
        URLQueryItem(name: "q", value: query),
        URLQueryItem(name: "mode", value: kSearchMode),
        URLQueryItem(name: "k", value: String(kMaxResults)),
    ]
    if allFolders {
        comp.queryItems?.append(URLQueryItem(name: "scope", value: "all"))
    } else if let dir, !dir.isEmpty {
        comp.queryItems?.append(URLQueryItem(name: "dir", value: dir))
    }
    if !notes { comp.queryItems?.append(URLQueryItem(name: "exclude", value: "kb")) }
    guard let url = comp.url else { completion([], nil); return }
    var req = URLRequest(url: url)
    req.timeoutInterval = 30  // first search after a (re)index can be slow to warm up
    URLSession.shared.dataTask(with: req) { data, _, err in
        var out: [SearchResult] = []
        var msg: String?
        if err != nil {
            msg = "Can't reach InfoGrep — is `infogrep serve` running?"
        } else if let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let e = obj["error"] as? String, !e.isEmpty { msg = e }
            if let arr = obj["results"] as? [[String: Any]] {
                for r in arr {
                    out.append(SearchResult(
                        path: r["path"] as? String ?? "",
                        absPath: r["abs_path"] as? String,
                        snippet: r["snippet"] as? String ?? "",
                        score: r["score"] as? Double ?? 0,
                        page: r["page"] as? Int,
                        ext: r["ext"] as? String,
                        retriever: r["retriever"] as? String ?? ""))
                }
            }
        } else {
            msg = "Unexpected response from the InfoGrep server"
        }
        DispatchQueue.main.async { completion(out, msg) }
    }.resume()
}

struct IndexEntry { let name: String; let dir: String; let indexed: Bool
                    let indexing: Bool; let scheduled: Bool
                    let kb: Bool }  // an Obsidian knowledge base is configured for it

@MainActor
final class AppController: NSObject, NSApplicationDelegate, NSTextFieldDelegate,
                          NSTableViewDataSource, NSTableViewDelegate, NSMenuDelegate {
    var statusItem: NSStatusItem!
    var panel: NSPanel!
    var field: NSTextField!
    var spinner: NSProgressIndicator!
    var notesToggle: NSButton!
    var statusLabel: NSTextField!
    var table: NSTableView!
    var results: [SearchResult] = []
    var hotKeyRef: EventHotKeyRef?
    var seq = 0
    var debounce: DispatchWorkItem?  // set only while a search waits out the typing pause
    var loading = false  // a search for the field's current text is pending or running
    // Which index to search (nil = the server's default). Persisted across launches.
    var indexes: [IndexEntry] = []
    var selectedDir: String? = UserDefaults.standard.string(forKey: "infogrep.selectedDir")
    // Search every indexed folder at once — the default — rather than selectedDir alone.
    var searchAll = UserDefaults.standard.object(forKey: "infogrep.searchAll") as? Bool ?? true
    // "All" only differs from the one folder there is once a second one exists.
    var searchingAll: Bool { searchAll && indexes.count > 1 }

    func currentFolderName() -> String {
        if searchingAll { return "all folders" }
        if let d = selectedDir, !d.isEmpty { return (d as NSString).lastPathComponent }
        return "InfoGrep"
    }
    func updatePlaceholder() { field?.placeholderString = "Search \(currentFolderName())…" }
    // The Notes checkbox only means something where a knowledge base is configured.
    func updateNotesToggle() {
        let searched = searchingAll ? indexes : indexes.filter { $0.dir == selectedDir }
        notesToggle?.isHidden = !searched.contains(where: { $0.kb })
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        setupMainMenu()
        setupStatusItem()
        setupPanel()
        registerHotKey()
        ensureBackend()
    }

    // MARK: main menu
    // An agent app shows no menu bar, but AppKit still routes ⌘-key equivalents through
    // the main menu — and cut/copy/paste/undo reach a text field *only* that way (a field
    // editor has no performKeyEquivalent: of its own). Without this menu, ⌘V in the search
    // field silently does nothing. Never drawn; it exists purely for the shortcuts.
    func setupMainMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "Quit InfoGrep",
                                   action: #selector(NSApplication.terminate(_:)),
                                   keyEquivalent: "q"))
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        // undo:/redo: aren't exposed on NSResponder in Swift, hence the string selectors.
        let entries: [(String, Selector, String)] = [
            ("Undo", Selector(("undo:")), "z"),
            ("Redo", Selector(("redo:")), "Z"),
            ("Cut", #selector(NSText.cut(_:)), "x"),
            ("Copy", #selector(NSText.copy(_:)), "c"),
            ("Paste", #selector(NSText.paste(_:)), "v"),
            ("Select All", #selector(NSText.selectAll(_:)), "a"),
        ]
        for (title, action, key) in entries {
            // target stays nil so each action travels the responder chain to the field editor.
            editMenu.addItem(NSMenuItem(title: title, action: action, keyEquivalent: key))
        }
        editItem.submenu = editMenu
        main.addItem(editItem)

        NSApp.mainMenu = main
    }

    func applicationWillTerminate(_ note: Notification) {
        serverProcess?.terminate()  // only set when this app started the bundled server
    }

    // MARK: bundled backend (standalone builds)
    var serverProcess: Process?

    /// Reach the API; if nothing answers and this build bundles a backend, start it.
    func ensureBackend() {
        pingServer { up in
            if up { self.refreshIndexes(); return }
            self.startBundledServer()
            self.waitForBackend(until: Date().addingTimeInterval(40))
        }
    }

    func pingServer(_ done: @escaping @MainActor (Bool) -> Void) {
        guard let url = URL(string: "\(kAPIBase)/api/status") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { data, _, err in
            let up = (err == nil && data != nil)
            DispatchQueue.main.async { done(up) }
        }.resume()
    }

    func startBundledServer() {
        if let p = serverProcess, p.isRunning { return }
        guard let res = Bundle.main.resourcePath else { return }
        let py = res + "/python/bin/python3"
        guard FileManager.default.isExecutableFile(atPath: py) else { return }  // thin build
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: py)
        proc.arguments = ["-m", "infogrep", "serve",
                          "--dir", NSHomeDirectory(), "--port", kPort,
                          "--exit-with-parent"]  // server dies with the app, even on crash/SIGKILL
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = res + "/backend"
        env["PYTHONNOUSERSITE"] = "1"
        // Never write .pyc into the bundle: that would invalidate its code signature and
        // macOS would refuse the app as "damaged" on the next launch. build.sh precompiles
        // the backend before signing, so the sealed bytecode is already there.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if FileManager.default.isExecutableFile(atPath: res + "/jre/bin/java") {
            env["JAVA_HOME"] = res + "/jre"
        }
        proc.environment = env
        let logDir = NSHomeDirectory() + "/.infogrep"
        try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
        let log = logDir + "/app-server.log"
        if !FileManager.default.fileExists(atPath: log) {
            FileManager.default.createFile(atPath: log, contents: nil)
        }
        if let h = FileHandle(forWritingAtPath: log) {
            h.seekToEndOfFile()
            proc.standardOutput = h
            proc.standardError = h
        }
        do { try proc.run(); serverProcess = proc }
        catch { NSLog("InfoGrep: could not start the bundled server: \(error)") }
    }

    func waitForBackend(until deadline: Date) {
        pingServer { up in
            if up { self.refreshIndexes(); return }
            if Date() < deadline {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) {
                    self.waitForBackend(until: deadline)
                }
            }
        }
    }

    // MARK: menu bar
    func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "🔎"
        let menu = NSMenu()
        menu.delegate = self  // refresh the index list each time the menu opens
        statusItem.menu = menu
        rebuildMenu()
    }

    func menuWillOpen(_ menu: NSMenu) { refreshIndexes() }

    func rebuildMenu() {
        guard let menu = statusItem?.menu else { return }
        menu.removeAllItems()
        let header = NSMenuItem(title: "Search in:", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        if indexes.isEmpty {
            let none = NSMenuItem(title: "   (no indexes yet)", action: nil, keyEquivalent: "")
            none.isEnabled = false
            menu.addItem(none)
        }
        if indexes.count > 1 {
            let all = NSMenuItem(title: "   All Folders", action: #selector(selectAll), keyEquivalent: "")
            all.target = self
            all.state = searchingAll ? .on : .off
            menu.addItem(all)
        }
        for idx in indexes {
            let title = "   " + idx.name
                + (idx.indexing ? "  (indexing…)" : (idx.indexed ? "" : "  (not indexed)"))
            let item = NSMenuItem(title: title, action: #selector(selectIndex(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = idx.dir
            item.state = (!searchingAll && idx.dir == selectedDir) ? .on : .off
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let add = NSMenuItem(title: "Index a Folder…", action: #selector(indexFolder), keyEquivalent: "i")
        add.target = self
        menu.addItem(add)
        // One switch per searched folder: every folder under "All Folders", else the selected one.
        for idx in indexes where searchingAll || idx.dir == selectedDir {
            let daily = NSMenuItem(title: "Daily Re-index of \(idx.name)  (3 AM)",
                                   action: #selector(toggleSchedule(_:)), keyEquivalent: "")
            daily.target = self
            daily.representedObject = idx.dir
            daily.state = idx.scheduled ? .on : .off  // checkmark = daily updates enabled
            menu.addItem(daily)
        }
        menu.addItem(.separator())
        let search = NSMenuItem(title: "Search…  (⌘⇧Space)", action: #selector(togglePanel), keyEquivalent: "")
        search.target = self
        menu.addItem(search)
        menu.addItem(NSMenuItem(title: "Quit InfoGrep",
                                action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }

    func refreshIndexes() {
        guard let url = URL(string: "\(kAPIBase)/api/indexes") else { return }
        URLSession.shared.dataTask(with: url) { data, _, _ in
            var list: [IndexEntry] = []
            var defaultDir: String?
            if let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                defaultDir = obj["default"] as? String
                if let arr = obj["indexes"] as? [[String: Any]] {
                    for i in arr where (i["dir"] as? String) != nil {
                        let dir = i["dir"] as! String
                        list.append(IndexEntry(
                            name: (i["name"] as? String) ?? (dir as NSString).lastPathComponent,
                            dir: dir,
                            indexed: (i["indexed"] as? Bool) ?? false,
                            indexing: (i["indexing"] as? Bool) ?? false,
                            scheduled: (i["scheduled"] as? Bool) ?? false,
                            kb: (i["kb"] as? Bool) ?? false))
                    }
                }
            }
            DispatchQueue.main.async {
                self.indexes = list
                // Searching a folder without an index silently returns nothing, so if
                // the remembered selection isn't searchable (gone, or never indexed and
                // not being built), fall back to the server default or any indexed folder.
                func usable(_ d: String?) -> String? {
                    guard let d, let e = list.first(where: { $0.dir == d }),
                          e.indexed || e.indexing else { return nil }
                    return d
                }
                if usable(self.selectedDir) == nil {
                    let pick = usable(defaultDir) ?? list.first(where: { $0.indexed })?.dir
                    self.selectedDir = pick
                    if let pick { UserDefaults.standard.set(pick, forKey: "infogrep.selectedDir") }
                }
                self.rebuildMenu()
                self.updatePlaceholder()
                self.updateNotesToggle()
            }
        }.resume()
    }

    @objc func selectIndex(_ sender: NSMenuItem) {
        guard let dir = sender.representedObject as? String else { return }
        selectedDir = dir
        UserDefaults.standard.set(dir, forKey: "infogrep.selectedDir")
        setSearchAll(false)
    }

    @objc func selectAll() { setSearchAll(true) }

    func setSearchAll(_ on: Bool) {
        searchAll = on
        UserDefaults.standard.set(on, forKey: "infogrep.searchAll")
        rebuildMenu()
        updatePlaceholder()
        updateNotesToggle()
    }

    @objc func toggleSchedule(_ sender: NSMenuItem) {
        guard let dir = sender.representedObject as? String,
              let idx = indexes.first(where: { $0.dir == dir }) else { return }
        let turnOn = !idx.scheduled
        guard var comp = URLComponents(string: "\(kAPIBase)/api/schedule") else { return }
        comp.queryItems = [URLQueryItem(name: "dir", value: dir),
                           URLQueryItem(name: "on", value: turnOn ? "1" : "0")]
        guard let u = comp.url else { return }
        var req = URLRequest(url: u)
        req.httpMethod = "POST"
        URLSession.shared.dataTask(with: req) { _, _, _ in
            DispatchQueue.main.async { self.refreshIndexes() }
        }.resume()
    }

    @objc func indexFolder() {
        let p = NSOpenPanel()
        p.canChooseDirectories = true
        p.canChooseFiles = false
        p.allowsMultipleSelection = false
        p.prompt = "Index"
        p.message = "Choose a folder to index with InfoGrep"
        NSApp.activate(ignoringOtherApps: true)
        guard p.runModal() == .OK, let url = p.url else { return }
        let dir = url.path
        guard var comp = URLComponents(string: "\(kAPIBase)/api/index") else { return }
        comp.queryItems = [URLQueryItem(name: "dir", value: dir)]
        guard let u = comp.url else { return }
        var req = URLRequest(url: u)
        req.httpMethod = "POST"
        URLSession.shared.dataTask(with: req) { _, _, _ in
            DispatchQueue.main.async {
                self.selectedDir = dir
                UserDefaults.standard.set(dir, forKey: "infogrep.selectedDir")
                self.refreshIndexes()  // will show "(indexing…)" until it finishes
                self.updatePlaceholder()
            }
        }.resume()
    }

    // MARK: panel UI
    func setupPanel() {
        let w: CGFloat = 720, h: CGFloat = 440
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: w, height: h),
                        styleMask: [.titled, .fullSizeContentView, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isMovableByWindowBackground = true
        panel.level = .floating
        panel.hidesOnDeactivate = true
        panel.isFloatingPanel = true
        panel.standardWindowButton(.closeButton)?.isHidden = true
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true

        let blur = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        blur.material = .hudWindow
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.autoresizingMask = [.width, .height]
        blur.wantsLayer = true
        blur.layer?.cornerRadius = 12
        panel.contentView = blur

        field = NSTextField(frame: NSRect(x: 16, y: h - 56, width: w - 136, height: 40))
        field.placeholderString = "Search InfoGrep…"
        field.font = NSFont.systemFont(ofSize: 24, weight: .light)
        field.isBezeled = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.delegate = self
        field.autoresizingMask = [.width, .minYMargin]
        blur.addSubview(field)

        // Spins from the moment the query changes until its results are on screen, so
        // there is always a visible answer to "did my search go through?".
        spinner = NSProgressIndicator(frame: NSRect(x: w - 38, y: h - 46, width: 20, height: 20))
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        spinner.autoresizingMask = [.minXMargin, .minYMargin]
        blur.addSubview(spinner)

        // Off by default, and off again at every launch: searching notes goes through
        // the Obsidian app, which is what makes a search slow. Hidden until the selected
        // folder is known to have a knowledge base (see updateNotesToggle).
        notesToggle = NSButton(checkboxWithTitle: "Notes", target: self,
                               action: #selector(notesToggled))
        notesToggle.frame = NSRect(x: w - 110, y: h - 46, width: 66, height: 20)
        notesToggle.controlSize = .small
        notesToggle.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        notesToggle.toolTip = "Also search the Obsidian knowledge base (slower)"
        notesToggle.refusesFirstResponder = true  // typing stays in the search field
        notesToggle.autoresizingMask = [.minXMargin, .minYMargin]
        notesToggle.isHidden = true
        blur.addSubview(notesToggle)

        let sep = NSBox(frame: NSRect(x: 0, y: h - 64, width: w, height: 1))
        sep.boxType = .separator
        sep.autoresizingMask = [.width, .minYMargin]
        blur.addSubview(sep)

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: w, height: h - 64))
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.autoresizingMask = [.width, .height]
        table = NSTableView()
        table.headerView = nil
        table.backgroundColor = .clear
        table.rowHeight = 64
        table.intercellSpacing = NSSize(width: 0, height: 0)
        table.selectionHighlightStyle = .regular
        let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("c"))
        col.width = w
        table.addTableColumn(col)
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.doubleAction = #selector(openSelected)
        scroll.documentView = table
        blur.addSubview(scroll)

        // Feedback line under the search field: "searching", errors and "no results"
        // (hidden otherwise).
        statusLabel = NSTextField(labelWithString: "")
        statusLabel.frame = NSRect(x: 16, y: h - 100, width: w - 32, height: 20)
        statusLabel.font = NSFont.systemFont(ofSize: 13)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.alignment = .center
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.autoresizingMask = [.width, .minYMargin]
        statusLabel.isHidden = true
        blur.addSubview(statusLabel)
    }

    func showStatus(_ message: String?) {
        statusLabel.stringValue = message ?? ""
        statusLabel.isHidden = (message == nil)
    }

    func setLoading(_ on: Bool) {
        loading = on
        if on {
            spinner.startAnimation(nil)
            if results.isEmpty { showStatus("Searching…") }
        } else {
            spinner.stopAnimation(nil)
        }
    }

    // MARK: hotkey
    func registerHotKey() {
        gController = self
        let id = EventHotKeyID(signature: fourCharCode("IGrp"), id: 1)
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetEventDispatcherTarget(), { (_, _, _) -> OSStatus in
            DispatchQueue.main.async { gController?.togglePanel() }
            return noErr
        }, 1, &spec, nil, nil)
        // ⌘⇧-Space (Command+Shift+Space). Change the modifiers/key code to rebind.
        RegisterEventHotKey(UInt32(kVK_Space), UInt32(cmdKey | shiftKey), id,
                            GetEventDispatcherTarget(), 0, &hotKeyRef)
    }

    @objc func togglePanel() {
        if panel.isVisible { hidePanel(); return }
        if let screen = NSScreen.main {
            let f = screen.visibleFrame
            let origin = NSPoint(x: f.midX - panel.frame.width / 2,
                                 y: f.midY - panel.frame.height / 2 + 80)
            panel.setFrameOrigin(origin)
        }
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        updatePlaceholder()
        updateNotesToggle()
        warmBackend()
        // Keep the last query/results so reopening the panel picks up where it left off;
        // select the text so typing immediately replaces it, like Spotlight.
        field.selectText(nil)
    }

    func hidePanel() { panel.orderOut(nil) }

    /// The first query after the server (re)starts pays a one-time second or so (JVM
    /// boot, index open). Ask for that work as the panel opens, so it overlaps with typing.
    func warmBackend() {
        guard var comp = URLComponents(string: "\(kAPIBase)/api/warm") else { return }
        if searchingAll {
            comp.queryItems = [URLQueryItem(name: "scope", value: "all")]
        } else if let dir = selectedDir, !dir.isEmpty {
            comp.queryItems = [URLQueryItem(name: "dir", value: dir)]
        }
        guard let url = comp.url else { return }
        URLSession.shared.dataTask(with: url).resume()
    }

    // MARK: search
    func query() -> String { field.stringValue.trimmingCharacters(in: .whitespaces) }

    func controlTextDidChange(_ note: Notification) {
        // While an input method (pinyin, kana, …) is composing, the field holds raw
        // keystrokes rather than the query. Committing the text fires this again.
        if let editor = field.currentEditor() as? NSTextView, editor.hasMarkedText() { return }
        debounce?.cancel()
        debounce = nil
        seq += 1  // whatever is in flight no longer matches the field
        if query().isEmpty {
            results = []; table.reloadData(); setLoading(false); showStatus(nil)
            return
        }
        setLoading(true)
        let work = DispatchWorkItem { [weak self] in self?.runSearch() }
        debounce = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12, execute: work)
    }

    func runSearch() {
        debounce?.cancel()  // ↵ can get here before the typing pause has run out
        debounce = nil
        let q = query()
        guard !q.isEmpty else { return }
        let mySeq = seq
        apiSearch(q, dir: selectedDir, allFolders: searchingAll,
                  notes: notesToggle.state == .on) { [weak self] res, err in
            guard let self, mySeq == self.seq else { return }
            self.results = res
            self.table.reloadData()
            self.setLoading(false)
            if let err {
                self.showStatus(err)
                self.ensureBackend()  // restart the bundled server if it died
            } else if res.isEmpty {
                self.showStatus(self.indexes.contains(where: { $0.indexed })
                    ? "No results in \(self.currentFolderName())"
                    : "No indexes yet — choose “Index a Folder…” from the 🔎 menu")
            } else {
                self.showStatus(nil)
                self.table.selectRowIndexes([0], byExtendingSelection: false)
            }
        }
    }

    // The list on screen was fetched with the old setting: search again right away.
    @objc func notesToggled() {
        guard !query().isEmpty else { return }
        seq += 1
        setLoading(true)
        runSearch()
    }

    // ↵ opens the selected result, but only once the list belongs to the text in the
    // field. Before that it means "search now": it skips the rest of the typing pause,
    // and never opens a hit left over from an earlier, shorter query.
    func submit() {
        if debounce != nil { runSearch() } else if !loading { openSelected() }
    }

    // Intercept arrows / return / escape from the search field.
    func control(_ control: NSControl, textView: NSTextView, doCommandBy sel: Selector) -> Bool {
        switch sel {
        case #selector(NSResponder.moveDown(_:)): move(1); return true
        case #selector(NSResponder.moveUp(_:)): move(-1); return true
        case #selector(NSResponder.insertNewline(_:)): submit(); return true
        case #selector(NSResponder.cancelOperation(_:)): hidePanel(); return true
        default: return false
        }
    }

    func move(_ delta: Int) {
        guard !results.isEmpty else { return }
        let cur = table.selectedRow
        let next = max(0, min(results.count - 1, (cur < 0 ? 0 : cur) + delta))
        table.selectRowIndexes([next], byExtendingSelection: false)
        table.scrollRowToVisible(next)
    }

    @objc func openSelected() {
        let row = table.selectedRow
        guard row >= 0, row < results.count, let abs = results[row].absPath else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: abs)])
        hidePanel()
    }

    // MARK: table data
    func numberOfRows(in tableView: NSTableView) -> Int { results.count }

    func tableView(_ tableView: NSTableView, viewFor col: NSTableColumn?, row: Int) -> NSView? {
        let r = results[row]
        let v = NSView()
        let name = NSTextField(labelWithString: r.filename + (r.page != nil ? "  · p.\(r.page!)" : ""))
        name.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        name.textColor = .labelColor
        let pathLabel = NSTextField(labelWithString: r.displayPath)
        pathLabel.font = NSFont.systemFont(ofSize: 10)
        pathLabel.textColor = .tertiaryLabelColor
        pathLabel.lineBreakMode = .byTruncatingMiddle
        let sub = NSTextField(labelWithString: r.snippet)
        sub.font = NSFont.systemFont(ofSize: 11)
        sub.textColor = .secondaryLabelColor
        sub.lineBreakMode = .byTruncatingTail
        name.translatesAutoresizingMaskIntoConstraints = false
        pathLabel.translatesAutoresizingMaskIntoConstraints = false
        sub.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(name); v.addSubview(pathLabel); v.addSubview(sub)
        NSLayoutConstraint.activate([
            name.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 16),
            name.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -16),
            name.topAnchor.constraint(equalTo: v.topAnchor, constant: 7),
            pathLabel.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 16),
            pathLabel.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -16),
            pathLabel.topAnchor.constraint(equalTo: name.bottomAnchor, constant: 1),
            sub.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 16),
            sub.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -16),
            sub.topAnchor.constraint(equalTo: pathLabel.bottomAnchor, constant: 2),
        ])
        return v
    }
}

// Top-level code runs on the main thread at launch; enter the main actor to build the
// @MainActor controller, then hand control to AppKit's run loop.
MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)  // agent app (no Dock icon)
    let controller = AppController()
    app.delegate = controller
    app.run()
}
