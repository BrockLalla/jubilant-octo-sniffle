"""Entry point for the packaged macOS app.

Runs as a small menu-bar item (no Dock icon, no window) rather than a
normal windowed app. A plain --windowed PyInstaller app that never opens
a window gets stuck endlessly "bouncing" in the Dock, because macOS is
waiting for a window that will never come — the server underneath still
works, but it looks frozen and invites a Force Quit that kills a
perfectly good server. A menu-bar icon sidesteps that entirely: nothing
in the Dock, and a clear, always-visible way to reopen the app or quit it.
"""
import subprocess
import threading
import time
import urllib.request
import webbrowser

import rumps

import updater
from app import create_app, resource_path
from netinfo import get_lan_ip

PORT = 5050
UPDATE_CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # every 6 hours is plenty, and well under GitHub's rate limit


def run_server():
    app = create_app()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


def wait_for_server_healthy(timeout_seconds=15):
    """Polls the local server until it actually answers a request, rather
    than just assuming a fixed sleep was long enough."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def update_checker_loop():
    """Runs for the lifetime of the app: periodically checks for a newer
    release and stages it if found. Never applies anything to the running
    app -- see updater.apply_staged_update_if_present, which only ever
    runs at the very start of the next launch."""
    # A short initial delay so this doesn't compete with app startup.
    time.sleep(60)
    while True:
        try:
            info = updater.check_for_update()
            if info:
                updater.download_and_stage(info)
        except Exception:
            pass  # best-effort; try again next interval regardless of what went wrong
        time.sleep(UPDATE_CHECK_INTERVAL_SECONDS)


class PantryTrackerApp(rumps.App):
    def __init__(self):
        # template=True lets macOS auto-tint the icon (black in a light menu
        # bar, white in a dark one) instead of showing it in fixed colors,
        # which would be unreadable against a light-colored bar.
        super().__init__(
            "Pantry Tracker",
            icon=resource_path("static", "images", "menubar_icon.png"),
            template=True,
            quit_button="Quit Pantry Tracker",
        )
        version_item = rumps.MenuItem(f"Version {updater.get_current_version()}", callback=None)
        self.menu = [
            "Open Pantry Tracker",
            "Volunteer Check-In Address",
            None,  # separator
            version_item,
            "Check for Updates Now",
        ]

    @rumps.clicked("Open Pantry Tracker")
    def open_main(self, _):
        webbrowser.open(f"http://127.0.0.1:{PORT}/host")

    @rumps.clicked("Check for Updates Now")
    def check_updates_now(self, _):
        info = updater.check_for_update()
        if not info:
            rumps.alert(title="Check for Updates", message="You're already on the latest version.")
            return
        staged = updater.download_and_stage(info)
        if staged:
            rumps.alert(
                title="Update Ready",
                message=(
                    f"Version {info['version']} has been downloaded and will be installed "
                    f"automatically the next time Pantry Tracker restarts."
                ),
            )
        else:
            rumps.alert(title="Check for Updates", message="Found an update but couldn't download it. Will try again automatically later.")

    @rumps.clicked("Volunteer Check-In Address")
    def show_address(self, _):
        ip = get_lan_ip()
        url = f"http://{ip}:{PORT}/checkin"
        # The native alert's text isn't easily selectable, so copy the URL
        # to the clipboard directly -- nothing to select, just paste it
        # into a text or email to send to volunteers.
        subprocess.run("pbcopy", input=url.encode(), check=False)
        rumps.alert(
            title="Volunteer Check-In Address",
            message=(
                f"Copied to your clipboard -- paste it into a text or email to volunteers:\n\n"
                f"{url}\n\n"
                f"On each volunteer's tablet or phone (same WiFi), open that address, then use "
                f"Share > Add to Home Screen so it opens like an app icon."
            ),
        )


if __name__ == "__main__":
    # Must run before anything else: applies a staged update (or rolls
    # back a previous one that never confirmed healthy) and, if it does
    # either, relaunches and exits -- so this only ever falls through to
    # the rest of startup when there's nothing to do.
    updater.apply_staged_update_if_present()

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    if wait_for_server_healthy():
        updater.confirm_update_healthy()
    # If the server never came up, deliberately skip confirming -- if this
    # launch was running a freshly-applied update, leaving the pending
    # marker in place means the next launch attempt will roll back to the
    # last-known-good version instead of repeating a broken one.

    threading.Thread(target=update_checker_loop, daemon=True).start()

    if updater.running_from_protected_location():
        rumps.alert(
            title="Move Pantry Tracker to Applications",
            message=(
                "Pantry Tracker is running from Downloads, Desktop, or Documents. macOS blocks apps "
                "there from updating themselves, so automatic updates will silently fail to install.\n\n"
                "Quit the app, drag it into your Applications folder, and open it from there instead."
            ),
        )

    webbrowser.open(f"http://127.0.0.1:{PORT}/host")
    PantryTrackerApp().run()
