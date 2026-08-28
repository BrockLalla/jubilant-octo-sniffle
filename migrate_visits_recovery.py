"""Recovers additional visit records missed by the original migrate_visits.py:

1. Typo'd checkmarks in the historical grid (columns AI:HG, i.e. 2022-01-04
   through 2026-05-26) — cells where someone typed "x" or a mistyped "true"
   instead of using the real checkbox. Column HH is a known blank/error
   column in the source file and is skipped entirely per instruction.
2. The five real summer-2026 pickup dates hidden behind a broken header
   format (columns HM:IB) — 2026-07-07, 07-14, 07-21, 07-28, 08-04, 08-11.
   These columns have a real day-of-month number in the header but the
   month has to be tracked via a separately-placed text label, unlike the
   rest of the sheet where the header cell itself holds a full date.

Explicitly excluded (see conversation record / commit message for full
reasoning):
- Column HH (index 215): blank/error column, confirmed by the pantry admin.
- Columns HI:HL (index 216-219): a 4-column block that would be "June 2026"
  but whose day-of-month labels (7/14/21/28) fall on Sundays -- inconsistent
  with every other week in the dataset, which is Tuesday-only. Real
  attendance data exists in these columns (399, 3, 305, 1 households) but
  the exact calendar dates are unrecoverable from the file alone. Left for
  a follow-up once the admin confirms the actual dates.
- Columns after IK (index 245+): impossible day-of-month values (32, 39,
  46, ...) from a dragged-fill artifact, and confirmed to have zero
  checkmarks of any kind -- nothing to recover.
- Non-checkmark values found in the grid that are NOT counted as a visit:
  misplaced names, "f"+number tokens (household-ID-shaped, likely pasted
  into the wrong cell), "falsef" (a typo of FALSE, i.e. explicitly did NOT
  attend), "#N/A", stray single letters, empty strings, and a misplaced
  date value.

Usage:
    python migrate_visits_recovery.py            # dry run
    python migrate_visits_recovery.py --commit    # write to the real database
"""
import argparse
import datetime
import shutil

from numbers_parser import Document

import db

SOURCE_FILE = "/Users/brocklalla/Documents/TnC Pantry Project/Imported Data/Visit Data.numbers"
REAL_DB_PATH = "/Users/brocklalla/Library/Application Support/PantryTracker/pantry.db"

DATE_HEADER_ROW = 2
MONTH_LABEL_ROW = 1
DATA_START_ROW = 3
ID_COL = 1

HISTORICAL_END_COL = 214  # HG, 2026-05-26 -- last column with a real date header
SKIP_COL = 215            # HH -- confirmed blank/error column, ignore entirely
SUMMER_COLS = {           # index -> ISO date, confirmed directly against the source by the pantry admin
    216: "2026-06-07",
    217: "2026-06-14",
    218: "2026-06-21",
    219: "2026-06-28",  # co-located with a stray "JULY" label; confirmed to actually be June 28
    220: "2026-07-07",
    221: "2026-07-14",
    222: "2026-07-21",
    # 223 ("day=28", forward-filled JULY) is a duplicate column for the same
    # week as 219 -- its one real record was hand-corrected from a wrongly
    # imported 2026-07-28 to 2026-06-28. Mapped here too so a fresh run
    # converges on the same, already-deduped, result instead of drifting.
    223: "2026-06-28",
    224: "2026-08-04",
    225: "2026-08-11",
}


def is_attended(v):
    if v is True:
        return True
    if isinstance(v, str):
        norm = v.strip().lower()
        if norm == "x":
            return True
        if norm.replace(" ", "") == "true":
            return True
    return False


def find_historical_date_columns(table):
    cols = []
    for c in range(0, HISTORICAL_END_COL + 1):
        if c == SKIP_COL:
            continue
        v = table.cell(DATE_HEADER_ROW, c).value
        if isinstance(v, datetime.datetime):
            cols.append((c, v.date().isoformat()))
    return cols


