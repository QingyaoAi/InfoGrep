#!/bin/bash
# Build InfoGrep.app — a menu-bar Spotlight-style launcher for the InfoGrep web API.
set -euo pipefail
cd "$(dirname "$0")"

APP="InfoGrep.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

echo "compiling…"
swiftc -O -o "$APP/Contents/MacOS/InfoGrep" main.swift \
    -framework AppKit -framework Carbon

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>InfoGrep</string>
  <key>CFBundleDisplayName</key><string>InfoGrep</string>
  <key>CFBundleIdentifier</key><string>com.infogrep.launcher</string>
  <key>CFBundleExecutable</key><string>InfoGrep</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Ad-hoc sign so macOS will run it.
codesign --force --sign - "$APP" >/dev/null 2>&1 || true

echo "built $(pwd)/$APP"
echo "run it:   open $APP        (then press ⌥-Space)"
