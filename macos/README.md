# InfoGrep launcher (macOS)

A tiny menu-bar app that gives InfoGrep a **Spotlight-style search bar**: press a global
hotkey, type, and open the matching file.

- **Hotkey:** ⌘⇧-Space (Command+Shift+Space) toggles the search panel
- **↑ / ↓** navigate · **↵** reveal the selected file in Finder · **Esc** dismiss
- Menu-bar 🔎 icon → Search… / Quit

## Download & run (standalone app)

The GitHub-release `InfoGrep.app.zip` is **self-contained** (Apple Silicon): it bundles a
Python runtime, the InfoGrep backend, and a Java runtime, and starts its own local server
when nothing is listening on port 7421 (the server stops when the app quits). Unzip, then
first launch needs one Gatekeeper approval because the app is not notarized:

```bash
xattr -dr com.apple.quarantine InfoGrep.app   # or right-click → Open, once
open InfoGrep.app                              # menu-bar 🔎 appears; press ⌘⇧-Space
```

Pick **Index a Folder…** from the 🔎 menu to build your first index. (Dense/semantic
search isn't in the bundle — it needs torch; use the pip install for that. The Anserini
jar for keyword search is downloaded once, ~112 MB, on first indexing.)

## Build & run from source

```bash
./build.sh               # thin app: UI only, needs `infogrep serve` running separately
./build.sh --standalone  # self-contained app (needs uv + a JDK 21 on the build machine)
open InfoGrep.app        # launches the menu-bar agent; press ⌘⇧-Space
```

The thin app is a UI client over the InfoGrep web API, so the backend must be running:

```bash
infogrep serve --dir <indexed-dir>        # default http://127.0.0.1:7421
```

The repo's top-level **`./install.sh`** does all of this for you — builds the thin app,
installs it to `/Applications`, and runs both the backend and the app at login.

## Customize

- **Backend URL / search mode / result count:** the `kAPIBase`, `kSearchMode`,
  `kMaxResults` constants at the top of `main.swift`.
- **Hotkey:** the `RegisterEventHotKey(... kVK_Space, optionKey ...)` call in
  `registerHotKey()` (e.g. swap `optionKey` for `controlKey`, or `kVK_Space` for another
  key code).

## Pick / index folders

The menu-bar 🔎 lists every InfoGrep index under **Search in:** — click one to switch which
folder ⌘⇧Space searches (the choice is remembered). **Index a Folder…** opens a folder
picker and indexes it in the background (shown as `(indexing…)` until ready). The browser
UI has the same controls (a folder dropdown + **＋ folder**).

## Auto-start at login

Use the top-level **`./install.sh`** (it installs the app to `/Applications` and loads a
`com.infogrep.launcher` LaunchAgent with `RunAtLoad=true`, `KeepAlive=false` — starts at
login, stays quit if you Quit it from the menu). **`./uninstall.sh`** removes it. It won't
appear in System Settings → Login Items because it's an unsigned, hand-loaded LaunchAgent,
but it runs.
