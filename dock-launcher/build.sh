#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="H2D Dashboard"
BUNDLE_ID="local.h2d-dashboard"
VERSION="1.0"
BUILD_DIR="${SCRIPT_DIR}/build"
TEMP_APP="${BUILD_DIR}/${APP_NAME}.app"
TARGET_APP="/Applications/${APP_NAME}.app"
ICNS_SRC="${SCRIPT_DIR}/../assets/H2DDashboard.icns"

echo "==> Building ${APP_NAME}..."

mkdir -p "${BUILD_DIR}"
touch "${BUILD_DIR}/.metadata_never_index"
rm -rf "${TEMP_APP}"

# Compile AppleScript to application bundle
osacompile -o "${TEMP_APP}" "${SCRIPT_DIR}/launcher.applescript"

# Remove default AppleScript asset catalog so macOS uses our custom .icns
rm -f "${TEMP_APP}/Contents/Resources/Assets.car"

# Copy custom application icon
if [ -f "${ICNS_SRC}" ]; then
    cp "${ICNS_SRC}" "${TEMP_APP}/Contents/Resources/applet.icns"
    cp "${ICNS_SRC}" "${TEMP_APP}/Contents/Resources/H2DDashboard.icns"
fi

# Configure Info.plist
PLIST="${TEMP_APP}/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier ${BUNDLE_ID}" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string ${BUNDLE_ID}" "${PLIST}"

/usr/libexec/PlistBuddy -c "Set :CFBundleName ${APP_NAME}" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleName string ${APP_NAME}" "${PLIST}"

/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName ${APP_NAME}" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string ${APP_NAME}" "${PLIST}"

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${VERSION}" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string ${VERSION}" "${PLIST}"

/usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${VERSION}" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string ${VERSION}" "${PLIST}"

/usr/libexec/PlistBuddy -c "Set :CFBundleIconFile H2DDashboard" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string H2DDashboard" "${PLIST}"

/usr/libexec/PlistBuddy -c "Set :LSUIElement true" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "${PLIST}"

/usr/libexec/PlistBuddy -c "Set :OSAAppletShowStartupScreen false" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :OSAAppletShowStartupScreen bool false" "${PLIST}"

/usr/libexec/PlistBuddy -c "Set :NSAppleEventsUsageDescription Controls Google Chrome to activate or open the H2D ventilation dashboard." "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :NSAppleEventsUsageDescription string Controls Google Chrome to activate or open the H2D ventilation dashboard." "${PLIST}"

# Sign ad-hoc
codesign --force --deep --sign - "${TEMP_APP}"

# Pre-quit running instances
osascript -e "tell application \"${APP_NAME}\" to quit" 2>/dev/null || killall "${APP_NAME}" 2>/dev/null || true

# Install to /Applications
echo "==> Installing to ${TARGET_APP}..."
rm -rf "${TARGET_APP}"
ditto "${TEMP_APP}" "${TARGET_APP}"
rm -rf "${TEMP_APP}"

# Re-register with LaunchServices
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "${TARGET_APP}"

echo "==> Verifying Spotlight indexing..."
mdfind "kMDItemFSName == '${APP_NAME}.app'"

echo "==> Installed successfully."
