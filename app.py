import os
import sys

from flask import Flask

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
    db.seed_database_if_missing()
    app.secret_key = db.get_secret_key()
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # allow database-backup uploads
    db.init_db()
    app.jinja_env.globals["household_status"] = db.household_status
    app.jinja_env.filters["commas"] = lambda v: "{:,}".format(v) if isinstance(v, (int, float)) else v

    from routes.public import bp as public_bp
    from routes.admin import bp as admin_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    return app
