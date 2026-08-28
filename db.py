"""SQLite access layer for the Pantry Tracker.

All queries are parameterized. Callers get plain dicts back (via sqlite3.Row)
so templates and route code never touch raw SQL.
"""
import os
import sys
import sqlite3
import datetime
import math
import secrets
import shutil


def get_data_dir():
    """Where the database (and secret key) live.

    When packaged with PyInstaller (sys.frozen), the app bundle itself is
    read-only, so real data goes in the user's Application Support folder
    and survives app rebuilds/updates. When running from source, keep it
    next to the code for easy dev access.
    """
    if getattr(sys, "frozen", False):
        base = os.path.expanduser("~/Library/Application Support/PantryTracker")
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(base, exist_ok=True)
    return base


DB_PATH = os.path.join(get_data_dir(), "pantry.db")
SECRET_KEY_PATH = os.path.join(get_data_dir(), "secret.txt")


def get_secret_key():
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "r") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_KEY_PATH, "w") as f:
        f.write(key)
    return key


def seed_database_if_missing():
    """On a brand-new install with no database yet, copy in whatever data
    was bundled into the app at build time (see pantry.spec) instead of
    starting blank. Never touches a machine that already has its own data --
    this only ever fires once, on that machine's very first launch.
    """
    if os.path.exists(DB_PATH):
        return
    if not getattr(sys, "frozen", False):
        return
    seed_path = os.path.join(getattr(sys, "_MEIPASS", ""), "seed_data", "pantry.db")
    if os.path.exists(seed_path):
        shutil.copy2(seed_path, DB_PATH)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def restore_database(uploaded_path):
    """Validate an uploaded backup file and swap it in as the live database.

    The current database is copied into a `backups/` subfolder first, so an
    import can always be undone by restoring that file by hand. Returns the
    path of that pre-import backup.
    """
    try:
        conn = sqlite3.connect(uploaded_path)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
    except sqlite3.DatabaseError:
        raise ValueError("That file doesn't look like a valid database backup.")

    required = {"households", "members", "visits", "admin_users"}
    if not required.issubset(tables):
        raise ValueError("That file doesn't look like a Pantry Tracker backup (missing expected tables).")

    backup_dir = os.path.join(get_data_dir(), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(backup_dir, f"pantry-before-import-{timestamp}.db")
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup_path)

    shutil.copy2(uploaded_path, DB_PATH)
    init_db()  # apply any schema migrations the imported file might be missing
    return backup_path


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS timeslots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_of_week INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS households (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_code TEXT UNIQUE,
            primary_first_name TEXT NOT NULL,
            primary_last_name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            pref1_timeslot_id INTEGER REFERENCES timeslots(id),
            pref2_timeslot_id INTEGER REFERENCES timeslots(id),
            pref3_timeslot_id INTEGER REFERENCES timeslots(id),
            assigned_timeslot_id INTEGER REFERENCES timeslots(id),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL REFERENCES households(id),
            member_code TEXT UNIQUE,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            date_of_birth TEXT,
            relationship TEXT NOT NULL DEFAULT 'Self',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL REFERENCES households(id),
            visit_date TEXT NOT NULL,
            checked_in_by TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            invited_by TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS admin_password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_user_id INTEGER NOT NULL REFERENCES admin_users(id),
            token TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS closures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            closure_date TEXT NOT NULL UNIQUE,
            reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_members_household ON members(household_id);
        CREATE INDEX IF NOT EXISTS idx_visits_household ON visits(household_id);
        CREATE INDEX IF NOT EXISTS idx_visits_date ON visits(visit_date);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_visits_household_date ON visits(household_id, visit_date);
        CREATE INDEX IF NOT EXISTS idx_households_assigned_timeslot ON households(assigned_timeslot_id);
        """
    )
    _ensure_columns(conn, "households", {
        "designate_first_name": "TEXT",
        "designate_last_name": "TEXT",
        "designate_relationship": "TEXT",
        "id_verified": "INTEGER NOT NULL DEFAULT 0",
        "needs_diapers": "INTEGER NOT NULL DEFAULT 0",
        "needs_formula": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure_columns(conn, "admin_users", {
        "email": "TEXT",
    })
    _ensure_columns(conn, "members", {
        "id_verified": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure_columns(conn, "households", {
        "anonymized_at": "TEXT",
    })
    conn.commit()
    conn.close()


def _ensure_columns(conn, table, columns):
    """Adds any missing columns to an already-existing table (ALTER TABLE ADD
    COLUMN), so schema changes don't require wiping real data that's already
    on disk. `columns` is {column_name: sql_type_and_default}.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def today_iso():
    return datetime.date.today().isoformat()


def format_date_nice(iso_str):
    if not iso_str:
        return ""
    d = datetime.date.fromisoformat(iso_str)
    return d.strftime("%A, %B %-d")


def format_date_short(iso_str):
    if not iso_str:
        return ""
    d = datetime.date.fromisoformat(iso_str)
    return d.strftime("%b %-d, %Y")


def household_status(member_count):
    """Color-coded household size category, always derived from the live
    member count rather than stored/typed in — same reasoning as household
    size itself: a computed field can't drift out of sync."""
    if member_count <= 1:
        return {"code": "S", "label": "Single", "color": "yellow"}
    if member_count == 2:
        return {"code": "C", "label": "Couple", "color": "blue"}
    if member_count <= 4:
        return {"code": "F", "label": "Family of 3-4", "color": "red"}
    return {"code": "F+", "label": "Family of 5+", "color": "green"}


DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def format_time_12h(hhmm):
    t = datetime.datetime.strptime(hhmm, "%H:%M")
    return t.strftime("%-I:%M %p")


def timeslot_label(day_of_week, start_time, end_time):
    return f"{DAY_NAMES[day_of_week]}, {format_time_12h(start_time)} – {format_time_12h(end_time)}"


def combine_date_parts(year, month, day):
    """Combine dropdown-selected year/month/day strings into an ISO date.

    Returns None if all three are blank (date not provided). Raises
    ValueError if only some are filled in, or if the combination isn't a
    real calendar date (e.g. Feb 30) — callers should show a validation
    error rather than silently dropping a bad date.
    """
    year = (year or "").strip()
    month = (month or "").strip()
    day = (day or "").strip()
    if not year and not month and not day:
        return None
    if not (year and month and day):
        raise ValueError("Incomplete date")
    try:
        return datetime.date(int(year), int(month), int(day)).isoformat()
    except (ValueError, TypeError):
        raise ValueError("Invalid date")


def next_household_number(conn):
    """Next household_code number, computed fresh from the highest existing
    one each time rather than a stored counter — self-heals regardless of
    deletes/imports instead of risking drift."""
    row = conn.execute(
        "SELECT MAX(CAST(household_code AS INTEGER)) AS mx FROM households "
        "WHERE household_code GLOB '[0-9]*'"
    ).fetchone()
    return (row["mx"] or 0) + 1


# ---------- Timeslots ----------

TIMESLOT_CAPACITY = 30                # true physical/staffing capacity of a slot
DEFAULT_TIMESLOT_ACTIVE_CAPACITY = 25  # default for the admin-editable setting below


def get_timeslot_active_capacity():
    """Capacity used for assignment decisions -- admin-editable (Timeslots
    page), capped to TIMESLOT_CAPACITY. Defaults a bit below the true
    capacity as a buffer, since "active" excludes stale households who
    could still return anytime; an admin comfortable with that risk can
    raise it up to the full TIMESLOT_CAPACITY."""
    raw = get_setting("timeslot_active_capacity")
    if raw is None:
        return DEFAULT_TIMESLOT_ACTIVE_CAPACITY
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMESLOT_ACTIVE_CAPACITY
    return max(1, min(value, TIMESLOT_CAPACITY))


def set_timeslot_active_capacity(value):
    """Returns True if saved, False if value was out of the allowed 1..TIMESLOT_CAPACITY range."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return False
    if not (1 <= value <= TIMESLOT_CAPACITY):
        return False
    set_setting("timeslot_active_capacity", str(value))
    return True


def _timeslot_count(conn, timeslot_id):
    # Anonymized households (PII removed, visit history kept for grant
    # reporting) no longer count against capacity -- they've effectively
    # left the schedule, freeing their spot for someone else.
    return conn.execute(
        "SELECT COUNT(*) FROM households WHERE assigned_timeslot_id = ? AND anonymized_at IS NULL",
        (timeslot_id,),
    ).fetchone()[0]


def _timeslot_active_count(conn, timeslot_id):
    """Households assigned to this slot who are neither anonymized nor
    stale (a visit -- or, if they've never visited, their registration --
    within the last 6 months). A slot can look full on paper while most of
    its households have quietly stopped coming; this is the number that
    reflects real, current load for assignment purposes. Unlike
    anonymizing, staleness is reversible -- a quiet household could show up
    again anytime -- which is why assignment uses a reduced, admin-editable
    buffer (get_timeslot_active_capacity()) against this count rather than
    the full TIMESLOT_CAPACITY."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    return conn.execute(
        """
        SELECT COUNT(*) FROM households h
        WHERE h.assigned_timeslot_id = ?
          AND h.anonymized_at IS NULL
          AND COALESCE(
                (SELECT MAX(v.visit_date) FROM visits v WHERE v.household_id = h.id),
                substr(h.created_at, 1, 10)
              ) >= ?
        """,
        (timeslot_id, cutoff),
    ).fetchone()[0]


