#!/bin/bash
# 重新编译 Jarvis.app（菜单栏应用）
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
APP="$ROOT/Jarvis.app"
BUNDLE="$APP/Contents"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
MIN_MACOS="13.0"

if [ ! -f "build/AppIcon.icns" ]; then
  echo "缺少 build/AppIcon.icns"
  echo "请先运行：python3 build/make_icon.py（需要 Pillow）"
  exit 1
fi

rm -rf "$APP"
mkdir -p "$BUNDLE/MacOS" "$BUNDLE/Resources"

cp "build/Info.plist" "$BUNDLE/Info.plist"
/usr/libexec/PlistBuddy -c "Set :JarvisProjectRoot $ROOT" "$BUNDLE/Info.plist" >/dev/null
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$BUNDLE/Info.plist" >/dev/null
/usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion $MIN_MACOS" "$BUNDLE/Info.plist" >/dev/null
cp "build/AppIcon.icns" "$BUNDLE/Resources/AppIcon.icns"
printf 'APPL????' > "$BUNDLE/PkgInfo"

echo "编译 Swift 菜单栏程序 ..."
ARCH="$(uname -m)"
mkdir -p "build/.modulecache"
xcrun swiftc \
  -O \
  -module-cache-path "$ROOT/build/.modulecache" \
  -swift-version 5 \
  -target "${ARCH}-apple-macos${MIN_MACOS}" \
  -framework Cocoa \
  "build/main.swift" \
  -o "$BUNDLE/MacOS/JarvisMenu"

chmod +x "$BUNDLE/MacOS/JarvisMenu"

if codesign --force --sign - "$APP" >/dev/null 2>&1; then
  echo "已做本地签名（ad-hoc）"
else
  echo "警告：ad-hoc 签名失败，App 仍可运行"
fi

touch "$APP"
rm -rf "build/.modulecache"
echo "✓ 已生成：$APP"
