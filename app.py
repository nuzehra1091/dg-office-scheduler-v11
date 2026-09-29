"""DG Office Meeting Scheduler - MPA edition.

Multi-page Flask application with:
- Department meeting requests requiring PSO/VITO approval.
- DG Meeting page available only to PSO/VITO.
- DG meetings may use DG Office, a conference room, or online and are not
  blocked by room conflicts.
- Department meetings can request DG as participant or chair; PSO approves.
- Online meeting support (Zoom, Teams, Google Meet, Other).
"""
import os, re, sqlite3, smtplib, csv, io
from email.message import EmailMessage
from datetime import datetime
from functools import wraps
from flask import Flask, g, request, session, jsonify, render_template, redirect, url_for, send_file
from werkzeug.security import generate_password_hash, check_password_hash

APP_SECRET = os.environ.get("DG_APP_SECRET", "dg-office-scheduler-secret-key-change-me")
DATABASE = "scheduler.db"
ROOMS = ["Main Conference Room", "Mini Conference Room"]
DEPARTMENTS = [
    "HR Department", "IT Department", "Finance Department",
    "Administrative Department", "Commercial Department", "Operational Department"
]
MEETING_WITH_OPTIONS = DEPARTMENTS + ["DG Office", "External Client / Guest", "Vendor / Partner", "Other"]
ONLINE_PLATFORMS = ["Zoom", "Microsoft Teams", "Google Meet", "Other"]
DEFAULT_USERS = [
    ("hr", "hr123", "HR Department", "hr@example.com", "department"),
    ("it", "it123", "IT Department", "it@example.com", "department"),
    ("finance", "finance123", "Finance Department", "finance@example.com", "department"),
    ("administrative", "admin123", "Administrative Department", "administrative@example.com", "department"),
    ("commercial", "commercial123", "Commercial Department", "commercial@example.com", "department"),
    ("operational", "operational123", "Operational Department", "operational@example.com", "department"),
    ("pso", "pso123", "DG Office (PSO)", "pso@example.com", "pso"),
]
app = Flask(__name__)
app.secret_key = APP_SECRET
SMTP_HOST = os.environ.get("DG_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("DG_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("DG_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("DG_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("DG_SMTP_FROM", SMTP_USER)


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
    return db

@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None: db.close()

def column_exists(db, table, column):
    return any(row["name"] == column for row in db.execute(f"PRAGMA table_info({table})"))

def init_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL, display_name TEXT NOT NULL, role TEXT NOT NULL, email TEXT
    );
    CREATE TABLE IF NOT EXISTS meetings (
      id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL DEFAULT '',
      meeting_date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
      title TEXT NOT NULL, department TEXT NOT NULL, booked_by TEXT NOT NULL,
      created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'approved',
      meeting_type TEXT NOT NULL DEFAULT 'department', meeting_mode TEXT NOT NULL DEFAULT 'room',
      meeting_with TEXT DEFAULT '', dg_role TEXT NOT NULL DEFAULT 'none',
      online_platform TEXT DEFAULT '', meeting_link TEXT DEFAULT '', notes TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS requests (
      id INTEGER PRIMARY KEY AUTOINCREMENT, request_type TEXT NOT NULL, meeting_id INTEGER,
      room TEXT DEFAULT '', meeting_date TEXT, start_time TEXT, end_time TEXT, title TEXT,
      department TEXT NOT NULL, requested_by TEXT NOT NULL, requester_email TEXT,
      status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, decided_at TEXT,
      decided_by TEXT, decision_note TEXT, cancellation_reason TEXT,
      meeting_type TEXT NOT NULL DEFAULT 'department', meeting_mode TEXT NOT NULL DEFAULT 'room',
      meeting_with TEXT DEFAULT '', dg_role TEXT NOT NULL DEFAULT 'none',
      online_platform TEXT DEFAULT '', meeting_link TEXT DEFAULT '', notes TEXT DEFAULT '',
      FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE SET NULL
    );
    """)
    migrations = [
      ("meetings", "meeting_type", "TEXT NOT NULL DEFAULT 'department'"),
      ("meetings", "meeting_mode", "TEXT NOT NULL DEFAULT 'room'"),
      ("meetings", "meeting_with", "TEXT DEFAULT ''"),
      ("meetings", "dg_role", "TEXT NOT NULL DEFAULT 'none'"),
      ("meetings", "online_platform", "TEXT DEFAULT ''"),
      ("meetings", "meeting_link", "TEXT DEFAULT ''"),
      ("meetings", "notes", "TEXT DEFAULT ''"),
      ("requests", "cancellation_reason", "TEXT"),
      ("requests", "meeting_type", "TEXT NOT NULL DEFAULT 'department'"),
      ("requests", "meeting_mode", "TEXT NOT NULL DEFAULT 'room'"),
      ("requests", "meeting_with", "TEXT DEFAULT ''"),
      ("requests", "dg_role", "TEXT NOT NULL DEFAULT 'none'"),
      ("requests", "online_platform", "TEXT DEFAULT ''"),
      ("requests", "meeting_link", "TEXT DEFAULT ''"),
      ("requests", "notes", "TEXT DEFAULT ''"),
    ]
    for table, col, definition in migrations:
        if not column_exists(db, table, col): db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        for u,p,n,e,r in DEFAULT_USERS:
            db.execute("INSERT INTO users(username,password_hash,display_name,email,role) VALUES(?,?,?,?,?)", (u,generate_password_hash(p),n,e,r))
    else:
        for u,_,_,e,_ in DEFAULT_USERS:
            db.execute("UPDATE users SET email=COALESCE(NULLIF(email,''),?) WHERE username=?", (e,u))
    db.commit(); db.close()

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"): return jsonify({"error":"Not logged in"}),401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped

def pso_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user=current_user()
        if not user or user["role"] != "pso": return jsonify({"error":"Only PSO/VITO can perform this action."}),403
        return view(*args, **kwargs)
    return wrapped

def current_user():
    return get_db().execute("SELECT * FROM users WHERE id=?",(session["user_id"],)).fetchone() if session.get("user_id") else None

def to_minutes(t):
    h,m=t.split(":"); return int(h)*60+int(m)

def times_overlap(a,b,c,d): return to_minutes(a)<to_minutes(d) and to_minutes(c)<to_minutes(b)

def valid_url(value): return bool(re.match(r"^https?://\S+$", value or "", re.I))

def send_email(to, subject, body):
    if not to or not SMTP_HOST or not SMTP_FROM: return False
    try:
        msg=EmailMessage(); msg["From"]=SMTP_FROM; msg["To"]=to; msg["Subject"]=subject; msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST,SMTP_PORT,timeout=10) as smtp:
            smtp.starttls()
            if SMTP_USER: smtp.login(SMTP_USER,SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception:
        app.logger.exception("Email delivery failed"); return False

def notify_pso(subject, body):
    row=get_db().execute("SELECT email FROM users WHERE role='pso' LIMIT 1").fetchone()
    return send_email(row["email"] if row else "",subject,body)

def notify_requester(email, subject, body): return send_email(email,subject,body)

def notify_department(department_name, subject, body):
    row=get_db().execute("SELECT email FROM users WHERE display_name=? AND role='department'",(department_name,)).fetchone()
    return send_email(row["email"] if row else "",subject,body)

def common_context(user):
    return dict(display_name=user["display_name"], email=user["email"], role=user["role"], rooms=ROOMS,
                departments=DEPARTMENTS, meeting_with_options=MEETING_WITH_OPTIONS,
                online_platforms=ONLINE_PLATFORMS, today=datetime.now().strftime("%Y-%m-%d"))

@app.route("/")
def root(): return redirect(url_for("dashboard") if current_user() else url_for("login"))
@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="GET":
        if current_user(): return redirect(url_for("dashboard"))
        return render_template("login.html")
    value=request.form.get("username","").strip().lower(); password=request.form.get("password","")
    user=get_db().execute("SELECT * FROM users WHERE lower(username)=? OR lower(email)=?",(value,value)).fetchone()
    if not user or not check_password_hash(user["password_hash"],password): return render_template("login.html",error="Invalid username/email or password.")
    session.clear(); session["user_id"]=user["id"]; session["display_name"]=user["display_name"]; session["role"]=user["role"]
    return redirect(url_for("dashboard"))
@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard(): return render_template("dashboard.html", **common_context(current_user()))
@app.route("/meetings")
@login_required
def meetings_page(): return render_template("meetings.html", **common_context(current_user()))
@app.route("/new-meeting")
@login_required
def new_meeting_page(): return render_template("new_meeting.html", **common_context(current_user()))
@app.route("/dg-meeting")
@login_required
def dg_meeting_page():
    user=current_user()
    if user["role"]!="pso": return redirect(url_for("new_meeting_page"))
    return render_template("dg_meeting.html", **common_context(user))
@app.route("/approvals")
@login_required
def approvals_page():
    user=current_user()
    if user["role"]!="pso": return redirect(url_for("dashboard"))
    return render_template("approvals.html", **common_context(user))
@app.route("/rooms")
@login_required
def rooms_page(): return render_template("rooms.html", **common_context(current_user()))

@app.route("/calendar")
@login_required
def calendar_page(): return render_template("calendar.html", **common_context(current_user()))

@app.route("/reports")
@login_required
def reports():
    user=current_user()
    if user["role"]!="pso": return jsonify({"error":"Only PSO can access reports."}),403
    return render_template("reports.html", **common_context(user))

@app.route("/api/meetings")
@login_required
def api_meetings():
    date=request.args.get("date")
    if not date:return jsonify({"error":"date is required"}),400
    rows=get_db().execute("SELECT * FROM meetings WHERE meeting_date=? AND status='approved' ORDER BY start_time",(date,)).fetchall()
    return jsonify({"date":date,"meetings":[dict(r) for r in rows]})

@app.route("/api/my-meetings")
@login_required
def api_my_meetings():
    user=current_user(); today=datetime.now().strftime("%Y-%m-%d"); db=get_db()
    if user["role"]=="pso":
        rows=db.execute("SELECT * FROM meetings WHERE meeting_date>=? AND status='approved' ORDER BY meeting_date,start_time",(today,)).fetchall()
    else:
        # A department's calendar includes meetings it booked AND meetings the DG booked with it
        # (e.g. a DG online/DG-office meeting where this department is the 'meeting_with' party).
        rows=db.execute("SELECT * FROM meetings WHERE meeting_date>=? AND (department=? OR meeting_with=?) AND status='approved' ORDER BY meeting_date,start_time",(today,user["display_name"],user["display_name"])).fetchall()
    return jsonify({"meetings":[dict(r) for r in rows]})

@app.route("/api/all-meetings")
@login_required
def api_all_meetings():
    user=current_user(); db=get_db(); today=datetime.now().strftime("%Y-%m-%d")
    if user["role"]=="pso": rows=db.execute("SELECT * FROM meetings WHERE meeting_date>=? AND status='approved' ORDER BY meeting_date,start_time",(today,)).fetchall()
    else: rows=db.execute("SELECT * FROM meetings WHERE meeting_date>=? AND (department=? OR meeting_with=?) AND status='approved' ORDER BY meeting_date,start_time",(today,user["display_name"],user["display_name"])).fetchall()
    return jsonify({"meetings":[dict(r) for r in rows]})
@app.route("/api/calendar")
@login_required
def api_calendar():
    user=current_user(); db=get_db()
    date=(request.args.get("date") or datetime.now().strftime("%Y-%m-%d")).strip()
    try: datetime.strptime(date,"%Y-%m-%d")
    except ValueError: return jsonify({"error":"Invalid date. Use YYYY-MM-DD."}),400
    # PSO sees the complete office calendar. Department users see meetings booked by their
    # department AND meetings the DG has scheduled with their department (meeting_with match),
    # since those are just as relevant to the department even though PSO is the booker of record.
    if user["role"]=="pso":
        rows=db.execute("SELECT * FROM meetings WHERE meeting_date=? AND status='approved' ORDER BY start_time,end_time,id",(date,)).fetchall()
    else:
        rows=db.execute("SELECT * FROM meetings WHERE meeting_date=? AND (department=? OR meeting_with=?) AND status='approved' ORDER BY start_time,end_time,id",(date,user["display_name"],user["display_name"])).fetchall()
    meetings=[dict(r) for r in rows]
    # Calculate free/busy state for physical rooms in the requested day. Online and DG Office do not consume conference-room capacity.
    room_state={}
    for room in ROOMS:
        room_meetings=[m for m in meetings if m.get("room")==room and m.get("meeting_mode")=="room"]
        room_state[room]={"status":"occupied" if room_meetings else "free", "meetings":room_meetings}
    return jsonify({"date":date,"rooms":room_state,"meetings":meetings,"rooms_list":ROOMS})

@app.route("/api/requests")
@login_required
def api_requests():
    user=current_user(); db=get_db()
    if user["role"]=="pso":
        rows=db.execute("SELECT * FROM requests WHERE status='pending' ORDER BY datetime(created_at) ASC, id ASC").fetchall()
    else:
        rows=db.execute("SELECT * FROM requests WHERE requested_by=? ORDER BY datetime(created_at) DESC, id DESC",(user["display_name"],)).fetchall()
    return jsonify({"requests":[dict(r) for r in rows], "count":len(rows)})

@app.route("/api/approval-queue")
@login_required
def api_approval_queue():
    user=current_user()
    if user["role"]!="pso": return jsonify({"error":"Only PSO/VITO can access the approval queue."}),403
    rows=get_db().execute("SELECT * FROM requests WHERE status='pending' ORDER BY datetime(created_at) ASC, id ASC").fetchall()
    return jsonify({"requests":[dict(r) for r in rows], "count":len(rows)})

def validate_meeting_payload(data, require_room=False):
    title=(data.get("title") or "").strip(); date=(data.get("date") or "").strip(); start=(data.get("start_time") or "").strip(); end=(data.get("end_time") or "").strip()
    mode=(data.get("meeting_mode") or "room").strip(); room=(data.get("room") or "").strip(); meeting_with=(data.get("meeting_with") or "").strip()
    dg_role=(data.get("dg_role") or "none").strip(); platform=(data.get("online_platform") or "").strip(); link=(data.get("meeting_link") or "").strip(); notes=(data.get("notes") or "").strip()
    if not all([title,date,start,end,meeting_with]): return None,"Title, date, time and meeting-with are required."
    if mode not in {"room","dg_office","online"}: return None,"Invalid meeting mode."
    if dg_role not in {"none","participant","chair"}: return None,"Invalid DG involvement."
    if mode=="room" and room not in ROOMS: return None,"Please select a valid room."
    if mode!="room": room=""
    if mode=="online":
        if platform not in ONLINE_PLATFORMS:return None,"Please select an online platform."
        if not valid_url(link):return None,"Please provide a valid meeting link beginning with http:// or https://."
    else: platform=""; link=""
    try: datetime.strptime(date,"%Y-%m-%d"); datetime.strptime(start,"%H:%M"); datetime.strptime(end,"%H:%M")
    except ValueError:return None,"Invalid date or time format."
    if to_minutes(start)>=to_minutes(end):return None,"End time must be after start time."
    return {"title":title,"date":date,"start":start,"end":end,"mode":mode,"room":room,"meeting_with":meeting_with,"dg_role":dg_role,"platform":platform,"link":link,"notes":notes},None

def room_conflict(db, room, date, start, end):
    if not room:return None
    rows=db.execute("SELECT * FROM meetings WHERE room=? AND meeting_date=? AND status='approved'",(room,date)).fetchall()
    for m in rows:
        if times_overlap(start,end,m["start_time"],m["end_time"]):return m
    return None

def insert_meeting(db,p,user,meeting_type):
    cur=db.execute("""INSERT INTO meetings
      (room,meeting_date,start_time,end_time,title,department,booked_by,created_at,status,meeting_type,meeting_mode,meeting_with,dg_role,online_platform,meeting_link,notes)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (p["room"],p["date"],p["start"],p["end"],p["title"],user["display_name"],user["display_name"],datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"approved",meeting_type,p["mode"],p["meeting_with"],p["dg_role"],p["platform"],p["link"],p["notes"]))
    return cur.lastrowid

@app.route("/api/book",methods=["POST"])
@login_required
def api_book():
    user=current_user(); data=request.get_json(force=True) or {}; p,error=validate_meeting_payload(data)
    if error:return jsonify({"error":error}),400
    if data.get("meeting_type")=="dg": return jsonify({"error":"Use the dedicated DG Meeting page. Only PSO/VITO can create DG Meetings."}),403
    db=get_db()
    # Department meetings always require PSO approval, including DG participant/chair requests.
    if p["mode"]=="room":
        conflict=room_conflict(db,p["room"],p["date"],p["start"],p["end"])
        if conflict:return jsonify({"error":f"{p['room']} is already booked from {conflict['start_time']} to {conflict['end_time']}."}),409
    db.execute("""INSERT INTO requests
      (request_type,room,meeting_date,start_time,end_time,title,department,requested_by,requester_email,status,created_at,meeting_type,meeting_mode,meeting_with,dg_role,online_platform,meeting_link,notes)
      VALUES('booking',?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?)""",
      (p["room"],p["date"],p["start"],p["end"],p["title"],user["display_name"],user["display_name"],user["email"] or "",datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"department",p["mode"],p["meeting_with"],p["dg_role"],p["platform"],p["link"],p["notes"]))
    request_id=db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    created=db.execute("SELECT id,status FROM requests WHERE id=?",(request_id,)).fetchone()
    notify_pso("DG Office: New department meeting request",f"{user['display_name']} requested '{p['title']}' with {p['meeting_with']} on {p['date']} {p['start']}-{p['end']}. DG role: {p['dg_role']}. Mode: {p['mode']}. Please review the PSO approval queue.")
    return jsonify({"success":True,"pending":True,"request_id":request_id,"request_status":created["status"] if created else "pending","message":"Meeting request submitted to PSO/VITO for approval."})

@app.route("/api/dg-meeting",methods=["POST"])
@login_required
@pso_required
def api_dg_meeting():
    user=current_user(); data=request.get_json(force=True) or {}; p,error=validate_meeting_payload(data)
    if error:return jsonify({"error":error}),400
    # A dedicated DG Meeting is inherently a meeting led/held by the DG;
    # there is no separate DG-involvement selector in this workflow.
    p["dg_role"] = "none"
    db=get_db()
    # DG meetings are controlled by PSO and deliberately bypass room conflict checks.
    meeting_id=insert_meeting(db,p,user,"dg")
    db.commit()
    # If the DG is holding an online meeting with a department, that department must be notified.
    if p["mode"]=="online" and p["meeting_with"] in DEPARTMENTS:
        notify_department(p["meeting_with"],"DG Office: DG has scheduled an online meeting with your department",
            f"The DG would like to hold an online meeting with your department: '{p['title']}' on {p['date']} from {p['start']} to {p['end']}. Platform: {p['platform']}. Join link: {p['link']}."+(f" Notes: {p['notes']}" if p['notes'] else ""))
    return jsonify({"success":True,"meeting_id":meeting_id,"message":"DG Meeting created successfully. DG meetings are not blocked by conference-room availability."})

@app.route("/api/cancel/<int:meeting_id>",methods=["POST"])
@login_required
def api_cancel(meeting_id):
    user=current_user(); db=get_db(); data=request.get_json(force=True) or {}; meeting=db.execute("SELECT * FROM meetings WHERE id=?",(meeting_id,)).fetchone()
    if not meeting:return jsonify({"error":"Meeting not found."}),404
    if user["role"]=="pso": db.execute("DELETE FROM meetings WHERE id=?",(meeting_id,)); db.commit(); return jsonify({"success":True,"message":"Meeting cancelled by PSO."})
    if meeting["department"]!=user["display_name"]:return jsonify({"error":"You can only request cancellation of your department's meetings."}),403
    reason=(data.get("reason") or "").strip()
    if len(reason)<5:return jsonify({"error":"Please provide a cancellation reason of at least 5 characters."}),400
    existing=db.execute("SELECT id FROM requests WHERE request_type='cancellation' AND meeting_id=? AND status='pending'",(meeting_id,)).fetchone()
    if existing:return jsonify({"error":"A cancellation request is already pending."}),409
    db.execute("""INSERT INTO requests(request_type,meeting_id,department,requested_by,requester_email,cancellation_reason,status,created_at,meeting_type,meeting_mode,meeting_with,dg_role,online_platform,meeting_link,notes)
      VALUES('cancellation',?,?,?,?,?,'pending',?,?,?,?,?,?,?,?)""",(meeting_id,user["display_name"],user["display_name"],user["email"] or "",reason,datetime.now().strftime("%Y-%m-%d %H:%M:%S"),meeting["meeting_type"],meeting["meeting_mode"],meeting["meeting_with"],meeting["dg_role"],meeting["online_platform"],meeting["meeting_link"],meeting["notes"]))
    db.commit(); notify_pso("DG Office: Meeting cancellation request",f"{user['display_name']} requested cancellation of '{meeting['title']}'. Reason: {reason}")
    return jsonify({"success":True,"pending":True,"message":"Cancellation request submitted to PSO/VITO."})

@app.route("/api/requests/<int:request_id>/decision",methods=["POST"])
@login_required
@pso_required
def decide_request(request_id):
    user=current_user(); db=get_db(); req=db.execute("SELECT * FROM requests WHERE id=? AND status='pending'",(request_id,)).fetchone()
    if not req:return jsonify({"error":"Pending request not found."}),404
    data=request.get_json(force=True) or {}; decision=(data.get("decision") or "").lower(); note=(data.get("note") or "").strip()
    if decision not in {"approve","reject"}:return jsonify({"error":"Decision must be approve or reject."}),400
    now=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if decision=="approve" and req["request_type"]=="booking":
        # Normal department room meetings are rechecked. Online/DG-office have no room conflict.
        if req["meeting_mode"]=="room":
            conflict=room_conflict(db,req["room"],req["meeting_date"],req["start_time"],req["end_time"])
            if conflict:
                db.execute("UPDATE requests SET status='rejected',decided_at=?,decided_by=?,decision_note=? WHERE id=?",(now,user["display_name"],"Rejected because the room is no longer available.",request_id)); db.commit()
                notify_requester(req["requester_email"],"Meeting request rejected",f"Your request for '{req['title']}' was rejected because the room is no longer available.")
                return jsonify({"success":True,"message":"Request rejected because the room is no longer available."})
        cur=db.execute("""INSERT INTO meetings(room,meeting_date,start_time,end_time,title,department,booked_by,created_at,status,meeting_type,meeting_mode,meeting_with,dg_role,online_platform,meeting_link,notes)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(req["room"],req["meeting_date"],req["start_time"],req["end_time"],req["title"],req["department"],req["requested_by"],now,"approved",req["meeting_type"],req["meeting_mode"],req["meeting_with"],req["dg_role"],req["online_platform"],req["meeting_link"],req["notes"]))
        db.execute("UPDATE requests SET status='approved',decided_at=?,decided_by=?,decision_note=?,meeting_id=? WHERE id=?",(now,user["display_name"],note,cur.lastrowid,request_id)); db.commit()
        notify_requester(req["requester_email"],"Meeting request approved",f"Your meeting '{req['title']}' with {req['meeting_with']} has been approved by PSO/VITO.")
        return jsonify({"success":True,"message":"Meeting request approved and added to the schedule."})
    if decision=="approve" and req["request_type"]=="cancellation":
        meeting=db.execute("SELECT * FROM meetings WHERE id=?",(req["meeting_id"],)).fetchone()
        if not meeting:
            db.execute("UPDATE requests SET status='rejected',decided_at=?,decided_by=?,decision_note=? WHERE id=?",(now,user["display_name"],"Meeting no longer exists.",request_id)); db.commit(); return jsonify({"success":True,"message":"Meeting no longer exists."})
        db.execute("DELETE FROM meetings WHERE id=?",(req["meeting_id"],)); db.execute("UPDATE requests SET status='approved',decided_at=?,decided_by=?,decision_note=? WHERE id=?",(now,user["display_name"],note,request_id)); db.commit()
        notify_requester(req["requester_email"],"Meeting cancellation approved",f"Cancellation of '{meeting['title']}' was approved by PSO/VITO.")
        return jsonify({"success":True,"message":"Cancellation approved."})
    db.execute("UPDATE requests SET status='rejected',decided_at=?,decided_by=?,decision_note=? WHERE id=?",(now,user["display_name"],note,request_id)); db.commit()
    notify_requester(req["requester_email"],"Meeting request rejected",f"Your {req['request_type']} request was rejected by PSO/VITO."+(f" Note: {note}" if note else ""))
    return jsonify({"success":True,"message":"Request rejected."})


def report_rows(date_from=None,date_to=None,department=None,room=None):
    db=get_db(); clauses=["status='approved'"]; args=[]
    if date_from:clauses.append("meeting_date>=?");args.append(date_from)
    if date_to:clauses.append("meeting_date<=?");args.append(date_to)
    if department:clauses.append("department=?");args.append(department)
    if room:clauses.append("room=?");args.append(room)
    return db.execute(f"SELECT * FROM meetings WHERE {' AND '.join(clauses)} ORDER BY meeting_date,start_time",args).fetchall()
@app.route("/api/reports")
@login_required
def api_reports():
    if current_user()["role"]!="pso":return jsonify({"error":"Only PSO can access reports."}),403
    return jsonify({"rows":[dict(r) for r in report_rows(request.args.get("from"),request.args.get("to"),request.args.get("department"),request.args.get("room"))]})
@app.route("/reports/export/<fmt>")
@login_required
def export_report(fmt):
    if current_user()["role"]!="pso":return jsonify({"error":"Only PSO can export reports."}),403
    rows=report_rows(request.args.get("from"),request.args.get("to"),request.args.get("department"),request.args.get("room")); fields=["id","meeting_date","start_time","end_time","meeting_type","meeting_mode","room","meeting_with","dg_role","online_platform","meeting_link","title","department","booked_by","created_at","status"]; filename="dg-office-meetings"
    if fmt=="csv":
        out=io.StringIO(); w=csv.DictWriter(out,fieldnames=fields);w.writeheader();[w.writerow({k:r[k] for k in fields}) for r in rows];return send_file(io.BytesIO(out.getvalue().encode("utf-8-sig")),as_attachment=True,download_name=filename+".csv",mimetype="text/csv")
    if fmt=="xlsx":
        from openpyxl import Workbook
        wb=Workbook();ws=wb.active;ws.title="Meetings";ws.append(fields)
        for r in rows:ws.append([r[k] for k in fields])
        buf=io.BytesIO();wb.save(buf);buf.seek(0);return send_file(buf,as_attachment=True,download_name=filename+".xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if fmt=="pdf":
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4,landscape
        from reportlab.platypus import SimpleDocTemplate,Table,TableStyle,Paragraph,Spacer
        from reportlab.lib.styles import getSampleStyleSheet
        buf=io.BytesIO();doc=SimpleDocTemplate(buf,pagesize=landscape(A4),rightMargin=20,leftMargin=20,topMargin=20,bottomMargin=20);styles=getSampleStyleSheet();story=[Paragraph("DG Office — Meeting Report",styles["Title"]),Spacer(1,8)]
        data=[["Date","Time","Type","Mode","Room","With","DG Role","Title"]]+[[r["meeting_date"],f"{r['start_time']}-{r['end_time']}",r["meeting_type"],r["meeting_mode"],r["room"] or "—",r["meeting_with"] or "—",r["dg_role"],r["title"]] for r in rows]
        table=Table(data,repeatRows=1);table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#10233f")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.4,colors.grey),("FONTSIZE",(0,0),(-1,-1),7)]));story.append(table);doc.build(story);buf.seek(0);return send_file(buf,as_attachment=True,download_name=filename+".pdf",mimetype="application/pdf")
    return jsonify({"error":"Unsupported export format."}),400

if __name__=="__main__":
    init_db(); print("\nDG Office Meeting Scheduler — MPA\nOpen: http://127.0.0.1:5000\n"); app.run(debug=True,host="127.0.0.1",port=5000)
