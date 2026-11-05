#!/usr/bin/env python3
"""KTx-CONNECT local-only synthetic demo. NOT clinical monitoring."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json, sqlite3, threading, re, os, argparse, socket
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
# Task status: needs_plan -> plan_agreed -> keep_checking (MAPS steps 4-5, packet page 4).
TASK_STATUSES = ("needs_plan", "plan_agreed", "keep_checking")
# The owner checks back within 2 weeks of agreeing a plan (packet page 4).
CHECK_BACK_DAYS = 14
# The five parts of a concrete plan, plus the version the patient sees.
PLAN_FIELDS = ("who", "what", "how", "where", "when", "patientText")
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
        # Clinician-owned follow-up tasks: one per "yes" barrier per assessment.
        # due_date is the next check-back. The plan text lives in the plans table.
        c.execute(
            "CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, assessment TEXT NOT NULL, barrier TEXT NOT NULL, owner_role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'needs_plan', created_at TEXT NOT NULL, plan TEXT, due_date TEXT, outcome TEXT, UNIQUE(patient_id, assessment, barrier))"
        )
        c.execute("UPDATE tasks SET status='needs_plan' WHERE status='open'")
        # Clinician-owned plans, one row per version. The open row (closed_at IS NULL) is
        # the current plan; closed rows keep the history with their outcome.
        c.execute(
            "CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, who TEXT NOT NULL, what TEXT NOT NULL, how TEXT NOT NULL, where_text TEXT NOT NULL, when_text TEXT NOT NULL, patient_text TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, closed_at TEXT, outcome TEXT, FOREIGN KEY(task_id) REFERENCES tasks(id))"
        )
        if c.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0:
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
            # Ideas the demo patients picked (same wording as patient.html). Barriers that only
            # get a care-team message have no ideas.
            seed_ideas = {
                "forget": [
                    ("reminder", "Phone or app reminders"),
                    ("routine", "Pair doses with a daily routine, like brushing teeth"),
                ],
                "taste": [("taste_flavoring", "Ask the pharmacist about flavoring")],
                "swallow": [("swallow_candy", "Practice swallowing with small candy")],
                "embarrass": [
                    ("embarrass_discreet", "Carry and take your medicine discreetly")
                ],
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
                    "strategies": [k for k, _ in seed_ideas.get(barrier, [])],
                    "plan": " + ".join(text for _, text in seed_ideas.get(barrier, []))
                    or "Talk with your care team to make a plan that fits your day.",
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
        # Create tasks for seed patients and for databases made before the tasks table existed.
        for row in c.execute("SELECT id, state FROM patients").fetchall():
            sync_tasks(c, row["id"], json.loads(row["state"]))


def event(c, pid, kind, message):
    c.execute(
        "INSERT INTO events(patient_id,kind,message,at) VALUES (?,?,?,?)",
        (pid, kind, message, iso()),
    )


def sync_tasks(c, pid, state):
    """Keep one task per "yes" barrier for the patient's current assessment.

    Runs only once all 14 BAT items are answered. A barrier changed back to "no"
    in the same assessment removes its task only while it has no plan yet.
    """
    answers = state.get("batAnswers") or {}
    if not all(answers.get(k) in ("yes", "no") for k in BAT_KEYS):
        return
    assessment = state.get("batId") or "initial"
    yes = [b for b in state.get("barriers", []) if b in BARRIER_OWNERS]
    existing = {
        r["barrier"]: r
        for r in c.execute(
            "SELECT id, barrier, status FROM tasks WHERE patient_id=? AND assessment=?",
            (pid, assessment),
        )
    }
    for b in yes:
        if b not in existing:
            c.execute(
                "INSERT INTO tasks(patient_id,assessment,barrier,owner_role,status,created_at) VALUES (?,?,?,?,'needs_plan',?)",
                (pid, assessment, b, BARRIER_OWNERS[b], iso()),
            )
            event(c, pid, "task_created", f"Task for {BARRIER_OWNERS[b]}: barrier '{b}'")
    for b, r in existing.items():
        if b not in yes and r["status"] == "needs_plan":
            c.execute("DELETE FROM tasks WHERE id=?", (r["id"],))
            event(c, pid, "task_removed", f"Patient changed '{b}' to No; open task removed")


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
        "status": t["status"],
        "createdAt": t["created_at"],
        "dueDate": t["due_date"],
        "isPrimary": st.get("primary") == t["barrier"],
        "plan": plan_view(current) if current else None,
        "planHistory": [plan_view(p) for p in plans if p["closed_at"] is not None],
        # Patient context shown next to the plan form (read-only).
        "strategies": st.get("strategies", []),
        # The patient's own words, exactly as written ("Something else").
        "ownWords": st.get("other", "") if "other" in st.get("barriers", []) else "",
    }


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


def derive(row, c):
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
    if state.get("silence"):
        reasons.append("Demo silence flag")
    if stage in ("caregiver_notice", "clinician_review"):
        reasons.append("Overdue check-in: " + stage.replace("_", " "))
    weekly = state.get("weekly") or []
    if weekly and weekly[-1].get("status") in ("yellow", "red"):
        reasons.append("Weekly " + weekly[-1]["status"] + " response")
    last = state.get("weekly") or []
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
    return dict(
        **state,
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
        barrierOwners={
            b: BARRIER_OWNERS[b] for b in state.get("barriers", []) if b in BARRIER_OWNERS
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
    )


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
    for key in ("silence", "shift"):
        if key in state and type(state[key]) != bool:
            return "Invalid " + key
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
            r"/api/patients/([A-Za-z0-9_-]+)(?:/(care-plan|escalation|simulate|requests))?",
            path,
        )
        return path, m

    def do_GET(self):
        path, m = self.route()
        if path == "/api/health":
            return self.json(
                {"ok": True, "mode": "synthetic-local-demo", "clinicalUse": False}
            )
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
            return self.json({"roles": OWNER_ROLES, "tasks": tasks})
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
            return {"patients": [derive(row, c) for row in rows]}, 200
        row = c.execute("SELECT * FROM patients WHERE id=?", (m.group(1),)).fetchone()
        if not row:
            return {"error": "Not found"}, 404
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
            existing = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
            old = json.loads(existing["state"]) if existing else {}
            activity = not existing or any(
                state.get(k) != old.get(k)
                for k in ("daily", "weekly", "contactRequests")
            )
            when = iso() if activity else existing["last_engagement"]
            c.execute(
                "INSERT INTO patients(id,state,care_plan,updated_at,last_engagement,demo_offset_hours) VALUES (?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at,last_engagement=excluded.last_engagement,demo_offset_hours=CASE WHEN ? THEN 0 ELSE patients.demo_offset_hours END",
                (pid, json.dumps(state), "", iso(), when, int(activity)),
            )
            if activity:
                er = c.execute(
                    "SELECT * FROM escalation WHERE patient_id=?", (pid,)
                ).fetchone()
                if er and er["stage"] != "none":
                    c.execute(
                        "UPDATE escalation SET stage='none',acknowledged=0,note='New patient activity recorded',updated_at=? WHERE patient_id=?",
                        (iso(), pid),
                    )
                    event(
                        c,
                        pid,
                        "activity",
                        "Patient activity ended the previous non-response episode",
                    )
            if any(state.get(k) != old.get(k) for k in ("barriers", "batAnswers", "batId")):
                sync_tasks(c, pid, state)
            row = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
            recalc(c, row)
            reply = {"patient": derive(row, c)}
        # Sent only after the block above has committed.
        return self.json(reply)

    def do_POST(self):
        path, m = self.route()
        data = self.body()
        tm = re.fullmatch(r"/api/tasks/(\d+)/(plan)", path)
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

    def task_action(self, c, task_id, action, data):
        """Clinician actions on one barrier task; returns (reply, status)."""
        t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t or t["status"] not in TASK_STATUSES:
            return {"error": "Task not found"}, 404
        pid = t["patient_id"]
        by = acting_role(data)
        now = NOW()
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
            due = now + timedelta(days=CHECK_BACK_DAYS)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KTx-CONNECT synthetic local demo server")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete and re-create the demo database with fresh demo patients, then start",
    )
    args = parser.parse_args()
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
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