def assign_timeslot(conn, pref_ids):
    """Pick the least-populated of the given timeslot ids (rank order) that
    still has room under get_timeslot_active_capacity(), counting only
    currently-active households (see _timeslot_active_count) -- a slot full
    of households that stopped coming months ago is treated as having real
    room, not as full. Ties go to the earlier (higher-ranked) preference,
    since we only ever replace the current best on a strictly lower count.
    If all 3 preferences are full, falls back to the least-populated active
    timeslot with room anywhere in the schedule. Returns None if every
    active timeslot is at capacity. Must be called with an open connection
    from the same transaction as the household insert that will follow, so
    count-then-assign is atomic against concurrent registrations.
    """
    active_capacity = get_timeslot_active_capacity()
    best_id = None
    best_count = None
    seen = set()
    for tid in pref_ids:
        if not tid or tid in seen:
            continue
        seen.add(tid)
        count = _timeslot_active_count(conn, tid)
        if count >= active_capacity:
            continue
        if best_count is None or count < best_count:
            best_id, best_count = tid, count
    if best_id is not None:
        return best_id

    # All 3 preferences were full (or none given) — fall back to whatever
    # active slot has the most room.
    row = conn.execute(
        """
        SELECT id FROM (
            SELECT t.id AS id,
                   (SELECT COUNT(*) FROM households h
                    WHERE h.assigned_timeslot_id = t.id AND h.anonymized_at IS NULL
                      AND COALESCE(
                            (SELECT MAX(v2.visit_date) FROM visits v2 WHERE v2.household_id = h.id),
                            substr(h.created_at, 1, 10)
                          ) >= ?) AS cnt
            FROM timeslots t WHERE t.active = 1
        ) WHERE cnt < ?
        ORDER BY cnt ASC LIMIT 1
        """,
        ((datetime.date.today() - datetime.timedelta(days=180)).isoformat(), active_capacity),
    ).fetchone()
    return row["id"] if row else None


def _timeslot_label_by_id(conn, timeslot_id):
    if not timeslot_id:
        return None
    row = conn.execute(
        "SELECT day_of_week, start_time, end_time FROM timeslots WHERE id = ?", (timeslot_id,)
    ).fetchone()
    return timeslot_label(row["day_of_week"], row["start_time"], row["end_time"]) if row else None


def list_timeslots(active_only=False):
    conn = get_db()
    try:
        q = (
            "SELECT *, (SELECT COUNT(*) FROM households h WHERE h.assigned_timeslot_id = timeslots.id "
            "AND h.anonymized_at IS NULL) AS assigned_count FROM timeslots"
        )
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY day_of_week, start_time"
        rows = conn.execute(q).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["label"] = timeslot_label(d["day_of_week"], d["start_time"], d["end_time"])
            # Real current load (excludes stale households too) -- this is
            # what new registrants are actually weighed against, which can
            # be well below assigned_count if a slot is full of households
            # that stopped coming.
            d["active_count"] = _timeslot_active_count(conn, d["id"])
            result.append(d)
        return result
    finally:
        conn.close()


