
import os
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "safepath.db"
UPLOAD_FOLDER = BASE_DIR / "uploads"

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ALLOWED_STATUSES = {"Reported", "Under review", "Resolved"}
ALLOWED_SEVERITIES = {"Low", "Medium", "High"}
ALLOWED_CATEGORIES = {
    "Broken footpath",
    "Missing curb ramp",
    "Blocked walkway",
    "Unsafe crossing",
    "Poor lighting",
    "Missing tactile paving",
    "Other",
}

app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)


# ---------------- DATABASE ----------------

def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT UNIQUE NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                area TEXT NOT NULL,
                severity TEXT NOT NULL,
                report_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Reported',
                latitude REAL,
                longitude REAL,
                image_filename TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)


def serialize_report(row):
    report = dict(row)
    filename = report.pop("image_filename", None)

    report["id"] = report["public_id"]
    report.pop("public_id", None)
    report["date"] = report.pop("report_date")
    report["photo"] = (
        f"/uploads/{filename}" if filename else ""
    )
    report["demo"] = False
    return report


def get_report(public_id):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM reports WHERE public_id = ?",
            (public_id,),
        ).fetchone()

    return row


# ---------------- VALIDATION ----------------

def validate_report(data):
    category = str(data.get("category", "")).strip()
    area = str(data.get("area", "")).strip()
    description = str(data.get("description", "")).strip()
    severity = str(data.get("severity", "Medium")).strip()
    report_date = str(data.get("date", date.today().isoformat())).strip()

    if category not in ALLOWED_CATEGORIES:
        return None, "Select a valid hazard category."

    if severity not in ALLOWED_SEVERITIES:
        return None, "Select a valid severity."

    if not area or len(area) > 100:
        return None, "Area must contain 1–100 characters."

    if not description or len(description) > 500:
        return None, "Description must contain 1–500 characters."

    try:
        date.fromisoformat(report_date)
    except ValueError:
        return None, "Enter a valid date in YYYY-MM-DD format."

    latitude = data.get("lat")
    longitude = data.get("lng")

    if latitude in ("", None) and longitude in ("", None):
        latitude = longitude = None
    else:
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            return None, "Latitude and longitude must both be valid numbers."

        if not (-90 <= latitude <= 90):
            return None, "Latitude must be between -90 and 90."

        if not (-180 <= longitude <= 180):
            return None, "Longitude must be between -180 and 180."

    return {
        "category": category,
        "area": area,
        "description": description,
        "severity": severity,
        "date": report_date,
        "lat": latitude,
        "lng": longitude,
    }, None


def save_image(file):
    if not file or not file.filename:
        return None, None

    original_name = secure_filename(file.filename)

    if "." not in original_name:
        return None, "Image must have a valid file extension."

    extension = original_name.rsplit(".", 1)[1].lower()

    if extension not in ALLOWED_EXTENSIONS:
        return None, "Only JPG, JPEG, PNG and WebP images are allowed."

    filename = f"{uuid.uuid4().hex}.{extension}"
    destination = UPLOAD_FOLDER / filename

    file.save(destination)

    # Basic signature checks help reject incorrectly labelled files.
    # For production, also decode/re-encode images and scan uploads.
    try:
        with destination.open("rb") as image_file:
            header = image_file.read(12)

        valid = (
            (extension in {"jpg", "jpeg"}
             and header.startswith(b"\xff\xd8\xff"))
            or (extension == "png"
                and header.startswith(b"\x89PNG\r\n\x1a\n"))
            or (extension == "webp"
                and header.startswith(b"RIFF")
                and header[8:12] == b"WEBP")
        )

        if not valid:
            destination.unlink(missing_ok=True)
            return None, "The uploaded file is not a valid supported image."

    except OSError:
        destination.unlink(missing_ok=True)
        return None, "Could not validate the uploaded image."

    return filename, None


def delete_image(filename):
    if not filename:
        return

    path = UPLOAD_FOLDER / filename

    # Only delete a file directly inside the configured upload directory.
    if path.parent == UPLOAD_FOLDER:
        path.unlink(missing_ok=True)


# ---------------- FRONTEND ----------------

@app.get("/")
def home():
    return render_template("index.html")


