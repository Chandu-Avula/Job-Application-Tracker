
import os
from datetime import datetime, timezone
from functools import wraps

from bson import ObjectId
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, abort
)
from pymongo import MongoClient, DESCENDING
from pymongo.errors import DuplicateKeyError
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY")

if not app.config["SECRET_KEY"]:
    raise RuntimeError("Set FLASK_SECRET_KEY in your .env file")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017/"),
    serverSelectionTimeoutMS=3000
)
db = client["job_tracker"]
users = db["users"]
applications = db["applications"]

STATUSES = [
    "Applied", "Assessment", "Interview",
    "Offer", "Rejected", "Withdrawn"
]
JOB_TYPES = ["Full-time", "Part-time", "Internship", "Contract"]


@app.before_request
def initialize_indexes():
    # Runs once per process in this simple local-development version.
    if not getattr(app, "_indexes_created", False):
        users.create_index("email", unique=True)
        applications.create_index([("user_id", 1), ("applied_date", -1)])
        applications.create_index([("user_id", 1), ("status", 1)])
        app._indexes_created = True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def current_user_id():
    try:
        return ObjectId(session["user_id"])
    except (KeyError, ValueError):
        abort(401)


def owned_application(application_id):
    if not ObjectId.is_valid(application_id):
        abort(404)

    record = applications.find_one({
        "_id": ObjectId(application_id),
        "user_id": current_user_id()
    })

    if record is None:
        abort(404)

    return record


def parse_application_form():
    company = request.form.get("company", "").strip()
    job_title = request.form.get("job_title", "").strip()
    applied_date = request.form.get("applied_date", "").strip()
    status = request.form.get("status", "Applied").strip()
    job_type = request.form.get("job_type", "Full-time").strip()

    if not company or not job_title or not applied_date:
        raise ValueError("Company, job title, and application date are required.")

    try:
        datetime.strptime(applied_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Enter a valid application date.")

    if status not in STATUSES:
        raise ValueError("Invalid application status.")

    if job_type not in JOB_TYPES:
        raise ValueError("Invalid job type.")

    interview_date = request.form.get("interview_date", "").strip()
    if interview_date:
        try:
            datetime.strptime(interview_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Enter a valid interview date.")

    return {
        "company": company,
        "job_title": job_title,
        "location": request.form.get("location", "").strip(),
        "job_type": job_type,
        "job_url": request.form.get("job_url", "").strip(),
        "salary_range": request.form.get("salary_range", "").strip(),
        "applied_date": applied_date,
        "status": status,
        "interview_date": interview_date or None,
        "recruiter_name": request.form.get("recruiter_name", "").strip(),
        "notes": request.form.get("notes", "").strip(),
        "updated_at": datetime.now(timezone.utc)
    }


@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/health")
def health():
    try:
        client.admin.command("ping")
        return jsonify({"status": "ok", "database": "connected"})
    except Exception:
        app.logger.exception("MongoDB health check failed")
        return jsonify({"status": "error", "database": "unavailable"}), 503


# ---------------- AUTHENTICATION ----------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or len(password) < 8:
            flash("Enter your name, email, and a password of at least 8 characters.", "error")
            return render_template("register.html")

        try:
            users.insert_one({
                "name": name,
                "email": email,
                "password_hash": generate_password_hash(password),
                "created_at": datetime.now(timezone.utc)
            })
            flash("Registration successful. Please log in.", "success")
            return redirect(url_for("login"))
        except DuplicateKeyError:
            flash("An account with that email already exists.", "error")
        except Exception:
            app.logger.exception("Registration failed")
            flash("Could not create the account. Please try again.", "error")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = users.find_one({"email": email})

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = str(user["_id"])
            session["user_name"] = user["name"]
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- DASHBOARD ----------------

@app.route("/dashboard")
@login_required
def dashboard():
    uid = current_user_id()

    counts = {
        status: applications.count_documents({
            "user_id": uid,
            "status": status
        })
        for status in STATUSES
    }

    total = applications.count_documents({"user_id": uid})
    recent = list(
        applications.find({"user_id": uid})
        .sort("applied_date", DESCENDING)
        .limit(5)
    )

    return render_template(
        "dashboard.html",
        total=total,
        counts=counts,
        recent=recent
    )


# ---------------- HTML APPLICATION CRUD ----------------

@app.route("/applications")
@login_required
def application_list():
    uid = current_user_id()
    query = {"user_id": uid}

    search = request.args.get("search", "").strip()
    status = request.args.get("status", "").strip()

    if search:
        # Literal substring search; escape regex special characters.
        import re
        pattern = {"$regex": re.escape(search), "$options": "i"}
        query["$or"] = [{"company": pattern}, {"job_title": pattern}]

    if status in STATUSES:
        query["status"] = status

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    per_page = 10
    total = applications.count_documents(query)
    records = list(
        applications.find(query)
        .sort("applied_date", DESCENDING)
        .skip((page - 1) * per_page)
        .limit(per_page)
    )

    return render_template(
        "applications.html",
        records=records,
        statuses=STATUSES,
        search=search,
        selected_status=status,
        page=page,
        pages=max(1, (total + per_page - 1) // per_page)
    )


@app.route("/applications/add", methods=["GET", "POST"])
@login_required
def add_application():
    if request.method == "POST":
        try:
            record = parse_application_form()
            record["user_id"] = current_user_id()
            record["created_at"] = datetime.now(timezone.utc)
            applications.insert_one(record)
            flash("Application added successfully.", "success")
            return redirect(url_for("application_list"))
        except ValueError as exc:
            flash(str(exc), "error")

    return render_template(
        "application_form.html",
        record={},
        statuses=STATUSES,
        job_types=JOB_TYPES,
        heading="Add Application"
    )


@app.route("/applications/<application_id>")
@login_required
def application_details(application_id):
    record = owned_application(application_id)
    return render_template("application_details.html", record=record)


@app.route("/applications/<application_id>/edit", methods=["GET", "POST"])
@login_required
def edit_application(application_id):
    record = owned_application(application_id)

    if request.method == "POST":
        try:
            updated = parse_application_form()
            applications.update_one(
                {"_id": record["_id"], "user_id": current_user_id()},
                {"$set": updated}
            )
            flash("Application updated successfully.", "success")
            return redirect(url_for("application_details", application_id=application_id))
        except ValueError as exc:
            flash(str(exc), "error")

    return render_template(
        "application_form.html",
        record=record,
        statuses=STATUSES,
        job_types=JOB_TYPES,
        heading="Edit Application"
    )


@app.route("/applications/<application_id>/delete", methods=["POST"])
@login_required
def delete_application(application_id):
    record = owned_application(application_id)
    applications.delete_one({
        "_id": record["_id"],
        "user_id": current_user_id()
    })
    flash("Application deleted.", "success")
    return redirect(url_for("application_list"))


# ---------------- REST API ----------------

@app.route("/api/applications", methods=["GET", "POST"])
@login_required
def api_applications():
    uid = current_user_id()

    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "A JSON object is required."}), 400

        company = data.get("company", "").strip() if isinstance(data.get("company", ""), str) else ""
        job_title = data.get("job_title", "").strip() if isinstance(data.get("job_title", ""), str) else ""
        applied_date = data.get("applied_date", "")
        status = data.get("status", "Applied")

        if not company or not job_title:
            return jsonify({"error": "Company and job title are required."}), 400
        if status not in STATUSES:
            return jsonify({"error": "Invalid status."}), 400

        try:
            datetime.strptime(applied_date, "%Y-%m-%d")
        except (TypeError, ValueError):
            return jsonify({"error": "applied_date must use YYYY-MM-DD."}), 400

        record = {
            "user_id": uid,
            "company": company,
            "job_title": job_title,
            "location": str(data.get("location", "")),
            "job_type": data.get("job_type", "Full-time"),
            "job_url": str(data.get("job_url", "")),
            "salary_range": str(data.get("salary_range", "")),
            "applied_date": applied_date,
            "status": status,
            "interview_date": data.get("interview_date") or None,
            "recruiter_name": str(data.get("recruiter_name", "")),
            "notes": str(data.get("notes", "")),
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc)
        }

        result = applications.insert_one(record)
        record["_id"] = str(result.inserted_id)
        record.pop("user_id", None)
        record.pop("created_at", None)
        record.pop("updated_at", None)
        return jsonify(record), 201

    query = {"user_id": uid}
    status = request.args.get("status", "")
    search = request.args.get("search", "").strip()

    if status in STATUSES:
        query["status"] = status

    if search:
        import re
        pattern = {"$regex": re.escape(search), "$options": "i"}
        query["$or"] = [{"company": pattern}, {"job_title": pattern}]

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    per_page = 10
    total = applications.count_documents(query)
    records = list(
        applications.find(query)
        .sort("applied_date", DESCENDING)
        .skip((page - 1) * per_page)
        .limit(per_page)
    )

    for record in records:
        record["_id"] = str(record["_id"])
        record.pop("user_id", None)

    return jsonify({
        "items": records,
        "page": page,
        "per_page": per_page,
        "total": total
    }), 200