def create_timeslot(day_of_week, start_time, end_time):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO timeslots (day_of_week, start_time, end_time, active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (day_of_week, start_time, end_time, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def set_timeslot_active(timeslot_id, active):
    conn = get_db()
    try:
        conn.execute("UPDATE timeslots SET active = ? WHERE id = ?", (1 if active else 0, timeslot_id))
        conn.commit()
    finally:
        conn.close()


def delete_timeslot(timeslot_id):
    """Deletes only if no household currently references it; returns False otherwise."""
    conn = get_db()
    try:
        in_use = conn.execute(
            "SELECT COUNT(*) FROM households WHERE assigned_timeslot_id = ? OR pref1_timeslot_id = ? "
            "OR pref2_timeslot_id = ? OR pref3_timeslot_id = ?",
            (timeslot_id, timeslot_id, timeslot_id, timeslot_id),
        ).fetchone()[0]
        if in_use:
            return False
        conn.execute("DELETE FROM timeslots WHERE id = ?", (timeslot_id,))
        conn.commit()
        return True
    finally:
        conn.close()


# ---------- Households / members ----------

def find_duplicate_person(first_name, last_name, date_of_birth, exclude_household_id=None):
    """Looks for this person already registered as a member of a DIFFERENT
    household -- used to catch the same person being registered twice under
    two household codes (double-dipping on pickups).

    Matches on first name + last name + birth YEAR, not the full date.
    Nearly every DOB in this dataset turned out to be January 1st of the
    birth year -- a placeholder used when only the birth year was actually
    known -- so comparing full dates was really just comparing years
    dressed up as more precise than it actually was. Year of birth is real
    signal (it's what was actually collected) and a name + year match
    hard-blocks automatically.

    When no DOB was given at all -- not even a year -- falls back to
    matching on name alone. That's a much weaker signal (nothing to rule
    out two different people who share a name), so it's graded as low
    confidence: callers should surface it as a warning a human can confirm
    or dismiss rather than an automatic block.

    Returns None if no match, otherwise {household_code, primary_name,
    confidence}, confidence being "high" (name + year matched) or "low"
    (name-only match, no year available to compare)."""
    if not first_name or not last_name:
        return None
    conn = get_db()
    try:
        q = (
            "SELECT h.household_code, h.primary_first_name, h.primary_last_name "
            "FROM members m JOIN households h ON h.id = m.household_id "
            "WHERE m.first_name = ? COLLATE NOCASE AND m.last_name = ? COLLATE NOCASE"
        )
        params = [first_name.strip(), last_name.strip()]
        year = date_of_birth[:4] if date_of_birth else None
        if year:
            q += " AND substr(m.date_of_birth, 1, 4) = ?"
            params.append(year)
        if exclude_household_id:
            q += " AND m.household_id != ?"
            params.append(exclude_household_id)
        row = conn.execute(q, params).fetchone()
        if not row:
            return None
        return {
            "household_code": row["household_code"],
            "primary_name": f"{row['primary_first_name']} {row['primary_last_name']}",
            "confidence": "high" if year else "low",
        }
    finally:
        conn.close()


def possible_duplicate_people(confidence=None):
    """Audits the WHOLE database for same-name people across different
    households -- catches pre-existing duplicates that were bulk-migrated
    from the old spreadsheet system and never passed through
    find_duplicate_person (which only runs at registration time).

    confidence: None for all pairs, "high" for name + matching birth year,
    or "low" for a name match where at least one side has no birth year on
    file to compare. A name match where both sides HAVE a birth year but
    the years differ isn't a duplicate at all (different people, same
    name) and is never returned.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT "
            "  h1.id AS hh1_id, h1.household_code AS hh1_code, "
            "  h1.primary_first_name AS hh1_first, h1.primary_last_name AS hh1_last, "
            "  m1.first_name AS m1_first, m1.last_name AS m1_last, m1.date_of_birth AS dob1, "
            "  h2.id AS hh2_id, h2.household_code AS hh2_code, "
            "  h2.primary_first_name AS hh2_first, h2.primary_last_name AS hh2_last, "
            "  m2.date_of_birth AS dob2 "
            "FROM members m1 "
            "JOIN members m2 ON m1.first_name = m2.first_name COLLATE NOCASE "
            "  AND m1.last_name = m2.last_name COLLATE NOCASE "
            "  AND m1.household_id < m2.household_id "
            "JOIN households h1 ON h1.id = m1.household_id "
            "JOIN households h2 ON h2.id = m2.household_id "
            "WHERE h1.anonymized_at IS NULL AND h2.anonymized_at IS NULL "
            "  AND ("
            "    m1.date_of_birth IS NULL OR m1.date_of_birth = '' OR "
            "    m2.date_of_birth IS NULL OR m2.date_of_birth = '' OR "
            "    substr(m1.date_of_birth, 1, 4) = substr(m2.date_of_birth, 1, 4)"
            "  ) "
            "ORDER BY m1.last_name COLLATE NOCASE, m1.first_name COLLATE NOCASE"
        ).fetchall()
        results = []
        for r in rows:
            pair_confidence = "high" if (r["dob1"] and r["dob2"]) else "low"
            if confidence and pair_confidence != confidence:
                continue
            results.append(
                {
                    "name": f"{r['m1_first']} {r['m1_last']}",
                    "confidence": pair_confidence,
                    "hh1_id": r["hh1_id"], "hh1_code": r["hh1_code"],
                    "hh1_primary": f"{r['hh1_first']} {r['hh1_last']}",
                    "hh2_id": r["hh2_id"], "hh2_code": r["hh2_code"],
                    "hh2_primary": f"{r['hh2_first']} {r['hh2_last']}",
                }
            )
        return results
    finally:
        conn.close()


def create_household(primary_first_name, primary_last_name, phone, email,
                      pref_timeslot_ids, member_rows, designate_first_name=None,
                      designate_last_name=None, designate_relationship=None,
                      id_verified=False, needs_diapers=False, needs_formula=False):
    """member_rows: list of dicts with first_name, last_name, date_of_birth,
    relationship. The primary applicant should be included as one of the
    rows with relationship='Self'. pref_timeslot_ids: up to 3 timeslot ids in
    rank order. designate_* fields describe someone other than a household
    member who's authorized to pick up on the household's behalf (e.g. a
    caregiver). Returns the new household id.
    """
    conn = get_db()
    try:
        ts = now_iso()
        pref1, pref2, pref3 = (list(pref_timeslot_ids) + [None, None, None])[:3]
        assigned_timeslot_id = assign_timeslot(conn, [pref1, pref2, pref3])
        household_code = str(next_household_number(conn))

        cur = conn.execute(
            "INSERT INTO households (household_code, primary_first_name, primary_last_name, phone, email, "
            "pref1_timeslot_id, pref2_timeslot_id, pref3_timeslot_id, "
            "assigned_timeslot_id, designate_first_name, designate_last_name, "
            "designate_relationship, id_verified, needs_diapers, needs_formula, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (household_code, primary_first_name, primary_last_name, phone, email,
             pref1, pref2, pref3, assigned_timeslot_id, designate_first_name or None,
             designate_last_name or None, designate_relationship or None,
             1 if id_verified else 0, 1 if needs_diapers else 0, 1 if needs_formula else 0, ts),
        )
        household_id = cur.lastrowid

        for row in member_rows:
            m_cur = conn.execute(
                "INSERT INTO members (household_id, first_name, last_name, date_of_birth, relationship, "
                "id_verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (household_id, row["first_name"], row["last_name"], row.get("date_of_birth") or None,
                 row.get("relationship") or "Other", 1 if row.get("id_verified") else 0, ts),
            )
            member_id = m_cur.lastrowid
            conn.execute(
                "UPDATE members SET member_code = ? WHERE id = ?",
                (f"M-{member_id:05d}", member_id),
            )

        conn.commit()
        return household_id
    finally:
        conn.close()


def get_household(household_id):
    conn = get_db()
    try:
        household = conn.execute(
            "SELECT *, (primary_first_name || ' ' || primary_last_name) AS primary_name "
            "FROM households WHERE id = ?",
            (household_id,),
        ).fetchone()
        if not household:
            return None
        household = dict(household)
        household["assigned_timeslot_label"] = _timeslot_label_by_id(conn, household.get("assigned_timeslot_id"))
        household["pref1_label"] = _timeslot_label_by_id(conn, household.get("pref1_timeslot_id"))
        household["pref2_label"] = _timeslot_label_by_id(conn, household.get("pref2_timeslot_id"))
        household["pref3_label"] = _timeslot_label_by_id(conn, household.get("pref3_timeslot_id"))
        members = conn.execute(
            "SELECT *, (first_name || ' ' || last_name) AS name FROM members "
            "WHERE household_id = ? ORDER BY id",
            (household_id,),
        ).fetchall()
        return {"household": household, "members": [dict(m) for m in members]}
    finally:
        conn.close()


# Sort keys the Households admin list can use -- "status" reuses "size"
# since a household's status badge is derived purely from its member count.
_HOUSEHOLD_SORT_COLUMNS = {
    "code": ["CAST(h.household_code AS INTEGER)"],
    "name": ["h.primary_last_name COLLATE NOCASE", "h.primary_first_name COLLATE NOCASE"],
    "phone": ["h.phone"],
    "size": ["member_count"],
    "status": ["member_count"],
}


def _household_order_by(sort, direction):
    cols = _HOUSEHOLD_SORT_COLUMNS.get(sort, _HOUSEHOLD_SORT_COLUMNS["name"])
    order_dir = "DESC" if direction == "desc" else "ASC"
    return ", ".join(f"{c} {order_dir}" for c in cols)


def search_households(query, sort=None, direction="asc"):
    """Search by primary name, member name, phone, or household code."""
    conn = get_db()
    try:
        like = f"%{query.strip()}%"
        rows = conn.execute(
            """
            SELECT DISTINCT h.*, (h.primary_first_name || ' ' || h.primary_last_name) AS primary_name
            FROM households h
            LEFT JOIN members m ON m.household_id = h.id
            WHERE (h.primary_first_name || ' ' || h.primary_last_name) LIKE ?
               OR h.phone LIKE ?
               OR h.household_code LIKE ?
               OR (m.first_name || ' ' || m.last_name) LIKE ?
            ORDER BY h.primary_last_name, h.primary_first_name
            LIMIT 25
            """,
            (like, like, like, like),
        ).fetchall()
        results = []
        for row in rows:
            members = conn.execute(
                "SELECT COUNT(*) FROM members WHERE household_id = ?", (row["id"],)
            ).fetchone()[0]
            results.append({**dict(row), "member_count": members})

        if sort:
            reverse = direction == "desc"
            key_fns = {
                "code": lambda r: int(r["household_code"]),
                "name": lambda r: (r["primary_last_name"].lower(), r["primary_first_name"].lower()),
                "phone": lambda r: r["phone"] or "",
                "size": lambda r: r["member_count"],
                "status": lambda r: r["member_count"],
            }
            results.sort(key=key_fns.get(sort, key_fns["name"]), reverse=reverse)
        return results
    finally:
        conn.close()


def search_households_by_code(query):
    """Volunteer check-in search -- household code only, not name or phone.
    Two people can share a name, and a phone can be shared/reused across a
    family, but the household code is the one identifier that's supposed to
    map to exactly one household, so check-in relies on it exclusively."""
    conn = get_db()
    try:
        like = f"%{query.strip()}%"
        rows = conn.execute(
            """
            SELECT h.*, (h.primary_first_name || ' ' || h.primary_last_name) AS primary_name,
                   (SELECT COUNT(*) FROM members m WHERE m.household_id = h.id) AS member_count
            FROM households h
            WHERE h.household_code LIKE ?
            ORDER BY CAST(h.household_code AS INTEGER)
            LIMIT 25
            """,
            (like,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_all_households(sort=None, direction="asc"):
    conn = get_db()
    try:
        order_by = _household_order_by(sort, direction)
        rows = conn.execute(
            f"SELECT h.*, (h.primary_first_name || ' ' || h.primary_last_name) AS primary_name, "
            f"(SELECT COUNT(*) FROM members m WHERE m.household_id = h.id) AS member_count "
            f"FROM households h ORDER BY {order_by}"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_household(household_id, primary_first_name, primary_last_name, phone, email,
                      assigned_timeslot_id, designate_first_name=None,
                      designate_last_name=None, designate_relationship=None,
                      id_verified=False, needs_diapers=False, needs_formula=False):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE households SET primary_first_name = ?, primary_last_name = ?, phone = ?, email = ?, "
            "assigned_timeslot_id = ?, designate_first_name = ?, "
            "designate_last_name = ?, designate_relationship = ?, id_verified = ?, "
            "needs_diapers = ?, needs_formula = ? WHERE id = ?",
            (primary_first_name, primary_last_name, phone, email,
             assigned_timeslot_id, designate_first_name or None, designate_last_name or None,
             designate_relationship or None, 1 if id_verified else 0, 1 if needs_diapers else 0,
             1 if needs_formula else 0, household_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_member(member_id, first_name, last_name, date_of_birth, relationship, id_verified=False):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE members SET first_name = ?, last_name = ?, date_of_birth = ?, relationship = ?, "
            "id_verified = ? WHERE id = ?",
            (first_name, last_name, date_of_birth or None, relationship, 1 if id_verified else 0, member_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_member(member_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
        conn.commit()
    finally:
        conn.close()


def delete_household(household_id):
    """Permanently deletes a household along with all its members and visit
    history. There's no undo — the caller (admin UI) is responsible for
    confirming with the user first. Children are deleted before the parent
    since foreign_keys=ON would otherwise reject the household delete.
    """
    conn = get_db()
    try:
        conn.execute("DELETE FROM visits WHERE household_id = ?", (household_id,))
        conn.execute("DELETE FROM members WHERE household_id = ?", (household_id,))
        conn.execute("DELETE FROM households WHERE id = ?", (household_id,))
        conn.commit()
    finally:
        conn.close()


def anonymize_household(household_id):
    """Alternative to delete_household for a household that's gone stale
    (6+ months, no visits): strips personally-identifying fields but keeps
    the household row, its members' ages, and every visit record intact --
    so weekly/annual grant totals (attendance, people fed, age brackets)
    stay accurate for weeks this household really did visit, without
    holding onto their name/contact info indefinitely. date_of_birth is
    kept specifically because grant reporting buckets by age; it's the one
    "identifying" field that's load-bearing for that and is a poor
    re-identification vector on its own without a name attached to it."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE households SET primary_first_name = 'Removed', primary_last_name = '(anonymized)', "
            "phone = NULL, email = NULL, designate_first_name = NULL, designate_last_name = NULL, "
            "designate_relationship = NULL, anonymized_at = ? WHERE id = ?",
            (now_iso(), household_id),
        )
        conn.execute(
            "UPDATE members SET first_name = 'Removed', last_name = '(anonymized)' WHERE household_id = ?",
            (household_id,),
        )
        conn.commit()
    finally:
        conn.close()


def add_member(household_id, first_name, last_name, date_of_birth, relationship, id_verified=False):
    conn = get_db()
    try:
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO members (household_id, first_name, last_name, date_of_birth, relationship, "
            "id_verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (household_id, first_name, last_name, date_of_birth or None, relationship or "Other",
             1 if id_verified else 0, ts),
        )
        member_id = cur.lastrowid
        conn.execute(
            "UPDATE members SET member_code = ? WHERE id = ?",
            (f"M-{member_id:05d}", member_id),
        )
        conn.commit()
        return member_id
    finally:
        conn.close()


