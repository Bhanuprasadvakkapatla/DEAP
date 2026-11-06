#!/usr/bin/env python3
"""KTx-CONNECT local-only synthetic demo. NOT clinical monitoring."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote
from pathlib import Path
from datetime import datetime, timezone, timedelta, date
import json, sqlite3, threading, re, os, argparse, socket, statistics, calendar, secrets
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("DEAP_DB", str(ROOT / "deap_demo.sqlite3")))
# 127.0.0.1 = this computer only. 0.0.0.0 = also phones on the same network.
HOST = os.environ.get("DEAP_HOST", "127.0.0.1")
PORT = 8000
CAREGIVER_HOURS = float(os.environ.get("DEAP_CAREGIVER_HOURS", "24"))
CLINICIAN_HOURS = float(os.environ.get("DEAP_CLINICIAN_HOURS", "48"))
if not 0 < CAREGIVER_HOURS < CLINICIAN_HOURS:
    raise ValueError("Escalation thresholds must increase and be positive")
LOCK = threading.RLock()
# Barriers Assessment Tool (BAT): the 14 official items, in packet order.
BAT_KEYS = (
    "forget",
    "taste",
    "swallow",
    "side",
    "embarrass",
    "noneed",
    "toomany",
    "notwork",
    "confusing",
    "ranout",
    "afford",
    "activities",
    "refuse",
    "inconvenient",
)
# Allowed barrier codes: the 14 BAT items plus free-text "Something else".
BARRIER_KEYS = BAT_KEYS + ("other",)

# Who follows up on each barrier (packet page 5).
# Site-specific; each center can change this.
BARRIER_OWNERS = {
    "forget": "Transplant coordinator",
    "taste": "Transplant coordinator",
    "swallow": "Transplant coordinator",
    "side": "Transplant coordinator",
    "embarrass": "Transplant coordinator",
    "noneed": "Transplant coordinator",
    "toomany": "Nephrologist / NP / Pharmacist",
    "notwork": "Nephrologist / NP / Pharmacist",
    "confusing": "Nephrologist / NP / Pharmacist",
    "ranout": "Social worker",
    "afford": "Social worker",
    "activities": "Psychologist",
    "refuse": "Psychologist",
    "inconvenient": "Psychologist",
    "other": "Transplant coordinator",  # to review and re-route
}
OWNER_ROLES = list(dict.fromkeys(BARRIER_OWNERS.values()))
# Center setup (packet judge question 5; page 5: the owner mapping is site specific). A role that is
# not on staff sends its tasks to another role on staff, or to an outside partner.
OUTSIDE_PARTNER = "Outside partner (e.g., regional telehealth)"


def center_setup(c):
    """{role: {"onStaff": bool, "routeTo": str|None}}. Default: every role on staff."""
    rows = {r["role"]: r for r in c.execute("SELECT * FROM center_roles")}
    return {
        role: {
            "onStaff": bool(rows[role]["on_staff"]) if role in rows else True,
            "routeTo": rows[role]["route_to"] if role in rows else None,
        }
        for role in OWNER_ROLES
    }


def route_role(setup, home):
    """Who handles tasks for this packet role at this center, and why (None if not re-routed)."""
    s = setup.get(home, {"onStaff": True})
    if s["onStaff"] or not s.get("routeTo"):
        return home, None
    return s["routeTo"], f"Routed to {s['routeTo']} (no {home[0].lower() + home[1:]} on staff)"
# Task status: needs_plan -> plan_agreed -> keep_checking (MAPS steps 4-5, packet page 4).
TASK_STATUSES = ("needs_plan", "plan_agreed", "keep_checking")
# The owner checks back within 2 weeks of agreeing a plan (packet page 4).
CHECK_BACK_DAYS = 14
# Durability interval is a demo setting; the clinical team sets the real schedule.
# Days until the next check-back once the owner marks a plan "Working" (keep checking).
DURABILITY_DAYS = int(os.environ.get("DEAP_DURABILITY_DAYS", "30"))
if DURABILITY_DAYS < 1:
    raise ValueError("DEAP_DURABILITY_DAYS must be at least 1")
OUTCOME_NAMES = {"working": "Working", "not_working": "Not working", "adjust": "Adjust"}

# The 14-item checklist is repeated every 6 months, or sooner if something changes (packet page 4).
BAT_MONTHS = int(os.environ.get("DEAP_BAT_MONTHS", "6"))
if BAT_MONTHS < 1:
    raise ValueError("DEAP_BAT_MONTHS must be at least 1")

# Families (packet page 4, must-keep #2): caregiver only under 10; patient and caregiver from 10.
# "Who leads" decides who gets reminders and check-backs (hand-off of responsibility).
LEADS_NAMES = {"parent": "Parent leads", "shared": "Shared", "patient": "Patient leads"}


def default_leads(age):
    return "parent" if age < 10 else "shared" if age < 18 else "patient"


# Chart risk signals (packet page 4, must-keep #6). Demo settings; the clinical team sets real ones.
# The packet says "swing more than 2 SD across 4 draws". We read this as the standard deviation
# of the last 4 tacrolimus levels being above 2. Confirm with the clinical team.
TAC_SD = float(os.environ.get("DEAP_TAC_SD", "2.0"))
TAC_DRAWS = 4
VISIT_MONTHS = int(os.environ.get("DEAP_VISIT_MONTHS", "4"))  # calendar months
LAB_DAYS = int(os.environ.get("DEAP_LAB_DAYS", "42"))  # 6 weeks
if TAC_SD <= 0 or VISIT_MONTHS < 1 or LAB_DAYS < 1:
    raise ValueError("Chart flag settings must be positive")
# The five parts of a concrete plan, plus the version the patient sees.
PLAN_FIELDS = ("who", "what", "how", "where", "when", "patientText")
# Patient check-back answers ("Is your plan working?"), stored in the patient's own state.
CHECK_ANSWERS = {
    "working": "Yes, it's working",
    "not_really": "Not really",
    "change": "I want to change it",
}
NOW = lambda: datetime.now(timezone.utc)


def iso(d=None):
    return (d or NOW()).isoformat().replace("+00:00", "Z")


def conn():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def db():
    """Open a connection; commit on success, roll back on error, always close.

    Handlers build their reply inside this block and send it after it ends,
    so every write is committed before the response goes out.
    """
    c = conn()
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with db() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS patients (id TEXT PRIMARY KEY, state TEXT NOT NULL, care_plan TEXT DEFAULT '', updated_at TEXT NOT NULL, last_engagement TEXT NOT NULL, demo_offset_hours INTEGER DEFAULT 0)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS escalation (patient_id TEXT PRIMARY KEY, stage TEXT NOT NULL DEFAULT 'none', acknowledged INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, FOREIGN KEY(patient_id) REFERENCES patients(id))"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, at TEXT NOT NULL)"
        )
        # Clinician-owned count of support requests already handled. Kept outside the
        # patient's state because the patient page overwrites state on every sync.
        cols = [r["name"] for r in c.execute("PRAGMA table_info(patients)")]
        if "handled_requests" not in cols:
            c.execute(
                "ALTER TABLE patients ADD COLUMN handled_requests INTEGER NOT NULL DEFAULT 0"
            )
        # Demo-only: days this patient's check-back clock is moved forward. Real time is unchanged.
        if "plan_clock_days" not in cols:
            c.execute(
                "ALTER TABLE patients ADD COLUMN plan_clock_days INTEGER NOT NULL DEFAULT 0"
            )
        # Clinician-owned follow-up tasks: one per "yes" barrier per assessment.
        # due_date is the next check-back. The plan text lives in the plans table.
        c.execute(
            "CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, assessment TEXT NOT NULL, barrier TEXT NOT NULL, owner_role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'needs_plan', created_at TEXT NOT NULL, plan TEXT, due_date TEXT, outcome TEXT, UNIQUE(patient_id, assessment, barrier))"
        )
        c.execute("UPDATE tasks SET status='needs_plan' WHERE status='open'")
        # home_role: the packet owner for the barrier; owner_role: who handles it at this center.
        if "home_role" not in [r["name"] for r in c.execute("PRAGMA table_info(tasks)")]:
            c.execute("ALTER TABLE tasks ADD COLUMN home_role TEXT")
        for r in c.execute("SELECT id, barrier FROM tasks WHERE home_role IS NULL").fetchall():
            c.execute("UPDATE tasks SET home_role=? WHERE id=?", (BARRIER_OWNERS.get(r["barrier"], ""), r["id"]))
        # Completed follow-ups (owner chose Working / Not working / Adjust), with the due date
        # they answered, so reports can show on time vs late. done_at uses the patient's demo clock.
        c.execute(
            "CREATE TABLE IF NOT EXISTS followups (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, patient_id TEXT NOT NULL, barrier TEXT NOT NULL, due_date TEXT, done_at TEXT NOT NULL, outcome TEXT NOT NULL)"
        )
        # Center setup: which owner roles are on staff and where the others' tasks go.
        c.execute(
            "CREATE TABLE IF NOT EXISTS center_roles (role TEXT PRIMARY KEY, on_staff INTEGER NOT NULL DEFAULT 1, route_to TEXT)"
        )
        # Clinician-owned plans, one row per version. The open row (closed_at IS NULL) is
        # the current plan; closed rows keep the history with their outcome.
        c.execute(
            "CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, who TEXT NOT NULL, what TEXT NOT NULL, how TEXT NOT NULL, where_text TEXT NOT NULL, when_text TEXT NOT NULL, patient_text TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, closed_at TEXT, outcome TEXT, FOREIGN KEY(task_id) REFERENCES tasks(id))"
        )
        # Families and personal links. Clinician-owned; separate from every person's state.
        c.execute(
            "CREATE TABLE IF NOT EXISTS families (id TEXT PRIMARY KEY, age INTEGER NOT NULL, leads TEXT NOT NULL, caregiver_name TEXT NOT NULL, created_at TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'en', FOREIGN KEY(id) REFERENCES patients(id))"
        )
        if "lang" not in [r["name"] for r in c.execute("PRAGMA table_info(families)")]:
            c.execute("ALTER TABLE families ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'")
        c.execute(
            "CREATE TABLE IF NOT EXISTS invites (token TEXT PRIMARY KEY, family_id TEXT NOT NULL, person TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(family_id) REFERENCES families(id))"
        )
        # The caregiver's own answers for a patient aged 10+ (the patient's live in patients.state).
        c.execute(
            "CREATE TABLE IF NOT EXISTS caregiver_states (family_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(family_id) REFERENCES families(id))"
        )
        # Every completed checklist (each person, each assessment), so old answers are kept.
        c.execute(
            "CREATE TABLE IF NOT EXISTS bat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, person TEXT NOT NULL, bat_id TEXT NOT NULL, completed_at TEXT NOT NULL, answers TEXT NOT NULL, barriers TEXT NOT NULL, own_words TEXT NOT NULL DEFAULT '', UNIQUE(patient_id, person, bat_id))"
        )
        # Demo only: days added to "now" for the 6-month re-check. Real time is unchanged.
        if "bat_clock_days" not in [r["name"] for r in c.execute("PRAGMA table_info(patients)")]:
            c.execute("ALTER TABLE patients ADD COLUMN bat_clock_days INTEGER NOT NULL DEFAULT 0")
        # Weekly check-ins sent by text reply (simulated SMS). Kept apart from the patient's state.
        c.execute(
            "CREATE TABLE IF NOT EXISTS text_replies (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, person TEXT NOT NULL, body TEXT NOT NULL, status TEXT, reply TEXT NOT NULL, at TEXT NOT NULL)"
        )
        fresh_seed = c.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
        if fresh_seed:
            # Fictional demo patients: (name, BAT "yes" barriers with the biggest first, weekly status)
            sample = [
                ("Alex", ["toomany"], "yellow"),
                ("Jordan", ["forget"], "green"),
                ("Taylor", ["refuse"], "red"),
                ("Sam", ["swallow", "taste"], "green"),
                ("Riley", ["side"], "yellow"),
                ("Morgan", ["embarrass", "forget"], "green"),
                ("Casey", ["ranout"], "yellow"),
                ("Avery", ["taste"], "green"),
                ("Jamie", ["afford", "ranout"], "red"),
                ("Cameron", ["activities", "inconvenient"], "green"),
                ("Drew", ["confusing", "notwork"], "green"),
                ("Parker", ["noneed"], "yellow"),
            ]
            # Ideas the demo patients ticked. Barriers that only get a care-team message have none.
            seed_ideas = {
                "forget": ["reminder", "routine"],
                "taste": ["taste_flavoring"],
                "swallow": ["swallow_candy"],
                "embarrass": ["embarrass_discreet"],
            }
            for i, (name, yes, status) in enumerate(sample):
                barrier = yes[0]
                state = {
                    "patientName": name,
                    "patientLastInitial": chr(65 + i),
                    "role": "caregiver" if i % 4 == 0 else "self",
                    "caregiverName": "Maria" if i % 4 == 0 else "",
                    "lang": "en",
                    "channel": "SMS",
                    "time": "07:00",
                    "primary": barrier,
                    "barriers": [k for k in BARRIER_KEYS if k in yes],
                    "batAnswers": {k: "yes" if k in yes else "no" for k in BAT_KEYS},
                    "batId": iso(NOW() - timedelta(days=3)),
                    "strategies": list(seed_ideas.get(barrier, [])),
                    "daily": [
                        {
                            "date": (NOW() - timedelta(days=j)).date().isoformat(),
                            "status": "reported_taken",
                        }
                        for j in range(1, 4)
                    ],
                    "weekly": [
                        {
                            "date": iso(NOW() - timedelta(days=2)),
                            "status": status,
                            "note": "",
                        }
                    ],
                    "contactRequests": 0,
                    "silence": False,
                }
                c.execute(
                    "INSERT INTO patients(id,state,care_plan,updated_at,last_engagement,demo_offset_hours) VALUES (?,?,?,?,?,?)",
                    (
                        f"demo-{i+1:02}",
                        json.dumps(state),
                        "",
                        iso(),
                        iso(NOW() - timedelta(hours=2)),
                        0,
                    ),
                )
            seed_families(c)
        # Create tasks for seed patients and for databases made before the tasks table existed.
        for row in c.execute("SELECT id, state FROM patients").fetchall():
            sync_tasks(c, row["id"], json.loads(row["state"]))
        if fresh_seed:
            seed_plans(c)
        # Record checklists finished before the history table existed (and the seed ones).
        for row in c.execute("SELECT id, state, updated_at FROM patients").fetchall():
            record_bat(c, row["id"], owner_person(c, row["id"]), json.loads(row["state"]), backfill=True)
        for row in c.execute("SELECT family_id, state FROM caregiver_states").fetchall():
            record_bat(c, row["family_id"], "caregiver", json.loads(row["state"]), backfill=True)
        # Chart data (demo only, no real EHR). Chart-owned: never part of the patient's state.
        c.execute(
            "CREATE TABLE IF NOT EXISTS chart_visits (patient_id TEXT PRIMARY KEY, last_visit TEXT NOT NULL, FOREIGN KEY(patient_id) REFERENCES patients(id))"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS labs (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, drawn_on TEXT NOT NULL, tacrolimus REAL NOT NULL, source TEXT NOT NULL DEFAULT 'demo seed', FOREIGN KEY(patient_id) REFERENCES patients(id))"
        )
        if c.execute("SELECT COUNT(*) FROM labs").fetchone()[0] == 0:
            seed_chart(c)


def create_family(c, first, initial, age, lang, caregiver, leads=None):
    """Enroll a family: one patient record plus a personal link per person. Returns the family id."""
    fid = "fam-" + secrets.token_hex(6)
    state = {
        "patientName": first,
        "patientLastInitial": initial,
        "caregiverName": caregiver,
        # Under 10 the caregiver is the user and answers in this record.
        "role": "caregiver" if age < 10 else "self",
        "lang": lang,
        "barriers": [],
        "batAnswers": {},
        "strategies": [],
        "daily": [],
        "weekly": [],
        "contactRequests": 0,
    }
    c.execute(
        "INSERT INTO patients(id,state,care_plan,updated_at,last_engagement,demo_offset_hours) VALUES (?,?,?,?,?,0)",
        (fid, json.dumps(state), "", iso(), iso()),
    )
    c.execute(
        "INSERT INTO families(id,age,leads,caregiver_name,created_at,lang) VALUES (?,?,?,?,?,?)",
        (fid, age, leads or default_leads(age), caregiver, iso(), lang),
    )
    for person in (["caregiver"] if age < 10 else ["patient", "caregiver"]):
        c.execute(
            "INSERT INTO invites(token,family_id,person,created_at) VALUES (?,?,?,?)",
            (secrets.token_urlsafe(12), fid, person, iso()),
        )
    event(c, fid, "enrolled", f"Family enrolled (age {age}, {LEADS_NAMES[leads or default_leads(age)]}); personal links created")
    return fid


def reports(c):
    """Measures that show whether the loop works (packet page 6), from the server tables.
    Fictional demo data only; no clinical outcome is claimed."""
    active = c.execute(
        "SELECT patient_id, barrier, owner_role, status, due_date FROM tasks WHERE status IN ('needs_plan','plan_agreed','keep_checking')"
    ).fetchall()
    resolved = sum(t["status"] == "keep_checking" for t in active)
    # Follow-ups: completed on or before the due day vs late; and those still overdue now.
    on_time = late = 0
    for f in c.execute("SELECT due_date, done_at FROM followups").fetchall():
        if f["due_date"] and f["done_at"][:10] > f["due_date"][:10]:
            late += 1
        else:
            on_time += 1
    overdue = 0
    for t in active:
        if t["status"] != "needs_plan" and t["due_date"]:
            today = (NOW() + timedelta(days=clock_days(c, t["patient_id"]))).date().isoformat()
            overdue += t["due_date"][:10] < today
    events = c.execute("SELECT message FROM events WHERE kind='check_back'").fetchall()
    by_patient = sum(e["message"].startswith("Patient") for e in events)
    by_caregiver = sum(e["message"].startswith("Caregiver") for e in events)
    texts = c.execute("SELECT COUNT(*) FROM text_replies WHERE status IS NOT NULL").fetchone()[0]
    # Drug-level steadiness from the chart (spread of the last 4 tacrolimus levels).
    steady = unsteady = no_data = 0
    for r in c.execute("SELECT id FROM patients").fetchall():
        sp = chart_signals(c, r["id"])["spread"]
        if sp is None:
            no_data += 1
        elif sp <= TAC_SD:
            steady += 1
        else:
            unsteady += 1
    owners = {}
    for t in active:
        o = owners.setdefault(t["owner_role"], {"needs_plan": 0, "plan_agreed": 0, "keep_checking": 0})
        o[t["status"]] += 1
    reported = {}
    for r in c.execute("SELECT state FROM patients").fetchall():
        for b in json.loads(r["state"]).get("barriers", []):
            reported[b] = reported.get(b, 0) + 1
    return {
        "barriers": {"total": len(active), "resolved": resolved},
        "followups": {"onTime": on_time, "late": late, "overdue": overdue},
        "noCall": {"patientCheckbacks": by_patient, "caregiverCheckbacks": by_caregiver, "textReplies": texts},
        "levels": {"steady": steady, "unsteady": unsteady, "noData": no_data, "threshold": TAC_SD},
        "ownerWork": owners,
        "reported": reported,
    }


def save_center(c, data):
    """Save which roles are on staff and where the others' tasks go; re-route open tasks."""
    given = {r.get("role"): r for r in data.get("roles", []) if isinstance(r, dict)}
    if set(given) != set(OWNER_ROLES):
        return {"error": "Send all four owner roles"}, 400
    on = {role for role in OWNER_ROLES if given[role].get("onStaff") is True}
    for role in OWNER_ROLES:
        if role not in on:
            to = given[role].get("routeTo")
            if to != OUTSIDE_PARTNER and to not in on:
                return {"error": f"Choose where {role} tasks go: a role on staff or an outside partner"}, 400
    before = center_setup(c)
    for role in OWNER_ROLES:
        c.execute(
            "INSERT INTO center_roles(role,on_staff,route_to) VALUES (?,?,?) ON CONFLICT(role) DO UPDATE SET on_staff=excluded.on_staff, route_to=excluded.route_to",
            (role, int(role in on), None if role in on else given[role]["routeTo"]),
        )
    setup = center_setup(c)
    changed = [r for r in OWNER_ROLES if before[r] != setup[r]]
    if changed:
        event(c, "center", "center_setup", "Center setup changed: " + "; ".join(
            f"{r}: {'on staff' if setup[r]['onStaff'] else 'not on staff, tasks go to ' + setup[r]['routeTo']}" for r in changed))
    # Re-route every open task to who handles it now.
    moved = 0
    for t in c.execute("SELECT id, patient_id, barrier, owner_role, home_role FROM tasks WHERE status IN ('needs_plan','plan_agreed','keep_checking')").fetchall():
        home = t["home_role"] or BARRIER_OWNERS.get(t["barrier"], "")
        owner, why = route_role(setup, home)
        if owner != t["owner_role"]:
            c.execute("UPDATE tasks SET owner_role=? WHERE id=?", (owner, t["id"]))
            event(c, t["patient_id"], "rerouted", f"Task for '{t['barrier']}' re-routed from {t['owner_role']} to {owner} (center setup{': ' + why if why else ''})")
            moved += 1
    return {"center": setup, "rerouted": moved}, 200


