import re
from io import BytesIO

from flask import render_template, request, send_file
from flask_login import current_user, login_required

from config import DB_PATH
from database import (
    get_athlete_param, get_zones,
    save_workout, list_saved_workouts, delete_saved_workout,
)
from workout_generator import generate_workout, build_custom_workout
from fit_writer import build_fit_workout
from zwo_writer import build_zwo_workout


def _export_filename(data, ext):
    label = data.get("goal_label") or data.get("goal") or "workout"
    duration = data.get("duration_min")
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    if duration:
        slug += f"-{duration}min"
    return f"bikeodon-{slug}.{ext}"


def _resolve_ftp_and_zones(uid):
    ftp = get_athlete_param(DB_PATH, uid, "ftp") or get_athlete_param(DB_PATH, uid, "cp_watts")
    power_zones = get_zones(DB_PATH, uid, "power") or None
    return ftp, power_zones


def register_routes(app):

    @app.route("/training")
    @login_required
    def training():
        return render_template("training.html")

    @app.route("/training/generate", methods=["POST"])
    @login_required
    def generate_training_workout():
        uid = int(current_user.id)
        ftp, power_zones = _resolve_ftp_and_zones(uid)
        if not ftp:
            return {"ok": False, "error": "no_ftp",
                    "message": "Set your FTP in Settings before generating a workout."}, 200
        data = request.get_json(force=True) or {}
        try:
            duration_min = int(data.get("duration_min", 60))
            hardness = float(data.get("hardness", 0.5))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_input", "message": "Invalid duration or hardness."}, 200
        result = generate_workout(data.get("goal"), duration_min, hardness, ftp, power_zones)
        return result, 200

    @app.route("/training/custom/finalize", methods=["POST"])
    @login_required
    def finalize_custom_training_workout():
        uid = int(current_user.id)
        ftp, power_zones = _resolve_ftp_and_zones(uid)
        if not ftp:
            return {"ok": False, "error": "no_ftp",
                    "message": "Set your FTP in Settings before building a workout."}, 200
        data = request.get_json(force=True) or {}
        result = build_custom_workout(data.get("steps"), ftp, power_zones, data.get("goal_label"))
        return result, 200

    @app.route("/training/save", methods=["POST"])
    @login_required
    def save_training_workout():
        uid = int(current_user.id)
        data = request.get_json(force=True) or {}
        name = (data.get("name") or "").strip()
        workout = data.get("workout")
        if not name:
            return {"ok": False, "error": "bad_input", "message": "Name is required."}, 200
        if not workout or not workout.get("steps"):
            return {"ok": False, "error": "bad_input", "message": "No workout to save."}, 200
        workout_id = save_workout(DB_PATH, uid, name, workout)
        return {"ok": True, "id": workout_id}, 200

    @app.route("/training/saved", methods=["GET"])
    @login_required
    def list_saved_training_workouts():
        uid = int(current_user.id)
        return {"ok": True, "workouts": list_saved_workouts(DB_PATH, uid)}, 200

    @app.route("/training/saved/<int:workout_id>/delete", methods=["POST"])
    @login_required
    def delete_saved_training_workout(workout_id):
        uid = int(current_user.id)
        deleted = delete_saved_workout(DB_PATH, uid, workout_id)
        return {"ok": deleted}, 200

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
