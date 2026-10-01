
import os
import ssl
import smtplib
import secrets
import hashlib
import hmac
from email.message import EmailMessage
from datetime import datetime, timedelta

import pyodbc
from flask import Flask, render_template, render_template_string, request, redirect, url_for, session

app = Flask(__name__)
app.secret_key = os.environ.get("SHANTIRAMAN_SECRET_KEY", "")

# ============================================================
# DATABASE
# ============================================================
def get_connection():
    return pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};"
        "SERVER=(local);"
        "DATABASE=ShantiramanDB;"
        "Trusted_Connection=yes;"
        "TrustServerCertificate=yes;",
        timeout=5
    )

# ============================================================
# SCHOOL NAME
# ============================================================
SCHOOL_LIST = {
    "ramraji": "RAMRAJI SARASWATI BALIKA VIDYA MANDIR INTER COLLEGE"
}

SCHOOL_SELECT_HTML = """
<!doctype html>
<html lang="hi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>School Select - Shantiraman ERP</title>
<style>
body{margin:0;font-family:Arial,sans-serif;background:#f3f6fb;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{width:min(520px,92%);background:#fff;border-radius:16px;padding:30px;box-shadow:0 8px 30px rgba(0,0,0,.12)}
h1{text-align:center;margin:0 0 8px;color:#1d3557;font-size:25px}
p{text-align:center;color:#666;margin:0 0 25px}
label{font-weight:700;display:block;margin-bottom:8px}
select,button{width:100%;box-sizing:border-box;padding:13px;border-radius:9px;font-size:16px}
select{border:1px solid #bbb;background:#fff;margin-bottom:18px}
button{border:0;background:#1769aa;color:#fff;font-weight:700;cursor:pointer}
button:disabled{background:#aaa;cursor:not-allowed}
</style>
</head>
<body>
<div class="card">
<h1>Shantiraman Mobile ERP</h1>
<p>कृपया अपना School Select करें</p>
<label for="school">School</label>
<select id="school">
<option value="">-- Select School --</option>
{% for code, name in school_list.items() %}
<option value="{{ code }}">{{ name }}</option>
{% endfor %}
</select>
<button id="continueBtn" disabled onclick="continueERP()">Continue</button>
</div>
<script>
const school = document.getElementById("school");
const btn = document.getElementById("continueBtn");
school.addEventListener("change", function(){ btn.disabled = !this.value; });
function continueERP(){
    if(!school.value) return;
    window.location.href = "/login?school=" + encodeURIComponent(school.value);
}
</script>
</body>
</html>
"""

def get_school_name():
    code = request.args.get("school", "").strip().lower()
    if code in SCHOOL_LIST:
        session["school_code"] = code
        session["school_name"] = SCHOOL_LIST[code]
        return SCHOOL_LIST[code]

    if session.get("school_name"):
        return session["school_name"]

    name = request.args.get("school_name", "").strip()
    if not name:
        name = SCHOOL_LIST["ramraji"]
        session["school_code"] = "ramraji"

    session["school_name"] = name
    return name

# ============================================================
# PASSWORD HASH
# ============================================================
def hash_mobile_password(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 200000
    )
    return salt.hex() + ":" + digest.hex()

def verify_mobile_password(password, stored_hash):
    try:
        if not stored_hash or ":" not in str(stored_hash):
            return False
        salt_hex, digest_hex = str(stored_hash).strip().split(":", 1)
        salt = bytes.fromhex(salt_hex)
        new_digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, 200000
        )
        return hmac.compare_digest(new_digest.hex(), digest_hex)
    except Exception:
        return False

def normalize_role(role):
    value = str(role or "").strip().lower().replace(" ", "")
    if value in ("classteacher", "class_teacher"):
        return "classteacher"
    if value == "teacher":
        return "teacher"
    if value in ("admin", "administrator", "superadmin"):
        return "admin"
    return value

# ============================================================
# MOBILE USER
# ============================================================
def get_mobile_login_user(email):
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT M.ID, M.EmployeeID, M.EmailID, M.PasswordHash,
                   M.Designation, M.Role, M.IsActive, M.PasswordChanged,
                   E.Name
            FROM dbo.MobileLoginUser M
            INNER JOIN dbo.EmployeeMaster E ON E.ID = M.EmployeeID
            WHERE LOWER(LTRIM(RTRIM(M.EmailID))) =
                  LOWER(LTRIM(RTRIM(?)))
        """, email)
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "employee_id": row[1],
            "email": str(row[2] or "").strip(),
            "password_hash": row[3],
            "designation": str(row[4] or "").strip(),
            "role": str(row[5] or "").strip(),
            "is_active": bool(row[6]),
            "password_changed": bool(row[7]),
            "name": str(row[8] or "").strip()
        }
    finally:
        if cur: cur.close()
        if conn: conn.close()

def update_last_login(employee_id):
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE dbo.MobileLoginUser
            SET LastLoginDate=GETDATE(), UpdateDate=GETDATE()
            WHERE EmployeeID=?
        """, employee_id)
        conn.commit()
    finally:
        if cur: cur.close()
        if conn: conn.close()