def owner_person(c, pid):
    """Who answers in the patient record: the caregiver for a child under 10, else the patient."""
    f = c.execute("SELECT age FROM families WHERE id=?", (pid,)).fetchone()
    return "caregiver" if f and f["age"] < 10 else "patient"


def record_bat(c, pid, person, st, backfill=False):
    """Keep each completed checklist (one row per assessment). Answers changed during the same
    assessment update that row; a new batId is a new assessment."""
    if not bat_complete(st):
        return
    bat_id = st.get("batId") or "initial"
    row = c.execute(
        "SELECT id FROM bat_history WHERE patient_id=? AND person=? AND bat_id=?", (pid, person, bat_id)
    ).fetchone()
    answers = json.dumps(st.get("batAnswers") or {})
    barriers = json.dumps([b for b in BARRIER_KEYS if b in st.get("barriers", [])])
    # The person's "Something else" words for this assessment, exactly as written.
    words = st.get("other", "") if "other" in st.get("barriers", []) else ""
    if row:
        c.execute("UPDATE bat_history SET answers=?, barriers=?, own_words=? WHERE id=?", (answers, barriers, words, row["id"]))
        return
    when = iso()
    if backfill:
        try:
            when = iso(datetime.fromisoformat(bat_id.replace("Z", "+00:00")))
        except ValueError:
            pass
    c.execute(
        "INSERT INTO bat_history(patient_id,person,bat_id,completed_at,answers,barriers,own_words) VALUES (?,?,?,?,?,?,?)",
        (pid, person, bat_id, when, answers, barriers, words),
    )
    if not backfill:
        # A finished checklist starts a new 6-month period (and clears the demo jump).
        c.execute("UPDATE patients SET bat_clock_days=0 WHERE id=?", (pid,))
        event(c, pid, "checklist", f"{person.capitalize()} completed the 14-item checklist")


