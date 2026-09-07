import os
import io
import re
import csv
import json
import time
import secrets
import logging
import datetime
import threading
from functools import wraps
from logging.handlers import RotatingFileHandler

from flask import (
    Flask, render_template, request, jsonify, send_file, redirect,
    url_for, session, abort, g,
)
from werkzeug.security import generate_password_hash, check_password_hash

import db
import storage
from util import atomic_write_json
from config import get_config
from model import (
    train_model, extract_embedding_for_image, load_model_from_bytes,
    predict_with_model, is_valid_image,
)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app():
    config_class = get_config()
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(config_class)
    app.permanent_session_lifetime = datetime.timedelta(hours=app.config["SESSION_LIFETIME_HOURS"])

    if not app.config.get("DATABASE_URL"):
        os.makedirs(app.config["DATASET_DIR"], exist_ok=True)

    _configure_logging(app)
    db.register_db(app)

    # Provision the one account that must always exist. Idempotent -- an
    # existing super_admin's password is never touched by this. From here
    # on, authentication is entirely database-driven (see the `users`
    # table); SUPER_ADMIN_EMAIL/PASSWORD only matter on the very first run.
    with app.app_context():
        db.ensure_super_admin(
            app.config["SUPER_ADMIN_NAME"],
            _normalize_email(app.config["SUPER_ADMIN_EMAIL"]),
            generate_password_hash(app.config["SUPER_ADMIN_PASSWORD"]),
        )
    if app.config["ENV_NAME"] != "production" and not os.environ.get("SUPER_ADMIN_PASSWORD"):
        app.logger.warning(
            "SUPER_ADMIN_PASSWORD is not set -- using the insecure default dev password. "
            "Set SUPER_ADMIN_EMAIL/SUPER_ADMIN_PASSWORD in your environment before deploying."
        )

    _reset_stale_training_status(app)
    _register_request_hooks(app)
    _register_routes(app)
    _register_error_handlers(app)

    return app


def _configure_logging(app):
    level = getattr(logging, app.config.get("LOG_LEVEL", "INFO"))
    app.logger.setLevel(level)
    if not app.debug:
        formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

        # Stdout is the handler that actually matters on a host with an
        # ephemeral filesystem (Render, etc.): the platform's log dashboard
        # captures stdout/stderr, not arbitrary local files -- and on such
        # a host the file below may not even survive the next restart. Log
        # to both; stdout is what you'll actually see in production.
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(level)
        app.logger.addHandler(stream_handler)

        try:
            file_handler = RotatingFileHandler(app.config["LOG_PATH"], maxBytes=1_000_000, backupCount=3)
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            app.logger.addHandler(file_handler)
        except OSError:
            # Read-only or otherwise unwritable filesystem -- stdout above
            # already covers logging, so this is a soft failure, not fatal.
            app.logger.warning("Could not open LOG_PATH for file logging; continuing with stdout only.")
    logging.getLogger("model").setLevel(level)
    logging.getLogger("db").setLevel(level)


def _reset_stale_training_status(app):
    """A process that just started can't possibly have a training thread
    running. If the status file claims otherwise (crash / restart / deploy
    mid-training), reset it so the UI doesn't lock users out of retraining
    forever.
    """
    status = _read_train_status(app)
    if status.get("running"):
        _write_train_status(app, {
            "running": False,
            "progress": 0,
            "status": "error",
            "message": "Training was interrupted by a server restart. Please start training again.",
            "updated_at": datetime.datetime.utcnow().isoformat(),
        })


# ---------------------------------------------------------------------------
# Train status persistence
# ---------------------------------------------------------------------------

_train_lock = threading.Lock()


def _write_train_status(app, status_dict):
    atomic_write_json(app.config["TRAIN_STATUS_PATH"], status_dict)


def _read_train_status(app):
    path = app.config["TRAIN_STATUS_PATH"]
    if not os.path.exists(path):
        return {"running": False, "progress": 0, "status": "idle", "message": "Not trained yet."}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"running": False, "progress": 0, "status": "idle", "message": "Not trained yet."}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_email(email):
    return (email or "").strip().lower()


def _valid_email(email):
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


_login_attempts = {}  # ip -> (fail_count, locked_until_epoch)


def _is_locked_out(ip):
    count, locked_until = _login_attempts.get(ip, (0, 0))
    return locked_until > time.time()


