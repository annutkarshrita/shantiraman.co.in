from app import app, get_school_name, normalize_role, get_class_teacher_assignment_user
from flask import render_template, redirect, url_for, session
from class_teacher_common import require_class_teacher

@app.route("/my-profile")
def my_profile():
    if "employee_id" not in session:
        return redirect(url_for("home"))

    role = normalize_role(session.get("role", ""))
    ct = get_class_teacher_assignment_user(session.get("user_email", "")) if role == "classteacher" else None
    return render_template(
        "my_profile.html",
        teacher_name=session.get("user_name"),
        designation=session.get("designation"),
        role=session.get("role"),
        email=session.get("user_email"),
        class_teacher=ct,
        school_name=get_school_name()
    )


# ============================================================
# CLASS TEACHER : FEE STATUS
# ============================================================

FEE_MONTHS = [
    "Apr", "May", "Jun", "Jul", "Aug", "Sep",
    "Oct", "Nov", "Dec", "Jan", "Feb", "Mar",
]


