#!/bin/sh
# Builds menubar/build/GameON.app (shown as "Pluvia"; the file, executable and bundle id keep the GameON name so running
# watch/LaunchAgent/permissions stay valid) (swiftc only, no Xcode project). Bundles ../gamefocus.py.
set -e
cd "$(dirname "$0")"
APP=build/GameON.app
rm -rf "$APP" && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -swift-version 5 main.swift launcher.swift -o "$APP/Contents/MacOS/GameON"
cp ../gamefocus.py ../steamfix.py "$APP/Contents/Resources/"
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.gameon.menubar</string>
<key>CFBundleName</key><string>Pluvia</string>
<key>CFBundleDisplayName</key><string>Pluvia</string>
<key>CFBundleExecutable</key><string>GameON</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1</string>
<key>LSUIElement</key><true/>
<key>NSAppleEventsUsageDescription</key><string>Pluvia asks apps to quit to free RAM while you play.</string>
</dict></plist>
PL
codesign --force --sign - "$APP" 2>/dev/null || true
echo "built $APP"