def recheck_info(c, pid, person):
    """Last completed checklist, next due date, and then-vs-now for one person."""
    hist = c.execute(
        "SELECT completed_at, barriers FROM bat_history WHERE patient_id=? AND person=? ORDER BY completed_at, id",
        (pid, person),
    ).fetchall()
    if not hist:
        return None
    clock = c.execute("SELECT bat_clock_days FROM patients WHERE id=?", (pid,)).fetchone()["bat_clock_days"]
    last = datetime.fromisoformat(hist[-1]["completed_at"].replace("Z", "+00:00"))
    due = add_months(last.date(), BAT_MONTHS)
    info = {
        "person": person,
        "lastDone": hist[-1]["completed_at"],
        "nextDue": due.isoformat(),
        "due": (NOW() + timedelta(days=clock)).date() >= due,
        "count": len(hist),
    }
    if len(hist) >= 2:
        then, now = set(json.loads(hist[-2]["barriers"])), set(json.loads(hist[-1]["barriers"]))
        info["added"] = [b for b in BARRIER_KEYS if b in now - then]
        info["resolved"] = [b for b in BARRIER_KEYS if b in then - now]
    return info


def family_rechecks(c, pid):
    """Re-check info for each person who answers for this patient."""
    out = [recheck_info(c, pid, owner_person(c, pid))]
    f = c.execute("SELECT age FROM families WHERE id=?", (pid,)).fetchone()
    if f and f["age"] >= 10:
        out.append(recheck_info(c, pid, "caregiver"))
    return [x for x in out if x]


# Text replies for the weekly check-in: 1 = Green, 2 = Yellow, 3 = Red.
TEXT_STATUS = {"1": "green", "2": "yellow", "3": "red"}
TEXT_REPLIES = {
    "en": {"ok": "Thanks, got it.", "help": "Reply 1, 2 or 3"},
    "es": {"ok": "Gracias, recibido.", "help": "Responde 1, 2 o 3"},
}


def weekly_answers(c, pid, st):
    """All weekly answers for this patient record: in-app ones plus text replies, oldest first.
    The care team treats a text reply exactly like an in-app answer (marked via text)."""
    rows = c.execute(
        "SELECT status, at FROM text_replies WHERE patient_id=? AND status IS NOT NULL", (pid,)
    ).fetchall()
    texts = [{"date": r["at"], "status": r["status"], "note": "", "via": "text"} for r in rows]
    return sorted((st.get("weekly") or []) + texts, key=lambda w: w.get("date", ""))


def text_inbound(c, token, body):
    """One inbound text. Returns (reply, http status)."""
    inv = invite_row(c, token)
    if not inv:
        return {"error": "Unknown sender"}, 404
    pid = inv["family_id"]
    main = json.loads(c.execute("SELECT state FROM patients WHERE id=?", (pid,)).fetchone()["state"])
    words = TEXT_REPLIES["es" if main.get("lang") == "es" else "en"]
    if not owns_record(inv):
        # The weekly check-in belongs to the patient's own link.
        return {"error": "Text check-ins are only for the patient's own link"}, 403
    status = TEXT_STATUS.get(str(body).strip())
    reply = words["ok"] if status else words["help"]
    c.execute(
        "INSERT INTO text_replies(patient_id,person,body,status,reply,at) VALUES (?,?,?,?,?,?)",
        (pid, inv["person"], str(body)[:160], status, reply, iso()),
    )
    if status:
        # A text reply counts as check-in activity, just like an in-app weekly answer.
        c.execute("UPDATE patients SET last_engagement=?, demo_offset_hours=0 WHERE id=?", (iso(), pid))
        record_activity(c, pid)
        event(c, pid, "weekly", f"Weekly check-in via text reply: {status.capitalize()}")
    return {"reply": reply, "status": status}, 200


def is_family(c, pid):
    return c.execute("SELECT 1 FROM families WHERE id=?", (pid,)).fetchone() is not None


def record_activity(c, pid):
    """New check-in activity ends any non-response episode."""
    er = c.execute("SELECT * FROM escalation WHERE patient_id=?", (pid,)).fetchone()
    if er and er["stage"] != "none":
        c.execute(
            "UPDATE escalation SET stage='none',acknowledged=0,note='New patient activity recorded',updated_at=? WHERE patient_id=?",
            (iso(), pid),
        )
        event(c, pid, "activity", "Patient activity ended the previous non-response episode")


def save_main_state(c, pid, state):
    """Save the state of the person who owns this patient record. Returns the row."""
    existing = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    old = json.loads(existing["state"]) if existing else {}
    activity = not existing or any(
        state.get(k) != old.get(k) for k in ("daily", "weekly", "contactRequests", "checkbacks")
    )
    when = iso() if activity else existing["last_engagement"]
    c.execute(
        "INSERT INTO patients(id,state,care_plan,updated_at,last_engagement,demo_offset_hours) VALUES (?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at,last_engagement=excluded.last_engagement,demo_offset_hours=CASE WHEN ? THEN 0 ELSE patients.demo_offset_hours END",
        (pid, json.dumps(state), "", iso(), when, int(activity)),
    )
    if activity:
        record_activity(c, pid)
    if any(state.get(k) != old.get(k) for k in ("barriers", "batAnswers", "batId")):
        sync_tasks(c, pid, state)
        record_bat(c, pid, owner_person(c, pid), state)
    log_checkbacks(c, pid, old.get("checkbacks") or {}, state.get("checkbacks") or {})
    row = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    recalc(c, row)
    return row


def save_caregiver_state(c, fid, state):
    """Save the caregiver's own state (patient aged 10+). Never touches the patient's state."""
    old = caregiver_state(c, fid) or {}
    c.execute(
        "INSERT INTO caregiver_states(family_id,state,updated_at) VALUES (?,?,?) ON CONFLICT(family_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at",
        (fid, json.dumps(state), iso()),
    )
    if any(state.get(k) != old.get(k) for k in ("barriers", "batAnswers", "batId")):
        main = json.loads(c.execute("SELECT state FROM patients WHERE id=?", (fid,)).fetchone()["state"])
        sync_tasks(c, fid, main)
        record_bat(c, fid, "caregiver", state)
    log_checkbacks(c, fid, old.get("checkbacks") or {}, state.get("checkbacks") or {}, "Caregiver")
    # When the caregiver helps lead, their check-ins count as activity for the family.
    leads = c.execute("SELECT leads FROM families WHERE id=?", (fid,)).fetchone()["leads"]
    if leads != "patient" and any(
        state.get(k) != old.get(k) for k in ("daily", "weekly", "contactRequests", "checkbacks")
    ):
        c.execute(
            "UPDATE patients SET last_engagement=?, demo_offset_hours=0 WHERE id=?", (iso(), fid)
        )
        record_activity(c, fid)


def invite_row(c, token):
    return c.execute(
        "SELECT i.person, i.family_id, f.age, f.leads, f.caregiver_name, f.lang FROM invites i JOIN families f ON f.id=i.family_id WHERE i.token=?",
        (token,),
    ).fetchone()


def owns_record(inv):
    """The patient (10+) or the caregiver of a child under 10 owns the family's patient record."""
    return inv["person"] == "patient" or inv["age"] < 10


