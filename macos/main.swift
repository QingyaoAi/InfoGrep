// InfoGrep launcher — a Spotlight-style search bar for the InfoGrep index.
//
// A menu-bar (agent) app: press the global hotkey (default ⌘⇧-Space) to pop a search
// panel, type to search the local InfoGrep web API, ↑/↓ to navigate, ↵ to reveal the
// file in Finder, Esc to dismiss. The backend is `infogrep serve` (http://127.0.0.1:7421).
// The Ask button (or Tab) turns the panel from searching to asking: ↵ then sends the
// text as a question to a DeepSeek Harness agent on the server, and the answer shows
// where the results were.
//
// Standalone builds (build.sh --standalone) ship the backend inside the app bundle
// (Resources/python + backend + jre); if nothing answers on the port at launch, the
// app starts that bundled server itself and stops it on quit. Thin builds have no
// bundled backend and rely on an externally managed `infogrep serve` as before.

import AppKit
import Carbon.HIToolbox
import QuickLookThumbnailing
import UniformTypeIdentifiers

let kPort = "7421"
let kAPIBase = "http://127.0.0.1:\(kPort)"
let kSearchMode = "hybrid"
let kMaxResults = 25
let kFilterBarHeight: CGFloat = 32  // strip between the search field and the results

// One colour per kind of file (keyed by the server's file-type ids), for the tag that
// names a result's type. System colours, so they follow the appearance.
let kTypeColors: [String: NSColor] = [
    "pdf": .systemRed, "doc": .systemBlue, "slides": .systemOrange, "sheet": .systemGreen,
    "text": .systemGray, "image": .systemPurple, "data": .systemTeal,
]
let kIconSide: CGFloat = 44  // the box a row's thumbnail is fitted into
let kThumbnailsKept = 400  // most recent thumbnails held in memory (~12 MB at ~30 KB each)

/// The small capsule that names a result's file type: "PDF", "DOCX", … in its colour.
func typeTag(ext: String, color: NSColor) -> NSImage {
    let label = ext.uppercased() as NSString
    let attrs: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: 10.5, weight: .semibold),
        .foregroundColor: color,
    ]
    let text = label.size(withAttributes: attrs)
    return NSImage(size: NSSize(width: ceil(text.width) + 14, height: 18), flipped: false) { rect in
        color.withAlphaComponent(0.2).setFill()
        NSBezierPath(roundedRect: rect, xRadius: 9, yRadius: 9).fill()
        label.draw(at: NSPoint(x: 7, y: (rect.height - text.height) / 2), withAttributes: attrs)
        return true
    }
}

/// A thumbnail straight from the image decoder. Quick Look now and then refuses a
/// picture it ought to manage (seen with an ordinary 300x167 PNG); ImageIO still reads it.
func imageThumbnail(_ path: String, pixels: CGFloat) -> NSImage? {
    let options: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,  // honour EXIF rotation
        kCGImageSourceThumbnailMaxPixelSize: pixels,
    ]
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
    else { return nil }
    return NSImage(cgImage: image, size: NSSize(width: image.width, height: image.height))
}

