"""Nightly off-site backup: an encrypted snapshot of the database uploaded
to Backblaze B2. Only runs on the cloud deployment -- see
db.is_cloud_deployment(). Closes the single point of failure that comes
with the database living only on Render's persistent disk: if that disk is
ever corrupted or the service deleted, this is the separate copy that
survives it, living entirely outside Render's infrastructure.

The database file is encrypted client-side (see create_encrypted_snapshot)
before it ever leaves this app, since it holds real household PII -- so a
compromise of the B2 bucket alone doesn't expose it.
"""
import base64
import datetime
import os
import sqlite3
import tempfile
import threading
import time

import db

BACKUP_HOUR_UTC = 8  # once daily; lands overnight for a North American pantry


def _derive_key(passphrase, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def create_encrypted_snapshot(passphrase):
    """Returns (payload_bytes, filename). Uses sqlite3's own backup() API
    rather than a raw file copy, so a write landing at the exact same
    moment can't produce a torn/inconsistent snapshot -- it's a proper
    transactionally-consistent copy even while the source stays live and
    in use. The random salt is prepended to the ciphertext (not secret --
    needed to re-derive the same key on decrypt) so rotating the
    passphrase later doesn't invalidate how older backups were encrypted."""
    from cryptography.fernet import Fernet

    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        source = sqlite3.connect(db.DB_PATH)
        dest = sqlite3.connect(tmp_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        with open(tmp_path, "rb") as f:
            raw_bytes = f.read()
    finally:
        os.remove(tmp_path)

    salt = os.urandom(16)
    encrypted = Fernet(_derive_key(passphrase, salt)).encrypt(raw_bytes)
    payload = salt + b"." + encrypted
    filename = f"pantry-backup-{datetime.date.today().isoformat()}.db.enc"
    return payload, filename


def decrypt_snapshot(payload, passphrase):
    """Reverses create_encrypted_snapshot() -- for restoring from an
    off-site backup if ever needed."""
    from cryptography.fernet import Fernet

    salt, encrypted = payload.split(b".", 1)
    return Fernet(_derive_key(passphrase, salt)).decrypt(encrypted)


def upload_to_b2(payload, filename, key_id, application_key, bucket_name):
    from b2sdk.v2 import B2Api, InMemoryAccountInfo

    api = B2Api(InMemoryAccountInfo())
    api.authorize_account("production", key_id, application_key)
    bucket = api.get_bucket_by_name(bucket_name)
    bucket.upload_bytes(payload, filename)


def run_backup_once():
    """One backup attempt now -- used by both the nightly scheduler and the
    admin's "Run Backup Now" button. Never raises: a backup failure must
    never be allowed to take down the live app. Returns (True, None) on
    success or (False, error_message) on failure."""
    cfg = db.get_settings_dict(["b2_key_id", "b2_application_key", "b2_bucket_name", "backup_passphrase"])
    key_id = cfg.get("b2_key_id")
    application_key = cfg.get("b2_application_key")
    bucket_name = cfg.get("b2_bucket_name")
    passphrase = cfg.get("backup_passphrase")
    if not (key_id and application_key and bucket_name and passphrase):
        return False, "Off-site backup isn't fully configured yet -- fill in all the fields above."

    try:
        payload, filename = create_encrypted_snapshot(passphrase)
        upload_to_b2(payload, filename, key_id, application_key, bucket_name)
    except Exception as e:
        db.set_setting("last_offsite_backup_status", f"failed: {e}")
        db.set_setting("last_offsite_backup_at", db.now_iso())
        db.log_audit_event(None, "offsite_backup_failed", detail=str(e))
        return False, str(e)

    db.set_setting("last_offsite_backup_status", "success")
    db.set_setting("last_offsite_backup_at", db.now_iso())
    db.log_audit_event(None, "offsite_backup_succeeded")
    return True, None


def _scheduler_loop():
    while True:
        now = datetime.datetime.utcnow()
        target = now.replace(hour=BACKUP_HOUR_UTC, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        time.sleep((target - now).total_seconds())
        try:
            run_backup_once()
        except Exception:
            pass  # run_backup_once already catches/logs everything; last-resort guard


def start_background_scheduler():
    """No-op locally -- see db.is_cloud_deployment(). Safe to call once per
    process; this app only ever runs a single worker process (required
    anyway since SQLite is single-writer -- see Dockerfile/render.yaml), so
    there's no risk of multiple schedulers double-running the backup."""
    if not db.is_cloud_deployment():
        return
    threading.Thread(target=_scheduler_loop, daemon=True).start()