def invite_view(c, inv):
    """What one person's link may receive. The server decides; the page cannot ask for more."""
    prow = c.execute("SELECT * FROM patients WHERE id=?", (inv["family_id"],)).fetchone()
    main = json.loads(prow["state"])
    who = {
        "person": inv["person"],
        "patientName": main.get("patientName", ""),
        "patientLastInitial": main.get("patientLastInitial", ""),
        "caregiverName": inv["caregiver_name"],
        "age": inv["age"],
        "lang": inv["lang"],
        "leads": inv["leads"],
    }
    sharing = family_sharing(main, inv["leads"])
    who["sharing"] = sharing
    # Who leads decides who gets reminders and "Is your plan working?" (hand-off of responsibility).
    leads = inv["leads"]
    if inv["age"] < 10:
        gets = True  # under 10 the caregiver is the user
    elif inv["person"] == "patient":
        gets = leads in ("shared", "patient")
    else:
        # A caregiver can only be asked about plans the patient shares with them.
        gets = leads in ("parent", "shared") and sharing["plans"]
    who["getsReminders"] = inv["age"] < 10 or (
        leads in ("shared", "patient") if inv["person"] == "patient" else leads in ("parent", "shared")
    )
    who["getsCheckbacks"] = gets
    # This person's own 6-month re-check.
    who["recheck"] = recheck_info(c, inv["family_id"], owner_person(c, inv["family_id"]) if owns_record(inv) else "caregiver")
    if owns_record(inv):
        # ownState: this person's own saved answers, so a new device can pick them up.
        return {"invite": who, "patient": derive(prow, c), "ownState": main}
    # Caregiver of a patient aged 10+ (proposed demo rules): never the patient's checklist
    # answers, own words, weekly ratings or check-back notes. Plans and the weekly colour only
    # when the patient shares them; check-back dates always.
    plans = patient_plans(c, inv["family_id"], main)
    weekly = (weekly_answers(c, inv["family_id"], main) or [None])[-1]
    return {
        "invite": who,
        "ownState": caregiver_state(c, inv["family_id"]),
        "patient": {
            "plans": plans if sharing["plans"] else [],
            "checkbackDates": sorted(p["dueDate"] for p in plans if p["dueDate"]),
            "sharedWeekly": {"status": weekly["status"], "date": weekly["date"]} if sharing["weekly"] and weekly else None,
            "carePlan": "",
            "clockOffsetDays": prow["plan_clock_days"],
        },
    }


def family_sharing(main, leads):
    """What the patient (10+) shares with the caregiver. Patient's own setting, or the default."""
    sp, sw = main.get("sharePlans"), main.get("shareWeekly")
    return {
        "plans": sp if isinstance(sp, bool) else leads != "patient",
        "weekly": sw if isinstance(sw, bool) else False,
    }


# Fictional families based on the packet personas (packet pages 2 and 4).
# (first, last initial, age, language, caregiver, who leads, patient's own answers, caregiver's own answers)
SEED_FAMILIES = [
    ("Maya", "R", 15, "en", "Laura", "shared",
     {"yes": ["forget", "embarrass", "other"], "other": "I forget on weekends and at sleepovers", "weekly": "green"},
     {"yes": ["forget"], "weekly": "green"}),
    ("Jordan", "P", 20, "en", "Denise", "patient",
     {"yes": ["ranout", "afford", "other"], "other": "Mom used to handle refills and insurance", "weekly": "yellow"},
     None),  # caregiver link exists; Mom has not answered
    ("Leo", "S", 8, "es", "Ana", "parent",
     None,  # under 10: no patient link
     {"yes": ["taste", "swallow"], "weekly": "green"}),
]


def seed_answers(first, initial, caregiver, lang, role, a):
    yes = a["yes"]
    st = {
        "patientName": first,
        "patientLastInitial": initial,
        "caregiverName": caregiver,
        "role": role,
        "lang": lang,
        "view": "today",
        "step": 4,
        "barriers": [k for k in BARRIER_KEYS if k in yes],
        "primary": yes[0],
        "batAnswers": {**{k: "yes" if k in yes else "no" for k in BAT_KEYS}, **({"other": "yes"} if "other" in yes else {})},
        "batId": iso(NOW() - timedelta(days=4)),
        "other": a.get("other", ""),
        "strategies": [],
        "daily": [{"date": (NOW() - timedelta(days=j)).date().isoformat(), "status": "reported_taken"} for j in range(1, 3)],
        "weekly": [{"date": iso(NOW() - timedelta(days=2)), "status": a["weekly"], "note": ""}],
        "contactRequests": 0,
    }
    return st


def seed_families(c):
    for first, initial, age, lang, cg, leads, pt, cga in SEED_FAMILIES:
        fid = create_family(c, first, initial, age, lang, cg, leads)
        if age < 10:
            # The caregiver is the user: their answers are the child's record.
            c.execute("UPDATE patients SET state=? WHERE id=?", (json.dumps(seed_answers(first, initial, cg, lang, "caregiver", cga)), fid))
            continue
        c.execute("UPDATE patients SET state=? WHERE id=?", (json.dumps(seed_answers(first, initial, cg, lang, "self", pt)), fid))
        if cga:
            c.execute(
                "INSERT INTO caregiver_states(family_id,state,updated_at) VALUES (?,?,?)",
                (fid, json.dumps(seed_answers(first, initial, cg, lang, "caregiver", cga)), iso()),
            )


def family_info(c, pid):
    """Family details for the care team, or None for a walk-in demo patient."""
    f = c.execute("SELECT * FROM families WHERE id=?", (pid,)).fetchone()
    if not f:
        return None
    links = c.execute(
        "SELECT person, token FROM invites WHERE family_id=? ORDER BY person DESC", (pid,)
    ).fetchall()
    return {
        "age": f["age"],
        "leads": f["leads"],
        "caregiverName": f["caregiver_name"],
        "links": [{"person": l["person"], "token": l["token"]} for l in links],
    }


# Fictional chart data for the demo patients (no real EHR). Levels are tacrolimus in ng/mL.
# Each: (patient, days since last clinic visit, [(days ago drawn, level), ...] newest first).
SEED_CHART = {
    "demo-01": (40, [(56, 7.2), (70, 6.9), (84, 7.3), (98, 7.0)]),  # Alex: no labs in over 6 weeks
    "demo-02": (21, [(9, 6.6), (23, 6.9), (37, 6.4), (51, 6.8)]),
    "demo-03": (35, [(12, 8.1), (26, 7.8), (40, 8.3), (54, 7.9)]),
    "demo-04": (18, [(5, 7.4), (19, 7.1), (33, 7.6), (47, 7.2), (61, 7.5)]),
    "demo-05": (28, [(6, 10.4), (20, 6.0), (34, 9.8), (48, 5.2)]),  # Riley: levels unsteady
    "demo-06": (50, [(14, 6.2), (28, 6.5), (42, 6.1), (56, 6.4)]),
    "demo-07": (33, [(10, 7.7), (24, 7.3), (38, 7.9), (52, 7.5)]),
    "demo-08": (25, [(8, 6.9), (22, 7.2), (36, 6.7), (50, 7.0)]),
    "demo-09": (60, [(15, 8.4), (29, 8.0), (43, 8.6), (57, 8.2)]),
    "demo-10": (14, [(3, 7.0), (17, 7.3), (31, 6.8), (45, 7.1)]),
    "demo-11": (30, [(7, 9.9), (21, 5.8), (35, 10.2), (49, 5.4)]),  # Drew: weekly Green, levels unsteady
    "demo-12": (152, [(11, 7.5), (25, 7.2), (39, 7.6), (53, 7.3)]),  # Parker: no clinic visit in over 4 months
}


def add_months(d, n):
    """Same day n calendar months later (clamped to the month's last day)."""
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def chart_signals(c, pid):
    """Chart risk signals for one patient (clinician-only). No target range; never high/low."""
    today = NOW().date()
    v = c.execute("SELECT last_visit FROM chart_visits WHERE patient_id=?", (pid,)).fetchone()
    draws = c.execute(
        "SELECT drawn_on, tacrolimus FROM labs WHERE patient_id=? ORDER BY drawn_on DESC, id DESC",
        (pid,),
    ).fetchall()
    recent = [{"date": d["drawn_on"], "level": d["tacrolimus"]} for d in draws[:TAC_DRAWS]]
    # Sample standard deviation of the last 4 levels; needs at least 4 draws.
    spread = (
        round(statistics.stdev(x["level"] for x in recent), 2)
        if len(recent) == TAC_DRAWS
        else None
    )
    last_visit = v["last_visit"] if v else None
    last_lab = draws[0]["drawn_on"] if draws else None
    flags = []
    if spread is not None and spread > TAC_SD:
        flags.append({"kind": "unsteady", "text": f"Levels unsteady: spread {spread:.1f} across last {TAC_DRAWS} draws"})
    if last_visit and today > add_months(date.fromisoformat(last_visit), VISIT_MONTHS):
        flags.append({"kind": "no_visit", "text": f"No clinic visit in over {VISIT_MONTHS} months"})
    if last_lab and (today - date.fromisoformat(last_lab)).days > LAB_DAYS:
        span = f"{LAB_DAYS // 7} weeks" if LAB_DAYS % 7 == 0 else f"{LAB_DAYS} days"
        flags.append({"kind": "no_labs", "text": f"No labs in over {span}"})
    return {
        "hasData": bool(v or draws),
        "lastVisit": last_visit,
        "lastLab": last_lab,
        "recentLevels": recent,
        "spread": spread,
        "flags": flags,
    }


def seed_chart(c):
    today = NOW().date()
    have = {r["id"] for r in c.execute("SELECT id FROM patients")}
    for pid, (visit_ago, draws) in SEED_CHART.items():
        if pid not in have:
            continue
        c.execute(
            "INSERT OR REPLACE INTO chart_visits(patient_id,last_visit) VALUES (?,?)",
            (pid, (today - timedelta(days=visit_ago)).isoformat()),
        )
        for ago, level in draws:
            c.execute(
                "INSERT INTO labs(patient_id,drawn_on,tacrolimus) VALUES (?,?,?)",
                (pid, (today - timedelta(days=ago)).isoformat(), level),
            )