@app.route("/api/applications/<application_id>", methods=["GET", "PATCH", "DELETE"])
@login_required
def api_application_detail(application_id):
    record = owned_application(application_id)
    uid = current_user_id()

    if request.method == "GET":
        record["_id"] = str(record["_id"])
        record.pop("user_id", None)
        return jsonify(record), 200

    if request.method == "DELETE":
        applications.delete_one({
            "_id": record["_id"],
            "user_id": uid
        })
        return "", 204

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required."}), 400

    allowed = {
        "company", "job_title", "location", "job_type",
        "job_url", "salary_range", "applied_date", "status",
        "interview_date", "recruiter_name", "notes"
    }
    updates = {key: value for key, value in data.items() if key in allowed}

    if not updates:
        return jsonify({"error": "No editable fields supplied."}), 400

    if "status" in updates and updates["status"] not in STATUSES:
        return jsonify({"error": "Invalid status."}), 400

    for field in ("company", "job_title"):
        if field in updates and (
            not isinstance(updates[field], str) or not updates[field].strip()
        ):
            return jsonify({"error": f"{field} cannot be empty."}), 400

    if "applied_date" in updates:
        try:
            datetime.strptime(updates["applied_date"], "%Y-%m-%d")
        except (TypeError, ValueError):
            return jsonify({"error": "applied_date must use YYYY-MM-DD."}), 400

    updates["updated_at"] = datetime.now(timezone.utc)
    applications.update_one(
        {"_id": record["_id"], "user_id": uid},
        {"$set": updates}
    )
    return jsonify({"message": "Application updated successfully."}), 200


@app.route("/api/dashboard/stats")
@login_required
def api_dashboard_stats():
    uid = current_user_id()
    counts = {
        status: applications.count_documents({
            "user_id": uid, "status": status
        })
        for status in STATUSES
    }
    return jsonify({
        "total": applications.count_documents({"user_id": uid}),
        "by_status": counts
    }), 200


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Resource not found."}), 404
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=True)