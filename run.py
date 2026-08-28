import os

from app import create_app
from netinfo import get_lan_ip


if __name__ == "__main__":
    app = create_app()
    # 5000 collides with macOS AirPlay Receiver on many Macs, so default to 5050.
    port = int(os.environ.get("PANTRY_PORT", 5050))
    ip = get_lan_ip()
    print("Pantry Tracker is running.")
    print(f"  On this computer: http://127.0.0.1:{port}")
    print(f"  On the WiFi network: http://{ip}:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
