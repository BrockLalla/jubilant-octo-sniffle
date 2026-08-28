import socket
import subprocess


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def get_local_hostname():
    """This Mac's .local mDNS hostname (e.g. 'Brocks-MacBook-Air.local') —
    keeps working automatically even when the IP address changes, with no
    router configuration needed. Returns None if it can't be determined,
    so callers can fall back to the raw IP."""
    try:
        name = subprocess.check_output(
            ["scutil", "--get", "LocalHostName"], stderr=subprocess.DEVNULL, timeout=3
        ).decode().strip()
        return f"{name}.local" if name else None
    except Exception:
        return None
