from flask import render_template, redirect, url_for, session, request
import time
import re

from app import app, get_connection, get_school_name
from class_teacher_common import require_class_teacher, _clean_value, _class_students

# Performance revision: fetch ExamMarks only for the active class-teacher roster.
MY_RESULT_PERFORMANCE_REVISION = "2026-10-16-my-std-roster-session-v3"


def _get_active_session(cur):
    """उसी DB connection से Active Session निकालें; अतिरिक्त connection न खोलें."""
    cur.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='SessionTable'
    """)
    cols = {
        str(r[0]).strip().lower(): str(r[0]).strip()
        for r in cur.fetchall()
    }

    id_col = cols.get("id")
    session_col = (
        cols.get("sessionname")
        or cols.get("session_name")
        or cols.get("session")
    )
    status_col = (
        cols.get("sessstatus")
        or cols.get("status")
        or cols.get("isactive")
    )

    if not id_col or not session_col or not status_col:
        return "", ""

    if status_col.lower() == "isactive":
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
        return "", ""
    return str(row[0] or "").strip(), str(row[1] or "").strip()


def _student_list(cur, ct, session_id, selected_student=""):
    """Return the class teacher's active-session roster, excluding inactive master students."""
    # StudentMaster status column names differ between installations. Detect the
    # available column instead of assuming one fixed schema.
    cur.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'StudentMaster'
    """)
    master_columns = [str(r[0]).strip() for r in cur.fetchall()]
    master_lookup = {c.casefold(): c for c in master_columns}
    master_status_col = next((master_lookup[name] for name in (
        "status", "isactive", "active", "studentstatus", "student_status",
        "is_active", "recordstatus", "record_status"
    ) if name in master_lookup), None)

    sql = """
        SELECT
            SA.SRNo,
            SM.ID,
            SM.Student
        FROM dbo.StudentAcademic SA
        INNER JOIN dbo.StudentMaster SM
            ON SM.ID = SA.StudentID
        WHERE SA.SessionID = ?
          AND SA.Class = ?
          AND SA.Section = ?
          AND (
              SA.SessStatus IS NULL OR
              LOWER(LTRIM(RTRIM(CONVERT(VARCHAR(100), SA.SessStatus))))
                  IN ('active','1','true','yes')
          )
    """
    params = [
        session_id,
        ct["class_name"].strip(),
        ct["section"].strip(),
    ]
    if master_status_col:
        sql += f"""
          AND LOWER(LTRIM(RTRIM(CONVERT(VARCHAR(100), SM.[{master_status_col}]))))
              IN ('active','1','true','yes','y')
        """
    if selected_student:
        sql += " AND SM.ID = ?"
        params.append(selected_student)
    sql += " ORDER BY TRY_CONVERT(INT, SM.Student), SM.Student"
    cur.execute(sql, params)

    rows = []
    for r in cur.fetchall():
        rows.append({
            "srno": _clean_value(r[0]),
            "id": _clean_value(r[1]),
            "name": _clean_value(r[2]),
        })
    return rows


def _available_exams(cur, class_name, section):
    """Load configured exams with a simple query, then apply MasterTable order in Python.

    The old query joined MasterTable and applied several string conversions in SQL.
    On some SQL Server installations that query can wait behind locks. Keep the
    SubjectSetup lookup simple and use NOLOCK for this read-only dropdown query.
    """
    started = time.perf_counter()
    print(f"[MY_RESULT_TIMING] exams_query_start class={class_name!r} section={section!r}")

    cur.execute("""
        SELECT DISTINCT LTRIM(RTRIM(CONVERT(NVARCHAR(100), ExamType))) AS ExamType
        FROM dbo.SubjectSetup WITH (NOLOCK)
        WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), [Class]))) = ?
          AND LTRIM(RTRIM(CONVERT(NVARCHAR(100), [Section]))) = ?
          AND ExamType IS NOT NULL
          AND LTRIM(RTRIM(CONVERT(NVARCHAR(100), ExamType))) <> ''
    """, (class_name, section))
    exams = [_clean_value(r[0]) for r in cur.fetchall() if _clean_value(r[0])]
    print(f"[MY_RESULT_TIMING] exams_subjectsetup={(time.perf_counter()-started)*1000:.1f} ms; count={len(exams)}")

    if not exams:
        return []

    # MasterTable ordering is optional; if its lookup fails, still show exams.
    order_map = {}
    try:
        cur.execute("""
            SELECT LTRIM(RTRIM(CONVERT(NVARCHAR(100), ItemName))) AS ItemName,
                   TRY_CONVERT(INT, [Order]) AS SortOrder
            FROM dbo.MasterTable WITH (NOLOCK)
            WHERE LOWER(LTRIM(RTRIM(CONVERT(NVARCHAR(100), Category)))) = 'exam type'
        """)
        for row in cur.fetchall():
            name = _clean_value(row[0]).casefold()
            if name and row[1] is not None:
                order_map[name] = int(row[1])
    except Exception as exc:
        print(f"[MY_RESULT_TIMING] exam_order_lookup_warning={exc}")

    # Stable fallback ordering keeps unlisted exams visible.
    exams = list(dict.fromkeys(exams))
    exams.sort(key=lambda name: (order_map.get(name.casefold(), 10**9), name.casefold()))
    print(f"[MY_RESULT_TIMING] exams_total={(time.perf_counter()-started)*1000:.1f} ms; values={exams}")
    return exams


def _norm_subject(value):
    """Normalize subject text for matching assignments and SubjectSetup rows."""
    return " ".join(_clean_value(value).casefold().split())


def _flag_is_true(value):
    return str(value if value is not None else "").strip().casefold() in {
        "1", "true", "yes", "y", "on", "extra", "optional"
    }


def _pick_column(columns, candidates):
    lookup = {str(c).strip().casefold(): str(c).strip() for c in columns}
    for name in candidates:
        found = lookup.get(name.casefold())
        if found:
            return found
    return None


def _subjects_for_exam(cur, class_name, section, exam):
    """Read subject/paper list including optional and extra-subject flags."""
    _subject_start = time.perf_counter()
    print(f"[MY_RESULT_TIMING] subjects_query_start class={class_name!r} section={section!r} exam={exam!r}", flush=True)
    _schema_start = time.perf_counter()
    cur.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'SubjectSetup'
    """)
    columns = [str(r[0]).strip() for r in cur.fetchall()]
    print(f"[MY_RESULT_TIMING] subjects_schema={(time.perf_counter()-_schema_start)*1000:.1f} ms; columns={len(columns)}", flush=True)
    if not columns:
        raise RuntimeError("SubjectSetup table या उसके columns नहीं मिले।")

    exam_col = _pick_column(columns, ["ExamType", "Exam_Type", "Exam Type"])
    class_col = _pick_column(columns, ["Class", "ClassName"])
    section_col = _pick_column(columns, ["Section", "SectionName"])
    subject_col = _pick_column(columns, ["Subject", "SubjectName", "Paper", "SubjectPaper"])
    subsubject_col = _pick_column(columns, ["SubSubject", "Sub Subject", "Sub_Subject", "PaperType"])
    max_col = _pick_column(columns, ["MaxMarks", "MM", "MaximumMarks", "Maximum_Marks"])
    optional_col = _pick_column(columns, ["IsOptional", "OptionalSubject", "IsOptionalSubject", "Optional"])
    extra_col = _pick_column(columns, ["IsExtraSubject", "ExtraSubject", "IsExtra"])

    if not (exam_col and class_col and section_col and subject_col):
        raise RuntimeError("SubjectSetup में ExamType, Class, Section और Subject columns आवश्यक हैं।")

    subject_expr = f"LTRIM(RTRIM(ISNULL([{subject_col}],'')))"
    subsubject_expr = (
        f"LTRIM(RTRIM(ISNULL([{subsubject_col}],'')))" if subsubject_col
        else "CAST('' AS NVARCHAR(200))"
    )
    max_expr = f"[{max_col}]" if max_col else "CAST(NULL AS NVARCHAR(50))"
    optional_expr = f"ISNULL([{optional_col}], 0)" if optional_col else "CAST(0 AS BIT)"
    extra_expr = f"ISNULL([{extra_col}], 0)" if extra_col else "CAST(0 AS BIT)"

    _query_start = time.perf_counter()
    print("[MY_RESULT_TIMING] subjects_data_query_execute_start (NOLOCK_TEST)", flush=True)
    cur.execute(f"""
        SELECT DISTINCT
            {subject_expr} AS SubjectName,
            {subsubject_expr} AS SubSubject,
            {max_expr} AS MaxMarks,
            {optional_expr} AS IsOptional,
            {extra_expr} AS IsExtraSubject
        FROM dbo.SubjectSetup WITH (NOLOCK)
        WHERE [{class_col}] = ?
          AND [{section_col}] = ?
          AND [{exam_col}] = ?
          AND {subject_expr} <> ''
        ORDER BY SubjectName, SubSubject
    """, (class_name, section, exam))
    print(f"[MY_RESULT_TIMING] subjects_data_query_execute={(time.perf_counter()-_query_start)*1000:.1f} ms", flush=True)
    _fetch_start = time.perf_counter()
    # Duplicate configuration rows are combined by Subject + Paper; flags are ORed.
    out_by_key = {}
    for r in cur.fetchall():
        subject = _clean_value(r[0])
        subsubject = _clean_value(r[1])
        key = (_norm_subject(subject), _norm_subject(subsubject))
        if key not in out_by_key:
            out_by_key[key] = {
                "subject": subject,
                "subsubject": subsubject,
                "max_marks": _clean_value(r[2]),
                "is_optional": _flag_is_true(r[3]),
                "is_extra": _flag_is_true(r[4]),
            }
        else:
            current = out_by_key[key]
            current["is_optional"] = current["is_optional"] or _flag_is_true(r[3])
            current["is_extra"] = current["is_extra"] or _flag_is_true(r[4])
            if not current["max_marks"] and _clean_value(r[2]):
                current["max_marks"] = _clean_value(r[2])
    result = list(out_by_key.values())
    print(f"[MY_RESULT_TIMING] subjects_fetch_and_group={(time.perf_counter()-_fetch_start)*1000:.1f} ms; count={len(result)}", flush=True)
    print(f"[MY_RESULT_TIMING] subjects_helper_total={(time.perf_counter()-_subject_start)*1000:.1f} ms", flush=True)
    return result


