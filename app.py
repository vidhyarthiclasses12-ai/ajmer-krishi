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
            ("u-admin", "admin", "जिला एडमिन", "All", "9000000000", "admin",
             generate_password_hash("Ajmer@2026")),
        )
    con.execute("UPDATE users SET active=0 WHERE username IN ('clusterA','crpA')")
    if not con.execute("SELECT 1 FROM clusters").fetchone():
        for block, villages in BLOCKS.items():
            cid = uuid.uuid4().hex
            con.execute("INSERT INTO clusters VALUES (?,?,?,?)", (cid, "अजमेर", block, block))
            for v in villages:
                con.execute("INSERT INTO places VALUES (?,?,?,?)", (uuid.uuid4().hex, cid, v, v))
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
        if not u or u["role"] != "admin":
            return jsonify({"error": "सिर्फ एडमिन"}), 403
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
    data = request.json or {}
    con = db()
    user = con.execute("SELECT * FROM users WHERE username=? AND active=1", (data.get("username", "").strip(),)).fetchone()
    con.close()
    if not user or not check_password_hash(user["password_hash"], data.get("password", "")):
        return jsonify({"error": "गलत यूजरनेम या पासवर्ड"}), 401
    session["uid"] = user["id"]
    return jsonify({"id": user["id"], "name": user["name"], "role": user["role"], "cluster": user["cluster"]})

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.get("/api/me")
def me():
    u = current()
    if not u:
        return jsonify(None)
    return jsonify({"id": u["id"], "name": u["name"], "role": u["role"], "cluster": u["cluster"]})

@app.get("/api/meta")
def meta():
    con = db()
    out = {}
    for c in con.execute("SELECT * FROM clusters ORDER BY name").fetchall():
        places = [dict(p) for p in con.execute("SELECT id, village, gram_panchayat FROM places WHERE cluster_id=?", (c["id"],)).fetchall()]
        out[c["name"]] = {"id": c["id"], "district": c["district"], "block": c["block"], "places": places}
    con.close()
    return jsonify(out)

@app.post("/api/clusters")
@admin_required
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
@admin_required
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
        return jsonify({"error": "नया पासवर्ड कम से कम 8 अक्षर"}), 400
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
@admin_required
def admin_reset():
    data = request.json or {}
    password = data.get("password", "")
    if len(password) < 8:
        return jsonify({"error": "पासवर्ड कम से कम 8 अक्षर"}), 400
    con = db()
    con.execute("UPDATE users SET password_hash=? WHERE username=?", (generate_password_hash(password), data.get("username")))
    con.execute("UPDATE resets SET status='done' WHERE username=?", (data.get("username"),))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.post("/api/users")
@admin_required
def add_user():
    data = request.json or {}
    if data.get("role") not in ("admin", "krishi_sakhi", "crp"):
        return jsonify({"error": "रोल गलत है"}), 400
    if len(data.get("password", "")) < 8:
        return jsonify({"error": "पासवर्ड कम से कम 8 अक्षर"}), 400
    mobile = (data.get("mobile") or "").strip()
    if len(mobile) < 10:
        return jsonify({"error": "मोबाइल नंबर जरूरी है"}), 400
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
        return jsonify({"error": "यूजरनेम पहले से है"}), 400
    con.close()
    link = os.environ.get("SITE_URL", "https://ajmer-krishi.onrender.com")
    text = f"Ajmer Agriculture Information\nLink: {link}\nUsername: {data['username'].strip()}\nPassword: {data['password']}\nCluster: {data.get('cluster') or 'All'}"
    sent = send_text(mobile, text)
    return jsonify({"ok": True, "sms": sent})

@app.get("/api/users")
@admin_required
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

def visible_sql(u):
    if u["role"] == "admin":
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
    con.close()
    return jsonify(rows)

@app.post("/api/records")
@login_required
def create_record():
    u = current()
    data = request.json or {}
    cluster = u["cluster"] if u["role"] != "admin" else (data.get("cluster") or u["cluster"])
    if u["role"] != "admin" and data.get("cluster") and data.get("cluster") != u["cluster"]:
        return jsonify({"error": "आप केवल अपने क्लस्टर का डेटा भर सकते हैं"}), 403
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
    if not rec or (u["role"] != "admin" and rec["cluster"] != u["cluster"]):
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
    if not row or (u["role"] != "admin" and row["cluster"] != u["cluster"]):
        return jsonify({"error": "नहीं"}), 404
    return send_from_directory(UPLOAD, row["filename"])

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

@app.post("/api/excel")
@login_required
def excel_upload():
    u = current()
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "फाइल नहीं"}), 400
    wb = load_workbook(f, data_only=True)
    ws = wb.active
    headers = [str(c.value or "").strip() for c in next(ws.iter_rows(max_row=1))]
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
        item = {
            "farmer": row[idx["farmer"]] if idx["farmer"] is not None else "",
            "crop": row[idx["crop"]] if idx["crop"] is not None else "",
            "kind": row[idx["kind"]] if idx["kind"] is not None else "",
            "area": row[idx["area"]] if idx["area"] is not None else "",
            "yield": row[idx["yield"]] if idx["yield"] is not None else "",
            "yieldType": row[idx["ytype"]] if idx["ytype"] is not None else "अनुमानित",
        }
        missing = [k for k in REQUIRED if str(item.get(k) or "").strip() == ""]
        key = norm(item["farmer"]) + "|" + norm(item["crop"])
        if missing:
            errors.append({"row": n, "type": "error", "detail": ", ".join(missing) + " खाली"})
            continue
        if key in seen:
            errors.append({"row": n, "type": "duplicate", "detail": "किसान+फसल पहले से है"})
            continue
        seen.add(key)
        clean.append(item)
    saved = []
    now = datetime.now().isoformat(timespec="seconds")
    for item in clean:
        rid = uuid.uuid4().hex
        con.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, u["id"], u["name"], u["role"], u["cluster"], "", "Excel परिशिष्ट 8",
             json.dumps(item, ensure_ascii=False), "submitted", now),
        )
        add_history(con, rid, "submitted", "Excel से जमा", u["name"])
        saved.append(rid)
    con.commit()
    con.close()
    return jsonify({"saved": len(saved), "errors": errors})

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
