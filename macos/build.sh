#!/bin/bash
# Build InfoGrep.app — a menu-bar Spotlight-style launcher for the InfoGrep web API.
#
#   ./build.sh               thin app: UI only, expects `infogrep serve` to be running
#                            (install.sh sets that up from the repo's venv)
#   ./build.sh --standalone  self-contained app: bundles a Python runtime, the infogrep
#                            backend, and a trimmed Java runtime, and starts the server
#                            itself — download, open, done. Needs `uv` and a JDK 21+
#                            (for jlink) on the *build* machine only.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"

STANDALONE=0
[ "${1:-}" = "--standalone" ] && STANDALONE=1

APP="InfoGrep.app"
RES="$APP/Contents/Resources"
PYVER="3.12"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES"

echo "compiling…"
swiftc -O -o "$APP/Contents/MacOS/InfoGrep" main.swift \
    -framework AppKit -framework Carbon

cp assets/AppIcon.icns "$RES/AppIcon.icns"

if [ "$STANDALONE" = 1 ]; then
  command -v uv >/dev/null 2>&1 || { echo "error: --standalone needs uv (https://astral.sh/uv)"; exit 1; }

  # 1) Relocatable CPython (python-build-standalone via uv's managed pythons).
  echo "bundling Python $PYVER…"
  uv python install "$PYVER" --quiet
  # Resolve the *managed* interpreter, never a dev venv (which would drag its whole
  # site-packages, e.g. torch, into the bundle) — hence no VIRTUAL_ENV / project.
  # (run from / so uv can't discover the repo's .venv by walking up from cwd)
  PYBIN="$(cd / && env -u VIRTUAL_ENV uv python find --managed-python --no-project "$PYVER")"
  case "$PYBIN" in
    */.venv/*) echo "error: refusing to bundle a virtualenv python ($PYBIN)"; exit 1 ;;
  esac
  PYHOME="$(cd "$(dirname "$PYBIN")/.." && pwd)"
  ditto "$PYHOME" "$RES/python"
  # Trim pieces a headless backend never imports.
  for d in test idlelib tkinter turtledemo; do
    rm -rf "$RES/python/lib/python$PYVER/$d"
  done

  # 2) The infogrep backend + dependencies (base extras only: sparse/kb/graph search.
  #    Dense search pulls in torch — far too big to ship; it stays a pip extra).
  echo "bundling the infogrep backend…"
  ( cd "$ROOT" && rm -rf dist && uv build --wheel --quiet )
  uv pip install --quiet --python "$RES/python/bin/python3" \
      --target "$RES/backend" "$ROOT"/dist/infogrep-*.whl

  # 3) Trimmed Java runtime for the sparse (Lucene/BM25) backend.
  echo "bundling a Java runtime (jlink)…"
  JDK="${JAVA_HOME_21:-${JAVA_HOME:-}}"
  if [ -z "$JDK" ] || [ ! -x "$JDK/bin/jlink" ]; then
    JDK="$(brew --prefix openjdk@21 2>/dev/null || true)/libexec/openjdk.jdk/Contents/Home"
  fi
  if [ ! -x "$JDK/bin/jlink" ]; then
    JDK="$(/usr/libexec/java_home -v 21 2>/dev/null || true)"
  fi
  [ -x "${JDK:-}/bin/jlink" ] || { echo "error: --standalone needs a JDK 21+ (jlink). Set JAVA_HOME_21."; exit 1; }
  "$JDK/bin/jlink" \
      --add-modules java.se,jdk.unsupported,jdk.incubator.vector,jdk.zipfs,jdk.crypto.ec \
      --strip-debug --no-header-files --no-man-pages --compress=zip-6 \
      --output "$RES/jre"
fi

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>InfoGrep</string>
  <key>CFBundleDisplayName</key><string>InfoGrep</string>
  <key>CFBundleIdentifier</key><string>com.infogrep.launcher</string>
  <key>CFBundleExecutable</key><string>InfoGrep</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.0.3</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Ad-hoc sign so macOS will run it (--deep also signs the bundled python/jre binaries).
if [ "$STANDALONE" = 1 ]; then
  codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
  du -sh "$APP" | awk '{print "bundle size: " $1}'
else
  codesign --force --sign - "$APP" >/dev/null 2>&1 || true
fi

echo "built $(pwd)/$APP"
echo "run it:   open $APP        (then press ⌘⇧-Space)"
