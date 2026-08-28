"""Migrates historical visit/attendance data from "Visit Data.numbers" (the
REGISTRATION sheet — one row per household, one column per pickup date,
True/False attendance grid) into the visits table.

Matches each row to an already-imported household via the same ID scheme
used in the household migration (col 1 here == the number in "H-<n>").
Households not found (e.g. IDs that didn't make it into the cleaned master
roster) are skipped and reported, not guessed at.

Usage:
    python migrate_visits.py            # dry run — prints a summary, writes nothing
    python migrate_visits.py --commit   # actually writes to the real database
"""
import argparse
import datetime
import shutil

from numbers_parser import Document

import db

SOURCE_FILE = "/Users/brocklalla/Documents/TnC Pantry Project/Imported Data/Visit Data.numbers"
REAL_DB_PATH = "/Users/brocklalla/Library/Application Support/PantryTracker/pantry.db"

DATE_HEADER_ROW = 2
DATA_START_ROW = 3
ID_COL = 1


def find_date_columns(table):
    """Returns [(col_index, iso_date), ...] for every column in the header
    row that actually holds a date — skips stray non-date cells (a couple
    of corrupted header cells exist in the source) automatically."""
    cols = []
    for c in range(table.num_cols):
        v = table.cell(DATE_HEADER_ROW, c).value
        if isinstance(v, datetime.datetime):
            cols.append((c, v.date().isoformat()))
    return cols


def parse_visits(table, household_lookup):
    date_cols = find_date_columns(table)
    matched_visits = []  # (household_id, visit_date)
    unmatched_ids = []
    total_households_with_visits = 0

    for r in range(DATA_START_ROW, table.num_rows):
        raw_id = table.cell(r, ID_COL).value
        if raw_id is None:
            continue
        code = f"H-{int(raw_id)}"
        household_id = household_lookup.get(code)
        if household_id is None:
            unmatched_ids.append((r, code))
            continue

        row_had_visit = False
        for col_idx, visit_date in date_cols:
            if table.cell(r, col_idx).value is True:
                matched_visits.append((household_id, visit_date))
                row_had_visit = True
        if row_had_visit:
            total_households_with_visits += 1

    return matched_visits, unmatched_ids, total_households_with_visits, date_cols


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    print(f"Reading {SOURCE_FILE} ...")
    doc = Document(SOURCE_FILE)
    table = doc.sheets[1].tables[0]  # "REGISTRATION" sheet

    db.DB_PATH = REAL_DB_PATH
    conn = db.get_db()
    household_lookup = {
        r["household_code"]: r["id"] for r in conn.execute("SELECT id, household_code FROM households")
    }
    conn.close()

    visits, unmatched, households_with_visits, date_cols = parse_visits(table, household_lookup)

    print(f"\nDate range covered: {date_cols[0][1]} .. {date_cols[-1][1]} ({len(date_cols)} pickup dates)")
    print(f"Households with at least one visit: {households_with_visits}")
    print(f"Total visit records to import: {len(visits)}")
    print(f"Unmatched household IDs (no corresponding household in the database): {len(unmatched)}")
    for r, code in unmatched[:15]:
        print(f"  - row {r}: {code} not found")

    if not args.commit:
        print("\nDry run only — nothing written. Re-run with --commit to import for real.")
        return

    backup_path = REAL_DB_PATH + f".backup-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(REAL_DB_PATH, backup_path)
    print(f"\nBacked up real database to {backup_path}")

    conn = db.get_db()
    try:
        existing_visits = conn.execute("SELECT COUNT(*) AS c FROM visits").fetchone()["c"]
        if existing_visits:
            print(f"Note: {existing_visits} visit(s) already exist and will be kept as-is; new ones are added alongside them.")
        now = db.now_iso()
        conn.executemany(
            "INSERT INTO visits (household_id, visit_date, checked_in_by, created_at) VALUES (?, ?, NULL, ?)",
            [(hid, vdate, now) for hid, vdate in visits],
        )
        conn.commit()
        print(f"\nImported {len(visits)} visit records across {households_with_visits} households.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
