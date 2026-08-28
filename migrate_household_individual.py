"""Second migration: replaces the previous Excel import with this cleaner,
ID-numbered master roster ("Household:Individual Data.numbers").

Unlike the first migration, household codes come directly from the file's
own ID column (col 0) instead of being generated — these become the
household_code going forward, and db.py's next_household_number() will
continue the sequence for future live registrations.

Usage:
    python migrate_household_individual.py            # dry run — prints a summary, writes nothing
    python migrate_household_individual.py --commit    # deletes existing imported households and re-imports from this file
"""
import argparse
import datetime
import re
import shutil

from numbers_parser import Document

import db

SOURCE_FILE = "/Users/brocklalla/Documents/TnC Pantry Project/Imported Data/Household:Individual Data.numbers"
REAL_DB_PATH = "/Users/brocklalla/Library/Application Support/PantryTracker/pantry.db"

# Same repeating-block layout as the first file, but every column index is
# +1 because of the new leading ID column.
MEMBER_BLOCK_COLS = [12, 18, 24, 30, 36, 42, 48, 54, 60, 66]


def clean_email(v):
    if not v:
        return ""
    v = str(v).strip()
    EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.][^@\s]*\.[^@\s]+$")
    return v if EMAIL_RE.match(v) else ""


def clean_phone(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v))
    return str(v).strip()


def clean_relationship(v):
    if not v:
        return "Other"
    return str(v).strip().title()


def proof_to_id_verified(v):
    if not v:
        return False
    s = str(v).strip().lower()
    return bool(s) and "none" not in s


def approx_dob_from_age(age, as_of_date):
    if age < 0 or age > 115:
        return None
    return datetime.date(as_of_date.year - age, 1, 1).isoformat()


def parse_age_or_dob(value, as_of_date):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return approx_dob_from_age(int(value), as_of_date)
    s = str(value).strip()
    m = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            pass
    if "baby" in s.lower():
        return approx_dob_from_age(0, as_of_date)
    m2 = re.match(r"^(\d{1,3})", s)
    if m2:
        return approx_dob_from_age(int(m2.group(1)), as_of_date)
    return None


def parse_timeslot_label(label):
    if not label:
        return None
    s = str(label).strip()
    if s in ("-", "", "n/a", "N/A"):
        return None
    m = re.match(r"^([TW])\s+(\d{1,2})\s*(am|pm)?\s*-\s*(\d{1,2})\s*(am|pm)?$", s)
    if not m:
        return None
    day_letter, sh, sp, eh, ep = m.groups()
    if not ep:
        return None
    sp = sp or ep
    sh, eh = int(sh), int(eh)

    def to24(h, p):
        if p == "am":
            return 0 if h == 12 else h
        return 12 if h == 12 else h + 12

    day = 1 if day_letter == "T" else 2
    return day, f"{to24(sh, sp):02d}:00", f"{to24(eh, ep):02d}:00"


def build_timeslot_lookup(conn):
    rows = conn.execute("SELECT id, day_of_week, start_time, end_time FROM timeslots").fetchall()
    return {(r["day_of_week"], r["start_time"], r["end_time"]): r["id"] for r in rows}


def parse_rows(table):
    def col(r, c):
        return table.cell(r, c).value

    parsed = []
    warnings = []
    for r in range(1, table.num_rows):
        source_id = col(r, 0)
        first_name = col(r, 2)
        last_name = col(r, 3)
        if source_id is None or source_id == 1000 or not first_name or not last_name:
            continue  # header/example row, or genuinely blank

        household_code = f"H-{int(source_id)}"
        timestamp = col(r, 1)
        as_of = timestamp.date() if isinstance(timestamp, datetime.datetime) else datetime.date.today()
        created_at = (timestamp or datetime.datetime.now()).isoformat(timespec="seconds")

        members = [
            {
                "first_name": str(first_name).strip(),
                "last_name": str(last_name).strip(),
                "date_of_birth": parse_age_or_dob(col(r, 6), as_of),
                "relationship": "Self",
            }
        ]
        for base in MEMBER_BLOCK_COLS:
            mfn, mln, mage, mrel = col(r, base), col(r, base + 1), col(r, base + 2), col(r, base + 3)
            if not mfn and not mln and not mage and not mrel:
                continue
            members.append(
                {
                    "first_name": str(mfn).strip() if mfn else "Unnamed",
                    "last_name": str(mln).strip() if mln else str(last_name).strip(),
                    "date_of_birth": parse_age_or_dob(mage, as_of),
                    "relationship": clean_relationship(mrel),
                }
            )

        timeslot_key = parse_timeslot_label(col(r, 80))
        raw_slot = col(r, 80)
        if raw_slot and str(raw_slot).strip() not in ("-", "") and not timeslot_key:
            warnings.append(f"row {r} (ID {household_code}): unrecognized timeslot label {raw_slot!r}")

        parsed.append(
            {
                "row": r,
                "household_code": household_code,
                "email": clean_email(col(r, 4)),
                "phone": clean_phone(col(r, 5)),
                "id_verified": proof_to_id_verified(col(r, 10)),
                "needs_formula": col(r, 72) == "Yes",
                "needs_diapers": col(r, 73) == "Yes",
                "created_at": created_at,
                "timeslot_key": timeslot_key,
                "members": members,
            }
        )

    ids = [p["household_code"] for p in parsed]
    dup_codes = {c for c in ids if ids.count(c) > 1}
    if dup_codes:
        warnings.append(f"DUPLICATE household_code values in source file: {sorted(dup_codes)}")

    return parsed, warnings