# Fictional demo plans so the seed patients show every stage of the follow-up loop.
# Each: (patient, barrier, days since plan agreed, status, check-back in days, plan).
SEED_PLANS = [
    ("demo-02", "forget", 5, "plan_agreed", 9, {
        "who": "Jordan; a parent is the backup", "what": "Phone or app reminders",
        "how": "Alarm on Jordan's phone labelled 'meds'", "where": "Kitchen, with breakfast",
        "when": "Every morning at 7:00",
        "patientText": "My phone alarm says 'meds' at 7. I take them with breakfast."}),
    ("demo-10", "activities", 3, "plan_agreed", 11, {
        "who": "Cameron, with the psychologist", "what": "Keep medicine with sports gear",
        "how": "A small pouch that stays in the sports bag", "where": "Sports bag",
        "when": "Packed the night before practice",
        "patientText": "My medicine pouch lives in my sports bag, so it comes to practice with me."}),
    ("demo-04", "swallow", 14, "plan_agreed", 0, {
        "who": "Sam, with the transplant coordinator", "what": "Practice swallowing with small candy",
        "how": "Start with tiny candies, then slightly bigger ones", "where": "At home",
        "when": "5 minutes after dinner",
        "patientText": "I practice with small candies for 5 minutes after dinner."}),
    ("demo-11", "confusing", 14, "plan_agreed", 0, {
        "who": "Drew, with the pharmacist", "what": "Go over the medicine instructions together",
        "how": "Pharmacist makes a simple chart of each medicine", "where": "Clinic visit",
        "when": "At the next clinic visit",
        "patientText": "My pharmacist made me a simple chart of my medicines. I keep it on my desk."}),
    ("demo-08", "taste", 14, "plan_agreed", 0, {
        "who": "Avery, with the transplant coordinator", "what": "Numb taste buds with a popsicle first",
        "how": "Keep popsicles in the freezer", "where": "At home", "when": "Right before each dose",
        "patientText": "I eat a popsicle right before my medicine."}),
    ("demo-07", "ranout", 17, "plan_agreed", -3, {
        "who": "Casey, with the social worker", "what": "Refill reminders from the pharmacy",
        "how": "Social worker helps sign up for pharmacy refill texts", "where": "By phone",
        "when": "This week",
        "patientText": "The pharmacy will text me when it is time to refill."}),
    ("demo-06", "embarrass", 20, "keep_checking", 24, {
        "who": "Morgan, with the transplant coordinator", "what": "Carry and take medicine discreetly",
        "how": "A small pill case in the backpack", "where": "School", "when": "At lunch",
        "patientText": "I keep a small pill case in my backpack and take it quietly at lunch."}),
]


def seed_plans(c):
    now = NOW()
    for pid, barrier, ago, status, due_in, f in SEED_PLANS:
        t = c.execute(
            "SELECT id, owner_role FROM tasks WHERE patient_id=? AND barrier=?", (pid, barrier)
        ).fetchone()
        c.execute(
            "INSERT INTO plans(task_id,who,what,how,where_text,when_text,patient_text,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (t["id"], f["who"], f["what"], f["how"], f["where"], f["when"], f["patientText"],
             iso(now - timedelta(days=ago)), t["owner_role"] + " (demo, no login)"),
        )
        c.execute(
            "UPDATE tasks SET status=?, due_date=? WHERE id=?",
            (status, iso(now + timedelta(days=due_in)), t["id"]),
        )
        event(c, pid, "plan_agreed", f"Demo seed: plan for '{barrier}' ({status.replace('_', ' ')})")
    # Avery answered the check-back "Not really". Patient-owned, so it lives in her own state.
    t = c.execute("SELECT id, due_date FROM tasks WHERE patient_id='demo-08' AND barrier='taste'").fetchone()
    row = c.execute("SELECT state FROM patients WHERE id='demo-08'").fetchone()
    st = json.loads(row["state"])
    st["checkbacks"] = {
        str(t["id"]): {
            "answer": "not_really",
            "note": "The popsicle helps a little, but I can still taste it after.",
            "at": iso(now - timedelta(hours=3)),
            "for": t["due_date"],
        }
    }
    c.execute("UPDATE patients SET state=? WHERE id='demo-08'", (json.dumps(st),))
    log_checkbacks(c, "demo-08", {}, st["checkbacks"])
    # Morgan's plan was checked back on its due day and marked Working (now Keep checking).
    m = c.execute("SELECT id FROM tasks WHERE patient_id='demo-06' AND barrier='embarrass'").fetchone()
    on_day = iso(now - timedelta(days=6))
    c.execute(
        "INSERT INTO followups(task_id,patient_id,barrier,due_date,done_at,outcome) VALUES (?,?,?,?,?,'working')",
        (m["id"], "demo-06", "embarrass", on_day, on_day),
    )


def event(c, pid, kind, message):
    c.execute(
        "INSERT INTO events(patient_id,kind,message,at) VALUES (?,?,?,?)",
        (pid, kind, message, iso()),
    )


def bat_complete(st):
    return all((st.get("batAnswers") or {}).get(k) in ("yes", "no") for k in BAT_KEYS)


def caregiver_state(c, fid):
    r = c.execute("SELECT state FROM caregiver_states WHERE family_id=?", (fid,)).fetchone()
    return json.loads(r["state"]) if r else None


def reported_by(c, pid, st, barrier):
    """Who said "yes" to this barrier: Patient, Caregiver or Both."""
    fam = c.execute("SELECT age FROM families WHERE id=?", (pid,)).fetchone()
    if not fam:
        return "Caregiver" if st.get("role") == "caregiver" else "Patient"
    if fam["age"] < 10:
        return "Caregiver"  # under 10 the caregiver answers for the child
    cs = caregiver_state(c, pid) or {}
    p = bat_complete(st) and barrier in st.get("barriers", [])
    g = bat_complete(cs) and barrier in cs.get("barriers", [])
    return "Both" if p and g else "Caregiver" if g else "Patient"


def answer_compare(c, pid, st):
    """Patient vs caregiver on the 14 BAT items (age 10+), for the care team only."""
    fam = c.execute("SELECT age FROM families WHERE id=?", (pid,)).fetchone()
    if not fam or fam["age"] < 10:
        return None
    cs = caregiver_state(c, pid) or {}
    pa, ca = st.get("batAnswers") or {}, cs.get("batAnswers") or {}
    both = bat_complete(st) and bat_complete(cs)
    return {
        "patientDone": bat_complete(st),
        "caregiverDone": bat_complete(cs),
        "patient": {k: pa.get(k) for k in BAT_KEYS},
        "caregiver": {k: ca.get(k) for k in BAT_KEYS},
        # Only meaningful once both have answered all 14 items.
        "differ": [k for k in BAT_KEYS if pa.get(k) != ca.get(k)] if both else [],
        "caregiverWords": cs.get("other", "") if "other" in cs.get("barriers", []) else "",
    }


def sync_tasks(c, pid, state):
    """Keep one active task per "yes" barrier.

    Runs only once all 14 BAT items are answered. A re-check (new assessment) carries on
    the existing task for a barrier that is still "yes" instead of adding a second one.
    A barrier that is no longer "yes" loses its task only while it has no plan yet.
    """
    fam = c.execute("SELECT age FROM families WHERE id=?", (pid,)).fetchone()
    # Each person who has finished the 14 items counts. For a family: one task per barrier.
    voices = [v for v in [state, caregiver_state(c, pid) if fam and fam["age"] >= 10 else None] if v and bat_complete(v)]
    if not voices:
        return
    # New tasks are labelled with the newest assessment among the voices.
    assessment = max((v.get("batId") or "initial") for v in voices)
    said = {b for v in voices for b in v.get("barriers", [])}
    yes = [b for b in BARRIER_OWNERS if b in said]
    existing = {
        r["barrier"]: r
        for r in c.execute(
            "SELECT id, barrier, status FROM tasks WHERE patient_id=? AND status IN ('needs_plan','plan_agreed','keep_checking')",
            (pid,),
        )
    }
    setup = center_setup(c)
    for b in yes:
        if b not in existing:
            owner, _ = route_role(setup, BARRIER_OWNERS[b])
            c.execute(
                "INSERT INTO tasks(patient_id,assessment,barrier,owner_role,home_role,status,created_at) VALUES (?,?,?,?,?,'needs_plan',?)",
                (pid, assessment, b, owner, BARRIER_OWNERS[b], iso()),
            )
            event(c, pid, "task_created", f"Task for {owner}: barrier '{b}'")
    for b, r in existing.items():
        if b not in yes and r["status"] == "needs_plan":
            c.execute("DELETE FROM tasks WHERE id=?", (r["id"],))
            event(c, pid, "task_removed", f"Patient changed '{b}' to No; open task removed")


def log_checkbacks(c, pid, old, new, who="Patient"):
    """Record each new check-back answer, so history is kept when a later one replaces it."""
    for k, v in new.items():
        if old.get(k) == v:
            continue
        t = c.execute(
            "SELECT barrier FROM tasks WHERE id=? AND patient_id=?", (int(k), pid)
        ).fetchone()
        if not t:
            continue
        note = v.get("note", "")
        event(
            c,
            pid,
            "check_back",
            f"{who} check-back for '{t['barrier']}': {CHECK_ANSWERS[v['answer']]}"
            + (f' ({who.lower()} wrote: "{note}")' if note.strip() else ""),
        )


def plan_view(p):
    return {
        "id": p["id"],
        "who": p["who"],
        "what": p["what"],
        "how": p["how"],
        "where": p["where_text"],
        "when": p["when_text"],
        "patientText": p["patient_text"],
        "createdAt": p["created_at"],
        "createdBy": p["created_by"],
        "closedAt": p["closed_at"],
        "outcome": p["outcome"],
    }


def clock_days(c, pid):
    """Demo-only days added to this patient's check-back clock."""
    r = c.execute("SELECT plan_clock_days FROM patients WHERE id=?", (pid,)).fetchone()
    return r["plan_clock_days"] if r else 0


def task_view(c, t, st):
    """One task as the clinician sees it: the current plan, plan history and patient context."""
    plans = c.execute(
        "SELECT * FROM plans WHERE task_id=? ORDER BY id", (t["id"],)
    ).fetchall()
    current = next((p for p in plans if p["closed_at"] is None), None)
    return {
        "id": t["id"],
        "patientId": t["patient_id"],
        "patientName": st.get("patientName", ""),
        "patientLastInitial": st.get("patientLastInitial", ""),
        "barrier": t["barrier"],
        "ownerRole": t["owner_role"],
        "homeRole": t["home_role"] or BARRIER_OWNERS.get(t["barrier"], ""),
        # Why this task is not with its usual (packet) owner, from the center setup.
        "routedReason": route_role(center_setup(c), t["home_role"] or BARRIER_OWNERS.get(t["barrier"], ""))[1]
        if t["owner_role"] != (t["home_role"] or BARRIER_OWNERS.get(t["barrier"], ""))
        else None,
        "status": t["status"],
        "createdAt": t["created_at"],
        "dueDate": t["due_date"],
        "clockOffsetDays": clock_days(c, t["patient_id"]),
        "isPrimary": st.get("primary") == t["barrier"],
        "reportedBy": reported_by(c, t["patient_id"], st, t["barrier"]),
        "plan": plan_view(current) if current else None,
        "planHistory": [plan_view(p) for p in plans if p["closed_at"] is not None],
        # The patient's latest check-back answer for this task, exactly as they wrote it.
        "patientAnswer": (st.get("checkbacks") or {}).get(str(t["id"])),
        # The caregiver's own check-back answer (patient 10+, when they are asked), exactly as written.
        "caregiverAnswer": ((caregiver_state(c, t["patient_id"]) or {}).get("checkbacks") or {}).get(str(t["id"])),
        "latestWeekly": (weekly_answers(c, t["patient_id"], st) or [None])[-1],
        # Patient context shown next to the plan form (read-only).
        "strategies": st.get("strategies", []),
        # The patient's own words, exactly as written ("Something else").
        "ownWords": st.get("other", "") if "other" in st.get("barriers", []) else "",
    }


