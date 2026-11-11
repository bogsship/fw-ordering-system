import hashlib

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from config import BASE_DIR, Config
from database import query_one
from routes import register_routes
from services.common import (
    current_user,
    csrf_token,
    ensure_customer_verification_schema,
    ensure_feedback_product_schema,
)


def development_source_version():
    watched_files = list(BASE_DIR.glob("*.py"))
    for folder, suffix in (
        ("routes", ".py"),
        ("services", ".py"),
        ("templates", ".html"),
        ("static/css", ".css"),
        ("static/js", ".js"),
    ):
        watched_files.extend(
            path for path in (BASE_DIR / folder).rglob(f"*{suffix}")
            if path.is_file()
        )

    signature = hashlib.sha256()
    for path in sorted(watched_files):
        stat = path.stat()
        signature.update(path.relative_to(BASE_DIR).as_posix().encode("utf-8"))
        signature.update(f"{stat.st_mtime_ns}:{stat.st_size}".encode("ascii"))
    return signature.hexdigest()


def inject_globals():
    user = current_user()
    if getattr(g, "rendering_error_page", False):
        return {
            "current_user": None,
            "csrf_token": csrf_token,
            "unread_notifications": 0,
            "admin_unread_notifications": 0,
            "admin_new_order_count": 0,
            "admin_new_feedback_count": 0,
            "pending_verification_count": 0,
            "cart_item_count": 0,
        }
    unread_notifications = 0
    admin_unread_notifications = 0
    admin_new_order_count = 0
    admin_new_feedback_count = 0
    pending_verification_count = 0
    cart = session.get("cart", {})
    cart_item_count = (
        sum(int(quantity) for quantity in cart.values() if str(quantity).isdigit())
        if isinstance(cart, dict) else 0
    )
    if user and user.get("role") == "customer":
        result = query_one(
            "SELECT COUNT(*) AS count FROM notifications WHERE user_id=%s AND is_read=0",
            (user["id"],),
        )
        unread_notifications = result["count"]
    elif user and user.get("role") == "admin":
        ensure_customer_verification_schema()
        ensure_feedback_product_schema()
        result = query_one(
            "SELECT COUNT(*) AS count FROM notifications WHERE user_id=%s AND is_read=0",
            (user["id"],),
        )
        admin_unread_notifications = result["count"]
        admin_new_order_count = query_one(
            """SELECT COUNT(*) AS count FROM notifications
               WHERE user_id=%s AND title='New order received' AND is_read=0""",
            (user["id"],),
        )["count"]
        admin_new_feedback_count = query_one(
            "SELECT COUNT(*) AS count FROM feedback WHERE admin_checked_at IS NULL"
        )["count"]
        pending_verification_count = query_one(
            """SELECT COUNT(*) AS count FROM users
               WHERE role='customer' AND verification_status='Pending'"""
        )["count"]
    return {
        "current_user": user,
        "csrf_token": csrf_token,
        "unread_notifications": unread_notifications,
        "admin_unread_notifications": admin_unread_notifications,
        "admin_new_order_count": admin_new_order_count,
        "admin_new_feedback_count": admin_new_feedback_count,
        "pending_verification_count": pending_verification_count,
        "cart_item_count": cart_item_count,
    }


def inject_development_reload():
    if not current_app.config.get("DEV_AUTO_RELOAD"):
        return {}
    return {"dev_reload_version": development_source_version()}


def development_live_reload():
    if not current_app.config.get("DEV_AUTO_RELOAD"):
        abort(404)
    response = jsonify(version=development_source_version())
    response.headers["Cache-Control"] = "no-store"
    return response


def protect_post_forms():
    if request.method == "POST":
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        expected = session.get("_csrf")
        if not sent or not expected or sent != expected:
            abort(400, description="Invalid or missing CSRF token. Refresh the page and try again.")


def validate_session():
    stored_user = session.get("user")
    if not stored_user:
        return None
    if not isinstance(stored_user, dict):
        session.clear()
        flash("Your session is invalid. Please log in again.", "warning")
        return redirect(url_for("login"))
    if not session.get("auth_user_id"):
        if current_app.testing:
            return None
        session.clear()
        flash("Your session has expired. Please log in again.", "warning")
        return redirect(url_for("login"))
    if session.get("auth_user_id") != stored_user.get("id"):
        session.clear()
        flash("Your session is invalid. Please log in again.", "warning")
        return redirect(url_for("login"))

    ensure_customer_verification_schema()
    user = query_one(
        """SELECT id,full_name,email,role,phone,customer_id,verification_status,
                  email_verified,account_status,verification_rejection_reason
           FROM users WHERE id=%s""",
        (session["auth_user_id"],),
    )
    if not user or str(user.get("role") or "").strip().lower() != stored_user.get("role"):
        session.clear()
        flash("Your session is no longer valid. Please log in again.", "warning")
        return redirect(url_for("login"))
    user["role"] = str(user["role"]).strip().lower()
    user["email_verified"] = bool(user.get("email_verified", True))
    session["user"] = user
    session.permanent = True
    if user["role"] == "customer" and user.get("account_status") == "Deactivated":
        session.clear()
        flash("Your account is deactivated. Contact the shop for assistance.", "danger")
        return redirect(url_for("login"))
    return None


def forbidden(_error):
    g.rendering_error_page = True
    return render_template("error.html", code=403, message="You do not have permission to view this page."), 403


def not_found(_error):
    g.rendering_error_page = True
    return render_template("error.html", code=404, message="The page you’re looking for could not be found."), 404


def bad_request(error):
    g.rendering_error_page = True
    return render_template("error.html", code=400, message=getattr(error, "description", "Bad request.")), 400


def internal_server_error(_error):
    g.rendering_error_page = True
    return render_template(
        "error.html",
        code=500,
        message="Something went wrong on our end. Please try again in a little while.",
    ), 500


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.context_processor(inject_globals)
    app.context_processor(inject_development_reload)
    app.before_request(validate_session)
    app.before_request(protect_post_forms)
    app.add_url_rule(
        "/__dev/live-reload",
        endpoint="development_live_reload",
        view_func=development_live_reload,
    )

    @app.after_request
    def disable_development_caching(response):
        if app.config.get("DEV_AUTO_RELOAD"):
            response.headers["Cache-Control"] = "no-store"
            app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
        return response

    register_routes(app)
    app.register_error_handler(403, forbidden)
    app.register_error_handler(404, not_found)
    app.register_error_handler(400, bad_request)
    app.register_error_handler(500, internal_server_error)
    return app


app = create_app()
