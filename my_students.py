from app import app, get_school_name
from flask import render_template, redirect, url_for, session, request
from datetime import datetime
from class_teacher_common import (
    require_class_teacher, _class_students, _clean_value,
    _student_columns, _table_columns, _pick_col, get_connection
)


OPTIONAL_ASSIGNMENT_TABLE_SQL = """
IF OBJECT_ID(N'dbo.StudentOptionalSubject', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.StudentOptionalSubject
    (
        ID BIGINT IDENTITY(1,1) NOT NULL
            CONSTRAINT PK_StudentOptionalSubject PRIMARY KEY,
        SessionID NVARCHAR(100) NOT NULL,
        SessionName NVARCHAR(100) NULL,
        StudentID NVARCHAR(100) NOT NULL,
        ClassName NVARCHAR(100) NOT NULL,
        SectionName NVARCHAR(100) NOT NULL,
        SubjectName NVARCHAR(255) NOT NULL,
        AssignedBy NVARCHAR(255) NULL,
        AssignedAt DATETIME2(0) NOT NULL
            CONSTRAINT DF_StudentOptionalSubject_AssignedAt DEFAULT (SYSDATETIME()),
        CONSTRAINT UQ_StudentOptionalSubject_Session_Student
            UNIQUE (SessionID, StudentID)
    );
END
"""


def _format_dob(value):
    """DOB को छोटा dd-mm-yyyy फ़ॉर्मेट में दिखाएँ; stored value नहीं बदलते।"""
    if value is None:
        return "—"
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%d-%m-%Y")
        except Exception:
            pass
    text_value = str(value).strip()
    if not text_value:
        return "—"
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00")).strftime("%d-%m-%Y")
    except (ValueError, TypeError):
        pass
    # SQL Server datetime strings can have 7 fractional-second digits.
    date_part = text_value[:10]
    try:
        return datetime.strptime(date_part, "%Y-%m-%d").strftime("%d-%m-%Y")
    except (ValueError, TypeError):
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text_value.split(" ")[0], fmt).strftime("%d-%m-%Y")
        except (ValueError, TypeError):
            continue
    return text_value.split(" ")[0]


def _load_optional_subjects(cur, class_name, section):
    """SubjectSetup से केवल selected Class/Section के Optional Subject लाएँ."""
    columns = _table_columns(cur, "SubjectSetup")
    if not columns:
        return [], "SubjectSetup table नहीं मिली।"

    class_col = _pick_col(columns, ["Class", "ClassName"])
    section_col = _pick_col(columns, ["Section", "SectionName"])
    subject_col = _pick_col(columns, ["Subject", "SubjectName"])
    optional_col = _pick_col(columns, ["IsOptional", "OptionalSubject"])

    if not all([class_col, section_col, subject_col, optional_col]):
        return [], "SubjectSetup में Class, Section, Subject और IsOptional/OptionalSubject कॉलम आवश्यक हैं।"

    cur.execute(f"""
        SELECT DISTINCT LTRIM(RTRIM(CONVERT(NVARCHAR(255), [{subject_col}]))) AS SubjectName
        FROM dbo.SubjectSetup
        WHERE UPPER(LTRIM(RTRIM(CONVERT(NVARCHAR(255), ISNULL([{class_col}], '')))))
                  = UPPER(LTRIM(RTRIM(?)))
          AND UPPER(LTRIM(RTRIM(CONVERT(NVARCHAR(255), ISNULL([{section_col}], '')))))
                  = UPPER(LTRIM(RTRIM(?)))
          AND LOWER(LTRIM(RTRIM(CONVERT(NVARCHAR(20), ISNULL([{optional_col}], 0)))))
                  IN ('1', 'true', 'yes', 'y', 'optional')
          AND LTRIM(RTRIM(CONVERT(NVARCHAR(255), ISNULL([{subject_col}], '')))) <> ''
        ORDER BY SubjectName
    """, (class_name, section))
    subjects = [str(row[0]).strip() for row in cur.fetchall() if row[0] is not None and str(row[0]).strip()]
    return subjects, None


def _load_optional_assignments(cur, session_id, class_name, section):
    cur.execute("""
        SELECT StudentID, SubjectName
        FROM dbo.StudentOptionalSubject
        WHERE SessionID = ? AND ClassName = ? AND SectionName = ?
    """, (str(session_id), class_name, section))
    assignments = {}
    for row in cur.fetchall():
        student_id = _clean_value(row[0])
        subject = _clean_value(row[1])
        if student_id:
            assignments[student_id] = subject
    return assignments


