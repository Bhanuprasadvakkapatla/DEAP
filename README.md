# DEAP Connect — Patient/Caregiver + Clinician Full-Stack Demo

**Synthetic hackathon prototype only. Not for clinical use.**

## Start the application

Requires Python 3.9+; no external packages or internet connection.

```bash
cd DEAP_Connect_FullStack
python3 server.py
```

Open both views in the same browser or different browsers on the same computer:

- Clinician: http://localhost:8000/clinician.html
- Patient/caregiver: http://localhost:8000/patient.html

Do **not** open `patient.html` by double-clicking it (`file://`): the shared backend requires the server.

## Architecture

- `patient_v1_locked.html` — untouched, locked snapshot of personalized patient/caregiver v1.0.
- `patient.html` — identical core UX with an additive backend synchronization adapter and read-only care-team note.
- `clinician.html` — clinician roster, attention queue, patient details, reporting, and care-team follow-up notes.
- `server.py` — Python standard-library HTTP server + JSON API + SQLite database.
- `deap_demo.sqlite3` — automatically created local database with 12 fictional patient records.

Patient data is saved to the browser's localStorage and synchronized to the SQLite backend. Clinician changes are fetched by the patient app every five seconds; the clinician portal refreshes every four seconds. Patient identities are anonymous demo IDs stored in the browser; multiple tabs of the same browser profile share the same demo patient identity. Separate browser profiles create separate patients.

### API

- `GET /api/health`
- `GET /api/patients`
- `GET /api/patients/{id}`
- `PUT /api/patients/{id}` JSON `{ "state": {...} }`
- `POST /api/patients/{id}/care-plan` JSON `{ "carePlan": "..." }`

### Demonstrate

1. Start the server and open both views in two tabs.
2. Complete patient onboarding with fictional names and barrier choices.
3. See the new patient in clinician roster.
4. Submit a Red/Yellow check-in or contact request; see the attention queue update.
5. Save a care-team follow-up note; see it appear in the patient Today view.

### Safety and limitations

No authentication, authorization, transport security, auditing, HIPAA compliance, real SMS, EHR integration, or verified clinical assessment content. Do not deploy publicly or use real patient data. Barrier labels are demo placeholders, not the validated MAPS instrument. Status rules are illustrative workflow rules, not clinical risk scores. Clinician notes are demo messages, not medical instructions. Reset demo data by stopping the server and deleting `deap_demo.sqlite3`.

## Overdue Check-in & Escalation — engagement safety net (new)

This is a **simulation of non-response**, not an unconsciousness detector, missed-dose detector, or emergency monitoring system. The system does not know whether medication was taken. An unconscious/unresponsive person needs emergency services (911 in the US), not a response from this application.

- **24 hours** after last recorded check-in activity: a *simulated* caregiver notification appears in the connected patient view and clinician record. **No SMS or push is sent.**
- **48 hours**: the patient is flagged for clinician review in the attention queue. **No external clinician alert is sent.**
- Clinicians can **acknowledge** the flag, **mark it resolved**, and review a basic **event history**.
- Patient check-in activity resets the non-response stage. A passive page visit does not count as a check-in.
- Demo controls in clinician patient details: **Simulate 25h**, **Simulate 49h**, **Reset timer**. Demo offsets do not change actual system time.
- These durations are **illustrative hackathon values**, not approved medical escalation rules. The clinical team must determine thresholds, consent, authorized caregiver access, escalation ownership, alert delivery, and escalation response time before any real implementation.

### New API endpoints

- `POST /api/patients/{id}/simulate` with `{"hours":0|25|49}`
- `POST /api/patients/{id}/escalation` with `{"action":"acknowledge"|"resolve"}`
- `GET /api/patients/{id}/escalation` returns demo audit events

Run locally only (`127.0.0.1:8000`). The local SQLite file is generated on first run and should not be committed to Git.
