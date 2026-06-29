// InfoGrep launcher — a Spotlight-style search bar for the InfoGrep index.
//
// A menu-bar (agent) app: press the global hotkey (default ⌘⇧-Space) to pop a search
// panel, type to search the local InfoGrep web API, ↑/↓ to navigate, ↵ to reveal the
// file in Finder, Esc to dismiss. The backend is `infogrep serve` (http://127.0.0.1:7421).

import AppKit
import Carbon.HIToolbox

let kAPIBase = "http://127.0.0.1:7421"
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
}

func apiSearch(_ query: String, completion: @escaping ([SearchResult]) -> Void) {
    guard var comp = URLComponents(string: "\(kAPIBase)/api/search") else { completion([]); return }
    comp.queryItems = [
        URLQueryItem(name: "q", value: query),
        URLQueryItem(name: "mode", value: kSearchMode),
        URLQueryItem(name: "k", value: String(kMaxResults)),
    ]
    guard let url = comp.url else { completion([]); return }
    var req = URLRequest(url: url)
    req.timeoutInterval = 10
    URLSession.shared.dataTask(with: req) { data, _, _ in
        var out: [SearchResult] = []
        if let data = data,
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let arr = obj["results"] as? [[String: Any]] {
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
        DispatchQueue.main.async { completion(out) }
    }.resume()
}

@MainActor
final class AppController: NSObject, NSApplicationDelegate, NSTextFieldDelegate,
                          NSTableViewDataSource, NSTableViewDelegate {
    var statusItem: NSStatusItem!
    var panel: NSPanel!
    var field: NSTextField!
    var table: NSTableView!
    var results: [SearchResult] = []
    var hotKeyRef: EventHotKeyRef?
    var seq = 0
    var debounce: DispatchWorkItem?

    func applicationDidFinishLaunching(_ note: Notification) {
        setupStatusItem()
        setupPanel()
        registerHotKey()
    }

    // MARK: menu bar
    func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "🔎"
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Search…  (⌘⇧Space)", action: #selector(togglePanel), keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit InfoGrep", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        for item in menu.items where item.action == #selector(togglePanel) { item.target = self }
        statusItem.menu = menu
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

        field = NSTextField(frame: NSRect(x: 16, y: h - 56, width: w - 32, height: 40))
        field.placeholderString = "Search InfoGrep…"
        field.font = NSFont.systemFont(ofSize: 24, weight: .light)
        field.isBezeled = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.delegate = self
        field.autoresizingMask = [.width, .minYMargin]
        blur.addSubview(field)

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
        table.rowHeight = 52
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
        panel.makeFirstResponder(field)
        field.stringValue = ""
        results = []
        table.reloadData()
    }

    func hidePanel() { panel.orderOut(nil) }

    // MARK: search
    func controlTextDidChange(_ note: Notification) {
        let q = field.stringValue.trimmingCharacters(in: .whitespaces)
        debounce?.cancel()
        if q.isEmpty { results = []; table.reloadData(); return }
        seq += 1
        let mySeq = seq
        let work = DispatchWorkItem { [weak self] in
            apiSearch(q) { res in
                guard let self, mySeq == self.seq else { return }
                self.results = res
                self.table.reloadData()
                if !res.isEmpty { self.table.selectRowIndexes([0], byExtendingSelection: false) }
            }
        }
        debounce = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12, execute: work)
    }

    // Intercept arrows / return / escape from the search field.
    func control(_ control: NSControl, textView: NSTextView, doCommandBy sel: Selector) -> Bool {
        switch sel {
        case #selector(NSResponder.moveDown(_:)): move(1); return true
        case #selector(NSResponder.moveUp(_:)): move(-1); return true
        case #selector(NSResponder.insertNewline(_:)): openSelected(); return true
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
        let sub = NSTextField(labelWithString: r.snippet.isEmpty ? r.path : r.snippet)
        sub.font = NSFont.systemFont(ofSize: 11)
        sub.textColor = .secondaryLabelColor
        sub.lineBreakMode = .byTruncatingTail
        name.translatesAutoresizingMaskIntoConstraints = false
        sub.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(name); v.addSubview(sub)
        NSLayoutConstraint.activate([
            name.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 16),
            name.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -16),
            name.topAnchor.constraint(equalTo: v.topAnchor, constant: 7),
            sub.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 16),
            sub.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -16),
            sub.topAnchor.constraint(equalTo: name.bottomAnchor, constant: 2),
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