@app.get("/uploads/<path:filename>")
def uploaded_image(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ---------------- REPORT API ----------------

@app.get("/api/reports")
def list_reports():
    with get_db() as db:
        rows = db.execute("""
            SELECT * FROM reports
            ORDER BY created_at DESC, id DESC
        """).fetchall()

    return jsonify([serialize_report(row) for row in rows])


@app.post("/api/reports")
def create_report():
    data = request.form.to_dict()

    if data.get("consent") not in {"true", "on", "1"}:
        return jsonify(error="Please confirm the demo-data notice."), 400

    clean, error = validate_report(data)

    if error:
        return jsonify(error=error), 400

    image_filename, image_error = save_image(request.files.get("photo"))

    if image_error:
        return jsonify(error=image_error), 400

    public_id = "SP-" + uuid.uuid4().hex[:12].upper()

    try:
        with get_db() as db:
            db.execute("""
                INSERT INTO reports (
                    public_id, category, description, area,
                    severity, report_date, status,
                    latitude, longitude, image_filename
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                public_id,
                clean["category"],
                clean["description"],
                clean["area"],
                clean["severity"],
                clean["date"],
                "Reported",
                clean["lat"],
                clean["lng"],
                image_filename,
            ))
    except sqlite3.Error:
        delete_image(image_filename)
        app.logger.exception("Could not create report")
        return jsonify(error="Could not save the report."), 500

    return jsonify(serialize_report(get_report(public_id))), 201


@app.patch("/api/reports/<public_id>/status")
def update_status(public_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")

    if status not in ALLOWED_STATUSES:
        return jsonify(error="Invalid report status."), 400

    with get_db() as db:
        result = db.execute(
            "UPDATE reports SET status = ? WHERE public_id = ?",
            (status, public_id),
        )

    if result.rowcount == 0:
        return jsonify(error="Report not found."), 404

    return jsonify(serialize_report(get_report(public_id)))


@app.put("/api/reports/<public_id>")
def edit_report(public_id):
    existing = get_report(public_id)

    if not existing:
        return jsonify(error="Report not found."), 404

    data = request.get_json(silent=True) or {}
    clean, error = validate_report(data)

    if error:
        return jsonify(error=error), 400

    with get_db() as db:
        db.execute("""
            UPDATE reports SET
                category = ?,
                description = ?,
                area = ?,
                severity = ?,
                report_date = ?,
                latitude = ?,
                longitude = ?
            WHERE public_id = ?
        """, (
            clean["category"],
            clean["description"],
            clean["area"],
            clean["severity"],
            clean["date"],
            clean["lat"],
            clean["lng"],
            public_id,
        ))

    return jsonify(serialize_report(get_report(public_id)))


@app.delete("/api/reports/<public_id>")
def delete_report(public_id):
    with get_db() as db:
        row = db.execute(
            "SELECT image_filename FROM reports WHERE public_id = ?",
            (public_id,),
        ).fetchone()

        if not row:
            return jsonify(error="Report not found."), 404

        db.execute(
            "DELETE FROM reports WHERE public_id = ?",
            (public_id,),
        )

    delete_image(row["image_filename"])

    return jsonify(message="Report deleted successfully.")


# ---------------- DASHBOARD STATISTICS ----------------

@app.get("/api/stats")
def get_stats():
    with get_db() as db:
        total = db.execute(
            "SELECT COUNT(*) FROM reports"
        ).fetchone()[0]

        high = db.execute("""
            SELECT COUNT(*) FROM reports
            WHERE severity = 'High' AND status != 'Resolved'
        """).fetchone()[0]

        under_review = db.execute("""
            SELECT COUNT(*) FROM reports
            WHERE status = 'Under review'
        """).fetchone()[0]

        resolved = db.execute("""
            SELECT COUNT(*) FROM reports
            WHERE status = 'Resolved'
        """).fetchone()[0]

        categories = db.execute("""
            SELECT category, COUNT(*) AS count
            FROM reports
            GROUP BY category
            ORDER BY count DESC
        """).fetchall()

    return jsonify({
        "total": total,
        "high_priority": high,
        "under_review": under_review,
        "resolved": resolved,
        "categories": [
            {"category": row["category"], "count": row["count"]}
            for row in categories
        ],
    })


@app.errorhandler(413)
def file_too_large(_error):
    return jsonify(error="Request too large. Images must be under 2 MB."), 413


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify(error="API endpoint or report not found."), 404
    return "Page not found", 404


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
