import os
import sys

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

import db


def resource_path(*parts):
    """Resolve a path to bundled resources, whether running from source or
    from a PyInstaller-frozen app (which extracts/collects files under
    sys._MEIPASS instead of alongside this script)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def create_app():
    app = Flask(
        __name__,
        template_folder=resource_path("templates"),
        static_folder=resource_path("static"),
    )
    # Trusts one layer of reverse-proxy headers (X-Forwarded-For/Proto/Host)
    # so request.remote_addr and url_for(_external=True) reflect the real
    # client and https:// scheme when deployed behind Fly.io's edge proxy.
    # A no-op locally / in the packaged Mac app, since nothing there sends
    # those headers in the first place.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.seed_database_if_missing()
    app.secret_key = db.get_secret_key()
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # allow database-backup uploads
    app.config["PREFERRED_URL_SCHEME"] = "https"
    db.init_db()
    app.jinja_env.globals["household_status"] = db.household_status
    app.jinja_env.filters["commas"] = lambda v: "{:,}".format(v) if isinstance(v, (int, float)) else v

    from routes.public import bp as public_bp
    from routes.admin import bp as admin_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    return app
