
import sys
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
import traceback
app = Flask(__name__)
sys.modules["app"] = sys.modules[__name__]
app.secret_key = os.environ.get("SHANTIRAMAN_SECRET_KEY", "")

# ============================================================
# DATABASE
# ============================================================

# PRINCIPAL_LOGIN_FIX_V1
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
<h1>Shantiraman Mobile Application</h1>
<p>Please Select Your School</p>
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
    if value in ("principal", "principle"):
        return "principal"
    if value == "teacher":
        return "teacher"
    if value in ("admin", "administrator", "superadmin"):
        return "admin"
    return value

# ============================================================
# MOBILE USER
# ============================================================

def _record_principal_login_error(context, exc):
    """Record Principal-only authentication/runtime errors without affecting other roles."""
    import traceback as _traceback
    import os as _os
    details = (
        "\n" + "=" * 72 + "\n"
        + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " | " + str(context) + "\n"
        + _traceback.format_exc() + "\n"
    )
    print(details, flush=True)
    try:
        log_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'mobileerp_error.log')
        with open(log_path, 'a', encoding='utf-8') as log_file:
            log_file.write(details)
    except Exception:
        pass

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

def get_employee_master_principal(email):
    """Find the one Principal in EmployeeMaster using available column names."""
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='EmployeeMaster'
        """)
        cols = {str(row[0]).strip().casefold(): str(row[0]).strip() for row in cur.fetchall()}

        def pick(*names):
            for name in names:
                hit = cols.get(name.casefold())
                if hit:
                    return hit
            return None

        def qi(name):
            return '[' + name.replace(']', ']]') + ']'

        id_col = pick('ID', 'EmployeeID', 'EmpID')
        name_col = pick('Name', 'EmployeeName', 'StaffName')
        email_col = pick('EmailID', 'Email', 'EmailAddress')
        designation_col = pick('Designation', 'DesignationName', 'Post')
        mobile_col = pick('Moble', 'Mobile', 'MobileNo', 'MobileNumber', 'Phone', 'ContactNo', 'ContactNumber')

        if not all([id_col, name_col, email_col, designation_col]):
            missing = [label for label, col in (
                ('ID', id_col), ('Name', name_col), ('Email', email_col),
                ('Designation', designation_col)
            ) if not col]
            return None, 'EmployeeMaster is missing required column(s): ' + ', '.join(missing)

        mobile_expr = f"CONVERT(NVARCHAR(100), {qi(mobile_col)})" if mobile_col else "CAST('' AS NVARCHAR(100))"
        sql = f"""
            SELECT CONVERT(NVARCHAR(100), {qi(id_col)}),
                   CONVERT(NVARCHAR(255), {qi(name_col)}),
                   CONVERT(NVARCHAR(255), {qi(email_col)}),
                   {mobile_expr},
                   CONVERT(NVARCHAR(100), {qi(designation_col)})
            FROM dbo.EmployeeMaster
            WHERE UPPER(LTRIM(RTRIM(ISNULL(CONVERT(NVARCHAR(100), {qi(designation_col)}), '')))) = N'PRINCIPAL'
            ORDER BY {qi(id_col)}
        """
        cur.execute(sql)
        rows = cur.fetchall()
        if not rows:
            return None, 'No EmployeeMaster record with Designation Principal was found.'
        if len(rows) != 1:
            return None, 'More than one Principal designation exists in EmployeeMaster. Keep only one Principal.'

        row = rows[0]
        principal = {
            'employee_id': str(row[0] or '').strip(),
            'name': str(row[1] or '').strip(),
            'email': str(row[2] or '').strip(),
            'mobile': str(row[3] or '').strip(),
            'designation': str(row[4] or 'Principal').strip() or 'Principal',
        }
        if not principal['email']:
            return None, 'The Principal email address is blank in EmployeeMaster.'
        if principal['email'].casefold() != str(email or '').strip().casefold():
            return None, 'The entered email does not match the Principal email in EmployeeMaster.'
        return principal, None
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def get_mobile_login_user_by_employee_id(employee_id):
    """Find an existing mobile account by EmployeeID, even if its email is old."""
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
            WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), M.EmployeeID))) =
                  LTRIM(RTRIM(CONVERT(NVARCHAR(100), ?)))
        """, str(employee_id).strip())
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "employee_id": row[1],
            "email": str(row[2] or "").strip(), "password_hash": row[3],
            "designation": str(row[4] or "").strip(), "role": str(row[5] or "").strip(),
            "is_active": bool(row[6]), "password_changed": bool(row[7]),
            "name": str(row[8] or "").strip(),
        }
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def create_principal_mobile_login(principal):
    """Create first Principal login with EmployeeMaster mobile as temporary password."""
    first_password = "".join(ch for ch in str(principal.get("mobile", "")) if ch.isdigit())
    if not first_password or not principal.get("email"):
        return None
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        # MobileLoginUser.ID is a required string ID in this ERP schema.
        # The existing Mobile Teacher Assign form creates it with token_hex(8).
        cur.execute("""
            INSERT INTO dbo.MobileLoginUser
            (
                ID, EmployeeID, EmailID, PasswordHash,
                Designation, Role, IsActive, PasswordChanged,
                LastLoginDate, EntryDate, UpdateDate
            )
            VALUES (?, ?, ?, ?, ?, 'Principal', 1, 0, NULL, GETDATE(), GETDATE())
        """, secrets.token_hex(8), principal["employee_id"], principal["email"],
             hash_mobile_password(first_password), principal.get("designation", "Principal"))
        conn.commit()
        return get_mobile_login_user(principal["email"])
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        print("PRINCIPAL MOBILE LOGIN CREATE ERROR:", repr(exc))
        return None
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


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
# OTP / GMAIL SMTP
# ============================================================
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_EMAIL = "shanteeraman@gmail.com"

