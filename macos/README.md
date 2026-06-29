# InfoGrep launcher (macOS)

A tiny menu-bar app that gives InfoGrep a **Spotlight-style search bar**: press a global
hotkey, type, and open the matching file.

- **Hotkey:** ⌘⇧-Space (Command+Shift+Space) toggles the search panel
- **↑ / ↓** navigate · **↵** reveal the selected file in Finder · **Esc** dismiss
- Menu-bar 🔎 icon → Search… / Quit

It's a thin UI client over the InfoGrep web API, so the backend must be running:

```bash
infogrep serve --dir <indexed-dir>        # default http://127.0.0.1:7421
```

(On this machine the `com.infogrep.webui` launchd agent already serves the Dropbox index,
so the launcher works out of the box.)

## Build & run

```bash
./build.sh            # compiles InfoGrep.app with swiftc (needs the Xcode CLT)
open InfoGrep.app     # launches the menu-bar agent; press ⌘⇧-Space
```

## Customize

- **Backend URL / search mode / result count:** the `kAPIBase`, `kSearchMode`,
  `kMaxResults` constants at the top of `main.swift`.
- **Hotkey:** the `RegisterEventHotKey(... kVK_Space, optionKey ...)` call in
  `registerHotKey()` (e.g. swap `optionKey` for `controlKey`, or `kVK_Space` for another
  key code).

## Auto-start at login

Move `InfoGrep.app` to `/Applications` and add it under System Settings → General →
Login Items, or run it from a LaunchAgent. (A `--install` helper can be added later.)
