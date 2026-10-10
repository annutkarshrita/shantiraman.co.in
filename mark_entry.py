# -*- coding: utf-8 -*-
"""
Shantiraman ERP - Exam Marks Entry module.
Exam marks routes and database helpers are kept separate from app.py.
Gmail/SMTP configuration is not used by this module.
"""
import secrets
from datetime import datetime

from flask import render_template, request, redirect, url_for, session
from app import app, get_connection, normalize_role, get_school_name

# ============================================================
# EXAM / MARKS
# ============================================================
def get_active_session():
    """
    SessionTable à¤¸à¥‡ Active Session à¤¨à¤¿à¤•à¤¾à¤²à¥‡à¥¤
    à¤…à¤²à¤—-à¤…à¤²à¤— à¤ªà¥à¤°à¤¾à¤¨à¥‡ DB versions à¤®à¥‡à¤‚ status column à¤•à¤¾ à¤¨à¤¾à¤®
    SessStatus / Status / IsActive à¤¹à¥‹ à¤¸à¤•à¤¤à¤¾ à¤¹à¥ˆ, à¤‡à¤¸à¤²à¤¿à¤ schema à¤•à¥‡
    à¤…à¤¨à¥à¤¸à¤¾à¤° query à¤¬à¤¨à¤¾à¤ˆ à¤œà¤¾à¤¤à¥€ à¤¹à¥ˆà¥¤
    """
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        # Separate status column keeps Absent/Medical out of numeric ObtMarks.
        cur.execute("""
            IF COL_LENGTH('dbo.ExamMarks', 'AttendanceStatus') IS NULL
                ALTER TABLE dbo.ExamMarks ADD AttendanceStatus NVARCHAR(20) NULL
        """)
        conn.commit()

        # SessionTable à¤•à¥‡ à¤‰à¤ªà¤²à¤¬à¥à¤§ columns à¤ªà¤¤à¤¾ à¤•à¤°à¥‡à¤‚
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

        # Status column à¤‰à¤ªà¤²à¤¬à¥à¤§ à¤¹à¥ˆ à¤¤à¥‹ à¤‰à¤¸à¥€ à¤•à¥‡ à¤…à¤¨à¥à¤¸à¤¾à¤° Active Session à¤–à¥‹à¤œà¥‡à¤‚
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
            # à¤ªà¥à¤°à¤¾à¤¨à¥‡ DB à¤®à¥‡à¤‚ SubSubject column à¤¨ à¤¹à¥‹ à¤¤à¥‹ 4-column fallback.
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


def _master_order_map(cur, category):
    """MasterTable से किसी Category का ItemName -> numeric Order map लें."""
    cur.execute("""
        SELECT LTRIM(RTRIM(ISNULL(ItemName,''))), TRY_CONVERT(INT, [Order])
        FROM dbo.MasterTable
        WHERE LTRIM(RTRIM(ISNULL(Category,''))) = ?
          AND LTRIM(RTRIM(ISNULL(ItemName,''))) <> ''
    """, category)
    result = {}
    for row in cur.fetchall():
        name = str(row[0] or "").strip()
        if name:
            result[name.casefold()] = row[1] if row[1] is not None else 2147483647
    return result


def _sort_by_master_order(cur, category, values):
    """List values को MasterTable के Order से sort करें; unknown items अंत में."""
    order_map = _master_order_map(cur, category)
    unique = {}
    for value in values:
        name = str(value or "").strip()
        if name:
            unique.setdefault(name.casefold(), name)
    return sorted(
        unique.values(),
        key=lambda name: (order_map.get(name.casefold(), 2147483647), name.casefold())
    )