def parse_new_visits(table, household_lookup, existing_visits):
    # Some households appear on more than one spreadsheet row (confirmed:
    # 20 of them). A set here -- rather than a list -- means two rows for
    # the same household both marking the same date can never produce a
    # duplicate insert.
    date_cols = find_historical_date_columns(table) + sorted(SUMMER_COLS.items())

    new_visits = set()
    unmatched_ids = set()

    for r in range(DATA_START_ROW, table.num_rows):
        raw_id = table.cell(r, ID_COL).value
        if raw_id is None:
            continue
        code = str(int(raw_id))
        household_id = household_lookup.get(code)
        if household_id is None:
            unmatched_ids.add(code)
            continue

        for col_idx, visit_date in date_cols:
            v = table.cell(r, col_idx).value
            if col_idx <= HISTORICAL_END_COL and v is True:
                continue  # already imported by migrate_visits.py
            if is_attended(v) and (household_id, visit_date) not in existing_visits:
                new_visits.add((household_id, visit_date))

    by_date_new = {}
    for hid, vdate in new_visits:
        by_date_new[vdate] = by_date_new.get(vdate, 0) + 1

    return sorted(new_visits), unmatched_ids, by_date_new, date_cols


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    print(f"Reading {SOURCE_FILE} ...")
    doc = Document(SOURCE_FILE)
    table = doc.sheets[0].tables[0]  # "REGISTRATION"

    db.DB_PATH = REAL_DB_PATH
    conn = db.get_db()
    household_lookup = {
        r["household_code"]: r["id"] for r in conn.execute("SELECT id, household_code FROM households")
    }
    existing_visits = {
        (r["household_id"], r["visit_date"]) for r in conn.execute("SELECT household_id, visit_date FROM visits")
    }
    conn.close()

    new_visits, unmatched, by_date_new, date_cols = parse_new_visits(table, household_lookup, existing_visits)

    conn = db.get_db()
    dupe_groups = conn.execute(
        "SELECT household_id, visit_date, COUNT(*) c, MIN(id) keep_id FROM visits "
        "GROUP BY household_id, visit_date HAVING c > 1"
    ).fetchall()
    conn.close()
    dupe_rows_to_delete = sum(g["c"] - 1 for g in dupe_groups)

    print(f"\nScanned {len(date_cols)} date columns (historical AI:HG minus HH, plus summer HM:IB).")
    print(f"New visit records to add: {len(new_visits)}")
    print("\nBy date:")
    for d in sorted(by_date_new):
        print(f"  {d}: +{by_date_new[d]}")
    print(f"\nUnmatched household IDs seen in the sheet but not in the database: {len(unmatched)}")
    print(f"\nPre-existing duplicate (household, date) pairs already in the database: {len(dupe_groups)} "
          f"({dupe_rows_to_delete} extra rows to remove)")
    for g in dupe_groups:
        print(f"  household {g['household_id']}, {g['visit_date']}: {g['c']} rows -> keeping id {g['keep_id']}")

    if not args.commit:
        print("\nDry run only -- nothing written. Re-run with --commit to import for real.")
        return

    backup_path = REAL_DB_PATH + f".backup-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(REAL_DB_PATH, backup_path)
    print(f"\nBacked up real database to {backup_path}")

    conn = db.get_db()
    try:
        cur = conn.execute(
            "DELETE FROM visits WHERE id NOT IN (SELECT MIN(id) FROM visits GROUP BY household_id, visit_date)"
        )
        print(f"Removed {cur.rowcount} pre-existing duplicate visit row(s).")

        now = db.now_iso()
        conn.executemany(
            "INSERT INTO visits (household_id, visit_date, checked_in_by, created_at) VALUES (?, ?, NULL, ?)",
            [(hid, vdate, now) for hid, vdate in new_visits],
        )

        # Guards against this exact class of bug ever silently recurring.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_visits_household_date ON visits(household_id, visit_date)"
        )

        conn.commit()
        print(f"Imported {len(new_visits)} new visit records.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