def patient_plans(c, pid, state):
    """Agreed plans the patient sees, in their own words; the biggest-problem one first."""
    rows = c.execute(
        "SELECT t.id, t.barrier, t.status, t.due_date, p.patient_text FROM tasks t JOIN plans p ON p.task_id = t.id AND p.closed_at IS NULL WHERE t.patient_id=? AND t.status IN ('plan_agreed','keep_checking') ORDER BY t.id",
        (pid,),
    ).fetchall()
    plans = [
        {
            "taskId": r["id"],
            "barrier": r["barrier"],
            "status": r["status"],
            "dueDate": r["due_date"],
            "patientText": r["patient_text"],
            "isPrimary": r["barrier"] == state.get("primary"),
        }
        for r in rows
    ]
    return sorted(plans, key=lambda p: not p["isPrimary"])


def clean_plan(data):
    """Validate the five plan parts and the patient's version. Returns (fields, error)."""
    fields = {}
    for k in PLAN_FIELDS:
        v = data.get(k)
        if not isinstance(v, str) or not v.strip() or len(v) > 400:
            return None, f"Please fill in '{k}' (up to 400 characters)"
        fields[k] = v
    return fields, None


def acting_role(data):
    """Who is acting, for the audit trail. There is no login in this demo."""
    by = data.get("by")
    return (by if by in OWNER_ROLES else "Clinician portal") + " (demo, no login)"


def derive(row, c, chart=False):
    """One patient record. chart=True only for the care team: chart signals never go to the patient app."""
    state = json.loads(row["state"])
    pid = row["id"]
    er = c.execute("SELECT * FROM escalation WHERE patient_id=?", (pid,)).fetchone()
    stage = er["stage"] if er else "none"
    reasons = []
    total_requests = state.get("contactRequests") or 0
    handled = min(row["handled_requests"], total_requests)
    open_requests = total_requests - handled
    if open_requests:
        reasons.append("Support requested")
    # "Ran out of medicine" = Yes: listed in the Attention queue until its task has a plan (colour unchanged).
    for t in c.execute(
        "SELECT owner_role FROM tasks WHERE patient_id=? AND barrier='ranout' AND status='needs_plan'", (pid,)
    ):
        reasons.append(f"Ran out of medicine. {t['owner_role']} to follow up.")
    if state.get("silence"):
        reasons.append("Demo silence flag")
    if stage in ("caregiver_notice", "clinician_review"):
        reasons.append("Overdue check-in: " + stage.replace("_", " "))
    # In-app weekly answers and text replies count the same way.
    weekly = weekly_answers(c, pid, state)
    if weekly and weekly[-1].get("status") in ("yellow", "red"):
        reasons.append(
            "Weekly " + weekly[-1]["status"] + " response" + (" via text reply" if weekly[-1].get("via") == "text" else "")
        )
    last = weekly
    status = (
        "blue"
        # Awaiting assessment: no barriers yet and the 14 BAT items are not all answered.
        if not state.get("barriers")
        and not all(
            (state.get("batAnswers") or {}).get(k) in ("yes", "no") for k in BAT_KEYS
        )
        else (
            "red"
            if open_requests
            or state.get("silence")
            or (last and last[-1].get("status") == "red")
            else "yellow" if last and last[-1].get("status") == "yellow" else "green"
        )
    )
    # An overdue stage can raise the status but never lower it.
    overdue = {"clinician_review": "red", "caregiver_notice": "yellow"}.get(stage)
    rank = {"blue": 0, "green": 1, "yellow": 2, "red": 3}
    if overdue and rank[overdue] > rank[status]:
        status = overdue
    extra = {}
    if chart:
        signals = chart_signals(c, pid)
        latest = (weekly or [{}])[-1].get("status")
        if signals["flags"]:
            reasons.append("Chart flag for team review")
            # A chart flag raises the status to at least yellow, never lowers it.
            if rank[status] < rank["yellow"]:
                status = "yellow"
        extra = {
            "family": family_info(c, pid),
            "rechecks": family_rechecks(c, pid),
            "answers": answer_compare(c, pid, state),
            "sharing": family_sharing(state, (family_info(c, pid) or {}).get("leads", "")),
            "chart": signals,
            # Latest weekly answer is Green but the chart has a flag.
            "chartMismatch": bool(signals["flags"]) and latest == "green",
        }
    # Start from the patient's state, then let server-owned fields overwrite any key with
    # the same name (a patient state can never fake or break these).
    return {
        **state,
        **extra,
        **dict(
        id=pid,
        attentionReasons=reasons,
        elapsedHours=max(
            0,
            (
                NOW()
                - datetime.fromisoformat(row["last_engagement"].replace("Z", "+00:00"))
            ).total_seconds()
            / 3600
            + row["demo_offset_hours"],
        ),
        carePlan=row["care_plan"],
        plans=patient_plans(c, pid, state),
        clockOffsetDays=row["plan_clock_days"],
        # Who handles each barrier at this center (after any center-setup routing).
        barrierOwners={
            b: route_role(center_setup(c), BARRIER_OWNERS[b])[0]
            for b in state.get("barriers", [])
            if b in BARRIER_OWNERS
        },
        handledRequests=handled,
        openRequests=open_requests,
        status=status,
        escalation={
            "stage": stage,
            "acknowledged": bool(er["acknowledged"]) if er else False,
            "note": er["note"] if er else "",
            "updatedAt": er["updated_at"] if er else None,
        },
        lastEngagement=row["last_engagement"],
        demoOffsetHours=row["demo_offset_hours"],
        # All weekly answers, in-app and by text reply (each text one marked via "text").
        weekly=weekly,
        # The record owner's 6-month re-check (last checklist, next due).
        recheck=recheck_info(c, pid, owner_person(c, pid)),
        textReplies=[
            dict(r)
            for r in c.execute(
                "SELECT body, status, reply, at FROM text_replies WHERE patient_id=? ORDER BY id DESC LIMIT 6",
                (pid,),
            ).fetchall()
        ][::-1],
        ),
    }


def recalc(c, row):
    """Demo-only thresholds: 24h caregiver notice, 48h clinician review. NOT medical guidance."""
    pid = row["id"]
    state = json.loads(row["state"])
    last = datetime.fromisoformat(row["last_engagement"].replace("Z", "+00:00"))
    age = (NOW() - last).total_seconds() / 3600 + row["demo_offset_hours"]
    target = (
        "clinician_review"
        if age >= CLINICIAN_HOURS
        else "caregiver_notice" if age >= CAREGIVER_HOURS else "none"
    )
    # Patient leads: no caregiver notice; only the clinician review stage.
    fam = c.execute("SELECT leads FROM families WHERE id=?", (pid,)).fetchone()
    if fam and fam["leads"] == "patient" and target == "caregiver_notice":
        target = "none"
    old = c.execute("SELECT * FROM escalation WHERE patient_id=?", (pid,)).fetchone()
    if old and old["stage"] == "resolved":
        return
    if target == "none":
        if old and old["stage"] != "none":
            c.execute(
                "UPDATE escalation SET stage=?, acknowledged=0, note=?, updated_at=? WHERE patient_id=?",
                ("none", "New patient activity recorded", iso(), pid),
            )
            event(
                c, pid, "resolved", "New patient activity reset non-response escalation"
            )
    elif not old or old["stage"] != target:
        c.execute(
            "INSERT INTO escalation(patient_id,stage,acknowledged,note,updated_at) VALUES (?,?,0,?,?) ON CONFLICT(patient_id) DO UPDATE SET stage=excluded.stage, acknowledged=0,note=excluded.note,updated_at=excluded.updated_at",
            (pid, target, "Simulated notice only — not delivered", iso()),
        )
        event(
            c,
            pid,
            target,
            (
                "Simulated caregiver notice (not sent)"
                if target == "caregiver_notice"
                else "Clinician review flagged (dashboard only)"
            ),
        )


def validate_state(state):
    for key in (
        "patientName",
        "caregiverName",
        "other",
        "routine",
        "plan",
        "time",
        "channel",
        "tone",
        "lang",
        "role",
        "primary",
        "batId",
    ):
        if key in state and (not isinstance(state[key], str) or len(state[key]) > 1200):
            return "Invalid " + key
    for key in ("barriers", "strategies"):
        if not isinstance(state.get(key, []), list) or any(
            not isinstance(x, str) for x in state.get(key, [])
        ):
            return "Invalid " + key
    barriers = state.get("barriers", [])
    if any(b not in BARRIER_KEYS for b in barriers) or len(set(barriers)) != len(barriers):
        return "Unknown barrier code"
    answers = state.get("batAnswers", {})
    if not isinstance(answers, dict) or any(
        k not in BARRIER_KEYS or v not in ("yes", "no") for k, v in answers.items()
    ):
        return "Invalid batAnswers"
    if state.get("primary") and state["primary"] not in state.get("barriers", []):
        return "Primary barrier must be selected"
    for key in ("daily", "weekly"):
        records = state.get(key, [])
        if not isinstance(records, list) or len(records) > 1000:
            return "Invalid " + key
        for item in records:
            if not isinstance(item, dict) or not isinstance(item.get("date"), str):
                return "Invalid check-in"
            try:
                parsed = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
            except ValueError:
                return "Invalid check-in date"
            allowed = (
                ("reported_taken", "reported_missed")
                if key == "daily"
                else ("green", "yellow", "red")
            )
            if item.get("status") not in allowed:
                return "Invalid check-in status"
            if (
                not isinstance(item.get("note", ""), str)
                or len(item.get("note", "")) > 400
            ):
                return "Invalid comment"
        if key == "daily" and len({x["date"] for x in records}) != len(records):
            return "Duplicate daily check-in"
    if (
        type(state.get("contactRequests", 0)) != int
        or not 0 <= state.get("contactRequests", 0) <= 100000
    ):
        return "Invalid support request count"
    for key in ("silence", "shift", "sharePlans", "shareWeekly", "discreet", "textCheckin"):
        if key in state and type(state[key]) != bool:
            return "Invalid " + key
    checks = state.get("checkbacks", {})
    if not isinstance(checks, dict) or len(checks) > 200:
        return "Invalid checkbacks"
    for k, v in checks.items():
        if (
            not k.isdigit()
            or not isinstance(v, dict)
            or v.get("answer") not in CHECK_ANSWERS
            or not isinstance(v.get("note", ""), str)
            or len(v.get("note", "")) > 400
            or not isinstance(v.get("at", ""), str)
            or not isinstance(v.get("for", ""), str)
        ):
            return "Invalid check-back answer"
    return None


