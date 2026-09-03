import csv
import datetime
import io
import os
import sqlite3
import tempfile
from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, Response, send_file
from werkzeug.security import generate_password_hash, check_password_hash

import db
import emailer
import netinfo

bp = Blueprint("admin", __name__)


def external_url(path):
    """Builds a link using this Mac's LAN-reachable address (its .local
    hostname, or LAN IP as a fallback) rather than Flask's _external=True,
    which builds from whatever host the CURRENT request came in on. The
    admin always opens this app via 127.0.0.1 (see mac_launcher.py), so an
    _external=True link built while they're logged in would silently
    become "http://127.0.0.1:.../..." -- correct on their own Mac, but
    dead on arrival for anyone else who opens it, since 127.0.0.1 on their
    device just means their own device."""
    host = netinfo.get_local_hostname() or netinfo.get_lan_ip()
    port = request.host.split(":")[-1] if ":" in request.host else "80"
    return f"http://{host}:{port}{path}"


def _safe_next(default_url):
    """Returns the posted 'next' field if it's a same-app admin path,
    otherwise the given default -- lets an action taken from a list page
    (e.g. Stale Households) return there instead of always landing on the
    generic Households list. Restricted to /admin/ paths so a form can't
    be tricked into redirecting somewhere off-site."""
    next_url = request.form.get("next", "")
    if next_url.startswith("/admin/"):
        return next_url
    return default_url


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_username"):
            return redirect(url_for("admin.login"))
        return view(*args, **kwargs)

    return wrapped


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if db.admin_user_count() > 0:
        return redirect(url_for("admin.login"))

    if request.method == "POST" and request.form.get("action") == "restore_backup":
        uploaded = request.files.get("db_file")
        if not uploaded or not uploaded.filename:
            flash("Please choose a backup file to restore.", "error")
            return render_template("admin/setup.html")

        fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        uploaded.save(tmp_path)
        try:
            db.restore_database(tmp_path)
        except ValueError as e:
            flash(str(e), "error")
            return render_template("admin/setup.html")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        if db.admin_user_count() == 0:
            flash(
                "That backup was restored, but it doesn't contain any admin accounts -- "
                "you'll need to create one below.", "error",
            )
            return render_template("admin/setup.html")

        flash("Data restored from your backup. Log in with the account from your other computer.", "success")
        return redirect(url_for("admin.login"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors = []
        if not username:
            errors.append("Please choose a username.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/setup.html")

        try:
            db.create_admin_user(username, generate_password_hash(password, method="pbkdf2:sha256"))
        except sqlite3.IntegrityError:
            # Most likely cause: the form was submitted twice (e.g. a slow
            # double-click, or a page reload after already succeeding) --
            # the first submission already created the account, so send
            # them to log in with it instead of showing a raw crash.
            flash("An account already exists. Try logging in with the username/password you just set.", "error")
            return redirect(url_for("admin.login"))
        except OSError as e:
            # The data directory (~/Library/Application Support/PantryTracker)
            # couldn't be written to -- surface this instead of silently
            # looping back to setup on every subsequent visit.
            flash(f"Couldn't save the new account -- the app couldn't write to its data folder ({e}).", "error")
            return render_template("admin/setup.html")

        flash("Admin account created. Please log in.", "success")
        return redirect(url_for("admin.login"))

    return render_template("admin/setup.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if db.admin_user_count() == 0:
        return redirect(url_for("admin.setup"))

    if request.method == "POST":
        identifier = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.get_admin_user(identifier) or db.get_admin_user_by_email(identifier)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["admin_username"] = user["username"]
            return redirect(url_for("admin.dashboard"))
        flash("Incorrect username/email or password.", "error")

    return render_template("admin/login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        user = db.get_admin_user_by_email(email) if email else None
        if user:
            token = db.create_password_reset(user["id"])
            reset_link = external_url(url_for("admin.reset_password", token=token))
            try:
                emailer.send_password_reset_email(email, reset_link)
            except Exception:
                pass  # Same message either way below -- never reveal whether the email matched an account.
        # Deliberately identical whether or not the email matched anything, and
        # regardless of whether sending succeeded -- otherwise this page could
        # be used to discover which emails have admin accounts.
        flash("If that email has an admin account, a password reset link is on its way.", "success")
        return redirect(url_for("admin.login"))

    return render_template("admin/forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    reset = db.get_valid_password_reset(token)
    if not reset:
        flash("That password reset link is invalid or has expired. Request a new one below.", "error")
        return redirect(url_for("admin.forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        errors = []
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/reset_password.html", token=token)

        db.update_admin_password(reset["admin_user_id"], generate_password_hash(password, method="pbkdf2:sha256"))
        db.mark_password_reset_used(reset["id"])
        flash("Password updated. Please log in.", "success")
        return redirect(url_for("admin.login"))

    return render_template("admin/reset_password.html", token=token)


@bp.route("/stale-households")
@login_required
def stale_households():
    return render_template("admin/stale_households.html", rows=db.stale_households())


@bp.route("/possible-duplicates")
@login_required
def possible_duplicates():
    confidence = request.args.get("confidence", "").strip() or None
    if confidence not in (None, "high", "low"):
        confidence = None
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort", "").strip() or "name"
    direction = "desc" if request.args.get("dir") == "desc" else "asc"

    rows = db.possible_duplicate_people(confidence=confidence)
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in r["name"].lower()]
    if sort == "name":
        rows = sorted(rows, key=lambda r: r["name"].lower(), reverse=(direction == "desc"))

    return render_template(
        "admin/possible_duplicates.html",
        rows=rows,
        confidence=confidence,
        query=search,
        sort=sort,
        direction=direction,
        counts={
            "all": len(db.possible_duplicate_people()),
            "high": len(db.possible_duplicate_people(confidence="high")),
            "low": len(db.possible_duplicate_people(confidence="low")),
        },
    )


@bp.route("/users", methods=["GET", "POST"])
@login_required
def users():
    current_admin = db.get_admin_user(session.get("admin_username"))

    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_email":
            email = request.form.get("my_email", "").strip()
            existing = db.get_admin_user_by_email(email) if email else None
            if not email:
                flash("Enter an email address.", "error")
            elif existing and existing["id"] != current_admin["id"]:
                flash(f"{email} is already used by another admin account.", "error")
            else:
                db.update_admin_email(current_admin["id"], email)
                flash("Your email has been updated.", "success")
        elif action == "delete_admin":
            if not current_admin["is_super_admin"]:
                flash("Only a super admin can remove admin accounts.", "error")
            else:
                target_id = int(request.form.get("admin_id"))
                if target_id == current_admin["id"]:
                    flash("You can't remove your own account. Have another super admin do it instead.", "error")
                elif db.delete_admin_user(target_id):
                    flash("Admin account removed.", "success")
                else:
                    flash("Can't remove the last remaining admin account.", "error")
        elif action == "toggle_super":
            if not current_admin["is_super_admin"]:
                flash("Only a super admin can grant or remove super admin access.", "error")
            else:
                target_id = int(request.form.get("admin_id"))
                target = db.get_admin_user_by_id(target_id)
                if target:
                    db.set_admin_super(target_id, not target["is_super_admin"])
                    flash(
                        f"{target['username']} is {'now' if not target['is_super_admin'] else 'no longer'} a super admin.",
                        "success",
                    )
        else:
            email = request.form.get("email", "").strip()
            if not email:
                flash("Enter an email address to invite.", "error")
            elif db.get_admin_user_by_email(email):
                flash(f"{email} already has an admin account.", "error")
            else:
                token = db.create_admin_invite(email, invited_by=session.get("admin_username"))
                invite_link = external_url(url_for("admin.accept_invite", token=token))
                try:
                    emailer.send_admin_invite_email(email, invite_link, session.get("admin_username"))
                    flash(f"Invite sent to {email}.", "success")
                except emailer.EmailNotConfigured as e:
                    flash(str(e), "error")
                except Exception as e:
                    flash(f"Couldn't send invite email: {e}", "error")
        return redirect(url_for("admin.users"))

    return render_template(
        "admin/users.html",
        admins=db.list_admin_users(),
        invite_valid_hours=db.INVITE_VALID_HOURS,
        current_admin=current_admin,
    )


@bp.route("/users/<int:admin_id>/edit", methods=["GET", "POST"])
@login_required
def edit_admin_user(admin_id):
    current_admin = db.get_admin_user(session.get("admin_username"))
    target = db.get_admin_user_by_id(admin_id)
    if not target:
        flash("Admin account not found.", "error")
        return redirect(url_for("admin.users"))

    editing_self = target["id"] == current_admin["id"]
    if not editing_self and not current_admin["is_super_admin"]:
        flash("Only a super admin can edit another admin's account.", "error")
        return redirect(url_for("admin.users"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        errors = []

        if not username:
            errors.append("Username can't be blank.")
        else:
            existing = db.get_admin_user(username)
            if existing and existing["id"] != target["id"]:
                errors.append(f'"{username}" is already taken by another admin.')

        if password or confirm_password:
            if len(password) < 8:
                errors.append("New password must be at least 8 characters.")
            elif password != confirm_password:
                errors.append("New passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/edit_admin.html", target=target, editing_self=editing_self)

        db.update_admin_username(target["id"], username)
        if password:
            db.update_admin_password(target["id"], generate_password_hash(password, method="pbkdf2:sha256"))
        if editing_self:
            session["admin_username"] = username
        flash("Account updated." if editing_self else f"{username}'s account has been updated.", "success")
        return redirect(url_for("admin.users"))

    return render_template("admin/edit_admin.html", target=target, editing_self=editing_self)


@bp.route("/accept-invite/<token>", methods=["GET", "POST"])
def accept_invite(token):
    invite = db.get_valid_admin_invite(token)
    if not invite:
        flash("That invite link is invalid or has expired. Ask an admin to send a new one.", "error")
        return redirect(url_for("admin.login"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        errors = []
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if db.get_admin_user(invite["email"]):
            errors.append("An account already exists for this email.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/accept_invite.html", token=token, email=invite["email"])

        db.create_admin_user(
            invite["email"], generate_password_hash(password, method="pbkdf2:sha256"), email=invite["email"]
        )
        db.mark_admin_invite_used(invite["id"])
        flash("Account created. Please log in.", "success")
        return redirect(url_for("admin.login"))

    return render_template("admin/accept_invite.html", token=token, email=invite["email"])


@bp.route("/")
@login_required
def dashboard():
    db.check_and_notify_stale_households()
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    stats = db.dashboard_stats(start_date, end_date)
    chart = db.weekly_visits_chart()
    return render_template(
        "admin/dashboard.html", stats=stats, chart=chart, start_date=start_date or "", end_date=end_date or ""
    )


@bp.route("/households")
@login_required
def households():
    query = request.args.get("q", "").strip()
    sort = request.args.get("sort", "").strip()
    if sort:
        direction = "desc" if request.args.get("dir") == "desc" else "asc"
    else:
        # Default view: most recently registered households first.
        sort, direction = "code", "desc"
    rows = (
        db.search_households(query, sort=sort, direction=direction)
        if query
        else db.list_all_households(sort=sort, direction=direction)
    )
    return render_template(
        "admin/households.html", rows=rows, query=query, sort=sort, direction=direction
    )


@bp.route("/households/<int:household_id>", methods=["GET", "POST"])
@login_required
def household_detail(household_id):
    data = db.get_household(household_id)
    if not data:
        flash("Household not found.", "error")
        return redirect(url_for("admin.households"))

    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_household":
            raw_timeslot = request.form.get("assigned_timeslot_id", "").strip()
            db.update_household(
                household_id,
                request.form.get("primary_first_name", "").strip(),
                request.form.get("primary_last_name", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("email", "").strip(),
                int(raw_timeslot) if raw_timeslot else None,
                designate_first_name=request.form.get("designate_first_name", "").strip(),
                designate_last_name=request.form.get("designate_last_name", "").strip(),
                designate_relationship=request.form.get("designate_relationship", "").strip(),
                designate_id_verified=bool(request.form.get("designate_id_verified")),
                id_verified=bool(request.form.get("id_verified")),
                needs_diapers=bool(request.form.get("needs_diapers")),
                needs_formula=bool(request.form.get("needs_formula")),
            )
            flash("Household updated.", "success")
        elif action == "update_member":
            try:
                dob = db.combine_date_parts(
                    request.form.get("date_of_birth_year"),
                    request.form.get("date_of_birth_month"),
                    request.form.get("date_of_birth_day"),
                )
            except ValueError:
                flash("That date of birth wasn't complete — member not updated.", "error")
            else:
                db.update_member(
                    int(request.form.get("member_id")),
                    request.form.get("first_name", "").strip(),
                    request.form.get("last_name", "").strip(),
                    dob,
                    request.form.get("relationship", "").strip() or "Other",
                    id_verified=bool(request.form.get("id_verified")),
                )
                flash("Member updated.", "success")
        elif action == "delete_member":
            db.delete_member(int(request.form.get("member_id")))
            flash("Member removed.", "success")
        elif action == "delete_household":
            code = data["household"]["household_code"]
            db.delete_household(household_id)
            flash(f"Household {code} and all its records were permanently deleted.", "success")
            return redirect(_safe_next(url_for("admin.households")))
        elif action == "anonymize_household":
            code = data["household"]["household_code"]
            db.anonymize_household(household_id)
            flash(
                f"Household {code}'s personal info was removed. Its visit history stays in grant reports.",
                "success",
            )
            return redirect(_safe_next(url_for("admin.households")))
        elif action == "add_member":
            first_name = request.form.get("first_name", "").strip()
            last_name = request.form.get("last_name", "").strip()
            if first_name or last_name:
                try:
                    dob = db.combine_date_parts(
                        request.form.get("date_of_birth_year"),
                        request.form.get("date_of_birth_month"),
                        request.form.get("date_of_birth_day"),
                    )
                except ValueError:
                    flash("That date of birth wasn't complete — member not added.", "error")
                else:
                    dup = db.find_duplicate_person(first_name, last_name, dob, exclude_household_id=household_id)
                    if dup and dup["confidence"] == "high":
                        flash(
                            f"{first_name} {last_name} is already registered under household "
                            f"{dup['household_code']} ({dup['primary_name']}) — not added here too.",
                            "error",
                        )
                    else:
                        if dup:
                            flash(
                                f"Note: {first_name} {last_name} has the same name as someone under household "
                                f"{dup['household_code']} ({dup['primary_name']}), but there's no birth year on "
                                f"file to confirm it's the same person — added anyway, worth a double check.",
                                "warning",
                            )
                        db.add_member(
                            household_id,
                            first_name,
                            last_name,
                            dob,
                            request.form.get("relationship", "").strip() or "Other",
                            id_verified=bool(request.form.get("id_verified")),
                        )
                        flash("Member added.", "success")
        return redirect(url_for("admin.household_detail", household_id=household_id))

    data = db.get_household(household_id)
    return render_template(
        "admin/household_detail.html",
        household=data["household"],
        members=data["members"],
        current_year=datetime.date.today().year,
        timeslots=db.list_timeslots(active_only=False),
    )


@bp.route("/timeslots", methods=["GET", "POST"])
@login_required
def timeslots():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            day = request.form.get("day_of_week", "").strip()
            start = request.form.get("start_time", "").strip()
            end = request.form.get("end_time", "").strip()
            if day == "" or not start or not end:
                flash("Please fill in the day, start time, and end time.", "error")
            elif start >= end:
                flash("End time must be after start time.", "error")
            else:
                db.create_timeslot(int(day), start, end)
                flash("Timeslot added.", "success")
        elif action == "activate":
            db.set_timeslot_active(int(request.form.get("timeslot_id")), True)
            flash("Timeslot activated.", "success")
        elif action == "deactivate":
            db.set_timeslot_active(int(request.form.get("timeslot_id")), False)
            flash("Timeslot deactivated — no longer offered at registration, existing assignments kept.", "success")
        elif action == "delete":
            if db.delete_timeslot(int(request.form.get("timeslot_id"))):
                flash("Timeslot deleted.", "success")
            else:
                flash("Can't delete a timeslot that households are still assigned to — deactivate it instead.", "error")
        elif action == "set_active_capacity":
            if db.set_timeslot_active_capacity(request.form.get("active_capacity", "")):
                flash("Assignment capacity updated.", "success")
            else:
                flash(f"Capacity must be a number between 1 and {db.TIMESLOT_CAPACITY}.", "error")
        return redirect(url_for("admin.timeslots"))

    return render_template(
        "admin/timeslots.html", rows=db.list_timeslots(active_only=False), day_names=db.DAY_NAMES,
        capacity=db.TIMESLOT_CAPACITY, active_capacity=db.get_timeslot_active_capacity(),
    )


@bp.route("/closures", methods=["GET", "POST"])
@login_required
def closures():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            closure_date = request.form.get("closure_date", "").strip()
            reason = request.form.get("reason", "").strip()
            if not closure_date:
                flash("Please choose a date.", "error")
            elif db.add_closure(closure_date, reason):
                flash(f"{closure_date} marked as a closure.", "success")
            else:
                flash(f"{closure_date} is already recorded as a closure.", "error")
        elif action == "delete":
            db.delete_closure(int(request.form.get("closure_id")))
            flash("Closure removed.", "success")
        return redirect(url_for("admin.closures"))

    return render_template("admin/closures.html", rows=db.list_closures())


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "save":
            db.set_setting("smtp_host", request.form.get("smtp_host", "").strip())
            db.set_setting("smtp_port", request.form.get("smtp_port", "").strip() or "587")
            db.set_setting("smtp_username", request.form.get("smtp_username", "").strip())
            new_password = request.form.get("smtp_password", "")
            if new_password:
                db.set_setting("smtp_password", new_password)
            db.set_setting("from_name", request.form.get("from_name", "").strip())
            db.set_setting("admin_notify_email", request.form.get("admin_notify_email", "").strip())
            flash("Email settings saved.", "success")
        elif action == "save_template":
            db.set_setting("email_subject", request.form.get("email_subject", "").strip() or emailer.DEFAULT_EMAIL_SUBJECT)
            db.set_setting("email_body", request.form.get("email_body", "").strip() or emailer.DEFAULT_EMAIL_BODY)
            flash("Registration email updated.", "success")
        elif action == "test_template":
            test_address = request.form.get("template_test_address", "").strip()
            if not test_address:
                flash("Enter an address to send the sample registration email to.", "error")
            else:
                try:
                    emailer.send_test_registration_email(
                        test_address,
                        request.form.get("email_subject", "").strip() or emailer.DEFAULT_EMAIL_SUBJECT,
                        request.form.get("email_body", "").strip() or emailer.DEFAULT_EMAIL_BODY,
                    )
                    flash(f"Sample registration email sent to {test_address}.", "success")
                except emailer.EmailNotConfigured as e:
                    flash(str(e), "error")
                except Exception as e:
                    flash(f"Couldn't send sample email: {e}", "error")
        return redirect(url_for("admin.settings"))

    cfg = db.get_settings_dict(
        ["smtp_host", "smtp_port", "smtp_username", "from_name", "admin_notify_email", "email_subject", "email_body"]
    )
    cfg["email_subject"] = cfg.get("email_subject") or emailer.DEFAULT_EMAIL_SUBJECT
    cfg["email_body"] = emailer.ensure_html_body(cfg.get("email_body") or emailer.DEFAULT_EMAIL_BODY)
    return render_template("admin/settings.html", cfg=cfg)


@bp.route("/visits")
@login_required
def visits():
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    sort = request.args.get("sort", "").strip()
    if sort:
        direction = "desc" if request.args.get("dir") == "desc" else "asc"
    else:
        # Default view: most recent pickups first.
        sort, direction = "date", "desc"
    rows = db.list_visits(start_date, end_date, sort=sort, direction=direction)
    return render_template(
        "admin/visits.html", rows=rows, start_date=start_date or "", end_date=end_date or "",
        sort=sort, direction=direction,
    )


@bp.route("/visits/<int:visit_id>/delete", methods=["POST"])
@login_required
def delete_visit(visit_id):
    db.delete_visit(visit_id)
    flash("Visit record deleted.", "success")
    return redirect(url_for(
        "admin.visits",
        start_date=request.form.get("start_date") or None,
        end_date=request.form.get("end_date") or None,
        sort=request.form.get("sort") or None,
        dir=request.form.get("direction") or None,
    ))


@bp.route("/reports")
@login_required
def reports():
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    return render_template(
        "admin/reports.html",
        stats=db.grant_report_stats(start_date, end_date),
        weeks=db.weekly_grant_report(start_date, end_date),
        start_date=start_date or "",
        end_date=end_date or "",
    )


@bp.route("/reports/export.csv")
@login_required
def export_report():
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    stats = db.grant_report_stats(start_date, end_date)
    weeks = db.weekly_grant_report(start_date, end_date)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Metric", "Value"])
    writer.writerow(["Unique Individuals Served (ever visited)", stats["individuals_served"]])
    writer.writerow(["Total Registered Individuals", stats["total_individuals"]])
    writer.writerow(["Children Age 0-3", stats["children_0_3"]])
    writer.writerow(["Children Age 4-12", stats["children_4_12"]])
    writer.writerow(["Children Age 13-18", stats["children_13_18"]])
    writer.writerow(["Seniors Over Age 65", stats["seniors_65plus"]])
    writer.writerow([])
    writer.writerow(["Week", "New", "Attendance", "People Fed", "Family", "Senior",
                      "Child 0-3", "Child 4-12", "Child 13-18"])
    for w in weeks:
        if w.get("closed") and w["attendance"] == 0:
            label = "Closed" + (f" — {w['closure_reason']}" if w.get("closure_reason") else "")
            writer.writerow([w["date"], label, "", "", "", "", "", "", ""])
        else:
            date_label = w["date"]
            if w.get("closed"):
                date_label += " (Closed" + (f" — {w['closure_reason']}" if w.get("closure_reason") else "") + ", but visited anyway)"
            writer.writerow([date_label, w["new"], w["attendance"], w["people_fed"], w["family"],
                              w["senior"], w["child_0_3"], w["child_4_12"], w["child_13_18"]])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=grant_report.csv"},
    )


@bp.route("/export/households.csv")
@login_required
def export_households():
    rows = db.list_all_households()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Household Code", "First Name", "Last Name", "Phone", "Household Size", "Status",
         "ID Verified", "Needs Diapers", "Needs Formula", "Registered"]
    )
    for r in rows:
        status = db.household_status(r["member_count"])
        writer.writerow(
            [r["household_code"], r["primary_first_name"], r["primary_last_name"], r["phone"] or "",
             r["member_count"], f"{status['code']} - {status['label']}",
             "Yes" if r["id_verified"] else "No", "Yes" if r["needs_diapers"] else "No",
             "Yes" if r["needs_formula"] else "No", r["created_at"]]
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=households.csv"},
    )


@bp.route("/export/visits.csv")
@login_required
def export_visits():
    start_date = request.args.get("start_date", "").strip() or None
    end_date = request.args.get("end_date", "").strip() or None
    rows = db.list_visits(start_date, end_date)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Visit Date", "Household Code", "Primary Name", "Household Size", "Checked In By"])
    for r in rows:
        writer.writerow(
            [r["visit_date"], r["household_code"], r["primary_name"], r["member_count"],
             r["checked_in_by"] or ""]
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=visits.csv"},
    )


@bp.route("/backup")
@login_required
def backup():
    return render_template("admin/backup.html")


@bp.route("/backup/export")
@login_required
def backup_export():
    filename = f"pantry-backup-{datetime.date.today().isoformat()}.db"
    return send_file(db.DB_PATH, as_attachment=True, download_name=filename, mimetype="application/x-sqlite3")


@bp.route("/backup/import", methods=["POST"])
@login_required
def backup_import():
    uploaded = request.files.get("db_file")
    if not uploaded or not uploaded.filename:
        flash("Please choose a backup file to import.", "error")
        return redirect(url_for("admin.backup"))

    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    uploaded.save(tmp_path)

    try:
        backup_path = db.restore_database(tmp_path)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.backup"))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    flash(
        f"Database restored from the uploaded backup. Your previous data was saved to "
        f"{backup_path} first, just in case.",
        "success",
    )
    return redirect(url_for("admin.backup"))
