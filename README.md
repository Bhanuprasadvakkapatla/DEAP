# KTx-CONNECT — Patient/Caregiver + Clinician Full-Stack Demo

**Synthetic hackathon prototype only. Not for clinical use.**

## Start the application

Requires Python 3.9+; no external packages or internet connection.

From the project folder:

```bash
python server.py
```

On Mac/Linux you may need `python3 server.py` instead.

Open both views in the same browser or different browsers on the same computer:

- Clinician: http://localhost:8000/clinician.html
- Patient/caregiver: http://localhost:8000/patient.html

Do **not** open `patient.html` by double-clicking it (`file://`): the shared backend requires the server.

### Fresh demo data: `--reset`

The 12 demo patients are created with recent check-in times, so after about a day they turn yellow and after about two days red. To start over with fresh times:

```bash
python server.py --reset
```

This deletes the demo database (`deap_demo.sqlite3`, or the file named in `DEAP_DB`) and re-creates it with fresh demo data (12 demo patients plus the 3 demo families, 15 in all), then starts the server. It prints a message when it does. All patients added during earlier demos are removed.

### Open the patient app on a phone: `DEAP_HOST`

By default the server only accepts connections from this computer (`127.0.0.1`). To let a phone on the **same Wi-Fi** open it, set `DEAP_HOST` to `0.0.0.0`:

```powershell
# Windows PowerShell
$env:DEAP_HOST="0.0.0.0"; python server.py
```

```bash
# Mac/Linux
DEAP_HOST=0.0.0.0 python3 server.py
```

On start, the server prints the address to open on the phone, for example `http://192.168.1.20:8000/patient.html`. Windows may ask once whether to allow Python through the firewall; allow it for private networks. When the server is started this way, the invite links shown on the clinician page use that network address even if the clinician page itself was opened at `localhost`. When it is not, the page says phones cannot open the links yet. There is no login, so anyone on the same network can open the demo: use fictional data only, and leave `DEAP_HOST` unset when you do not need phone access.

## Architecture

- `patient.html` — patient/caregiver app with backend synchronization and a read-only care-team note.
- `clinician.html` — clinician roster, attention queue, patient details, reporting, and care-team follow-up notes.
- `server.py` — Python standard-library HTTP server + JSON API + SQLite database.
- `deap_demo.sqlite3` — automatically created local database with 15 fictional patient records (12 patients and 3 families).

Patient data is saved to the browser's localStorage and synchronized to the SQLite backend. Clinician changes are fetched by the patient app every five seconds; the clinician portal refreshes every four seconds. Patient identities are anonymous demo IDs stored in the browser; multiple tabs of the same browser profile share the same demo patient identity. Separate browser profiles create separate patients.

### Barrier owners and worklist