def escalation_worker():
    while True:
        try:
            with LOCK, db() as c:
                for row in c.execute("SELECT * FROM patients").fetchall():
                    recalc(c, row)
        except Exception as exc:
            print("Escalation sweep failed:", exc, flush=True)
        threading.Event().wait(30)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def json(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n < 0 or n > 100000:
            return None
        try:
            return json.loads(self.rfile.read(n))
        except (ValueError, UnicodeError):
            return None

    def route(self):
        path = unquote(urlparse(self.path).path)
        m = re.fullmatch(
            r"/api/patients/([A-Za-z0-9_-]+)(?:/(care-plan|escalation|simulate|requests|plan-clock|labs|bat-recheck))?",
            path,
        )
        return path, m

    def do_GET(self):
        path, m = self.route()
        if path == "/api/health":
            return self.json(
                {"ok": True, "mode": "synthetic-local-demo", "clinicalUse": False, **phone_access()}
            )
        if path == "/api/reports":
            with LOCK, db() as c:
                data = reports(c)
            return self.json(data)
        im = re.fullmatch(r"/api/invite/([A-Za-z0-9_-]{8,64})", path)
        if im:
            with LOCK, db() as c:
                inv = invite_row(c, im.group(1))
                reply = (invite_view(c, inv), 200) if inv else ({"error": "This link is not valid"}, 404)
            return self.json(*reply)
        if path == "/api/tasks":
            with LOCK, db() as c:
                rows = c.execute(
                    "SELECT t.*, p.state FROM tasks t JOIN patients p ON p.id = t.patient_id ORDER BY t.created_at, t.id"
                ).fetchall()
                tasks = [
                    task_view(c, r, json.loads(r["state"]))
                    for r in rows
                    if r["status"] in TASK_STATUSES
                ]
                setup = center_setup(c)
            # "Viewing as" also offers the outside partner once any role is routed there.
            roles = OWNER_ROLES + ([OUTSIDE_PARTNER] if any(s["routeTo"] == OUTSIDE_PARTNER and not s["onStaff"] for s in setup.values()) else [])
            return self.json({"roles": roles, "tasks": tasks, "center": setup, "outsidePartner": OUTSIDE_PARTNER})
        if path == "/api/patients" or (m and not m.group(2)):
            with LOCK, db() as c:
                reply = self.read_patients(c, path, m)
            return self.json(*reply)
        if m and m.group(2) == "escalation":
            with db() as c:
                events = c.execute(
                    "SELECT kind,message,at FROM events WHERE patient_id=? ORDER BY id DESC LIMIT 30",
                    (m.group(1),),
                ).fetchall()
            return self.json({"events": [dict(e) for e in events]})
        if path not in ("/patient.html", "/clinician.html", "/"):
            return self.json({"error": "Not found"}, 404)
        if path == "/":
            self.path = "/patient.html"
        return super().do_GET()

    def read_patients(self, c, path, m):
        if path == "/api/patients":
            rows = c.execute("SELECT * FROM patients").fetchall()
            for row in rows:
                recalc(c, row)
            # The patient list is the care-team view: include chart signals.
            return {"patients": [derive(row, c, chart=True) for row in rows]}, 200
        row = c.execute("SELECT * FROM patients WHERE id=?", (m.group(1),)).fetchone()
        if not row:
            return {"error": "Not found"}, 404
        if is_family(c, row["id"]):
            return {"error": "Use your personal link"}, 403
        recalc(c, row)
        return {"patient": derive(row, c)}, 200

    def do_HEAD(self):
        path, _ = self.route()
        if path not in ("/patient.html", "/clinician.html", "/"):
            return self.send_error(404)
        if path == "/":
            self.path = "/patient.html"
        return super().do_HEAD()

    def do_PUT(self):
        path, m = self.route()
        data = self.body()
        im = re.fullmatch(r"/api/invite/([A-Za-z0-9_-]{8,64})", path)
        if im:
            # One person's link: their state goes to their own record only.
            if not isinstance(data, dict) or not isinstance(data.get("state"), dict):
                return self.json({"error": "Invalid request"}, 400)
            state = data["state"]
            error = validate_state(state)
            if error or len(json.dumps(state)) > 80000:
                return self.json({"error": error or "Too large"}, 400)
            with LOCK, db() as c:
                inv = invite_row(c, im.group(1))
                if not inv:
                    reply = ({"error": "This link is not valid"}, 404)
                else:
                    if owns_record(inv):
                        save_main_state(c, inv["family_id"], state)
                    else:
                        save_caregiver_state(c, inv["family_id"], state)
                    reply = (invite_view(c, inv), 200)
            return self.json(*reply)
        if (
            not m
            or m.group(2)
            or not isinstance(data, dict)
            or not isinstance(data.get("state"), dict)
        ):
            return self.json({"error": "Invalid request"}, 400)
        state = data["state"]
        pid = m.group(1)
        error = validate_state(state)
        if error:
            return self.json({"error": error}, 400)
        if len(json.dumps(state)) > 80000:
            return self.json({"error": "Too large"}, 413)
        with LOCK, db() as c:
            # Family records are only reachable through each person's own link.
            if is_family(c, pid):
                return self.json({"error": "Use your personal link"}, 403)
            row = save_main_state(c, pid, state)
            reply = {"patient": derive(row, c)}
        # Sent only after the block above has committed.
        return self.json(reply)

    def do_POST(self):
        path, m = self.route()
        data = self.body()
        if path == "/api/families" and isinstance(data, dict):
            with LOCK, db() as c:
                reply = self.enroll(c, data)
            return self.json(*reply)
        if path == "/api/center" and isinstance(data, dict):
            with LOCK, db() as c:
                reply = save_center(c, data)
            return self.json(*reply)
        if path == "/api/sms/inbound" and isinstance(data, dict):
            # Simulated SMS (no real texts are sent or received). Shaped like a texting-service
            # webhook so a real SMS provider could be connected later: who sent it, and the body.
            token, body = data.get("token"), data.get("body")
            if not isinstance(token, str) or not isinstance(body, str):
                return self.json({"error": "token and body are required"}, 400)
            with LOCK, db() as c:
                reply = text_inbound(c, token, body)
            return self.json(*reply)
        fm = re.fullmatch(r"/api/families/([A-Za-z0-9_-]+)/leads", path)
        if fm and isinstance(data, dict):
            with LOCK, db() as c:
                f = c.execute("SELECT leads FROM families WHERE id=?", (fm.group(1),)).fetchone()
                if not f:
                    reply = ({"error": "Family not found"}, 404)
                elif data.get("leads") not in LEADS_NAMES:
                    reply = ({"error": "Who leads must be parent, shared or patient"}, 400)
                else:
                    new = data["leads"]
                    if new != f["leads"]:
                        c.execute("UPDATE families SET leads=? WHERE id=?", (new, fm.group(1)))
                        event(c, fm.group(1), "who_leads", f"Who leads changed from {LEADS_NAMES[f['leads']]} to {LEADS_NAMES[new]} ({acting_role(data)})")
                        row = c.execute("SELECT * FROM patients WHERE id=?", (fm.group(1),)).fetchone()
                        recalc(c, row)
                    reply = ({"family": family_info(c, fm.group(1))}, 200)
            return self.json(*reply)
        tm = re.fullmatch(r"/api/tasks/(\d+)/(plan|outcome)", path)
        if tm and isinstance(data, dict):
            with LOCK, db() as c:
                reply = self.task_action(c, int(tm.group(1)), tm.group(2), data)
            # Sent only after the block above has committed.
            return self.json(*reply)
        if not m or not m.group(2) or not isinstance(data, dict):
            return self.json({"error": "Invalid request"}, 400)
        pid, action = m.groups()
        with LOCK, db() as c:
            reply = self.post_action(c, pid, action, data)
        # Sent only after the block above has committed.
        return self.json(*reply)

    def enroll(self, c, data):
        """Clinician enrolls a family; returns the personal links."""
        first = data.get("patientName")
        initial = data.get("patientLastInitial")
        caregiver = data.get("caregiverName")
        age = data.get("age")
        lang = data.get("lang")
        leads = data.get("leads") or None
        for v, label in ((first, "patient first name"), (caregiver, "caregiver first name")):
            if not isinstance(v, str) or not v.strip() or len(v) > 45:
                return {"error": f"Please enter the {label}"}, 400
        if not isinstance(initial, str) or not re.fullmatch(r"[A-Za-zÀ-ÿ]", initial):
            return {"error": "Last initial must be one letter"}, 400
        if type(age) != int or not 0 <= age <= 30:
            return {"error": "Age must be a whole number from 0 to 30"}, 400
        if lang not in ("en", "es"):
            return {"error": "Language must be English or Spanish"}, 400
        if leads is not None and leads not in LEADS_NAMES:
            return {"error": "Who leads must be parent, shared or patient"}, 400
        fid = create_family(c, first.strip(), initial.upper(), age, lang, caregiver.strip(), leads)
        return {"id": fid, "family": family_info(c, fid)}, 200

    def task_action(self, c, task_id, action, data):
        """Clinician actions on one barrier task; returns (reply, status)."""
        t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t or t["status"] not in TASK_STATUSES:
            return {"error": "Task not found"}, 404
        pid = t["patient_id"]
        by = acting_role(data)
        # Audit times use real time; due dates count from the patient's (demo) clock.
        now = NOW()
        clock_now = now + timedelta(days=clock_days(c, pid))
        if action == "plan":
            # One plan per barrier: a new plan only when the task has none.
            if t["status"] != "needs_plan":
                return {"error": "This barrier already has a plan"}, 409
            fields, error = clean_plan(data)
            if error:
                return {"error": error}, 400
            c.execute(
                "INSERT INTO plans(task_id,who,what,how,where_text,when_text,patient_text,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, fields["who"], fields["what"], fields["how"], fields["where"], fields["when"], fields["patientText"], iso(now), by),
            )
            due = clock_now + timedelta(days=CHECK_BACK_DAYS)
            c.execute(
                "UPDATE tasks SET status='plan_agreed', due_date=? WHERE id=?",
                (iso(due), task_id),
            )
            event(
                c,
                pid,
                "plan_agreed",
                f"Plan agreed for '{t['barrier']}' by {by}; check back by {due.date().isoformat()}",
            )
        elif action == "outcome":
            outcome = data.get("outcome")
            if outcome not in OUTCOME_NAMES:
                return {"error": "Outcome must be working, not_working or adjust"}, 400
            current = c.execute(
                "SELECT * FROM plans WHERE task_id=? AND closed_at IS NULL", (task_id,)
            ).fetchone()
            if not current or t["status"] == "needs_plan":
                return {"error": "This barrier has no plan to review yet"}, 409
            b = t["barrier"]
            # Record the completed follow-up and its due date, to report on-time vs late.
            c.execute(
                "INSERT INTO followups(task_id,patient_id,barrier,due_date,done_at,outcome) VALUES (?,?,?,?,?,?)",
                (task_id, pid, b, t["due_date"], iso(clock_now), outcome),
            )
            if outcome == "working":
                # Durability phase: keep the plan, check again later.
                due = clock_now + timedelta(days=DURABILITY_DAYS)
                c.execute(
                    "UPDATE tasks SET status='keep_checking', due_date=? WHERE id=?",
                    (iso(due), task_id),
                )
                event(c, pid, "outcome", f"'{b}': Working - keep checking; next check-back {due.date().isoformat()} ({by})")
            elif outcome == "not_working":
                # Close the plan (kept in history); the barrier needs a new plan.
                c.execute(
                    "UPDATE plans SET closed_at=?, outcome='not_working' WHERE id=?",
                    (iso(now), current["id"]),
                )
                c.execute(
                    "UPDATE tasks SET status='needs_plan', due_date=NULL WHERE id=?",
                    (task_id,),
                )
                event(c, pid, "outcome", f"'{b}': Not working - plan closed; needs a new plan ({by})")
            else:
                fields, error = clean_plan(data)
                if error:
                    return {"error": error}, 400
                # Keep the old version in history; the edited plan becomes current.
                c.execute(
                    "UPDATE plans SET closed_at=?, outcome='adjusted' WHERE id=?",
                    (iso(now), current["id"]),
                )
                c.execute(
                    "INSERT INTO plans(task_id,who,what,how,where_text,when_text,patient_text,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                    (task_id, fields["who"], fields["what"], fields["how"], fields["where"], fields["when"], fields["patientText"], iso(now), by),
                )
                due = clock_now + timedelta(days=CHECK_BACK_DAYS)
                c.execute(
                    "UPDATE tasks SET status='plan_agreed', due_date=? WHERE id=?",
                    (iso(due), task_id),
                )
                event(c, pid, "outcome", f"'{b}': Adjust - plan changed; new check-back {due.date().isoformat()} ({by})")
        st = json.loads(
            c.execute("SELECT state FROM patients WHERE id=?", (pid,)).fetchone()["state"]
        )
        t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return {"task": task_view(c, t, st)}, 200

    def post_action(self, c, pid, action, data):
        """Clinician actions; returns (reply, status)."""
        row = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not row:
            return {"error": "Not found"}, 404
        if action == "care-plan":
            value = data.get("carePlan", "")
            if not isinstance(value, str) or len(value) > 1200:
                return {"error": "Invalid note"}, 400
            c.execute("UPDATE patients SET care_plan=? WHERE id=?", (value, pid))
            event(c, pid, "care_plan", "Clinician updated follow-up note")
        elif action == "simulate":
            hours = data.get("hours")
            if type(hours) != int or hours not in (0, 25, 49):
                return {"error": "Demo hours must be 0, 25, or 49"}, 400
            c.execute(
                "UPDATE escalation SET stage='none',acknowledged=0 WHERE patient_id=? AND stage='resolved'",
                (pid,),
            )
            c.execute(
                "UPDATE patients SET demo_offset_hours=? WHERE id=?", (hours, pid)
            )
            event(c, pid, "simulation", f"Demo time offset set to {hours} hours")
        elif action == "bat-recheck":
            # Demo only: move this patient's re-check clock so the 6-month checklist is due now.
            infos = family_rechecks(c, pid)
            if not infos:
                return {"error": "No completed checklist yet"}, 409
            today = NOW().date()
            days = max(0, max((date.fromisoformat(i["nextDue"]) - today).days for i in infos))
            c.execute("UPDATE patients SET bat_clock_days=? WHERE id=?", (days, pid))
            event(c, pid, "simulation", f"Demo only: 6-month checklist re-check made due (clock +{days} days)")
        elif action == "labs":
            # Demo only (no real EHR): add a fictional lab result so a chart flag can appear live.
            try:
                drawn = date.fromisoformat(str(data.get("date", "")))
            except ValueError:
                return {"error": "Date must be YYYY-MM-DD"}, 400
            level = data.get("level")
            if type(level) not in (int, float) or not 0 < level < 100:
                return {"error": "Level must be a number above 0 and below 100"}, 400
            # Allow one day ahead of the server's date for time-zone differences.
            if drawn > NOW().date() + timedelta(days=1):
                return {"error": "Date cannot be in the future"}, 400
            c.execute(
                "INSERT INTO labs(patient_id,drawn_on,tacrolimus,source) VALUES (?,?,?,'demo form')",
                (pid, drawn.isoformat(), float(level)),
            )
            event(c, pid, "simulation", f"Demo only: lab result added ({drawn.isoformat()}, {float(level)} ng/mL)")
        elif action == "plan-clock":
            # Demo only: move this patient's check-back clock forward. Real time is unchanged.
            days = data.get("days")
            if type(days) != int or days != CHECK_BACK_DAYS:
                return {"error": f"Demo jump must be {CHECK_BACK_DAYS} days"}, 400
            if row["plan_clock_days"] + days > 3650:
                return {"error": "Demo clock is already far ahead; use --reset"}, 400
            c.execute(
                "UPDATE patients SET plan_clock_days=plan_clock_days+? WHERE id=?",
                (days, pid),
            )
            event(c, pid, "simulation", f"Demo only: check-back clock moved forward {days} days")
        elif action == "requests":
            if data.get("action") != "handled":
                return {"error": "Invalid action"}, 400
            total = json.loads(row["state"]).get("contactRequests") or 0
            newly = total - min(row["handled_requests"], total)
            if newly <= 0:
                return {"error": "No open support requests"}, 409
            c.execute(
                "UPDATE patients SET handled_requests=? WHERE id=?", (total, pid)
            )
            event(
                c,
                pid,
                "request_handled",
                f"Clinician portal (demo, no login) marked {newly} support request(s) handled; {total} received in total",
            )
        elif action == "escalation":
            if data.get("action") not in ("acknowledge", "resolve"):
                return {"error": "Invalid action"}, 400
            recalc(c, row)
            er = c.execute(
                "SELECT * FROM escalation WHERE patient_id=?", (pid,)
            ).fetchone()
            if er and er["stage"] == "resolved" and data["action"] == "resolve":
                return {"patient": derive(row, c)}, 200
            if not er or er["stage"] in ("none", "resolved"):
                return {"error": "No active overdue alert"}, 409
            if data["action"] == "acknowledge" and er["acknowledged"]:
                return {"patient": derive(row, c)}, 200
            if data["action"] == "acknowledge":
                c.execute(
                    "UPDATE escalation SET acknowledged=1,updated_at=? WHERE patient_id=?",
                    (iso(), pid),
                )
                event(
                    c,
                    pid,
                    "acknowledged",
                    "Clinician acknowledged non-response alert",
                )
            else:
                c.execute(
                    "UPDATE escalation SET stage=?,acknowledged=0,note=?,updated_at=? WHERE patient_id=?",
                    (
                        "resolved",
                        "Clinician marked demo follow-up resolved",
                        iso(),
                        pid,
                    ),
                )
                event(
                    c,
                    pid,
                    "resolved",
                    "Clinician marked alert resolved (demo only)",
                )
        row = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        recalc(c, row)
        return {"patient": derive(row, c)}, 200


def reset_db():
    """Delete the demo database (and SQLite side files) so init() re-creates fresh demo patients."""
    existed = DB.exists()
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(DB) + suffix).unlink(missing_ok=True)
    init()
    print(
        ("Demo database reset: deleted and re-created " if existed else "Demo database created: ")
        + f"{DB} with 12 fresh demo patients (check-in times start from now).",
        flush=True,
    )