/// `image` as a row shows it at its left: fitted to the icon box and drawn, once, into a
/// bitmap of exactly the pixels displayed (at most 88x88 on a Retina screen, ~30 KB).
///
/// Handing an image view the original instead makes Core Animation keep it at full
/// resolution for as long as it is cached: measured at ~4 MB per Finder icon (they carry
/// a 1024-pixel version) and ~180 KB per Quick Look thumbnail, which in a launcher that
/// runs for weeks is most of its memory. `framed` rounds the corners and adds a hairline
/// edge, so a thumbnail of a white page keeps its outline.
func rowImage(_ image: NSImage, scale: CGFloat, framed: Bool) -> NSImage {
    let full = image.size
    let fit = min(kIconSide / full.width, kIconSide / full.height)
    let size = NSSize(width: (full.width * fit).rounded(), height: (full.height * fit).rounded())
    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale), pixelsHigh: Int(size.height * scale),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)
    else { return image }
    // Size in points, set before the context is made: that is what makes the context
    // draw `scale` pixels per point. Set afterwards, a 44-point drawing lands in the
    // bottom-left quarter of an 88-pixel bitmap.
    bitmap.size = size
    guard let context = NSGraphicsContext(bitmapImageRep: bitmap) else { return image }
    let rect = NSRect(origin: .zero, size: size)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    if framed {
        let edge = NSBezierPath(roundedRect: rect.insetBy(dx: 0.5, dy: 0.5), xRadius: 4, yRadius: 4)
        NSGraphicsContext.saveGraphicsState()
        edge.addClip()
        image.draw(in: rect)
        NSGraphicsContext.restoreGraphicsState()
        NSColor(white: 0.6, alpha: 0.45).setStroke()
        edge.lineWidth = 1
        edge.stroke()
    } else {
        image.draw(in: rect)
    }
    NSGraphicsContext.restoreGraphicsState()
    let drawn = NSImage(size: size)
    drawn.addRepresentation(bitmap)
    return drawn
}

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
// `notes` adds the Obsidian knowledge base to the search (the Notes toggle, on by default).
// `type` (a file-type id from /api/indexes) keeps only files of that kind.
func apiSearch(_ query: String, dir: String?, allFolders: Bool, notes: Bool, type: String?,
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
    if let type { comp.queryItems?.append(URLQueryItem(name: "type", value: type)) }
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

/// What the server reports about a question: how far the agent has got, then its answer.
struct AskState {
    let status: String  // "running", then "done" | "error" | "cancelled"
    let steps: [String]  // the searches and files the agent has turned to so far
    let answer: String
    let sources: [String]  // absolute paths of the files the answer relies on
    let session: String?  // the conversation, to pass back for a follow-up
    let error: String
}

// Hands a question to the server's agent and calls back with the id to poll, or with
// why it couldn't start. `session` continues the conversation an earlier answer came from.
// As for a search, the knowledge base is only consulted when `notes` asks for it.
func apiAsk(_ question: String, dir: String?, allFolders: Bool, notes: Bool, session: String?,
            completion: @escaping (Int?, String?) -> Void) {
    guard var comp = URLComponents(string: "\(kAPIBase)/api/ask") else { completion(nil, nil); return }
    comp.queryItems = [URLQueryItem(name: "q", value: question)]
    if allFolders {
        comp.queryItems?.append(URLQueryItem(name: "scope", value: "all"))
    } else if let dir, !dir.isEmpty {
        comp.queryItems?.append(URLQueryItem(name: "dir", value: dir))
    }
    if notes { comp.queryItems?.append(URLQueryItem(name: "notes", value: "1")) }
    if let session { comp.queryItems?.append(URLQueryItem(name: "session", value: session)) }
    // URLComponents leaves "+" as it is, and the server reads a bare "+" as a space.
    comp.percentEncodedQuery = comp.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
    guard let url = comp.url else { completion(nil, nil); return }
    var req = URLRequest(url: url)
    req.httpMethod = "POST"
    req.timeoutInterval = 15
    URLSession.shared.dataTask(with: req) { data, _, err in
        var id: Int?
        var msg: String?
        if err != nil {
            msg = "Can't reach InfoGrep — is `infogrep serve` running?"
        } else if let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            id = obj["id"] as? Int
            if id == nil { msg = obj["error"] as? String ?? "The server could not take the question" }
        } else {
            msg = "Unexpected response from the InfoGrep server"
        }
        DispatchQueue.main.async { completion(id, msg) }
    }.resume()
}

// Calls back with nil when the server can't be reached.
func apiAskState(_ id: Int, completion: @escaping (AskState?) -> Void) {
    guard let url = URL(string: "\(kAPIBase)/api/ask?id=\(id)") else { completion(nil); return }
    var req = URLRequest(url: url)
    req.timeoutInterval = 10
    URLSession.shared.dataTask(with: req) { data, _, _ in
        var state: AskState?
        if let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let status = obj["status"] as? String {
            state = AskState(
                status: status,
                steps: obj["steps"] as? [String] ?? [],
                answer: obj["answer"] as? String ?? "",
                sources: obj["sources"] as? [String] ?? [],
                session: obj["session"] as? String,
                error: obj["error"] as? String ?? "")
        }
        DispatchQueue.main.async { completion(state) }
    }.resume()
}