Each barrier has a named owner (packet page 5), set in `BARRIER_OWNERS` in `server.py`. This mapping is site-specific; each center can change it. When a patient finishes the 14 BAT items, the server creates one open task per "yes" barrier in the `tasks` table (clinician-owned, separate from the patient's synced state). The clinician **My worklist** view shows open tasks for a chosen role ("Viewing as"). A "yes" to *Ran out of medicine* also lists the patient in the **Attention queue** ("Ran out of medicine. Social worker to follow up.", naming whoever owns it at this center). While that task has no plan, the patient shows as at least "Follow-up due" (yellow); a higher status (red) is never lowered. Both the queue entry and the raised colour clear when a plan is saved for that task.

### Plans and check-backs (MAPS steps 4–5)

For each barrier task the owner agrees **one concrete plan** with the patient (who, what, how, where, when, plus the plan in the patient's own words, which is what the patient sees). Saving sets a check-back **14 days** later. When it is due, the patient is asked "Is your plan working?" and their answer appears on the task with their latest weekly Green/Yellow/Red answer. The owner then marks the plan **Working** (moves to *Keep checking*), **Not working** (plan closed and kept in history; the barrier needs a new plan) or **Adjust** (edited plan, history kept, new 14-day check-back). Plans, due dates and outcomes are clinician-owned and stored in the `tasks` and `plans` tables, never in the patient's synced state; every action is logged in the events table.

- **Demo: jump forward 14 days** (clinician patient detail, demo only) moves that patient's check-back clock forward so check-backs come due. Real time is not changed; audit times stay real.
- `DEAP_DURABILITY_DAYS` (default `30`): days until the next check-back after a plan is marked Working. Durability interval is a demo setting; the clinical team sets the real schedule.

### Families: patient and caregiver separately (must-keep #2)

The clinician enrolls a family (**Enroll family**). Under 10: a caregiver link only (the caregiver is the user). 10 and older: a patient link and a caregiver link. Each link (`patient.html?invite=TOKEN`) is one person with their own answers; two links in the same browser are two people. Each person answers the 14-item BAT on their own; tasks show "Reported by: Patient / Caregiver / Both", and the care team sees where the answers differ.

**Who sees what.** Proposed privacy rules for the demo; the clinical team decides the real rules.

- Care team: sees everything.
- Patient (10+): sees their own answers, own words, plans and check-backs. Never sees the caregiver's checklist answers.
- Caregiver of a patient 10+: sees their own answers and upcoming check-back dates. Does not see the patient's checklist answers, own words, weekly ratings or check-back notes. Sees the patient's plans only if the patient's setting "Share my plans with [caregiver]" is on (default on for Parent leads and Shared, off for Patient leads). The patient can also turn on "Share my weekly check-in with [caregiver]" (default off; shares the Green/Yellow/Red answer and date, not the note).
- Caregiver of a patient under 10: sees everything about the child.
- Plans for barriers only the caregiver reported (the patient has never said Yes to them) are shown to the caregiver under "Plans for you", not to the patient. The patient's link never receives their plan text, barrier name or check-back date. If both said Yes, the patient sees the plan as usual. The care team sees these plans marked "Visible to caregiver only", and the caregiver is always asked "Is your plan working?" about them (the patient cannot be).
- The server enforces these rules: each link's page only receives what that person may see, and family records cannot be read through the open patient endpoint.

**Hand-off of responsibility.** "Who leads" (set at enrollment, changeable in patient detail; every change is logged) decides who gets reminders and "Is your plan working?" check-backs: Parent leads → caregiver; Shared → both; Patient leads → patient only. A caregiver is only asked about plans the patient shares with them. Under 10 the caregiver always gets them. For Patient leads the 24-hour caregiver-notice stage is skipped (clinician review only).

New Spanish text for families (who sees what, sharing, hand-off, the Progress tab, the small headings above screens) is a draft translation for the clinical team to review.

**Progress tab, no scoreboard.** The packet calls streaks that punish a missed day weak. The Progress tab shows one positive number (days you checked in, last 30 days), a calendar that marks check-in days with a ✓ and leaves other days neutral, and "Missed a day? That's okay. Tell us what got in the way." There are no streaks, resetting counters, points or badges. The "I missed a dose" button on Today still records the answer for the care team.

**6-month re-check** (must-keep #1: "every 6 months, or sooner if something changes"). Each person's next 14-item checklist is due 6 months after their last completed one (`DEAP_BAT_MONTHS`, default `6`). When due, Today shows "Time for your 6-month check-in"; Settings has "Something changed? Retake my checklist". A retake is a new assessment (new `batId`); earlier answers are kept in the `bat_history` table. A barrier that is still "yes" keeps its existing task and plan. The clinician sees last and next dates, "Then vs now" (barriers added or resolved), a worklist group "Checklist re-check due", and a demo-only button "Demo: make 6-month re-check due".

**Check in by text reply** (simulated; no real SMS). Patient setting "Check in by text reply (no data needed)" shows a phone-style text thread on Today. `POST /api/sms/inbound` with `{ "token": "<invite token>", "body": "1" }` takes a reply: 1 = Green, 2 = Yellow, 3 = Red for the weekly check-in; anything else gets "Reply 1, 2 or 3". It is shaped like a texting-service webhook so a real SMS provider could be connected later. Replies are stored in their own table (not the patient's state) and the care team sees them exactly like in-app weekly answers, marked "via text reply". Only the patient's own link (or the caregiver of a child under 10) can check in this way. A patient who has not finished the 14 items is shown as "Awaiting assessment" (blue), but a help request or a Yellow/Red reply still lists them in the **Attention queue** with the reason ("Support requested", "Weekly red response via text reply"). It clears the same way as for any patient: mark the request handled, or a newer check-in answer.

**Discreet mode** (patient setting; default on for ages 10–17): the invite preview, reminder previews, the app header and the tab title use neutral words with no "transplant", "medicine", "pills" or "dose" (for example "Hi Maya, your check-in is ready (1 min)."). Inside the app the normal wording stays. Spanish discreet wording is a draft.

**Demo families** (fictional, based on the packet personas; their links are in each patient's detail): **Maya R., 15, Shared**: Maya and her mom Laura both answered; they differ on one item. **Devon P., 20, Patient leads**: own words about the hand-off from Mom; Mom (Denise) has a link but has not answered. **Leo S., 8, Parent leads, Spanish**: his mom Ana is the user (caregiver link only).

### Chart risk signals (demo — no real EHR)

Packet must-keep #6: use risk signals already in the chart. All chart data here is fictional and stored in server tables (`chart_visits`, `labs`), never in the patient's state. Three flags, computed on the server:

- **Levels unsteady**: standard deviation of the last 4 tacrolimus levels above `DEAP_TAC_SD` (default `2.0`). Needs at least 4 draws. The packet says "swing more than 2 SD across 4 draws". We read this as the standard deviation of the last 4 tacrolimus levels being above 2. Confirm with the clinical team. (We use the sample standard deviation.)
- **No clinic visit in over 4 months** (calendar months; `DEAP_VISIT_MONTHS`, default `4`).
- **No labs in over 6 weeks** (`DEAP_LAB_DAYS`, default `42`).

No target range is shown and levels are never called high or low; we were not given those. Chart flags appear only for the care team (patient detail "Chart signals" and the worklist group "Chart flags — for team review", next action "Review at weekly team meeting"); a flag raises the patient's status to at least yellow. The patient and caregiver app never receives chart data. **Add a lab result** and **Add last clinic visit date** (patient detail, collapsed **Demo tools** section, labelled "Demo, no real EHR") add fictional chart data so a flag can appear or clear live.

### Center setup ("What if we have no psychologist?")

The owner mapping is site specific (packet page 5). In **Center setup** the clinician ticks which of the 4 owner roles are on staff; for any role not on staff, its tasks go to another role on staff or to "Outside partner (e.g., regional telehealth)". The setup is stored on the server (`center_roles`). New and existing open tasks follow it, each routed task says why (e.g. "Routed to Social worker (no psychologist on staff)"), and every change is logged. Default: all 4 roles on staff.

### API

- `GET /api/health`
- `GET /api/patients`
- `GET /api/patients/{id}`
- `PUT /api/patients/{id}` JSON `{ "state": {...} }`
- `POST /api/patients/{id}/care-plan` JSON `{ "carePlan": "..." }`
- `GET /api/tasks` — follow-up tasks (one per "yes" barrier per assessment) with current plan, plan history and patient check-back answer, plus the list of owner roles
- `POST /api/patients/{id}/labs` JSON `{ "date": "YYYY-MM-DD", "level": 7.2 }` — demo only: add a fictional tacrolimus result
- `POST /api/tasks/{id}/plan` JSON `{ "who", "what", "how", "where", "when", "patientText" }` — agree a plan
- `POST /api/tasks/{id}/outcome` JSON `{ "outcome": "working" | "not_working" | "adjust" }` (adjust also takes the plan fields)
- `POST /api/patients/{id}/requests` JSON `{ "action": "handled" }` — clinician marks open support requests handled (stored on the server, outside the patient's state)

### Demonstrate

1. Start the server and open both views in two tabs.
2. Complete patient onboarding with fictional names and barrier choices.
3. See the new patient in clinician roster.
4. Submit a Red/Yellow check-in or contact request; see the attention queue update.
5. Save a care-team follow-up note; see it appear in the patient Today view.

### Safety and limitations

No authentication, authorization, transport security, auditing, HIPAA compliance, real SMS, EHR integration, or verified clinical assessment content. Do not deploy publicly or use real patient data. Barriers use the 14-item Barriers Assessment Tool (BAT) from the Hackathon Builder Packet, as a Yes/No checklist, plus "Something else" in the patient's own words. Spanish BAT wording is a draft translation; replace with the clinical team's validated Spanish BAT. "What might help" options depend on the patient's biggest barrier and come only from the packet's action plans (page 5); barriers without a self-help list show a care-team message instead. If the biggest problem is "Something else" (no packet action plan), the patient sees: "Thanks for telling us in your own words. Your transplant coordinator will read it and talk with you." The Spanish wording of these options and messages, including this one, is also a draft translation for the clinical team to review. The check-back question and answers ("Is your plan working?") have draft Spanish wording too. Status rules are illustrative workflow rules, not clinical risk scores. Clinician notes are demo messages, not medical instructions. Reset demo data by stopping the server and running `python server.py --reset`.

## Overdue Check-in & Escalation — engagement safety net (new)

This is a **simulation of non-response**, not an unconsciousness detector, missed-dose detector, or emergency monitoring system. The system does not know whether medication was taken. An unconscious/unresponsive person needs emergency services (911 in the US), not a response from this application.

- **24 hours** after last recorded check-in activity: a *simulated* caregiver notification appears in the clinician record. **No SMS or push is sent.**
- **48 hours**: the patient is flagged for clinician review in the attention queue. **No external clinician alert is sent.**
- Clinicians can **acknowledge** the flag, **mark it resolved**, and review a basic **event history**.
- Patient check-in activity resets the non-response stage. A passive page visit does not count as a check-in.
- Demo controls in clinician patient details: **Simulate 25h**, **Simulate 49h**, **Reset timer**. Demo offsets do not change actual system time.
- These durations are **illustrative hackathon values**, not approved medical escalation rules. The clinical team must determine thresholds, consent, authorized caregiver access, escalation ownership, alert delivery, and escalation response time before any real implementation.

### New API endpoints

- `POST /api/patients/{id}/simulate` with `{"hours":0|25|49}`
- `POST /api/patients/{id}/escalation` with `{"action":"acknowledge"|"resolve"}`
- `GET /api/patients/{id}/escalation` returns demo audit events

Runs locally only (`127.0.0.1:8000`) unless you set `DEAP_HOST=0.0.0.0` for phone testing on the same network. The local SQLite file is generated on first run and should not be committed to Git.
