#!/usr/bin/env bash
# InfoGrep uninstaller — removes the app, login agents and MCP registration.
# Indexes (~/.infogrep) are kept unless you pass --purge.
#
#   ./uninstall.sh            # remove app + agents + MCP, keep indexes
#   ./uninstall.sh --purge    # also delete all indexes
set -uo pipefail

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
LA="$HOME/Library/LaunchAgents"

say() { printf "\033[1;34m▸\033[0m %s\n" "$*"; }

# 1) Stop + remove login agents (incl. per-directory daily reindex agents) ----
for plist in "$LA"/com.infogrep.webui.plist "$LA"/com.infogrep.launcher.plist \
             "$LA"/com.infogrep.reindex.*.plist; do
  if [ -f "$plist" ]; then
    say "Removing login agent $(basename "$plist" .plist)…"
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
  fi
done

# 2) Kill anything still running ---------------------------------------------
pkill -f "infogrep serve" 2>/dev/null || true
pkill -f "InfoGrep.app/Contents/MacOS/InfoGrep" 2>/dev/null || true

# 3) Remove the app ----------------------------------------------------------
for app in /Applications/InfoGrep.app "$HOME/Applications/InfoGrep.app"; do
  if [ -d "$app" ]; then say "Removing $app"; rm -rf "$app"; fi
done

# 4) Unregister the Claude Code MCP server -----------------------------------
if command -v claude >/dev/null 2>&1; then
  say "Unregistering InfoGrep MCP server…"
  claude mcp remove infogrep -s user >/dev/null 2>&1 || true
fi

# 5) Indexes -----------------------------------------------------------------
if [ "$PURGE" = "1" ]; then
  say "Purging indexes (~/.infogrep)…"
  rm -rf "$HOME/.infogrep"
else
  echo "  Indexes kept at ~/.infogrep  (re-run with --purge to delete them)."
fi

cat <<EOF

✅ InfoGrep uninstalled.
The Python env lives in this repo's .venv — delete the cloned folder to remove it entirely.
EOF