/// An answer as the panel shows it: **bold**, *italic*, `code` and links are rendered,
/// while line breaks and list markers stay exactly as the agent wrote them.
func answerText(_ markdown: String, size: CGFloat) -> NSAttributedString {
    let base = NSFont.systemFont(ofSize: size)
    let plain: [NSAttributedString.Key: Any] = [.font: base, .foregroundColor: NSColor.labelColor]
    guard let parsed = try? AttributedString(
        markdown: markdown,
        options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))
    else { return NSAttributedString(string: markdown, attributes: plain) }
    let out = NSMutableAttributedString()
    for run in parsed.runs {
        var attrs = plain
        let intent = run.inlinePresentationIntent ?? []
        if intent.contains(.code) {
            attrs[.font] = NSFont.monospacedSystemFont(ofSize: size - 1, weight: .regular)
        } else {
            var traits: NSFontDescriptor.SymbolicTraits = []
            if intent.contains(.stronglyEmphasized) { traits.insert(.bold) }
            if intent.contains(.emphasized) { traits.insert(.italic) }
            if !traits.isEmpty {
                attrs[.font] = NSFont(descriptor: base.fontDescriptor.withSymbolicTraits(traits),
                                      size: size) ?? base
            }
        }
        if let link = run.link { attrs[.link] = link }
        out.append(NSAttributedString(string: String(parsed[run.range].characters), attributes: attrs))
    }
    return out
}

/// The answer area. Selecting text in it takes the keyboard focus, but whatever is
/// typed next is still meant for the question field — as are Tab, ↵ and Esc.
final class AnswerTextView: NSTextView {
    weak var typingTarget: NSTextField?

    // Blended into the panel's blur, as the labels of the result list are. A text view
    // is by default laid flat over it instead, which leaves its text duller and thinner
    // than theirs — coloured text, the links, most of all.
    override var allowsVibrancy: Bool { true }

    override func keyDown(with event: NSEvent) {
        guard let field = typingTarget, let window, window.makeFirstResponder(field),
              let editor = field.currentEditor()
        else { super.keyDown(with: event); return }
        editor.keyDown(with: event)
    }
}

struct IndexEntry { let name: String; let dir: String; let indexed: Bool
                    let indexing: Bool; let scheduled: Bool
                    let kb: Bool }  // an Obsidian knowledge base is configured for it

