# InfoGrep launcher (macOS)

A tiny menu-bar app that gives InfoGrep a **Spotlight-style search bar**: press a global
hotkey, type, and open the matching file.

- **Hotkey:** ⌘⇧-Space (Command+Shift+Space) toggles the search panel
- **↑ / ↓** navigate · **↵** reveal the selected file in Finder · **Esc** dismiss
- Results update as you type; a spinner next to the query shows while a search is running.
  Pressing **↵** before the results for what you typed have arrived searches right away
  (it never opens a result left over from an earlier query).
- **Notes** checkbox (shown for folders with an Obsidian knowledge base configured): also
  search the vault. Off by default — that part answers through the Obsidian app and is
  much slower than the local index. It applies to questions too (see **Ask** below): the
  agent leaves the vault alone unless the box is ticked when you send the question.
- **File-type bar** above the results (All · PDF · Word · Slides · Sheets · Text · Images ·
  JSON): show only that kind of file — the best matches of that type, not a filtered-down
  top 25. Each result leads with a Quick Look thumbnail of the file itself (first page,
  picture, first slide) and carries a small tag naming its type, coloured by kind.
- **Ask** button next to the search field, or **Tab**: switch the panel from searching to
  asking, and back. In Ask mode **↵** sends what you typed as a question to a
  [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) agent, which searches
  and reads your files and answers in the panel, with the files it used listed underneath
  (click one to reveal it in Finder). **⇧↵** asks a follow-up in the same conversation,
  **↑ / ↓** scroll the answer. The question covers the folders the search would. You can
  dismiss the panel while it works; the answer is there when you bring it back. The button
  only appears when `dsh` is installed — the top-level README has the steps
  ([Set up question answering](../README.md#set-up-question-answering-deepseek-harness)),
  what to do when the button is missing, and what a question runs and leaves on disk.
- Menu-bar 🔎 icon → Search… / Quit

## Download & run (standalone app)

Each GitHub release ships `InfoGrep.dmg` (open it and drag InfoGrep to Applications) and
`InfoGrep.app.zip` — the same bundle, take whichever you prefer. Both are **self-contained**
(Apple Silicon): they bundle a Python runtime, the InfoGrep backend, and a Java runtime, and
start their own local server when nothing is listening on port 7421 (the server stops when
the app quits).

First launch needs one Gatekeeper approval either way, because the app is ad-hoc signed
rather than notarized — the DMG does not change that:

```bash
xattr -dr com.apple.quarantine /Applications/InfoGrep.app   # clears the download flag
open /Applications/InfoGrep.app                             # 🔎 appears; press ⌘⇧-Space
```

Without a terminal: open it once, let macOS refuse, then **System Settings → Privacy &
Security → Open Anyway**. (Control-click → Open used to do this in one step; macOS 15
removed that shortcut for apps that aren't notarized, so on macOS 15 and later it just
shows the same refusal.)

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
folder ⌘⇧Space searches (the choice is remembered). With more than one index, **All Folders**
(the default) searches them together, ranked as a single collection. **Index a Folder…** opens a folder
picker and indexes it in the background (shown as `(indexing…)` until ready). The browser
UI has the same controls (a folder dropdown + **＋ folder**).

## Auto-start at login

Use the top-level **`./install.sh`** (it installs the app to `/Applications` and loads a
`com.infogrep.launcher` LaunchAgent with `RunAtLoad=true`, `KeepAlive=false` — starts at
login, stays quit if you Quit it from the menu). **`./uninstall.sh`** removes it. It won't
appear in System Settings → Login Items because it's an unsigned, hand-loaded LaunchAgent,
but it runs.