@app.route("/my-students", methods=["GET", "POST"])
def my_students():
    ct = require_class_teacher()
    if not ct:
        return redirect(url_for("teacher_dashboard"))

    conn = cur = None
    error = None
    success = None
    students = []
    optional_subjects = []
    assigned_subjects = {}
    sid = sname = ""
    class_name = _clean_value(ct.get("class_name"))
    section = _clean_value(ct.get("section"))

    try:
        conn = get_connection()
        cur = conn.cursor()
        students, sid, sname = _class_students(cur, ct)

        # Optional subject assignment is stored in its own table so that
        # StudentMaster/StudentAcademic and existing student details remain untouched.
        cur.execute(OPTIONAL_ASSIGNMENT_TABLE_SQL)
        conn.commit()

        optional_subjects, optional_error = _load_optional_subjects(cur, class_name, section)
        if optional_error:
            error = optional_error

        # Load current assignments before POST validation. This lets us keep an
        # older assignment unchanged even if that subject is no longer flagged
        # optional in SubjectSetup; it will not block assigning other students.
        if sid:
            assigned_subjects = _load_optional_assignments(cur, sid, class_name, section)

        if request.method == "POST" and request.form.get("action") == "assign_all":
            if not sid:
                error = "Active Session नहीं मिली। Optional Subject सेव नहीं हुआ।"
            elif optional_error:
                error = optional_error
            else:
                allowed_ids = [str(st.get("id", "")).strip() for st in students]
                chosen = []
                invalid_ids = []
                allowed_subjects = set(optional_subjects)
                submitted_any = False

                # Empty rows are intentionally skipped; an old assignment is not deleted.
                # Unchanged pre-existing values are also skipped, preventing a legacy
                # assignment from blocking updates to other students.
                for student_id in allowed_ids:
                    if not student_id:
                        continue
                    subject = request.form.get(f"optional_subject_{student_id}", "").strip()
                    if not subject:
                        continue
                    submitted_any = True
                    old_subject = assigned_subjects.get(student_id, "")
                    if subject == old_subject:
                        continue
                    if subject not in allowed_subjects:
                        invalid_ids.append(student_id)
                    else:
                        chosen.append((student_id, subject))

                if invalid_ids:
                    error = "कुछ विद्यार्थियों के लिए चुना गया विषय इस Class/Section के Optional Subject में उपलब्ध नहीं है। कोई बदलाव सेव नहीं किया गया।"
                elif not chosen:
                    if submitted_any:
                        success = "कोई नया बदलाव नहीं था। पहले से assigned Optional Subjects सुरक्षित हैं।"
                    else:
                        error = "किसी विद्यार्थी का Optional Subject नहीं चुना गया। कोई बदलाव सेव नहीं किया गया।"
                else:
                    user_name = _clean_value(
                        session.get("user_email") or session.get("user_name") or session.get("employee_id")
                    )
                    inserted = 0
                    updated = 0
                    try:
                        for student_id, subject in chosen:
                            cur.execute("""
                                SELECT ID
                                FROM dbo.StudentOptionalSubject
                                WHERE SessionID = ? AND StudentID = ?
                            """, (str(sid), student_id))
                            existing = cur.fetchone()
                            if existing:
                                cur.execute("""
                                    UPDATE dbo.StudentOptionalSubject
                                    SET SessionName = ?, ClassName = ?, SectionName = ?,
                                        SubjectName = ?, AssignedBy = ?, AssignedAt = SYSDATETIME()
                                    WHERE ID = ?
                                """, (sname, class_name, section, subject, user_name, existing[0]))
                                updated += 1
                            else:
                                cur.execute("""
                                    INSERT INTO dbo.StudentOptionalSubject
                                        (SessionID, SessionName, StudentID, ClassName, SectionName,
                                         SubjectName, AssignedBy, AssignedAt)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, SYSDATETIME())
                                """, (str(sid), sname, student_id, class_name, section, subject, user_name))
                                inserted += 1
                        conn.commit()
                        return redirect(url_for(
                            "my_students", saved="1", assigned=str(len(chosen)),
                            inserted=str(inserted), updated=str(updated)
                        ))
                    except Exception:
                        conn.rollback()
                        raise

        if request.method == "GET" and request.args.get("saved") == "1":
            assigned_count = request.args.get("assigned", "0")
            inserted_count = request.args.get("inserted", "0")
            updated_count = request.args.get("updated", "0")
            success = (
                f"{assigned_count} विद्यार्थियों के Optional Subject सेव/अपडेट हो गए "
                f"(नए: {inserted_count}, अपडेट: {updated_count})।"
            )

        if sid and not assigned_subjects:
            assigned_subjects = _load_optional_assignments(cur, sid, class_name, section)
        for student in students:
            student["dob_display"] = _format_dob(student.get("dob"))

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        error = "My Students / Optional Subject load-save में समस्या: " + str(e)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return render_template(
        "my_students.html",
        teacher_name=session.get("user_name"),
        school_name=get_school_name(),
        class_name=class_name,
        section=section,
        session_name=sname,
        students=students,
        optional_subjects=optional_subjects,
        assigned_subjects=assigned_subjects,
        error=error,
        success=success
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