# ============================================================
# OTP
# ============================================================
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_EMAIL = "shanteeraman@gmail.com"
# IMPORTANT: put your existing Gmail App Password locally here,
# without spaces. Never share it in chat.
SMTP_APP_PASSWORD = os.environ.get("SHANTIRAMAN_SMTP_APP_PASSWORD", "")
# अपना existing Gmail App Password स्थानीय environment variable में रखें।
OTP_VALID_MINUTES = 5
FORGOT_OTP = {}

def send_mobile_otp(receiver_email, username, otp):
    if not SMTP_EMAIL or not SMTP_APP_PASSWORD or \
       SMTP_APP_PASSWORD == "YOUR_GMAIL_APP_PASSWORD":
        raise RuntimeError("SMTP App Password app.py में set करें।")

    msg = EmailMessage()
    msg["Subject"] = "Shantiraman ERP - Password Reset OTP"
    msg["From"] = SMTP_EMAIL
    msg["To"] = receiver_email
    msg.set_content(
        f"""Dear {username},

Your Shantiraman ERP password reset OTP is:

{otp}

This OTP is valid for {OTP_VALID_MINUTES} minutes.
Please do not share this OTP with anyone.

Regards,
Shantiraman ERP
"""
    )
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        SMTP_HOST, SMTP_PORT, context=context, timeout=20
    ) as server:
        server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
        server.send_message(msg)

