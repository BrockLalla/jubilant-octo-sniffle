"""Sends registration confirmation emails over SMTP.

Credentials are entered by the admin on /admin/settings and stored via
db.get_setting/set_setting — nothing is hardcoded here. A failure to send
(bad credentials, no internet, etc.) always raises so callers can catch it
and flash a warning rather than losing the registration itself.
"""
import html
import re
import smtplib
import ssl
from email.message import EmailMessage
from string import Template

import db

SETTING_KEYS = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_name"]

# The registration email's subject/body are editable (with bold/italic/lists)
# on Admin > Email Settings and stored as HTML. These are the starting point
# shown there, and the fallback if the church hasn't customized them yet.
# $primary_name / $household_code / $timeslot are filled in per-household;
# Template.safe_substitute is used (not str.format) so a stray "{" or "$" a
# non-technical admin types while editing can never raise and break sending.
DEFAULT_EMAIL_SUBJECT = "Your Community Pantry Registration"
DEFAULT_EMAIL_BODY = (
    "<p>Hi $primary_name,</p>"
    "<p>Thanks for registering with the community pantry. Here's what to keep for your records:</p>"
    "<ul>"
    "<li><strong>Household code:</strong> $household_code</li>"
    "<li><strong>Your assigned pickup time:</strong> $timeslot</li>"
    "</ul>"
    "<p>Please come by during that weekly time slot to pick up your food. "
    "If you have any questions, just ask a volunteer at the pantry.</p>"
    "<p>Thank you,<br>Community Pantry Team</p>"
)


class EmailNotConfigured(Exception):
    pass


def _get_config():
    cfg = db.get_settings_dict(SETTING_KEYS)
    if not cfg.get("smtp_host") or not cfg.get("smtp_username") or not cfg.get("smtp_password"):
        raise EmailNotConfigured("Email settings haven't been filled in yet (Admin > Email Settings).")
    cfg["smtp_port"] = int(cfg.get("smtp_port") or 587)
    cfg["from_name"] = cfg.get("from_name") or "Community Pantry"
    return cfg


def ensure_html_body(content):
    """The registration email body used to be saved as plain text (before
    the rich-text editor existed). Loading an old plain-text save into the
    HTML editor as-is would collapse every blank line into one squished
    paragraph -- so a value with no tags in it gets migrated to real <p>
    markup first, once, the first time it's read."""
    if "<" in content and ">" in content:
        return content
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [p.strip() for p in normalized.split("\n\n") if p.strip()]
    return "".join(
        f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs
    )


def _html_to_text(content):
    """Plain-text fallback for email clients that don't render HTML --
    intentionally simple (tag stripping, not a real HTML parser) since the
    input is always the admin's own saved template, not untrusted markup."""
    text = re.sub(r"(?is)<li[^>]*>", "  - ", content)
    text = re.sub(r"(?is)</(p|div|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _send(to_email, subject, body, html_body=None):
    cfg = _get_config()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg['from_name']} <{cfg['smtp_username']}>"
    msg["To"] = to_email
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if cfg["smtp_port"] == 465:
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=context, timeout=15) as server:
            server.login(cfg["smtp_username"], cfg["smtp_password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=15) as server:
            server.starttls(context=context)
            server.login(cfg["smtp_username"], cfg["smtp_password"])
            server.send_message(msg)


def render_registration_email(subject_template, body_template, household_code, primary_name, timeslot_label):
    """Shared by the real send and the Email Settings preview, so what the
    admin previews is exactly what goes out. body_template is HTML (the
    editor saves bold/italic/lists as markup); this also derives the
    plain-text fallback part for clients that don't render HTML."""
    fields = {
        "primary_name": primary_name,
        "household_code": household_code,
        "timeslot": timeslot_label or "not yet assigned — a volunteer will follow up",
    }
    subject = Template(subject_template).safe_substitute(fields)
    html_body = Template(body_template).safe_substitute(fields)
    text_body = _html_to_text(html_body)
    return subject, text_body, html_body


def send_registration_email(to_email, household_code, primary_name, timeslot_label):
    cfg = db.get_settings_dict(["email_subject", "email_body"])
    subject, text_body, html_body = render_registration_email(
        cfg.get("email_subject") or DEFAULT_EMAIL_SUBJECT,
        ensure_html_body(cfg.get("email_body") or DEFAULT_EMAIL_BODY),
        household_code, primary_name, timeslot_label,
    )
    _send(to_email, subject, text_body, html_body=html_body)


def send_test_registration_email(to_email, subject_template, body_template):
    """Sends the actual drafted registration email, filled in with sample
    data, so the admin can see exactly what a household would receive."""
    subject, text_body, html_body = render_registration_email(
        subject_template, body_template,
        household_code="1234", primary_name="Sample Household",
        timeslot_label="Wednesday, 10:00 AM – 11:00 AM",
    )
    _send(to_email, f"[TEST] {subject}", text_body, html_body=html_body)


def send_stale_households_alert(to_email, stale_households):
    subject = f"Pantry Tracker: {len(stale_households)} household(s) haven't visited in 6+ months"
    lines = [
        f"The following {len(stale_households)} household(s) haven't picked up in over 6 months "
        f"(or have never picked up since registering):\n",
    ]
    for h in stale_households:
        last = f"last visit {h['last_visit_display']}" if h.get("last_visit_display") else \
            f"never visited, registered {h['created_at'][:10]}"
        phone = f", {h['phone']}" if h.get("phone") else ""
        lines.append(f"  {h['household_code']} — {h['primary_name']}{phone} ({last})")
    lines.append("\nYou may want to follow up, or remove them from the system if no longer needed.")
    _send(to_email, subject, "\n".join(lines))


def send_admin_invite_email(to_email, invite_link, invited_by):
    subject = "You've been invited to The Neighbourhood Pantry Tracker"
    body = (
        f"{invited_by or 'An admin'} has invited you to create an admin account for "
        f"The Neighbourhood Pantry Tracker.\n\n"
        f"Follow this link to set your password and finish creating your account:\n"
        f"{invite_link}\n\n"
        f"This link works once and expires in {db.INVITE_VALID_HOURS} hours. If you weren't expecting "
        f"this, you can safely ignore it."
    )
    _send(to_email, subject, body)


def send_password_reset_email(to_email, reset_link):
    subject = "Reset your Pantry Tracker admin password"
    body = (
        f"Someone (hopefully you) requested a password reset for the admin account on "
        f"The Neighbourhood Pantry Tracker.\n\n"
        f"Follow this link to set a new password:\n"
        f"{reset_link}\n\n"
        f"This link works once and expires in {db.RESET_VALID_HOURS} hour(s). If you didn't request this, "
        f"you can safely ignore it -- your password won't be changed."
    )
    _send(to_email, subject, body)