@MainActor
final class AppController: NSObject, NSApplicationDelegate, NSTextFieldDelegate,
                          NSTableViewDataSource, NSTableViewDelegate, NSMenuDelegate,
                          NSTextViewDelegate {
    var statusItem: NSStatusItem!
    var panel: NSPanel!
    var field: NSTextField!
    var spinner: NSProgressIndicator!
    var notesToggle: NSButton!
    var askButton: NSButton!
    var typeBar: NSSegmentedControl!
    var statusLabel: NSTextField!
    var table: NSTableView!
    var resultsScroll: NSScrollView!
    var answerScroll: NSScrollView!
    var answerView: AnswerTextView!
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
    // File-type filter: the kinds the server offers, and the one picked (nil = any).
    var fileTypes: [(id: String, label: String, exts: [String])] = []
    var selectedType: String?
    // Ask mode: ↵ sends the field's text to a DeepSeek Harness agent as a question
    // instead of searching for it. Search is the mode at every launch.
    var askMode = false
    var askAvailable = false  // the server has `dsh` to answer with (see /api/indexes)
    var asking = false  // a question is with the agent
    var askSeq = 0  // one per question, so word of an earlier one is ignored
    var askSession: String?  // the conversation the answer on screen belongs to
    var askShown = ""  // what the answer area was last drawn from
    var iconCache: [String: NSImage] = [:]  // Finder's icon per file extension
    var tagCache: [String: NSImage] = [:]  // the type tag per file extension
    let thumbnails = NSCache<NSString, NSImage>()  // framed Quick Look thumbnail per file

    func currentFolderName() -> String {
        if searchingAll { return "all folders" }
        if let d = selectedDir, !d.isEmpty { return (d as NSString).lastPathComponent }
        return "InfoGrep"
    }
    func updatePlaceholder() {
        field?.placeholderString = "\(askMode ? "Ask about" : "Search") \(currentFolderName())…"
    }
    // The Notes checkbox only means something where a knowledge base is configured. It
    // works the same in both modes: unticked, neither a search nor a question touches it.
    func updateNotesToggle() {
        let searched = searchingAll ? indexes : indexes.filter { $0.dir == selectedDir }
        notesToggle?.isHidden = !searched.contains(where: { $0.kb })
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        thumbnails.countLimit = kThumbnailsKept
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
        // A template symbol, so the menu bar tints it like its neighbours in either
        // appearance (an emoji stays full-colour and looks pasted on). The magnifier
        // with a spark in it is from macOS 14; the plain text one stands in before that.
        let symbol = ["sparkle.magnifyingglass", "text.magnifyingglass"].lazy
            .compactMap { NSImage(systemSymbolName: $0, accessibilityDescription: "InfoGrep") }
            .first?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 15, weight: .medium))
        symbol?.isTemplate = true
        statusItem.button?.image = symbol
        statusItem.button?.toolTip = "InfoGrep"
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
            var types: [(id: String, label: String, exts: [String])] = []
            var defaultDir: String?
            var canAsk = false
            if let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                defaultDir = obj["default"] as? String
                canAsk = obj["ask"] as? Bool ?? false
                for t in (obj["types"] as? [[String: Any]]) ?? [] {
                    if let id = t["id"] as? String, let label = t["label"] as? String {
                        types.append((id: id, label: label, exts: (t["exts"] as? [String]) ?? []))
                    }
                }
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
                self.setFileTypes(types)
                self.setAskAvailable(canAsk)
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

        // On by default, and on again at every launch. Hidden until the selected
        // folder is known to have a knowledge base (see updateNotesToggle).
        notesToggle = NSButton(checkboxWithTitle: "Notes", target: self,
                               action: #selector(filtersChanged))
        notesToggle.frame = NSRect(x: w - 110, y: h - 46, width: 66, height: 20)
        notesToggle.controlSize = .small
        notesToggle.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        notesToggle.state = .on
        notesToggle.toolTip = "Also search the Obsidian knowledge base"
        notesToggle.refusesFirstResponder = true  // typing stays in the search field
        notesToggle.autoresizingMask = [.minXMargin, .minYMargin]
        notesToggle.isHidden = true
        blur.addSubview(notesToggle)

        // Turns the panel from searching to asking, and back (Tab does the same from the
        // keyboard). Hidden until the server says it has dsh to answer with.
        askButton = NSButton(title: "Ask", target: self, action: #selector(toggleAskMode))
        askButton.isBordered = false  // it draws its own two states (styleAskButton)
        askButton.wantsLayer = true
        askButton.layer?.cornerRadius = 6
        askButton.image = NSImage(systemSymbolName: "sparkles", accessibilityDescription: nil)
        askButton.imagePosition = .imageLeading
        askButton.imageHugsTitle = true
        askButton.toolTip = "Ask a question about your files; a DeepSeek Harness agent answers it (Tab)"
        askButton.refusesFirstResponder = true  // typing stays in the search field
        askButton.autoresizingMask = [.minXMargin, .minYMargin]
        styleAskButton()
        askButton.sizeToFit()
        askButton.setFrameSize(NSSize(width: askButton.frame.width + 16, height: 22))
        askButton.isHidden = true
        blur.addSubview(askButton)

        let sep = NSBox(frame: NSRect(x: 0, y: h - 64, width: w, height: 1))
        sep.boxType = .separator
        sep.autoresizingMask = [.width, .minYMargin]
        blur.addSubview(sep)

        // Filter bar: narrow the list to one kind of file. Starts with "All" alone and
        // gains the server's file types once they are known (setFileTypes).
        typeBar = NSSegmentedControl(labels: ["All"], trackingMode: .selectOne,
                                     target: self, action: #selector(typeChanged))
        typeBar.controlSize = .small
        typeBar.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        typeBar.selectedSegment = 0
        typeBar.refusesFirstResponder = true  // typing stays in the search field
        typeBar.autoresizingMask = [.maxXMargin, .minYMargin]
        typeBar.sizeToFit()
        typeBar.setFrameOrigin(NSPoint(x: 14, y: h - 64 - kFilterBarHeight + 5))
        blur.addSubview(typeBar)

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: w,
                                                height: h - 64 - kFilterBarHeight))
        resultsScroll = scroll
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

        // Ask mode shows this where the results (and the filter bar) were: the agent's
        // steps as it works, then its answer and the files it came from.
        answerScroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: w, height: h - 65))
        answerScroll.hasVerticalScroller = true
        answerScroll.drawsBackground = false
        answerScroll.autoresizingMask = [.width, .height]
        answerView = AnswerTextView(frame: NSRect(x: 0, y: 0, width: w, height: h - 65))
        answerView.typingTarget = field
        answerView.isEditable = false
        answerView.isSelectable = true  // so an answer can be copied
        answerView.drawsBackground = false
        answerView.textContainerInset = NSSize(width: 12, height: 12)
        answerView.isVerticallyResizable = true
        answerView.autoresizingMask = [.width]
        answerView.textContainer?.widthTracksTextView = true
        answerView.linkTextAttributes = [.foregroundColor: NSColor.linkColor,
                                         .underlineStyle: NSUnderlineStyle.single.rawValue,
                                         .cursor: NSCursor.pointingHand]
        answerView.delegate = self
        answerScroll.documentView = answerView
        answerScroll.isHidden = true
        blur.addSubview(answerScroll)

        // Feedback line under the search field: "searching", errors and "no results"
        // (hidden otherwise).
        statusLabel = NSTextField(labelWithString: "")
        statusLabel.frame = NSRect(x: 16, y: h - 100 - kFilterBarHeight, width: w - 32, height: 20)
        statusLabel.font = NSFont.systemFont(ofSize: 13)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.alignment = .center
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.autoresizingMask = [.width, .minYMargin]
        statusLabel.isHidden = true
        blur.addSubview(statusLabel)
        layoutTopBar()
    }

    // What sits right of the search field: Notes, Ask, then the spinner at the edge.
    // Ask takes up room only once it is on offer, and the field gets what is left.
    func layoutTopBar() {
        guard let content = panel.contentView else { return }
        let w = content.bounds.width, h = content.bounds.height
        var x = w - 44  // the spinner keeps the corner
        if askAvailable {
            x -= askButton.frame.width
            askButton.setFrameOrigin(NSPoint(x: x, y: h - 36 - askButton.frame.height / 2))
            x -= 8
        }
        x -= 66
        notesToggle.frame = NSRect(x: x, y: h - 46, width: 66, height: 20)
        field.frame = NSRect(x: 16, y: h - 56, width: x - 26, height: 40)
    }

    func showStatus(_ message: String?) {
        statusLabel.stringValue = message ?? ""
        statusLabel.isHidden = (message == nil)
    }

    func setFileTypes(_ types: [(id: String, label: String, exts: [String])]) {
        guard types.map({ $0.id }) != fileTypes.map({ $0.id }) else { return }
        fileTypes = types
        tagCache.removeAll()  // a tag takes its colour from the type its extension is in
        table.reloadData()
        if !types.contains(where: { $0.id == selectedType }) { selectedType = nil }
        typeBar.segmentCount = types.count + 1
        typeBar.setLabel("All", forSegment: 0)
        for (i, t) in types.enumerated() { typeBar.setLabel(t.label, forSegment: i + 1) }
        typeBar.selectedSegment = (types.firstIndex(where: { $0.id == selectedType }) ?? -1) + 1
        typeBar.sizeToFit()
    }

    @objc func typeChanged() {
        let i = typeBar.selectedSegment
        selectedType = (i >= 1 && i <= fileTypes.count) ? fileTypes[i - 1].id : nil
        filtersChanged()
    }

    // Finder's icon for a file type: what a row shows until its thumbnail is ready, and
    // all it shows for a result with no file to preview (a note from the knowledge base).
    func icon(forExtension ext: String) -> NSImage {
        if let cached = iconCache[ext] { return cached }
        let image = rowImage(NSWorkspace.shared.icon(for: UTType(filenameExtension: ext) ?? .data),
                             scale: panel.backingScaleFactor, framed: false)
        iconCache[ext] = image
        return image
    }

    func tag(forExtension ext: String) -> NSImage {
        if let cached = tagCache[ext] { return cached }
        let kind = fileTypes.first(where: { $0.exts.contains(ext) })?.id ?? ""
        let image = typeTag(ext: ext, color: kTypeColors[kind] ?? .systemGray)
        tagCache[ext] = image
        return image
    }

    // A row leads with the file's own content as Quick Look renders it: the first page,
    // the picture, the first slide. Generating one takes a moment, so it arrives after
    // the row is on screen (the type icon stands in), and is kept for the next search.
    func loadThumbnail(for result: SearchResult, into view: NSImageView) {
        guard let path = result.absPath else { return }
        if let ready = thumbnails.object(forKey: path as NSString) {
            view.image = ready
            return
        }
        let scale = panel.backingScaleFactor
        let request = QLThumbnailGenerator.Request(
            fileAt: URL(fileURLWithPath: path), size: CGSize(width: kIconSide, height: kIconSide),
            scale: scale, representationTypes: .thumbnail)
        QLThumbnailGenerator.shared.generateBestRepresentation(for: request) {
            [weak self, weak view] representation, _ in
            guard let image = representation?.nsImage
                    ?? imageThumbnail(path, pixels: kIconSide * scale)
            else { return }  // nothing to preview (or the file is gone): the icon stays
            DispatchQueue.main.async {
                let shown = rowImage(image, scale: scale, framed: true)
                self?.thumbnails.setObject(shown, forKey: path as NSString)
                view?.image = shown
            }
        }
    }

    func setLoading(_ on: Bool) {
        loading = on
        updateSpinner()
        if on && results.isEmpty && !askMode { showStatus("Searching…") }
    }

    // One spinner for both modes: a search on its way, or a question with the agent.
    func updateSpinner() {
        if askMode ? asking : loading {
            spinner.startAnimation(nil)
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
        styleAskButton()  // its colours are fixed when drawn; the appearance may have changed
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
        if askMode { return }  // a question is sent with ↵, not as it is typed
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
                  notes: notesToggle.state == .on, type: selectedType) { [weak self] res, err in
            guard let self, mySeq == self.seq else { return }
            self.results = res
            self.table.reloadData()
            self.setLoading(false)
            if let err {
                self.showStatus(err)
                self.ensureBackend()  // restart the bundled server if it died
            } else if res.isEmpty {
                let only = self.fileTypes.first(where: { $0.id == self.selectedType })
                    .map { " (\($0.label) only)" } ?? ""
                self.showStatus(self.indexes.contains(where: { $0.indexed })
                    ? "No results in \(self.currentFolderName())\(only)"
                    : "No indexes yet — choose “Index a Folder…” from the menu-bar icon")
            } else {
                self.showStatus(nil)
                self.table.selectRowIndexes([0], byExtendingSelection: false)
            }
        }
    }

    // Notes ticked or a file type picked: the list on screen was fetched with the old
    // setting, so search again right away.
    @objc func filtersChanged() {
        if askMode { return }  // Notes then applies to the next question, sent with ↵
        guard !query().isEmpty else { return }
        seq += 1
        setLoading(true)
        runSearch()
    }

    // ↵ opens the selected result, but only once the list belongs to the text in the
    // field. Before that it means "search now": it skips the rest of the typing pause,
    // and never opens a hit left over from an earlier, shorter query.
    func submit() {
        if askMode {
            // ⇧↵ continues the conversation on screen; plain ↵ starts a new one.
            ask(followUp: NSApp.currentEvent?.modifierFlags.contains(.shift) ?? false)
        } else if debounce != nil {
            runSearch()
        } else if !loading {
            openSelected()
        }
    }

    // MARK: ask
    func setAskAvailable(_ available: Bool) {
        guard available != askAvailable else { return }
        askAvailable = available
        askButton.isHidden = !available
        layoutTopBar()
        if !available && askMode { setAskMode(false) }
    }

    @objc func toggleAskMode() { setAskMode(!askMode) }

    // Filled with the accent colour while the panel is asking, a faint outline of a
    // button while it is searching: the mode has to read at a glance.
    func styleAskButton() {
        panel.effectiveAppearance.performAsCurrentDrawingAppearance {
            let ink: NSColor = askMode ? .white : .secondaryLabelColor
            let fill: NSColor = askMode ? .controlAccentColor : .labelColor.withAlphaComponent(0.1)
            askButton.layer?.backgroundColor = fill.cgColor
            askButton.contentTintColor = ink
            askButton.attributedTitle = NSAttributedString(string: "Ask", attributes: [
                .font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize, weight: .medium),
                .foregroundColor: ink,
            ])
        }
    }

    func setAskMode(_ on: Bool) {
        askMode = on && askAvailable
        styleAskButton()
        resultsScroll.isHidden = askMode
        typeBar.isHidden = askMode
        answerScroll.isHidden = !askMode
        updatePlaceholder()
        updateNotesToggle()
        debounce?.cancel()
        debounce = nil
        seq += 1  // a search still on its way belongs to the mode just left
        if askMode {
            loading = false
            showStatus(nil)
            if askShown.isEmpty { showAnswer(nil) }
            updateSpinner()
        } else if query().isEmpty {
            results = []; table.reloadData(); setLoading(false); showStatus(nil)
        } else {
            setLoading(true)  // the list on screen predates the text in the field
            runSearch()
        }
    }

    func ask(followUp: Bool) {
        let question = query()
        guard !question.isEmpty else { return }
        askSeq += 1
        let mine = askSeq
        asking = true
        updateSpinner()
        showAnswer(AskState(status: "running", steps: [], answer: "", sources: [],
                            session: nil, error: ""))
        apiAsk(question, dir: selectedDir, allFolders: searchingAll,
               notes: notesToggle.state == .on && !notesToggle.isHidden,
               session: followUp ? askSession : nil) { [weak self] id, problem in
            guard let self, mine == self.askSeq else { return }
            guard let id else {
                self.askFailed(problem ?? "The question could not be sent")
                self.ensureBackend()  // restart the bundled server if it died
                return
            }
            self.pollAsk(id, mine)
        }
    }

    func pollAsk(_ id: Int, _ mine: Int) {
        apiAskState(id) { [weak self] state in
            guard let self, mine == self.askSeq else { return }
            guard let state else { self.askFailed("Lost contact with InfoGrep"); return }
            self.showAnswer(state)
            if state.status == "running" {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                    guard let self, mine == self.askSeq else { return }
                    self.pollAsk(id, mine)
                }
            } else {
                self.asking = false
                self.updateSpinner()
                if state.status == "done" { self.askSession = state.session }
            }
        }
    }

    func askFailed(_ message: String) {
        asking = false
        updateSpinner()
        showAnswer(AskState(status: "error", steps: [], answer: "", sources: [],
                            session: nil, error: message))
    }

    /// Draw the answer area: how to ask (nil), the agent's steps so far, or its answer.
    func showAnswer(_ state: AskState?) {
        let note: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.secondaryLabelColor,
        ]
        let out = NSMutableAttributedString()
        if let state {
            // Polling repeats itself; redrawing would drop a selection and the scroll position.
            let drawnFrom = "\(askSeq)|\(state.status)|\(state.steps.count)|\(state.answer.count)"
            if drawnFrom == askShown { return }
            askShown = drawnFrom
            let steps = NSAttributedString(
                string: state.steps.map { "› \($0)\n" }.joined(), attributes: note)
            switch state.status {
            case "running":
                out.append(steps)
                out.append(NSAttributedString(string: "› Thinking…\n", attributes: note))
            case "done":  // the answer leads; how the agent got there follows it
                out.append(answerText(state.answer, size: 14))
                if !state.sources.isEmpty {
                    out.append(NSAttributedString(string: "\n\nSources\n", attributes: note))
                    for path in state.sources {  // a click reveals the file in Finder
                        out.append(NSAttributedString(
                            string: (path as NSString).lastPathComponent + "\n",
                            attributes: [.font: NSFont.systemFont(ofSize: 13, weight: .medium),
                                         .link: URL(fileURLWithPath: path), .toolTip: path]))
                    }
                }
                out.append(NSAttributedString(
                    string: "\n⇧↵ follow-up   ·   ↵ new question   ·   Tab back to search\n\n",
                    attributes: [.font: NSFont.systemFont(ofSize: 11),
                                 .foregroundColor: NSColor.secondaryLabelColor]))
                out.append(steps)
            default:
                let problem = state.error.isEmpty ? "The question was not answered" : state.error
                out.append(steps)
                out.append(NSAttributedString(
                    string: (state.steps.isEmpty ? "" : "\n") + problem,
                    attributes: [.font: NSFont.systemFont(ofSize: 13),
                                 .foregroundColor: NSColor.systemRed]))
            }
        } else {
            askShown = ""
            out.append(NSAttributedString(
                string: "Type a question and press ↵. An agent searches and reads your files, "
                    + "then answers here.\nWhat it reads is sent to the model dsh uses.\n\n"
                    + "Tab goes back to search.",
                attributes: [.font: NSFont.systemFont(ofSize: 13),
                             .foregroundColor: NSColor.secondaryLabelColor]))
        }
        answerView.textStorage?.setAttributedString(out)
        answerView.scroll(.zero)
    }

    // A source under an answer: show the file in Finder, as ↵ does for a search result.
    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        guard let url = link as? URL, url.isFileURL else { return false }
        NSWorkspace.shared.activateFileViewerSelecting([url])
        hidePanel()
        return true
    }

    // Intercept arrows / return / escape from the search field.
    func control(_ control: NSControl, textView: NSTextView, doCommandBy sel: Selector) -> Bool {
        switch sel {
        case #selector(NSResponder.moveDown(_:)): move(1); return true
        case #selector(NSResponder.moveUp(_:)): move(-1); return true
        case #selector(NSResponder.insertNewline(_:)): submit(); return true
        case #selector(NSResponder.cancelOperation(_:)): hidePanel(); return true
        case #selector(NSResponder.insertTab(_:)), #selector(NSResponder.insertBacktab(_:)):
            guard askAvailable else { return false }
            toggleAskMode()
            return true
        default: return false
        }
    }

    func move(_ delta: Int) {
        if askMode {  // ↑/↓ scroll the answer
            let clip = answerScroll.contentView
            let lowest = max(0, answerView.frame.height - clip.bounds.height)
            let y = min(lowest, max(0, clip.bounds.origin.y + CGFloat(delta) * 60))
            clip.scroll(to: NSPoint(x: 0, y: y))
            answerScroll.reflectScrolledClipView(clip)
            return
        }
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
        let ext = (r.ext ?? (r.path as NSString).pathExtension).lowercased()
        let iconView = NSImageView(image: icon(forExtension: ext))
        iconView.imageScaling = .scaleProportionallyUpOrDown
        iconView.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(iconView)
        loadThumbnail(for: r, into: iconView)
        let tagView = NSImageView(image: ext.isEmpty ? NSImage() : tag(forExtension: ext))
        tagView.translatesAutoresizingMaskIntoConstraints = false
        tagView.setContentCompressionResistancePriority(.required, for: .horizontal)
        tagView.setContentHuggingPriority(.required, for: .horizontal)
        v.addSubview(tagView)
        let name = NSTextField(labelWithString: r.filename + (r.page != nil ? "  · p.\(r.page!)" : ""))
        name.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        name.textColor = .labelColor
        name.lineBreakMode = .byTruncatingMiddle  // gives way to the tag, keeps the extension
        name.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
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
            iconView.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 14),
            iconView.centerYAnchor.constraint(equalTo: v.centerYAnchor),
            iconView.widthAnchor.constraint(equalToConstant: kIconSide),
            iconView.heightAnchor.constraint(equalToConstant: kIconSide),
            tagView.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -16),
            tagView.centerYAnchor.constraint(equalTo: name.centerYAnchor),
            name.leadingAnchor.constraint(equalTo: iconView.trailingAnchor, constant: 10),
            name.trailingAnchor.constraint(lessThanOrEqualTo: tagView.leadingAnchor, constant: -8),
            name.topAnchor.constraint(equalTo: v.topAnchor, constant: 7),
            pathLabel.leadingAnchor.constraint(equalTo: name.leadingAnchor),
            pathLabel.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -16),
            pathLabel.topAnchor.constraint(equalTo: name.bottomAnchor, constant: 1),
            sub.leadingAnchor.constraint(equalTo: name.leadingAnchor),
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
