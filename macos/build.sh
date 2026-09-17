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
# Oldest macOS the app supports. Stamped into the binary (-target, below) *and* into
# Info.plist's LSMinimumSystemVersion, so the two can't drift apart.
MACOS_MIN="13.0"
# Version comes from the package, so the app and the backend can't disagree.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/infogrep/__init__.py")"
[ -n "$VERSION" ] || { echo "error: could not read __version__ from infogrep/__init__.py"; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES"

echo "compiling…"
# -target is not optional. Without it swiftc takes the *build machine's* OS as the
# deployment target, so building on a newer macOS silently stamps that version into
# the binary and the app refuses to launch on anything older — while Info.plist still
# advertises 13.0 and the failure only shows up on someone else's Mac. (Building on
# macOS 27 produced minos 27.0: a release nobody could run.) Only the architecture
# follows the build machine, so Intel source builds keep working.
swiftc -O -target "$(uname -m)-apple-macos$MACOS_MIN" \
    -o "$APP/Contents/MacOS/InfoGrep" main.swift \
    -framework AppKit -framework Carbon

# Read the floor back out of the Mach-O rather than trusting the flag: this is the one
# property of the build that depends on the machine it was built on, and getting it
# wrong ships an app that launches here and nowhere else.
BUILT_MIN="$(vtool -show-build-version "$APP/Contents/MacOS/InfoGrep" 2>/dev/null \
    | awk '$1 == "minos" { print $2 }')"
[ "$BUILT_MIN" = "$MACOS_MIN" ] || {
    echo "error: binary targets macOS ${BUILT_MIN:-?}, expected $MACOS_MIN"; exit 1; }

cp assets/AppIcon.icns "$RES/AppIcon.icns"

if [ "$STANDALONE" = 1 ]; then
  command -v uv >/dev/null 2>&1 || { echo "error: --standalone needs uv (https://astral.sh/uv)"; exit 1; }

  # 1) Relocatable CPython (python-build-standalone via uv's managed pythons).
  echo "bundling Python ${PYVER}..."
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

  # Precompile the backend *before* signing. Writing .pyc into a signed bundle breaks
  # its seal, so an app that was validly signed at download would turn "damaged" on
  # first launch. Sealing the bytecode here (plus PYTHONDONTWRITEBYTECODE at runtime,
  # see main.swift) means the bundle is never mutated. A stdlib copy of some vendored
  # module may legitimately fail to compile, so don't treat that as fatal.
  "$RES/python/bin/python3" -m compileall -q -j 0 "$RES/backend" >/dev/null 2>&1 \
      || echo "  (note: some backend modules did not precompile)"

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

  # jlink writes its legal/ files read-only (444). `xattr -dr com.apple.quarantine` —
  # the one command a user runs after downloading — then fails on them with EACCES and
  # exits non-zero, which looks like the de-quarantine didn't work. Nothing here needs
  # to stay read-only, so make the tree user-writable before signing.
  chmod -R u+w "$RES/jre"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
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
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>${MACOS_MIN}</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Ad-hoc sign so macOS will run it (--deep also signs the bundled python/jre binaries).
# Failures are fatal and the result is verified: a bundle that ships with a broken seal
# is rejected as "damaged" on other Macs, which is invisible if signing errors are
# swallowed. Nothing may touch the bundle after this point.
echo "signing…"
if [ "$STANDALONE" = 1 ]; then
  codesign --force --deep --sign - "$APP"
  codesign --verify --deep --strict "$APP"
  du -sh "$APP" | awk '{print "bundle size: " $1}'
else
  codesign --force --sign - "$APP"
  codesign --verify --strict "$APP"
fi

echo "built $(pwd)/$APP"
echo "run it:   open $APP        (then press ⌘⇧-Space)"
