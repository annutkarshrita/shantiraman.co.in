from flask import render_template, redirect, url_for, session, request

from app import app, get_connection, get_school_name
from class_teacher_common import require_class_teacher, _clean_value


def _get_active_session():
    """SessionTable के वास्तविक column names देखकर Active Session निकाले।"""
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='SessionTable'
        """)
        cols = {
            str(r[0]).strip().lower(): str(r[0]).strip()
            for r in cur.fetchall()
        }

        id_col = cols.get('id')
        session_col = (
            cols.get('sessionname')
            or cols.get('session_name')
            or cols.get('session')
        )
        status_col = (
            cols.get('sessstatus')
            or cols.get('status')
            or cols.get('isactive')
        )

        if not id_col or not session_col or not status_col:
            return '', ''

        if status_col.lower() == 'isactive':
            sql = f"""
                SELECT TOP 1 [{id_col}], [{session_col}]
                FROM dbo.SessionTable
                WHERE [{status_col}] IN (1, '1', 'True', 'true', 'Yes', 'yes')
                ORDER BY [{id_col}] DESC
            """
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
        if not row:
            return '', ''

        return str(row[0] or '').strip(), str(row[1] or '').strip()
    except Exception:
        return '', ''
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def _student_list(cur, ct, session_id):
    """Return only the active students of the logged-in Class Teacher."""
    cur.execute("""
        SELECT
            SA.SRNo,
            SM.ID,
            SM.Student,
            SM.Father,
            SM.Mother,
            SM.DOB
        FROM dbo.StudentAcademic SA
        INNER JOIN dbo.StudentMaster SM
            ON SM.ID = SA.StudentID
        WHERE SA.SessionID = ?
          AND LTRIM(RTRIM(ISNULL(SA.Class,''))) = ?
          AND LTRIM(RTRIM(ISNULL(SA.Section,''))) = ?
          AND (
              SA.SessStatus IS NULL OR
              LOWER(LTRIM(RTRIM(CONVERT(VARCHAR(100), SA.SessStatus))))
                  IN ('active','1','true','yes')
          )
        ORDER BY TRY_CONVERT(INT, SA.SRNo), SA.SRNo
    """, (
        session_id,
        ct["class_name"].strip(),
        ct["section"].strip(),
    ))

    rows = []
    for r in cur.fetchall():
        rows.append({
            "srno": _clean_value(r[0]),
            "id": _clean_value(r[1]),
            "name": _clean_value(r[2]),
            "father": _clean_value(r[3]),
            "mother": _clean_value(r[4]),
            "dob": _clean_value(r[5]),
        })
    return rows


def _available_exams(cur, class_name, section):
    """Read exam types from the same SubjectSetup used by Exam/Marks."""
    cur.execute("""
        SELECT DISTINCT
            LTRIM(RTRIM(ISNULL(ExamType,'')))
        FROM dbo.SubjectSetup
        WHERE LTRIM(RTRIM(ISNULL(Class,''))) = ?
          AND LTRIM(RTRIM(ISNULL(Section,''))) = ?
          AND LTRIM(RTRIM(ISNULL(ExamType,''))) <> ''
        ORDER BY LTRIM(RTRIM(ISNULL(ExamType,'')))
    """, (class_name, section))
    return [_clean_value(r[0]) for r in cur.fetchall()]


def _subjects_for_exam(cur, class_name, section, exam):
    """Return SubjectSetup subjects and papers in display order."""
    cur.execute("""
        SELECT DISTINCT
            LTRIM(RTRIM(ISNULL(Subject,''))) AS SubjectName,
            LTRIM(RTRIM(ISNULL(SubSubject,''))) AS SubSubject,
            MaxMarks
        FROM dbo.SubjectSetup
        WHERE LTRIM(RTRIM(ISNULL(Class,''))) = ?
          AND LTRIM(RTRIM(ISNULL(Section,''))) = ?
          AND LTRIM(RTRIM(ISNULL(ExamType,''))) = ?
          AND LTRIM(RTRIM(ISNULL(Subject,''))) <> ''
        ORDER BY
            LTRIM(RTRIM(ISNULL(Subject,''))),
            LTRIM(RTRIM(ISNULL(SubSubject,'')))
    """, (class_name, section, exam))

    out = []
    seen = set()
    for r in cur.fetchall():
        subject = _clean_value(r[0])
        subsubject = _clean_value(r[1])
        max_marks = _clean_value(r[2])
        key = (subject, subsubject)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "subject": subject,
            "subsubject": subsubject,
            "max_marks": max_marks,
        })
    return out


@app.route("/my-result")
def my_result():
    ct = require_class_teacher()
    if not ct:
        return redirect(url_for("teacher_dashboard"))

    selected_exam = request.args.get("exam", "").strip()
    selected_student = request.args.get("student", "").strip()

    conn = cur = None
    error = None
    session_id = ""
    session_name = ""
    exams = []
    students = []
    subjects = []
    result_rows = []

    try:
        conn = get_connection()
        cur = conn.cursor()

        # Use app.py's verified dynamic SessionTable logic.
        session_id, session_name = _get_active_session()
        if not session_id:
            raise RuntimeError("Active Session नहीं मिली।")

        students = _student_list(cur, ct, session_id)

        if selected_student:
            students = [s for s in students if s["id"] == selected_student]

        exams = _available_exams(
            cur,
            ct["class_name"].strip(),
            ct["section"].strip(),
        )

        if not selected_exam and exams:
            selected_exam = exams[0]

        if selected_exam:
            subjects = _subjects_for_exam(
                cur,
                ct["class_name"].strip(),
                ct["section"].strip(),
                selected_exam,
            )

            # ExamMarks is the same table used by Exam / Marks.
            cur.execute("""
                SELECT
                    StudentID,
                    SubjectName,
                    SubSubject,
                    MaxMarks,
                    ObtMarks
                FROM dbo.ExamMarks
                WHERE LTRIM(RTRIM(ISNULL(SessionName,''))) = ?
                  AND LTRIM(RTRIM(ISNULL(ExamType,''))) = ?
                  AND LTRIM(RTRIM(ISNULL(ClassName,''))) = ?
                  AND LTRIM(RTRIM(ISNULL(SectionName,''))) = ?
                ORDER BY ID DESC
            """, (
                session_name,
                selected_exam,
                ct["class_name"].strip(),
                ct["section"].strip(),
            ))

            mark_map = {}
            for r in cur.fetchall():
                key = (
                    _clean_value(r[0]),
                    _clean_value(r[1]),
                    _clean_value(r[2]),
                )
                # Latest record wins because query is ID DESC.
                if key not in mark_map:
                    mark_map[key] = {
                        "max_marks": _clean_value(r[3]),
                        "obt_marks": _clean_value(r[4]),
                    }

            for student in students:
                cells = []
                total_max = 0.0
                total_obt = 0.0
                numeric_count = 0
                special_status = ""

                for subject in subjects:
                    rec = mark_map.get((
                        student["id"],
                        subject["subject"],
                        subject["subsubject"],
                    ))

                    if rec:
                        max_marks = rec["max_marks"] or subject["max_marks"]
                        obtained = rec["obt_marks"]
                    else:
                        max_marks = subject["max_marks"]
                        obtained = ""

                    if obtained.lower() in ("absent", "medical"):
                        if not special_status:
                            special_status = obtained.upper()
                        cells.append(obtained.upper())
                        continue

                    if obtained == "":
                        cells.append("—")
                        continue

                    cells.append(obtained)

                    try:
                        total_max += float(max_marks or 0)
                        total_obt += float(obtained)
                        numeric_count += 1
                    except (TypeError, ValueError):
                        pass

                percentage = ""
                result_status = "Pending"
                if special_status:
                    result_status = special_status
                elif subjects and numeric_count == len(subjects):
                    if total_max > 0:
                        percentage = f"{(total_obt / total_max) * 100:.2f}%"
                        result_status = "PASS" if (total_obt / total_max) * 100 >= 33 else "FAIL"
                elif numeric_count > 0:
                    result_status = "Partial"

                result_rows.append({
                    "srno": student["srno"],
                    "name": student["name"],
                    "marks": cells,
                    "total_max": f"{total_max:g}",
                    "total_obt": f"{total_obt:g}",
                    "percentage": percentage,
                    "status": result_status,
                })

    except Exception as e:
        error = "Result load में समस्या: " + str(e)

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return render_template(
        "my_result.html",
        teacher_name=session.get("user_name", ""),
        school_name=get_school_name(),
        class_name=ct["class_name"],
        section=ct["section"],
        session_name=session_name,
        exams=exams,
        selected_exam=selected_exam,
        students=students,
        subjects=subjects,
        rows=result_rows,
        error=error,
    )
