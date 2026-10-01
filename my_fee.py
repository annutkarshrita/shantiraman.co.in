from app import app, get_school_name, get_connection
from flask import render_template, redirect, url_for, session
from decimal import Decimal, InvalidOperation
from class_teacher_common import require_class_teacher, _class_students, _clean_value, _table_columns, _pick_col, _student_columns, get_active_session

FEE_MONTHS = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]

def _fee_money(value):
    try:
        return Decimal(str(value if value is not None else "0").replace(",", "").strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _fee_month(value):
    v = _clean_value(value).lower()
    aliases = {
        "april":"Apr", "apr":"Apr", "may":"May", "june":"Jun", "jun":"Jun",
        "july":"Jul", "jul":"Jul", "august":"Aug", "aug":"Aug",
        "september":"Sep", "sept":"Sep", "sep":"Sep", "october":"Oct", "oct":"Oct",
        "november":"Nov", "nov":"Nov", "december":"Dec", "dec":"Dec",
        "january":"Jan", "jan":"Jan", "february":"Feb", "feb":"Feb",
        "march":"Mar", "mar":"Mar"
    }
    return aliases.get(v, _clean_value(value))


def _fee_type(value):
    v = _clean_value(value).lower().replace(".", "").replace("-", " ")
    v = " ".join(v.split())
    return {
        "genral fee": "genfee",
        "general fee": "genfee",
        "gen fee": "genfee",
        "general": "genfee",
        "genral": "genfee",
    }.get(v, v)


def _fee_status_data(cur, ct):
    students, sid, sname = _class_students(cur, ct)
    if not students:
        return [], sid, sname

    fs_cols = _table_columns(cur, "FeeSetup")
    fr_cols = _table_columns(cur, "Feerecept")
    sm_cols = _table_columns(cur, "StudentMaster")

    fs = {
        "session": _pick_col(fs_cols, ["SessionID", "SessionId", "Session"]),
        "class": _pick_col(fs_cols, ["Class", "ClassName"]),
        "section": _pick_col(fs_cols, ["Section", "Sec"]),
        "month": _pick_col(fs_cols, ["Month", "Months"]),
        "type": _pick_col(fs_cols, ["FeeType", "Fee Type", "Feetype"]),
        "oldnew": _pick_col(fs_cols, ["OldNew", "Old/New", "StudentType"]),
        "amount": _pick_col(fs_cols, ["Amount", "FeeAmount"]),
        "head": _pick_col(fs_cols, ["Fee Head", "FeeHead", "Head"]),
    }
    fr = {
        "session": _pick_col(fr_cols, ["SessionID", "SessionId", "Session"]),
        "srno": _pick_col(fr_cols, ["SRNo", "SrNo", "ScholarNo"]),
        "month": _pick_col(fr_cols, ["Month", "Months"]),
        "amount": _pick_col(fr_cols, ["AmountPaid", "Amount", "PaidAmount"]),
    }
    sm_session = _pick_col(sm_cols, ["SessionID", "SessionId", "Session"])
    sm_fee_type = _pick_col(sm_cols, ["FeeType", "Fee Type", "Feetype"])

    if not all(fs.values()):
        raise RuntimeError("FeeSetup में SessionID, Class, Section, Month, FeeType, OldNew, Fee Head और Amount columns आवश्यक हैं।")
    if not all(fr.values()):
        raise RuntimeError("Feerecept में SessionID, SRNo, Month और Amount columns आवश्यक हैं।")
    if not sm_session:
        raise RuntimeError("StudentMaster में SessionID column आवश्यक है।")

    # Fee setup for this class/section/session.
    cur.execute(f"""
        SELECT [{fs['class']}], [{fs['section']}], [{fs['month']}],
               [{fs['type']}], [{fs['oldnew']}], [{fs['amount']}]
        FROM dbo.FeeSetup
        WHERE [{fs['session']}] = ?
          AND LTRIM(RTRIM(CONVERT(VARCHAR(255), ISNULL([{fs['class']}],'')))) = ?
          AND LTRIM(RTRIM(CONVERT(VARCHAR(255), ISNULL([{fs['section']}],'')))) = ?
    """, sid, _clean_value(ct["class_name"]), _clean_value(ct["section"]))

    setup = {}
    for r in cur.fetchall():
        month = _fee_month(r[2])
        oldnew = _clean_value(r[4])
        key = (_fee_month(r[2]), _fee_type(r[3]), _clean_value(oldnew).lower())
        setup[key] = setup.get(key, Decimal("0")) + _fee_money(r[5])

    # Paid fee by SRNo + month. Only this Class Teacher's section is loaded.
    sa_cols = _table_columns(cur, "StudentAcademic")
    sa_srno = _pick_col(sa_cols, ["SRNo", "SrNo", "ScholarNo", "StudentNo"])
    sa_session = _pick_col(sa_cols, ["SessionID", "SessionId", "Session"])
    sa_class = _pick_col(sa_cols, ["Class", "ClassName"])
    sa_section = _pick_col(sa_cols, ["Section", "SectionName", "Sec"])
    if not all([sa_srno, sa_session, sa_class, sa_section]):
        raise RuntimeError("StudentAcademic में SRNo, SessionID, Class और Section columns आवश्यक हैं।")

    cur.execute(f"""
        SELECT fr.[{fr['srno']}], fr.[{fr['month']}],
               SUM(TRY_CONVERT(decimal(18,2), fr.[{fr['amount']}]))
        FROM dbo.Feerecept fr
        INNER JOIN dbo.StudentAcademic sa
          ON LTRIM(RTRIM(CONVERT(VARCHAR(255), fr.[{fr['srno']}])) ) =
             LTRIM(RTRIM(CONVERT(VARCHAR(255), sa.[{sa_srno}])))
        WHERE fr.[{fr['session']}] = ?
          AND sa.[{sa_session}] = ?
          AND LTRIM(RTRIM(CONVERT(VARCHAR(255), ISNULL(sa.[{sa_class}],'')))) = ?
          AND LTRIM(RTRIM(CONVERT(VARCHAR(255), ISNULL(sa.[{sa_section}],'')))) = ?
        GROUP BY fr.[{fr['srno']}], fr.[{fr['month']}]
    """, sid, sid, _clean_value(ct["class_name"]), _clean_value(ct["section"]))
    paid = {}
    for r in cur.fetchall():
        paid[(_clean_value(r[0]), _fee_month(r[1]))] = _fee_money(r[2])

    # Get StudentMaster SessionID + FeeType for Old/New and FeeSetup matching.
    ids = [st["id"] for st in students]
    student_info = {}
    sm_id = _student_columns(cur)["id"]
    if ids:
        ph = ",".join("?" for _ in ids)
        fee_type_select = f", [{sm_fee_type}]" if sm_fee_type else ", CAST('' AS NVARCHAR(100))"
        cur.execute(
            f"SELECT [{sm_id}], [{sm_session}] {fee_type_select} "
            f"FROM dbo.StudentMaster WHERE [{sm_id}] IN ({ph})",
            *ids
        )
        student_info = {
            _clean_value(r[0]): {
                "session": _clean_value(r[1]),
                "fee_type": _clean_value(r[2])
            }
            for r in cur.fetchall()
        }

    out = []
    for st in students:
        info = student_info.get(st["id"], {})
        oldnew = "New" if info.get("session", "") == _clean_value(sid) else "Old"
        student_fee_type = _fee_type(info.get("fee_type", ""))
        month_rows = []
        total_fee = Decimal("0")
        total_paid = Decimal("0")
        total_balance = Decimal("0")

        for month in FEE_MONTHS:
            expected = Decimal("0")
            # FeeSetup can contain multiple FeeType rows; use the student's FeeType.
            # If no exact FeeType match exists, do not invent a fee.
            expected = setup.get((month, student_fee_type, oldnew.lower()), Decimal("0"))
            p = paid.get((st["srno"], month), Decimal("0"))
            balance = max(expected - p, Decimal("0"))
            if expected or p:
                total_fee += expected
                total_paid += p
                total_balance += balance
                month_rows.append({"month": month, "fee": expected, "paid": p, "balance": balance})

        out.append({
            **st,
            "total_fee": total_fee,
            "total_paid": total_paid,
            "balance": total_balance,
            "status": "Paid" if total_balance <= 0 and total_fee > 0 else ("No Fee Setup" if total_fee <= 0 and total_paid <= 0 else "Balance"),
            "months": month_rows,
        })
    return out, sid, sname


@app.route("/my-fee")
def my_fee():
    ct = require_class_teacher()
    if not ct:
        return redirect(url_for("teacher_dashboard"))
    conn = cur = None
    error = None
    rows = []
    try:
        conn = get_connection()
        cur = conn.cursor()
        rows, sid, sname = _fee_status_data(cur, ct)
    except Exception as e:
        error = "Fee status load में समस्या: " + str(e)
        sname = ""
    finally:
        if cur: cur.close()
        if conn: conn.close()
    return render_template(
        "my_fee.html",
        teacher_name=session.get("user_name"),
        school_name=get_school_name(),
        class_name=ct["class_name"], section=ct["section"],
        session_name=sname, rows=rows, error=error
    )


@app.route("/my-fee/<student_id>")
def my_fee_detail(student_id):
    ct = require_class_teacher()
    if not ct:
        return redirect(url_for("teacher_dashboard"))
    conn = cur = None
    error = None
    student = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        rows, sid, sname = _fee_status_data(cur, ct)
        student = next((r for r in rows if r["id"] == str(student_id).strip()), None)
        if not student:
            error = "यह विद्यार्थी आपकी assigned Class + Section में नहीं है।"
    except Exception as e:
        error = "Fee detail load में समस्या: " + str(e)
        sname = ""
    finally:
        if cur: cur.close()
        if conn: conn.close()
    return render_template(
        "my_fee_detail.html",
        teacher_name=session.get("user_name"), school_name=get_school_name(),
        class_name=ct["class_name"], section=ct["section"], session_name=sname,
        student=student, error=error
    )



# ============================================================
# EXAM / MARKS
# ============================================================
