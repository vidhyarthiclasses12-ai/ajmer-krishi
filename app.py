import json, os, random, sqlite3, uuid
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO

from flask import Flask, jsonify, request, send_from_directory, session
from openpyxl import Workbook, load_workbook
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "portal.db")
UPLOAD = os.path.join(BASE, "data", "photos")
os.makedirs(UPLOAD, exist_ok=True)
os.makedirs(os.path.dirname(DB), exist_ok=True)

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "change-this-ajmer-krishi-secret")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=60*60*8)
FAILS = {}

@app.after_request
def secure_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

BLOCKS = {
    "अजमेर ग्रामीण": ["गेगल", "घूघरा", "दौराई", "पुष्कर", "लोहागल", "हटूंडी", "सेदरिया"],
    "बड़ल्या": ["बड़ल्या", "सराधना", "नारेली", "मगरा", "खोड़ा", "आंबा मसीना"],
    "पीसांगन": ["पीसांगन", "गोला", "केसरपुरा", "बुधवाड़ा", "जेठाना", "गणहेड़ा"],
    "किशनगढ़ (सिलोरा)": ["सिलोरा", "रूपनगढ़", "हरमाड़ा", "बांदरसिंदरी", "पाटन", "मोतीपुरा"],
    "नसीराबाद": ["नसीराबाद", "तबाजी", "माकरवाली", "रामसर", "सरसुंडा"],
    "श्रीनगर": ["श्रीनगर", "रामगढ़", "खारवा", "देलवाड़ा"],
    "अराई": ["अराई", "बगहेरा", "जूनिया", "घाटियाली", "देवगांव"],
    "भिनाय": ["भिनाय", "बांदनवाड़ा", "बारली", "देवलिया कलां", "हीरापुरा"],
    "केकड़ी": ["केकड़ी", "जूनिया", "घाटियाली", "धूंधरी", "गुल्गांव"],
    "सरवाड़": ["सरवाड़", "टांटुटी", "रामगढ़", "सांकलिया"],
    "सावर": ["सावर", "रामगढ़", "खारवा", "टीटड़िया"],
}

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY, role TEXT, name TEXT, cluster TEXT, mobile TEXT,
      username TEXT UNIQUE, password_hash TEXT, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS records (
      id TEXT PRIMARY KEY, user_id TEXT, by_name TEXT, role TEXT, cluster TEXT,
      village TEXT, form_type TEXT, payload TEXT, status TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS photos (
      id TEXT PRIMARY KEY, record_id TEXT, filename TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS history (
      id TEXT PRIMARY KEY, record_id TEXT, status TEXT, note TEXT, by_name TEXT, at TEXT
    );
    CREATE TABLE IF NOT EXISTS resets (
      id TEXT PRIMARY KEY, username TEXT, status TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS clusters (
      id TEXT PRIMARY KEY, district TEXT, block TEXT, name TEXT UNIQUE
    );
    CREATE TABLE IF NOT EXISTS places (
      id TEXT PRIMARY KEY, cluster_id TEXT, village TEXT, gram_panchayat TEXT
    );
    CREATE TABLE IF NOT EXISTS otps (
      id TEXT PRIMARY KEY, username TEXT, mobile TEXT, code TEXT, expires TEXT, used INTEGER DEFAULT 0
    );
    """)
    if not con.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        con.execute(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?,1)",
            ("u-admin", "admin", "System Admin", "All", "9000000000", "admin",
             generate_password_hash("Ajmer@2026")),
        )
    con.commit()
    con.close()

init()

def current():
    uid = session.get("uid")
    if not uid:
        return None
    con = db()
    row = con.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    con.close()
    return row

def login_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        if not current():
            return jsonify({"error": "लॉगिन जरूरी है"}), 401
        return fn(*a, **k)
    return wrap

def admin_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        u = current()
        if not u or not can_see_all(u):
            return jsonify({"error": "सिर्फ System Admin"}), 403
        return fn(*a, **k)
    return wrap

def send_sms(mobile, code):
    key = os.environ.get("FAST2SMS_API_KEY", "").strip()
    if not key:
        return False
    phone = "".join(ch for ch in mobile if ch.isdigit())[-10:]
    body = urllib.parse.urlencode({
        "authorization": key,
        "route": "otp",
        "variables_values": code,
        "numbers": phone,
        "flash": "0",
    }).encode()
    req = urllib.request.Request("https://www.fast2sms.com/dev/bulkV2", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status == 200
    except Exception:
        return False

def send_text(mobile, text):
    key = os.environ.get("FAST2SMS_API_KEY", "").strip()
    if not key:
        return False
    phone = "".join(ch for ch in mobile if ch.isdigit())[-10:]
    body = urllib.parse.urlencode({
        "authorization": key,
        "route": "q",
        "message": text,
        "numbers": phone,
        "flash": "0",
    }).encode()
    req = urllib.request.Request("https://www.fast2sms.com/dev/bulkV2", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status == 200
    except Exception:
        return False


def manager_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        u = current()
        if not u or u["role"] not in ("admin", "district_admin"):
            return jsonify({"error": "System Admin या District Admin"}), 403
        return fn(*a, **k)
    return wrap


def audit(con, user, action, entity, entity_id="", detail=""):
    con.execute("CREATE TABLE IF NOT EXISTS audit_logs (id TEXT PRIMARY KEY, user_name TEXT, role TEXT, action TEXT, entity TEXT, entity_id TEXT, detail TEXT, ip TEXT, at TEXT)")
    con.execute(
        "INSERT INTO audit_logs VALUES (?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, (user or {}).get("name"), (user or {}).get("role"), action, entity, entity_id, detail, request.remote_addr or "", datetime.now().isoformat(timespec="seconds")),
    )

def add_history(con, rid, status, note, by):
    con.execute(
        "INSERT INTO history VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, rid, status, note, by, datetime.now().isoformat(timespec="seconds")),
    )

@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")

@app.post("/api/login")
def login():
    ip = request.remote_addr or "local"
    if FAILS.get(ip, 0) >= 8:
        return jsonify({"error": "Too many failed logins. Try again later."}), 429
    data = request.json or {}
    con = db()
    user = con.execute("SELECT * FROM users WHERE username=? AND active=1", (data.get("username", "").strip(),)).fetchone()
    con.close()
    if not user or not check_password_hash(user["password_hash"], data.get("password", "")):
        return jsonify({"error": "गलत यूजरनेम या पासवर्ड"}), 401
    FAILS[ip] = 0
    session.permanent = True
    session["uid"] = user["id"]
    con = db()
    audit(con, dict(user), "login", "session", user["id"])
    con.commit(); con.close()
    return jsonify({"id": user["id"], "name": user["name"], "role": user["role"], "cluster": user["cluster"], "username": user["username"], "mobile": user["mobile"]})

@app.post("/api/password")
@login_required
def change_password():
    u = current()
    data = request.json or {}
    pw = data.get("password") or ""
    if len(pw) < 8 or pw.lower()==pw or pw.upper()==pw or not any(ch.isdigit() for ch in pw):
        return jsonify({"error": "Password needs 8 characters, upper, lower, and a number"}), 400
    if not check_password_hash(u["password_hash"], data.get("old") or ""):
        return jsonify({"error": "Current password is wrong"}), 400
    con = db()
    con.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), u["id"]))
    audit(con, u, "password_change", "user", u["id"])
    con.commit(); con.close()
    return jsonify({"ok": True})

@app.get("/api/backup")
@admin_required
def backup():
    from flask import send_file
    return send_file(DB, as_attachment=True, download_name="ajmer-krishi-backup.db")

@app.get("/api/notifications")
@login_required
def notifications():
    u = current()
    con = db()
    rows = [dict(r) for r in con.execute("SELECT status, note, by_name, at FROM history ORDER BY at DESC LIMIT 30").fetchall()]
    con.close()
    return jsonify(rows)

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.get("/api/me")
def me():
    u = current()
    if not u:
        return jsonify(None)
    return jsonify({"id": u["id"], "name": u["name"], "role": u["role"], "cluster": u["cluster"], "username": u["username"], "mobile": u["mobile"]})

TEHSILS = ["अजमेर","पीसांगन","पुष्कर","किशनगढ़","नसीराबाद","श्रीनगर","अराई","केकड़ी","भिनाय","सरवाड़","सावर"]

def lgd_tree():
    path = os.path.join(BASE, "ajmer_lgd.json")
    tree = json.loads(open(path, encoding="utf-8").read()) if os.path.exists(path) else {}
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS extra_villages (id TEXT PRIMARY KEY, tehsil TEXT, gp TEXT, village TEXT)")
    for row in con.execute("SELECT tehsil, gp, village FROM extra_villages"):
        tree.setdefault(row["tehsil"], {}).setdefault(row["gp"] or "Unassigned", [])
        if row["village"] not in tree[row["tehsil"]][row["gp"] or "Unassigned"]:
            tree[row["tehsil"]][row["gp"] or "Unassigned"].append(row["village"])
    con.close()
    return tree

@app.get("/api/directory")
def directory():
    try:
        return jsonify({"district": "अजमेर", "tree": lgd_tree()})
    except Exception:
        return jsonify({"district": "अजमेर", "tree": {}})

@app.post("/api/directory/village")
@admin_required
def map_village():
    data = request.json or {}
    tehsil, gp, village = (data.get("tehsil") or "").strip(), (data.get("gp") or "").strip(), (data.get("village") or "").strip()
    if not tehsil or not village:
        return jsonify({"error": "तहसील और गाँव जरूरी"}), 400
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS extra_villages (id TEXT PRIMARY KEY, tehsil TEXT, gp TEXT, village TEXT)")
    con.execute("INSERT INTO extra_villages VALUES (?,?,?,?)", (uuid.uuid4().hex, tehsil, gp or "Unassigned", village))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.post("/api/map/cluster")
@manager_required
def map_cluster():
    data = request.json or {}
    scheme = data.get("scheme")
    if scheme not in ("pkvy", "natural", "minikit", "demo"):
        return jsonify({"error": "Choose PKVY, Natural Farming, Minikit, or Demonstration"}), 400
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Cluster name is required"}), 400
    con = db()
    row = con.execute("SELECT id FROM clusters WHERE name=?", (name,)).fetchone()
    cid = row["id"] if row else uuid.uuid4().hex
    code = (scheme or "CL")[:3].upper() + "-2026-" + cid[:4].upper()
    if not row:
        con.execute("INSERT INTO clusters VALUES (?,?,?,?)", (cid, "अजमेर", data.get("tehsil") or "", name))
    con.execute("CREATE TABLE IF NOT EXISTS scheme_clusters (id TEXT PRIMARY KEY, scheme TEXT, cluster_id TEXT, gp TEXT)")
    con.execute("INSERT INTO scheme_clusters VALUES (?,?,?,?)", (uuid.uuid4().hex, scheme, cid, data.get("gp") or ""))
    for village in data.get("villages") or []:
        con.execute("INSERT INTO places VALUES (?,?,?,?)", (uuid.uuid4().hex, cid, village, data.get("gp") or village))
    con.commit()
    con.close()
    return jsonify({"ok": True, "id": cid, "cluster_id": code})

@app.post("/api/map/gp")
@manager_required
def map_gp():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    gp = (data.get("gp") or "").strip()
    tehsil = (data.get("tehsil") or "").strip()
    con = db()
    user = con.execute("SELECT * FROM users WHERE username=? AND role='supervisor' AND active=1", (username,)).fetchone()
    if not user:
        con.close()
        return jsonify({"error": "Agriculture Supervisor नहीं मिला"}), 404
    con.execute("CREATE TABLE IF NOT EXISTS supervisor_gp (id TEXT PRIMARY KEY, user_id TEXT, tehsil TEXT, gp TEXT)")
    con.execute("INSERT INTO supervisor_gp VALUES (?,?,?,?)", (uuid.uuid4().hex, user["id"], tehsil, gp))
    cname = gp + " cluster"
    row = con.execute("SELECT id FROM clusters WHERE name=?", (cname,)).fetchone()
    cid = row["id"] if row else uuid.uuid4().hex
    if not row:
        con.execute("INSERT INTO clusters VALUES (?,?,?,?)", (cid, "अजमेर", tehsil, cname))
    tree = lgd_tree()
    for village in (tree.get(tehsil) or {}).get(gp) or []:
        exists = con.execute("SELECT 1 FROM places WHERE cluster_id=? AND village=?", (cid, village)).fetchone()
        if not exists:
            con.execute("INSERT INTO places VALUES (?,?,?,?)", (uuid.uuid4().hex, cid, village, gp))
    con.execute("UPDATE users SET cluster=? WHERE id=?", (cname, user["id"]))
    con.commit()
    con.close()
    return jsonify({"ok": True, "cluster": cname})

@app.post("/api/map/aao")
@manager_required
def map_aao():
    data = request.json or {}
    con = db()
    user = con.execute("SELECT * FROM users WHERE username=? AND role='aao'", (data.get("username"),)).fetchone()
    if not user:
        con.close()
        return jsonify({"error": "AAO user not found"}), 404
    con.execute("CREATE TABLE IF NOT EXISTS aao_map (id TEXT PRIMARY KEY, user_id TEXT, block TEXT, office_type TEXT, office_name TEXT, supervisors TEXT)")
    con.execute("INSERT INTO aao_map VALUES (?,?,?,?,?,?)", (uuid.uuid4().hex, user["id"], data.get("block") or "", data.get("office_type") or "block", data.get("office_name") or "", ",".join(data.get("supervisors") or [])))
    con.commit(); con.close()
    return jsonify({"ok": True})

@app.get("/api/meta")
def meta():
    con = db()
    out = {}
    con.execute("CREATE TABLE IF NOT EXISTS scheme_clusters (id TEXT PRIMARY KEY, scheme TEXT, cluster_id TEXT, gp TEXT)")
    for c in con.execute("SELECT * FROM clusters ORDER BY name").fetchall():
        places = [dict(p) for p in con.execute("SELECT id, village, gram_panchayat FROM places WHERE cluster_id=?", (c["id"],)).fetchall()]
        schemes = [r["scheme"] for r in con.execute("SELECT scheme FROM scheme_clusters WHERE cluster_id=?", (c["id"],)).fetchall()]
        out[c["name"]] = {"id": c["id"], "district": c["district"], "block": c["block"], "places": places, "schemes": schemes, "gp": places[0]["gram_panchayat"] if places else ""}
    con.close()
    return jsonify(out)

@app.post("/api/clusters")
@manager_required
def add_cluster():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "क्लस्टर नाम लिखें"}), 400
    con = db()
    try:
        con.execute("INSERT INTO clusters VALUES (?,?,?,?)", (uuid.uuid4().hex, data.get("district") or "अजमेर", data.get("block") or name, name))
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        return jsonify({"error": "यह क्लस्टर पहले से है"}), 400
    con.close()
    return jsonify({"ok": True})

@app.delete("/api/clusters/<cid>")
@admin_required
def del_cluster(cid):
    con = db()
    con.execute("DELETE FROM places WHERE cluster_id=?", (cid,))
    con.execute("DELETE FROM clusters WHERE id=?", (cid,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.post("/api/places")
@manager_required
def add_place():
    data = request.json or {}
    if not data.get("cluster_id") or not data.get("village"):
        return jsonify({"error": "क्लस्टर और गाँव जरूरी"}), 400
    con = db()
    con.execute("INSERT INTO places VALUES (?,?,?,?)", (uuid.uuid4().hex, data["cluster_id"], data["village"].strip(), (data.get("gram_panchayat") or data["village"]).strip()))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.delete("/api/places/<pid>")
@admin_required
def del_place(pid):
    con = db()
    con.execute("DELETE FROM places WHERE id=?", (pid,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.post("/api/otp/send")
def otp_send():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    mobile = (data.get("mobile") or "").strip()
    con = db()
    user = con.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    if not user or (user["mobile"] or "")[-10:] != mobile[-10:]:
        con.close()
        return jsonify({"error": "यूजरनेम और मोबाइल मेल नहीं खाते"}), 400
    code = f"{random.randint(100000, 999999)}"
    exp = (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds")
    con.execute("INSERT INTO otps VALUES (?,?,?,?,?,0)", (uuid.uuid4().hex, username, mobile, code, exp))
    con.commit()
    con.close()
    sent = send_sms(mobile, code)
    if sent:
        return jsonify({"ok": True, "message": "OTP मोबाइल पर भेज दिया। 10 मिनट तक मान्य है।"})
    return jsonify({"ok": True, "message": "OTP बन गया, SMS कुंजी अभी सेट नहीं है। एडमिन Users में OTP देख सकता है।"})

@app.post("/api/otp/reset")
def otp_reset():
    data = request.json or {}
    if len(data.get("password") or "") < 8:
        return jsonify({"error": "नया Password must be at least 8 characters"}), 400
    con = db()
    row = con.execute("SELECT * FROM otps WHERE username=? AND code=? AND used=0 ORDER BY expires DESC", (data.get("username"), data.get("otp"))).fetchone()
    if not row or row["expires"] < datetime.now().isoformat(timespec="seconds"):
        con.close()
        return jsonify({"error": "OTP गलत या खत्म हो गया"}), 400
    con.execute("UPDATE users SET password_hash=? WHERE username=?", (generate_password_hash(data["password"]), data.get("username")))
    con.execute("UPDATE otps SET used=1 WHERE id=?", (row["id"],))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.get("/api/otps")
@admin_required
def list_otps():
    con = db()
    rows = [dict(r) for r in con.execute("SELECT username, mobile, code, expires, used FROM otps ORDER BY expires DESC LIMIT 20").fetchall()]
    con.close()
    return jsonify(rows)

@app.post("/api/reset-request")
def reset_request():
    username = (request.json or {}).get("username", "").strip()
    if not username:
        return jsonify({"error": "यूजरनेम लिखें"}), 400
    con = db()
    con.execute("INSERT INTO resets VALUES (?,?,?,?)", (uuid.uuid4().hex, username, "pending", datetime.now().isoformat(timespec="seconds")))
    con.commit()
    con.close()
    return jsonify({"ok": True, "message": "अनुरोध एडमिन के पास चला गया"})

@app.get("/api/resets")
@admin_required
def resets():
    con = db()
    rows = [dict(r) for r in con.execute("SELECT * FROM resets ORDER BY created_at DESC").fetchall()]
    con.close()
    return jsonify(rows)

@app.post("/api/admin/reset")
@manager_required
def admin_reset():
    data = request.json or {}
    password = data.get("password", "")
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    con = db()
    con.execute("UPDATE users SET password_hash=? WHERE username=?", (generate_password_hash(password), data.get("username")))
    con.execute("UPDATE resets SET status='done' WHERE username=?", (data.get("username"),))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.post("/api/users")
@manager_required
def add_user():
    data = request.json or {}
    me = current()
    allowed = ("aao", "supervisor", "lrp", "krishi_sakhi", "crp") if me["role"] == "district_admin" else ("district_admin", "aao", "supervisor", "lrp", "krishi_sakhi", "crp")
    if data.get("role") not in allowed:
        return jsonify({"error": "District Admin can only create cluster users"}), 403
    pw = data.get("password", "")
    if len(pw) < 8 or pw.lower()==pw or pw.upper()==pw or not any(ch.isdigit() for ch in pw):
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    mobile = (data.get("mobile") or "").strip()
    if len(mobile) < 10:
        return jsonify({"error": "Mobile number is required"}), 400
    con = db()
    try:
        con.execute(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?,1)",
            (uuid.uuid4().hex, data["role"], data.get("name"), data.get("cluster") or "All",
             mobile, data["username"].strip(), generate_password_hash(data["password"])),
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        return jsonify({"error": "Username already exists"}), 400
    con.close()
    link = os.environ.get("SITE_URL", "https://ajmer-krishi.onrender.com")
    text = f"Ajmer Agriculture Information\nLink: {link}\nUsername: {data['username'].strip()}\nPassword: {data['password']}\nCluster: {data.get('cluster') or 'All'}"
    sent = send_text(mobile, text)
    return jsonify({"ok": True, "sms": sent})

@app.get("/api/users")
@manager_required
def users():
    con = db()
    rows = [dict(r) for r in con.execute("SELECT id, role, name, cluster, mobile, username, active FROM users").fetchall()]
    con.close()
    return jsonify(rows)

@app.delete("/api/users/<uid>")
@admin_required
def delete_user(uid):
    me = current()
    if uid == me["id"]:
        return jsonify({"error": "अपना लॉगिन नहीं मिटा सकते"}), 400
    con = db()
    con.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

SCHEME_BY_ROLE = {
    "lrp": {"pkvy"},
    "krishi_sakhi": {"natural"},
    "crp": {"natural"},
    "supervisor": {"pkvy", "natural", "minikit", "demo"},
}

def can_see_all(u):
    return u["role"] in ("admin", "district_admin", "supervisor", "aao")

def visible_sql(u):
    if u["role"] in ("admin", "district_admin"):
        return "", []
    return " WHERE cluster=?", [u["cluster"]]

@app.get("/api/records")
@login_required
def records():
    u = current()
    where, args = visible_sql(u)
    status = request.args.get("status")
    if status:
        where = (where + " AND " if where else " WHERE ") + "status=?"
        args.append(status)
    con = db()
    rows = [dict(r) for r in con.execute(f"SELECT * FROM records{where} ORDER BY created_at DESC", args).fetchall()]
    for r in rows:
        r["payload"] = json.loads(r["payload"] or "{}")
        r["photos"] = [dict(p) for p in con.execute("SELECT id, filename FROM photos WHERE record_id=?", (r["id"],)).fetchall()]
        r["history"] = [dict(h) for h in con.execute("SELECT status, note, by_name, at FROM history WHERE record_id=? ORDER BY at", (r["id"],)).fetchall()]
        con.execute("CREATE TABLE IF NOT EXISTS signatures (id TEXT PRIMARY KEY, record_id TEXT, role TEXT, by_name TEXT, image TEXT, at TEXT)")
        r["signatures"] = [dict(x) for x in con.execute("SELECT role, by_name, image, at FROM signatures WHERE record_id=?", (r["id"],)).fetchall()]
    allowed = SCHEME_BY_ROLE.get(u["role"])
    if allowed:
        rows = [r for r in rows if (r["payload"].get("scheme") in allowed) or r["form_type"] in allowed or r["form_type"] == "farmer_master"]
    con.close()
    return jsonify(rows)

@app.post("/api/records")
@login_required
def create_record():
    u = current()
    data = request.json or {}
    cluster = u["cluster"] if u["role"] not in ("admin", "district_admin") else (data.get("cluster") or u["cluster"])
    if u["role"] not in ("admin", "district_admin") and data.get("cluster") and data.get("cluster") != u["cluster"]:
        return jsonify({"error": "आप केवल अपने क्लस्टर का डेटा भर सकते हैं"}), 403
    scheme = (data.get("form_type") or "")
    allowed = SCHEME_BY_ROLE.get(u["role"])
    if allowed and scheme not in allowed and scheme != "farmer_master":
        return jsonify({"error": "This scheme is not allowed for your role"}), 403
    rid = uuid.uuid4().hex
    now = datetime.now().isoformat(timespec="seconds")
    con = db()
    con.execute(
        "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rid, u["id"], u["name"], u["role"], cluster, data.get("village", ""), data.get("form_type", "परिशिष्ट 8"),
         json.dumps(data.get("payload") or {}, ensure_ascii=False), "submitted", now),
    )
    add_history(con, rid, "submitted", "जमा किया", u["name"])
    con.commit()
    con.close()
    return jsonify({"id": rid})

@app.post("/api/records/<rid>/photo")
@login_required
def upload_photo(rid):
    u = current()
    con = db()
    rec = con.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    if not rec or (not can_see_all(u) and rec["cluster"] != u["cluster"]):
        con.close()
        return jsonify({"error": "रिपोर्ट नहीं मिली"}), 404
    f = request.files.get("photo")
    if not f:
        con.close()
        return jsonify({"error": "फोटो नहीं"}), 400
    ext = os.path.splitext(secure_filename(f.filename or "photo.jpg"))[1] or ".jpg"
    name = rid + "_" + uuid.uuid4().hex[:8] + ext
    f.save(os.path.join(UPLOAD, name))
    pid = uuid.uuid4().hex
    con.execute("INSERT INTO photos VALUES (?,?,?,?)", (pid, rid, name, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    con.close()
    return jsonify({"id": pid})

@app.get("/api/photos/<pid>")
@login_required
def photo(pid):
    u = current()
    con = db()
    row = con.execute(
        "SELECT photos.filename, records.cluster FROM photos JOIN records ON records.id=photos.record_id WHERE photos.id=?",
        (pid,),
    ).fetchone()
    con.close()
    if not row or (not can_see_all(u) and row["cluster"] != u["cluster"]):
        return jsonify({"error": "नहीं"}), 404
    return send_from_directory(UPLOAD, row["filename"])

@app.post("/api/records/<rid>/edit")
@login_required
def edit_record(rid):
    u = current()
    data = request.json or {}
    con = db()
    rec = con.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    if not rec:
        con.close()
        return jsonify({"error": "Record not found"}), 404
    if rec["status"] not in ("submitted", "draft", "returned", "correction_required"):
        con.close()
        return jsonify({"error": "Signed record cannot be edited. Ask District Admin to revert it."}), 400
    payload = json.loads(rec["payload"] or "{}")
    payload.update(data.get("payload") or {})
    new_status = "submitted" if rec["status"] in ("returned", "correction_required", "corrected") else rec["status"]
    con.execute("UPDATE records SET payload=?, village=?, status=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), payload.get("Village") or rec["village"], new_status, rid))
    add_history(con, rid, "corrected", "Edited before sign", u["name"])
    con.commit(); con.close()
    return jsonify({"ok": True})

@app.post("/api/records/<rid>/status")
@admin_required
def set_status(rid):
    data = request.json or {}
    status = data.get("status")
    if status not in ("submitted", "correction_required", "resubmitted", "approved"):
        return jsonify({"error": "स्टेटस गलत"}), 400
    u = current()
    con = db()
    con.execute("UPDATE records SET status=? WHERE id=?", (status, rid))
    add_history(con, rid, status, data.get("note", ""), u["name"])
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.post("/api/records/<rid>/resubmit")
@login_required
def resubmit(rid):
    u = current()
    con = db()
    rec = con.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    if not rec or rec["user_id"] != u["id"]:
        con.close()
        return jsonify({"error": "नहीं मिली"}), 404
    payload = request.json or {}
    con.execute("UPDATE records SET payload=?, status='resubmitted' WHERE id=?", (json.dumps(payload.get("payload") or {}, ensure_ascii=False), rid))
    add_history(con, rid, "resubmitted", payload.get("note", "दोबारा जमा"), u["name"])
    con.commit()
    con.close()
    return jsonify({"ok": True})

REQUIRED = ["farmer", "crop", "kind", "area", "yield"]

def norm(v):
    return str(v or "").strip().lower()


TEMPLATES = {
  "pkvy": ["Farmer ID","Farmer Name","Father/Husband Name","Jan Aadhaar","Mobile","District","Tehsil","Block","GP","Village","Cluster ID","Financial Year","Demonstration Type","Demonstration Category","Season","Demonstration Date","Khasra No.","Area","Crop","Variety"],
  "natural": ["Farmer ID","Farmer Name","Father/Husband Name","Jan Aadhaar","Mobile","District","Tehsil","Block","GP","Village","Cluster ID","Financial Year","Demonstration Type","Demonstration Category","Season","Demonstration Date","Khasra No.","Area","Crop","Variety"],
  "minikit": ["Farmer ID","Farmer Name","Father/Husband Name","Jan Aadhaar","Mobile","District","Tehsil","Block","GP","Village","Cluster ID","Financial Year","Demonstration Type","Demonstration Category","Season","Demonstration Date","Khasra No.","Area","Crop","Variety"],
  "demo": ["Farmer ID","Farmer Name","Father/Husband Name","Jan Aadhaar","Mobile","District","Tehsil","Block","GP","Village","Cluster ID","Financial Year","Demonstration Type","Demonstration Category","Season","Demonstration Date","Khasra No.","Area","Crop","Variety"],
}
REQUIRED_BY = {k: ["Farmer Name","Village","Mobile"] for k in TEMPLATES}

@app.get("/api/template")
@login_required
def template():
    scheme = request.args.get("scheme") or "pkvy"
    cols = TEMPLATES.get(scheme)
    if not cols:
        return jsonify({"error": "योजना नहीं मिली"}), 400
    wb = Workbook()
    ws = wb.active
    ws.title = scheme
    ws.append(cols)
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    from flask import send_file
    return send_file(bio, as_attachment=True, download_name=f"{scheme}-template.xlsx")

@app.post("/api/excel")
@login_required
def excel_upload():
    u = current()
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "File is missing"}), 400
    try:
        wb = load_workbook(f, data_only=True)
    except Exception:
        return jsonify({"error": "This is not a valid Excel file. Download the template and upload .xlsx"}), 400
    ws = wb.active
    scheme = request.form.get("scheme") or "pkvy"
    headers = [str(c.value or "").strip() for c in next(ws.iter_rows(max_row=1))]
    missing_cols = [c for c in TEMPLATES.get(scheme, []) if c not in headers]
    if missing_cols:
        err_id = uuid.uuid4().hex
        ew = Workbook(); ews = ew.active
        ews.append(["Row", "Field", "Entered", "Error", "Correction"])
        ews.append([1, "Header", ", ".join(headers), "कॉलम नहीं मिले", "टेम्पलेट वाले कॉलम रखें: " + ", ".join(missing_cols)])
        os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
        ew.save(os.path.join(BASE, "data", err_id + ".xlsx"))
        return jsonify({"saved": 0, "errors": [{"row": 1, "type": "error", "detail": "Missing columns: " + ", ".join(missing_cols)}], "error_file": err_id, "message": "Excel header does not match the template"})
    def col(*parts):
        for i, h in enumerate(headers):
            if any(p in h.lower() for p in parts):
                return i
        return None
    idx = {
        "farmer": col("किसान", "farmer"),
        "crop": col("फसल", "crop"),
        "kind": col("किस्म", "kind"),
        "area": col("क्षेत्र", "area"),
        "yield": col("उपज", "yield"),
        "ytype": col("अनुमानित", "वास्तविक", "type"),
    }
    con = db()
    seen = {norm(json.loads(r["payload"]).get("farmer")) + "|" + norm(json.loads(r["payload"]).get("crop"))
            for r in con.execute("SELECT payload FROM records WHERE cluster=?", (u["cluster"],)).fetchall()}
    clean, errors = [], []
    for n, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not any(row):
            continue
        item = {}
        for i, h in enumerate(headers):
            val = row[i] if i < len(row) else ""
            item[h] = "" if val is None else str(val)
        item["scheme"] = scheme
        item["farmer"] = item.get("Farmer Name") or item.get("farmer")
        item["crop"] = item.get("Crop") or item.get("crop")
        missing = [k for k in REQUIRED_BY.get(scheme, []) if str(item.get(k) or "").strip() == ""]
        mobile = "".join(ch for ch in str(item.get("Mobile") or "") if ch.isdigit())
        if mobile and len(mobile) != 10:
            missing.append("Mobile 10 digit nahi")
        key = scheme + "|" + norm(item.get("Farmer Name")) + "|" + norm(item.get("Village")) + "|" + norm(item.get("Crop"))
        if missing:
            errors.append({"row": n, "field": ", ".join(missing), "entered": item.get("Farmer Name") or "", "type": "error", "detail": ", ".join(missing) + " khali ya galat"})
            continue
        if key in seen:
            errors.append({"row": n, "type": "duplicate", "detail": "किसान+फसल पहले से है"})
            continue
        seen.add(key)
        clean.append(item)
    if request.form.get("preview") == "1":
        con.close()
        return jsonify({"saved": 0, "preview": clean[:20], "valid": len(clean), "errors": errors, "message": f"Preview: {len(clean)} valid, {len(errors)} invalid"})
    saved = []
    now = datetime.now().isoformat(timespec="seconds")
    for item in clean:
        rid = uuid.uuid4().hex
        con.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, u["id"], u["name"], u["role"], u["cluster"], str(item.get("Village") or ""), scheme,
             json.dumps(item, ensure_ascii=False), "submitted", now),
        )
        add_history(con, rid, "submitted", "Excel से जमा", u["name"])
        saved.append(rid)
    con.commit()
    con.close()
    err_id = None
    if errors:
        ew = Workbook()
        ews = ew.active
        ews.append(["Row", "Field", "Entered", "Error", "Correction"])
        for e in errors:
            ews.append([e.get("row"), e.get("field") or e.get("detail"), e.get("entered"), e.get("type"), e.get("detail")])
        err_id = uuid.uuid4().hex
        os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
        ew.save(os.path.join(BASE, "data", err_id + ".xlsx"))
    return jsonify({"saved": len(saved), "errors": errors, "error_file": err_id, "message": f"{len(saved)} saved, {len(errors)} error"})

@app.get("/api/errors/<eid>")
@login_required
def error_xlsx(eid):
    path = os.path.join(BASE, "data", eid + ".xlsx")
    if not os.path.exists(path):
        return jsonify({"error": "एरर File is missing"}), 404
    from flask import send_file
    return send_file(path, as_attachment=True, download_name="error-rows.xlsx")

WORKFLOW = ["submitted", "supervisor_signed", "aao_approved"]
NEXT_ROLE = {"submitted": "supervisor", "supervisor_signed": "aao", "signed": "aao"}

@app.post("/api/workflow/<rid>")
@login_required
def workflow(rid):
    u = current()
    data = request.json or {}
    action = data.get("action")
    note = (data.get("note") or "").strip()
    con = db()
    rec = con.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    if not rec:
        con.close()
        return jsonify({"error": "Record not found"}), 404
    status = rec["status"]
    if action == "return":
        if not note:
            con.close()
            return jsonify({"error": "A return reason is required"}), 400
        new = "returned"
    elif action == "sign":
        actor = data.get("actor") if u["role"] == "admin" and data.get("actor") in ("lrp", "crp", "krishi_sakhi", "supervisor", "aao") else u["role"]
        stage = "submitted" if status in ("returned", "correction_required", "corrected", "draft") else status
        scheme = rec["form_type"]
        chain = ["lrp_signed", "crp_signed", "sakhi_signed", "supervisor_signed", "aao_approved"] if scheme in ("pkvy", "natural") else ["supervisor_signed", "aao_approved"]
        role_status = {"lrp": "lrp_signed", "crp": "crp_signed", "krishi_sakhi": "sakhi_signed", "crp": "sakhi_signed", "supervisor": "supervisor_signed", "aao": "aao_approved"}
        order = ["lrp", "supervisor", "aao"] if scheme == "pkvy" else ["krishi_sakhi", "supervisor", "aao"] if scheme == "natural" else ["supervisor", "aao"]
        current = {"submitted": -1, "lrp_signed": 0, "crp_signed": 1, "sakhi_signed": 2, "supervisor_signed": 3, "signed": 3}.get(stage, -1)
        if scheme not in ("pkvy", "natural") and stage in ("submitted", "draft"):
            current = -1
        expect = order[current + 1] if current + 1 < len(order) else None
        if scheme == "natural" and actor in ("crp", "krishi_sakhi") and stage in ("submitted", "returned", "draft"):
            expect = actor
        if actor != expect and u["role"] != "admin":
            con.close()
            return jsonify({"error": "You cannot sign at this stage"}), 403
        new = role_status.get(actor if u["role"] == "admin" else expect, "signed")
    else:
        con.close()
        return jsonify({"error": "Invalid action"}), 400
    con.execute("UPDATE records SET status=? WHERE id=?", (new, rid))
    add_history(con, rid, new, note or action, u["name"])
    sig = data.get("signature") or ""
    if sig.startswith("data:image"):
        con.execute("CREATE TABLE IF NOT EXISTS signatures (id TEXT PRIMARY KEY, record_id TEXT, role TEXT, by_name TEXT, image TEXT, at TEXT)")
        role = data.get("actor") or u["role"]
        con.execute("DELETE FROM signatures WHERE record_id=? AND role=?", (rid, role))
        con.execute("INSERT INTO signatures VALUES (?,?,?,?,?,?)", (uuid.uuid4().hex, rid, role, u["name"], sig, datetime.now().isoformat(timespec="seconds")))
    try:
        audit(con, u, action, "record", rid, new)
    except Exception:
        pass
    con.commit()
    con.close()
    return jsonify({"ok": True, "status": new})

@app.post("/api/revert")
@login_required
def revert():
    u = current()
    if u["role"] not in ("admin", "district_admin"):
        return jsonify({"error": "Only District Admin can do this"}), 403
    data = request.json or {}
    cid = (data.get("cluster") or "").strip()
    note = (data.get("reason") or "").strip()
    if not cid or not note:
        return jsonify({"error": "Cluster ID and reason are required"}), 400
    con = db()
    rows = con.execute("SELECT id, payload FROM records").fetchall()
    n = 0
    for r in rows:
        pld = json.loads(r["payload"] or "{}")
        if cid in (pld.get("cluster"), pld.get("PKVYClusterID"), pld.get("Cluster ID"), r["id"]):
            con.execute("UPDATE records SET status='returned' WHERE id=?", (r["id"],))
            add_history(con, r["id"], "returned", note, u["name"])
            n += 1
    con.commit()
    con.close()
    return jsonify({"ok": True, "count": n})

@app.get("/api/export")
@login_required
def export():
    u = current()
    where, args = visible_sql(u)
    cluster = request.args.get("cluster")
    if cluster and u["role"] == "admin":
        where, args = " WHERE cluster=?", [cluster]
    con = db()
    rows = con.execute(f"SELECT * FROM records{where} ORDER BY created_at", args).fetchall()
    con.close()
    wb = Workbook()
    ws = wb.active
    ws.title = "Consolidated"
    ws.append(["Date", "Cluster", "Village", "Who", "Role", "Status", "Form", "Farmer", "Crop", "Kind", "Area", "Yield"])
    for r in rows:
        p = json.loads(r["payload"] or "{}")
        ws.append([r["created_at"], r["cluster"], r["village"], r["by_name"], r["role"], r["status"], r["form_type"],
                   p.get("farmer") or p.get("farmerName"), p.get("crop"), p.get("kind"), p.get("area") or p.get("areaHa"), p.get("yield") or p.get("yieldQ")])
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    from flask import send_file
    return send_file(bio, as_attachment=True, download_name="ajmer-krishi-consolidated.xlsx")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
