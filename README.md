# DG Office Scheduler — MPA / DG Meeting Edition

This version is a Flask **Multiple Page Application (MPA)** for controlled office scheduling.

## Main workflows

### 1. Department Meeting
Every department can request a meeting with a participant selected from the meeting-with list:
- HR Department
- IT Department
- Finance Department
- Administrative Department
- Commercial Department
- Operational Department
- DG Office
- External Client / Guest
- Vendor / Partner
- Other

The requester can also choose:
- No DG involvement
- DG as Participant
- DG as Chair

All department requests are sent to **PSO/VITO for approval**.

### 2. DG Meeting
The **DG Meeting** page is visible and accessible only to the PSO/VITO account.

A PSO can create a DG meeting with an internal department, DG Office, external client/guest, vendor/partner or other participant.

A DG meeting can be:
- DG Office (DG Office only)
- Conference Room → Main Conference Room or Mini Conference Room
- Online

The DG Meeting workflow does not show a separate "DG involvement" field because the meeting itself is already a DG meeting.

**DG conference-room meetings deliberately bypass normal room conflict checks.** This means PSO/VITO can schedule a DG meeting even if the selected conference room already has an approved booking at the same time.

### 3. Online meetings
Both workflows support online meetings through:
- Zoom
- Microsoft Teams
- Google Meet
- Other

An online meeting requires an `http://` or `https://` meeting link and does not reserve a physical room. The meeting-link field is displayed only after **Online Meeting** is selected.

## MPA pages
- `/dashboard` — command centre
- `/meetings` — approved meeting list
- `/new-meeting` — department meeting request
- `/dg-meeting` — PSO-only DG meeting creation
- `/approvals` — PSO/VITO approval queue
- `/rooms` — room availability
- `/reports` — PSO/VITO reporting and exports

## PSO/VITO approval rule
Department requests never become approved meetings until PSO/VITO approves them. If DG is requested as Participant or Chair, that request is shown explicitly in the approval queue.

## Demo accounts
- `hr / hr123`
- `it / it123`
- `finance / finance123`
- `administrative / admin123`
- `commercial / commercial123`
- `operational / operational123`
- `pso / pso123`

Change demo passwords before production use.

## Run
From the folder containing `app.py` and `requirements.txt`:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

## Optional email configuration
Set:
- `DG_SMTP_HOST`
- `DG_SMTP_PORT` (default 587)
- `DG_SMTP_USER`
- `DG_SMTP_PASSWORD`
- `DG_SMTP_FROM`

If SMTP is not configured, the approval workflow still works and requests remain visible inside the application.

## SQA / Validation Notes

The V8 maintenance pass included:
- Python syntax compilation check for `app.py`.
- JavaScript syntax check for `static/script.js`.
- SQLite workflow tests for department booking requests, PSO approval, cancellation requests, DG-room overlap, online meetings and room-conflict conditions.
- Route/template audit, including the new Calendar page and API.
- Verification that Online Meeting fields are conditional, Conference Room is limited to Main/Mini, DG Office is separate, and the dedicated DG Meeting page has no DG-involvement selector.

A critical approval-queue defect was found and fixed: the department booking INSERT had one extra placeholder before the `pending` status value, causing the submitted request's `status` and `created_at` values to be written into the wrong columns. This made the PSO approval queue appear empty. The INSERT is now correctly aligned and the API returns the created request ID/status.

The application should be run locally with the packages in `requirements.txt`; a full browser/Flask runtime test could not be executed in the packaging environment because Flask is not installed there. The static, database, SQL workflow and structural checks above were completed before packaging.
