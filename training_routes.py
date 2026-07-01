import re
from io import BytesIO

from flask import render_template, request, send_file
from flask_login import current_user, login_required

from config import DB_PATH
from database import get_athlete_param
from workout_generator import generate_workout
from fit_writer import build_fit_workout
from zwo_writer import build_zwo_workout


def _export_filename(data, ext):
    label = data.get("goal_label") or data.get("goal") or "workout"
    duration = data.get("duration_min")
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    if duration:
        slug += f"-{duration}min"
    return f"bikeodon-{slug}.{ext}"


def register_routes(app):

    @app.route("/training")
    @login_required
    def training():
        return render_template("training.html")

    @app.route("/training/generate", methods=["POST"])
    @login_required
    def generate_training_workout():
        uid = int(current_user.id)
        ftp = get_athlete_param(DB_PATH, uid, "ftp") or get_athlete_param(DB_PATH, uid, "cp_watts")
        if not ftp:
            return {"ok": False, "error": "no_ftp",
                    "message": "Set your FTP in Settings before generating a workout."}, 200
        data = request.get_json(force=True) or {}
        try:
            duration_min = int(data.get("duration_min", 60))
            hardness = float(data.get("hardness", 0.5))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_input", "message": "Invalid duration or hardness."}, 200
        result = generate_workout(data.get("goal"), duration_min, hardness, ftp)
        return result, 200

    @app.route("/training/export.fit", methods=["POST"])
    @login_required
    def export_training_fit():
        data = request.get_json(force=True) or {}
        try:
            fit_bytes = build_fit_workout(data)
        except (KeyError, ValueError, TypeError):
            return {"ok": False, "error": "bad_workout", "message": "Invalid workout data."}, 400
        return send_file(
            BytesIO(fit_bytes), mimetype="application/octet-stream",
            as_attachment=True, download_name=_export_filename(data, "fit"),
        )

    @app.route("/training/export.zwo", methods=["POST"])
    @login_required
    def export_training_zwo():
        data = request.get_json(force=True) or {}
        try:
            zwo_xml = build_zwo_workout(data)
        except (KeyError, ValueError, TypeError):
            return {"ok": False, "error": "bad_workout", "message": "Invalid workout data."}, 400
        return send_file(
            BytesIO(zwo_xml.encode("utf-8")), mimetype="application/octet-stream",
            as_attachment=True, download_name=_export_filename(data, "zwo"),
        )
