import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

import db
import emailer
from netinfo import get_lan_ip, get_local_hostname

bp = Blueprint("public", __name__)


@bp.route("/")
def index():
    is_local = request.remote_addr in ("127.0.0.1", "::1")
    return render_template("index.html", is_local=is_local)


@bp.route("/host")
def host():
    # Only the computer running the server can see this page — it's the
    # admin's launch screen, not something volunteer devices on the LAN
    # should stumble onto.
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(404)
    return render_template(
        "host.html",
        lan_ip=get_lan_ip(),
        local_hostname=get_local_hostname(),
        port=request.host.split(":")[-1],
    )


@bp.route("/register", methods=["GET", "POST"])
def register():
    current_year = datetime.date.today().year
    timeslots = db.list_timeslots(active_only=True)

    if request.method == "POST":
        primary_first_name = request.form.get("primary_first_name", "").strip()
        primary_last_name = request.form.get("primary_last_name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        id_verified = bool(request.form.get("id_verified"))
        needs_diapers = bool(request.form.get("needs_diapers"))
        needs_formula = bool(request.form.get("needs_formula"))

        errors = []
        if not primary_first_name or not primary_last_name:
            errors.append("Please enter the primary applicant's first and last name.")

        pref_ids = []
        for field in ("pref1_timeslot_id", "pref2_timeslot_id", "pref3_timeslot_id"):
            raw = request.form.get(field, "").strip()
            pref_ids.append(int(raw) if raw else None)
        filled_prefs = [p for p in pref_ids if p is not None]
        if len(filled_prefs) != len(set(filled_prefs)):
            errors.append("Please choose 3 different pickup times — the same time was picked twice.")

        try:
            primary_dob = db.combine_date_parts(
                request.form.get("primary_dob_year"),
                request.form.get("primary_dob_month"),
                request.form.get("primary_dob_day"),
            )
        except ValueError:
            primary_dob = None
            errors.append("Please select a complete date of birth for the primary applicant.")
        else:
            if not primary_dob:
                errors.append("Please select the primary applicant's date of birth.")

        member_first_names = request.form.getlist("member_first_name[]")
        member_last_names = request.form.getlist("member_last_name[]")
        member_months = request.form.getlist("member_dob_month[]")
        member_days = request.form.getlist("member_dob_day[]")
        member_years = request.form.getlist("member_dob_year[]")
        member_rels = request.form.getlist("member_relationship[]")
        # A hidden field (kept in sync with the checkbox by register.js)
        # guarantees exactly one "0"/"1" entry per row, so this list always
        # lines up with the other member_*[] lists even when boxes are
        # left unchecked -- a plain checkbox would just omit itself from
        # the POST entirely and shift every later row out of alignment.
        member_id_verified = request.form.getlist("member_id_verified[]")

        member_rows = [
            {
                "first_name": primary_first_name,
                "last_name": primary_last_name,
                "date_of_birth": primary_dob,
                "relationship": "Self",
                "id_verified": id_verified,
            }
        ]
        for first, last, month, day, year, rel, verified in zip(
            member_first_names, member_last_names, member_months, member_days, member_years, member_rels,
            member_id_verified,
        ):
            first, last = first.strip(), last.strip()
            if not first and not last:
                continue
            try:
                member_dob = db.combine_date_parts(year, month, day)
            except ValueError:
                errors.append(f"Please select a complete date of birth for {first or last}.")
                member_dob = None
            member_rows.append(
                {
                    "first_name": first,
                    "last_name": last,
                    "date_of_birth": member_dob,
                    "relationship": rel.strip() or "Other",
                    "id_verified": verified == "1",
                }
            )

        # Hard block on registering the same real person under a second
        # household code -- checked by name + birth YEAR (not the full
        # date -- day/month turned out to be a placeholder for nearly
        # everyone) so a family that already picked up food under one
        # household ID can't also register a second one and double-dip.
        # A name + matching year blocks automatically. A name alone, with
        # no year at all to compare, isn't reliable enough to block on by
        # itself -- it's surfaced as a warning the registrant has to
        # confirm past instead, so a coincidental same-name match doesn't
        # turn away a real new family.
        possible_duplicates = []
        for row in member_rows:
            if not row["first_name"] or not row["last_name"]:
                continue
            dup = db.find_duplicate_person(row["first_name"], row["last_name"], row["date_of_birth"])
            if not dup:
                continue
            if dup["confidence"] == "high":
                errors.append(
                    f"{row['first_name']} {row['last_name']} is already registered under household "
                    f"{dup['household_code']} ({dup['primary_name']}). Each person can only be part of one "
                    f"household — look up that household code at check-in instead of registering again."
                )
            else:
                possible_duplicates.append(
                    f"{row['first_name']} {row['last_name']} might already be registered under household "
                    f"{dup['household_code']} ({dup['primary_name']}) — we can't be fully sure since there's "
                    f"no birth year on file to compare. Please double check before continuing."
                )

        confirmed_not_duplicate = request.form.get("confirm_not_duplicate") == "1"
        show_duplicate_confirm = bool(possible_duplicates) and not confirmed_not_duplicate

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "register.html", form=request.form, current_year=current_year, timeslots=timeslots
            ), 400

        if show_duplicate_confirm:
            for w in possible_duplicates:
                flash(w, "warning")
            return render_template(
                "register.html", form=request.form, current_year=current_year, timeslots=timeslots,
                show_duplicate_confirm=True,
            ), 400

        household_id = db.create_household(
            primary_first_name, primary_last_name, phone, email, pref_ids, member_rows,
            designate_first_name=request.form.get("designate_first_name", "").strip(),
            designate_last_name=request.form.get("designate_last_name", "").strip(),
            designate_relationship=request.form.get("designate_relationship", "").strip(),
            designate_id_verified=bool(request.form.get("designate_id_verified")),
            id_verified=id_verified, needs_diapers=needs_diapers, needs_formula=needs_formula,
        )
        data = db.get_household(household_id)
        household = data["household"]

        if filled_prefs and not household["assigned_timeslot_id"]:
            flash(
                "All pickup times are currently full (30 households each) — an admin will "
                "follow up to assign you a time slot.",
                "error",
            )

        email_sent = False
        if email:
            try:
                emailer.send_registration_email(
                    email, household["household_code"], household["primary_name"],
                    household["assigned_timeslot_label"],
                )
                email_sent = True
            except Exception:
                flash(
                    f"Registration saved, but the confirmation email couldn't be sent — "
                    f"please write down household code {household['household_code']}.",
                    "error",
                )

        return render_template(
            "register_success.html",
            household=household,
            members=data["members"],
            email_sent=email_sent,
        )

    return render_template("register.html", form={}, current_year=current_year, timeslots=timeslots)