# ============================================================
# >>> à¤•à¥‡à¤µà¤² à¤‡à¤¸à¥€ à¤à¤• à¤œà¤—à¤¹ à¤…à¤ªà¤¨à¤¾ Gmail App Password à¤¡à¤¾à¤²à¥‡à¤‚ <<<
# Google App Password à¤®à¥‡à¤‚ spaces à¤¹à¤Ÿà¤¾à¤•à¤° 16 characters à¤¡à¤¾à¤²à¥‡à¤‚à¥¤
# à¤‡à¤¸à¥‡ à¤•à¤¿à¤¸à¥€ à¤•à¥‡ à¤¸à¤¾à¤¥ share à¤¨ à¤•à¤°à¥‡à¤‚à¥¤
# ============================================================
SMTP_APP_PASSWORD = os.environ.get("SHANTIRAMAN_SMTP_APP_PASSWORD", "")

OTP_VALID_MINUTES = 5
FORGOT_OTP = {}


def send_mobile_otp(receiver_email, username, otp):
    print("========================================")
    print("OTP SEND START")
    print("Receiver Email:", receiver_email)
    print("Username:", username)
    print("SMTP Email:", SMTP_EMAIL)
    print("SMTP Host:", SMTP_HOST)
    print("SMTP Port:", SMTP_PORT)
    print("SMTP Password Available:", bool(SMTP_APP_PASSWORD))
    print("SMTP Password Length:", len(SMTP_APP_PASSWORD or ""))
    print("========================================")

    if not SMTP_EMAIL:
        raise RuntimeError("SMTP Email à¤–à¤¾à¤²à¥€ à¤¹à¥ˆà¥¤")

    if not SMTP_APP_PASSWORD or SMTP_APP_PASSWORD == "PASTE_YOUR_GMAIL_APP_PASSWORD_HERE":
        raise RuntimeError("app.py à¤®à¥‡à¤‚ Gmail App Password à¤¡à¤¾à¤²à¥‡à¤‚à¥¤")

    # App Password à¤®à¥‡à¤‚ accidental spaces à¤¹à¥‹à¤¨à¥‡ à¤ªà¤° à¤‰à¤¨à¥à¤¹à¥‡à¤‚ à¤¹à¤Ÿà¤¾à¤à¤à¥¤
    smtp_password = SMTP_APP_PASSWORD.replace(" ", "").strip()

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

    server = None

    try:
        print("STEP 1: Connecting Gmail SMTP...")

        context = ssl.create_default_context()

        server = smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            context=context,
            timeout=30
        )

        print("STEP 2: Gmail SMTP connection successful")

        server.ehlo()

        print("STEP 3: EHLO successful")

        server.login(
            SMTP_EMAIL,
            smtp_password
        )

        print("STEP 4: Gmail SMTP LOGIN successful")

        server.send_message(msg)

        print("STEP 5: OTP EMAIL SENT SUCCESSFULLY")

    except Exception as e:
        print("========================================")
        print("SMTP ERROR:", repr(e))
        print("ERROR TYPE:", type(e).__name__)
        print("========================================")
        raise

    finally:
        if server is not None:
            try:
                server.quit()
                print("STEP 6: SMTP connection closed")
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass

    print("========================================")
    print("OTP SEND END")
    print("========================================")


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
            error = "à¤•à¥ƒà¤ªà¤¯à¤¾ Role Select à¤•à¤°à¥‡à¤‚à¥¤"
        elif not email:
            error = "à¤•à¥ƒà¤ªà¤¯à¤¾ Email ID à¤¡à¤¾à¤²à¥‡à¤‚à¥¤"
        elif not password:
            error = "à¤•à¥ƒà¤ªà¤¯à¤¾ Password à¤¡à¤¾à¤²à¥‡à¤‚à¥¤"
        elif normalize_role(role) == "admin":
            # Mobile ERP does not provide Admin login; administration stays in desktop ERP.
            error = "Mobile ERP में Admin Login बंद है। कृपया Teacher, Class Teacher या Principal चुनें।"
        else:
            selected_role = normalize_role(role)
            try:
                user = get_mobile_login_user(email)
            except Exception as principal_user_lookup_exc:
                if selected_role != "principal":
                    raise
                _record_principal_login_error("Principal MobileLoginUser email lookup failed", principal_user_lookup_exc)
                user = None
                error = "Principal login could not be completed. Check mobileerp_error.log."

            # ========================================================
            # CLASS TEACHER LOGIN
            # ClassTeacherAssign is the source of Class Teacher status.
            # First login password = EmployeeMaster Mobile No.
            # MobileLoginUser is created automatically on first login.
            # ========================================================
            if selected_role == "principal":
                if error:
                    principal, principal_error = None, error
                else:
                    try:
                        principal, principal_error = get_employee_master_principal(email)
                    except Exception as principal_record_exc:
                        _record_principal_login_error("Principal EmployeeMaster lookup failed", principal_record_exc)
                        principal, principal_error = None, "Principal login could not be completed. Check mobileerp_error.log."
                if not principal:
                    error = principal_error
                else:
                    # An email already attached to a different employee must never be accepted.
                    if user and str(user.get("employee_id")) != str(principal["employee_id"]):
                        error = "यह Email ID किसी दूसरे Employee के Mobile Login से जुड़ी है। Admin से जाँच कराएँ।"

                    # Existing accounts may still have the old email stored in MobileLoginUser.
                    if not error and not user:
                        try:
                            user = get_mobile_login_user_by_employee_id(principal["employee_id"])
                        except Exception as principal_user_id_exc:
                            _record_principal_login_error("Principal MobileLoginUser employee lookup failed", principal_user_id_exc)
                            error = "Principal login could not be completed. Check mobileerp_error.log."

                    if not error and user and str(user.get("employee_id")) != str(principal["employee_id"]):
                        error = "Mobile Login का EmployeeID Principal रिकॉर्ड से मेल नहीं खाता।"
                    elif not error and user and not user["is_active"]:
                        error = "आपका Mobile Login बंद है। Admin से संपर्क करें।"
                    elif not error and user and not user["password_hash"]:
                        error = "Principal के Mobile Login का Password Setup नहीं है। Admin से जाँच कराएँ।"
                    elif not error and user and not verify_mobile_password(password, user["password_hash"]):
                        error = "Password गलत है।"
                    elif not error and not user:
                        first_password = "".join(ch for ch in str(principal.get("mobile", "")) if ch.isdigit())
                        if not first_password:
                            error = "Employee Master में Principal का वैध Mobile No. उपलब्ध नहीं है।"
                        elif password != first_password:
                            error = "पहला Password Employee Master में दर्ज Principal का Mobile No. है।"
                        else:
                            user = create_principal_mobile_login(principal)
                            if not user:
                                user = get_mobile_login_user_by_employee_id(principal["employee_id"])
                                if not user or not verify_mobile_password(password, user.get("password_hash")):
                                    error = "Principal का Mobile Login नहीं बन पाया। Admin से जाँच कराएँ।"

                    if user and not error:
                        # Keep this Employee's mobile login credentials linked to the authoritative EmployeeMaster row.
                        conn_sync = cur_sync = None
                        try:
                            conn_sync = get_connection()
                            cur_sync = conn_sync.cursor()
                            cur_sync.execute("""
                                UPDATE dbo.MobileLoginUser
                                SET EmailID=?, Designation=?, Role='Principal', UpdateDate=GETDATE()
                                WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), EmployeeID))) =
                                      LTRIM(RTRIM(CONVERT(NVARCHAR(100), ?)))
                            """, principal["email"], principal["designation"], principal["employee_id"])
                            conn_sync.commit()
                        except Exception as sync_error:
                            if conn_sync:
                                try:
                                    conn_sync.rollback()
                                except Exception:
                                    pass
                            print("PRINCIPAL MOBILE PROFILE SYNC ERROR:", repr(sync_error))
                        finally:
                            if cur_sync:
                                cur_sync.close()
                            if conn_sync:
                                conn_sync.close()

                        session["user_id"] = str(principal["employee_id"])
                        session["employee_id"] = str(principal["employee_id"])
                        session["user_name"] = principal["name"] or user.get("name") or "Principal"
                        session["user_email"] = principal["email"]
                        session["designation"] = principal["designation"] or "Principal"
                        session["role"] = "Principal"

                        if not user["password_changed"]:
                            return redirect(url_for("change_password"))

                        # Last-login timestamp is audit metadata; it must not block a valid Principal login.
                        try:
                            update_last_login(principal["employee_id"])
                        except Exception as principal_last_login_exc:
                            _record_principal_login_error("Principal LastLoginDate update failed", principal_last_login_exc)
                        return redirect(url_for("principal_dashboard"))

            elif selected_role == "classteacher":
                ct = get_class_teacher_assignment_user(email)

                if not ct:
                    error = "यह Email ID किसी Active Class Teacher Assignment में उपलब्ध नहीं है।"

                elif not is_active_class_teacher(ct["employee_id"]):
                    error = "\u0907\u0938 Employee \u0915\u093e Active Class Teacher Assignment \u0928\u0939\u0940\u0902 \u092e\u093f\u0932\u093e\u0964"

                else:
                    if user:
                        if not user["is_active"]:
                            error = "à¤†à¤ªà¤•à¤¾ Mobile Login à¤¬à¤‚à¤¦ à¤¹à¥ˆà¥¤"

                        elif not user["password_hash"]:
                            error = (
                                "à¤‡à¤¸ Class Teacher à¤•à¤¾ Mobile Login à¤¬à¤¨à¤¾ à¤¹à¥à¤† à¤¹à¥ˆ, "
                                "à¤²à¥‡à¤•à¤¿à¤¨ Password Setup à¤¨à¤¹à¥€à¤‚ à¤¹à¥ˆà¥¤"
                            )

                        elif not verify_mobile_password(
                            password, user["password_hash"]
                        ):
                            error = "Password à¤—à¤²à¤¤ à¤¹à¥ˆà¥¤"

                        else:
                            # Keep the database Role synchronized with the
                            # active Class Teacher assignment.
                            conn = cur = None
                            try:
                                conn = get_connection()
                                cur = conn.cursor()
                                cur.execute("""
                                    UPDATE dbo.MobileLoginUser
                                    SET Role='Class Teacher',
                                        UpdateDate=GETDATE()
                                    WHERE EmployeeID=?
                                """, user["employee_id"])
                                conn.commit()
                            except Exception as e:
                                if conn:
                                    try:
                                        conn.rollback()
                                    except Exception:
                                        pass
                                print("CLASS TEACHER ROLE UPDATE ERROR:", repr(e))
                            finally:
                                if cur:
                                    cur.close()
                                if conn:
                                    conn.close()

                            user["role"] = "Class Teacher"

                    else:
                        # First login: EmployeeMaster Mobile No. is the
                        # default password. The MobileLoginUser row is
                        # created automatically.
                        first_password = "".join(
                            ch for ch in str(ct["mobile"]) if ch.isdigit()
                        )

                        if not first_password:
                            error = (
                                "à¤‡à¤¸ Class Teacher à¤•à¤¾ valid Mobile No. "
                                "Employee Master à¤®à¥‡à¤‚ à¤‰à¤ªà¤²à¤¬à¥à¤§ à¤¨à¤¹à¥€à¤‚ à¤¹à¥ˆà¥¤"
                            )

                        elif password != first_password:
                            error = (
                                "Password à¤—à¤²à¤¤ à¤¹à¥ˆà¥¤ First Password à¤†à¤ªà¤•à¤¾ "
                                "Mobile No. à¤¹à¥ˆà¥¤"
                            )

                        else:
                            user = create_class_teacher_mobile_login(ct)

                            if not user:
                                error = (
                                    "Class Teacher à¤•à¤¾ Mobile Login User "
                                    "à¤¬à¤¨à¤¾à¤¯à¤¾ à¤¨à¤¹à¥€à¤‚ à¤œà¤¾ à¤¸à¤•à¤¾à¥¤"
                                )

                    if user and not error:
                        session["user_id"] = str(user["employee_id"])
                        session["employee_id"] = str(user["employee_id"])
                        session["user_name"] = user["name"]
                        session["user_email"] = user["email"]
                        session["designation"] = user["designation"]
                        session["role"] = "Class Teacher"

                        if not user["password_changed"]:
                            return redirect(url_for("change_password"))

                        update_last_login(user["employee_id"])
                        return redirect(url_for("teacher_dashboard"))

            # ========================================================
            # EXISTING TEACHER / ADMIN LOGIN
            # ========================================================
            else:
                if not user:
                    error = "à¤¯à¤¹ Email ID Mobile User Setup à¤®à¥‡à¤‚ à¤‰à¤ªà¤²à¤¬à¥à¤§ à¤¨à¤¹à¥€à¤‚ à¤¹à¥ˆà¥¤"
                elif not user["is_active"]:
                    error = "à¤†à¤ªà¤•à¤¾ Mobile Login à¤¬à¤‚à¤¦ à¤¹à¥ˆà¥¤"
                elif not user["password_hash"]:
                    error = "à¤‡à¤¸ Mobile User à¤•à¤¾ Password Setup à¤¨à¤¹à¥€à¤‚ à¤¹à¥ˆà¥¤"
                elif selected_role != normalize_role(user["role"]):
                    error = "à¤šà¤¯à¤¨à¤¿à¤¤ Role à¤‡à¤¸ Mobile User à¤•à¥‡ Role à¤¸à¥‡ à¤®à¥‡à¤² à¤¨à¤¹à¥€à¤‚ à¤–à¤¾à¤¤à¤¾à¥¤"
                elif not verify_mobile_password(password, user["password_hash"]):
                    error = "Password à¤—à¤²à¤¤ à¤¹à¥ˆà¥¤"
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
    return "EMPLOYEE_MASTER_PRINCIPAL_LOGIN_V1"

