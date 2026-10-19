#!/bin/sh
# Builds menubar/build/GameON.app (swiftc only, no Xcode project). Bundles ../gamefocus.py.
set -e
cd "$(dirname "$0")"
APP=build/GameON.app
rm -rf "$APP" && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -swift-version 5 main.swift -o "$APP/Contents/MacOS/GameON"
cp ../gamefocus.py "$APP/Contents/Resources/gamefocus.py"
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.gameon.menubar</string>
<key>CFBundleName</key><string>GameON</string>
<key>CFBundleExecutable</key><string>GameON</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1</string>
<key>LSUIElement</key><true/>
<key>NSAppleEventsUsageDescription</key><string>GameON asks apps to quit to free RAM while you play.</string>
</dict></plist>
PL
codesign --force --sign - "$APP" 2>/dev/null || true
echo "built $APP"