def _register_failed_login(app, ip):
    count, _ = _login_attempts.get(ip, (0, 0))
    count += 1
    locked_until = 0
    if count >= app.config["LOGIN_MAX_ATTEMPTS"]:
        locked_until = time.time() + app.config["LOGIN_LOCKOUT_SECONDS"]
        count = 0
    _login_attempts[ip] = (count, locked_until)


def _clear_login_attempts(ip):
    _login_attempts.pop(ip, None)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.current_user:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def login_required_api(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.current_user:
            return jsonify({"error": "authentication required"}), 401
        return view(*args, **kwargs)
    return wrapped


def super_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.current_user:
            return redirect(url_for("login", next=request.path))
        if g.current_user["role"] != "super_admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def super_admin_required_api(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.current_user:
            return jsonify({"error": "authentication required"}), 401
        # Role is read from the authenticated database user loaded by
        # load_current_user() below -- never from request data -- so a
        # forged "role" field in a form/JSON body has no effect here.
        if g.current_user["role"] != "super_admin":
            return jsonify({"error": "forbidden"}), 403
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Request hooks: session csrf token + csrf validation
# ---------------------------------------------------------------------------

CSRF_EXEMPT_PATHS = {"/recognize_face"}


def _register_request_hooks(app):
    @app.before_request
    def ensure_csrf_token():
        session.permanent = True
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)

    @app.before_request
    def load_current_user():
        # The single source of truth for "who is this request from" and
        # "what role do they have" -- every decorator and template below
        # reads g.current_user, never session data directly, so a request
        # can never claim a role for itself. A session that points at a
        # user who's since been deactivated or whose approval was revoked
        # is treated as logged out and cleared here, not left dangling.
        g.current_user = None
        uid = session.get("user_id")
        if uid:
            user = db.get_user(uid)
            if user and user["is_approved"] and user["is_active"]:
                g.current_user = user
            else:
                session.clear()

    @app.before_request
    def csrf_protect():
        if app.config.get("TESTING"):
            return
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return
        if request.path in CSRF_EXEMPT_PATHS:
            return
        token = session.get("csrf_token")
        sent = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
        if not token or not sent or not secrets.compare_digest(str(token), str(sent)):
            abort(400, description="Invalid or missing CSRF token.")

    NAV_BY_ENDPOINT = {
        "index": "dashboard",
        "students_page": "students",
        "mark_attendance_page": "mark",
        "attendance_record": "records",
        "analytics": "analytics",
        "admin_page": "admin",
    }

    @app.context_processor
    def inject_globals():
        user = g.get("current_user")
        return {
            "csrf_token": session.get("csrf_token", ""),
            "current_year": datetime.date.today().year,
            "is_authenticated": user is not None,
            "current_user": user,
            "is_super_admin": bool(user and user["role"] == "super_admin"),
            "active_nav": NAV_BY_ENDPOINT.get(request.endpoint, ""),
        }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _register_routes(app):

    @app.route("/health")
    def health():
        try:
            conn_ok = True
            db.count_students()
        except Exception:
            conn_ok = False
        status = "ok" if conn_ok else "degraded"
        code = 200 if conn_ok else 503
        return jsonify({"status": status, "time": datetime.datetime.utcnow().isoformat()}), code

    # ---------- Auth ----------

    def _safe_next_path(value):
        # Only ever redirect to a same-site relative path -- an
        # unvalidated `next` value (e.g. "//evil.com" or
        # "https://evil.com") would otherwise be an open redirect.
        if not value or not value.startswith("/") or value.startswith("//"):
            return None
        return value

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.current_user:
            return redirect(url_for("index"))
        error = None
        if request.method == "POST":
            ip = request.remote_addr or "unknown"
            if _is_locked_out(ip):
                error = "Too many failed attempts. Please wait a minute and try again."
            else:
                email = _normalize_email(request.form.get("email", ""))
                password = request.form.get("password", "")
                user = db.get_user_by_email(email) if email else None
                password_ok = user is not None and check_password_hash(user["password_hash"], password)

                if password_ok and user["is_approved"] and user["is_active"]:
                    _clear_login_attempts(ip)
                    session.clear()  # drop any pre-auth session state (session fixation)
                    session["user_id"] = user["id"]
                    session["csrf_token"] = secrets.token_hex(32)
                    db.update_last_login(user["id"])
                    next_path = _safe_next_path(request.form.get("next")) or url_for("index")
                    return redirect(next_path)

                # Specific states (pending/disabled) are only ever shown once
                # the password has already been verified correct -- so this
                # never tells a guesser whether an email is registered, only
                # confirms account state to someone who already knows the
                # password.
                _register_failed_login(app, ip)
                if password_ok and not user["is_approved"]:
                    error = "Your account is pending administrator approval."
                elif password_ok and not user["is_active"]:
                    error = "This account has been disabled. Contact an administrator."
                else:
                    error = "Invalid email or password."
        return render_template("login.html", error=error, next=request.args.get("next", ""))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if g.current_user:
            return redirect(url_for("index"))
        errors = {}
        name = ""
        email = ""
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = _normalize_email(request.form.get("email", ""))
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")

            if not name or len(name) > 120:
                errors["name"] = "Enter your full name."
            if not _valid_email(email):
                errors["email"] = "Enter a valid email address."
            if len(password) < 8 or len(password) > 128:
                errors["password"] = "Password must be at least 8 characters."
            if password != confirm:
                errors["confirm_password"] = "Passwords do not match."
            if not errors and db.get_user_by_email(email):
                errors["email"] = "An account with this email already exists."

            if not errors:
                db.create_user(
                    name, email, generate_password_hash(password),
                    role="user", is_active=True, is_approved=False, is_verified=False,
                )
                app.logger.info("New user registered pending approval: %s", email)
                return render_template("register_success.html", email=email)
        return render_template("register.html", errors=errors, name=name, email=email)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---------- Dashboard ----------

    @app.route("/")
    @login_required
    def index():
        meta = storage.load_model_meta(app.config)
        total_students = db.count_students()
        ready_count = len(meta.get("trained_student_ids", [])) if meta else 0
        today_count = db.today_attendance_count()
        attendance_rate = round((today_count / total_students) * 100) if total_students else 0
        recent = db.recent_attendance(limit=8)
        train_status = _read_train_status(app)
        stats = {
            "total_students": total_students,
            "ready_count": ready_count,
            "today_count": today_count,
            "attendance_rate": attendance_rate,
            "model_exists": meta is not None,
            "last_trained": meta.get("trained_at") if meta else None,
            "num_samples": meta.get("num_samples") if meta else 0,
            "recent": recent,
            "train_status": train_status,
        }
        return render_template("index.html", stats=stats)

    @app.route("/attendance_stats")
    @login_required_api
    def attendance_stats():
        days = request.args.get("days", 30, type=int)
        days = max(7, min(days, 90))
        labels, counts = db.attendance_by_day(days=days)
        return jsonify({"dates": labels, "counts": counts})

    @app.route("/analytics")
    @login_required
    def analytics():
        by_student = db.attendance_by_student(limit=10)
        return render_template("analytics.html", by_student=by_student)

    # ---------- Students ----------

    @app.route("/students")
    @login_required
    def students_page():
        search = request.args.get("q", "").strip()
        rows = db.list_students(search=search or None)
        meta = storage.load_model_meta(app.config)
        trained_ids = set(meta.get("trained_student_ids", [])) if meta else set()

        students = []
        for r in rows:
            image_count = storage.count_face_images(app.config, r["id"])
            if r["id"] in trained_ids:
                readiness = "ready"
            elif image_count == 0:
                readiness = "no_images"
            else:
                readiness = "needs_training"
            students.append({
                **dict(r),
                "image_count": image_count,
                "attendance_count": db.attendance_count_for_student(r["id"]),
                "readiness": readiness,
            })
        return render_template("students.html", students=students, search=search,
                                capture_count=app.config["CAPTURE_IMAGE_COUNT"])

    @app.route("/students/<int:sid>", methods=["GET"])
    @login_required_api
    def student_detail(sid):
        student = db.get_student(sid)
        if not student:
            return jsonify({"error": "not found"}), 404
        return jsonify(dict(student))

    @app.route("/students/<int:sid>", methods=["DELETE"])
    @super_admin_required_api
    def delete_student_route(sid):
        student = db.get_student(sid)
        if not student:
            return jsonify({"error": "not found"}), 404
        db.delete_student(sid)
        storage.delete_face_images(app.config, sid)
        app.logger.info("Deleted student id=%s name=%r", sid, student["name"])
        return jsonify({"deleted": True})

    # ---------- Registration / capture ----------
    # GET (render the page) and POST (create the record) need different auth
    # decorators -- login_required redirects, login_required_api returns
    # JSON 401 -- so they're bound as two view functions on the same URL
    # rather than one view branching on request.method.

    @app.route("/add_student", methods=["GET"], endpoint="add_student_page")
    @login_required
    def add_student_page():
        return render_template(
            "add_student.html",
            capture_count=app.config["CAPTURE_IMAGE_COUNT"],
        )

    @app.route("/add_student", methods=["POST"], endpoint="add_student_submit")
    @login_required_api
    def add_student_submit():
        data = request.form
        name = data.get("name", "").strip()
        roll = data.get("roll", "").strip()
        cls = data.get("class", "").strip()
        sec = data.get("sec", "").strip()
        reg_no = data.get("reg_no", "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        sid = db.create_student(name, roll, cls, sec, reg_no)
        if not app.config.get("DATABASE_URL"):
            os.makedirs(os.path.join(app.config["DATASET_DIR"], str(sid)), exist_ok=True)
        return jsonify({"student_id": sid})

    @app.route("/upload_face", methods=["POST"])
    @login_required_api
    def upload_face():
        raw_id = request.form.get("student_id", "")
        if not raw_id.isdigit():
            return jsonify({"error": "invalid student_id"}), 400
        student_id = int(raw_id)
        student = db.get_student(student_id)
        if not student:
            return jsonify({"error": "unknown student_id"}), 404

        # student_id is validated as an existing integer PK above, so it's
        # safe to use as a storage key (prevents path traversal on the local
        # filesystem backend).
        files = request.files.getlist("images[]")
        saved = 0
        rejected = 0
        for f in files:
            if not f or not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png"):
                rejected += 1
                continue
            # Extension allowlist alone doesn't verify content -- a renamed
            # non-image file would pass that check. Decode it for real
            # before storing it.
            data = f.read()
            if len(data) > 8 * 1024 * 1024 or not is_valid_image(data):
                rejected += 1
                continue
            fname = f"{datetime.datetime.utcnow().timestamp():.6f}_{saved}{ext}"
            try:
                storage.save_face_image(app.config, student_id, fname, data)
                saved += 1
            except Exception as e:
                app.logger.error("Failed to save uploaded image: %s", e)
                # A failed insert can leave a Postgres connection in an
                # aborted-transaction state that rejects further queries
                # until rolled back; harmless no-op on SQLite.
                db.get_db().rollback()
        return jsonify({"saved": saved, "rejected": rejected})

    # ---------- Training ----------

    def _train_worker():
        # Runs in a background thread, which has no Flask request/app
        # context of its own -- storage.py's database backend needs one
        # (it goes through db.get_db(), which reads current_app/g), so the
        # whole body runs inside an explicit app context pushed here.
        with app.app_context():
            def progress_callback(pct, message, status="running"):
                _write_train_status(app, {
                    "running": status == "running",
                    "progress": pct,
                    "status": status,
                    "message": message,
                    "updated_at": datetime.datetime.utcnow().isoformat(),
                })
            try:
                student_ids = storage.student_ids_with_images(app.config)
                image_data_by_student = {
                    sid: storage.list_face_images(app.config, sid) for sid in student_ids
                }
                model_bytes, meta = train_model(
                    image_data_by_student,
                    min_images_per_student=app.config["MIN_TRAINING_IMAGES_PER_STUDENT"],
                    min_students=app.config["MIN_TRAINING_STUDENTS"],
                    face_detection_confidence=app.config["FACE_DETECTION_CONFIDENCE"],
                    progress_callback=progress_callback,
                )
                if model_bytes is not None:
                    storage.save_model(app.config, model_bytes, meta)
            except Exception as e:  # belt-and-braces: train_model already catches internally
                app.logger.exception("Unhandled training error")
                _write_train_status(app, {
                    "running": False, "progress": 0, "status": "error",
                    "message": f"Training failed: {e}",
                    "updated_at": datetime.datetime.utcnow().isoformat(),
                })
            finally:
                _train_lock.release()

    @app.route("/train_model", methods=["POST"])
    @super_admin_required_api
    def train_model_route():
        status = _read_train_status(app)
        if status.get("running"):
            return jsonify({"status": "already_running", "message": status.get("message")}), 409
        acquired = _train_lock.acquire(blocking=False)
        if not acquired:
            return jsonify({"status": "already_running"}), 409
        _write_train_status(app, {
            "running": True, "progress": 0, "status": "running",
            "message": "Starting training…",
            "updated_at": datetime.datetime.utcnow().isoformat(),
        })
        t = threading.Thread(target=_train_worker, daemon=True)
        t.start()
        return jsonify({"status": "started"}), 202

    @app.route("/train_status")
    @login_required_api
    def train_status_route():
        status = _read_train_status(app)
        meta = storage.load_model_meta(app.config)
        status["model_exists"] = meta is not None
        status["last_trained"] = meta.get("trained_at") if meta else None
        status["num_students"] = meta.get("num_students") if meta else 0
        status["num_samples"] = meta.get("num_samples") if meta else 0
        return jsonify(status)

    # ---------- Live recognition (public kiosk) ----------

    @app.route("/mark_attendance")
    def mark_attendance_page():
        return render_template(
            "mark_attendance.html",
            poll_interval=app.config["RECOGNITION_POLL_INTERVAL_MS"],
        )

    @app.route("/recognize_face", methods=["POST"])
    def recognize_face():
        if "image" not in request.files:
            return jsonify({"status": "error", "error": "no image provided"}), 400
        try:
            result = extract_embedding_for_image(
                request.files["image"].stream,
                min_detection_confidence=app.config["FACE_DETECTION_CONFIDENCE"],
            )
        except Exception:
            app.logger.exception("Face extraction failed")
            return jsonify({"status": "error", "error": "could not process image"}), 500

        if result["status"] in ("no_face", "multiple_faces", "decode_error"):
            return jsonify({"status": result["status"]}), 200

        clf = load_model_from_bytes(storage.load_model_bytes(app.config))
        if clf is None:
            return jsonify({"status": "model_not_trained"}), 200

        try:
            pred_label, conf = predict_with_model(clf, result["embedding"])
        except Exception:
            app.logger.exception("Prediction failed")
            return jsonify({"status": "error", "error": "prediction failed"}), 500

        threshold = app.config["RECOGNITION_CONFIDENCE_THRESHOLD"]
        if conf < threshold:
            return jsonify({"status": "unknown", "confidence": conf}), 200

        student = db.get_student(int(pred_label))
        if not student:
            return jsonify({"status": "unknown", "confidence": conf}), 200

        if db.has_marked_today(student["id"]):
            return jsonify({
                "status": "already_marked", "student_id": student["id"],
                "name": student["name"], "confidence": conf,
            }), 200

        inserted = db.mark_attendance(student["id"], student["name"])
        status = "marked" if inserted else "already_marked"
        return jsonify({
            "status": status, "student_id": student["id"],
            "name": student["name"], "confidence": conf,
        }), 200

    # ---------- Attendance history ----------

    @app.route("/attendance_record")
    @login_required
    def attendance_record():
        period = request.args.get("period", "all")
        search = request.args.get("q", "").strip()
        # Upper-bounded, not just lower-bounded: an unbounded ?page= value
        # flows into a SQL LIMIT/OFFSET and can overflow SQLite's 64-bit
        # integer column, turning a bogus page number into a 500 instead of
        # just an empty result set.
        page = max(1, min(request.args.get("page", 1, type=int), 1_000_000))
        per_page = 25
        rows, total = db.attendance_records(period=period, search=search or None, page=page, per_page=per_page)
        total_pages = max(1, (total + per_page - 1) // per_page)
        return render_template(
            "attendance_record.html", records=rows, period=period, search=search,
            page=page, total_pages=total_pages, total=total,
        )

    @app.route("/download_csv")
    @login_required
    def download_csv():
        rows = db.all_attendance_for_export()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "student_id", "name", "timestamp"])
        for r in rows:
            writer.writerow([r["id"], r["student_id"], r["name"], r["timestamp"]])
        mem = io.BytesIO(output.getvalue().encode("utf-8"))
        return send_file(mem, as_attachment=True, download_name="attendance.csv", mimetype="text/csv")

    # ---------- Admin: user management ----------
    # Every route below re-checks the role server-side via
    # super_admin_required(_api) -- the admin nav link is hidden from normal
    # users in the template too, but that's cosmetic; these decorators are
    # the actual enforcement, and a request straight to the URL/API without
    # them would 403/redirect exactly the same as clicking through.

    @app.route("/admin")
    @super_admin_required
    def admin_page():
        users = db.list_users()
        counts = db.user_counts_by_status()
        return render_template("admin.html", users=users, counts=counts)

    def _user_or_404(uid):
        user = db.get_user(uid)
        if not user:
            abort(404)
        return user

    def _would_remove_last_super_admin(user):
        return user["role"] == "super_admin" and db.count_super_admins(active_only=True) <= 1

    @app.route("/admin/users/<int:uid>/approve", methods=["POST"])
    @super_admin_required_api
    def admin_approve_user(uid):
        user = _user_or_404(uid)
        db.set_user_approved(user["id"], True)
        app.logger.info("User %s approved by %s", user["email"], g.current_user["email"])
        return jsonify({"ok": True})

    @app.route("/admin/users/<int:uid>/deactivate", methods=["POST"])
    @super_admin_required_api
    def admin_deactivate_user(uid):
        user = _user_or_404(uid)
        if _would_remove_last_super_admin(user):
            return jsonify({"error": "Cannot deactivate the only active super admin."}), 400
        db.set_user_active(user["id"], False)
        app.logger.info("User %s deactivated by %s", user["email"], g.current_user["email"])
        return jsonify({"ok": True})

    @app.route("/admin/users/<int:uid>/reactivate", methods=["POST"])
    @super_admin_required_api
    def admin_reactivate_user(uid):
        user = _user_or_404(uid)
        db.set_user_active(user["id"], True)
        app.logger.info("User %s reactivated by %s", user["email"], g.current_user["email"])
        return jsonify({"ok": True})

    @app.route("/admin/users/<int:uid>/role", methods=["POST"])
    @super_admin_required_api
    def admin_change_user_role(uid):
        user = _user_or_404(uid)
        new_role = request.form.get("role", "")
        if new_role not in ("super_admin", "user"):
            return jsonify({"error": "invalid role"}), 400
        if new_role == "user" and _would_remove_last_super_admin(user):
            return jsonify({"error": "Cannot remove the only active super admin's role."}), 400
        db.set_user_role(user["id"], new_role)
        app.logger.info("User %s role changed to %s by %s", user["email"], new_role, g.current_user["email"])
        return jsonify({"ok": True})

    @app.route("/admin/users/<int:uid>", methods=["DELETE"])
    @super_admin_required_api
    def admin_delete_user(uid):
        user = _user_or_404(uid)
        if _would_remove_last_super_admin(user):
            return jsonify({"error": "Cannot delete the only active super admin."}), 400
        db.delete_user(user["id"])
        app.logger.info("User %s deleted by %s", user["email"], g.current_user["email"])
        return jsonify({"ok": True})