def get_class_teacher_assignment_user(email):
    """Return the active Class Teacher assignment for an EmployeeMaster email."""
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT TOP 1
                E.ID, E.Name, E.EmailID, E.Moble, E.Designation,
                C.Class, C.Section, C.Session
            FROM dbo.EmployeeMaster E
            INNER JOIN dbo.ClassTeacherAssign C
                ON TRY_CONVERT(int, C.EmployeeID) = E.ID
            WHERE LOWER(LTRIM(RTRIM(E.EmailID))) = LOWER(LTRIM(RTRIM(?)))
              AND C.IsActive = 1
            ORDER BY C.ID DESC
        """, email)
        row = cur.fetchone()
        if not row:
            return None
        return {
            "employee_id": row[0],
            "name": str(row[1] or "").strip(),
            "email": str(row[2] or "").strip(),
            "mobile": str(row[3] or "").strip(),
            "designation": str(row[4] or "").strip(),
            "class_name": str(row[5] or "").strip(),
            "section": str(row[6] or "").strip(),
            "session": str(row[7] or "").strip(),
        }
    finally:
        if cur: cur.close()
        if conn: conn.close()

# ============================================================
# CLASS TEACHER MOBILE LOGIN
# ============================================================
def is_active_class_teacher(employee_id):
    """Check whether this employee has an active Class Teacher assignment."""
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT TOP 1 1
            FROM dbo.ClassTeacherAssign
            WHERE IsActive = 1
              AND TRY_CONVERT(int, EmployeeID) = ?
            ORDER BY ID DESC
        """, employee_id)
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def create_class_teacher_mobile_login(ct):
    """Create the first MobileLoginUser for a Class Teacher."""
    conn = cur = None
    try:
        first_password = "".join(ch for ch in str(ct.get("mobile", "")) if ch.isdigit())
        if not first_password:
            return None

        password_hash = hash_mobile_password(first_password)

        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO dbo.MobileLoginUser
            (
                ID, EmployeeID, EmailID, PasswordHash,
                Designation, Role, IsActive, PasswordChanged,
                LastLoginDate, EntryDate, UpdateDate
            )
            VALUES
            (?, ?, ?, ?, ?, 'Class Teacher', 1, 0,
             NULL, GETDATE(), GETDATE())
        """,
        secrets.token_hex(8),
        ct["employee_id"],
        ct["email"],
        password_hash,
        ct["designation"])

        conn.commit()

        return get_mobile_login_user(ct["email"])

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        print("CLASS TEACHER MOBILE USER CREATE ERROR:", repr(e))
        return None
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


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
            error = "à¤¨à¤¯à¤¾ Password à¤¡à¤¾à¤²à¥‡à¤‚à¥¤"
        elif len(new_password) < 6:
            error = "Password à¤•à¤® à¤¸à¥‡ à¤•à¤® 6 characters à¤•à¤¾ à¤¹à¥‹à¤¨à¤¾ à¤šà¤¾à¤¹à¤¿à¤à¥¤"
        elif new_password != confirm:
            error = "à¤¦à¥‹à¤¨à¥‹à¤‚ Password à¤¸à¤®à¤¾à¤¨ à¤¨à¤¹à¥€à¤‚ à¤¹à¥ˆà¤‚à¥¤"
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
                error = "Password update à¤¨à¤¹à¥€à¤‚ à¤¹à¥‹ à¤ªà¤¾à¤¯à¤¾: " + str(e)
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
            error = "Email ID à¤¡à¤¾à¤²à¥‡à¤‚à¥¤"
        else:
            user = get_mobile_login_user(email)
            if not user:
                error = "à¤¯à¤¹ Email ID Mobile User Setup à¤®à¥‡à¤‚ à¤‰à¤ªà¤²à¤¬à¥à¤§ à¤¨à¤¹à¥€à¤‚ à¤¹à¥ˆà¥¤"
            elif not user["is_active"]:
                error = "à¤¯à¤¹ Mobile Login à¤¬à¤‚à¤¦ à¤¹à¥ˆà¥¤"
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
                    error = "OTP à¤­à¥‡à¤œà¤¾ à¤¨à¤¹à¥€à¤‚ à¤œà¤¾ à¤¸à¤•à¤¾: " + str(e)

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
            error = "OTP à¤¨à¤¹à¥€à¤‚ à¤®à¤¿à¤²à¤¾à¥¤ à¤«à¤¿à¤° à¤¸à¥‡ Forgot Password à¤•à¤°à¥‡à¤‚à¥¤"
        elif datetime.now() - data["created"] > timedelta(minutes=OTP_VALID_MINUTES):
            FORGOT_OTP.pop(email.lower(), None)
            error = "OTP expire à¤¹à¥‹ à¤—à¤¯à¤¾à¥¤ à¤¨à¤¯à¤¾ OTP à¤²à¥‡à¤‚à¥¤"
        elif not hmac.compare_digest(str(otp), str(data["otp"])):
            error = "OTP à¤—à¤²à¤¤ à¤¹à¥ˆà¥¤"
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
            error = "Password à¤•à¤® à¤¸à¥‡ à¤•à¤® 6 characters à¤•à¤¾ à¤¹à¥‹à¤¨à¤¾ à¤šà¤¾à¤¹à¤¿à¤à¥¤"
        elif new_password != confirm:
            error = "à¤¦à¥‹à¤¨à¥‹à¤‚ Password à¤¸à¤®à¤¾à¤¨ à¤¨à¤¹à¥€à¤‚ à¤¹à¥ˆà¤‚à¥¤"
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
                error = "Password reset à¤¨à¤¹à¥€à¤‚ à¤¹à¥‹ à¤ªà¤¾à¤¯à¤¾: " + str(e)
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
    if normalize_role(session.get("role", "")) == "principal":
        return redirect(url_for("principal_dashboard"))

    return render_template(
        "teacher_dashboard.html",
        teacher_name=session.get("user_name"),
        designation=session.get("designation"),
        role=session.get("role"),
        school_name=get_school_name()
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
import mark_entry  # Exam Marks Entry routes/helpers live in mark_entry.py
import principal_dashboard  # Principal school-wide read-only reports/routes

# Principal-only exception logging: teacher and class-teacher handling remains unchanged.
@app.errorhandler(Exception)
def _principal_only_error_handler(exc):
    from werkzeug.exceptions import HTTPException
    is_principal_login_post = (
        request.method == 'POST'
        and request.path in ('/', '/login', '/school-login')
        and normalize_role(request.form.get('role', '')) == 'principal'
    )
    is_principal_session = normalize_role(session.get('role', '')) == 'principal'
    if not (is_principal_login_post or is_principal_session):
        if isinstance(exc, HTTPException):
            return exc
        raise exc

    _record_principal_login_error(
        'Unhandled Principal error at ' + str(request.method) + ' ' + str(request.path), exc
    )
    if is_principal_login_post:
        return render_template(
            'login.html',
            error='Principal login could not be completed. Check mobileerp_error.log.',
            reset_success=False,
            school_name=session.get('school_name', 'Shantiraman Mobile ERP')
        ), 500
    return 'Principal request failed. Details have been saved to mobileerp_error.log.', 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5001,
        debug=False,
        use_reloader=False
    )
