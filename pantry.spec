# -*- mode: python ; coding: utf-8 -*-
import os

# Single source of truth for the version number -- bump the VERSION file
# before cutting a new release (see release.py), rather than editing this
# spec directly, so the built app and the published release tag always
# agree on what version they are.
with open("VERSION") as _f:
    _version = _f.read().strip()

# Bundle whatever database currently lives on THIS Mac as "seed_data" inside
# the app. On a brand-new install (no ~/Library/Application Support db yet),
# the app copies this in automatically -- so building & handing over the
# .app carries the current data with it, with no separate export/import
# step. Existing installs are untouched: seeding only happens when a
# machine has no database of its own yet (see db.seed_database_if_missing).
_datas = [("templates", "templates"), ("static", "static")]
_seed_db = os.path.expanduser("~/Library/Application Support/PantryTracker/pantry.db")
if os.path.exists(_seed_db):
    _datas.append((_seed_db, "seed_data"))

a = Analysis(
    ["mac_launcher.py"],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Pantry Tracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    # Universal2: one build that runs natively on both Apple Silicon and
    # Intel Macs, since we don't yet know which chip the church's dedicated
    # computer has.
    target_arch="universal2",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Pantry Tracker",
)

app = BUNDLE(
    coll,
    name="Pantry Tracker.app",
    icon="PantryTracker.icns",
    bundle_identifier="org.tncchurch.pantrytracker",
    info_plist={
        "NSHighResolutionCapable": True,
        "CFBundleShortVersionString": _version,
        "CFBundleName": "Pantry Tracker",
        # No Dock icon / app switcher entry — this app only has a menu-bar
        # icon (see mac_launcher.py). Without this, a window-less app gets
        # stuck endlessly bouncing in the Dock since macOS waits forever
        # for a window that will never appear.
        "LSUIElement": True,
    },
)
