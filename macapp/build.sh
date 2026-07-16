#!/bin/zsh
# Build the mybot menu-bar app and wrap the SPM executable into a proper
# .app bundle (LSUIElement agent, no Dock icon). Only Command Line Tools needed.
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="mybot"
BUNDLE_ID="com.example.mybot.menu"
CONFIG="${1:-release}"

# Stamp the bundle with the source commit so the app (and deploy.sh) can tell
# whether an installed copy is behind the repo.
GIT_SHA="$(git -C .. rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [[ -n "$(git -C .. status --porcelain 2>/dev/null)" ]]; then
    GIT_SHA="${GIT_SHA}-dirty"
fi
BUILD_DATE="$(date '+%Y-%m-%d %H:%M')"

echo "==> swift build -c $CONFIG"
swift build -c "$CONFIG"

BIN="$(swift build -c "$CONFIG" --show-bin-path)/MybotMenu"
# Stage the bundle in a dot-directory so Spotlight/Launchpad only ever see the
# installed copy (scripts/deploy.sh puts it in ~/Applications).
APP=".dist/${APP_NAME}.app"
MACOS="$APP/Contents/MacOS"

echo "==> bundling $APP"
rm -rf "$APP"
mkdir -p "$MACOS" "$APP/Contents/Resources"
cp "$BIN" "$MACOS/$APP_NAME"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key><string>mybot</string>
    <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
    <key>CFBundleExecutable</key><string>${APP_NAME}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>MybotGitSHA</key><string>${GIT_SHA}</string>
    <key>MybotBuildDate</key><string>${BUILD_DATE}</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSUIElement</key><true/>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>NSHumanReadableCopyright</key><string>mybot</string>
</dict>
</plist>
PLIST

# App/Finder icon: render the bot tile, then build an .icns from it.
echo "==> generating app icon"
"$BIN" --render-icons >/dev/null 2>&1 || true
ICON_SRC="/tmp/mybot_icons/appicon.png"
if [[ -f "$ICON_SRC" ]]; then
    ICONSET="$(mktemp -d)/AppIcon.iconset"
    mkdir -p "$ICONSET"
    for sz in 16 32 128 256 512; do
        sips -z $sz $sz "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null 2>&1
        sips -z $((sz * 2)) $((sz * 2)) "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns" \
        && echo "  wrote Resources/AppIcon.icns" || echo "  (icns generation skipped)"
else
    echo "  (icon render unavailable in this environment; skipping)"
fi

# Ad-hoc sign so macOS will run it and notifications get a stable identity.
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "(codesign skipped)"

echo "==> built $APP (commit $GIT_SHA)"
echo "Launch:  open $APP   (or ./.dist/${APP_NAME}.app/Contents/MacOS/${APP_NAME} for logs)"
