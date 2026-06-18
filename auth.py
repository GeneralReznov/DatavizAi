"""
auth.py — Authentication, RBAC, and OTP verification for DataVizAI.

Features
--------
- User signup (name, email, phone, password)
- 6-digit OTP verification via email with 10-minute expiry
- Login with role-based access control (roles: 'user', 'admin')
- Admin dashboard API: list users, list uploads, change role/status
- RBAC decorator ``require_role(*roles)`` for route protection
- OTP delivery via SMTP; falls back to console log for dev environments

Database : SQLite at flask_app/users.db (Flask-SQLAlchemy)
Sessions  : Flask-Login (sets current_user, persists via secure cookie)
Passwords : Werkzeug PBKDF2-SHA256 hashing (no external deps)
"""

import os
import random
import string
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

from flask import (
    Blueprint, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import (
    LoginManager, UserMixin,
    current_user, login_required, login_user, logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)

# SendGrid is optional — imported lazily so the app boots without it
try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail as _SgMail
    _SENDGRID_AVAILABLE = True
except ImportError:
    _SENDGRID_AVAILABLE = False

# ── Shared extensions (initialised by init_auth) ────────────────────────────
db        = SQLAlchemy()
login_mgr = LoginManager()


# ═══════════════════════════════════════════════════════════════════════════════
# Database models
# ═══════════════════════════════════════════════════════════════════════════════

class User(UserMixin, db.Model):
    """Registered user account."""
    __tablename__ = "users"

    id            = db.Column(db.Integer,    primary_key=True)
    name          = db.Column(db.String(120), nullable=False)
    email         = db.Column(db.String(180), unique=True, nullable=False, index=True)
    phone         = db.Column(db.String(30),  nullable=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role          = db.Column(db.String(20),  nullable=False, default="user")
    # UNVERIFIED → ACTIVE → DISABLED
    status        = db.Column(db.String(20),  nullable=False, default="UNVERIFIED")
    created_at    = db.Column(db.DateTime,    nullable=False, default=datetime.utcnow)

    otps = db.relationship("OTP", back_populates="user",
                           cascade="all, delete-orphan", lazy="dynamic")

    def __repr__(self):
        return f"<User {self.email!r} role={self.role!r} status={self.status!r}>"


class OTP(db.Model):
    """One-time password record (one active OTP per user at a time)."""
    __tablename__ = "otps"

    id         = db.Column(db.Integer,    primary_key=True)
    user_id    = db.Column(db.Integer,    db.ForeignKey("users.id"), nullable=False, index=True)
    otp_hash   = db.Column(db.String(256), nullable=False)
    expires_at = db.Column(db.DateTime,   nullable=False)
    created_at = db.Column(db.DateTime,   nullable=False, default=datetime.utcnow)

    user = db.relationship("User", back_populates="otps")

    @classmethod
    def create(cls, user_id: int, code: str) -> "OTP":
        """Return a new (unsaved) OTP for the given user and plaintext code."""
        return cls(
            user_id    = user_id,
            otp_hash   = generate_password_hash(code),
            expires_at = datetime.utcnow() + timedelta(minutes=10),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# OTP email delivery
# ═══════════════════════════════════════════════════════════════════════════════

def _send_otp(to_email: str, code: str) -> None:
    """Send OTP to *to_email*.

    Delivery priority:
      1. SendGrid  — when SENDGRID_API_KEY is set (preferred)
      2. SMTP      — when SMTP_HOST + SMTP_USER are set
      3. Console   — dev fallback; prints the code to stdout
    """
    sg_key = os.environ.get("SENDGRID_API_KEY", "")
    frm    = os.environ.get("FROM_EMAIL", "")

    html_body = (
        f"<div style='font-family:Arial,sans-serif;max-width:480px;margin:0 auto'>"
        f"<h2 style='color:#1e3a8a'>DataVizAI</h2>"
        f"<p>Your one-time verification code is:</p>"
        f"<div style='font-size:2.2rem;font-family:monospace;letter-spacing:8px;"
        f"color:#1e3a8a;background:#f1f5f9;padding:18px 24px;border-radius:8px;"
        f"display:inline-block;margin:12px 0'><strong>{code}</strong></div>"
        f"<p style='color:#64748b;font-size:0.9rem'>Valid for 10 minutes. "
        f"Do not share this code with anyone.</p></div>"
    )
    text_body = f"Your DataVizAI OTP is: {code}\n\nValid for 10 minutes."

    # ── 1. SendGrid ──────────────────────────────────────────────────────────
    if sg_key and _SENDGRID_AVAILABLE and frm:
        try:
            message = _SgMail(
                from_email=frm,
                to_emails=to_email,
                subject="Your OTP — DataVizAI",
                html_content=html_body,
            )
            sg = SendGridAPIClient(sg_key)
            resp = sg.send(message)
            logger.info("OTP sent via SendGrid to %s (status %s)", to_email, resp.status_code)
            return
        except Exception as exc:
            logger.error("SendGrid failed for %s: %s — trying SMTP fallback.", to_email, exc)

    # ── 2. SMTP ──────────────────────────────────────────────────────────────
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ.get("SMTP_USER", "")
    pwd  = os.environ.get("SMTP_PASS", "")
    frm_smtp = frm or user

    if host and user:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your OTP — DataVizAI"
        msg["From"]    = frm_smtp
        msg["To"]      = to_email
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        try:
            with smtplib.SMTP(host, port, timeout=10) as srv:
                srv.ehlo()
                srv.starttls()
                srv.login(user, pwd)
                srv.sendmail(frm_smtp, [to_email], msg.as_string())
            logger.info("OTP sent via SMTP to %s", to_email)
            return
        except Exception as exc:
            logger.error("SMTP failed for %s: %s — using console fallback.", to_email, exc)

    # ── 3. Console fallback ──────────────────────────────────────────────────
    print(f"\n{'═'*55}", flush=True)
    print("  OTP VERIFICATION CODE  (no email provider configured)")
    print(f"  Recipient : {to_email}")
    print(f"  OTP code  : {code}")
    print(f"  Expires   : 10 minutes from now")
    print(f"{'═'*55}\n", flush=True)
    logger.warning("No email provider configured. OTP for %s printed to console.", to_email)


def _make_otp() -> str:
    """Return a random 6-digit numeric OTP string."""
    return "".join(random.choices(string.digits, k=6))


# ═══════════════════════════════════════════════════════════════════════════════
# RBAC decorator
# ═══════════════════════════════════════════════════════════════════════════════

def require_role(*roles: str):
    """Route decorator — user must be authenticated AND hold one of *roles*.

    Usage::

        @some_blueprint.route("/secret")
        @require_role("admin")
        def secret_view(): ...
    """
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            if current_user.role not in roles:
                return jsonify({"error": "Forbidden — insufficient privileges"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# Auth blueprint  (/auth/*)
# ═══════════════════════════════════════════════════════════════════════════════

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/signup", methods=["POST"])
def signup():
    """Register a new user and send an OTP to their email."""
    body = request.get_json(silent=True) or {}

    name     = (body.get("name", "") or "").strip()
    email    = (body.get("email", "") or "").strip().lower()
    phone    = (body.get("phone", "") or "").strip() or None
    password = body.get("password", "") or ""

    if not name:
        return jsonify({"error": "Name is required"}), 400
    if not email or "@" not in email:
        return jsonify({"error": "A valid email address is required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "This email is already registered"}), 400

    # First registered user gets the admin role automatically
    role = "admin" if User.query.count() == 0 else "user"

    user = User(
        name          = name,
        email         = email,
        phone         = phone,
        password_hash = generate_password_hash(password),
        role          = role,
        status        = "UNVERIFIED",
    )
    db.session.add(user)
    db.session.flush()          # get user.id without committing

    code = _make_otp()
    otp  = OTP.create(user.id, code)
    db.session.add(otp)
    db.session.commit()

    _send_otp(user.email, code)
    logger.info("New user registered: %s (role=%s)", email, role)

    return jsonify({
        "success": True,
        "message": "Account created. Check your email for the OTP.",
        "userId":  user.id,
    }), 201


@auth_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    """Verify the OTP for an UNVERIFIED account and activate it."""
    body     = request.get_json(silent=True) or {}
    user_id  = body.get("userId")
    code     = str(body.get("otp", "")).strip()

    if not user_id or not code:
        return jsonify({"error": "userId and otp are required"}), 400

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    otp = (
        OTP.query
        .filter_by(user_id=user_id)
        .order_by(OTP.created_at.desc())
        .first()
    )

    if not otp or otp.expires_at < datetime.utcnow():
        return jsonify({"error": "OTP has expired. Request a new one."}), 400

    if not check_password_hash(otp.otp_hash, code):
        return jsonify({"error": "Incorrect OTP. Please try again."}), 400

    user.status = "ACTIVE"
    db.session.delete(otp)
    db.session.commit()

    login_user(user, remember=True)
    logger.info("User %s verified and logged in", user.email)
    return jsonify({"success": True, "role": user.role}), 200


@auth_bp.route("/login", methods=["POST"])
def login():
    """Authenticate an existing ACTIVE user and start their session."""
    body     = request.get_json(silent=True) or {}
    email    = (body.get("email", "") or "").strip().lower()
    password = body.get("password", "") or ""

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    user = User.query.filter_by(email=email).first()

    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid email or password"}), 401

    if user.status == "UNVERIFIED":
        return jsonify({
            "error": "Account not yet verified. Check your email for the OTP.",
            "unverified": True,
            "userId": user.id,
        }), 403

    if user.status == "DISABLED":
        return jsonify({"error": "Account is disabled. Contact the administrator."}), 403

    login_user(user, remember=True)
    logger.info("User %s logged in (role=%s)", email, user.role)
    return jsonify({"success": True, "role": user.role}), 200


@auth_bp.route("/resend-otp", methods=["POST"])
def resend_otp():
    """Delete the existing OTP and issue a fresh one for an unverified user."""
    body    = request.get_json(silent=True) or {}
    user_id = body.get("userId")

    if not user_id:
        return jsonify({"error": "userId is required"}), 400

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    OTP.query.filter_by(user_id=user_id).delete()

    code = _make_otp()
    otp  = OTP.create(user.id, code)
    db.session.add(otp)
    db.session.commit()

    _send_otp(user.email, code)
    logger.info("OTP resent to %s", user.email)
    return jsonify({"success": True, "message": "A new OTP has been sent."}), 200


@auth_bp.route("/logout")
@login_required
def logout():
    """End the current user's session and redirect to the login page."""
    logger.info("User %s logged out", current_user.email)
    logout_user()
    return redirect(url_for("login_page"))


# ═══════════════════════════════════════════════════════════════════════════════
# Admin blueprint  (/admin/*)
# ═══════════════════════════════════════════════════════════════════════════════

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/dashboard")
@require_role("admin")
def dashboard():
    """Render the admin dashboard HTML page."""
    return render_template("admin.html")


@admin_bp.route("/users")
@require_role("admin")
def list_users():
    """Return JSON list of all registered users."""
    users = User.query.order_by(User.created_at.desc()).all()
    return jsonify([
        {
            "id":         u.id,
            "name":       u.name,
            "email":      u.email,
            "phone":      u.phone or "",
            "role":       u.role,
            "status":     u.status,
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ])


@admin_bp.route("/users/<int:user_id>")
@require_role("admin")
def get_user(user_id):
    """Return JSON for a single user."""
    u = db.session.get(User, user_id)
    if not u:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "id": u.id, "name": u.name, "email": u.email,
        "phone": u.phone or "", "role": u.role, "status": u.status,
        "created_at": u.created_at.isoformat(),
    })


@admin_bp.route("/users/<int:user_id>/role", methods=["PATCH"])
@require_role("admin")
def update_role(user_id):
    """Promote or demote a user between 'user' and 'admin'."""
    body     = request.get_json(silent=True) or {}
    new_role = (body.get("role", "") or "").lower()
    if new_role not in ("user", "admin"):
        return jsonify({"error": "Role must be 'user' or 'admin'"}), 400
    if user_id == current_user.id:
        return jsonify({"error": "You cannot change your own role"}), 400
    u = db.session.get(User, user_id)
    if not u:
        return jsonify({"error": "User not found"}), 404
    u.role = new_role
    db.session.commit()
    logger.info("Admin %s changed user %s role to %s", current_user.email, u.email, new_role)
    return jsonify({"success": True, "role": u.role})


@admin_bp.route("/users/<int:user_id>/status", methods=["PATCH"])
@require_role("admin")
def update_status(user_id):
    """Enable or disable a user account."""
    body       = request.get_json(silent=True) or {}
    new_status = (body.get("status", "") or "").upper()
    if new_status not in ("ACTIVE", "DISABLED"):
        return jsonify({"error": "Status must be 'ACTIVE' or 'DISABLED'"}), 400
    if user_id == current_user.id:
        return jsonify({"error": "You cannot change your own status"}), 400
    u = db.session.get(User, user_id)
    if not u:
        return jsonify({"error": "User not found"}), 404
    u.status = new_status
    db.session.commit()
    logger.info("Admin %s set user %s status to %s", current_user.email, u.email, new_status)
    return jsonify({"success": True, "status": u.status})


@admin_bp.route("/files")
@require_role("admin")
def list_files():
    """Return paginated JSON list of files in the uploads folder."""
    from flask import current_app
    page     = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 10, type=int)

    uploads_dir = current_app.config.get(
        "UPLOAD_FOLDER",
        os.path.join(current_app.root_path, "uploads"),
    )
    all_files = []
    if os.path.isdir(uploads_dir):
        for fname in sorted(os.listdir(uploads_dir), reverse=True):
            fpath = os.path.join(uploads_dir, fname)
            if not os.path.isfile(fpath):
                continue
            stat = os.stat(fpath)
            all_files.append({
                "id":          len(all_files) + 1,
                "filename":    fname,
                "upload_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "file_size":   stat.st_size,
                "status":      "stored",
            })

    total     = len(all_files)
    pages     = max(1, (total + per_page - 1) // per_page)
    page_data = all_files[(page - 1) * per_page : page * per_page]

    return jsonify({
        "files":        page_data,
        "total":        total,
        "pages":        pages,
        "current_page": page,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Wiring helper — call from app.py
# ═══════════════════════════════════════════════════════════════════════════════

def init_auth(app):
    """Register auth/admin blueprints and initialise Flask-Login + SQLAlchemy.

    Call this in app.py immediately after creating the Flask ``app`` object::

        from auth import init_auth
        init_auth(app)
    """
    db_path = os.path.join(app.root_path, "users.db")
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", f"sqlite:///{db_path}")
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)

    db.init_app(app)
    login_mgr.init_app(app)
    login_mgr.login_view       = "login_page"
    login_mgr.login_message    = "Please log in to access this page."
    login_mgr.session_protection = "strong"

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

    with app.app_context():
        db.create_all()
        _ensure_admin()

    @login_mgr.user_loader
    def _load_user(user_id):
        return db.session.get(User, int(user_id))

    return app


def _ensure_admin() -> None:
    """Create (or promote) the admin account from ADMIN_EMAIL + ADMIN_PASSWORD.

    Called once inside an app context after db.create_all().  Safe to run on
    every startup — skips silently if the account already exists with the
    correct role and status.
    """
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    admin_pass  = os.environ.get("ADMIN_PASSWORD", "").strip()

    if not admin_email or not admin_pass:
        return  # env vars not set; nothing to do

    existing = User.query.filter_by(email=admin_email).first()

    if existing is None:
        admin = User(
            name          = "Administrator",
            email         = admin_email,
            password_hash = generate_password_hash(admin_pass),
            role          = "admin",
            status        = "ACTIVE",
        )
        db.session.add(admin)
        db.session.commit()
        print(f"[auth] Admin account created: {admin_email}", flush=True)
        logger.info("Admin account created: %s", admin_email)
    else:
        changed = False
        if existing.role != "admin":
            existing.role = "admin"
            changed = True
        if existing.status != "ACTIVE":
            existing.status = "ACTIVE"
            changed = True
        if changed:
            db.session.commit()
            logger.info("Admin account updated (role/status fixed): %s", admin_email)
