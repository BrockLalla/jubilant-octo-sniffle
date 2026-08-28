"""Migrates per-member ID-verification data ("Verified" Yes/No, one per
additional household member) from "Household:Individual Data.numbers" into
the new members.id_verified column.

The source has up to 9 additional-member blocks per row (First Name, Last
Name, Age/DOB, Relationship, Verified, "Additional Household Members"),
starting at column 12 and repeating every 6 columns. Households are matched
by the source's numeric ID (column 0) against households.household_code.
Within a household, each additional-member block is matched to a `members`
row by first+last name (case-insensitive) -- the primary/self applicant is
NOT touched here, since their verification already lives on
households.id_verified from the original migration.

Usage:
    python migrate_id_verification.py            # dry run
    python migrate_id_verification.py --commit    # write to the real database
"""
import argparse
import datetime
import shutil

from numbers_parser import Document

import db

SOURCE_FILE = "/Users/brocklalla/Documents/TnC Pantry Project/Imported Data/Household:Individual Data.numbers"
REAL_DB_PATH = "/Users/brocklalla/Library/Application Support/PantryTracker/pantry.db"

ID_COL = 0
BLOCK_START_COL = 12
BLOCK_SIZE = 6
NUM_BLOCKS = 9


def is_yes(v):
    return isinstance(v, str) and v.strip().lower() == "yes"


def clean_str(v):
    """Some name cells hold stray numbers/other junk from data-entry
    mistakes -- treat anything that isn't real text as blank rather than
    crashing or coercing it into a misleading string."""
    return v.strip() if isinstance(v, str) else ""


def parse_rows(table):
    """Returns [(household_code, [(first, last, verified_bool), ...]), ...]"""
    rows = []
    for r in range(1, table.num_rows):
        raw_id = table.cell(r, ID_COL).value
        if raw_id is None:
            continue
        code = str(int(raw_id))
        members = []
        for b in range(NUM_BLOCKS):
            base = BLOCK_START_COL + b * BLOCK_SIZE
            first = table.cell(r, base).value
            last = table.cell(r, base + 1).value
            verified_raw = table.cell(r, base + 4).value
            first, last = clean_str(first), clean_str(last)
            if not first and not last:
                continue
            members.append((first, last, is_yes(verified_raw)))
        if members:
            rows.append((code, members))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    print(f"Reading {SOURCE_FILE} ...")
    doc = Document(SOURCE_FILE)
    table = doc.sheets[0].tables[0]
    source_rows = parse_rows(table)
    print(f"Parsed {len(source_rows)} household rows with at least one additional member.")

    db.DB_PATH = REAL_DB_PATH
    conn = db.get_db()
    household_lookup = {
        r["household_code"]: r["id"] for r in conn.execute("SELECT id, household_code FROM households")
    }
    members_by_household = {}
    for r in conn.execute("SELECT id, household_id, first_name, last_name, id_verified FROM members"):
        members_by_household.setdefault(r["household_id"], []).append(dict(r))
    conn.close()

    updates = []  # (member_id, new_value)
    unmatched_households = []
    unmatched_members = []
    already_correct = 0
    changing = 0

    for code, source_members in source_rows:
        household_id = household_lookup.get(code)
        if household_id is None:
            unmatched_households.append(code)
            continue
        real_members = members_by_household.get(household_id, [])
        used_ids = set()
        for first, last, verified in source_members:
            match = None
            for m in real_members:
                if m["id"] in used_ids:
                    continue
                if m["first_name"].strip().lower() == first.lower() and m["last_name"].strip().lower() == last.lower():
                    match = m
                    break
            if match is None:
                unmatched_members.append((code, first, last))
                continue
            used_ids.add(match["id"])
            new_val = 1 if verified else 0
            if (match["id_verified"] or 0) == new_val:
                already_correct += 1
            else:
                changing += 1
                updates.append((match["id"], new_val))

    print(f"\nMatched members to update: {len(updates)} (already correct: {already_correct})")
    print(f"Unmatched households (code not found in database): {len(unmatched_households)}")
    for c in unmatched_households[:10]:
        print(f"  - {c}")
    print(f"Unmatched members (name not found within matched household): {len(unmatched_members)}")
    for c, f, l in unmatched_members[:15]:
        print(f"  - household {c}: {f} {l}")

    yes_count = sum(1 for _, v in updates if v == 1)
    no_count = len(updates) - yes_count
    print(f"\nOf the updates: {yes_count} set to verified, {no_count} set to not-verified")

    if not args.commit:
        print("\nDry run only -- nothing written. Re-run with --commit to import for real.")
        return

    backup_path = REAL_DB_PATH + f".backup-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(REAL_DB_PATH, backup_path)
    print(f"\nBacked up real database to {backup_path}")

    conn = db.get_db()
    try:
        conn.executemany("UPDATE members SET id_verified = ? WHERE id = ?", [(v, mid) for mid, v in updates])
        conn.commit()
        print(f"\nUpdated {len(updates)} member records.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
