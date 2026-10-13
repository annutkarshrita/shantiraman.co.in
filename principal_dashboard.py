"""Principal school-wide dashboard (read-only).

Routes and report queries are intentionally kept outside app.py.
The dashboard reads the existing SQL Server schema dynamically and never
accepts student, marks, attendance, or fee modifications from Principal.
"""
from flask import render_template, redirect, url_for, request, session
import re
import hashlib

from app import (
    app,
    get_connection,
    get_school_name,
    normalize_role,
    get_employee_master_principal,
)

# ============================================================
# PRINCIPAL DASHBOARD - ALL SCHOOL READ-ONLY OVERVIEW
# Principal identity is rechecked against EmployeeMaster on every request.
# ============================================================
def _pd_cols(cur, table_name):
    cur.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=?
    """, table_name)
    return {str(r[0]).strip().lower(): str(r[0]).strip() for r in cur.fetchall()}


def _pd_pick(cols, names):
    for name in names:
        found = cols.get(str(name).strip().lower())
        if found:
            return found
    return None


def _pd_qi(name):
    return "[" + str(name).replace("]", "]]" ) + "]"


def _pd_txt(value):
    return str(value or "").strip()


def _pd_norm(value):
    return _pd_txt(value).casefold()


def _pd_amount(value):
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(value if value is not None else "0").replace(",", "").strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _pd_money(value):
    try:
        return "₹{:,.2f}".format(float(value or 0))
    except Exception:
        return "₹0.00"


def _pd_date(value):
    if value is None or value == "":
        return None
    if hasattr(value, "date"):
        try:
            return value.date()
        except Exception:
            pass
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        try:
            from datetime import date
            return date(value.year, value.month, value.day)
        except Exception:
            pass
    from datetime import datetime
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%d-%m-%Y", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y", "%Y/%m/%d",
                "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw[:26], fmt).date()
        except Exception:
            continue
    return None


def _pd_show_date(value):
    parsed = _pd_date(value)
    return parsed.strftime("%d-%m-%Y") if parsed else _pd_txt(value)


def _pd_month(value):
    v = _pd_norm(value)
    aliases = {
        "april":"Apr", "apr":"Apr", "may":"May", "june":"Jun", "jun":"Jun",
        "july":"Jul", "jul":"Jul", "august":"Aug", "aug":"Aug",
        "september":"Sep", "sept":"Sep", "sep":"Sep", "october":"Oct", "oct":"Oct",
        "november":"Nov", "nov":"Nov", "december":"Dec", "dec":"Dec",
        "january":"Jan", "jan":"Jan", "february":"Feb", "feb":"Feb",
        "march":"Mar", "mar":"Mar"
    }
    return aliases.get(v, _pd_txt(value))


def _pd_fee_type(value):
    v = " ".join(_pd_norm(value).replace(".", "").replace("-", " ").split())
    return {"genral fee":"genfee", "general fee":"genfee", "gen fee":"genfee",
            "general":"genfee", "genral":"genfee"}.get(v, v)


def _pd_class_key(class_name, section):
    return (_pd_norm(class_name), _pd_norm(section))


_PD_CLASS_ORDER = {
    "pre nursery": 0, "prenursery": 0, "playgroup": 0, "play group": 0,
    "nursery": 1, "lkg": 2, "lower kg": 2, "lower kindergarten": 2,
    "ukg": 3, "upper kg": 3, "upper kindergarten": 3,
    "prep": 4, "preparatory": 4, "kg": 4,
}
_PD_CLASS_WORDS = ["one", "two", "three", "four", "five", "six", "seven",
                   "eight", "nine", "ten", "eleven", "twelve"]
_PD_ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"]
for _class_index, _class_word in enumerate(_PD_CLASS_WORDS, start=5):
    _PD_CLASS_ORDER[_class_word] = _class_index
    _PD_CLASS_ORDER[str(_class_index - 4)] = _class_index
    _PD_CLASS_ORDER[_PD_ROMAN[_class_index - 5]] = _class_index


def _pd_natural_sort_key(value):
    """Natural string order for section names such as A, B, 2, 10."""
    raw = _pd_norm(value)
    raw = re.sub(r"^section\s*", "", raw).strip()
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r"(\d+)", raw) if part != "")


def _pd_class_sort_key(value):
    raw = _pd_norm(value)
    raw = re.sub(r"^class\s+", "", raw).strip().replace(".", "")
    raw = re.sub(r"[-_]+", " ", raw)
    compact = " ".join(raw.split())
    compact_no_space = compact.replace(" ", "")
    index = _PD_CLASS_ORDER.get(compact, _PD_CLASS_ORDER.get(compact_no_space))
    if index is not None:
        return (0, index, _pd_natural_sort_key(compact))
    return (1, 999, _pd_natural_sort_key(compact))


def _pd_pair_sort_key(class_name, section):
    return (_pd_class_sort_key(class_name), _pd_natural_sort_key(section), _pd_norm(section))


def _pd_exam_identity(value):
    """Normalize exam labels like M-1, M1 and M 1 for matching/filtering."""
    compact = re.sub(r"[^a-z0-9]+", "", _pd_norm(value))
    aliases = {
        "monthly1": "m1", "month1": "m1", "m01": "m1",
        "monthly2": "m2", "month2": "m2", "m02": "m2",
        "monthly3": "m3", "month3": "m3", "m03": "m3",
        "annual": "yearly", "annualexam": "yearly", "year": "yearly",
    }
    return aliases.get(compact, compact)


def _pd_exam_sort_key(value, master_order=None):
    """Sort exams in dbo.MasterTable order; use the common sequence as fallback."""
    key = _pd_exam_identity(value)
    master_order = master_order or {}
    if key in master_order:
        return (0, master_order[key], key)
    fallback = {"m1": 0, "m2": 1, "m3": 2, "yearly": 3}
    if key in fallback:
        return (1, fallback[key], key)
    return (2, 9999, _pd_natural_sort_key(value))


def _pd_load_master_order(cur):
    """Read Class, Section and Exam Type display order from the existing MasterTable."""
    empty_items = {"class": [], "section": [], "exam type": []}
    empty_orders = {"class": {}, "section": {}, "exam type": {}}
    try:
        cols = _pd_cols(cur, "MasterTable")
        category_col = _pd_pick(cols, ["Category", "MasterType", "Type"])
        item_col = _pd_pick(cols, ["ItemName", "Item Name", "Name", "Value"])
        order_col = _pd_pick(cols, ["Order", "SortOrder", "Sequence", "DisplayOrder"])
        if not category_col or not item_col:
            return empty_items, empty_orders
        order_expr = (
            f"TRY_CONVERT(INT, NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(order_col)}))), ''))"
            if order_col else "CAST(NULL AS INT)"
        )
        cur.execute(
            f"SELECT {_pd_qi(category_col)}, {_pd_qi(item_col)}, {order_expr} AS MasterOrder "
            f"FROM dbo.MasterTable "
            f"WHERE LOWER(LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(category_col)})))) "
            f"IN ('class','section','exam type') "
            f"AND {_pd_qi(item_col)} IS NOT NULL "
            f"AND LTRIM(RTRIM(CONVERT(NVARCHAR(255), {_pd_qi(item_col)}))) <> '' "
            f"ORDER BY LOWER(LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(category_col)})))), "
            f"CASE WHEN {order_expr} IS NULL THEN 1 ELSE 0 END, {order_expr}, "
            f"LTRIM(RTRIM(CONVERT(NVARCHAR(255), {_pd_qi(item_col)})))"
        )
        seen = {"class": set(), "section": set(), "exam type": set()}
        for row in cur.fetchall():
            category = " ".join(_pd_norm(row[0]).split())
            item = _pd_txt(row[1])
            if category not in empty_items or not item:
                continue
            identity = _pd_exam_identity(item) if category == "exam type" else _pd_norm(item)
            if not identity or identity in seen[category]:
                continue
            seen[category].add(identity)
            empty_orders[category][identity] = len(empty_items[category])
            empty_items[category].append(item)
    except Exception as exc:
        # MasterTable ordering is a display enhancement; keep the report usable if
        # a deployment has a different/older MasterTable schema.
        print("PRINCIPAL MASTER ORDER WARNING:", repr(exc))
        return {"class": [], "section": [], "exam type": []}, {"class": {}, "section": {}, "exam type": {}}
    return empty_items, empty_orders


def _pd_master_pair_sort_key(class_name, section, master_orders):
    class_id = _pd_norm(class_name)
    section_id = _pd_norm(section)
    class_orders = master_orders.get("class", {})
    section_orders = master_orders.get("section", {})
    class_key = (0, class_orders[class_id]) if class_id in class_orders else (1, _pd_class_sort_key(class_name))
    section_key = (0, section_orders[section_id]) if section_id in section_orders else (1, _pd_natural_sort_key(section))
    return class_key, section_key, _pd_norm(class_name), _pd_norm(section)


def _pd_load_class_teachers(cur, session_id, session_name):
    cols = _pd_cols(cur, "ClassTeacherAssign")
    emp_cols = _pd_cols(cur, "EmployeeMaster")
    class_col = _pd_pick(cols, ["Class", "ClassName"])
    section_col = _pd_pick(cols, ["Section", "SectionName"])
    employee_col = _pd_pick(cols, ["EmployeeID", "EmpID"])
    active_col = _pd_pick(cols, ["IsActive", "Active"])
    id_col = _pd_pick(cols, ["ID", "AssignID"])
    session_col = _pd_pick(cols, ["Session", "SessionName", "SessionID", "SessionId"])
    emp_id = _pd_pick(emp_cols, ["ID", "EmployeeID"])
    emp_name = _pd_pick(emp_cols, ["Name", "EmployeeName"])
    if not all([class_col, section_col, employee_col, emp_id, emp_name]):
        return {}, "Class Teacher assignment is missing required columns."

    session_expr = "C." + _pd_qi(session_col) if session_col else "CAST('' AS NVARCHAR(100))"
    id_expr = "C." + _pd_qi(id_col) if id_col else "CAST(0 AS BIGINT)"
    active_sql = ""
    if active_col:
        active_sql = " AND LOWER(LTRIM(RTRIM(CONVERT(VARCHAR(20), ISNULL(C." + _pd_qi(active_col) + ",0))))) IN ('1','true','yes','active') "
    sql = f"""
        SELECT {id_expr}, C.{_pd_qi(class_col)}, C.{_pd_qi(section_col)},
               {session_expr}, E.{_pd_qi(emp_name)}
        FROM dbo.ClassTeacherAssign C
        LEFT JOIN dbo.EmployeeMaster E
          ON LTRIM(RTRIM(CONVERT(NVARCHAR(100), E.{_pd_qi(emp_id)}))) =
             LTRIM(RTRIM(CONVERT(NVARCHAR(100), C.{_pd_qi(employee_col)})))
        WHERE 1=1 {active_sql}
    """
    cur.execute(sql)
    session_targets = {_pd_norm(session_name), _pd_norm(session_id)} - {""}
    candidates = {}
    for row in cur.fetchall():
        assign_id = row[0]
        class_name, section = _pd_txt(row[1]), _pd_txt(row[2])
        assignment_session, teacher_name = _pd_txt(row[3]), _pd_txt(row[4])
        if not class_name or not section or not teacher_name:
            continue
        # If assignment table has session values, prefer only the active session.
        if session_col and assignment_session and _pd_norm(assignment_session) not in session_targets:
            continue
        key = _pd_class_key(class_name, section)
        rank = (1 if assignment_session and _pd_norm(assignment_session) in session_targets else 0,
                int(assign_id) if str(assign_id or "").isdigit() else 0)
        if key not in candidates or rank > candidates[key][0]:
            candidates[key] = (rank, teacher_name)
    return {key: value[1] for key, value in candidates.items()}, None


def _pd_exam_summary(cur, session_id, session_name, pairs, students):
    """Build class/section result-posting counts from explicit submission records when available."""
    assignment_cols = _pd_cols(cur, "MobileTeacherSubjectAssign")
    cl = _pd_pick(assignment_cols, ["Class", "ClassName"])
    sec = _pd_pick(assignment_cols, ["Section", "SectionName"])
    subj = _pd_pick(assignment_cols, ["Subject", "SubjectName"])
    exam = _pd_pick(assignment_cols, ["Exam", "ExamType"])
    subsubj = _pd_pick(assignment_cols, ["SubSubject", "SubSubjectName", "Paper"])
    ass_employee = _pd_pick(assignment_cols, ["EmployeeID", "EmpID", "TeacherID"])
    ass_session = _pd_pick(assignment_cols, ["SessionID", "SessionId", "Session"])
    ass_active = _pd_pick(assignment_cols, ["IsActive", "Active"])
    if not all([cl, sec, subj, exam]):
        return [], [], "MobileTeacherSubjectAssign is missing one or more required columns."

    fields = [
        f"LTRIM(RTRIM(CONVERT(NVARCHAR(255), ISNULL({_pd_qi(col)},''))))"
        for col in (cl, sec, subj, exam)
    ]
    sub_expr = (
        f"LTRIM(RTRIM(CONVERT(NVARCHAR(255), ISNULL({_pd_qi(subsubj)},''))))"
        if subsubj else "CAST('' AS NVARCHAR(255))"
    )
    where = []
    params = []
    if ass_session:
        where.append(f"LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(ass_session)}))) = ?")
        params.append(str(session_id))
    if ass_active:
        where.append(
            f"LOWER(LTRIM(RTRIM(CONVERT(VARCHAR(20), ISNULL({_pd_qi(ass_active)},0))))) "
            "IN ('1','true','yes','active')"
        )
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    employee_expr = f"LTRIM(RTRIM(CONVERT(NVARCHAR(100), ISNULL({_pd_qi(ass_employee)},''))))" if ass_employee else "CAST('' AS NVARCHAR(100))"
    cur.execute(
        "SELECT DISTINCT " + ", ".join(fields + [sub_expr, employee_expr]) +
        " FROM dbo.MobileTeacherSubjectAssign" + where_sql,
        *params
    )

    pair_lookup = {_pd_class_key(p["class_name"], p["section"]): p for p in pairs}
    assignment_map = {}
    for row in cur.fetchall():
        class_name, section, subject_name, exam_name, paper = [_pd_txt(x) for x in row[:5]]
        employee_id = _pd_txt(row[5])
        pair_key = _pd_class_key(class_name, section)
        if pair_key not in pair_lookup or not subject_name or not exam_name:
            continue
        key = (pair_key, _pd_norm(exam_name), _pd_norm(subject_name), _pd_norm(paper))
        if key not in assignment_map:
            assignment_map[key] = {
                "pair_key": pair_key, "class_name": class_name, "section": section,
                "subject": subject_name, "exam": exam_name, "paper": paper,
                "key": key, "employee_ids": set()
            }
        if employee_id:
            assignment_map[key]["employee_ids"].add(_pd_norm(employee_id))
    assignments = list(assignment_map.values())
    if not assignments:
        return [], [], "No active exam/subject assignments were found for the current session."

    # Active students per class/section.
    roster = {}
    for st in students:
        roster.setdefault(_pd_class_key(st["class_name"], st["section"]), set()).add(_pd_norm(st["id"]))

    # Match optional-subject eligibility with the teacher marks-status logic:
    # an optional flag OR an existing StudentOptionalSubject assignment means only
    # the selected students should be included in the expected roster.
    optional_subjects = set()
    ss_cols = _pd_cols(cur, "SubjectSetup")
    ss_class = _pd_pick(ss_cols, ["Class", "ClassName"])
    ss_section = _pd_pick(ss_cols, ["Section", "SectionName"])
    ss_subject = _pd_pick(ss_cols, ["Subject", "SubjectName"])
    ss_optional = _pd_pick(ss_cols, ["IsOptional", "OptionalSubject", "IsOptionalSubject", "Optional"])
    if all([ss_class, ss_section, ss_subject, ss_optional]):
        cur.execute(
            f"SELECT {_pd_qi(ss_class)}, {_pd_qi(ss_section)}, {_pd_qi(ss_subject)}, {_pd_qi(ss_optional)} "
            "FROM dbo.SubjectSetup"
        )
        for row in cur.fetchall():
            flag = _pd_norm(row[3])
            if flag in ("1", "true", "yes", "y", "optional"):
                optional_subjects.add((_pd_class_key(row[0], row[1]), _pd_norm(row[2])))

    optional_students = {}
    optional_subject_rows = set()
    oso_cols = _pd_cols(cur, "StudentOptionalSubject")
    oso_sid = _pd_pick(oso_cols, ["SessionID", "SessionId", "Session"])
    oso_student = _pd_pick(oso_cols, ["StudentID", "StudentId", "StdID"])
    oso_class = _pd_pick(oso_cols, ["ClassName", "Class"])
    oso_section = _pd_pick(oso_cols, ["SectionName", "Section"])
    oso_subject = _pd_pick(oso_cols, ["SubjectName", "Subject"])
    if all([oso_sid, oso_student, oso_class, oso_section, oso_subject]):
        cur.execute(
            f"SELECT {_pd_qi(oso_student)}, {_pd_qi(oso_class)}, {_pd_qi(oso_section)}, {_pd_qi(oso_subject)} "
            f"FROM dbo.StudentOptionalSubject WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(oso_sid)}))) = ?",
            str(session_id)
        )
        for row in cur.fetchall():
            pair_key = _pd_class_key(row[1], row[2])
            subject_key = _pd_norm(row[3])
            optional_students.setdefault((pair_key, subject_key), set()).add(_pd_norm(row[0]))
            optional_subject_rows.add((pair_key, subject_key))

    # Read the latest row for each student/paper, so cleared/old duplicate rows
    # do not make a paper appear submitted. Column names are detected dynamically.
    em_cols = _pd_cols(cur, "ExamMarks")
    em_session = _pd_pick(em_cols, ["SessionName", "Session", "SessionID", "SessionId"])
    em_exam = _pd_pick(em_cols, ["ExamType", "Exam"])
    em_class = _pd_pick(em_cols, ["ClassName", "Class"])
    em_section = _pd_pick(em_cols, ["SectionName", "Section"])
    em_subject = _pd_pick(em_cols, ["SubjectName", "Subject"])
    em_paper = _pd_pick(em_cols, ["SubSubject", "SubSubjectName", "Paper"])
    em_student = _pd_pick(em_cols, ["StudentID", "StudentId", "StdID"])
    em_obt = _pd_pick(em_cols, ["ObtMarks", "MarksObtained", "ObtainedMarks"])
    em_status = _pd_pick(em_cols, ["AttendanceStatus", "Status"])
    em_entry = _pd_pick(em_cols, ["EntryDate", "MarkEntryAt", "CreatedAt"])
    em_id = _pd_pick(em_cols, ["ID", "ExamMarkID"])
    if not all([em_session, em_exam, em_class, em_section, em_subject, em_student]) or not (em_obt or em_status):
        return [], [], "ExamMarks is missing columns required to check result posting."

    em_session_value = session_name if "name" in em_session.lower() else str(session_id)
    select_cols = [
        f"CONVERT(NVARCHAR(100), {_pd_qi(em_student)}) AS StudentID",
        f"CONVERT(NVARCHAR(255), {_pd_qi(em_exam)}) AS ExamType",
        f"CONVERT(NVARCHAR(255), {_pd_qi(em_class)}) AS ClassName",
        f"CONVERT(NVARCHAR(255), {_pd_qi(em_section)}) AS SectionName",
        f"CONVERT(NVARCHAR(255), {_pd_qi(em_subject)}) AS SubjectName",
        (f"CONVERT(NVARCHAR(255), {_pd_qi(em_paper)}) AS Paper" if em_paper else "CAST('' AS NVARCHAR(255)) AS Paper"),
        (f"CONVERT(NVARCHAR(100), {_pd_qi(em_obt)}) AS ObtMarks" if em_obt else "CAST(NULL AS NVARCHAR(100)) AS ObtMarks"),
        (f"CONVERT(NVARCHAR(100), {_pd_qi(em_status)}) AS AttendanceStatus" if em_status else "CAST(NULL AS NVARCHAR(100)) AS AttendanceStatus"),
        (f"TRY_CONVERT(DATETIME2, {_pd_qi(em_entry)}) AS EntryStamp" if em_entry else "CAST(NULL AS DATETIME2) AS EntryStamp"),
        (f"CONVERT(NVARCHAR(100), {_pd_qi(em_id)}) AS RecordID" if em_id else "CAST('' AS NVARCHAR(100)) AS RecordID"),
    ]
    cur.execute(
        f"SELECT {', '.join(select_cols)} FROM dbo.ExamMarks "
        f"WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(em_session)}))) = ?",
        em_session_value
    )
    latest = {}
    for row in cur.fetchall():
        student_id, exam_name, class_name, section, subject_name, paper = [_pd_txt(x) for x in row[:6]]
        if not student_id:
            continue
        key = (_pd_class_key(class_name, section), _pd_norm(exam_name), _pd_norm(subject_name), _pd_norm(paper), _pd_norm(student_id))
        stamp = row[8]
        record_id = _pd_txt(row[9])
        rank = (stamp is not None, stamp or "", record_id)
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, _pd_txt(row[6]), _pd_txt(row[7]))

    marks_by_assignment = {}
    for key, (_rank, obtained, status) in latest.items():
        if obtained != "" or status != "":
            akey = key[:4]
            marks_by_assignment.setdefault(akey, set()).add(key[4])

    # Use the explicit Submit action as the source of truth when the ERP has the
    # teacher submission tracking table. This intentionally allows a submitted
    # monthly result to show Submitted even if the visible marks count is partial.
    submission_cols = _pd_cols(cur, "TeacherMarksSubmission")
    ts_session = _pd_pick(submission_cols, ["SessionID", "SessionId", "Session"])
    ts_employee = _pd_pick(submission_cols, ["EmployeeID", "EmpID", "TeacherID"])
    ts_exam = _pd_pick(submission_cols, ["ExamType", "Exam"])
    ts_class = _pd_pick(submission_cols, ["ClassName", "Class"])
    ts_section = _pd_pick(submission_cols, ["SectionName", "Section"])
    ts_subject = _pd_pick(submission_cols, ["SubjectName", "Subject"])
    ts_paper = _pd_pick(submission_cols, ["SubSubject", "SubSubjectName", "Paper"])
    ts_status = _pd_pick(submission_cols, ["Status", "SubmissionStatus"])
    submission_tracking_schema_available = all([ass_employee, ts_session, ts_employee, ts_exam, ts_class, ts_section, ts_subject, ts_status])
    submitted_assignment_teachers = set()
    tracked_assignment_teachers = set()
    if submission_tracking_schema_available:
        paper_expr = f"CONVERT(NVARCHAR(255), ISNULL({_pd_qi(ts_paper)},''))" if ts_paper else "CAST('' AS NVARCHAR(255))"
        cur.execute(
            f"SELECT {_pd_qi(ts_employee)}, {_pd_qi(ts_exam)}, {_pd_qi(ts_class)}, "
            f"{_pd_qi(ts_section)}, {_pd_qi(ts_subject)}, {paper_expr}, {_pd_qi(ts_status)} "
            f"FROM dbo.TeacherMarksSubmission WHERE "
            f"LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(ts_session)})))=?",
            str(session_id)
        )
        assignment_employees = {assn["key"]: assn.get("employee_ids", set()) for assn in assignments}
        for row in cur.fetchall():
            emp_id, exam_name, class_name, section, subject_name, paper = [_pd_txt(x) for x in row[:6]]
            status = _pd_norm(row[6])
            base_key = (
                _pd_class_key(class_name, section), _pd_norm(exam_name),
                _pd_norm(subject_name), _pd_norm(paper)
            )
            submit_key = base_key + (_pd_norm(emp_id),)
            assigned_employees = assignment_employees.get(base_key, set())
            if _pd_norm(emp_id) and _pd_norm(emp_id) in assigned_employees:
                tracked_assignment_teachers.add(submit_key)
                if status == "submitted":
                    submitted_assignment_teachers.add(submit_key)
    # Some older databases have no submission rows/table. If there are applicable
    # rows, respect the explicit Submit/Pending status. Otherwise use the legacy
    # saved-result rule so existing deployments continue to show their posted work.
    submission_tracking_available = bool(tracked_assignment_teachers)

    posting_report = []
    summary_map = {}
    for assn in assignments:
        pair_key = assn["pair_key"]
        subject_key = (pair_key, _pd_norm(assn["subject"]))
        expected = set(roster.get(pair_key, set()))
        is_optional = subject_key in optional_subjects or subject_key in optional_subject_rows
        if is_optional:
            expected &= optional_students.get(subject_key, set())
        recorded = marks_by_assignment.get(assn["key"], set())
        mark_count = len(expected & recorded)
        total_count = len(expected)
        teacher_ids = assn.get("employee_ids", set())
        explicitly_submitted = submission_tracking_available and any(
            (pair_key, _pd_norm(assn["exam"]), _pd_norm(assn["subject"]),
             _pd_norm(assn["paper"]), teacher_id) in submitted_assignment_teachers
            for teacher_id in teacher_ids
        )
        # For the Principal's posting overview, a saved result record means the
        # subject/paper has been posted. Do not mark it Pending only because some
        # students still have blank marks. An explicit Submit record also counts.
        submitted = explicitly_submitted or (total_count > 0 and mark_count > 0)
        posting_report.append({
            "class_name": assn["class_name"], "section": assn["section"],
            "exam": assn["exam"], "subject": assn["subject"], "paper": assn["paper"],
            "mark_count": mark_count, "total_students": total_count,
            "status": "Submitted" if submitted else "Pending",
            "status_class": "submitted" if submitted else "pending",
        })
        summary_key = (pair_key, _pd_norm(assn["exam"]))
        if summary_key not in summary_map:
            summary_map[summary_key] = {
                "class_name": assn["class_name"], "section": assn["section"],
                "exam": assn["exam"], "total_subjects": 0, "completed": 0, "pending": 0
            }
        summary_map[summary_key]["total_subjects"] += 1
        if submitted:
            summary_map[summary_key]["completed"] += 1
        else:
            summary_map[summary_key]["pending"] += 1

    pair_order = {_pd_class_key(p["class_name"], p["section"]): i for i, p in enumerate(pairs)}
    summary_report = list(summary_map.values())
    summary_report.sort(key=lambda r: (pair_order.get(_pd_class_key(r["class_name"], r["section"]), 9999), _pd_norm(r["exam"])))
    posting_report.sort(key=lambda r: (pair_order.get(_pd_class_key(r["class_name"], r["section"]), 9999), _pd_norm(r["exam"]), _pd_norm(r["subject"]), _pd_norm(r["paper"])))
    return summary_report, posting_report, None

def _pd_attendance_summary(cur, session_id, pairs, students):
    cols = _pd_cols(cur, "AttendanceTable")
    date_col = _pd_pick(cols, ["AttDate", "AttendanceDate", "Date"])
    srno_col = _pd_pick(cols, ["SrNo", "SRNo", "StudentSrNo"])
    sess_col = _pd_pick(cols, ["SessionID", "SessionId", "Session"])
    status_col = _pd_pick(cols, ["Status", "AttendanceStatus"])
    if not all([date_col, srno_col, sess_col, status_col]):
        return [], "AttendanceTable is missing required Date, SRNo, SessionID, or Status columns.", ""
    cur.execute(f"""SELECT {_pd_qi(date_col)}, {_pd_qi(srno_col)}, {_pd_qi(status_col)}
                    FROM dbo.AttendanceTable
                    WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(sess_col)}))) = ?""", str(session_id))
    records = []
    latest_date = None
    for row in cur.fetchall():
        d = _pd_date(row[0])
        srno = _pd_norm(row[1])
        status = _pd_txt(row[2])
        if not d or not srno or not status:
            continue
        records.append((d, srno))
        if latest_date is None or d > latest_date:
            latest_date = d
    saved_srnos = {srno for d, srno in records if d == latest_date} if latest_date else set()
    by_pair = {}
    for st in students:
        key = _pd_class_key(st["class_name"], st["section"])
        row = by_pair.setdefault(key, {"class_name": st["class_name"], "section": st["section"],
                                       "total_students": 0, "submitted": 0, "pending": 0})
        row["total_students"] += 1
        srno = _pd_norm(st.get("attendance_srno") or st.get("srno"))
        if latest_date and srno and srno in saved_srnos:
            row["submitted"] += 1
        else:
            row["pending"] += 1
    pair_order = {_pd_class_key(p["class_name"], p["section"]): i for i, p in enumerate(pairs)}
    report = list(by_pair.values())
    report.sort(key=lambda r: pair_order.get(_pd_class_key(r["class_name"], r["section"]), 9999))
    date_label = latest_date.strftime("%d-%m-%Y") if latest_date else "No attendance recorded"
    return report, None, date_label



def _pd_attendance_detail(cur, session_id, selected_students, attendance_date_label=""):
    """Return read-only attendance status for the same latest school-wide date used by the summary."""
    cols = _pd_cols(cur, "AttendanceTable")
    date_col = _pd_pick(cols, ["AttDate", "AttendanceDate", "Date"])
    srno_col = _pd_pick(cols, ["SrNo", "SRNo", "StudentSrNo"])
    sess_col = _pd_pick(cols, ["SessionID", "SessionId", "Session"])
    status_col = _pd_pick(cols, ["Status", "AttendanceStatus"])
    if not all([date_col, srno_col, sess_col, status_col]):
        return [], "", "AttendanceTable is missing required Date, SRNo, SessionID, or Status columns."

    target_srnos = {
        _pd_norm(st.get("attendance_srno") or st.get("srno"))
        for st in selected_students
        if _pd_norm(st.get("attendance_srno") or st.get("srno"))
    }
    records = []
    target_date = _pd_date(attendance_date_label)
    if target_srnos and target_date:
        cur.execute(
            f"SELECT {_pd_qi(date_col)}, {_pd_qi(srno_col)}, {_pd_qi(status_col)} "
            f"FROM dbo.AttendanceTable "
            f"WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(sess_col)}))) = ?",
            str(session_id),
        )
        for row in cur.fetchall():
            parsed_date = _pd_date(row[0])
            srno_key = _pd_norm(row[1])
            status_text = _pd_txt(row[2])
            if parsed_date != target_date or srno_key not in target_srnos or not status_text:
                continue
            records.append((parsed_date, srno_key, status_text))

    status_by_srno = {}
    if target_date:
        # Use exactly the same school-wide latest date as the summary, so the
        # class counts and the student-level View are consistent.
        for rec_date, srno_key, raw_status in records:
            if rec_date != target_date:
                continue
            key = _pd_norm(raw_status)
            if key in ("present", "p", "1", "1.0"):
                label, css = "Present", "good"
            elif key in ("absent", "a", "0", "0.0"):
                label, css = "Absent", "pending"
            elif key in ("medical", "m"):
                label, css = "Medical", "no-fee"
            else:
                label, css = raw_status, "pending"
            status_by_srno[srno_key] = {"status": label, "status_class": css}

    detail_rows = []
    for st in selected_students:
        srno_key = _pd_norm(st.get("attendance_srno") or st.get("srno"))
        saved = status_by_srno.get(srno_key)
        detail_rows.append({
            "id": st.get("id", ""),
            "srno": st.get("srno", ""),
            "name": st.get("name", ""),
            "father": st.get("father", ""),
            "mobile": st.get("mobile", ""),
            "status": saved["status"] if saved else "Not Saved",
            "status_class": saved["status_class"] if saved else "pending",
        })

    date_label = target_date.strftime("%d-%m-%Y") if target_date else "No attendance recorded"
    return detail_rows, date_label, None

def _pd_fee_calculation(cur, session_id, students):
    """Return fee status per student using the same monthly setup/receipt model as My Fee."""
    fs_cols = _pd_cols(cur, "FeeSetup")
    fr_cols = _pd_cols(cur, "Feerecept")
    fs = {
        "session": _pd_pick(fs_cols, ["SessionID", "SessionId", "Session"]),
        "class": _pd_pick(fs_cols, ["Class", "ClassName"]),
        "section": _pd_pick(fs_cols, ["Section", "SectionName", "Sec"]),
        "month": _pd_pick(fs_cols, ["Month", "Months"]),
        "type": _pd_pick(fs_cols, ["FeeType", "Fee Type", "Feetype"]),
        "oldnew": _pd_pick(fs_cols, ["OldNew", "Old/New", "StudentType"]),
        "amount": _pd_pick(fs_cols, ["Amount", "FeeAmount"]),
    }
    fr = {
        "session": _pd_pick(fr_cols, ["SessionID", "SessionId", "Session"]),
        "srno": _pd_pick(fr_cols, ["SRNo", "SrNo", "ScholarNo"]),
        "month": _pd_pick(fr_cols, ["Month", "Months"]),
        "amount": _pd_pick(fr_cols, ["AmountPaid", "Amount", "PaidAmount"]),
    }
    if not all(fs.values()) or not all(fr.values()):
        raise RuntimeError("FeeSetup or Feerecept is missing required fee columns.")

    cur.execute(
        f"SELECT {_pd_qi(fs['class'])}, {_pd_qi(fs['section'])}, {_pd_qi(fs['month'])}, "
        f"{_pd_qi(fs['type'])}, {_pd_qi(fs['oldnew'])}, {_pd_qi(fs['amount'])} "
        f"FROM dbo.FeeSetup WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(fs['session'])}))) = ?",
        str(session_id)
    )
    setup = {}
    for row in cur.fetchall():
        key = (_pd_class_key(row[0], row[1]), _pd_month(row[2]), _pd_fee_type(row[3]), _pd_norm(row[4]))
        setup[key] = setup.get(key, _pd_amount(0)) + _pd_amount(row[5])

    cur.execute(
        f"SELECT {_pd_qi(fr['srno'])}, {_pd_qi(fr['month'])}, {_pd_qi(fr['amount'])} "
        f"FROM dbo.Feerecept WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), {_pd_qi(fr['session'])}))) = ?",
        str(session_id)
    )
    receipts = {}
    for row in cur.fetchall():
        key = (_pd_norm(row[0]), _pd_month(row[1]))
        receipts[key] = receipts.get(key, _pd_amount(0)) + _pd_amount(row[2])

    month_order = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]
    out = []
    for st in students:
        pair_key = _pd_class_key(st["class_name"], st["section"])
        fee_type = _pd_fee_type(st.get("fee_type", ""))
        oldnew = "new" if _pd_txt(st.get("student_session", "")) == _pd_txt(session_id) else "old"
        srno = _pd_norm(st.get("receipt_srno") or st.get("attendance_srno") or st.get("srno"))
        total_fee = _pd_amount(0)
        total_paid = _pd_amount(0)
        total_balance = _pd_amount(0)
        monthly = []
        for month in month_order:
            expected = setup.get((pair_key, month, fee_type, oldnew), _pd_amount(0))
            paid = receipts.get((srno, month), _pd_amount(0))
            balance = max(expected - paid, _pd_amount(0))
            if expected or paid:
                total_fee += expected
                total_paid += min(paid, expected) if expected >= 0 else _pd_amount(0)
                total_balance += balance
                monthly.append({"month": month, "fee": expected, "paid": paid, "balance": balance})
        if total_fee <= 0 and total_paid <= 0:
            status = "No Fee Setup"
        elif total_balance > 0:
            status = "Due"
        else:
            status = "Paid"
        out.append({
            **st, "total_fee": total_fee, "total_paid": total_paid, "balance": total_balance,
            "total_fee_display": _pd_money(total_fee), "total_paid_display": _pd_money(total_paid),
            "balance_display": _pd_money(total_balance), "status": status,
            "status_class": "due" if status == "Due" else ("paid" if status == "Paid" else "no-fee"),
            "monthly": monthly,
        })
    return out


def _pd_fee_summary(cur, session_id, pairs, students):
    try:
        details = _pd_fee_calculation(cur, session_id, students)
    except Exception as exc:
        return [], str(exc)

    fee_by_pair = {}
    for st in details:
        pair_key = _pd_class_key(st["class_name"], st["section"])
        row = fee_by_pair.setdefault(pair_key, {
            "class_name": st["class_name"], "section": st["section"],
            "class_teacher": st.get("class_teacher", "—"), "total_students": 0,
            "paid": 0, "due": 0, "no_fee_setup": 0,
            "total_fee": _pd_amount(0), "collected": _pd_amount(0),
        })
        row["total_students"] += 1
        if st["status"] == "Paid":
            row["paid"] += 1
        elif st["status"] == "Due":
            row["due"] += 1
        else:
            row["no_fee_setup"] += 1
        row["total_fee"] += st["total_fee"]
        row["collected"] += st["total_paid"]

    pair_order = {_pd_class_key(p["class_name"], p["section"]): i for i, p in enumerate(pairs)}
    report = list(fee_by_pair.values())
    report.sort(key=lambda r: pair_order.get(_pd_class_key(r["class_name"], r["section"]), 9999))
    for row in report:
        row["total_fee_display"] = _pd_money(row["total_fee"])
        row["collected_display"] = _pd_money(row["collected"])
    return report, None


@app.route("/principal-dashboard", methods=["GET"])
def principal_dashboard():
    """Read-only school-wide reports for the authenticated Principal."""
    if "employee_id" not in session:
        return redirect(url_for("home"))
    if normalize_role(session.get("role", "")) != "principal":
        return redirect(url_for("teacher_dashboard"))

    try:
        principal, auth_error = get_employee_master_principal(session.get("user_email", ""))
    except Exception as auth_exception:
        print("PRINCIPAL DASHBOARD AUTH ERROR:", repr(auth_exception))
        session.clear()
        return redirect(url_for("home"))
    if not principal or str(principal.get("employee_id")) != str(session.get("employee_id")):
        session.clear()
        return redirect(url_for("home"))

    view = request.args.get("view", "home").strip().lower() or "home"
    selected_class = request.args.get("class_name", "").strip()
    selected_section = request.args.get("section", "").strip()
    conn = cur = None
    error = None
    report_warnings = {}
    pairs = []
    all_students = []
    students = []
    exam_report = []
    exam_posting_report = []
    attendance_report = []
    attendance_detail_students = []
    attendance_detail_date = ""
    attendance_detail_totals = {"total": 0, "present": 0, "absent": 0, "medical": 0, "not_saved": 0}
    fee_report = []
    fee_detail_students = []
    fee_detail_totals = {"students": 0, "due": 0, "balance_display": _pd_money(0)}
    session_name = ""
    attendance_date = ""
    schoolwide_count = 0
    schoolwide_boys = 0
    schoolwide_girls = 0
    gender_counts_available = False
    class_teacher_map = {}
    student_view_ready = False
    selected_teacher_name = ""
    selected_pair_key = None
    selected_exam_class = request.args.get("exam_class", "All").strip() or "All"
    selected_exam_section = request.args.get("exam_section", "All").strip() or "All"
    selected_exam_type = request.args.get("exam_type", "All").strip() or "All"
    exam_filter_applied = request.args.get("exam_filter_applied", "") == "1"
    exam_type_options = []
    exam_section_options = []
    exam_filter_ready = False
    fee_student_detail = None
    selected_student_id = request.args.get("student_id", "").strip()

    try:
        from class_teacher_common import get_active_session
        conn = get_connection()
        cur = conn.cursor()
        session_id, session_name = get_active_session()
        if not session_id:
            raise RuntimeError("No active session is configured.")

        master_items, master_order_maps = _pd_load_master_order(cur)

        sa_cols = _pd_cols(cur, "StudentAcademic")
        sm_cols = _pd_cols(cur, "StudentMaster")
        sa_student = _pd_pick(sa_cols, ["StudentID", "StudentId", "StdID"])
        sa_session = _pd_pick(sa_cols, ["SessionID", "SessionId", "Session"])
        sa_class = _pd_pick(sa_cols, ["Class", "ClassName"])
        sa_section = _pd_pick(sa_cols, ["Section", "SectionName", "Sec"])
        sa_status = _pd_pick(sa_cols, ["SessStatus", "Status", "IsActive"])
        sa_srno = _pd_pick(sa_cols, ["SRNo", "SrNo", "Sr_No", "ScholarNo", "StudentNo"])
        sm_id = _pd_pick(sm_cols, ["ID", "StudentID", "StudentId"])
        sm_srno = _pd_pick(sm_cols, ["SRNo", "SrNo", "Sr_No", "ScholarNo", "StudentNo"])
        sm_name = _pd_pick(sm_cols, ["Student", "StudentName", "Name", "Student Name"])
        sm_father = _pd_pick(sm_cols, ["FatherName", "Father", "Father Name", "FName"])
        sm_dob = _pd_pick(sm_cols, ["DOB", "Dob", "DateOfBirth", "BirthDate"])
        sm_session = _pd_pick(sm_cols, ["SessionID", "SessionId", "Session"])
        sm_fee_type = _pd_pick(sm_cols, ["FeeType", "Fee Type", "Feetype"])
        sm_mobile = _pd_pick(sm_cols, ["MobileNo", "MobileNumber", "Mobile", "PhoneNo", "Phone", "ContactNo", "ContactNumber", "FatherMobile", "ParentMobile", "GuardianMobile"])
        sm_gender = _pd_pick(sm_cols, ["Sex", "Gender", "StudentGender", "GenderName"])
        if not all([sa_student, sa_session, sa_class, sa_section, sm_id, sm_name]):
            raise RuntimeError("StudentAcademic or StudentMaster is missing required student columns.")

        srno_source = (f"sm.{_pd_qi(sm_srno)}" if sm_srno else
                       (f"sa.{_pd_qi(sa_srno)}" if sa_srno else None))
        if not srno_source:
            raise RuntimeError("StudentMaster or StudentAcademic is missing an SRNo column.")

        select_exprs = [
            f"CONVERT(NVARCHAR(100), sm.{_pd_qi(sm_id)}) AS StudentID",
            f"CONVERT(NVARCHAR(100), {srno_source}) AS SRNo",
            f"CONVERT(NVARCHAR(255), sm.{_pd_qi(sm_name)}) AS StudentName",
            f"LTRIM(RTRIM(CONVERT(NVARCHAR(255), ISNULL(sa.{_pd_qi(sa_class)},'')))) AS ClassName",
            f"LTRIM(RTRIM(CONVERT(NVARCHAR(255), ISNULL(sa.{_pd_qi(sa_section)},'')))) AS SectionName",
            f"CONVERT(NVARCHAR(100), sm.{_pd_qi(sm_father)}) AS FatherName" if sm_father else "CAST('' AS NVARCHAR(100)) AS FatherName",
            f"CONVERT(NVARCHAR(100), sm.{_pd_qi(sm_dob)}) AS DOB" if sm_dob else "CAST('' AS NVARCHAR(100)) AS DOB",
            f"CONVERT(NVARCHAR(100), sa.{_pd_qi(sa_srno)}) AS AcademicSRNo" if sa_srno else f"CONVERT(NVARCHAR(100), {srno_source}) AS AcademicSRNo",
            f"CONVERT(NVARCHAR(100), sm.{_pd_qi(sm_session)}) AS StudentSession" if sm_session else "CAST('' AS NVARCHAR(100)) AS StudentSession",
            f"CONVERT(NVARCHAR(100), sm.{_pd_qi(sm_fee_type)}) AS FeeType" if sm_fee_type else "CAST('' AS NVARCHAR(100)) AS FeeType",
            f"CONVERT(NVARCHAR(100), sm.{_pd_qi(sm_mobile)}) AS MobileNo" if sm_mobile else "CAST('' AS NVARCHAR(100)) AS MobileNo",
            f"CONVERT(NVARCHAR(50), sm.{_pd_qi(sm_gender)}) AS Gender" if sm_gender else "CAST('' AS NVARCHAR(50)) AS Gender",
        ]
        status_sql = ""
        if sa_status:
            status_sql = (
                f" AND (sa.{_pd_qi(sa_status)} IS NULL OR "
                f"LOWER(LTRIM(RTRIM(CONVERT(VARCHAR(100), sa.{_pd_qi(sa_status)})))) "
                "IN ('active','1','true','yes'))"
            )
        cur.execute(
            f"SELECT {', '.join(select_exprs)} "
            f"FROM dbo.StudentAcademic sa INNER JOIN dbo.StudentMaster sm "
            f"ON sa.{_pd_qi(sa_student)} = sm.{_pd_qi(sm_id)} "
            f"WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(100), sa.{_pd_qi(sa_session)}))) = ? "
            f"{status_sql} AND NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(255), "
            f"ISNULL(sa.{_pd_qi(sa_class)},'')))), '') IS NOT NULL",
            str(session_id)
        )
        seen_students = set()
        for r in cur.fetchall():
            st = {
                "id": _pd_txt(r[0]), "srno": _pd_txt(r[1]), "name": _pd_txt(r[2]),
                "class_name": _pd_txt(r[3]), "section": _pd_txt(r[4]),
                "father": _pd_txt(r[5]), "dob": _pd_show_date(r[6]),
                "attendance_srno": _pd_txt(r[7]), "receipt_srno": _pd_txt(r[7]) or _pd_txt(r[1]),
                "student_session": _pd_txt(r[8]), "fee_type": _pd_txt(r[9]), "mobile": _pd_txt(r[10]),
                "gender": _pd_txt(r[11]),
            }
            dedupe_key = (st["id"], _pd_norm(st["class_name"]), _pd_norm(st["section"]))
            if not st["id"] or dedupe_key in seen_students:
                continue
            seen_students.add(dedupe_key)
            all_students.append(st)

        all_students.sort(key=lambda st: (
            _pd_master_pair_sort_key(st["class_name"], st["section"], master_order_maps),
            0 if st["srno"].isdigit() else 1,
            int(st["srno"]) if st["srno"].isdigit() else 999999999,
            _pd_norm(st["srno"]), _pd_norm(st["name"])
        ))
        pair_counts = {}
        for st in all_students:
            key = _pd_class_key(st["class_name"], st["section"])
            pair_counts.setdefault(key, {
                "class_name": st["class_name"], "section": st["section"], "count": 0,
                "class_teacher": "—"
            })
            pair_counts[key]["count"] += 1
        pairs = list(pair_counts.values())
        pairs.sort(key=lambda item: _pd_master_pair_sort_key(item["class_name"], item["section"], master_order_maps))
        schoolwide_count = len(all_students)
        gender_counts_available = bool(sm_gender)
        male_values = {"male", "m", "boy", "boys"}
        female_values = {"female", "f", "girl", "girls"}
        schoolwide_boys = sum(1 for st in all_students if _pd_norm(st.get("gender", "")) in male_values)
        schoolwide_girls = sum(1 for st in all_students if _pd_norm(st.get("gender", "")) in female_values)

        try:
            class_teacher_map, ct_warning = _pd_load_class_teachers(cur, session_id, session_name)
            if ct_warning:
                report_warnings["teacher"] = ct_warning
        except Exception as exc:
            report_warnings["teacher"] = "Class Teacher assignments could not be loaded: " + str(exc)
            class_teacher_map = {}
        for st in all_students:
            st["class_teacher"] = class_teacher_map.get(_pd_class_key(st["class_name"], st["section"]), "—")
        for item in pairs:
            item["class_teacher"] = class_teacher_map.get(_pd_class_key(item["class_name"], item["section"]), "—")

        valid_pair_map = {_pd_class_key(item["class_name"], item["section"]): item for item in pairs}
        exam_classes = list(dict.fromkeys(item["class_name"] for item in pairs))

        # A section appears only once in the selector, while retaining the classes
        # that contain it so the browser can narrow sections when a class is chosen.
        section_classes = {}
        for pair_item in pairs:
            section_name = _pd_txt(pair_item["section"])
            class_name = _pd_txt(pair_item["class_name"])
            if section_name:
                section_classes.setdefault(section_name, [])
                if class_name and class_name not in section_classes[section_name]:
                    section_classes[section_name].append(class_name)
        exam_section_options = [
            {"section": section_name, "classes": class_names}
            for section_name, class_names in section_classes.items()
        ]

        # Validate each filter independently. "All" is a valid selection for every field.
        if _pd_norm(selected_exam_class) != "all":
            canonical_class = next((
                class_name for class_name in exam_classes
                if _pd_norm(class_name) == _pd_norm(selected_exam_class)
            ), None)
            selected_exam_class = canonical_class or "All"
        else:
            selected_exam_class = "All"

        if _pd_norm(selected_exam_section) != "all":
            available_section_names = [
                item["section"] for item in pairs
                if _pd_norm(selected_exam_class) == "all"
                or _pd_norm(item["class_name"]) == _pd_norm(selected_exam_class)
            ]
            canonical_section = next((
                section_name for section_name in available_section_names
                if _pd_norm(section_name) == _pd_norm(selected_exam_section)
            ), None)
            selected_exam_section = canonical_section or "All"
        else:
            selected_exam_section = "All"

        selected_exam_pair_key = None
        if (_pd_norm(selected_exam_class) != "all"
                and _pd_norm(selected_exam_section) != "all"):
            candidate_exam_key = _pd_class_key(selected_exam_class, selected_exam_section)
            if candidate_exam_key in valid_pair_map:
                selected_exam_pair_key = candidate_exam_key
                selected_exam_class = valid_pair_map[candidate_exam_key]["class_name"]
                selected_exam_section = valid_pair_map[candidate_exam_key]["section"]
                exam_filter_ready = True
            else:
                selected_exam_section = "All"
        if selected_class and selected_section:
            selected_pair_key = _pd_class_key(selected_class, selected_section)
            selected_pair = valid_pair_map.get(selected_pair_key)
            if selected_pair:
                student_view_ready = True
                selected_teacher_name = selected_pair.get("class_teacher", "—")
                students = [st for st in all_students if _pd_class_key(st["class_name"], st["section"]) == selected_pair_key]
            else:
                selected_class = ""
                selected_section = ""
        # No default/all-class roster is exposed on the student directory screen.

        try:
            exam_report, exam_posting_report, warning = _pd_exam_summary(
                cur, session_id, session_name, pairs, all_students
            )
            if warning:
                report_warnings["exam"] = warning
        except Exception as exc:
            report_warnings["exam"] = "Result posting report could not be prepared: " + str(exc)
            print("PRINCIPAL EXAM SUMMARY ERROR:", repr(exc))

        # Use MasterTable's Exam Type order for the filter and report. Add any actual
        # exam labels found in assignments/results if they are missing from MasterTable.
        exam_type_options = list(master_items.get("exam type", []))
        exam_type_seen = {_pd_exam_identity(v) for v in exam_type_options}
        for row in exam_report + exam_posting_report:
            actual_exam = _pd_txt(row.get("exam"))
            actual_key = _pd_exam_identity(actual_exam)
            if actual_exam and actual_key not in exam_type_seen:
                exam_type_options.append(actual_exam)
                exam_type_seen.add(actual_key)
        # When a legacy database has no usable Exam Type MasterTable values, provide
        # the expected sequence first, followed by any other labels found in records.
        master_exam_order = master_order_maps.get("exam type", {})
        if not master_items.get("exam type"):
            preferred = ["M-1", "M-2", "M-3", "YEARLY"]
            merged = []
            identities = set()
            for value in preferred + exam_type_options:
                key = _pd_exam_identity(value)
                if key and key not in identities:
                    identities.add(key)
                    merged.append(value)
            exam_type_options = merged
        else:
            # MasterTable sequence remains intact; only newly discovered items are
            # ordered after the known master entries by the fallback exam sequence.
            prefix = list(master_items.get("exam type", []))
            prefix_ids = {_pd_exam_identity(v) for v in prefix}
            extra = [v for v in exam_type_options if _pd_exam_identity(v) not in prefix_ids]
            extra.sort(key=lambda v: _pd_exam_sort_key(v, master_exam_order))
            exam_type_options = prefix + extra

        if _pd_norm(selected_exam_type) != "all":
            selected_exam_key = _pd_exam_identity(selected_exam_type)
            canonical_option = next((v for v in exam_type_options if _pd_exam_identity(v) == selected_exam_key), None)
            selected_exam_type = canonical_option or "All"

        pair_order_for_report = {
            _pd_class_key(item["class_name"], item["section"]): index
            for index, item in enumerate(pairs)
        }
        exam_report.sort(key=lambda row: (
            pair_order_for_report.get(_pd_class_key(row["class_name"], row["section"]), 999999),
            _pd_exam_sort_key(row["exam"], master_exam_order),
        ))
        exam_posting_report.sort(key=lambda row: (
            pair_order_for_report.get(_pd_class_key(row["class_name"], row["section"]), 999999),
            _pd_exam_sort_key(row["exam"], master_exam_order),
            _pd_norm(row["subject"]), _pd_norm(row["paper"]),
        ))
        # Do not expose a school-wide result list when the page is first opened.
        # The user must submit the filter form. On submit, apply all three filters.
        if view == "exam":
            if exam_filter_applied:
                if _pd_norm(selected_exam_class) != "all":
                    exam_class_key = _pd_norm(selected_exam_class)
                    exam_report = [r for r in exam_report if _pd_norm(r["class_name"]) == exam_class_key]
                    exam_posting_report = [r for r in exam_posting_report if _pd_norm(r["class_name"]) == exam_class_key]
                if _pd_norm(selected_exam_section) != "all":
                    exam_section_key = _pd_norm(selected_exam_section)
                    exam_report = [r for r in exam_report if _pd_norm(r["section"]) == exam_section_key]
                    exam_posting_report = [r for r in exam_posting_report if _pd_norm(r["section"]) == exam_section_key]
                if _pd_norm(selected_exam_type) != "all":
                    selected_exam_key = _pd_exam_identity(selected_exam_type)
                    exam_report = [r for r in exam_report if _pd_exam_identity(r["exam"]) == selected_exam_key]
                    exam_posting_report = [r for r in exam_posting_report if _pd_exam_identity(r["exam"]) == selected_exam_key]
            else:
                exam_report = []
                exam_posting_report = []

        try:
            attendance_report, warning, attendance_date = _pd_attendance_summary(cur, session_id, pairs, all_students)
            if warning:
                report_warnings["attendance"] = warning
        except Exception as exc:
            report_warnings["attendance"] = "Attendance report could not be prepared: " + str(exc)
            print("PRINCIPAL ATTENDANCE SUMMARY ERROR:", repr(exc))
        for row in attendance_report:
            row["class_teacher"] = class_teacher_map.get(_pd_class_key(row["class_name"], row["section"]), "—")

        if view == "attendance-details":
            if not selected_class or not selected_section or not selected_pair_key:
                report_warnings["attendance_detail"] = "Select a valid class and section from the Student Attendance Report."
            else:
                try:
                    attendance_students_for_pair = [
                        st for st in all_students
                        if _pd_class_key(st["class_name"], st["section"]) == selected_pair_key
                    ]
                    attendance_detail_students, attendance_detail_date, warning = _pd_attendance_detail(
                        cur, session_id, attendance_students_for_pair, attendance_date
                    )
                    if warning:
                        report_warnings["attendance_detail"] = warning
                    selected_teacher_name = class_teacher_map.get(selected_pair_key, "—")
                    attendance_detail_totals = {
                        "total": len(attendance_detail_students),
                        "present": sum(1 for st in attendance_detail_students if st["status"] == "Present"),
                        "absent": sum(1 for st in attendance_detail_students if st["status"] == "Absent"),
                        "medical": sum(1 for st in attendance_detail_students if st["status"] == "Medical"),
                        "not_saved": sum(1 for st in attendance_detail_students if st["status"] == "Not Saved"),
                    }
                except Exception as exc:
                    report_warnings["attendance_detail"] = "Class attendance details could not be loaded: " + str(exc)
                    print("PRINCIPAL ATTENDANCE DETAIL ERROR:", repr(exc))

        try:
            fee_report, warning = _pd_fee_summary(cur, session_id, pairs, all_students)
            if warning:
                report_warnings["fee"] = warning
        except Exception as exc:
            report_warnings["fee"] = "Fee report could not be prepared: " + str(exc)
            print("PRINCIPAL FEE SUMMARY ERROR:", repr(exc))

        if view in ("fee-details", "fee-student-details"):
            if not selected_class or not selected_section or not selected_pair_key:
                report_warnings["fee_detail"] = "Select a valid class and section from the Fee Report."
            else:
                try:
                    selected_students = [st for st in all_students if _pd_class_key(st["class_name"], st["section"]) == selected_pair_key]
                    detailed = _pd_fee_calculation(cur, session_id, selected_students)
                    # Keep due and no-setup rows visible so missing fee configuration is not hidden.
                    fee_detail_students = detailed
                    fee_detail_totals = {
                        "students": len(detailed),
                        "due": sum(1 for st in detailed if st["status"] == "Due"),
                        "balance_display": _pd_money(sum((st["balance"] for st in detailed), _pd_amount(0))),
                    }
                    if view == "fee-student-details":
                        fee_student_detail = next((st for st in detailed if st["id"] == selected_student_id), None)
                        if fee_student_detail is None:
                            report_warnings["fee_detail"] = "The selected student could not be found in this class and section."
                    selected_teacher_name = class_teacher_map.get(selected_pair_key, "—")
                except Exception as exc:
                    report_warnings["fee_detail"] = "Student fee details could not be loaded: " + str(exc)
                    print("PRINCIPAL FEE DETAIL ERROR:", repr(exc))

    except Exception as exc:
        error = "Dashboard data could not be loaded: " + str(exc)
        print("PRINCIPAL DASHBOARD DATA ERROR:", repr(exc))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    exam_totals = {
        "total": sum(int(row["total_subjects"]) for row in exam_report),
        "completed": sum(int(row["completed"]) for row in exam_report),
        "pending": sum(int(row["pending"]) for row in exam_report),
    }
    posting_totals = {
        "total": len(exam_posting_report),
        "submitted": sum(1 for row in exam_posting_report if row["status"] == "Submitted"),
        "pending": sum(1 for row in exam_posting_report if row["status"] != "Submitted"),
    }
    attendance_totals = {
        "total": sum(int(row["total_students"]) for row in attendance_report),
        "submitted": sum(int(row["submitted"]) for row in attendance_report),
        "pending": sum(int(row["pending"]) for row in attendance_report),
    }
    # Prepare the class dropdown once. The same ordered list is used by both
    # the student directory and the result-posting filters.
    classes = list(dict.fromkeys(item["class_name"] for item in pairs))

    fee_total_amount = sum((row["total_fee"] for row in fee_report), _pd_amount(0))
    fee_collected_amount = sum((row["collected"] for row in fee_report), _pd_amount(0))
    fee_totals = {
        "total": sum(int(row["total_students"]) for row in fee_report),
        "paid": sum(int(row["paid"]) for row in fee_report),
        "due": sum(int(row["due"]) for row in fee_report),
        "no_fee_setup": sum(int(row["no_fee_setup"]) for row in fee_report),
        "total_fee_display": _pd_money(fee_total_amount),
        "collected_display": _pd_money(fee_collected_amount),
        "balance_display": _pd_money(max(fee_total_amount - fee_collected_amount, _pd_amount(0))),
    }
    return render_template(
        "principal_dashboard.html", school_name=get_school_name(),
        principal_name=session.get("user_name", "Principal"),
        designation=session.get("designation", "Principal"),
        session_name=session_name, view=view, pairs=pairs,
        classes=classes, students=students,
        selected_class=selected_class, selected_section=selected_section,
        student_view_ready=student_view_ready, selected_teacher_name=selected_teacher_name,
        schoolwide_count=schoolwide_count, schoolwide_boys=schoolwide_boys,
        schoolwide_girls=schoolwide_girls, gender_counts_available=gender_counts_available,
        exam_report=exam_report,
        exam_posting_report=exam_posting_report, exam_totals=exam_totals,
        posting_totals=posting_totals, attendance_report=attendance_report,
        attendance_date=attendance_date, attendance_totals=attendance_totals,
        attendance_detail_students=attendance_detail_students,
        attendance_detail_date=attendance_detail_date,
        attendance_detail_totals=attendance_detail_totals,
        fee_report=fee_report, fee_totals=fee_totals, fee_detail_students=fee_detail_students,
        fee_detail_totals=fee_detail_totals, fee_student_detail=fee_student_detail,
        selected_student_id=selected_student_id,
        exam_classes=classes, selected_exam_class=selected_exam_class,
        selected_exam_section=selected_exam_section, selected_exam_type=selected_exam_type,
        exam_type_options=exam_type_options, exam_section_options=exam_section_options,
        exam_filter_ready=exam_filter_ready, exam_filter_applied=exam_filter_applied,
        class_teacher_name_for_exam=(class_teacher_map.get(_pd_class_key(selected_exam_class, selected_exam_section), "—") if exam_filter_ready else "—"),
        exam_students_count=(sum(1 for st in all_students
            if (_pd_norm(selected_exam_class) == "all" or _pd_norm(st["class_name"]) == _pd_norm(selected_exam_class))
            and (_pd_norm(selected_exam_section) == "all" or _pd_norm(st["section"]) == _pd_norm(selected_exam_section))) if exam_filter_applied else 0),
        report_warnings=report_warnings, error=error
    )