def _class_grade(class_name):
    """Resolve common numeric, word and Roman-numeral class labels."""
    value = _norm_subject(class_name)
    digits = re.search(r"(?<!\d)(12|11|10|[6-9])(?:st|nd|rd|th)?(?!\d)", value)
    if digits:
        return int(digits.group(1))

    word_grades = {
        "six": 6, "sixth": 6, "vi": 6,
        "seven": 7, "seventh": 7, "vii": 7,
        "eight": 8, "eighth": 8, "viii": 8,
        "nine": 9, "ninth": 9, "ix": 9,
        "ten": 10, "tenth": 10, "x": 10,
        "eleven": 11, "eleventh": 11, "xi": 11,
        "twelve": 12, "twelfth": 12, "xii": 12,
    }
    for token in value.split():
        if token in word_grades:
            return word_grades[token]
    return None


def _optional_assignments_by_student(cur, session_id, class_name, section):
    """Read student-specific optional picks where StudentOptionalSubject exists."""
    cur.execute("SELECT OBJECT_ID(N'dbo.StudentOptionalSubject', N'U')")
    exists_row = cur.fetchone()
    if not exists_row or not exists_row[0]:
        return {}

    cur.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'StudentOptionalSubject'
    """)
    columns = [str(r[0]).strip() for r in cur.fetchall()]
    session_col = _pick_column(columns, ["SessionID", "SessionId", "Session"])
    student_col = _pick_column(columns, ["StudentID", "StudentId", "StdID"])
    class_col = _pick_column(columns, ["ClassName", "Class"])
    section_col = _pick_column(columns, ["SectionName", "Section"])
    subject_col = _pick_column(columns, ["SubjectName", "Subject"])
    if not all((session_col, student_col, class_col, section_col, subject_col)):
        return {}

    cur.execute(f"""
        SELECT
            LTRIM(RTRIM(CONVERT(NVARCHAR(100), [{student_col}]))),
            LTRIM(RTRIM(ISNULL([{subject_col}],'')))
        FROM dbo.StudentOptionalSubject
        WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), [{session_col}]))) =
              LTRIM(RTRIM(CONVERT(NVARCHAR(100), ?)))
          AND UPPER(LTRIM(RTRIM(ISNULL([{class_col}],'')))) = UPPER(LTRIM(RTRIM(?)))
          AND UPPER(LTRIM(RTRIM(ISNULL([{section_col}],'')))) = UPPER(LTRIM(RTRIM(?)))
          AND LTRIM(RTRIM(ISNULL([{subject_col}],''))) <> ''
    """, (session_id, class_name, section))

    assignments = {}
    for student_id, subject_name in cur.fetchall():
        sid = _norm_subject(student_id)
        subject_key = _norm_subject(subject_name)
        if sid and subject_key:
            assignments.setdefault(sid, set()).add(subject_key)
    return assignments


def _subject_groups(subjects):
    """Group papers under each subject so a multi-paper subject is complete only when all papers are entered."""
    groups_by_key = {}
    for idx, subject in enumerate(subjects):
        key = _norm_subject(subject.get("subject"))
        if not key:
            continue
        group = groups_by_key.setdefault(key, {
            "key": key,
            "subject": subject.get("subject", ""),
            "indices": [],
            "optional": False,
            "extra": False,
        })
        group["indices"].append(idx)
        group["optional"] = group["optional"] or bool(subject.get("is_optional"))
        group["extra"] = group["extra"] or bool(subject.get("is_extra"))
    return list(groups_by_key.values())


def _student_result_completeness(class_name, subjects, paper_marked, assigned_optional_subjects=None):
    """Return (complete, applicable paper indices) using per-class rules.

    Grades 6-8: 9 completed compulsory subjects + any one completed optional.
    Grades 9-10: 5 completed compulsory subjects + any one completed optional.
    Grades 11-12: any 5 completed non-extra subjects, regardless of Optional flag.
    Extra-subject groups are additional requirements and never count toward the base.
    A subject with multiple configured papers is complete only when all those papers
    have a saved mark, zero, Absent or Medical status.
    """
    assigned_keys = {
        _norm_subject(v) for v in (assigned_optional_subjects or set())
        if _norm_subject(v)
    }
    groups = _subject_groups(subjects)
    extra_groups = [g for g in groups if g["extra"]]
    non_extra_groups = [g for g in groups if not g["extra"]]
    configured_optional_groups = [g for g in non_extra_groups if g["optional"]]
    assigned_optional_groups = [g for g in non_extra_groups if g["key"] in assigned_keys]

    group_complete = {
        g["key"]: bool(g["indices"]) and all(
            i < len(paper_marked) and bool(paper_marked[i]) for i in g["indices"]
        )
        for g in groups
    }
    group_has_any_mark = {
        g["key"]: any(
            i < len(paper_marked) and bool(paper_marked[i]) for i in g["indices"]
        )
        for g in groups
    }

    # Student-specific assignments take priority if they match SubjectSetup.
    # If the assignment table has no matching subject, fall back to the optional
    # flags configured in SubjectSetup rather than keeping all students Pending.
    optional_candidates = assigned_optional_groups or configured_optional_groups
    optional_keys = {
        g["key"] for g in configured_optional_groups + assigned_optional_groups
    }
    mandatory_groups = [g for g in non_extra_groups if g["key"] not in optional_keys]

    def preferred(groups_to_choose, count=None):
        # Use completed groups first, then partially entered groups, preserving
        # SubjectSetup order inside each priority. This keeps partial totals useful.
        ordered = sorted(
            groups_to_choose,
            key=lambda g: (
                0 if group_complete[g["key"]] else
                1 if group_has_any_mark[g["key"]] else 2
            ),
        )
        return ordered if count is None else ordered[:count]

    def has_n_complete(group_list, n):
        return sum(1 for g in group_list if group_complete[g["key"]]) >= n

    # One optional choice is enough. A student with multiple optional assignment
    # rows is not required to complete every alternative.
    completed_optional = [g for g in optional_candidates if group_complete[g["key"]]]
    chosen_optional = (
        completed_optional[:1]
        if completed_optional
        else preferred(optional_candidates, 1)
    )
    optional_ok = bool(completed_optional)
    grade = _class_grade(class_name)
    applicable_groups = []

    if grade is not None and 6 <= grade <= 8:
        if optional_candidates:
            required_compulsory = 9
            compulsory_ok = has_n_complete(mandatory_groups, required_compulsory)
            chosen_compulsory = preferred(mandatory_groups, required_compulsory)
            applicable_groups = chosen_compulsory + chosen_optional
            base_ok = compulsory_ok and optional_ok
        else:
            # Legacy setup with no optional flags/assignments: count 10 subjects.
            required_total = 10
            base_ok = has_n_complete(non_extra_groups, required_total)
            applicable_groups = preferred(non_extra_groups, required_total)

    elif grade is not None and 9 <= grade <= 10:
        if optional_candidates:
            required_compulsory = 5
            compulsory_ok = has_n_complete(mandatory_groups, required_compulsory)
            chosen_compulsory = preferred(mandatory_groups, required_compulsory)
            applicable_groups = chosen_compulsory + chosen_optional
            base_ok = compulsory_ok and optional_ok
        else:
            # Legacy setup with no optional flags/assignments: count 6 subjects.
            required_total = 6
            base_ok = has_n_complete(non_extra_groups, required_total)
            applicable_groups = preferred(non_extra_groups, required_total)

    elif grade is not None and 11 <= grade <= 12:
        # Any five complete non-extra subjects count, whether compulsory or optional.
        required_total = 5
        base_ok = has_n_complete(non_extra_groups, required_total)
        applicable_groups = preferred(non_extra_groups, required_total)

    else:
        if optional_candidates:
            applicable_groups = preferred(mandatory_groups) + chosen_optional
            compulsory_ok = all(group_complete[g["key"]] for g in mandatory_groups)
            base_ok = compulsory_ok and optional_ok
        else:
            applicable_groups = list(non_extra_groups)
            base_ok = all(group_complete[g["key"]] for g in non_extra_groups)

    # Every configured Extra Subject must be complete in addition to the base rule.
    extra_ok = all(group_complete[g["key"]] for g in extra_groups)
    applicable_groups.extend(extra_groups)
    complete = bool(base_ok and extra_ok)

    seen_keys = set()
    applicable_indices = []
    for group in applicable_groups:
        if group["key"] in seen_keys:
            continue
        seen_keys.add(group["key"])
        applicable_indices.extend(group["indices"])
    return complete, sorted(set(applicable_indices))

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
    _diag_start = time.perf_counter()

    try:
        conn = get_connection()
        cur = conn.cursor()

        # Use exactly the same session and roster helper as My Std.
        # This prevents My Result from using a different active session/roster.
        _diag_t = time.perf_counter()
        students, session_id, session_name = _class_students(cur, ct)
        if not session_id:
            raise RuntimeError("Active Session नहीं मिली।")

        if selected_student:
            students = [
                student for student in students
                if str(student.get("id", "")).strip() == selected_student
            ]

        print(f"[MY_RESULT_TIMING] students={(time.perf_counter()-_diag_t)*1000:.1f} ms; count={len(students)}")
        _diag_t = time.perf_counter()

        exams = _available_exams(
            cur,
            ct["class_name"].strip(),
            ct["section"].strip(),
        )

        print(f"[MY_RESULT_TIMING] exams={(time.perf_counter()-_diag_t)*1000:.1f} ms; values={exams}")
        _diag_t = time.perf_counter()

        if not selected_exam and exams:
            selected_exam = exams[0]

        if selected_exam:
            print(f"[MY_RESULT_TIMING] before_subjects_helper exam={selected_exam!r}", flush=True)
            subjects = _subjects_for_exam(
                cur,
                ct["class_name"].strip(),
                ct["section"].strip(),
                selected_exam,
            )
            print(f"[MY_RESULT_TIMING] subjects={(time.perf_counter()-_diag_t)*1000:.1f} ms; count={len(subjects)}")
            _diag_t = time.perf_counter()

            # ExamMarks is the same table used by Exam / Marks.
            # SQL में ही प्रत्येक विद्यार्थी/विषय का नवीनतम रिकॉर्ड चुनें।
            # इससे पुराने duplicate records Python में लाने की जरूरत नहीं रहती।
            # Only retrieve marks for students currently present in this teacher's
            # active roster. The previous query ranked every matching ExamMarks row
            # in the class, including old/inactive students. Direct equality on the
            # four filter columns also allows SQL Server to use a matching index.
            roster_ids = [str(s.get("id", "")).strip() for s in students if str(s.get("id", "")).strip()]
            mark_rows = []
            if roster_ids:
                marks_sql = """
                    WITH RankedMarks AS (
                        SELECT
                            StudentID,
                            SubjectName,
                            SubSubject,
                            MaxMarks,
                            ObtMarks,
                            ROW_NUMBER() OVER (
                                PARTITION BY
                                    LTRIM(RTRIM(CONVERT(NVARCHAR(100), StudentID))),
                                    UPPER(LTRIM(RTRIM(ISNULL(SubjectName,'')))),
                                    UPPER(LTRIM(RTRIM(ISNULL(SubSubject,''))))
                                ORDER BY
                                    TRY_CONVERT(DATETIME, EntryDate) DESC,
                                    EntryDate DESC,
                                    ID DESC
                            ) AS rn
                        FROM dbo.ExamMarks
                        WHERE SessionName = ?
                          AND ExamType = ?
                          AND ClassName = ?
                          AND SectionName = ?
                """
                marks_params = [
                    session_name,
                    selected_exam,
                    ct["class_name"].strip(),
                    ct["section"].strip(),
                ]
                # Restrict ranking to this teacher's active roster before SQL Server
                # sorts ExamMarks. This avoids ranking marks for old/inactive students.
                # Keep well below SQL Server's 2,100-parameter limit.
                if len(roster_ids) <= 1900:
                    marks_sql += " AND StudentID IN (" + ",".join("?" for _ in roster_ids) + ")"
                    marks_params.extend(roster_ids)

                # Only rank subjects configured for the selected exam. This is often
                # the largest speed-up when ExamMarks contains many exams/subjects.
                # Compare case-insensitively after trimming, matching the Python key
                # normalization for ordinary subject names.
                subject_names = sorted({
                    str(subject.get("subject", "")).strip().upper()
                    for subject in subjects
                    if str(subject.get("subject", "")).strip()
                })
                if subject_names and len(marks_params) + len(subject_names) < 2000:
                    marks_sql += " AND UPPER(LTRIM(RTRIM(ISNULL(SubjectName,'')))) IN (" + ",".join("?" for _ in subject_names) + ")"
                    marks_params.extend(subject_names)

                marks_sql += """
                    )
                    SELECT StudentID, SubjectName, SubSubject, MaxMarks, ObtMarks
                    FROM RankedMarks
                    WHERE rn = 1
                    OPTION (RECOMPILE)
                """
                print(f"[MY_RESULT_TIMING] exam_marks_query_start roster={len(roster_ids)} subjects={len(subject_names)}", flush=True)
                cur.execute(marks_sql, marks_params)
                print(f"[MY_RESULT_TIMING] exam_marks_execute={(time.perf_counter()-_diag_t)*1000:.1f} ms", flush=True)
                _marks_fetch_start = time.perf_counter()
                mark_rows = cur.fetchall()
                print(f"[MY_RESULT_TIMING] exam_marks_fetch={(time.perf_counter()-_marks_fetch_start)*1000:.1f} ms", flush=True)
            print(f"[MY_RESULT_TIMING] exam_marks={(time.perf_counter()-_diag_t)*1000:.1f} ms; count={len(mark_rows)}", flush=True)
            _diag_t = time.perf_counter()
            mark_map = {}
            for r in mark_rows:
                key = (
                    _norm_subject(r[0]),
                    _norm_subject(r[1]),
                    _norm_subject(r[2]),
                )
                # Ranked SQL rows are returned newest-first per student/subject/paper.
                if key not in mark_map:
                    mark_map[key] = {
                        "max_marks": _clean_value(r[3]),
                        "obt_marks": _clean_value(r[4]),
                    }

            optional_assignment_map = _optional_assignments_by_student(
                cur,
                session_id,
                ct["class_name"].strip(),
                ct["section"].strip(),
            )

            # Display student names in A-Z order (case-insensitive).
            students = sorted(
                students,
                key=lambda student: str(student.get("name", "") or "").strip().casefold(),
            )

            for student in students:
                cells = []
                paper_marked = []
                paper_max_values = []
                paper_obtained_values = []
                special_status_by_index = {}

                for subject_index, subject in enumerate(subjects):
                    rec = mark_map.get((
                        _norm_subject(student["id"]),
                        _norm_subject(subject["subject"]),
                        _norm_subject(subject["subsubject"]),
                    ))

                    if rec:
                        max_marks = rec["max_marks"] or subject["max_marks"]
                        obtained = rec["obt_marks"]
                    else:
                        max_marks = subject["max_marks"]
                        obtained = ""

                    status_code = obtained.strip().upper()
                    entered = bool(status_code and status_code not in ("—", "-"))
                    paper_marked.append(entered)
                    paper_max_values.append(max_marks)
                    paper_obtained_values.append(obtained)

                    if status_code in ("A", "M", "ABSENT", "MEDICAL"):
                        special_code = "A" if status_code in ("A", "ABSENT") else "M"
                        special_status_by_index[subject_index] = special_code
                        cells.append(special_code)
                    elif obtained == "":
                        cells.append("—")
                    else:
                        cells.append(obtained)

                # Do not hide students when the selected exam has no numeric marks.
                # Display the full My Std roster; marks can be entered in other exams (e.g. M-2).

                assigned_optional = optional_assignment_map.get(
                    _norm_subject(student["id"]), set()
                )
                is_complete, applicable_indices = _student_result_completeness(
                    ct["class_name"],
                    subjects,
                    paper_marked,
                    assigned_optional,
                )

                # Sum only the subjects used by this student's completion rule, so
                # unselected optional alternatives do not inflate totals/percentage.
                total_max = 0.0
                total_obt = 0.0
                for idx in applicable_indices:
                    if idx >= len(paper_obtained_values):
                        continue
                    status_code = paper_obtained_values[idx].strip().upper()
                    if not status_code or status_code in ("—", "-", "A", "M", "ABSENT", "MEDICAL"):
                        continue
                    try:
                        total_max += float(paper_max_values[idx] or 0)
                        total_obt += float(paper_obtained_values[idx])
                    except (TypeError, ValueError):
                        # A non-numeric status is treated as recorded but not included
                        # in numeric total; ordinary A/M states are handled above.
                        pass

                percentage = ""
                required_special_status = ""
                for idx in applicable_indices:
                    if idx in special_status_by_index:
                        required_special_status = special_status_by_index[idx]
                        break

                # Keep entry completion separate from academic outcome. Previously
                # fully entered marks were labelled PASS/FAIL, so the user could not
                # see an explicit COMPLETE status.
                if is_complete:
                    entry_status = "COMPLETE"
                    if required_special_status:
                        academic_status = required_special_status
                    elif total_max > 0:
                        percentage = f"{(total_obt / total_max) * 100:.2f}%"
                        academic_status = "PASS" if (total_obt / total_max) * 100 >= 33 else "FAIL"
                    else:
                        academic_status = "—"
                elif any(paper_marked):
                    entry_status = "PARTIAL"
                    academic_status = "—"
                else:
                    entry_status = "PENDING"
                    academic_status = "—"

                result_rows.append({
                    "srno": student["srno"],
                    "name": student["name"],
                    "marks": cells,
                    "total_max": f"{total_max:g}",
                    "total_obt": f"{total_obt:g}",
                    "percentage": percentage,
                    "entry_status": entry_status,
                    "result_status": academic_status,
                    # Backward-compatible alias for any local template still using r.status.
                    "status": entry_status,
                })

        print(f"[MY_RESULT_TIMING] python_calculation={(time.perf_counter()-_diag_t)*1000:.1f} ms")
        print(f"[MY_RESULT_TIMING] total_route={(time.perf_counter()-_diag_start)*1000:.1f} ms")
    except Exception as e:
        error = "Result load में समस्या: " + str(e)
        print(f"[MY_RESULT_TIMING] ERROR after {(time.perf_counter()-_diag_start)*1000:.1f} ms: {e}", flush=True)

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

@app.route("/my-result/print/<student_id>")
def my_result_print(student_id):
    """Print cumulative exam marks for one student; show only entered mark records."""
    ct = require_class_teacher()
    if not ct:
        return redirect(url_for("teacher_dashboard"))
    selected_exam = request.args.get("exam", "").strip()
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        students, session_id, session_name = _class_students(cur, ct)
        student = next((x for x in students if str(x.get("id", "")).strip() == str(student_id).strip()), None)
        if not student:
            return "यह विद्यार्थी आपकी assigned Class + Section में नहीं है।", 404

        exams = _available_exams(cur, ct["class_name"].strip(), ct["section"].strip())
        if not selected_exam:
            selected_exam = exams[0] if exams else ""
        if selected_exam not in exams:
            return "चुनी गई परीक्षा उपलब्ध नहीं है।", 400

        # चुनी परीक्षा तक सभी परीक्षाओं का cumulative result.
        cumulative = exams[:exams.index(selected_exam) + 1]
        subjects_out = []
        exam_totals = []
        for exam in cumulative:
            cur.execute("""
                SELECT SubjectName, SubSubject, MaxMarks, ObtMarks
                FROM dbo.ExamMarks
                WHERE SessionName = ? AND ExamType = ? AND ClassName = ? AND SectionName = ?
                  AND LTRIM(RTRIM(CONVERT(NVARCHAR(100), StudentID))) = LTRIM(RTRIM(?))
            """, (session_name, exam, ct["class_name"].strip(), ct["section"].strip(), str(student_id)))
            mark_rows = cur.fetchall()

            exam_max = 0.0
            exam_obt = 0.0
            has_numeric_max = False
            for mr in mark_rows:
                subject_name = str(mr[0] or "").strip()
                subsubject = str(mr[1] or "").strip()
                max_value = _clean_value(mr[2])
                obtained_value = _clean_value(mr[3])

                # खाली/डैश अंक वाली पंक्ति न दिखाएँ; शून्य (0) वैध अंक है।
                obtained_text = str(obtained_value).strip() if obtained_value is not None else ""
                if not subject_name or not obtained_text or obtained_text in ("—", "-"):
                    continue

                subjects_out.append({
                    "exam": exam,
                    "subject": subject_name,
                    "subsubject": subsubject,
                    "max_marks": max_value,
                    "obtained": obtained_value
                })

                try:
                    exam_max += float(max_value)
                    has_numeric_max = True
                except (TypeError, ValueError):
                    pass
                try:
                    exam_obt += float(obtained_value)
                except (TypeError, ValueError):
                    # A / ABSENT जैसे स्टेटस को कुल अंकों में न जोड़ें।
                    pass

            exam_totals.append({
                "exam": exam,
                "obtained": f"{exam_obt:g}",
                "maximum": f"{exam_max:g}" if has_numeric_max else "0"
            })

        # Master Setup subject sequence (MasterTable Category='Subject').
        cur.execute("""
            SELECT ItemName
            FROM dbo.MasterTable
            WHERE LOWER(LTRIM(RTRIM(ISNULL(Category,'')))) = 'subject'
              AND ItemName IS NOT NULL
              AND LTRIM(RTRIM(ItemName)) <> ''
            ORDER BY
                CASE WHEN TRY_CONVERT(INT, [Order]) IS NULL THEN 1 ELSE 0 END,
                TRY_CONVERT(INT, [Order]),
                ItemName
        """)
        master_subject_order = [
            str(r[0]).strip() for r in cur.fetchall()
            if r[0] is not None and str(r[0]).strip()
        ]
        master_subject_rank = {
            name.casefold(): i for i, name in enumerate(master_subject_order)
        }

        # Overall total is the sum of each exam's total, not the same subject
        # maximum accidentally counted once for every cumulative exam.
        total_max = sum(float(x["maximum"]) for x in exam_totals)
        total_obt = sum(float(x["obtained"]) for x in exam_totals)

        # Student bio columns are discovered dynamically.
        cur.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='StudentMaster'")
        cols = [str(r[0]).strip() for r in cur.fetchall()]
        lookup = {c.casefold(): c for c in cols}
        id_col = lookup.get("id")
        bio = {}
        if id_col:
            cur.execute(f"SELECT * FROM dbo.StudentMaster WHERE [{id_col}] = ?", (student_id,))
            row = cur.fetchone()
            if row:
                for i, col in enumerate(cols):
                    bio[col.casefold()] = _clean_value(row[i])

        def first(*names):
            for n in names:
                if bio.get(n.casefold()):
                    return bio[n.casefold()]
            return ""

        student.update({
            "father": first("father", "fathername", "father_name", "father's name"),
            "mother": first("mother", "mothername", "mother_name", "mother's name"),
            "dob": first("dob", "dateofbirth", "date_of_birth", "birthdate"),
            "gender": first("gender", "sex"),
            "hindi_name": first("studenthindi", "studentnamehindi", "namehindi", "hindiname"),
            "father_hindi": first("fatherhindi", "fathernamehindi", "father_hindi"),
            "mother_hindi": first("motherhindi", "mothernamehindi", "mother_hindi")
        })

        return render_template(
            "my_result_print.html",
            school_name=get_school_name(),
            student=student,
            class_name=ct["class_name"],
            section=ct["section"],
            session_name=session_name,
            selected_exam=selected_exam,
            cumulative_exams=cumulative,
            subjects=subjects_out,
            subject_order=master_subject_order,
            exam_totals=exam_totals,
            total_max=f"{total_max:g}",
            total_obt=f"{total_obt:g}"
        )
    except Exception as e:
        return "Result Print में समस्या: " + str(e), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