# ---------- Visits ----------

def current_week_range(for_date=None):
    """Return (monday_iso, sunday_iso) for the week containing for_date (default today)."""
    d = for_date or datetime.date.today()
    monday = d - datetime.timedelta(days=d.weekday())
    sunday = monday + datetime.timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def visit_this_week(household_id):
    conn = get_db()
    try:
        start, end = current_week_range()
        row = conn.execute(
            "SELECT * FROM visits WHERE household_id = ? AND visit_date BETWEEN ? AND ? "
            "ORDER BY visit_date DESC LIMIT 1",
            (household_id, start, end),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_members_id_verified(household_id, member_ids):
    """Marks the given members as ID-verified -- used from the check-in
    screen so a volunteer can tick someone off as verified on the spot,
    without a separate trip to the admin panel. Scoped to household_id so
    a submitted id can only ever affect that household's own members."""
    if not member_ids:
        return
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in member_ids)
        conn.execute(
            f"UPDATE members SET id_verified = 1 WHERE household_id = ? AND id IN ({placeholders})",
            [household_id] + list(member_ids),
        )
        conn.commit()
    finally:
        conn.close()


def record_visit(household_id, checked_in_by=None):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO visits (household_id, visit_date, checked_in_by, created_at) VALUES (?, ?, ?, ?)",
            (household_id, today_iso(), checked_in_by, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_visit(visit_id):
    """Removes a single visit record -- for correcting mistakes (wrong
    household checked in, accidental double-confirm, a test entry), not a
    routine action. Does not touch the household or its other visits."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM visits WHERE id = ?", (visit_id,))
        conn.commit()
    finally:
        conn.close()


_VISIT_SORT_COLUMNS = {
    "date": ["v.visit_date", "v.id"],
    "household": ["h.primary_last_name COLLATE NOCASE", "h.primary_first_name COLLATE NOCASE"],
    "code": ["CAST(h.household_code AS INTEGER)"],
    "size": ["member_count"],
    "checked_in_by": ["v.checked_in_by COLLATE NOCASE"],
}


def list_visits(start_date=None, end_date=None, sort=None, direction="desc"):
    conn = get_db()
    try:
        q = (
            "SELECT v.*, h.household_code, (h.primary_first_name || ' ' || h.primary_last_name) AS primary_name, "
            "(SELECT COUNT(*) FROM members m WHERE m.household_id = h.id) AS member_count "
            "FROM visits v JOIN households h ON h.id = v.household_id"
        )
        params = []
        clauses = []
        if start_date:
            clauses.append("v.visit_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("v.visit_date <= ?")
            params.append(end_date)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        cols = _VISIT_SORT_COLUMNS.get(sort, _VISIT_SORT_COLUMNS["date"])
        order_dir = "DESC" if direction == "desc" else "ASC"
        q += " ORDER BY " + ", ".join(f"{c} {order_dir}" for c in cols)
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- Stats ----------

def age_in_years(dob_iso):
    return age_as_of(dob_iso, datetime.date.today().isoformat())


def age_as_of(dob_iso, as_of_iso):
    dob = datetime.date.fromisoformat(dob_iso)
    as_of = datetime.date.fromisoformat(as_of_iso)
    return as_of.year - dob.year - ((as_of.month, as_of.day) < (dob.month, dob.day))


def list_closures(start_date=None, end_date=None):
    """All recorded pantry closure dates (past, present, or scheduled
    ahead), used both for the admin's Blocked Dates page and to mark
    closed weeks in the grant report instead of showing them as 0s."""
    conn = get_db()
    try:
        q = "SELECT * FROM closures"
        clauses, params = [], []
        if start_date:
            clauses.append("closure_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("closure_date <= ?")
            params.append(end_date)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY closure_date"
        return [dict(r) for r in conn.execute(q, params).fetchall()]
    finally:
        conn.close()


def add_closure(closure_date, reason=None):
    """Returns True on success, False if that date is already recorded."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO closures (closure_date, reason, created_at) VALUES (?, ?, ?)",
            (closure_date, reason.strip() if reason else None, now_iso()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def delete_closure(closure_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM closures WHERE id = ?", (closure_id,))
        conn.commit()
    finally:
        conn.close()


def stale_households(months=6):
    """Households whose most recent visit (or registration date, if they've
    never visited) is more than `months` ago — roughly 30 days/month.
    Already-anonymized households are excluded -- their PII is gone, so
    there's nothing left to review or act on for this list."""
    conn = get_db()
    try:
        cutoff = (datetime.date.today() - datetime.timedelta(days=months * 30)).isoformat()
        rows = conn.execute(
            """
            SELECT h.*, (h.primary_first_name || ' ' || h.primary_last_name) AS primary_name,
                   (SELECT MAX(v.visit_date) FROM visits v WHERE v.household_id = h.id) AS last_visit
            FROM households h
            WHERE h.anonymized_at IS NULL
            """
        ).fetchall()
        stale = []
        for r in rows:
            d = dict(r)
            last_activity = d["last_visit"] or d["created_at"][:10]
            if last_activity < cutoff:
                d["last_visit_display"] = d["last_visit"] or None
                stale.append(d)
        return stale
    finally:
        conn.close()


def check_and_notify_stale_households():
    """Runs on admin dashboard load; sends at most one digest email per 30
    days, so admins get an "automatic" heads-up without being spammed on
    every page load. No-ops quietly if email isn't configured yet."""
    import emailer

    last_notified = get_setting("last_stale_notified_at")
    if last_notified:
        days_since = (datetime.date.today() - datetime.date.fromisoformat(last_notified[:10])).days
        if days_since < 30:
            return
    notify_email = get_setting("admin_notify_email") or get_setting("smtp_username")
    if not notify_email:
        return
    stale = stale_households(6)
    if not stale:
        return
    try:
        emailer.send_stale_households_alert(notify_email, stale)
        set_setting("last_stale_notified_at", now_iso())
    except Exception:
        pass  # best-effort; try again next week rather than erroring the dashboard


def grant_report_stats(start_date=None, end_date=None):
    """With no date range, tiles reflect all-time totals (unchanged from
    before filtering existed). With a range, 'individuals served' and the
    age brackets scope to households that visited within it — 'Total
    Registered Individuals' stays all-time either way since registration
    isn't inherently tied to a visit window."""
    conn = get_db()
    try:
        total_individuals = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        filtering = bool(start_date or end_date)

        if filtering:
            clauses, params = [], []
            if start_date:
                clauses.append("visit_date >= ?")
                params.append(start_date)
            if end_date:
                clauses.append("visit_date <= ?")
                params.append(end_date)
            household_ids = [
                r["household_id"]
                for r in conn.execute(f"SELECT DISTINCT household_id FROM visits WHERE {' AND '.join(clauses)}", params)
            ]
        else:
            household_ids = [r["household_id"] for r in conn.execute("SELECT DISTINCT household_id FROM visits")]

        if household_ids:
            placeholders = ",".join("?" * len(household_ids))
            individuals_served = conn.execute(
                f"SELECT COUNT(*) FROM members WHERE household_id IN ({placeholders})", household_ids
            ).fetchone()[0]
        else:
            individuals_served = 0

        if filtering:
            dob_rows = (
                conn.execute(
                    f"SELECT date_of_birth FROM members WHERE household_id IN ({placeholders}) "
                    "AND date_of_birth IS NOT NULL",
                    household_ids,
                ).fetchall()
                if household_ids
                else []
            )
        else:
            dob_rows = conn.execute(
                "SELECT date_of_birth FROM members WHERE date_of_birth IS NOT NULL"
            ).fetchall()

        as_of_date = end_date or datetime.date.today().isoformat()
        children_0_3 = children_4_12 = children_13_18 = seniors_65plus = 0
        for row in dob_rows:
            age = age_as_of(row["date_of_birth"], as_of_date)
            if 0 <= age <= 3:
                children_0_3 += 1
            elif 4 <= age <= 12:
                children_4_12 += 1
            elif 13 <= age <= 18:
                children_13_18 += 1
            if age > 65:
                seniors_65plus += 1

        household_rows = conn.execute(
            "SELECT h.created_at, (SELECT COUNT(*) FROM members m WHERE m.household_id = h.id) AS mc "
            "FROM households h"
        ).fetchall()
        monthly = {}
        for row in household_rows:
            month = row["created_at"][:7]
            entry = monthly.setdefault(month, {"new_households": 0, "new_individuals": 0})
            entry["new_households"] += 1
            entry["new_individuals"] += row["mc"]
        monthly_sorted = sorted(monthly.items(), key=lambda kv: kv[0], reverse=True)[:12]

        return {
            "individuals_served": individuals_served,
            "total_individuals": total_individuals,
            "children_0_3": children_0_3,
            "children_4_12": children_4_12,
            "children_13_18": children_13_18,
            "seniors_65plus": seniors_65plus,
            "monthly": monthly_sorted,
        }
    finally:
        conn.close()


def weekly_grant_report(start_date=None, end_date=None):
    """One row per distinct pickup date that actually occurred (not every
    calendar week — matches however the pantry's schedule really ran).
    'Family' = household size 3+ and 'Senior' = age 60+ -- reverse-engineered
    against the church's actual submitted 2024 grant report (not the same
    cutoffs as the household-size status badge or the "Seniors 65+" stat
    tile, which are separate UI concepts). 'Senior'/'Child N' count a
    household once if *any* member falls in that bracket, mirroring how the
    source spreadsheet's Y/N columns worked. 'New' always means the
    household's first visit ever, even when a date range is applied — the
    range narrows which weeks are shown, not what counts as a first-time
    visitor."""
    conn = get_db()
    try:
        member_counts = {
            r["household_id"]: r["c"]
            for r in conn.execute("SELECT household_id, COUNT(*) AS c FROM members GROUP BY household_id")
        }
        member_dobs = {}
        for r in conn.execute("SELECT household_id, date_of_birth FROM members WHERE date_of_birth IS NOT NULL"):
            member_dobs.setdefault(r["household_id"], []).append(r["date_of_birth"])
        first_visit = {
            r["household_id"]: r["fd"]
            for r in conn.execute("SELECT household_id, MIN(visit_date) AS fd FROM visits GROUP BY household_id")
        }
        visits_by_date = {}
        for r in conn.execute("SELECT household_id, visit_date FROM visits"):
            visits_by_date.setdefault(r["visit_date"], []).append(r["household_id"])

        dates = sorted(visits_by_date.keys(), reverse=True)  # newest first
        if start_date:
            dates = [d for d in dates if d >= start_date]
        if end_date:
            dates = [d for d in dates if d <= end_date]

        # Recorded closures that already happened (or are happening today)
        # always show as a zero "closed" row, even on the rare date where a
        # visit still got recorded anyway (an exception made, or a mistaken
        # check-in) -- the pantry was officially closed, so that pickup
        # isn't counted in the report regardless. The underlying visit stays
        # in the database untouched; it's just excluded from this report.
        # Future closures aren't shown yet since they haven't happened.
        closure_reasons = {c["closure_date"]: c["reason"] for c in list_closures(start_date, end_date)}
        closure_dates = {d for d in closure_reasons if d <= today_iso()}

        weeks = []
        for date in sorted(set(dates) | closure_dates, reverse=True):
            if date in closure_dates:
                weeks.append({
                    "date": date, "date_display": format_date_short(date), "closed": True,
                    "closure_reason": closure_reasons[date], "new": 0, "attendance": 0,
                    "people_fed": 0, "family": 0, "senior": 0,
                    "child_0_3": 0, "child_4_12": 0, "child_13_18": 0,
                })
                continue
            household_ids = visits_by_date[date]
            row = {
                "date": date, "date_display": format_date_short(date), "closed": False,
                "closure_reason": None, "new": 0,
                "attendance": len(household_ids), "people_fed": 0,
                "family": 0, "senior": 0, "child_0_3": 0, "child_4_12": 0, "child_13_18": 0,
            }
            for hid in household_ids:
                mc = member_counts.get(hid, 0)
                row["people_fed"] += mc
                if first_visit.get(hid) == date:
                    row["new"] += 1
                if mc >= 3:
                    row["family"] += 1
                ages = [age_as_of(dob, date) for dob in member_dobs.get(hid, [])]
                if any(a >= 60 for a in ages):
                    row["senior"] += 1
                if any(0 <= a <= 3 for a in ages):
                    row["child_0_3"] += 1
                if any(4 <= a <= 12 for a in ages):
                    row["child_4_12"] += 1
                if any(13 <= a <= 18 for a in ages):
                    row["child_13_18"] += 1
            weeks.append(row)
        return weeks
    finally:
        conn.close()


def dashboard_stats(start_date=None, end_date=None):
    """With no range, defaults to year-to-date (the original behavior).
    With a range, 'Households/Individuals/Visits Served' scope to it
    instead — 'period_label' tells the template which is in effect so the
    tiles can say so. Registration totals (total_households/members/avg
    size) stay all-time regardless, same reasoning as the Grant Report."""
    conn = get_db()
    try:
        if start_date or end_date:
            clauses, params = [], []
            if start_date:
                clauses.append("visit_date >= ?")
                params.append(start_date)
            if end_date:
                clauses.append("visit_date <= ?")
                params.append(end_date)
            where = " AND ".join(clauses)
            period_label = f"{start_date or 'earliest'} to {end_date or 'today'}"
        else:
            where = "visit_date >= ?"
            params = [f"{datetime.date.today().year}-01-01"]
            period_label = "Year to Date"

        households_served = conn.execute(
            f"SELECT COUNT(DISTINCT household_id) FROM visits WHERE {where}", params
        ).fetchone()[0]

        individuals_served = conn.execute(
            f"""
            SELECT COALESCE(SUM(mc), 0) FROM (
                SELECT v.household_id,
                       (SELECT COUNT(*) FROM members m WHERE m.household_id = v.household_id) AS mc
                FROM visits v WHERE {where}
                GROUP BY v.household_id
            )
            """,
            params,
        ).fetchone()[0]

        visits_in_range = conn.execute(f"SELECT COUNT(*) FROM visits WHERE {where}", params).fetchone()[0]

        total_households = conn.execute("SELECT COUNT(*) FROM households").fetchone()[0]
        total_members = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        avg_household_size = round(total_members / total_households, 1) if total_households else 0

        total_visits_all_time = conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]

        return {
            "households_served": households_served,
            "individuals_served": individuals_served,
            "visits_in_range": visits_in_range,
            "period_label": period_label,
            "total_households": total_households,
            "total_members": total_members,
            "avg_household_size": avg_household_size,
            "total_visits_all_time": total_visits_all_time,
        }
    finally:
        conn.close()


def _nice_axis_max(value):
    """Rounds up to a clean gridline step so axis labels read as round numbers
    (matches the rest of the app's style: 0 / 50 / 100, never a jagged max)."""
    if value <= 0:
        return 4, 1
    if value <= 10:
        step = 2
    elif value <= 50:
        step = 10
    elif value <= 200:
        step = 50
    elif value <= 1000:
        step = 100
    else:
        step = 500
    return math.ceil(value / step) * step, step


def weekly_visits_chart(n_weeks=12):
    """Precomputes everything the dashboard's inline-SVG line chart needs --
    geometry lives here (not in the template) so it's one legible, testable
    function instead of arithmetic scattered through Jinja."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT visit_date, COUNT(*) AS c FROM visits GROUP BY visit_date ORDER BY visit_date DESC LIMIT ?",
            (n_weeks,),
        ).fetchall()
    finally:
        conn.close()

    weeks = [{"date": r["visit_date"], "count": r["c"]} for r in reversed(rows)]
    if not weeks:
        return {"weeks": [], "has_data": False}

    width, height = 700, 170
    pad_left, pad_right, pad_top, pad_bottom = 46, 12, 12, 28
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    counts = [w["count"] for w in weeks]
    axis_max, step = _nice_axis_max(max(counts))
    n = len(weeks)

    def x_at(i):
        return pad_left if n == 1 else pad_left + (plot_w * i / (n - 1))

    def y_at(v):
        return pad_top + plot_h - (plot_h * v / axis_max)

    for i, w in enumerate(weeks):
        w["x"] = round(x_at(i), 1)
        w["y"] = round(y_at(w["count"]), 1)
        w["date_display"] = format_date_short(w["date"])

    points_str = " ".join(f"{w['x']},{w['y']}" for w in weeks)
    baseline_y = round(y_at(0), 1)
    area_path = (
        f"M {weeks[0]['x']},{baseline_y} "
        + " ".join(f"L {w['x']},{w['y']}" for w in weeks)
        + f" L {weeks[-1]['x']},{baseline_y} Z"
    )

    # Label every point if there's room; otherwise thin them out so text
    # never collides (mark-spec: label selectively, never every point when
    # they'd crowd). The last point always gets a label -- but if the regular
    # spacing already put one right next to it, drop that neighbor instead of
    # letting the two collide.
    label_every = 1 if n <= 8 else math.ceil(n / 8)
    show = [i % label_every == 0 for i in range(n)]
    last = n - 1
    if not show[last]:
        for j in range(last - 1, -1, -1):
            if show[j]:
                if last - j < label_every:
                    show[j] = False
                break
        show[last] = True
    for i, w in enumerate(weeks):
        w["show_label"] = show[i]

    gridlines = []
    for g in range(0, axis_max + 1, step):
        gridlines.append({"y": round(y_at(g), 1), "label": "{:,}".format(g)})

    return {
        "weeks": weeks,
        "has_data": True,
        "width": width,
        "height": height,
        "plot_left": pad_left,
        "plot_right": width - pad_right,
        "points_str": points_str,
        "area_path": area_path,
        "baseline_y": baseline_y,
        "gridlines": gridlines,
        "last_point": weeks[-1],
    }


# ---------- Admin users ----------

def admin_user_count():
    conn = get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
    finally:
        conn.close()


def create_admin_user(username, password_hash, email=None):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, email, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, email, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def get_admin_user(username):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM admin_users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_admin_user_by_email(email):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM admin_users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_admin_user_by_id(admin_user_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM admin_users WHERE id = ?", (admin_user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_admin_users():
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, username, email, created_at FROM admin_users ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_admin_password(admin_user_id, password_hash):
    conn = get_db()
    try:
        conn.execute("UPDATE admin_users SET password_hash = ? WHERE id = ?", (password_hash, admin_user_id))
        conn.commit()
    finally:
        conn.close()


def update_admin_email(admin_user_id, email):
    conn = get_db()
    try:
        conn.execute("UPDATE admin_users SET email = ? WHERE id = ?", (email, admin_user_id))
        conn.commit()
    finally:
        conn.close()


# ---------- Admin invites (invite-only registration: an existing admin
# enters an email, the invitee follows a one-time link to set their own
# password and create their account) ----------

INVITE_VALID_HOURS = 48
RESET_VALID_HOURS = 1


def create_admin_invite(email, invited_by):
    conn = get_db()
    try:
        token = secrets.token_urlsafe(32)
        now = datetime.datetime.now()
        conn.execute(
            "INSERT INTO admin_invites (email, token, invited_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (email, token, invited_by, now.isoformat(timespec="seconds"),
             (now + datetime.timedelta(hours=INVITE_VALID_HOURS)).isoformat(timespec="seconds")),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def get_valid_admin_invite(token):
    """Returns the invite row if the token exists, hasn't been used, and
    hasn't expired -- None otherwise (caller doesn't need to distinguish why)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM admin_invites WHERE token = ? AND used_at IS NULL AND expires_at > ?",
            (token, now_iso()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_admin_invite_used(invite_id):
    conn = get_db()
    try:
        conn.execute("UPDATE admin_invites SET used_at = ? WHERE id = ?", (now_iso(), invite_id))
        conn.commit()
    finally:
        conn.close()


# ---------- Admin password resets ("forgot password" links) ----------

def create_password_reset(admin_user_id):
    conn = get_db()
    try:
        token = secrets.token_urlsafe(32)
        now = datetime.datetime.now()
        conn.execute(
            "INSERT INTO admin_password_resets (admin_user_id, token, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (admin_user_id, token, now.isoformat(timespec="seconds"),
             (now + datetime.timedelta(hours=RESET_VALID_HOURS)).isoformat(timespec="seconds")),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def get_valid_password_reset(token):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM admin_password_resets WHERE token = ? AND used_at IS NULL AND expires_at > ?",
            (token, now_iso()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_password_reset_used(reset_id):
    conn = get_db()
    try:
        conn.execute("UPDATE admin_password_resets SET used_at = ? WHERE id = ?", (now_iso(), reset_id))
        conn.commit()
    finally:
        conn.close()


# ---------- Settings (SMTP config, key/value) ----------

def get_setting(key, default=None):
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default
    finally:
        conn.close()


def set_setting(key, value):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def get_settings_dict(keys):
    conn = get_db()
    try:
        result = {}
        for k in keys:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (k,)).fetchone()
            result[k] = row["value"] if row else None
        return result
    finally:
        conn.close()
