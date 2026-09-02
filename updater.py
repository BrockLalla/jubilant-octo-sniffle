"""Background self-update checker for the packaged macOS app.

Checks a GitHub repo's Releases for a version newer than the one currently
running, downloads and verifies it in the background, and stages it --
never touching the live, running app. The staged version is only ever
swapped in at the very start of the NEXT app launch, before the check-in
server starts accepting connections, so an update can never interrupt an
active session. After swapping in, the new version must prove it actually
starts up correctly (see confirm_update_healthy); if a later launch finds
that never happened, it automatically rolls back to the previous version
instead of leaving a broken app running at the pantry.

The database is never touched by any of this -- it already lives outside
the .app bundle, in Application Support, so it's naturally unaffected by
swapping the app bundle out from under it.
"""
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile

GITHUB_REPO = "BrockLalla/jubilant-octo-sniffle"  # owner/repo


def _data_dir():
    if getattr(sys, "frozen", False):
        base = os.path.expanduser("~/Library/Application Support/PantryTracker")
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return base


def _update_dir():
    d = os.path.join(_data_dir(), "updates")
    os.makedirs(d, exist_ok=True)
    return d


def _staging_dir():
    return os.path.join(_update_dir(), "staged")


def _staged_version_marker():
    return os.path.join(_update_dir(), "staged_version.txt")


def _pending_health_check_marker():
    return os.path.join(_update_dir(), "pending_health_check.txt")


def get_current_version():
    """Reads CFBundleShortVersionString from the running app's own
    Info.plist. Returns '0.0.0' in dev mode or if anything about reading
    the frozen bundle's plist goes wrong."""
    app_path = _running_app_path()
    if not app_path:
        return "0.0.0"
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        return info.get("CFBundleShortVersionString", "0.0.0")
    except Exception:
        return "0.0.0"


def _running_app_path():
    """.../Pantry Tracker.app, derived from sys.executable
    (.../Pantry Tracker.app/Contents/MacOS/Pantry Tracker). None in dev mode."""
    if not getattr(sys, "frozen", False):
        return None
    return os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))


def _version_tuple(v):
    v = (v or "").lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_for_update():
    """Returns {"version", "zip_url", "sha256"} for the latest GitHub
    release if it's newer than the running version, else None. Never
    raises -- any network hiccup or unexpected response just means "no
    update found this time," never a crash of the caller."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "PantryTracker-Updater"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode())
        latest_version = release.get("tag_name", "")
        if not latest_version:
            return None
        if _version_tuple(latest_version) <= _version_tuple(get_current_version()):
            return None
        assets = release.get("assets", [])
        zip_asset = next((a for a in assets if a["name"].endswith(".zip")), None)
        sha_asset = next((a for a in assets if a["name"].endswith(".sha256")), None)
        if not zip_asset:
            return None
        sha256 = None
        if sha_asset:
            with urllib.request.urlopen(sha_asset["browser_download_url"], timeout=15) as r:
                sha256 = r.read().decode().split()[0].strip()
        return {"version": latest_version, "zip_url": zip_asset["browser_download_url"], "sha256": sha256}
    except Exception:
        return None


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_app_in(folder):
    if not os.path.isdir(folder):
        return None
    for name in os.listdir(folder):
        if name.endswith(".app"):
            return os.path.join(folder, name)
    return None


def is_update_staged():
    return _find_app_in(_staging_dir()) is not None


def download_and_stage(update_info):
    """Downloads the release zip, verifies its checksum (when the release
    published one), and unpacks it into a staging folder -- never touching
    the live app. Returns True on success. Safe to call repeatedly; a
    matching version already staged is a no-op."""
    marker = _staged_version_marker()
    if os.path.exists(marker):
        with open(marker) as f:
            if f.read().strip() == update_info["version"]:
                return True

    tmp_zip = os.path.join(_update_dir(), "download.zip.part")
    try:
        urllib.request.urlretrieve(update_info["zip_url"], tmp_zip)
    except Exception:
        return False

    if update_info.get("sha256"):
        try:
            actual = _sha256_file(tmp_zip)
        except Exception:
            actual = None
        if not actual or actual.lower() != update_info["sha256"].lower():
            os.remove(tmp_zip)
            return False

    staging = _staging_dir()
    if os.path.exists(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    try:
        with zipfile.ZipFile(tmp_zip) as z:
            z.extractall(staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        return False
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)

    if not _find_app_in(staging):
        shutil.rmtree(staging, ignore_errors=True)
        return False

    with open(marker, "w") as f:
        f.write(update_info["version"])
    return True


def _relaunch(app_path):
    subprocess.Popen(["open", "-n", app_path])
    os._exit(0)  # replaced by the new process; this one is done


def apply_staged_update_if_present():
    """Call once, at the very start of app launch, before the server binds
    to a port. Three possible outcomes:

    1. A previous update was applied but never confirmed healthy (the
       pending_health_check marker is still there) -- roll back to the
       backed-up prior version and relaunch that instead.
    2. A new update is staged and ready -- swap it in, mark it pending
       confirmation, and relaunch (the new process will call
       confirm_update_healthy() once it's actually up and serving).
    3. Neither -- does nothing, returns normally.

    Cases 1 and 2 replace this process entirely (relaunch + os._exit), so
    control never returns to the caller in those cases.
    """
    current_app = _running_app_path()
    if not current_app:
        return  # dev mode: nothing to update

    backup_app = current_app + ".backup"

    if os.path.exists(_pending_health_check_marker()):
        if os.path.isdir(backup_app):
            try:
                if os.path.isdir(current_app):
                    shutil.rmtree(current_app)
                shutil.move(backup_app, current_app)
            except Exception:
                return  # couldn't roll back cleanly; leave things as-is rather than make it worse
            os.remove(_pending_health_check_marker())
            _relaunch(current_app)
        else:
            os.remove(_pending_health_check_marker())
        return

    staged_app = _find_app_in(_staging_dir())
    if not staged_app:
        return

    try:
        if os.path.isdir(backup_app):
            shutil.rmtree(backup_app, ignore_errors=True)
        shutil.move(current_app, backup_app)
        shutil.move(staged_app, current_app)
    except Exception:
        # Best-effort recovery: if the move partway failed, try to restore
        # the original app from backup so we don't leave nothing runnable.
        if os.path.isdir(backup_app) and not os.path.isdir(current_app):
            shutil.move(backup_app, current_app)
        return

    shutil.rmtree(_staging_dir(), ignore_errors=True)
    if os.path.exists(_staged_version_marker()):
        os.remove(_staged_version_marker())
    with open(_pending_health_check_marker(), "w") as f:
        f.write("pending")

    _relaunch(current_app)


def confirm_update_healthy():
    """Call once the server has actually started successfully. Clears the
    pending-rollback marker and removes the backup of the prior version --
    the update is now fully committed. A no-op if there was nothing
    pending (the common case, most launches)."""
    if os.path.exists(_pending_health_check_marker()):
        os.remove(_pending_health_check_marker())
    current_app = _running_app_path()
    if current_app:
        backup_app = current_app + ".backup"
        if os.path.isdir(backup_app):
            shutil.rmtree(backup_app, ignore_errors=True)
