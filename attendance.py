from datetime import datetime, timedelta

from flask import render_template, request, redirect, url_for, session

from app import app, get_school_name, get_connection
from class_teacher_common import (
    require_class_teacher,
    _class_students,
    _clean_value,
    _table_columns,
    _pick_col,
)


ATTENDANCE_TABLE = "AttendanceTable"


def _attendance_info(cur):
    """Detect the actual AttendanceTable column names."""
    cols = _table_columns(cur, ATTENDANCE_TABLE)
    info = {
        "id": _pick_col(cols, ["ID"]),
        "date": _pick_col(cols, ["AttDate", "AttendanceDate", "Date"]),
        "srno": _pick_col(cols, ["SrNo", "SRNo", "StudentSrNo"]),
        "session": _pick_col(cols, ["SessionID", "SessionId"]),
        "status": _pick_col(cols, ["Status", "AttendanceStatus"]),
        "user": _pick_col(cols, ["UpdateUser", "UpdatedBy", "UserName"]),
    }
    return info


def _parse_date(value):
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    raw = str(value).strip()
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(raw[:19], fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return None


def _attendance_rows(cur, ct, selected_date):
    """Return assigned-class students with attendance status for selected date."""
    students, session_id, session_name = _class_students(cur, ct)
    info = _attendance_info(cur)

    required = [info["date"], info["srno"], info["session"], info["status"]]
    if not all(required):
        missing = [k for k, v in info.items() if k in ("date", "srno", "session", "status") and not v]
        raise RuntimeError(
            "AttendanceTable के required columns नहीं मिले: " + ", ".join(missing)
        )

    cur.execute(
        f"""
        SELECT {('[%s]' % info['id']) if info['id'] else 'NULL'},
               [{info['date']}],
               [{info['srno']}],
               [{info['session']}],
               [{info['status']}]
        FROM dbo.{ATTENDANCE_TABLE}
        WHERE [{info['session']}] = ?
        """,
        session_id,
    )

    by_srno = {}
    first_save = None
    for row in cur.fetchall():
        row_date = _parse_date(row[1])
        if row_date != selected_date:
            continue
        srno = _clean_value(row[2])
        if not srno:
            continue
        by_srno[srno] = {
            "id": _clean_value(row[0]),
            "status": _clean_value(row[4]).lower(),
        }

    for st in students:
        saved = by_srno.get(st["srno"])
        status = saved["status"] if saved else ""
        if status in ("present", "p", "1", "1.0"):
            st["status"] = "Present"
        elif status in ("absent", "a", "0", "0.0"):
            st["status"] = "Absent"
        elif status in ("medical", "m"):
            st["status"] = "Medical"
        else:
            st["status"] = ""
        st["attendance_id"] = saved["id"] if saved else ""

    return students, session_id, session_name, first_save


def _next_id(cur, id_col):
    if not id_col:
        return None
    cur.execute(
        f"""
        SELECT ISNULL(MAX(TRY_CONVERT(BIGINT,[{id_col}])),0)+1
        FROM dbo.{ATTENDANCE_TABLE}
        """
    )
    value = cur.fetchone()[0]
    return str(value)


@app.route("/attendance", methods=["GET", "POST"])
def attendance():
    ct = require_class_teacher()
    if not ct:
        return redirect(url_for("teacher_dashboard"))

    selected_date = request.values.get("date", "").strip()
    if not selected_date:
        selected_date = datetime.now().strftime("%Y-%m-%d")

    error = None
    success = None
    students = []
    session_id = ""
    session_name = ""

    conn = cur = None
    try:
        datetime.strptime(selected_date, "%Y-%m-%d")

        conn = get_connection()
        cur = conn.cursor()

        students, session_id, session_name, _ = _attendance_rows(
            cur, ct, selected_date
        )

        if request.method == "POST":
            info = _attendance_info(cur)
            if not all([info["date"], info["srno"], info["session"], info["status"]]):
                raise RuntimeError("AttendanceTable के required columns नहीं मिले।")

            # Save/update every assigned student.
            user_name = _clean_value(session.get("user_name")) or "Mobile User"

            for st in students:
                status = request.form.get(f"att_{st['id']}", "").strip()
                if status not in ("Present", "Absent", "Medical"):
                    status = "Present"

                attendance_id = st.get("attendance_id", "")

                if attendance_id and info["id"]:
                    cur.execute(
                        f"""
                        UPDATE dbo.{ATTENDANCE_TABLE}
                        SET [{info['status']}] = ?
                            {', [' + info['user'] + '] = ?' if info['user'] else ''}
                        WHERE [{info['id']}] = ?
                        """,
                        (status, user_name, attendance_id) if info["user"] else (status, attendance_id),
                    )
                else:
                    fields = [info["date"], info["srno"], info["session"], info["status"]]
                    values = [selected_date, st["srno"], session_id, status]
                    if info["user"]:
                        fields.append(info["user"])
                        values.append(user_name)
                    if info["id"]:
                        fields.insert(0, info["id"])
                        values.insert(0, _next_id(cur, info["id"]))

                    cur.execute(
                        f"""
                        INSERT INTO dbo.{ATTENDANCE_TABLE}
                        ({', '.join('[' + x + ']' for x in fields)})
                        VALUES ({', '.join('?' for _ in values)})
                        """,
                        values,
                    )

            conn.commit()
            success = "Attendance successfully save/update हो गई।"
            students, session_id, session_name, _ = _attendance_rows(
                cur, ct, selected_date
            )

    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        error = "Attendance में समस्या: " + str(exc)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return render_template(
        "attendance.html",
        teacher_name=session.get("user_name"),
        school_name=get_school_name(),
        class_name=ct["class_name"],
        section=ct["section"],
        session_name=session_name,
        selected_date=selected_date,
        students=students,
        error=error,
        success=success,
    )


@app.route("/attendance-report")
def attendance_report():
    ct = require_class_teacher()
    if not ct:
        return redirect(url_for("teacher_dashboard"))

    mode = request.args.get("mode", "month").strip().lower()
    selected_date = request.args.get("date", "").strip()
    month = request.args.get("month", "").strip() or datetime.now().strftime("%Y-%m")
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()

    conn = cur = None
    error = None
    report = []
    summary = {"working": 0, "present": 0, "absent": 0, "medical": 0}
    students = []
    session_name = ""

    try:
        conn = get_connection()
        cur = conn.cursor()
        students, session_id, session_name = _class_students(cur, ct)
        info = _attendance_info(cur)
        if not all([info["date"], info["srno"], info["session"], info["status"]]):
            raise RuntimeError("AttendanceTable के required columns नहीं मिले।")

        if mode == "date":
            if not selected_date:
                selected_date = datetime.now().strftime("%Y-%m-%d")
            start = end = selected_date
        elif mode == "range":
            if not from_date:
                from_date = month + "-01"
            if not to_date:
                to_date = datetime.now().strftime("%Y-%m-%d")
            start, end = from_date, to_date
        else:
            start = month + "-01"
            y, m = map(int, month.split("-"))
            next_month = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
            end = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")

        start_dt = datetime.strptime(start, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end, "%Y-%m-%d").date()

        cur.execute(
            f"""
            SELECT [{info['date']}], [{info['srno']}], [{info['status']}]
            FROM dbo.{ATTENDANCE_TABLE}
            WHERE [{info['session']}] = ?
            """,
            session_id,
        )

        att = {}
        dates = set()
        for row in cur.fetchall():
            d = _parse_date(row[0])
            if not d:
                continue
            d_obj = datetime.strptime(d, "%Y-%m-%d").date()
            if start_dt <= d_obj <= end_dt:
                dates.add(d)
                att[(d, _clean_value(row[1]))] = _clean_value(row[2]).lower()

        working = len(dates)
        for st in students:
            statuses = [att.get((d, st["srno"]), "") for d in sorted(dates)]
            present = sum(1 for s in statuses if s in ("present", "p", "1", "1.0"))
            absent = sum(1 for s in statuses if s in ("absent", "a", "0", "0.0"))
            medical = sum(1 for s in statuses if s in ("medical", "m"))
            percent = round((present / working) * 100, 2) if working else 0
            report.append({
                **st,
                "working": working,
                "present": present,
                "absent": absent,
                "medical": medical,
                "percent": percent,
            })

        summary["working"] = working
        summary["present"] = sum(x["present"] for x in report)
        summary["absent"] = sum(x["absent"] for x in report)
        summary["medical"] = sum(x["medical"] for x in report)

    except Exception as exc:
        error = "Attendance report में समस्या: " + str(exc)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return render_template(
        "attendance_report.html",
        teacher_name=session.get("user_name"),
        school_name=get_school_name(),
        class_name=ct["class_name"],
        section=ct["section"],
        session_name=session_name,
        mode=mode,
        selected_date=selected_date,
        month=month,
        from_date=from_date,
        to_date=to_date,
        report=report,
        summary=summary,
        error=error,
    )