def lan_address():
    """This computer's address on the local network (no data is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


class Server(ThreadingHTTPServer):
    """On Windows, ports can normally be shared by two copies of the app; claim the port exclusively instead."""

    allow_reuse_address = os.name != "nt"  # restarts after a clean stop still work on Mac/Linux

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def phone_access():
    """Can phones reach this server, and at what address? Used to build invite links that work on a phone."""
    if HOST in ("0.0.0.0", "::"):
        ip = lan_address()
        return {"phoneAccess": True, "phoneBase": None if ip.startswith("127.") else f"http://{ip}:{PORT}"}
    if HOST in ("127.0.0.1", "localhost", "::1"):
        return {"phoneAccess": False, "phoneBase": None}
    return {"phoneAccess": True, "phoneBase": f"http://{HOST}:{PORT}"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KTx-CONNECT synthetic local demo server")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete and re-create the demo database with fresh demo patients, then start",
    )
    args = parser.parse_args()
    # Claim the port first: a second copy must stop here, before --reset can touch the running copy's data.
    try:
        server = Server((HOST, PORT), Handler)
    except OSError:
        print(f"Another copy of this app is already running on port {PORT}. Close that window first.", flush=True)
        raise SystemExit(1)
    if args.reset:
        reset_db()
    else:
        init()
    threading.Thread(target=escalation_worker, daemon=True).start()
    print(
        f"KTx-CONNECT synthetic demo: http://localhost:{PORT}/clinician.html",
        flush=True,
    )
    if HOST == "0.0.0.0":
        ip = lan_address()
        if ip.startswith("127."):
            print(
                "Phone access is on, but the network address could not be found. Run ipconfig (Windows) or ifconfig (Mac/Linux) to find it.",
                flush=True,
            )
        else:
            print(
                f"On a phone on the same Wi-Fi, open: http://{ip}:{PORT}/patient.html",
                flush=True,
            )
        print(
            "Warning: anyone on this network can open the demo (no login). Use fictional data only.",
            flush=True,
        )
    server.serve_forever()
