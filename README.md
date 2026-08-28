# The Neighbourhood Pantry Tracker

A small, free, fully local web app for the pantry: household registration, volunteer
check-in, and an admin master database — replacing the old Google Form / Google Sheet
combo (and its `#REF` errors).

Everything runs on one church computer. No internet connection, no monthly cost, no
accounts to pay for. Volunteers use it from their own tablets/phones over the church WiFi;
nothing leaves the building.

## For the admin: everyday use (after the one-time build below)

1. Double-click **`Pantry Tracker.app`** (in the `dist` folder, or wherever you've moved it —
   e.g. your Applications folder or Desktop).
2. The first time only, macOS will refuse to open it ("Apple could not verify..."). Fix this
   once:
   - Right-click (or Control-click) the app icon → **Open**.
   - A dialog appears — click **Open** again.
   - After this first time, double-clicking normally always works.
3. A browser window opens automatically to a **"Pantry Tracker Is Running"** screen with big
   buttons for Check-In, Register, and Admin, plus the network address to give volunteer
   devices.
4. This app has **no Dock icon and no window of its own** — that's intentional, not a bug.
   Instead, look for a small basket icon in the **menu bar at the top of the screen**. Click it any
   time to reopen the main screen or see the volunteer address again. Leave it running while
   the pantry is open.
5. To stop the server, click the basket menu bar icon and choose **Quit Pantry Tracker**.

> If you ever see the app's icon bouncing endlessly in the Dock and never settling, that's an
> old/rebuilt copy from before this version — replace it with the current `Pantry Tracker.app`,
> which only lives in the menu bar and never does this.

### Setting up a volunteer's tablet or phone (one-time per device)

1. Make sure the device is on the same WiFi as the church computer running the app.
2. Open Safari (or Chrome) and go to the address shown on the "Pantry Tracker Is Running"
   screen — prefer the `.local` address if one is shown (e.g. `http://mymac.local:5050/checkin`)
   over the plain numeric one. See below for why.
3. Tap the Share icon → **Add to Home Screen**. This creates an icon that opens straight to
   check-in — no browser bar, no typing, just tap the icon.

### Why the `.local` address, and what to do if a device won't use it

Volunteer devices are bookmarked to this Mac's network address. A plain numeric address (like
`192.168.1.74`) can silently change any time the Mac reconnects to WiFi, which would quietly
break every volunteer's bookmark — fixing that normally means logging into the church's router,
which is more setup than this really needs.

Instead, the "Pantry Tracker Is Running" screen shows a **`.local` address** (e.g.
`http://mymac.local:5050/checkin`) built into every Mac automatically. It keeps working even
when the numeric address underneath it changes — nothing to configure, no router login, no
password needed. Just use it instead of the numeric one when bookmarking volunteer devices.

This works out of the box on iPhones/iPads and the great majority of Android tablets. On the
rare device where it doesn't load, just use the plain numeric address shown right underneath
it on that same screen for that one device instead — everything else about setup is identical.

### Backing up your data

All data lives in one file:
`~/Library/Application Support/PantryTracker/pantry.db`

To back up, just copy that file somewhere safe (an external drive, a cloud-synced folder,
etc.) periodically. To restore, copy a backup back into that location (with the app closed).

## First-time build (one person, one time)

You need a Mac with Python 3 installed (macOS ships with one). From this project folder:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pyinstaller pantry.spec
```

This produces `dist/Pantry Tracker.app`. Move it to `/Applications` or the Desktop, and share
it with the church computer (AirDrop, USB drive, etc. — copying it this way does **not** count
as "downloaded from the internet," but if you do email it or download it from somewhere, the
one-time Gatekeeper right-click-Open step above will be needed).

You only need to repeat this build step if the app's code changes in the future.

## Running from source (for development, not needed day-to-day)

```bash
source venv/bin/activate
python run.py
```

Then visit `http://127.0.0.1:5050`. Port 5000 is avoided by default because macOS's AirPlay
Receiver commonly occupies it; override with `PANTRY_PORT=<port> python run.py` if 5050 is
also taken.

## How access is separated

- **`/register`** and **`/checkin`** — open to anyone on the church WiFi, no login. This is
  the volunteer/intake surface; volunteers never see the master database.
- **`/admin`** — password protected. First visit creates the one admin account (do this once,
  right after building the app, before handing it to volunteers). This is the only place
  full household records, visit history, and CSV exports for grant applications live.