def import_household(conn, rec, timeslot_lookup):
    ts = rec["created_at"]
    primary = rec["members"][0]
    assigned_timeslot_id = timeslot_lookup.get(rec["timeslot_key"]) if rec["timeslot_key"] else None

    cur = conn.execute(
        "INSERT INTO households (household_code, primary_first_name, primary_last_name, phone, email, "
        "assigned_timeslot_id, id_verified, needs_diapers, needs_formula, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rec["household_code"], primary["first_name"], primary["last_name"], rec["phone"], rec["email"],
         assigned_timeslot_id, 1 if rec["id_verified"] else 0, 1 if rec["needs_diapers"] else 0,
         1 if rec["needs_formula"] else 0, ts),
    )
    household_id = cur.lastrowid

    for m in rec["members"]:
        m_cur = conn.execute(
            "INSERT INTO members (household_id, first_name, last_name, date_of_birth, relationship, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (household_id, m["first_name"], m["last_name"], m["date_of_birth"], m["relationship"], ts),
        )
        member_id = m_cur.lastrowid
        conn.execute("UPDATE members SET member_code = ? WHERE id = ?", (f"M-{member_id:05d}", member_id))

    return household_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    print(f"Reading {SOURCE_FILE} ...")
    doc = Document(SOURCE_FILE)
    table = doc.sheets[0].tables[0]
    records, warnings = parse_rows(table)

    total_members = sum(len(r["members"]) for r in records)
    matched_timeslots = sum(1 for r in records if r["timeslot_key"])

    print(f"\nParsed {len(records)} households, {total_members} individuals total.")
    print(f"  Household code range: {records[0]['household_code']} .. {records[-1]['household_code']}")
    print(f"  With email: {sum(1 for r in records if r['email'])}")
    print(f"  With phone: {sum(1 for r in records if r['phone'])}")
    print(f"  ID verified: {sum(1 for r in records if r['id_verified'])}")
    print(f"  Needs diapers: {sum(1 for r in records if r['needs_diapers'])}")
    print(f"  Needs formula: {sum(1 for r in records if r['needs_formula'])}")
    print(f"  With a matched timeslot: {matched_timeslots}")
    print(f"\n{len(warnings)} warnings:")
    for w in warnings[:20]:
        print(f"  - {w}")

    print("\nSample of first 3 parsed households:")
    for r in records[:3]:
        p = r["members"][0]
        print(f"  {r['household_code']}: {p['first_name']} {p['last_name']} | email={r['email']!r} "
              f"phone={r['phone']!r} members={len(r['members'])} timeslot={r['timeslot_key']}")

    if not args.commit:
        print("\nDry run only — nothing written. Re-run with --commit to import for real.")
        return

    backup_path = REAL_DB_PATH + f".backup-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(REAL_DB_PATH, backup_path)
    print(f"\nBacked up real database to {backup_path}")

    db.DB_PATH = REAL_DB_PATH
    conn = db.get_db()
    try:
        existing = conn.execute("SELECT COUNT(*) AS c FROM households").fetchone()["c"]
        conn.execute("DELETE FROM visits")
        conn.execute("DELETE FROM members")
        conn.execute("DELETE FROM households")
        print(f"Deleted {existing} previously-imported households (and their members/visits).")

        timeslot_lookup = build_timeslot_lookup(conn)
        for rec in records:
            import_household(conn, rec, timeslot_lookup)
        conn.commit()
        print(f"\nImported {len(records)} households ({total_members} individuals) into the real database.")
        print(f"Next new registration through the app will be household_code H-{db.next_household_number(conn)}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
