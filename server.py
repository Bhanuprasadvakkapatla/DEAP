#!/usr/bin/env python3
"""DEAP Connect local-only synthetic demo. NOT clinical monitoring."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json, sqlite3, threading, re, os, argparse, socket

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
NOW = lambda: datetime.now(timezone.utc)


def iso(d=None):
    return (d or NOW()).isoformat().replace("+00:00", "Z")


def conn():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
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
        if c.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0:
            sample = [
                ("Alex", "schedule", "yellow"),
                ("Jordan", "forget", "green"),
                ("Taylor", "activities", "red"),
                ("Sam", "swallow", "green"),
                ("Riley", "side", "yellow"),
                ("Morgan", "forget", "green"),
                ("Casey", "schedule", "yellow"),
                ("Avery", "taste", "green"),
                ("Jamie", "forget", "red"),
                ("Cameron", "activities", "green"),
                ("Drew", "schedule", "green"),
                ("Parker", "side", "yellow"),
            ]
            for i, (name, barrier, status) in enumerate(sample):
                state = {
                    "patientName": name,
                    "patientLastInitial": chr(65 + i),
                    "role": "caregiver" if i % 4 == 0 else "self",
                    "caregiverName": "Maria" if i % 4 == 0 else "",
                    "lang": "en",
                    "channel": "SMS",
                    "time": "07:00",
                    "primary": barrier,
                    "barriers": [barrier],
                    "strategies": ["reminder"],
                    "plan": "Use a reminder with a routine",
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


def event(c, pid, kind, message):
    c.execute(
        "INSERT INTO events(patient_id,kind,message,at) VALUES (?,?,?,?)",
        (pid, kind, message, iso()),
    )


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
        if not state.get("barriers")
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
    ):
        if key in state and (not isinstance(state[key], str) or len(state[key]) > 1200):
            return "Invalid " + key
    for key in ("barriers", "strategies"):
        if not isinstance(state.get(key, []), list) or any(
            not isinstance(x, str) for x in state.get(key, [])
        ):
            return "Invalid " + key
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
            with LOCK, conn() as c:
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
        if path == "/api/patients" or (m and not m.group(2)):
            with LOCK, conn() as c:
                if path == "/api/patients":
                    rows = c.execute("SELECT * FROM patients").fetchall()
                    for row in rows:
                        recalc(c, row)
                    return self.json({"patients": [derive(row, c) for row in rows]})
                row = c.execute(
                    "SELECT * FROM patients WHERE id=?", (m.group(1),)
                ).fetchone()
                if not row:
                    return self.json({"error": "Not found"}, 404)
                recalc(c, row)
                return self.json({"patient": derive(row, c)})
        if m and m.group(2) == "escalation":
            with conn() as c:
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
        with LOCK, conn() as c:
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
            row = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
            recalc(c, row)
            return self.json({"patient": derive(row, c)})

    def do_POST(self):
        path, m = self.route()
        data = self.body()
        if not m or not m.group(2) or not isinstance(data, dict):
            return self.json({"error": "Invalid request"}, 400)
        pid, action = m.groups()
        with LOCK, conn() as c:
            row = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
            if not row:
                return self.json({"error": "Not found"}, 404)
            if action == "care-plan":
                value = data.get("carePlan", "")
                if not isinstance(value, str) or len(value) > 1200:
                    return self.json({"error": "Invalid note"}, 400)
                c.execute("UPDATE patients SET care_plan=? WHERE id=?", (value, pid))
                event(c, pid, "care_plan", "Clinician updated follow-up note")
            elif action == "simulate":
                hours = data.get("hours")
                if type(hours) != int or hours not in (0, 25, 49):
                    return self.json({"error": "Demo hours must be 0, 25, or 49"}, 400)
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
                    return self.json({"error": "Invalid action"}, 400)
                total = json.loads(row["state"]).get("contactRequests") or 0
                newly = total - min(row["handled_requests"], total)
                if newly <= 0:
                    return self.json({"error": "No open support requests"}, 409)
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
                    return self.json({"error": "Invalid action"}, 400)
                recalc(c, row)
                er = c.execute(
                    "SELECT * FROM escalation WHERE patient_id=?", (pid,)
                ).fetchone()
                if er and er["stage"] == "resolved" and data["action"] == "resolve":
                    return self.json({"patient": derive(row, c)})
                if not er or er["stage"] in ("none", "resolved"):
                    return self.json({"error": "No active overdue alert"}, 409)
                if data["action"] == "acknowledge" and er["acknowledged"]:
                    return self.json({"patient": derive(row, c)})
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
            return self.json({"patient": derive(row, c)})


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
    parser = argparse.ArgumentParser(description="DEAP Connect synthetic local demo server")
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
        f"DEAP Connect synthetic demo: http://localhost:{PORT}/clinician.html",
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