def _register_error_handlers(app):
    @app.errorhandler(400)
    def bad_request(e):
        if request.path.startswith(("/students", "/upload_face", "/train_model", "/recognize_face", "/admin/users")):
            return jsonify({"error": str(e.description) if hasattr(e, "description") else "bad request"}), 400
        return render_template("errors/400.html", error=e), 400

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(405)
    def method_not_allowed(e):
        if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
            return jsonify({"error": "method not allowed"}), 405
        return render_template("errors/404.html"), 405

    @app.errorhandler(404)
    def not_found(e):
        if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
            return jsonify({"error": "not found"}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception("Unhandled server error")
        return render_template("errors/500.html"), 500


app = create_app()

if __name__ == "__main__":
    # use_reloader is off by default: Werkzeug's reloader watches every
    # imported module including site-packages, and mediapipe/protobuf are
    # only imported lazily (on first recognition/training request). That
    # first import can register as a "file changed" false positive and
    # restart the whole process mid-training, killing the background
    # training thread. Debug mode (nicer error pages, auto-reload opt-in
    # via FLASK_USE_RELOADER=1) still works fine without it.
    use_reloader = os.environ.get("FLASK_USE_RELOADER", "0") == "1"
    app.run(
        debug=app.config["DEBUG"],
        use_reloader=use_reloader,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5000)),
    )