@bp.route("/checkin", methods=["GET"])
def checkin():
    query = request.args.get("q", "").strip()
    results = db.search_households_by_code(query) if query else []
    return render_template("checkin.html", query=query, results=results)


@bp.route("/checkin/<int:household_id>")
def checkin_confirm(household_id):
    data = db.get_household(household_id)
    if not data:
        flash("Household not found.", "error")
        return redirect(url_for("public.checkin"))
    existing_visit = db.visit_this_week(household_id)
    return render_template(
        "checkin_result.html",
        household=data["household"],
        members=data["members"],
        existing_visit=existing_visit,
        existing_visit_display=db.format_date_nice(existing_visit["visit_date"]) if existing_visit else None,
        blocked=bool(existing_visit),
        success=False,
    )


@bp.route("/checkin/<int:household_id>/confirm", methods=["POST"])
def checkin_do(household_id):
    data = db.get_household(household_id)
    if not data:
        flash("Household not found.", "error")
        return redirect(url_for("public.checkin"))

    verified_ids = [int(v) for v in request.form.getlist("verified_member_ids[]") if v.isdigit()]
    verify_designate = bool(request.form.get("verify_designate"))
    if verified_ids or verify_designate:
        if verified_ids:
            db.mark_members_id_verified(household_id, verified_ids)
        if verify_designate:
            db.mark_designate_id_verified(household_id)
        data = db.get_household(household_id)

    existing_visit = db.visit_this_week(household_id)
    if existing_visit:
        return render_template(
            "checkin_result.html",
            household=data["household"],
            members=data["members"],
            existing_visit=existing_visit,
            existing_visit_display=db.format_date_nice(existing_visit["visit_date"]),
            blocked=True,
            success=False,
        )

    checked_in_by = request.form.get("checked_in_by", "").strip() or None
    db.record_visit(household_id, checked_in_by)
    return render_template(
        "checkin_result.html",
        household=data["household"],
        members=data["members"],
        existing_visit=None,
        blocked=False,
        success=True,
    )
