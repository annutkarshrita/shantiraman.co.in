from app import app, get_connection, get_class_teacher_assignment_user, normalize_role
from flask import session



def get_active_session(preferred_session=""):
    """Return (SessionTable ID, SessionName) for the current active session.

    SessionTable में column names अलग versions में अलग हो सकते हैं,
    इसलिए actual schema को पहले पढ़ा जाता है। यदि ClassTeacherAssign का
    session दिया गया हो (जैसे 2026-27), तो उसी session को पहले match
    किया जाता है और यह भी सुनिश्चित किया जाता है कि वह Active हो।
    """
    conn = cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()

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
            raise RuntimeError("SessionTable में ID और Session/SessionName column नहीं मिला।")

        # 1) ClassTeacherAssign से मिले current session को पहले match करें.
        preferred = str(preferred_session or "").strip()
        if preferred:
            if status_col:
                if status_col.lower() == "isactive":
                    sql = f"""
                        SELECT TOP 1 [{id_col}], [{session_col}]
                        FROM dbo.SessionTable
                        WHERE LTRIM(RTRIM(CONVERT(VARCHAR(100), [{session_col}]))) = ?
                          AND [{status_col}] IN (1, '1', 'True', 'true', 'Yes', 'yes')
                        ORDER BY [{id_col}] DESC
                    """
                else:
                    sql = f"""
                        SELECT TOP 1 [{id_col}], [{session_col}]
                        FROM dbo.SessionTable
                        WHERE LTRIM(RTRIM(CONVERT(VARCHAR(100), [{session_col}]))) = ?
                          AND LOWER(LTRIM(RTRIM(CONVERT(VARCHAR(100), [{status_col}]))))
                              IN ('active','1','true','yes')
                        ORDER BY [{id_col}] DESC
                    """
                cur.execute(sql, preferred)
            else:
                cur.execute(f"""
                    SELECT TOP 1 [{id_col}], [{session_col}]
                    FROM dbo.SessionTable
                    WHERE LTRIM(RTRIM(CONVERT(VARCHAR(100), [{session_col}]))) = ?
                    ORDER BY [{id_col}] DESC
                """, preferred)

            row = cur.fetchone()
            if row and str(row[1] or "").strip():
                return str(row[0]).strip(), str(row[1]).strip()

        # 2) Fallback: सबसे नया Active Session.
        if status_col:
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
            if row and str(row[1] or "").strip():
                return str(row[0]).strip(), str(row[1]).strip()

        return "", ""
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def require_class_teacher():
    """Return the current active Class Teacher assignment or None."""
    if "employee_id" not in session:
        return None
    if normalize_role(session.get("role", "")) != "classteacher":
        return None
    ct = get_class_teacher_assignment_user(session.get("user_email", ""))
    if not ct:
        return None
    return ct


def _table_columns(cur, table_name):
    cur.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=?
        ORDER BY ORDINAL_POSITION
    """, table_name)
    return [str(r[0]).strip() for r in cur.fetchall()]


def _pick_col(columns, names):
    lookup = {str(c).strip().lower(): str(c).strip() for c in columns}
    for name in names:
        if name.lower() in lookup:
            return lookup[name.lower()]
    return None


def _clean_value(v):
    return str(v if v is not None else "").strip()


def _student_columns(cur):
    cols = _table_columns(cur, "StudentMaster")
    return {
        "id": _pick_col(cols, ["ID", "StudentID"]),
        "srno": _pick_col(cols, ["SrNo", "SRNo", "Sr_No", "StudentNo"]),
        "name": _pick_col(cols, ["Student", "StudentName", "Name"]),
        "father": _pick_col(cols, ["Father", "FatherName", "Father_Name", "FathersName"]),
        "mother": _pick_col(cols, ["Mother", "MotherName", "Mother_Name", "MothersName"]),
        "dob": _pick_col(cols, ["DOB", "DateOfBirth", "BirthDate", "Birth_Date"]),
    }


def _active_session_for_class_teacher(ct):
    sid, sname = get_active_session(ct.get("session", ""))
    # ClassTeacherAssign session is used only as a fallback/display value.
    if not sname:
        sname = _clean_value(ct.get("session"))
    return sid, sname


def _class_students(cur, ct):
    """Active-session students of only the assigned Class + Section."""
    sm = _student_columns(cur)
    required = [sm["id"], sm["srno"], sm["name"]]
    if not all(required):
        raise RuntimeError("StudentMaster में ID, SrNo और Student/StudentName columns आवश्यक हैं।")

    sa_cols = _table_columns(cur, "StudentAcademic")
    sa_student = _pick_col(sa_cols, ["StudentID", "StudentId", "StdID"])
    sa_session = _pick_col(sa_cols, ["SessionID", "SessionId"])
    sa_class = _pick_col(sa_cols, ["Class", "ClassName"])
    sa_section = _pick_col(sa_cols, ["Section", "SectionName"])
    sa_status = _pick_col(sa_cols, ["SessStatus", "Status", "IsActive"])

    if not all([sa_student, sa_session, sa_class, sa_section]):
        raise RuntimeError("StudentAcademic में StudentID, SessionID, Class और Section columns आवश्यक हैं।")

    sid, sname = _active_session_for_class_teacher(ct)
    if not sid:
        raise RuntimeError("Active Session नहीं मिली।")

    select_fields = [
        f"sm.[{sm['id']}] AS StudentID",
        f"sm.[{sm['srno']}] AS SrNo",
        f"sm.[{sm['name']}] AS StudentName",
    ]
    if sm["father"]:
        select_fields.append(f"sm.[{sm['father']}] AS FatherName")
    else:
        select_fields.append("CAST('' AS NVARCHAR(255)) AS FatherName")
    if sm["mother"]:
        select_fields.append(f"sm.[{sm['mother']}] AS MotherName")
    else:
        select_fields.append("CAST('' AS NVARCHAR(255)) AS MotherName")
    if sm["dob"]:
        select_fields.append(f"sm.[{sm['dob']}] AS DOB")
    else:
        select_fields.append("CAST('' AS NVARCHAR(50)) AS DOB")

    sql = f"""
        SELECT {", ".join(select_fields)}
        FROM dbo.StudentMaster sm
        INNER JOIN dbo.StudentAcademic sa
            ON sa.[{sa_student}] = sm.[{sm['id']}]
        WHERE sa.[{sa_session}] = ?
          AND LTRIM(RTRIM(CONVERT(VARCHAR(255), ISNULL(sa.[{sa_class}],'')))) = ?
          AND LTRIM(RTRIM(CONVERT(VARCHAR(255), ISNULL(sa.[{sa_section}],'')))) = ?
    """
    params = [sid, _clean_value(ct["class_name"]), _clean_value(ct["section"])]

    if sa_status:
        sql += f"""
          AND (
              sa.[{sa_status}] IS NULL OR
              LOWER(LTRIM(RTRIM(CONVERT(VARCHAR(100), sa.[{sa_status}]))))
              IN ('active','1','true','yes')
          )
        """

    sql += f"""
        ORDER BY TRY_CONVERT(INT, sm.[{sm['srno']}]), sm.[{sm['srno']}]
    """
    cur.execute(sql, params)

    rows = []
    for r in cur.fetchall():
        rows.append({
            "id": _clean_value(r[0]),
            "srno": _clean_value(r[1]),
            "name": _clean_value(r[2]),
            "father": _clean_value(r[3]),
            "mother": _clean_value(r[4]),
            "dob": _clean_value(r[5]),
        })
    return rows, sid, sname


