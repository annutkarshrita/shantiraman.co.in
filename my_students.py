from app import app, get_school_name
from flask import render_template, redirect, url_for, session
from class_teacher_common import require_class_teacher, _class_students, _clean_value, _student_columns, _table_columns, _pick_col, get_connection

@app.route("/my-students")
def my_students():
    ct = require_class_teacher()
    if not ct:
        return redirect(url_for("teacher_dashboard"))
    conn = cur = None
    error = None
    students = []
    try:
        conn = get_connection()
        cur = conn.cursor()
        students, sid, sname = _class_students(cur, ct)
    except Exception as e:
        error = "Student list load में समस्या: " + str(e)
        sid, sname = "", ""
    finally:
        if cur: cur.close()
        if conn: conn.close()

    return render_template(
        "my_students.html",
        teacher_name=session.get("user_name"),
        school_name=get_school_name(),
        class_name=ct["class_name"],
        section=ct["section"],
        session_name=sname,
        students=students,
        error=error
    )


@app.route("/my-student/<student_id>")
def my_student_detail(student_id):
    ct = require_class_teacher()
    if not ct:
        return redirect(url_for("teacher_dashboard"))

    conn = cur = None
    error = None
    detail = []
    allowed = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        students, sid, sname = _class_students(cur, ct)
        allowed = next((x for x in students if x["id"] == str(student_id).strip()), None)
        if not allowed:
            error = "यह विद्यार्थी आपकी assigned Class + Section में नहीं है।"
        else:
            cols = _table_columns(cur, "StudentMaster")
            sm = _student_columns(cur)
            # Show every StudentMaster column, exactly as stored.
            cur.execute(f"""
                SELECT *
                FROM dbo.StudentMaster
                WHERE [{sm['id']}] = ?
            """, student_id)
            row = cur.fetchone()
            if row:
                for i, col in enumerate(cols):
                    detail.append((col, _clean_value(row[i])))
    except Exception as e:
        error = "Student details load में समस्या: " + str(e)
    finally:
        if cur: cur.close()
        if conn: conn.close()

    return render_template(
        "my_student_detail.html",
        teacher_name=session.get("user_name"),
        school_name=get_school_name(),
        student=allowed if not error else None,
        details=detail,
        error=error
    )

