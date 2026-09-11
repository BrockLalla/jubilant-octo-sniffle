import datetime
import os
import sys

from flask import Flask
from flask_wtf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import offsite_backup


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
    # client and https:// scheme when deployed behind Render's reverse proxy
    # (or any standard one -- this just trusts X-Forwarded-* headers).
    # A no-op locally / in the packaged Mac app, since nothing there sends
    # those headers in the first place.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.seed_database_if_missing()
    app.secret_key = db.get_secret_key()
    CSRFProtect(app)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # allow database-backup uploads
    app.config["PREFERRED_URL_SCHEME"] = "https"
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Secure requires HTTPS to even send the cookie back -- true on Render
    # (real TLS, ProxyFix sees it via X-Forwarded-Proto), but the local Mac
    # app and LAN access are plain HTTP, so this must stay conditional or
    # every local admin login would silently stop working.
    app.config["SESSION_COOKIE_SECURE"] = db.is_cloud_deployment()
    # An admin session (see routes/admin.py's login()) now expires after 4
    # hours of inactivity instead of staying open indefinitely until the
    # browser is closed -- on a shared pantry laptop, a forgotten open tab
    # shouldn't leave an admin session live all day. Flask refreshes this
    # on every request by default (SESSION_REFRESH_EACH_REQUEST), so it's
    # a sliding idle timeout, not a fixed 4-hour cap from login.
    app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(hours=4)
    db.init_db()
    offsite_backup.start_background_scheduler()
    app.jinja_env.globals["household_status"] = db.household_status
    app.jinja_env.filters["commas"] = lambda v: "{:,}".format(v) if isinstance(v, (int, float)) else v

    from routes.public import bp as public_bp
    from routes.admin import bp as admin_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    @app.after_request
    def _security_headers(response):
        # 'unsafe-inline' is needed for script-src/style-src because the
        # templates rely throughout on inline onclick/onsubmit handlers and
        # style="..." attributes (see e.g. admin/backup.html, settings.html)
        # -- tightening that to a nonce-based policy would mean touching
        # every template's inline handlers, a separate follow-up. This still
        # blocks loading any *third-party* script/style/frame, which is the
        # actual risk a reverse-proxy'd, internet-facing deploy adds.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if db.is_cloud_deployment():
            # Only meaningful (and only honored by browsers) over a real
            # HTTPS connection, which is Render-only -- a no-op locally.
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    return app
