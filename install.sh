#!/usr/bin/env bash
# InfoGrep installer — clone the repo, then run ./install.sh
#
# Sets up the Python backend (uv) and the Claude Code MCP server on any platform.
# On macOS, also builds the menu-bar app (⌘⇧Space launcher) and login agents (web
# server + app). On Linux, the backend and CLI (`infogrep serve`, `infogrep mcp`, …)
# are fully supported; there's no menu-bar app and no auto-start service — run
# `infogrep serve` yourself, or wire up your own systemd unit / cron job.
# Re-runnable. Remove everything with ./uninstall.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"
PORT="${INFOGREP_PORT:-7421}"
SERVE_DIR="${INFOGREP_SERVE_DIR:-$HOME}"   # default folder the web server is bound to
LA="$HOME/Library/LaunchAgents"
VENV_BIN="$REPO/.venv/bin"
INFOGREP="$VENV_BIN/infogrep"

say() { printf "\033[1;34m▸\033[0m %s\n" "$*"; }

# 1) Python dependencies ------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it first:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi
say "Installing Python dependencies (uv sync)…"
uv sync --extra dense   # dense (embedding) search included; base deps alone are much smaller

# 2) Java check (sparse/BM25 needs JDK 21; auto-detected at runtime) -----------
if ! uv run python -c "from infogrep.jvm import ensure_jdk; ensure_jdk()" >/dev/null 2>&1; then
  echo "  ⚠ JDK 21 not found — sparse search needs it."
  if [ "$(uname)" = "Darwin" ]; then
    echo "    macOS:          brew install openjdk@21"
  else
    echo "    Debian/Ubuntu:  sudo apt install openjdk-21-jdk"
    echo "    Fedora/RHEL:    sudo dnf install java-21-openjdk"
    echo "    Arch:           sudo pacman -S jdk-openjdk"
  fi
fi

if [ "$(uname)" = "Darwin" ]; then
  mkdir -p "$LA" "$HOME/.infogrep"

  # 3) Build + install the menu-bar app -------------------------------------
  APP_BIN=""
  if command -v swiftc >/dev/null 2>&1; then
    say "Building the menu-bar app…"
    ( cd macos && ./build.sh >/dev/null )
    rm -rf /Applications/InfoGrep.app
    cp -R macos/InfoGrep.app /Applications/InfoGrep.app
    codesign --force --sign - /Applications/InfoGrep.app >/dev/null 2>&1 || true
    APP_BIN="/Applications/InfoGrep.app/Contents/MacOS/InfoGrep"
    say "Installed /Applications/InfoGrep.app"
  else
    echo "  (skipping the app: 'swiftc' not found — run 'xcode-select --install')"
  fi

  # 4) Login agents ---------------------------------------------------------
  say "Setting up login agents…"
  cat > "$LA/com.infogrep.webui.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.infogrep.webui</string>
  <key>ProgramArguments</key><array>
    <string>$INFOGREP</string><string>serve</string>
    <string>--dir</string><string>$SERVE_DIR</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$VENV_BIN:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.infogrep/webui.log</string>
  <key>StandardErrorPath</key><string>$HOME/.infogrep/webui.log</string>
</dict></plist>
EOF
  launchctl unload "$LA/com.infogrep.webui.plist" 2>/dev/null || true
  launchctl load "$LA/com.infogrep.webui.plist"

  if [ -n "$APP_BIN" ]; then
    cat > "$LA/com.infogrep.launcher.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.infogrep.launcher</string>
  <key>ProgramArguments</key><array><string>$APP_BIN</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>$HOME/.infogrep/launcher.log</string>
  <key>StandardErrorPath</key><string>$HOME/.infogrep/launcher.log</string>
</dict></plist>
EOF
    launchctl unload "$LA/com.infogrep.launcher.plist" 2>/dev/null || true
    launchctl load "$LA/com.infogrep.launcher.plist"
  fi
fi

# 5) Claude Code MCP (optional) ----------------------------------------------
if command -v claude >/dev/null 2>&1; then
  say "Registering InfoGrep with Claude Code (MCP)…"
  claude mcp remove infogrep -s user >/dev/null 2>&1 || true
  claude mcp add infogrep --scope user -- "$INFOGREP" mcp --dir "$SERVE_DIR" >/dev/null 2>&1 || true
fi

if [ "$(uname)" = "Darwin" ]; then
  cat <<EOF

✅ InfoGrep installed.
   • macOS app + ⌘⇧Space launcher  (menu-bar 🔎)
   • Web UI:  http://127.0.0.1:$PORT
   • Claude Code:  the 'infogrep' search tools are available in new sessions

Get started: index a folder from the app ("Index a Folder…") or the web UI ("＋ folder").
Uninstall:   ./uninstall.sh            (add --purge to also delete the indexes)
EOF
else
  cat <<EOF

✅ InfoGrep installed.
   • CLI:          uv run infogrep --help
   • Web UI:       uv run infogrep serve --dir <folder> --port $PORT
   • Claude Code:  the 'infogrep' search tools are available in new sessions

Get started:  uv run infogrep index <folder>   then   uv run infogrep serve --dir <folder>
There's no menu-bar app or auto-start service on Linux; run 'infogrep serve' yourself, or
wire up your own systemd --user unit / cron job to keep it running or reindex daily.
Uninstall:   ./uninstall.sh            (add --purge to also delete the indexes)
EOF
fi