def _is_optional_subject(cur, class_name, section, subject_name):
    """
    Selected subject को SubjectSetup के IsOptional/OptionalSubject flag से पहचानें.
    Schema column names runtime पर खोजे जाते हैं ताकि पुराने नाम वाले DB भी चलें.
    """
    cur.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='SubjectSetup'
    """)
    cols = {
        str(row[0]).strip().lower(): str(row[0]).strip()
        for row in cur.fetchall()
    }

    def pick(names):
        for name in names:
            found = cols.get(name.lower())
            if found:
                return found
        return None

    class_col = pick(["Class", "ClassName"])
    section_col = pick(["Section", "SectionName"])
    subject_col = pick(["Subject", "SubjectName"])
    optional_col = pick(["IsOptional", "OptionalSubject"])

    # यदि यह पुराना DB है जिसमें Optional flag मौजूद नहीं, तो पुराना behaviour रखें.
    if not all([class_col, section_col, subject_col, optional_col]):
        return False

    def q(identifier):
        return "[" + identifier.replace("]", "]]") + "]"

    cur.execute(f"""
        SELECT CASE WHEN EXISTS (
            SELECT 1
            FROM dbo.SubjectSetup
            WHERE UPPER(LTRIM(RTRIM(CONVERT(NVARCHAR(255), ISNULL({q(class_col)}, '')))))
                      = UPPER(LTRIM(RTRIM(?)))
              AND UPPER(LTRIM(RTRIM(CONVERT(NVARCHAR(255), ISNULL({q(section_col)}, '')))))
                      = UPPER(LTRIM(RTRIM(?)))
              AND UPPER(LTRIM(RTRIM(CONVERT(NVARCHAR(255), ISNULL({q(subject_col)}, '')))))
                      = UPPER(LTRIM(RTRIM(?)))
              AND LOWER(LTRIM(RTRIM(CONVERT(NVARCHAR(20), ISNULL({q(optional_col)}, 0)))))
                      IN ('1', 'true', 'yes', 'y', 'optional')
        ) THEN 1 ELSE 0 END
    """, (class_name, section, subject_name))
    row = cur.fetchone()
    return bool(row and row[0])


def _optional_assignment_table_exists(cur):
    cur.execute("SELECT CASE WHEN OBJECT_ID(N'dbo.StudentOptionalSubject', N'U') IS NULL THEN 0 ELSE 1 END")
    row = cur.fetchone()
    return bool(row and row[0])


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
        # 1. CLASS LIST - Exam à¤•à¥‡ à¤¬à¤¾à¤¦
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
            # à¤•à¥‡à¤µà¤² à¤‰à¤¸à¥€ exam à¤•à¥‡ assigned classes
            exam_rows = [a for a in allowed
                         if not selected_exam or norm(a[3]) == norm(selected_exam)]
            classes = sorted({a[0] for a in exam_rows if a[0]})

        # ----------------------------------------------------
        classes = _sort_by_master_order(cur, "Class", classes)

        # 2. SECTION LIST - Exam + Class à¤•à¥‡ à¤¬à¤¾à¤¦
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
        sections = _sort_by_master_order(cur, "Section", sections)

        # 3. SUBJECT LIST - Exam + Class + Section à¤•à¥‡ à¤¬à¤¾à¤¦
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
            # Subject à¤¸à¥€à¤§à¥‡ MobileTeacherSubjectAssign à¤¸à¥‡ à¤†à¤à¤—à¤¾à¥¤
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
                    'à¤‡à¤¸ Exam/Class/Section à¤•à¥‡ à¤²à¤¿à¤ à¤‡à¤¸ Teacher à¤•à¤¾ '
                    'Subject Assignment à¤¨à¤¹à¥€à¤‚ à¤®à¤¿à¤²à¤¾à¥¤'
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
                        error = 'à¤‡à¤¸ Teacher à¤•à¥‹ à¤šà¥à¤¨à¥‡ à¤¹à¥à¤ Subject/SubSubject à¤•à¥€ à¤…à¤¨à¥à¤®à¤¤à¤¿ à¤¨à¤¹à¥€à¤‚ à¤¹à¥ˆà¥¤'

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
                    error = 'Selected Subject/SubSubject à¤•à¤¾ setup à¤‰à¤ªà¤²à¤¬à¥à¤§ à¤¨à¤¹à¥€à¤‚ à¤¹à¥ˆà¥¤'

            # ----------------------------------------------------
            # 5. STUDENT LIST
            # ------------------------------------------------
            if selected_subject and selected_subsubject and max_marks and not error:
                if not session_id:
                    error = "Active Session à¤¨à¤¹à¥€à¤‚ à¤®à¤¿à¤²à¥€à¥¤ à¤ªà¤¹à¤²à¥‡ SessionTable à¤®à¥‡à¤‚ Active Session à¤¸à¥‡à¤Ÿ à¤•à¤°à¥‡à¤‚à¥¤"
                else:
                    # Optional subject होने पर सामान्य शिक्षक को केवल वही विद्यार्थी
                    # दिखें जिन्हें My Students में यही विषय assign किया गया है।
                    # Admin की पुरानी unrestricted access बनी रहती है।
                    is_optional = _is_optional_subject(
                        cur, selected_class, selected_section, selected_subject
                    )
                    student_rows = []

                    if is_optional and allowed is not None:
                        if not _optional_assignment_table_exists(cur):
                            error = (
                                "Optional Subject assignment अभी तैयार नहीं है। "
                                "पहले My Students खोलकर विद्यार्थियों के Optional Subject Assign करें।"
                            )
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
                                  AND EXISTS (
                                      SELECT 1
                                      FROM dbo.StudentOptionalSubject osa
                                      WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), osa.SessionID))) =
                                            LTRIM(RTRIM(CONVERT(NVARCHAR(100), ?)))
                                        AND LTRIM(RTRIM(CONVERT(NVARCHAR(100), osa.StudentID))) =
                                            LTRIM(RTRIM(CONVERT(NVARCHAR(100), sm.ID)))
                                        AND UPPER(LTRIM(RTRIM(ISNULL(osa.ClassName,'')))) =
                                            UPPER(LTRIM(RTRIM(?)))
                                        AND UPPER(LTRIM(RTRIM(ISNULL(osa.SectionName,'')))) =
                                            UPPER(LTRIM(RTRIM(?)))
                                        AND UPPER(LTRIM(RTRIM(ISNULL(osa.SubjectName,'')))) =
                                            UPPER(LTRIM(RTRIM(?)))
                                  )
                                ORDER BY TRY_CONVERT(INT,sm.Student), sm.Student
                            """, session_id, selected_class, selected_section,
                                 session_id, selected_class, selected_section, selected_subject)
                            student_rows = cur.fetchall()
                    else:
                        # अनिवार्य विषय या Admin: पुरानी student list logic.
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
                            ORDER BY TRY_CONVERT(INT,sm.Student), sm.Student
                        """, session_id, selected_class, selected_section)
                        student_rows = cur.fetchall()

                    for r in student_rows:
                        students.append({
                            "id": str(r[0]).strip(),
                            "srno": str(r[1] or "").strip(),
                            "name": str(r[2] or "").strip(),
                            "studentid": str(r[0]).strip(),
                            "obtmarks": ""
                        })

                    if is_optional and allowed is not None and not students and not error:
                        error = (
                            "इस Optional Subject के लिए किसी विद्यार्थी को Assign नहीं किया गया है। "
                            "पहले My Students में Optional Subject Assign करें।"
                        )

                    # Existing marks: à¤à¤• à¤¹à¥€ query à¤®à¥‡à¤‚ à¤¸à¤­à¥€ students à¤•à¥‡ marks load à¤•à¤°à¥‡à¤‚
                    student_ids = [st["id"] for st in students]
                    if student_ids:
                        placeholders = ",".join("?" for _ in student_ids)
                        cur.execute(f"""
                            SELECT StudentID, ObtMarks, AttendanceStatus
                            FROM (
                                SELECT StudentID, ObtMarks, AttendanceStatus,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY StudentID
                                           ORDER BY TRY_CONVERT(DATETIME, EntryDate) DESC, EntryDate DESC, ID DESC
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
                        marks_map = {
                            str(r[0]).strip(): ("" if r[1] is None else str(r[1]).strip(),
                                                "" if r[2] is None else str(r[2]).strip().lower())
                            for r in cur.fetchall()
                        }
                        for st in students:
                            mark_value, status_value = marks_map.get(st["id"], ("", ""))
                            st["status"] = status_value
                            st["obtmarks"] = "" if status_value in ("a", "absent", "m", "medical") else mark_value

                    # --------------------------------------------
                    # 6. SAVE / UPDATE / CLEAR MARKS / STATUS
                    # Blank marks with no status clears the existing record.
                    # A=Absent and M=Medical are stored separately from numeric marks.
                    # --------------------------------------------
                    if not error and request.method == "POST" and request.form.get("action") == "save":
                        row_actions = []
                        for st in students:
                            student_id = st["id"]
                            value = request.form.get("marks_" + student_id, "").strip()
                            absent = request.form.get("absent_" + student_id) == "1"
                            medical = request.form.get("medical_" + student_id) == "1"
                            if absent and medical:
                                error = f"{st['name']}: A और M में से केवल एक विकल्प चुनें।"
                                break
                            status_value = "Absent" if absent else ("Medical" if medical else "")
                            if status_value or value == "":
                                row_actions.append((st, student_id, value, status_value))
                                continue
                            try:
                                mm = float(max_marks)
                                om = float(value)
                                if om < 0 or om > mm:
                                    raise ValueError
                            except Exception:
                                error = f"{st['name']} के प्राप्तांक 0 से {max_marks} के बीच होने चाहिए।"
                                break
                            row_actions.append((st, student_id, value, ""))

                        if not error:
                            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            changed = 0
                            deleted = 0
                            for st, student_id, value, status_value in row_actions:
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
                                    ORDER BY TRY_CONVERT(DATETIME, EntryDate) DESC, EntryDate DESC, ID DESC
                                """, student_id, session_name, selected_exam, selected_class,
                                     selected_section, selected_subject, selected_subsubject)
                                old = cur.fetchone()

                                # Empty marks + no status means clear this student's mark for this paper.
                                if value == "" and not status_value:
                                    if old:
                                        cur.execute("""
                                            DELETE FROM dbo.ExamMarks
                                            WHERE LTRIM(RTRIM(ISNULL(StudentID,'')))=?
                                              AND LTRIM(RTRIM(ISNULL(SessionName,'')))=?
                                              AND LTRIM(RTRIM(ISNULL(ExamType,'')))=?
                                              AND LTRIM(RTRIM(ISNULL(ClassName,'')))=?
                                              AND LTRIM(RTRIM(ISNULL(SectionName,'')))=?
                                              AND LTRIM(RTRIM(ISNULL(SubjectName,'')))=?
                                              AND LTRIM(RTRIM(ISNULL(SubSubject,'')))=?
                                        """, student_id, session_name, selected_exam, selected_class,
                                             selected_section, selected_subject, selected_subsubject)
                                        deleted += max(cur.rowcount, 0)
                                    st["obtmarks"] = ""
                                    st["status"] = ""
                                    continue

                                obtained_value = None if status_value else value
                                if old:
                                    cur.execute("""
                                        UPDATE dbo.ExamMarks
                                        SET SrNo=?, MaxMarks=?, ObtMarks=?, AttendanceStatus=?,
                                            EntryDate=?, EntryRole=?
                                        WHERE ID=?
                                    """, st["srno"], max_marks, obtained_value, status_value or None,
                                         now, session.get("role", ""), old[0])
                                    changed += max(cur.rowcount, 0)
                                else:
                                    cur.execute("""
                                        INSERT INTO dbo.ExamMarks
                                        (ID,SessionName,ExamType,SrNo,ClassName,SectionName,
                                         SubjectName,SubSubject,MaxMarks,ObtMarks,AttendanceStatus,
                                         EntryDate,EntryRole,StudentID)
                                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                    """, secrets.token_hex(8), session_name, selected_exam, st["srno"],
                                         selected_class, selected_section, selected_subject, selected_subsubject,
                                         max_marks, obtained_value, status_value or None, now,
                                         session.get("role", ""), student_id)
                                    changed += max(cur.rowcount, 0)
                                st["obtmarks"] = value if not status_value else ""
                                st["status"] = status_value.lower()

                            conn.commit()
                            if deleted and changed:
                                success = f"सफलता: {changed} रिकॉर्ड सेव/अपडेट हुए और {deleted} रिकॉर्ड हटाए गए।"
                            elif deleted:
                                success = f"सफलता: {deleted} खाली किए गए अंक रिकॉर्ड हटाए गए।"
                            elif changed:
                                success = f"सफलता: {changed} रिकॉर्ड सेव/अपडेट हुए।"
                            else:
                                success = "कोई बदलाव नहीं हुआ।"

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        error = "Exam / Marks load/save à¤®à¥‡à¤‚ à¤¸à¤®à¤¸à¥à¤¯à¤¾: " + str(e)
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