# ============================================================
# LOGIN
# ============================================================
@app.route("/", methods=["GET", "POST"])
def home():
    selected_school = request.args.get("school", "").strip().lower()

    # Bare URL = school selection page.
    if request.method == "GET" and not selected_school and not session.get("school_code"):
        session.clear()
        return render_template_string(SCHOOL_SELECT_HTML, school_list=SCHOOL_LIST)

    if selected_school:
        if selected_school not in SCHOOL_LIST:
            return redirect(url_for("home"))
        session["school_code"] = selected_school
        session["school_name"] = SCHOOL_LIST[selected_school]

    school_name = get_school_name()
    error = None
    reset_success = session.pop("password_reset_success", False)

    if request.method == "POST":
        role = request.form.get("role", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        if not role:
            error = "कृपया Role Select करें।"
        elif not email:
            error = "कृपया Email ID डालें।"
        elif not password:
            error = "कृपया Password डालें।"
        else:
            user = get_mobile_login_user(email)
            if not user:
                error = "यह Email ID Mobile User Setup में उपलब्ध नहीं है।"
            elif not user["is_active"]:
                error = "आपका Mobile Login बंद है।"
            elif not user["password_hash"]:
                error = "इस Mobile User का Password Setup नहीं है।"
            elif normalize_role(role) != normalize_role(user["role"]):
                error = "चयनित Role इस Mobile User के Role से मेल नहीं खाता।"
            elif not verify_mobile_password(password, user["password_hash"]):
                error = "Password गलत है।"
            else:
                session["user_id"] = str(user["employee_id"])
                session["employee_id"] = str(user["employee_id"])
                session["user_name"] = user["name"]
                session["user_email"] = user["email"]
                session["designation"] = user["designation"]
                session["role"] = user["role"]
                if not user["password_changed"]:
                    return redirect(url_for("change_password"))
                update_last_login(user["employee_id"])
                return redirect(url_for("teacher_dashboard"))

    return render_template(
        "login.html",
        error=error,
        reset_success=reset_success,
        school_name=school_name
    )

# ============================================================
# SCHOOL SELECT / LOGIN ALIASES
# ============================================================
@app.route("/select-school", methods=["GET"])
def select_school():
    session.clear()
    return render_template_string(SCHOOL_SELECT_HTML, school_list=SCHOOL_LIST)

@app.route("/login", methods=["GET", "POST"])
def login_alias():
    # /login?school=ramraji = actual school login page
    return home()

@app.route("/school-login", methods=["GET", "POST"])
def school_login():
    return home()

@app.route("/version")
def app_version():
    return "CLASS_TEACHER_LOGIN_FIXED_V2"

def get_class_teacher_assignment_user(email):
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT TOP 1
                E.ID, E.Name, E.EmailID, E.Moble, E.Designation,
                C.Class, C.Section, C.Session
            FROM dbo.ClassTeacherAssign C
            INNER JOIN dbo.EmployeeMaster E
                ON TRY_CONVERT(int, C.EmployeeID) = E.ID
            WHERE C.IsActive = 1
              AND LOWER(LTRIM(RTRIM(E.EmailID))) = LOWER(LTRIM(RTRIM(?)))
            ORDER BY C.ID DESC
        """, email)
        row = cur.fetchone()
        if not row:
            return None
        return {
            'employee_id': row[0],
            'name': str(row[1] or '').strip(),
            'email': str(row[2] or '').strip(),
            'mobile': str(row[3] or '').strip(),
            'designation': str(row[4] or '').strip(),
            'class_name': str(row[5] or '').strip(),
            'section': str(row[6] or '').strip(),
            'session': str(row[7] or '').strip()
        }
    finally:
        if cur: cur.close()
        if conn: conn.close()

# ============================================================
# CHANGE PASSWORD
# ============================================================
@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    if "employee_id" not in session:
        return redirect(url_for("home"))

    school_name = get_school_name()
    error = None

    if request.method == "POST":
        new_password = request.form.get("new_password", "").strip()
        confirm = request.form.get("confirm_password", "").strip()

        if not new_password:
            error = "नया Password डालें।"
        elif len(new_password) < 6:
            error = "Password कम से कम 6 characters का होना चाहिए।"
        elif new_password != confirm:
            error = "दोनों Password समान नहीं हैं।"
        else:
            conn = cur = None
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute("""
                    UPDATE dbo.MobileLoginUser
                    SET PasswordHash=?, PasswordChanged=1, UpdateDate=GETDATE()
                    WHERE EmployeeID=?
                """, hash_mobile_password(new_password),
                session["employee_id"])
                conn.commit()
                update_last_login(session["employee_id"])
                return redirect(url_for("teacher_dashboard"))
            except Exception as e:
                error = "Password update नहीं हो पाया: " + str(e)
            finally:
                if cur: cur.close()
                if conn: conn.close()

    return render_template("change_password.html",
                           error=error, school_name=school_name)

# ============================================================
# FORGOT PASSWORD -> EMAIL -> OTP -> NEW PASSWORD
# ============================================================
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    school_name = get_school_name()
    error = None

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        if not email:
            error = "Email ID डालें।"
        else:
            user = get_mobile_login_user(email)
            if not user:
                error = "यह Email ID Mobile User Setup में उपलब्ध नहीं है।"
            elif not user["is_active"]:
                error = "यह Mobile Login बंद है।"
            else:
                otp = f"{secrets.randbelow(1000000):06d}"
                try:
                    send_mobile_otp(user["email"], user["name"], otp)
                    FORGOT_OTP[user["email"].lower()] = {
                        "otp": otp,
                        "created": datetime.now(),
                        "employee_id": user["employee_id"],
                        "name": user["name"]
                    }
                    session["forgot_email"] = user["email"]
                    return redirect(url_for("verify_forgot_otp"))
                except Exception as e:
                    error = "OTP भेजा नहीं जा सका: " + str(e)

    return render_template("forgot_password.html",
                           stage="email", error=error,
                           school_name=school_name)

@app.route("/verify-forgot-otp", methods=["GET", "POST"])
def verify_forgot_otp():
    school_name = get_school_name()
    email = session.get("forgot_email")
    error = None

    if not email:
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        otp = request.form.get("otp", "").strip()
        data = FORGOT_OTP.get(email.lower())

        if not data:
            error = "OTP नहीं मिला। फिर से Forgot Password करें।"
        elif datetime.now() - data["created"] > timedelta(minutes=OTP_VALID_MINUTES):
            FORGOT_OTP.pop(email.lower(), None)
            error = "OTP expire हो गया। नया OTP लें।"
        elif not hmac.compare_digest(str(otp), str(data["otp"])):
            error = "OTP गलत है।"
        else:
            session["forgot_verified"] = True
            return redirect(url_for("reset_forgot_password"))

    return render_template("forgot_password.html",
                           stage="otp", email=email,
                           error=error, school_name=school_name)

@app.route("/reset-forgot-password", methods=["GET", "POST"])
def reset_forgot_password():
    school_name = get_school_name()
    email = session.get("forgot_email")

    if not email or not session.get("forgot_verified"):
        return redirect(url_for("forgot_password"))

    error = None

    if request.method == "POST":
        new_password = request.form.get("new_password", "").strip()
        confirm = request.form.get("confirm_password", "").strip()

        if len(new_password) < 6:
            error = "Password कम से कम 6 characters का होना चाहिए।"
        elif new_password != confirm:
            error = "दोनों Password समान नहीं हैं।"
        else:
            conn = cur = None
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute("""
                    UPDATE dbo.MobileLoginUser
                    SET PasswordHash=?, PasswordChanged=1, UpdateDate=GETDATE()
                    WHERE LOWER(LTRIM(RTRIM(EmailID))) =
                          LOWER(LTRIM(RTRIM(?)))
                """, hash_mobile_password(new_password), email)
                conn.commit()

                FORGOT_OTP.pop(email.lower(), None)
                session.pop("forgot_email", None)
                session.pop("forgot_verified", None)
                session["password_reset_success"] = True
                return redirect(url_for("home"))
            except Exception as e:
                error = "Password reset नहीं हो पाया: " + str(e)
            finally:
                if cur: cur.close()
                if conn: conn.close()

    return render_template("forgot_password.html",
                           stage="reset", email=email,
                           error=error, school_name=school_name)

# ============================================================
# DASHBOARD
# ============================================================
@app.route("/teacher-dashboard")
def teacher_dashboard():
    if "employee_id" not in session:
        return redirect(url_for("home"))

    return render_template(
        "teacher_dashboard.html",
        teacher_name=session.get("user_name"),
        designation=session.get("designation"),
        role=session.get("role"),
        school_name=get_school_name()
    )

# ============================================================
# EXAM / MARKS
# ============================================================
def get_active_session():
    """
    SessionTable से Active Session निकाले।
    अलग-अलग पुराने DB versions में status column का नाम
    SessStatus / Status / IsActive हो सकता है, इसलिए schema के
    अनुसार query बनाई जाती है।
    """
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        # SessionTable के उपलब्ध columns पता करें
        cur.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='SessionTable'
        """)
        cols = {str(r[0]).strip().lower(): str(r[0]).strip()
                for r in cur.fetchall()}

        id_col = cols.get("id")
        session_col = (
            cols.get("session")
            or cols.get("sessionname")
            or cols.get("session_name")
        )
        status_col = (
            cols.get("sessstatus")
            or cols.get("status")
            or cols.get("isactive")
        )

        if not id_col or not session_col:
            return "", ""

        # Status column उपलब्ध है तो उसी के अनुसार Active Session खोजें
        if status_col:
            if status_col.lower() == "isactive":
                sql = f"""
                    SELECT TOP 1 [{id_col}], [{session_col}]
                    FROM dbo.SessionTable
                    WHERE [{status_col}] IN (1, '1', 'True', 'true', 'Yes', 'yes')
                    ORDER BY [{id_col}] DESC
                """
                cur.execute(sql)
            else:
                sql = f"""
                    SELECT TOP 1 [{id_col}], [{session_col}]
                    FROM dbo.SessionTable
                    WHERE LOWER(LTRIM(RTRIM(CONVERT(VARCHAR(100), [{status_col}]))))
                          IN ('active','1','true','yes')
                    ORDER BY [{id_col}] DESC
                """
                cur.execute(sql)

            row = cur.fetchone()
            if row and str(row[1] or "").strip():
                return str(row[0]).strip(), str(row[1]).strip()

        return "", ""

    except Exception:
        return "", ""
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def teacher_exam_assignments(employee_id, role):
    """Return teacher assignments as 5-tuples:
       (Class, Section, Subject, Exam, SubSubject).
       Admin returns None = unrestricted access.
    """
    if normalize_role(role) == "admin":
        return None

    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        try:
            cur.execute("""
                SELECT DISTINCT
                    LTRIM(RTRIM(ISNULL(Class,''))),
                    LTRIM(RTRIM(ISNULL(Section,''))),
                    LTRIM(RTRIM(ISNULL(Subject,''))),
                    LTRIM(RTRIM(ISNULL(Exam,''))),
                    LTRIM(RTRIM(ISNULL(SubSubject,'')))
                FROM dbo.MobileTeacherSubjectAssign
                WHERE EmployeeID=? AND IsActive=1
                ORDER BY
                    LTRIM(RTRIM(ISNULL(Class,''))),
                    LTRIM(RTRIM(ISNULL(Section,''))),
                    LTRIM(RTRIM(ISNULL(Subject,''))),
                    LTRIM(RTRIM(ISNULL(Exam,''))),
                    LTRIM(RTRIM(ISNULL(SubSubject,'')))
            """, employee_id)
            rows = cur.fetchall()
            return [tuple(str(x or "").strip() for x in r[:5]) for r in rows]

        except Exception:
            # पुराने DB में SubSubject column न हो तो 4-column fallback.
            try:
                conn.rollback()
            except Exception:
                pass

            cur.execute("""
                SELECT DISTINCT
                    LTRIM(RTRIM(ISNULL(Class,''))),
                    LTRIM(RTRIM(ISNULL(Section,''))),
                    LTRIM(RTRIM(ISNULL(Subject,''))),
                    LTRIM(RTRIM(ISNULL(Exam,'')))
                FROM dbo.MobileTeacherSubjectAssign
                WHERE EmployeeID=? AND IsActive=1
                ORDER BY
                    LTRIM(RTRIM(ISNULL(Class,''))),
                    LTRIM(RTRIM(ISNULL(Section,''))),
                    LTRIM(RTRIM(ISNULL(Subject,''))),
                    LTRIM(RTRIM(ISNULL(Exam,'')))
            """, employee_id)
            return [
                (str(r[0] or "").strip(), str(r[1] or "").strip(),
                 str(r[2] or "").strip(), str(r[3] or "").strip(), "")
                for r in cur.fetchall()
            ]
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def master_exam_types():
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT ID, ItemName, [Order]
            FROM dbo.MasterTable
            WHERE LTRIM(RTRIM(ISNULL(Category,''))) = 'Exam Type'
            ORDER BY TRY_CONVERT(INT,[Order]), ID
        """)
        return [
            {"id": r[0], "value": str(r[1] or "").strip(),
             "order": r[2]}
            for r in cur.fetchall()
        ]
    finally:
        if cur: cur.close()
        if conn: conn.close()

@app.route("/exam-marks", methods=["GET", "POST"])
def exam_marks():
    if "employee_id" not in session:
        return redirect(url_for("home"))

    role = session.get("role", "")
    employee_id = session["employee_id"]
    school_name = get_school_name()
    session_id, session_name = get_active_session()
    exams = master_exam_types()
    assignments = teacher_exam_assignments(employee_id, role)

    selected_exam = request.values.get("exam", "").strip()
    selected_class = request.values.get("class_name", "").strip()
    selected_section = request.values.get("section", "").strip()
    selected_subject = request.values.get("subject", "").strip()
    selected_subsubject = request.values.get("subsubject", "").strip()

    error = None
    success = None
    students = []
    subjects = []
    subsubjects = []
    max_marks = ""
    classes = []
    sections = []

    def norm(v):
        return str(v or "").strip().lower()

    # Admin = all SubjectSetup data.
    # Teacher = only MobileTeacherSubjectAssign data.
    allowed = None if assignments is None else assignments

    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        # ----------------------------------------------------
        # 1. CLASS LIST - Exam के बाद
        # ----------------------------------------------------
        if allowed is None:
            if selected_exam:
                cur.execute("""
                    SELECT DISTINCT LTRIM(RTRIM(Class))
                    FROM dbo.SubjectSetup
                    WHERE LTRIM(RTRIM(ISNULL(ExamType,'')))=?
                      AND LTRIM(RTRIM(ISNULL(Class,'')))<>''
                    ORDER BY LTRIM(RTRIM(Class))
                """, selected_exam)
            else:
                cur.execute("""
                    SELECT DISTINCT LTRIM(RTRIM(Class))
                    FROM dbo.SubjectSetup
                    WHERE LTRIM(RTRIM(ISNULL(Class,'')))<>''
                    ORDER BY LTRIM(RTRIM(Class))
                """)
            classes = [str(r[0]).strip() for r in cur.fetchall()]
        else:
            # केवल उसी exam के assigned classes
            exam_rows = [a for a in allowed
                         if not selected_exam or norm(a[3]) == norm(selected_exam)]
            classes = sorted({a[0] for a in exam_rows if a[0]})

        # ----------------------------------------------------
        # 2. SECTION LIST - Exam + Class के बाद
        # ----------------------------------------------------
        if selected_class:
            if allowed is None:
                if selected_exam:
                    cur.execute("""
                        SELECT DISTINCT LTRIM(RTRIM(Section))
                        FROM dbo.SubjectSetup
                        WHERE LTRIM(RTRIM(ISNULL(ExamType,'')))=?
                          AND LTRIM(RTRIM(ISNULL(Class,'')))=?
                          AND LTRIM(RTRIM(ISNULL(Section,'')))<>''
                        ORDER BY LTRIM(RTRIM(Section))
                    """, selected_exam, selected_class)
                else:
                    cur.execute("""
                        SELECT DISTINCT LTRIM(RTRIM(Section))
                        FROM dbo.SubjectSetup
                        WHERE LTRIM(RTRIM(ISNULL(Class,'')))=?
                          AND LTRIM(RTRIM(ISNULL(Section,'')))<>''
                        ORDER BY LTRIM(RTRIM(Section))
                    """, selected_class)
                sections = [str(r[0]).strip() for r in cur.fetchall()]
            else:
                sections = sorted({a[1] for a in allowed
                                   if a[0] and norm(a[0]) == norm(selected_class)
                                   and (not selected_exam or norm(a[3]) == norm(selected_exam))
                                   and a[1]})

        # ----------------------------------------------------
        # ----------------------------------------------------
        # 3. SUBJECT LIST - Exam + Class + Section के बाद
        # ----------------------------------------------------
        if selected_exam and selected_class and selected_section:

            # ====================================================
            # ADMIN
            # ====================================================
            if allowed is None:
                cur.execute("""
                    SELECT DISTINCT
                        LTRIM(RTRIM(ISNULL(Subject,''))),
                        LTRIM(RTRIM(ISNULL(SubSubject,''))),
                        LTRIM(RTRIM(CONVERT(VARCHAR(50), ISNULL(MaxMarks, 0))))
                    FROM dbo.SubjectSetup
                    WHERE LTRIM(RTRIM(ISNULL(ExamType,'')))=?
                      AND LTRIM(RTRIM(ISNULL(Class,'')))=?
                      AND LTRIM(RTRIM(ISNULL(Section,'')))=?
                      AND LTRIM(RTRIM(ISNULL(Subject,'')))<>''
                    ORDER BY
                        LTRIM(RTRIM(ISNULL(Subject,''))),
                        LTRIM(RTRIM(ISNULL(SubSubject,'')))
                """, selected_exam, selected_class, selected_section)
                setup_rows = cur.fetchall()

            # ====================================================
            # TEACHER
            # Subject सीधे MobileTeacherSubjectAssign से आएगा।
            # ====================================================
            else:
                cur.execute("""
                    SELECT DISTINCT
                        LTRIM(RTRIM(ISNULL(A.Subject,''))) AS SubjectName,
                        LTRIM(RTRIM(ISNULL(A.SubSubject,''))) AS SubSubjectName
                    FROM dbo.MobileTeacherSubjectAssign A
                    WHERE A.EmployeeID = ?
                      AND ISNULL(A.IsActive,0) = 1
                      AND LTRIM(RTRIM(ISNULL(A.SessionID,''))) = ?
                      AND LTRIM(RTRIM(ISNULL(A.Class,''))) = ?
                      AND LTRIM(RTRIM(ISNULL(A.Section,''))) = ?
                      AND LTRIM(RTRIM(ISNULL(A.Exam,''))) = ?
                      AND LTRIM(RTRIM(ISNULL(A.Subject,''))) <> ''
                    ORDER BY
                        LTRIM(RTRIM(ISNULL(A.Subject,''))),
                        LTRIM(RTRIM(ISNULL(A.SubSubject,'')))
                """, employee_id, session_id, selected_class, selected_section, selected_exam)
                teacher_rows = cur.fetchall()

                cur.execute("""
                    SELECT
                        LTRIM(RTRIM(ISNULL(Subject,''))),
                        LTRIM(RTRIM(ISNULL(SubSubject,''))),
                        LTRIM(RTRIM(CONVERT(VARCHAR(50), ISNULL(MaxMarks, 0))))
                    FROM dbo.SubjectSetup
                    WHERE LTRIM(RTRIM(ISNULL(ExamType,'')))=?
                      AND LTRIM(RTRIM(ISNULL(Class,'')))=?
                      AND LTRIM(RTRIM(ISNULL(Section,'')))=?
                      AND LTRIM(RTRIM(ISNULL(Subject,'')))<>''
                """, selected_exam, selected_class, selected_section)
                setup_rows = cur.fetchall()

                maxmarks_map = {}
                for r in setup_rows:
                    key = (str(r[0] or '').strip().lower(), str(r[1] or '').strip().lower())
                    maxmarks_map[key] = str(r[2] or '').strip()

                subjects = []
                seen = set()
                for r in teacher_rows:
                    subject_name = str(r[0] or '').strip()
                    subsubject_name = str(r[1] or '').strip()
                    key = (subject_name.lower(), subsubject_name.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    subjects.append({
                        'subject': subject_name,
                        'subsubject': subsubject_name,
                        'maxmarks': maxmarks_map.get(key, '')
                    })

            # ====================================================
            # ADMIN SUBJECTS
            # ====================================================
            if allowed is None:
                subjects = []
                seen = set()
                for r in setup_rows:
                    subject_name = str(r[0] or '').strip()
                    subsubject_name = str(r[1] or '').strip()
                    max_mark_value = str(r[2] or '').strip()
                    key = (subject_name.lower(), subsubject_name.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    subjects.append({
                        'subject': subject_name,
                        'subsubject': subsubject_name,
                        'maxmarks': max_mark_value
                    })

            if allowed is not None and not subjects:
                error = (
                    'इस Exam/Class/Section के लिए इस Teacher का '
                    'Subject Assignment नहीं मिला।'
                )

            subsubjects = sorted({
                x['subsubject']
                for x in subjects
                if x['subsubject']
                and (not selected_subject or norm(x['subject']) == norm(selected_subject))
            })

            # ------------------------------------------------
            # 4. EXACT MAX MARKS
            # ------------------------------------------------
            if selected_subject and selected_subsubject and not error:
                if allowed is not None:
                    assignment_ok = any(
                        norm(a[0]) == norm(selected_class)
                        and norm(a[1]) == norm(selected_section)
                        and norm(a[2]) == norm(selected_subject)
                        and norm(a[3]) == norm(selected_exam)
                        and (not norm(a[4]) or norm(a[4]) == norm(selected_subsubject))
                        for a in allowed
                    )
                    if not assignment_ok:
                        error = 'इस Teacher को चुने हुए Subject/SubSubject की अनुमति नहीं है।'

            if selected_subject and selected_subsubject and not error:
                cur.execute("""
                    SELECT TOP 1 CONVERT(VARCHAR(50), MaxMarks)
                    FROM dbo.SubjectSetup
                    WHERE LTRIM(RTRIM(ISNULL(ExamType,'')))=?
                      AND LTRIM(RTRIM(ISNULL(Class,'')))=?
                      AND LTRIM(RTRIM(ISNULL(Section,'')))=?
                      AND LTRIM(RTRIM(ISNULL(Subject,'')))=?
                      AND LTRIM(RTRIM(ISNULL(SubSubject,'')))=?
                    ORDER BY TRY_CONVERT(BIGINT, ID) DESC
                """, selected_exam, selected_class, selected_section, selected_subject, selected_subsubject)
                mm_row = cur.fetchone()
                if mm_row and mm_row[0] is not None:
                    max_marks = str(mm_row[0]).strip()
                else:
                    error = 'Selected Subject/SubSubject का setup उपलब्ध नहीं है।'

            # ----------------------------------------------------
            # 5. STUDENT LIST
            # ------------------------------------------------
            if selected_subject and selected_subsubject and max_marks and not error:
                if not session_id:
                    error = "Active Session नहीं मिली। पहले SessionTable में Active Session सेट करें।"
                else:
                    cur.execute("""
                        SELECT sm.ID, sm.SrNo, sm.Student
                        FROM dbo.StudentMaster sm
                        INNER JOIN dbo.StudentAcademic sa
                            ON sa.StudentID = sm.ID
                        WHERE sa.SessionID = ?
                          AND LTRIM(RTRIM(ISNULL(sa.Class,''))) = ?
                          AND LTRIM(RTRIM(ISNULL(sa.Section,''))) = ?
                          AND (
                              sa.SessStatus IS NULL
                              OR LOWER(LTRIM(RTRIM(CONVERT(VARCHAR(100), sa.SessStatus)))) IN
                                 ('active','1','true','yes')
                          )
                        ORDER BY TRY_CONVERT(INT,sm.SrNo), sm.SrNo
                    """, session_id, selected_class, selected_section)

                    for r in cur.fetchall():
                        students.append({
                            "id": str(r[0]).strip(),
                            "srno": str(r[1] or "").strip(),
                            "name": str(r[2] or "").strip(),
                            "studentid": str(r[0]).strip(),
                            "obtmarks": ""
                        })

                    # Existing marks: एक ही query में सभी students के marks load करें
                    student_ids = [st["id"] for st in students]
                    if student_ids:
                        placeholders = ",".join("?" for _ in student_ids)
                        cur.execute(f"""
                            SELECT StudentID, ObtMarks
                            FROM (
                                SELECT StudentID, ObtMarks,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY StudentID
                                           ORDER BY ID DESC
                                       ) AS rn
                                FROM dbo.ExamMarks
                                WHERE LTRIM(RTRIM(ISNULL(SessionName,'')))=?
                                  AND LTRIM(RTRIM(ISNULL(ExamType,'')))=?
                                  AND LTRIM(RTRIM(ISNULL(ClassName,'')))=?
                                  AND LTRIM(RTRIM(ISNULL(SectionName,'')))=?
                                  AND LTRIM(RTRIM(ISNULL(SubjectName,'')))=?
                                  AND LTRIM(RTRIM(ISNULL(SubSubject,'')))=?
                                  AND LTRIM(RTRIM(ISNULL(StudentID,''))) IN ({placeholders})
                            ) x
                            WHERE rn=1
                        """, session_name, selected_exam, selected_class,
                             selected_section, selected_subject, selected_subsubject, *student_ids)
                        marks_map = {str(r[0]).strip(): ("" if r[1] is None else str(r[1]).strip())
                                     for r in cur.fetchall()}
                        for st in students:
                            st["obtmarks"] = marks_map.get(st["id"], "")

                    # --------------------------------------------
                    # 6. SAVE / UPDATE MARKS
                    # --------------------------------------------
                    if request.method == "POST" and request.form.get("action") == "save":
                        for st in students:
                            value = request.form.get("marks_" + st["id"], "").strip()
                            if value == "":
                                continue
                            try:
                                mm = float(max_marks)
                                om = float(value)
                                if om < 0 or om > mm:
                                    raise ValueError
                            except Exception:
                                error = (
                                    f"{st['name']} के Obtained Marks "
                                    f"0 से {max_marks} के बीच होने चाहिए।"
                                )
                                break

                        if not error:
                            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            for st in students:
                                value = request.form.get("marks_" + st["id"], "").strip()
                                if value == "":
                                    continue

                                cur.execute("""
                                    SELECT TOP 1 ID
                                    FROM dbo.ExamMarks
                                    WHERE LTRIM(RTRIM(ISNULL(StudentID,'')))=?
                                      AND LTRIM(RTRIM(ISNULL(SessionName,'')))=?
                                      AND LTRIM(RTRIM(ISNULL(ExamType,'')))=?
                                      AND LTRIM(RTRIM(ISNULL(ClassName,'')))=?
                                      AND LTRIM(RTRIM(ISNULL(SectionName,'')))=?
                                      AND LTRIM(RTRIM(ISNULL(SubjectName,'')))=?
                                      AND LTRIM(RTRIM(ISNULL(SubSubject,'')))=?
                                    ORDER BY ID DESC
                                """, st["id"], session_name, selected_exam,
                                     selected_class, selected_section,
                                     selected_subject, selected_subsubject)
                                old = cur.fetchone()

                                if old:
                                    cur.execute("""
                                        UPDATE dbo.ExamMarks
                                        SET SrNo=?, MaxMarks=?, ObtMarks=?,
                                            EntryDate=?, EntryRole=?
                                        WHERE ID=?
                                    """, st["srno"], max_marks, value, now,
                                         session.get("role", ""), old[0])
                                else:
                                    cur.execute("""
                                        INSERT INTO dbo.ExamMarks
                                        (ID,SessionName,ExamType,SrNo,ClassName,
                                         SectionName,SubjectName,SubSubject,
                                         MaxMarks,ObtMarks,EntryDate,EntryRole,StudentID)
                                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                                    """, secrets.token_hex(8), session_name,
                                         selected_exam, st["srno"],
                                         selected_class, selected_section,
                                         selected_subject, selected_subsubject,
                                         max_marks, value, now,
                                         session.get("role", ""), st["id"])

                            conn.commit()
                            success = "Marks successfully save/update हो गए।"
                            for st in students:
                                value = request.form.get("marks_" + st["id"], "").strip()
                                if value != "":
                                    st["obtmarks"] = value

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        error = "Exam / Marks load/save में समस्या: " + str(e)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return render_template(
        "exam_marks.html",
        teacher_name=session.get("user_name"),
        designation=session.get("designation"),
        role=role,
        school_name=school_name,
        exams=exams,
        classes=classes,
        sections=sections,
        subjects=subjects,
        subsubjects=subsubjects,
        students=students,
        max_marks=max_marks,
        selected_exam=selected_exam,
        selected_class=selected_class,
        selected_section=selected_section,
        selected_subject=selected_subject,
        selected_subsubject=selected_subsubject,
        session_name=session_name,
        error=error,
        success=success
    )

# ============================================================
# LOGOUT
# ============================================================
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# ============================================================
# MODULAR ROUTES
# ============================================================
import my_students
import attendance
import my_result
import my_profile
import my_fee

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        use_reloader=False
    )