- **`/host`** — the "Pantry Tracker Is Running" launch screen. Only reachable from the
  computer actually running the server (not over WiFi), so it doesn't show up for volunteers
  browsing the network.

## Data model notes

- Every member gets a permanent ID code (`M-00001`, ...) assigned automatically at
  registration. Household codes are a plain sequential number (`1001`, `1002`, ...) with no
  prefix, continuing from whatever the highest existing number is.
- Household size is never typed in manually — it's simply the count of members entered,
  which is what eliminated the old spreadsheet's `#REF` mismatches.
- A household can only be checked in once per calendar week (Mon–Sun); a second attempt shows
  a clear "already picked up on \<date\>" message instead of logging a duplicate visit.
- Every household shows a color-coded size badge, computed live from the member count (never
  stored, so it can't drift out of sync): 🟡 Single (1), 🔵 Couple (2), 🔴 Family of 3-4,
  🟢 Family of 5+. Visible everywhere a household appears — registration, check-in, admin.
- A household can optionally name an **Authorized Pickup Designate** at registration (or later,
  in Admin > Households) — someone other than a household member allowed to pick up on their
  behalf, e.g. a caregiver. That name shows right alongside the household members at check-in
  so a volunteer can confirm the person in front of them is actually authorized.
- Each timeslot is capped at **30 households**. Full slots are skipped when assigning
  preferences; if all 3 of a household's choices are full, it falls back to whichever active
  slot has room. If literally everything is full, the household is left unassigned with a
  message that an admin will follow up — check Admin > Timeslots to see current fill levels.
- Admin > Households can permanently delete a member or an entire household (with all its
  visit history) — look for the "Remove" buttons and the "Danger Zone" on a household's page.
- Whenever the admin opens the Dashboard, the app checks (at most once every 7 days) for
  households that haven't picked up in 6+ months and emails a digest to the "Admin
  Notification Email" set on the Email Settings page. Requires SMTP to already be configured;
  fails silently and retries next time if sending doesn't work.
- **Admin > Reports** has the stats used for grant applications: unique individuals served,
  new households/individuals by month, and children/senior age breakdowns (0-3, 4-12, 13-17,
  65+), each with a CSV download.
- Registration has three checkboxes: **Government ID Checked**, **Diapers Needed**, and
  **Formula Needed**. There's no free-text dietary/allergy field anymore — it was replaced by
  these tickboxes. Diaper/formula needs show as a highlighted "Also Provide" / "Don't Forget"
  callout on the check-in screen, both before and after confirming pickup, so a volunteer
  can't miss handing those items over along with the food.
- The "Other Household Members" and "Authorized Pickup Designate" sections on the registration
  form are collapsible (tap the section title to expand/collapse) to keep the form shorter.
  Household Members starts open since most households have more than one person; Designate
  starts closed since most registrations won't have one.

## Weekly pickup timeslots

1. On `/admin/timeslots`, set up the weekly schedule (e.g. "Wednesday 10-11am", "Saturday
   9-10am"). Each is a recurring weekly slot, not a one-off date.
2. At registration, a household ranks up to 3 preferred slots. The app assigns them to
   whichever of their 3 choices currently has the fewest other households assigned — this
   naturally spreads households out over time as more register. The current load per slot is
   always visible on `/admin/timeslots`.
3. Deactivating a slot removes it from future registrations but keeps existing households
   assigned to it (their history isn't disturbed). Deleting is only allowed once nothing is
   assigned to that slot anymore.
4. An admin can always manually move a household to a different slot from that household's
   page under Admin > Households.

## Email confirmations

Households can optionally give an email address at registration; if they do, they're
immediately sent their household code and assigned pickup time. This requires SMTP
credentials, which nothing in this app can generate for you — set them up once on
`/admin/settings`:

1. Use (or create) a Gmail account for the pantry.
2. Turn on 2-Step Verification on that Google account, then generate an **App Password** at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) — a
   16-character code, not your normal Gmail password.
3. Enter that address and app password on `/admin/settings`, and send yourself a test email to
   confirm it works.

If email isn't configured yet, or a household leaves the field blank, or sending fails for any
reason (bad credentials, no internet that moment), **registration still succeeds** — the
household code and assigned timeslot are always shown on-screen too, so nobody is blocked by
email trouble.
