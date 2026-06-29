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

## Pick / index folders

The menu-bar 🔎 lists every InfoGrep index under **Search in:** — click one to switch which
folder ⌘⇧Space searches (the choice is remembered). **Index a Folder…** opens a folder
picker and indexes it in the background (shown as `(indexing…)` until ready). The browser
UI has the same controls (a folder dropdown + **＋ folder**).

## Auto-start at login

Copy the app somewhere stable and load a LaunchAgent that runs it at login:

```bash
cp -R InfoGrep.app ~/Applications/
cat > ~/Library/LaunchAgents/com.infogrep.launcher.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.infogrep.launcher</string>
  <key>ProgramArguments</key><array><string>$HOME/Applications/InfoGrep.app/Contents/MacOS/InfoGrep</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/com.infogrep.launcher.plist
```

`RunAtLoad` starts it on login; `KeepAlive=false` means it stays quit if you Quit it from
the menu. (It doesn't appear in System Settings → Login Items because it's an unsigned,
hand-loaded LaunchAgent, but it runs.) Remove with
`launchctl unload …/com.infogrep.launcher.plist && rm …/com.infogrep.launcher.plist`.
