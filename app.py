from flask import Flask, render_template, request, jsonify, redirect, session, url_for
from tensorflow.keras.applications.efficientnet import preprocess_input
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from datetime import date, datetime, timedelta
import os
import re
import io
import csv
from collections import Counter
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False

# ---------------------------------------------------
# AI IMPORTS (Optional – only if model exists)
# ---------------------------------------------------
AI_AVAILABLE = False
model = None
FOOD_LABELS = []
try:
    import tensorflow as tf
    from PIL import Image
    import numpy as np
    if os.path.exists("food101_efficientnet.h5") and os.path.exists("food101_labels.txt"):
        model = tf.keras.models.load_model("food101_efficientnet.h5", compile=False)
        with open("food101_labels.txt") as f:
            FOOD_LABELS = [line.strip() for line in f]
        AI_AVAILABLE = True
        print("✅ Food101 AI model loaded successfully.")
    else:
        print("⚠️ AI model files not found. Scan feature disabled.")
except Exception as e:
    print(f"⚠️ AI model could not be loaded: {e}. Scan feature disabled.")

# ---------------------------------------------------
# NUTRITION DATABASE (per standard serving)
# ---------------------------------------------------
NUTRITION_DB = {
    "pizza":        {"serving": "1 slice",  "calories": 266, "protein": 11, "carbs": 33, "fat": 10},
    "hamburger":    {"serving": "1 burger", "calories": 295, "protein": 17, "carbs": 30, "fat": 14},
    "apple pie":    {"serving": "1 slice",  "calories": 296, "protein": 3,  "carbs": 42, "fat": 14},
    "default":      {"serving": "1 serving","calories": 250, "protein": 8,  "carbs": 30, "fat": 9},
}

# ---------------------------------------------------
# RECIPE DATA (static, for recommendation)
# ---------------------------------------------------
RECIPE_DATA = [
    # ... (your existing RECIPE_DATA list)
]

# ---------------------------------------------------
# FLASK APP CONFIG
# ---------------------------------------------------
app = Flask(__name__)
app.secret_key = "nutrilens_secret_key_change_in_production"

# FIX: use absolute path so nutrilens.db is always found regardless of
# which directory you run "python app.py" from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE  = os.path.join(BASE_DIR, "nutrilens.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "static", "profile_images"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "static", "recipe_images"), exist_ok=True)

# ---------------------------------------------------
# DATABASE CONNECTION
# ---------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# ---------------------------------------------------
# HELPER: NOTIFICATION PUSH
# ---------------------------------------------------
def push_notification(user_id, message, notif_type="info", link=""):
    # FIX: use try/finally so the connection is always closed, even on error.
    # The ALTER TABLE migration is handled once in init_database(); no need to repeat it here.
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO notifications (user_id, message, type, link) VALUES (?,?,?,?)",
            (user_id, message, notif_type, link)
        )
        conn.commit()
    except Exception as e:
        print(f"Notification error: {e}")
    finally:
        conn.close()

# ---------------------------------------------------
# HELPER: FOOD IMAGE FROM API
# ---------------------------------------------------
def get_food_image(recipe_name):
    import urllib.request, urllib.parse, json as _json
    keywords = [w for w in recipe_name.split() if len(w) > 3][:2]
    for keyword in keywords:
        try:
            q = urllib.parse.quote_plus(keyword)
            url = f"https://www.themealdb.com/api/json/v1/1/search.php?s={q}"
            req = urllib.request.urlopen(url, timeout=3)
            data = _json.loads(req.read())
            if data.get("meals"):
                return data["meals"][0]["strMealThumb"]
        except Exception:
            pass
    seed = abs(hash(recipe_name)) % 9999
    return f"https://api.dicebear.com/7.x/shapes/svg?seed={seed}&backgroundColor=fce7f3,fff0f3,fdf2f8"

# ---------------------------------------------------
# INIT DATABASE TABLES & COLUMNS (run at startup)
# ---------------------------------------------------
def init_database():
    conn = get_db_connection()

    # --- Ensure recipes table has all needed columns ---
    for col_sql in [
        "ALTER TABLE recipes ADD COLUMN video_url TEXT DEFAULT ''",
        "ALTER TABLE recipes ADD COLUMN instructions TEXT DEFAULT ''",
        "ALTER TABLE recipes ADD COLUMN status TEXT DEFAULT 'pending'",
        "ALTER TABLE recipes ADD COLUMN is_verified INTEGER DEFAULT 0",
        "ALTER TABLE recipes ADD COLUMN verification_status TEXT DEFAULT 'pending'",
        "ALTER TABLE recipes ADD COLUMN dietitian_note TEXT DEFAULT ''",
        "ALTER TABLE recipes ADD COLUMN verified_by INTEGER DEFAULT NULL",
        "ALTER TABLE recipes ADD COLUMN verified_at DATETIME DEFAULT NULL",
        "ALTER TABLE recipes ADD COLUMN is_recommended INTEGER DEFAULT 0",
        "ALTER TABLE recipes ADD COLUMN expert_tag TEXT DEFAULT ''",
        "ALTER TABLE recipes ADD COLUMN recommended_by INTEGER DEFAULT NULL",
        "ALTER TABLE recipes ADD COLUMN recommended_at DATETIME DEFAULT NULL",
        "ALTER TABLE recipes ADD COLUMN recipe_tags TEXT DEFAULT ''",
        "ALTER TABLE recipes ADD COLUMN added_by_dietitian INTEGER DEFAULT NULL",
        "ALTER TABLE recipes ADD COLUMN dietitian_flag_count INTEGER DEFAULT 0",
        "ALTER TABLE recipes ADD COLUMN image_url TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(col_sql)
            conn.commit()
        except Exception:
            pass

    # --- Ensure notifications table has link column ---
    try:
        conn.execute("ALTER TABLE notifications ADD COLUMN link TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass

    # --- Ensure weight_logs has notes column ---
    try:
        conn.execute("ALTER TABLE weight_logs ADD COLUMN notes TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass

    # --- Ensure chat_messages table has is_read column ---
    try:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN is_read INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass

    # --- Ensure meals table has meal_type and notes columns ---
    for col_sql in [
        "ALTER TABLE meals ADD COLUMN meal_type TEXT DEFAULT 'other'",
        "ALTER TABLE meals ADD COLUMN notes TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(col_sql)
            conn.commit()
        except Exception:
            pass

    # --- Ensure saved_recipes table exists ---
    conn.execute("""CREATE TABLE IF NOT EXISTS saved_recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        recipe_id INTEGER NOT NULL,
        saved_at DATETIME DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
        UNIQUE (user_id, recipe_id)
    )""")
    conn.commit()
    conn.close()

# ---------------------------------------------------
# HOME PAGE
# ---------------------------------------------------
@app.route('/')
def home():
    return render_template('home.html')

# ---------------------------------------------------
# USER AUTH
# ---------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["full_name"]
            return redirect(url_for('dashboard'))
        else:
            return render_template('user/login.html', error="Invalid email or password")
    return render_template('user/login.html')

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("user/signup.html")
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()
    if not all([full_name, email, password, confirm_password]):
        return render_template("user/signup.html", error="All fields are required")
    if password != confirm_password:
        return render_template("user/signup.html", error="Passwords do not match")
    if len(password) < 8:
        return render_template("user/signup.html", error="Password must be at least 8 characters")
    hashed = generate_password_hash(password)
    conn = get_db_connection()
    try:
        conn.execute("""
        INSERT INTO users (full_name, email, password, created_at)
        VALUES (?, ?, ?, datetime('now'))
         """, (full_name, email, hashed))
        conn.commit()
        conn.close()
        return redirect(url_for("login"))
    except sqlite3.IntegrityError:
        conn.close()
        return render_template("user/signup.html", error="Email already registered")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------------------------------------------------
# ADMIN AUTH
# ---------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db_connection()
        admin = conn.execute(
            "SELECT * FROM admin WHERE email=?",
            (email,)
        ).fetchone()
        conn.close()

        # ❌ If admin not found
        if not admin:
            return render_template("admin/login.html", error="Invalid admin credentials")

        # ✅ Only hashed password check (PERMANENT FIX)
        if not check_password_hash(admin["password"], password):
            return render_template("admin/login.html", error="Invalid admin credentials")

        # ✅ Success
        session["admin_logged_in"] = True
        session["admin_email"] = admin["email"]

        return redirect("/admin/dashboard")

    return render_template("admin/login.html")

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    session.pop("admin_email", None)
    return redirect(url_for("home"))

# ---------------------------------------------------
# ADMIN DASHBOARD & MANAGEMENT
# ---------------------------------------------------
@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    conn = get_db_connection()

    # ── Core system counts ──
    total_users       = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_dietitians  = conn.execute("SELECT COUNT(*) FROM dietitians WHERE status='approved'").fetchone()[0]
    total_recipes     = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    total_ingredients = conn.execute("SELECT COUNT(*) FROM ingredients").fetchone()[0]

    # Pending dietitian approvals (list for quick-approve widget)
    pending_dietitians = conn.execute("SELECT * FROM dietitians WHERE status='pending'").fetchall()
    pending_count      = len(pending_dietitians)

    # Reported recipes needing action
    reported_recipes = conn.execute("SELECT SUM(reported) FROM recipes").fetchone()[0] or 0

    # New users registered today
    new_users_today = conn.execute(
        "SELECT COUNT(*) FROM users WHERE DATE(created_at) = DATE('now')"
    ).fetchone()[0]

    # ── User Growth — last 30 days (for chart) ──
    signup_rows = conn.execute("""
        SELECT DATE(created_at) as signup_date, COUNT(*) as count
        FROM users
        WHERE created_at >= DATE('now', '-30 days')
        GROUP BY DATE(created_at)
        ORDER BY signup_date ASC
    """).fetchall()
    from datetime import date as _date, timedelta
    signup_map = {r["signup_date"]: r["count"] for r in signup_rows}
    signups = [
        {"signup_date": (_date.today() - timedelta(days=29 - i)).isoformat(),
         "count": signup_map.get((_date.today() - timedelta(days=29 - i)).isoformat(), 0)}
        for i in range(30)
    ]

    # ── Recent user registrations (last 8) ──
    recent_users = conn.execute("""
        SELECT full_name, email, DATE(created_at) as joined
        FROM users
        ORDER BY created_at DESC
        LIMIT 8
    """).fetchall()

    # ── Recent feedback summary ──
    try:
        avg_rating = conn.execute("SELECT ROUND(AVG(rating),1) FROM feedback").fetchone()[0] or 0
        total_feedback = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        recent_feedback = conn.execute("""
            SELECT u.full_name, f.rating, f.comment, DATE(f.created_at) as fdate
            FROM feedback f
            LEFT JOIN users u ON f.user_id = u.id
            ORDER BY f.created_at DESC LIMIT 5
        """).fetchall()
    except Exception:
        avg_rating = 0
        total_feedback = 0
        recent_feedback = []

    # ── Recipe categories breakdown (admin manages content) ──
    recipe_categories = conn.execute("""
        SELECT COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') as category,
               COUNT(*) as count
        FROM recipes
        GROUP BY category
        ORDER BY count DESC
        LIMIT 8
    """).fetchall()

    # ── Most reported recipes (top 5 needing review) ──
    try:
        reported_list = conn.execute("""
            SELECT r.id, r.name,
                   COALESCE(u.full_name, 'Unknown') as submitter_name,
                   COUNT(rp.id) as report_count
            FROM recipes r
            LEFT JOIN users u ON r.user_id = u.id
            LEFT JOIN recipe_reports rp ON r.id = rp.recipe_id
            GROUP BY r.id
            HAVING report_count > 0
            ORDER BY report_count DESC
            LIMIT 5
        """).fetchall()
    except Exception:
        reported_list = []

    conn.close()
    return render_template("admin/dashboard.html",
                           total_users=total_users,
                           total_dietitians=total_dietitians,
                           total_recipes=total_recipes,
                           total_ingredients=total_ingredients,
                           pending_dietitians=pending_dietitians,
                           pending_count=pending_count,
                           reported_recipes=reported_recipes,
                           new_users_today=new_users_today,
                           signups=signups,
                           recent_users=recent_users,
                           avg_rating=avg_rating,
                           total_feedback=total_feedback,
                           recent_feedback=recent_feedback,
                           recipe_categories=recipe_categories,
                           reported_list=reported_list)

@app.route("/admin/users")
def admin_users():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    conn = get_db_connection()
    users = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin/admin_users.html", users=users)

@app.route("/admin/delete_user/<int:user_id>")
def admin_delete_user(user_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    conn = get_db_connection()
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_users"))

@app.route("/admin/dietitians")
def admin_all_dietitians():
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    conn = get_db_connection()
    dietitians = conn.execute("SELECT * FROM dietitians ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("admin/all_dietitians.html", dietitians=dietitians)

@app.route("/admin/approve_dietitian/<int:dietitian_id>")
def approve_dietitian(dietitian_id):
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    conn = get_db_connection()
    conn.execute("UPDATE dietitians SET status='approved' WHERE dietitian_id=?", (dietitian_id,))
    conn.commit()
    conn.close()
    return redirect("/admin/dietitians")

@app.route("/admin/approved-dietitians")
def admin_approved_dietitians():
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    conn = get_db_connection()
    dietitians = conn.execute("SELECT * FROM dietitians WHERE status='approved' ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("admin/approved_dietitians.html", dietitians=dietitians)

@app.route("/admin/pending-dietitians")
def admin_pending_dietitians():
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    conn = get_db_connection()
    dietitians = conn.execute("SELECT * FROM dietitians WHERE status='pending' ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("admin/pending_dietitians.html", dietitians=dietitians)


@app.route("/admin/recipe/approve/<int:recipe_id>")
def admin_approve_recipe(recipe_id):
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    conn = get_db_connection()
    recipe = conn.execute("SELECT user_id, name FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if recipe:
        conn.execute("UPDATE recipes SET status='active' WHERE id=?", (recipe_id,))
        conn.commit()
        push_notification(recipe["user_id"], f'Your recipe "{recipe["name"]}" has been approved!',
                          notif_type="approval", link=f"/recipe/{recipe_id}")
    conn.close()
    return redirect(request.referrer or "/admin/recipes")

@app.route("/admin/feedback")
def admin_feedback():
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")

    conn = get_db_connection()

    # Safe migration — ensure user_feedback table exists with all needed columns
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_feedback (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            rating       INTEGER NOT NULL DEFAULT 1,
            category     TEXT    NOT NULL DEFAULT 'general',
            message      TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'open',
            admin_reply  TEXT,
            submitted_at DATETIME DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.commit()

    # Try to add missing columns safely
    for col in [
        "ALTER TABLE user_feedback ADD COLUMN admin_reply TEXT",
        "ALTER TABLE user_feedback ADD COLUMN status TEXT DEFAULT 'open'",
    ]:
        try: conn.execute(col); conn.commit()
        except Exception: pass

    # ── All feedback ──
    feedbacks = conn.execute("""
        SELECT uf.id, uf.rating, uf.category, uf.message,
               uf.status, uf.admin_reply, uf.submitted_at,
               COALESCE(u.full_name, 'Anonymous') as user_name,
               COALESCE(u.email, '') as user_email
        FROM user_feedback uf
        LEFT JOIN users u ON uf.user_id = u.id
        ORDER BY uf.submitted_at DESC
    """).fetchall()

    total         = len(feedbacks)
    complaints    = sum(1 for f in feedbacks if f["category"] == "complaint")
    resolved      = sum(1 for f in feedbacks if f["status"] == "resolved")
    awaiting_reply = sum(1 for f in feedbacks if not f["admin_reply"] and f["status"] != "resolved")
    avg_rating    = round(sum(f["rating"] for f in feedbacks if f["rating"]) / total, 1) if total else 0

    stats = {
        "total": total,
        "complaints": complaints,
        "avg_rating": avg_rating,
        "resolved": resolved,
        "awaiting_reply": awaiting_reply,
    }

    # ── Chart data: daily feedback trend (last 14 days) ──
    daily_feedback = conn.execute("""
        SELECT DATE(submitted_at) as fb_date, COUNT(*) as count
        FROM user_feedback
        WHERE submitted_at >= DATE('now', '-13 days')
        GROUP BY DATE(submitted_at)
        ORDER BY fb_date ASC
    """).fetchall()

    # Fill in missing days with 0
    from datetime import date, timedelta
    daily_map = {r["fb_date"]: r["count"] for r in daily_feedback}
    daily_feedback_full = []
    for i in range(13, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        daily_feedback_full.append({"fb_date": d, "count": daily_map.get(d, 0)})

    # ── Chart data: rating distribution ──
    rating_dist = conn.execute("""
        SELECT rating, COUNT(*) as count
        FROM user_feedback
        WHERE rating IS NOT NULL
        GROUP BY rating ORDER BY rating DESC
    """).fetchall()

    # ── Chart data: category distribution ──
    category_dist = conn.execute("""
        SELECT category, COUNT(*) as count
        FROM user_feedback
        GROUP BY category ORDER BY count DESC
    """).fetchall()

    # ── Chart data: status distribution ──
    status_dist = conn.execute("""
        SELECT status, COUNT(*) as count
        FROM user_feedback
        GROUP BY status
    """).fetchall()

    # ── Improvements tracker ──
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS feedback_improvements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            description TEXT,
            category    TEXT DEFAULT 'general',
            status      TEXT DEFAULT 'planned',
            created_at  DATETIME DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.commit()
    improvements = conn.execute("""
        SELECT * FROM feedback_improvements ORDER BY created_at DESC
    """).fetchall()

    conn.close()

    return render_template(
        "admin/admin_feedback.html",
        feedbacks=[dict(f) for f in feedbacks],
        feedback_list=[dict(f) for f in feedbacks],
        stats=stats,
        daily_feedback=daily_feedback_full,
        rating_dist=[dict(r) for r in rating_dist],
        category_dist=[dict(r) for r in category_dist],
        status_dist=[dict(r) for r in status_dist],
        improvements=[dict(r) for r in improvements],
        awaiting_reply=awaiting_reply,
    )


@app.route("/admin/feedback/reply/<int:fb_id>", methods=["POST"])
def admin_feedback_reply(fb_id):
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    reply = request.form.get("admin_reply", "").strip()
    if reply:
        conn = get_db_connection()
        conn.execute("""
            UPDATE user_feedback SET admin_reply=?, status='resolved'
            WHERE id=?
        """, (reply, fb_id))
        conn.commit()
        # Notify the user
        row = conn.execute("SELECT user_id FROM user_feedback WHERE id=?", (fb_id,)).fetchone()
        if row and row["user_id"]:
            try:
                push_notification(row["user_id"],
                    "📨 The NutriLens team replied to your feedback.",
                    notif_type="info", link="/feedback")
            except Exception:
                pass
        conn.close()
    return redirect("/admin/feedback")


@app.route("/admin/feedback/status/<int:fb_id>", methods=["POST"])
def admin_feedback_status(fb_id):
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    status = request.form.get("status", "open")
    conn = get_db_connection()
    conn.execute("UPDATE user_feedback SET status=? WHERE id=?", (status, fb_id))
    conn.commit()
    conn.close()
    return redirect("/admin/feedback")


@app.route("/admin/feedback/improvement/add", methods=["POST"])
def admin_add_improvement():
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    title   = request.form.get("title","").strip()
    desc    = request.form.get("description","").strip()
    cat     = request.form.get("category","general")
    if title:
        conn = get_db_connection()
        conn.execute("""
            INSERT INTO feedback_improvements (title, description, category)
            VALUES (?,?,?)
        """, (title, desc, cat))
        conn.commit()
        conn.close()
    return redirect("/admin/feedback#improvements")


@app.route("/admin/feedback/improvement/status/<int:imp_id>", methods=["POST"])
def admin_improvement_status(imp_id):
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    status = request.form.get("status","planned")
    conn = get_db_connection()
    conn.execute("UPDATE feedback_improvements SET status=? WHERE id=?", (status, imp_id))
    conn.commit()
    conn.close()
    return redirect("/admin/feedback#improvements")


@app.route("/admin/feedback/improvement/delete/<int:imp_id>")
def admin_delete_improvement(imp_id):
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    conn = get_db_connection()
    conn.execute("DELETE FROM feedback_improvements WHERE id=?", (imp_id,))
    conn.commit()
    conn.close()
    return redirect("/admin/feedback#improvements")



@app.route("/admin/reported_recipes")
def admin_reported_recipes():
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")

    conn = get_db_connection()

    recipes = conn.execute(
        "SELECT * FROM recipes WHERE reported > 0"
    ).fetchall()

    conn.close()

    return render_template("admin/reported_recipes.html", recipes=recipes)

@app.route("/admin/reports")
def admin_reports():
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")

    conn = get_db_connection()

    total_users      = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_recipes    = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    total_meals      = conn.execute("SELECT COUNT(*) FROM meals").fetchone()[0]
    total_dietitians = conn.execute("SELECT COUNT(*) FROM dietitians").fetchone()[0]
    total_messages   = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
    total_reviews    = conn.execute("SELECT COUNT(*) FROM user_feedback").fetchone()[0]

    daily_rows = conn.execute("""
        SELECT date,
               COUNT(*)                        AS count,
               IFNULL(SUM(calories), 0)        AS total_calories,
               IFNULL(AVG(calories), 0)        AS avg_calories
        FROM meals
        GROUP BY date
        ORDER BY date ASC
        LIMIT 60
    """).fetchall()
    daily_meals = [dict(r) for r in daily_rows]

    top_food_rows = conn.execute("""
        SELECT name, COUNT(*) AS count
        FROM meals
        WHERE name IS NOT NULL AND name != ''
        GROUP BY LOWER(name)
        ORDER BY count DESC
        LIMIT 10
    """).fetchall()
    top_foods = [dict(r) for r in top_food_rows]

    macro_row = conn.execute("""
        SELECT ROUND(AVG(calories), 1) AS avg_cal,
               ROUND(AVG(protein),  1) AS avg_prot,
               ROUND(AVG(carbs),    1) AS avg_carbs,
               ROUND(AVG(fat),      1) AS avg_fat
        FROM meals
    """).fetchone()
    macro_avg = dict(macro_row) if macro_row else {}

    dow_map = {"0":"Sun","1":"Mon","2":"Tue","3":"Wed","4":"Thu","5":"Fri","6":"Sat"}
    dow_rows = conn.execute("""
        SELECT strftime('%w', date) AS dow,
               ROUND(AVG(calories), 0) AS avg_cal
        FROM meals
        WHERE calories IS NOT NULL
        GROUP BY dow
        ORDER BY dow ASC
    """).fetchall()
    dow_lookup = {r["dow"]: int(r["avg_cal"] or 0) for r in dow_rows}
    date_labels = [dow_map[str(i)] for i in range(7)]
    date_values = [dow_lookup.get(str(i), 0) for i in range(7)]

    activity_rows = conn.execute("""
        SELECT u.full_name,
               COUNT(m.id)               AS meal_count,
               ROUND(AVG(m.calories), 0) AS avg_cal
        FROM users u
        LEFT JOIN meals m ON m.user_id = u.id
        GROUP BY u.id
        ORDER BY meal_count DESC
        LIMIT 20
    """).fetchall()
    user_activity = [dict(r) for r in activity_rows]

    conn.close()

    return render_template(
        "admin/reports.html",
        total_users=total_users,
        total_recipes=total_recipes,
        total_meals=total_meals,
        total_dietitians=total_dietitians,
        total_messages=total_messages,
        total_reviews=total_reviews,
        daily_meals=daily_meals,
        top_foods=top_foods,
        macro_avg=macro_avg,
        date_labels=date_labels,
        date_values=date_values,
        user_activity=user_activity,
    )

@app.route("/admin/recipe/reject/<int:recipe_id>", methods=["POST","GET"])
def admin_reject_recipe(recipe_id):
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    reason = request.form.get("reason", "")
    conn = get_db_connection()
    recipe = conn.execute("SELECT user_id, name FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if recipe:
        conn.execute("UPDATE recipes SET status='rejected' WHERE id=?", (recipe_id,))
        conn.commit()
        msg = f'Your recipe "{recipe["name"]}" was not approved.'
        if reason: msg += f" Reason: {reason}"
        push_notification(recipe["user_id"], msg, notif_type="approval", link="/add_recipe")
    conn.close()
    return redirect(request.referrer or "/admin/recipes")

@app.route("/admin/recipe/delete/<int:recipe_id>")
def admin_delete_recipe(recipe_id):
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    conn = get_db_connection()
    conn.execute("DELETE FROM recipes WHERE id=?", (recipe_id,))
    conn.commit()
    conn.close()
    return redirect("/admin/recipes")

@app.route("/admin/recipe/verify/<int:recipe_id>")
def admin_verify_recipe(recipe_id):
    if not session.get("admin_logged_in"):
        return redirect("/admin/login")
    conn = get_db_connection()
    recipe = conn.execute("SELECT is_verified FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if recipe:
        new_val = 0 if recipe["is_verified"] else 1
        conn.execute("UPDATE recipes SET is_verified=? WHERE id=?", (new_val, recipe_id))
        conn.commit()
    conn.close()
    return redirect(request.referrer or "/admin/recipes")

# ---------------------------------------------------
# DIETITIAN AUTH & PANEL
# ---------------------------------------------------
@app.route("/dietitian/login", methods=["GET", "POST"])
def dietitian_login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db_connection()
        dietitian = conn.execute(
            "SELECT * FROM dietitians WHERE email=?",
            (email,)
        ).fetchone()
        conn.close()

        if not dietitian:
            return render_template("dietitian/login.html", error="Invalid email or password")

        # 🚫 If not approved
        if dietitian["status"] == "pending":
            return render_template("dietitian/login.html", pending=True)

        # ✅ Only hashed password check (FINAL FIX)
        if not check_password_hash(dietitian["password"], password):
            return render_template("dietitian/login.html", error="Invalid email or password")

        # ✅ Success
        session["dietitian_id"] = dietitian["dietitian_id"]
        session["dietitian_name"] = dietitian["name"]
        return redirect("/dietitian/dashboard")

    return render_template("dietitian/login.html")

@app.route("/dietitian/signup", methods=["GET", "POST"])
def dietitian_signup():
    if request.method == "GET":
        return render_template("dietitian/signup.html")
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    confirm = request.form.get("confirm_password", "").strip()
    license_number = request.form.get("license_number", "").strip()
    specialization = request.form.get("specialization", "").strip()
    experience_years = request.form.get("experience_years", "0").strip()
    bio = request.form.get("bio", "").strip()
    if not all([full_name, email, password, license_number]):
        return render_template("dietitian/signup.html", error="All required fields must be filled")
    if password != confirm:
        return render_template("dietitian/signup.html", error="Passwords do not match")
    if len(password) < 8:
        return render_template("dietitian/signup.html", error="Password must be at least 8 characters")
    hashed = generate_password_hash(password)
    conn = get_db_connection()
    try:
        existing = conn.execute("SELECT dietitian_id FROM dietitians WHERE email=?", (email,)).fetchone()
        if existing:
            return render_template("dietitian/signup.html", error="Email already registered")
        conn.execute("""
            INSERT INTO dietitians
                (name, email, password, license_number, specialization, experience_years, bio, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (full_name, email, hashed, license_number, specialization, experience_years, bio, "pending"))
        conn.commit()
        conn.close()
        return redirect("/dietitian/signup?signup=success")
    except Exception as e:
        conn.close()
        return render_template("dietitian/signup.html", error=f"An error occurred: {str(e)}")

@app.route("/dietitian/logout")
def dietitian_logout():
    session.clear()
    return redirect("/")

@app.route("/dietitian/dashboard")
def dietitian_dashboard():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    did = session["dietitian_id"]
    conn = get_db_connection()
    activities = conn.execute("""
        SELECT users.full_name, users.id as user_id, meals.name AS food_name, meals.calories, meals.date, meals.time
        FROM meals
        JOIN users ON meals.user_id = users.id
        JOIN user_dietitian ON users.id = user_dietitian.user_id
        WHERE user_dietitian.dietitian_id = ?
        ORDER BY meals.date DESC, meals.time DESC LIMIT 8
    """, (did,)).fetchall()
    clients = conn.execute("""
        SELECT users.id, users.full_name,
               ROUND(AVG(m.calories), 0) AS avg_cal,
               ROUND(AVG(m.protein), 0)  AS avg_prot,
               COUNT(DISTINCT m.date)    AS days_logged,
               CAST(julianday('now') - julianday(MAX(m.date)) AS INTEGER) AS days_since
        FROM users
        JOIN user_dietitian ON users.id = user_dietitian.user_id
        LEFT JOIN meals m ON m.user_id = users.id AND m.date >= date('now','-6 days')
        WHERE user_dietitian.dietitian_id = ?
        GROUP BY users.id
    """, (did,)).fetchall()
    reviewed_count = conn.execute("""
        SELECT COUNT(DISTINCT mr.id) FROM meal_reviews mr
        JOIN meals m ON mr.meal_id=m.id
        JOIN user_dietitian ud ON m.user_id=ud.user_id
        WHERE ud.dietitian_id=?
    """, (did,)).fetchone()[0]
    pending_count = conn.execute("""
        SELECT COUNT(m.id) FROM meals m
        JOIN user_dietitian ud ON m.user_id=ud.user_id
        WHERE ud.dietitian_id=?
        AND m.id NOT IN (SELECT meal_id FROM meal_reviews WHERE meal_id IS NOT NULL)
    """, (did,)).fetchone()[0]
    unread_messages = conn.execute("""
        SELECT COUNT(*) FROM chat_messages
        WHERE dietitian_id=? AND sender='user' AND COALESCE(is_read,0)=0
    """, (did,)).fetchone()[0]
    trend = conn.execute("""
        SELECT m.date, ROUND(AVG(m.calories),0) as avg_cal, COUNT(DISTINCT m.user_id) as active_clients
        FROM meals m JOIN user_dietitian ud ON m.user_id=ud.user_id
        WHERE ud.dietitian_id=? AND m.date >= date('now','-6 days')
        GROUP BY m.date ORDER BY m.date
    """, (did,)).fetchall()
    try:
        feedback_count = conn.execute("""
            SELECT COUNT(*) FROM user_feedback uf
            JOIN users u ON uf.user_id = u.id
            JOIN user_dietitian ud ON u.id = ud.user_id
            WHERE ud.dietitian_id = ?
              AND LOWER(uf.category) IN ('service', 'nutrition', 'recipe')
        """, (did,)).fetchone()[0]
    except Exception:
        feedback_count = 0
    conn.close()
    trend_labels = [row["date"] for row in trend]
    trend_data = [row["avg_cal"] or 0 for row in trend]
    return render_template("dietitian/dashboard.html",
        activities=activities, clients=clients, reviewed_count=reviewed_count,
        pending_count=pending_count, unread_messages=unread_messages,
        trend_labels=trend_labels, trend_data=trend_data,
        dietitian_name=session.get("dietitian_name", "Doctor"),
        now_hour=datetime.now().hour,
        feedback_count=feedback_count)

@app.route("/dietitian/clients")
def dietitian_clients():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    clients = conn.execute("""
        SELECT users.id, users.full_name, users.email
        FROM users JOIN user_dietitian ON users.id = user_dietitian.user_id
        WHERE user_dietitian.dietitian_id=?
    """, (session["dietitian_id"],)).fetchall()
    conn.close()
    return render_template("dietitian/clients.html", clients=clients)

@app.route("/dietitian/client/<int:user_id>")
def dietitian_client_page(user_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    user = conn.execute("SELECT full_name, email FROM users WHERE id=?", (user_id,)).fetchone()
    days = conn.execute("""
        SELECT date, COUNT(id) AS meals_count, SUM(calories) AS total_calories
        FROM meals WHERE user_id=? GROUP BY date ORDER BY date DESC
    """, (user_id,)).fetchall()
    conn.close()
    return render_template("dietitian/client_page.html", user=user, days=days, user_id=user_id)

@app.route("/dietitian/day-meals/<int:user_id>/<date>")
def day_meals(user_id, date):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    meals = conn.execute("""
        SELECT id, name, calories, protein, carbs, fat FROM meals WHERE user_id=? AND date=?
    """, (user_id, date)).fetchall()
    conn.close()
    return render_template("dietitian/day_meals.html", meals=meals, user_id=user_id, date=date)

@app.route("/dietitian/meal-reviews")
def dietitian_meal_reviews():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    days = conn.execute("""
        SELECT users.id AS user_id, users.full_name, meals.date,
               COUNT(meals.id) AS meals_count, SUM(meals.calories) AS total_calories
        FROM meals
        JOIN users ON meals.user_id = users.id
        JOIN user_dietitian ON users.id = user_dietitian.user_id
        WHERE user_dietitian.dietitian_id = ?
        GROUP BY users.id, meals.date ORDER BY meals.date DESC
    """, (session["dietitian_id"],)).fetchall()
    conn.close()
    return render_template("dietitian/meal_reviews.html", days=days)

@app.route("/dietitian/submit_review", methods=["POST"])
def submit_review():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    meal_id = request.form["meal_id"]
    review = request.form["review"]
    conn = get_db_connection()
    conn.execute("INSERT INTO meal_reviews (meal_id, dietitian_id, review) VALUES (?, ?, ?)",
                 (meal_id, session["dietitian_id"], review))
    conn.commit()
    conn.close()
    return redirect(request.referrer or "/dietitian/meal-reviews")

@app.route("/dietitian/progress")
def dietitian_progress_clients():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    clients = conn.execute("""
        SELECT users.id, users.full_name, users.email
        FROM users JOIN user_dietitian ON users.id = user_dietitian.user_id
        WHERE user_dietitian.dietitian_id=?
    """, (session["dietitian_id"],)).fetchall()
    conn.close()
    return render_template("dietitian/progress_clients.html", clients=clients)

@app.route("/dietitian/feedback")
def dietitian_feedback():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    did = session["dietitian_id"]
    conn = get_db_connection()
    # Ensure user_feedback table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_feedback (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            rating       INTEGER NOT NULL,
            category     TEXT    DEFAULT 'general',
            message      TEXT    NOT NULL,
            status       TEXT    DEFAULT 'pending',
            admin_reply  TEXT    DEFAULT '',
            submitted_at DATETIME DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    all_feedbacks = conn.execute("""
        SELECT
            uf.id,
            u.full_name,
            uf.rating,
            uf.category,
            uf.message,
            uf.status,
            uf.admin_reply,
            strftime('%d %b %Y', uf.submitted_at) AS submitted_at,
            uf.submitted_at AS raw_date
        FROM user_feedback uf
        JOIN users u ON uf.user_id = u.id
        JOIN user_dietitian ud ON u.id = ud.user_id
        WHERE ud.dietitian_id = ?
          AND LOWER(uf.category) IN ('service', 'nutrition', 'recipe')
        ORDER BY uf.submitted_at DESC
    """, (did,)).fetchall()
    conn.close()
    service_feedbacks   = [f for f in all_feedbacks if f["category"].lower() == "service"]
    nutrition_feedbacks = [f for f in all_feedbacks if f["category"].lower() == "nutrition"]
    recipe_feedbacks    = [f for f in all_feedbacks if f["category"].lower() == "recipe"]
    total_feedback = len(all_feedbacks)
    avg_rating = None
    if total_feedback > 0:
        avg_rating = round(sum(fb["rating"] for fb in all_feedbacks) / total_feedback, 1)
    rating_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for fb in all_feedbacks:
        r = fb["rating"]
        if r in rating_counts:
            rating_counts[r] += 1
    positive_count = rating_counts[4] + rating_counts[5]
    critical_count = rating_counts[1] + rating_counts[2]
    return render_template(
        "dietitian/dietitian_feedback.html",
        service_feedbacks=service_feedbacks,
        nutrition_feedbacks=nutrition_feedbacks,
        recipe_feedbacks=recipe_feedbacks,
        total_feedback=total_feedback,
        avg_rating=avg_rating,
        rating_counts=rating_counts,
        positive_count=positive_count,
        critical_count=critical_count,
    )

@app.route("/dietitian/client/<int:user_id>/progress")
def client_progress(user_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    progress = conn.execute("""
        SELECT date, SUM(calories) AS calories, SUM(protein) AS protein,
               SUM(carbs) AS carbs, SUM(fat) AS fat
        FROM meals WHERE user_id=? GROUP BY date ORDER BY date DESC
    """, (user_id,)).fetchall()
    trend = conn.execute("""
        SELECT date, SUM(calories) AS calories
        FROM meals WHERE user_id=? GROUP BY date ORDER BY date DESC LIMIT 7
    """, (user_id,)).fetchall()
    trend = trend[::-1]
    averages = conn.execute("""
        SELECT COALESCE(AVG(calories),0) AS avg_cal, COALESCE(AVG(protein),0) AS avg_protein,
               COALESCE(AVG(carbs),0) AS avg_carbs
        FROM (SELECT date, SUM(calories) calories, SUM(protein) protein, SUM(carbs) carbs
              FROM meals WHERE user_id=? GROUP BY date)
    """, (user_id,)).fetchone()
    correlation = conn.execute("""
        SELECT DATE(w.logged_at) AS date, w.weight, IFNULL(SUM(m.calories),0) AS calories
        FROM weight_logs w LEFT JOIN meals m ON w.user_id = m.user_id AND DATE(w.logged_at) = m.date
        WHERE w.user_id=? GROUP BY DATE(w.logged_at) ORDER BY DATE(w.logged_at) ASC
    """, (user_id,)).fetchall()
    weights = conn.execute("""
        SELECT logged_at, weight FROM weight_logs WHERE user_id=? ORDER BY logged_at
    """, (user_id,)).fetchall()
    conn.close()
    labels = [row["date"] for row in trend]
    data = [row["calories"] for row in trend]
    return render_template("dietitian/progress.html",
        progress=progress, trend=trend, averages=averages,
        weights=weights, correlation=correlation,
        labels=labels, data=data)

@app.route("/dietitian/messages")
def dietitian_messages_home():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    users = conn.execute("""
        SELECT DISTINCT users.id, users.full_name, users.email
        FROM users JOIN user_dietitian ON users.id = user_dietitian.user_id
        WHERE user_dietitian.dietitian_id = ? AND user_dietitian.payment_status = 'paid'
        ORDER BY users.full_name
    """, (session["dietitian_id"],)).fetchall()
    conn.close()
    return render_template("dietitian/messages_home.html", users=users)

@app.route("/dietitian/messages/<int:user_id>")
def dietitian_messages(user_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    conn.execute("""
        UPDATE chat_messages SET is_read=1
        WHERE user_id=? AND dietitian_id=? AND sender='user'
    """, (user_id, session["dietitian_id"]))
    conn.commit()
    messages = conn.execute("""
        SELECT * FROM chat_messages WHERE user_id=? AND dietitian_id=? ORDER BY timestamp
    """, (user_id, session["dietitian_id"])).fetchall()
    conn.close()
    return render_template("dietitian/messages.html", messages=messages, user_id=user_id)

@app.route("/dietitian/send_reply", methods=["POST"])
def dietitian_reply():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    user_id = request.form["user_id"]
    message = request.form["message"]
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO chat_messages (user_id, dietitian_id, sender, message) VALUES (?, ?, ?, ?)
    """, (user_id, session["dietitian_id"], "dietitian", message))
    conn.commit()
    dietitian = conn.execute("SELECT name FROM dietitians WHERE dietitian_id=?", (session["dietitian_id"],)).fetchone()
    conn.close()
    push_notification(user_id, f"{dietitian['name']} sent you a message.", notif_type="dietitian", link="/chat")
    return redirect(f"/dietitian/messages/{user_id}")


# ---------------------------------------------------
# DIETITIAN — MISSING ROUTES (Bug Fix)
# ---------------------------------------------------

@app.route("/dietitian/profile")
def dietitian_profile():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    dietitian = conn.execute("SELECT * FROM dietitians WHERE dietitian_id=?",
                             (session["dietitian_id"],)).fetchone()
    conn.close()
    return render_template("dietitian/profile.html", dietitian=dietitian)

@app.route("/dietitian/update_profile", methods=["POST"])
def dietitian_update_profile():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    bio = request.form.get("bio", "").strip()
    specialization = request.form.get("specialization", "").strip()
    experience_years = request.form.get("experience_years", "0").strip()
    conn = get_db_connection()
    photo = request.files.get("photo")
    if photo and photo.filename:
        filename = secure_filename(photo.filename)
        filepath = os.path.join("static", "uploads", filename)
        photo.save(filepath)
        conn.execute("UPDATE dietitians SET photo=? WHERE dietitian_id=?",
                     (filename, session["dietitian_id"]))
    conn.execute("""UPDATE dietitians SET bio=?, specialization=?, experience_years=?
                    WHERE dietitian_id=?""",
                 (bio, specialization, experience_years, session["dietitian_id"]))
    conn.commit()
    conn.close()
    return redirect("/dietitian/profile")


@app.route("/dietitian/recipes")
def dietitian_recipes():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")

    did = session["dietitian_id"]
    conn = get_db_connection()

    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()

    query = """
        SELECT r.*, u.full_name as user_name,
               d.name as verified_by_name
        FROM recipes r
        LEFT JOIN users u ON r.user_id = u.id
        LEFT JOIN dietitians d ON r.verified_by = d.dietitian_id
    """

    # Only show recipes added by users (not by dietitians)
    conditions = ["r.user_id IS NOT NULL", "COALESCE(r.added_by_dietitian, 0) = 0"]
    params = []

    if search:
        conditions.append("(r.name LIKE ? OR r.ingredients LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]

    if status_filter and status_filter != "all":
        conditions.append("r.verification_status = ?")
        params.append(status_filter)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY r.created_at DESC"

    recipes = conn.execute(query, params).fetchall()

    recommended = conn.execute(
        "SELECT * FROM recipes WHERE is_recommended=1 AND status='active' ORDER BY recommended_at DESC LIMIT 5"
    ).fetchall()

    trend_labels = []
    trend_data = []

    # ✅ ADD COUNTS BEFORE closing connection — user-submitted recipes only
    _user_only = "WHERE user_id IS NOT NULL AND COALESCE(added_by_dietitian, 0) = 0"
    total = conn.execute(f"SELECT COUNT(*) FROM recipes {_user_only}").fetchone()[0]
    pending = conn.execute(f"SELECT COUNT(*) FROM recipes {_user_only} AND verification_status='pending'").fetchone()[0]
    verified = conn.execute(f"SELECT COUNT(*) FROM recipes {_user_only} AND verification_status='verified'").fetchone()[0]
    flagged = conn.execute(f"SELECT SUM(dietitian_flag_count) FROM recipes {_user_only}").fetchone()[0] or 0

    counts = {
        "total": total,
        "pending": pending,
        "verified": verified,
        "flagged": flagged
    }

    conn.close()   # ✅ NOW correct position

    return render_template(
        "dietitian/recipe_verification.html",
        recipes=recipes,
        recommended=recommended,
        trend_labels=trend_labels,
        trend_data=trend_data,
        counts=counts,
        mode="view"
    )
       
@app.route("/dietitian/recipes/<int:recipe_id>")
def dietitian_recipe_detail(recipe_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    recipe = conn.execute("""
        SELECT r.*, u.full_name as user_name, u.email as user_email,
               d.name as verified_by_name
        FROM recipes r
        LEFT JOIN users u ON r.user_id = u.id
        LEFT JOIN dietitians d ON r.verified_by = d.dietitian_id
        WHERE r.id = ? AND r.user_id IS NOT NULL AND COALESCE(r.added_by_dietitian, 0) = 0
    """, (recipe_id,)).fetchone()
    conn.close()
    if not recipe:
        return redirect("/dietitian/recipes")
    # Parse instructions into numbered steps
    import re
    steps = []
    if recipe["instructions"]:
        raw = re.split(r'\d+\.\s*', recipe["instructions"])
        steps = [s.strip() for s in raw if s.strip()]
    # Parse ingredients into a list
    ingredients = []
    if recipe["ingredients"]:
        ingredients = [i.strip() for i in recipe["ingredients"].split(",") if i.strip()]
    return render_template(
        "dietitian/dietitian_recipe_view.html",
        recipe=recipe,
        steps=steps,
        ingredients=ingredients
    )

@app.route("/dietitian/recipes/verify/<int:recipe_id>", methods=["POST"])
def dietitian_verify_recipe(recipe_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    did = session["dietitian_id"]
    action = request.form.get("action", "verify")
    note = request.form.get("dietitian_note", "").strip()
    expert_tag = request.form.get("expert_tag", "").strip()
    recommend = request.form.get("recommend", "0") == "1"
    conn = get_db_connection()
    if action == "not_recommended":
        conn.execute("""UPDATE recipes SET verification_status='not_recommended',
                        dietitian_note=?, verified_by=?, verified_at=datetime('now')
                        WHERE id=?""", (note, did, recipe_id))
    else:
        conn.execute("""UPDATE recipes SET verification_status='verified', is_verified=1,
                        dietitian_note=?, verified_by=?, verified_at=datetime('now'),
                        status='active',
                        is_recommended=?, expert_tag=?,
                        recommended_by=CASE WHEN ? THEN ? ELSE recommended_by END,
                        recommended_at=CASE WHEN ? THEN datetime('now') ELSE recommended_at END
                        WHERE id=?""",
                     (note, did, int(recommend), expert_tag,
                      int(recommend), did, int(recommend), recipe_id))
    recipe = conn.execute("SELECT user_id, name FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    conn.commit()
    if recipe:
        msg = f'Your recipe "{recipe["name"]}" has been reviewed by a dietitian.'
        push_notification(recipe["user_id"], msg, notif_type="approval", link=f"/recipe/{recipe_id}")
    conn.close()
    return redirect(request.referrer or "/dietitian/recipes")

@app.route("/dietitian/recipes/edit/<int:recipe_id>", methods=["POST"])
def dietitian_edit_user_recipe(recipe_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    calories    = request.form.get("calories", "").strip()
    protein     = request.form.get("protein", "").strip()
    carbs       = request.form.get("carbs", "").strip()
    fat         = request.form.get("fat", "").strip()
    ingredients = request.form.get("ingredients", "").strip()
    instructions = request.form.get("instructions", "").strip()
    conn.execute("""
        UPDATE recipes
        SET calories=?, protein=?, carbs=?, fat=?, ingredients=?, instructions=?
        WHERE id=? AND user_id IS NOT NULL AND COALESCE(added_by_dietitian,0)=0
    """, (calories or None, protein or None, carbs or None, fat or None,
          ingredients, instructions, recipe_id))
    conn.commit()
    conn.close()
    return redirect(f"/dietitian/recipes/{recipe_id}")

@app.route("/dietitian/flag-recipe/<int:recipe_id>", methods=["POST"])
def dietitian_flag_recipe(recipe_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    conn.execute("""UPDATE recipes SET dietitian_flag_count = COALESCE(dietitian_flag_count,0) + 1
                    WHERE id=?""", (recipe_id,))
    conn.commit()
    conn.close()
    return redirect(request.referrer or "/dietitian/recipes")

@app.route("/dietitian/unflag-recipe/<int:recipe_id>", methods=["POST"])
def dietitian_unflag_recipe(recipe_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    conn.execute("UPDATE recipes SET dietitian_flag_count=0 WHERE id=?", (recipe_id,))
    conn.commit()
    conn.close()
    return redirect(request.referrer or "/dietitian/recipes")

@app.route("/dietitian/recommended")
def dietitian_recommended():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    recipes = conn.execute("""
        SELECT r.*, u.full_name as user_name
        FROM recipes r LEFT JOIN users u ON r.user_id = u.id
        WHERE r.status = 'active' ORDER BY r.is_recommended DESC, r.created_at DESC
    """).fetchall()
    conn.close()
    return render_template("dietitian/recommended_recipes.html", recipes=recipes)

@app.route("/dietitian/my-recipes")
def dietitian_my_recipes():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    did = session["dietitian_id"]
    conn = get_db_connection()
    recipes = conn.execute("""
        SELECT * FROM recipes WHERE added_by_dietitian=?
        ORDER BY created_at DESC
    """, (did,)).fetchall()
    conn.close()
    return render_template("dietitian/my_recipes.html", recipes=recipes, search="")

@app.route("/dietitian/add-recipe", methods=["GET", "POST"])
def dietitian_add_recipe():
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        ingredients = request.form.get("ingredients", "").strip()
        calories = request.form.get("calories", 0)
        protein = request.form.get("protein", 0)
        carbs = request.form.get("carbs", 0)
        fat = request.form.get("fat", 0)
        video_url = request.form.get("video_url", "").strip()
        instructions = request.form.get("instructions", "").strip()
        expert_tag = request.form.get("expert_tag", "").strip()
        did = session["dietitian_id"]
        conn = get_db_connection()
        conn.execute("""
            INSERT INTO recipes
                (name, description, ingredients, calories, protein, carbs, fat,
                 video_url, instructions, status, is_verified, verification_status,
                 added_by_dietitian, expert_tag)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, 'verified', ?, ?)
        """, (name, description, ingredients, calories, protein, carbs, fat,
               video_url, instructions, did, expert_tag))
        conn.commit()
        conn.close()
        return redirect("/dietitian/my-recipes")
    return render_template("dietitian/add_recipe.html")

@app.route("/dietitian/edit-recipe/<int:recipe_id>", methods=["GET", "POST"])
def dietitian_edit_recipe(recipe_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        ingredients = request.form.get("ingredients", "").strip()
        calories = request.form.get("calories", 0)
        protein = request.form.get("protein", 0)
        carbs = request.form.get("carbs", 0)
        fat = request.form.get("fat", 0)
        video_url = request.form.get("video_url", "").strip()
        instructions = request.form.get("instructions", "").strip()
        expert_tag = request.form.get("expert_tag", "").strip()
        conn.execute("""UPDATE recipes SET name=?, description=?, ingredients=?,
                        calories=?, protein=?, carbs=?, fat=?,
                        video_url=?, instructions=?, expert_tag=?
                        WHERE id=? AND added_by_dietitian=?""",
                     (name, description, ingredients, calories, protein, carbs, fat,
                      video_url, instructions, expert_tag, recipe_id, session["dietitian_id"]))
        conn.commit()
        conn.close()
        return redirect("/dietitian/my-recipes")
    recipe = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    conn.close()
    if not recipe:
        return redirect("/dietitian/my-recipes")
    return render_template("dietitian/add_recipe.html", recipe=recipe)

@app.route("/dietitian/edit-nutrition/<int:meal_id>", methods=["POST"])
def dietitian_edit_nutrition(meal_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    calories = request.form.get("calories", 0)
    protein = request.form.get("protein", 0)
    carbs = request.form.get("carbs", 0)
    fat = request.form.get("fat", 0)
    conn = get_db_connection()
    # Only update meals belonging to this dietitian's clients
    conn.execute("""
        UPDATE meals SET calories=?, protein=?, carbs=?, fat=?
        WHERE id=? AND user_id IN (
            SELECT user_id FROM user_dietitian WHERE dietitian_id=?
        )
    """, (calories, protein, carbs, fat, meal_id, session["dietitian_id"]))
    conn.commit()
    conn.close()
    return redirect(request.referrer or "/dietitian/meal-reviews")

@app.route("/dietitian/review-meal/<int:meal_id>")
def dietitian_review_meal(meal_id):
    if "dietitian_id" not in session:
        return redirect("/dietitian/login")
    conn = get_db_connection()
    meal = conn.execute("""
        SELECT m.*, u.full_name FROM meals m
        JOIN users u ON m.user_id = u.id
        WHERE m.id=?
    """, (meal_id,)).fetchone()
    conn.close()
    if not meal:
        return redirect("/dietitian/meal-reviews")
    return render_template("dietitian/review_meal.html", meal=meal)

# ---------------------------------------------------
# USER DASHBOARD & PROFILE
# ---------------------------------------------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")
    user_id = session["user_id"]
    today = datetime.now().strftime("%Y-%m-%d")
    # Recommendation from ingredient input
    ingredients_input = request.args.get("ingredients", "")
    recommended_meals = []
    if ingredients_input:
        user_items = [i.strip().lower() for i in ingredients_input.split(",") if i.strip()]
        scored = []
        conn_temp = get_db_connection()
        all_recipes = conn_temp.execute(
            "SELECT id, name, description, calories, protein, carbs, fat, ingredients FROM recipes WHERE status='active'"
        ).fetchall()
        conn_temp.close()
        for recipe in all_recipes:
            # Build a combined searchable text from name + ingredients + description
            combined = " ".join([
                (recipe["name"] or ""),
                (recipe["ingredients"] or ""),
                (recipe["description"] or "")
            ]).lower()
            # Count how many user ingredients appear anywhere in the combined text
            match_count = sum(1 for item in user_items if item in combined)
            if match_count > 0:
                scored.append((match_count, dict(recipe)))
        scored.sort(reverse=True, key=lambda x: x[0])
        recommended_meals = [r[1] for r in scored]
        # If still no matches, return all active recipes as fallback suggestions
        if not recommended_meals:
            conn_temp2 = get_db_connection()
            fallback = conn_temp2.execute(
                "SELECT id, name, description, calories, protein, carbs, fat, ingredients FROM recipes WHERE status='active' ORDER BY id DESC LIMIT 6"
            ).fetchall()
            conn_temp2.close()
            recommended_meals = [dict(r) for r in fallback]
    conn = get_db_connection()
    user = conn.execute("SELECT age, gender, height, weight, activity_level, water_goal FROM users WHERE id=?",
                        (user_id,)).fetchone()
    water_goal = user["water_goal"] if user and user["water_goal"] else 4.0
    profile_incomplete = not (user and user["age"] and user["gender"] and user["height"] and user["weight"] and user["activity_level"])
    bmi = bmi_category = None
    calorie_goal = 2000
    if not profile_incomplete:
        height_m = user["height"] / 100
        weight = user["weight"]
        age = user["age"]
        gender = user["gender"]
        act = user["activity_level"]
        bmi = round(weight / (height_m ** 2), 2)
        if bmi < 18.5: bmi_category = "Underweight"
        elif bmi < 25: bmi_category = "Normal"
        elif bmi < 30: bmi_category = "Overweight"
        else: bmi_category = "Obese"
        bmr = (10*weight + 6.25*user["height"] - 5*age + 5) if gender == "Male" else (10*weight + 6.25*user["height"] - 5*age - 161)
        multiplier = {"Sedentary": 1.2, "Moderate": 1.55, "Active": 1.725}.get(act, 1.2)
        calorie_goal = round(bmr * multiplier)
    water = conn.execute("SELECT IFNULL(SUM(liters),0) FROM water_logs WHERE user_id=? AND DATE(logged_at)=DATE('now')",
                         (user_id,)).fetchone()[0]
    totals = conn.execute("""
        SELECT IFNULL(SUM(calories),0) as calories, IFNULL(SUM(protein),0) as protein,
               IFNULL(SUM(carbs),0) as carbs, IFNULL(SUM(fat),0) as fat
        FROM meals WHERE user_id=? AND date=?
    """, (user_id, today)).fetchone()
    PROTEIN_GOAL, CARBS_GOAL, FAT_GOAL = 75, 220, 70
    protein_pct = min(100, round((totals["protein"] / PROTEIN_GOAL) * 100))
    carbs_pct = min(100, round((totals["carbs"] / CARBS_GOAL) * 100))
    fat_pct = min(100, round((totals["fat"] / FAT_GOAL) * 100))
    score = 0
    if totals["calories"] > 0:
        if totals["calories"] <= calorie_goal: score += 50
        if totals["protein"] >= 50: score += 30
        if totals["carbs"] >= 150: score += 20
    recent_meals = conn.execute("""
        SELECT name, calories, date, time FROM meals WHERE user_id=?
        ORDER BY date DESC, time DESC LIMIT 5
    """, (user_id,)).fetchall()
    trend_query = conn.execute("""
        SELECT date, SUM(calories) as total FROM meals WHERE user_id=? AND date >= date('now','-6 days')
        GROUP BY date ORDER BY date
    """, (user_id,)).fetchall()
    trend_labels = [row["date"] for row in trend_query]
    trend_data = [row["total"] for row in trend_query]
    # Streak
    meal_dates = conn.execute("SELECT DISTINCT date FROM meals WHERE user_id=? ORDER BY date DESC",
                              (user_id,)).fetchall()
    streak = 0
    check_date = date.today()
    for row in meal_dates:
        row_date = date.fromisoformat(row["date"])
        if row_date == check_date:
            streak += 1
            check_date -= timedelta(days=1)
        elif row_date < check_date:
            # FIX: a date older than expected means there's a gap — stop counting.
            break
    # Deficiency alerts
    week_avg = conn.execute("""
        SELECT ROUND(AVG(calories),0) as avg_cal, ROUND(AVG(protein),0) as avg_prot,
               ROUND(AVG(carbs),0) as avg_carbs, ROUND(AVG(fat),0) as avg_fat,
               COUNT(DISTINCT date) as days_logged
        FROM meals WHERE user_id=? AND date >= date('now','-6 days')
    """, (user_id,)).fetchone()
    deficiency_alerts = []
    if week_avg and week_avg["days_logged"] and week_avg["days_logged"] >= 3:
        if (week_avg["avg_prot"] or 0) < 40:
            deficiency_alerts.append({"type": "warning", "macro": "Protein",
                "msg": f"Your 7-day protein avg is {int(week_avg['avg_prot'] or 0)}g — below the 40g minimum.",
                "icon": "dumbbell"})
        if (week_avg["avg_cal"] or 0) < 1200:
            deficiency_alerts.append({"type": "danger", "macro": "Calories",
                "msg": f"You're averaging only {int(week_avg['avg_cal'] or 0)} kcal/day — dangerously low.",
                "icon": "fire"})
    fridge_items = conn.execute("SELECT id, item_name FROM fridge_items WHERE user_id=?", (user_id,)).fetchall()
    weight_logs = conn.execute("""
        SELECT weight, logged_at FROM weight_logs WHERE user_id=? ORDER BY logged_at DESC LIMIT 10
    """, (user_id,)).fetchall()
    weight_logs = list(reversed(weight_logs))
    # Expert recommended recipes (top 5)
    expert_recommended = conn.execute("""
        SELECT id, name, calories, protein, carbs, fat, description, expert_tag
        FROM recipes WHERE is_recommended = 1 AND status = 'active'
        ORDER BY recommended_at DESC LIMIT 5
    """).fetchall()
    conn.close()
    return render_template("user/dashboard.html",
        name=session["user_name"], calories=totals["calories"], protein=totals["protein"],
        carbs=totals["carbs"], fat=totals["fat"], daily_goal=calorie_goal,
        recent_meals=recent_meals, diet_score=score,
        protein_pct=protein_pct, carbs_pct=carbs_pct, fat_pct=fat_pct,
        water=water, water_goal=water_goal, ingredients_input=ingredients_input,
        recommended_meals=recommended_meals,
        profile_incomplete=profile_incomplete, user=user, bmi=bmi, bmi_category=bmi_category,
        streak=streak, deficiency_alerts=deficiency_alerts, fridge_items=fridge_items,
        trend_labels=trend_labels, trend_data=trend_data, weight_logs=weight_logs,
        expert_recommended=expert_recommended)

@app.route("/profile")
def profile():
    if "user_id" not in session:
        return redirect("/login")
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    bmi = None
    if user["height"] and user["weight"]:
        height_m = user["height"] / 100
        bmi = round(user["weight"] / (height_m ** 2), 2)
    return render_template("user/profile.html", user=user, bmi=bmi)

@app.route("/update_account", methods=["POST"])
def update_account():
    if "user_id" not in session:
        return redirect("/login")
    full_name = request.form.get("full_name", "")
    age = request.form.get("age")
    gender = request.form.get("gender")
    height = request.form.get("height")
    weight = request.form.get("weight")
    activity = request.form.get("activity_level")
    goal_type = request.form.get("goal_type")
    target_wt = request.form.get("target_weight")
    diet_pref = request.form.get("diet_preference")
    allergies = request.form.get("allergies")
    medical = request.form.get("medical_conditions")
    water_goal = request.form.get("water_goal")
    macro_pref = request.form.get("macro_preference")
    conn = get_db_connection()
    # Profile image upload
    image = request.files.get("profile_image")
    if image and image.filename:
        filename = secure_filename(image.filename)
        filepath = os.path.join("static/profile_images", filename)
        image.save(filepath)
        conn.execute("UPDATE users SET profile_image=? WHERE id=?", (filepath, session["user_id"]))
    conn.execute("""
        UPDATE users SET full_name=?, age=?, gender=?, height=?, weight=?,
               activity_level=?, goal_type=?, target_weight=?,
               diet_preference=?, allergies=?, medical_conditions=?,
               water_goal=?, macro_preference=?
        WHERE id=?
    """, (full_name, age, gender, height, weight, activity, goal_type, target_wt,
          diet_pref, allergies, medical, water_goal, macro_pref, session["user_id"]))
    # FIX: only insert a weight log if the weight field was submitted AND differs
    # from the last recorded weight — prevents duplicate entries on every profile save.
    if weight:
        last = conn.execute(
            "SELECT weight FROM weight_logs WHERE user_id=? ORDER BY logged_at DESC LIMIT 1",
            (session["user_id"],)
        ).fetchone()
        if last is None or abs(float(last["weight"]) - float(weight)) > 0.01:
            conn.execute("INSERT INTO weight_logs (user_id, weight) VALUES (?, ?)",
                         (session["user_id"], float(weight)))
    conn.commit()
    conn.close()
    # FIX: keep the session name in sync so the navbar reflects the new name immediately
    if full_name:
        session["user_name"] = full_name
    return redirect("/profile")

@app.route("/update_profile", methods=["POST"])
def update_profile():
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db_connection()
    conn.execute("""
        UPDATE users SET age=?, gender=?, height=?, weight=?, activity_level=?
        WHERE id=?
    """, (request.form["age"], request.form["gender"], request.form["height"],
          request.form["weight"], request.form["activity_level"], session["user_id"]))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))

@app.route("/security")
def security():
    if "user_id" not in session:
        return redirect("/login")
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    return render_template("user/security.html", user=user)

@app.route("/change_password", methods=["POST"])
def change_password():
    if "user_id" not in session:
        return redirect("/login")
    current_pw = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    confirm_pw = request.form.get("confirm_password", "")
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if not check_password_hash(user["password"], current_pw):
        conn.close()
        return render_template("user/security.html", user=user, error="Current password is incorrect")
    if new_pw != confirm_pw:
        conn.close()
        return render_template("user/security.html", user=user, error="New passwords do not match")
    if len(new_pw) < 8:
        conn.close()
        return render_template("user/security.html", user=user, error="Password must be at least 8 characters")
    conn.execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(new_pw), session["user_id"]))
    conn.commit()
    conn.close()
    return render_template("user/security.html", user=user, success="Password changed successfully")

@app.route("/delete_account", methods=["POST"])
def delete_account():
    if "user_id" not in session:
        return redirect("/login")
    conn = get_db_connection()
    conn.execute("DELETE FROM users WHERE id=?", (session["user_id"],))
    conn.commit()
    conn.close()
    session.clear()
    return redirect("/login")

# ---------------------------------------------------
# NOTIFICATIONS
# ---------------------------------------------------
@app.route("/notifications")
def notifications():
    if "user_id" not in session:
        return redirect("/login")
    filter_type = request.args.get("type", "all")
    conn = get_db_connection()
    # FIX: removed redundant ALTER TABLE here — init_database() already handles this migration
    query = "SELECT id, message, type, is_read, created_at, COALESCE(link,'') as link FROM notifications WHERE user_id=?"
    params = [session["user_id"]]
    if filter_type != "all":
        query += " AND type=?"
        params.append(filter_type)
    query += " ORDER BY created_at DESC LIMIT 100"
    rows = conn.execute(query, params).fetchall()
    unread_count = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
        (session["user_id"],)
    ).fetchone()[0]
    conn.close()
    return render_template("user/notifications.html",
        notifications=[dict(r) for r in rows],
        unread_count=unread_count,
        filter_type=filter_type)

@app.route("/mark_notification_read/<int:id>", methods=["POST"])
def mark_notification_read(id):
    if "user_id" not in session:
        return "", 401
    conn = get_db_connection()
    conn.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (id, session["user_id"]))
    conn.commit()
    conn.close()
    return "", 204

@app.route("/mark_all_notifications_read", methods=["POST"])
def mark_all_notifications_read():
    if "user_id" not in session:
        return "", 401
    conn = get_db_connection()
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (session["user_id"],))
    conn.commit()
    conn.close()
    return "", 204

@app.route("/delete_notification/<int:id>", methods=["POST"])
def delete_notification(id):
    if "user_id" not in session:
        return "", 401
    conn = get_db_connection()
    conn.execute("DELETE FROM notifications WHERE id=? AND user_id=?", (id, session["user_id"]))
    conn.commit()
    conn.close()
    return "", 204

@app.route("/notifications/unread_count")
def notifications_unread_count():
    if "user_id" not in session:
        return jsonify({"count": 0})
    conn = get_db_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
        (session["user_id"],)
    ).fetchone()[0]
    conn.close()
    return jsonify({"count": count})

# ---------------------------------------------------
# RECIPES (USER)
# ---------------------------------------------------
@app.route("/recipes", methods=["GET"])
def recipes():
    if "user_id" not in session:
        return redirect(url_for("login"))
    diet = request.args.get("diet", "")
    meal_type = request.args.get("meal_type", "")
    cal_min = request.args.get("cal_min", "")
    cal_max = request.args.get("cal_max", "")
    search = request.args.get("search", "").strip().lower()
    NON_VEG = ["chicken","beef","salmon","tuna","shrimp","prawn","turkey",
               "pork","lamb","cod","tilapia","bacon","ham","crab","duck",
               "sausage","pepperoni","mussel","scallop","anchov","fish"]
    ANIMAL = ["egg","milk","butter","yogurt","cream","ghee","cheese",
              "honey","paneer","whey","mozzarella","ricotta","parmesan"]
    MEAL_KW = {
        "breakfast": ["breakfast","brunch","oat","smoothie","pancake","cereal","porridge","toast","muesli"],
        "lunch": ["lunch","salad","sandwich","wrap","bowl","soup","dal","roti"],
        "dinner": ["dinner","stir-fry","stir fry","curry","roast","grill","bake","casserole","pasta","noodle","biryani"],
        "snack": ["snack","bar","bite","shake","protein ball","dip"],
    }
    def get_diet_type(recipe):
        combined = ((recipe["ingredients"] or "") + " " + (recipe["description"] or "")).lower()
        if any(w in combined for w in NON_VEG): return "nonveg"
        if any(w in combined for w in ANIMAL): return "vegetarian"
        return "vegan"
    def get_meal_type(recipe):
        text = ((recipe["name"] or "") + " " + (recipe["description"] or "")).lower()
        for mtype, kws in MEAL_KW.items():
            if any(k in text for k in kws): return mtype
        return "lunch"
    try: cal_min_val = int(cal_min)
    except: cal_min_val = 0
    try: cal_max_val = int(cal_max)
    except: cal_max_val = 99999
    conn = get_db_connection()
    all_recipes = conn.execute("SELECT * FROM recipes WHERE status='active' ORDER BY id DESC").fetchall()
    conn.close()
    results = []
    for recipe in all_recipes:
        d = dict(recipe)
        d["diet_type"] = get_diet_type(recipe)
        d["meal_type"] = get_meal_type(recipe)
        if search and search not in (recipe["name"] or "").lower() and search not in (recipe["ingredients"] or "").lower():
            continue
        if diet == "veg" and d["diet_type"] not in ("vegan","vegetarian"): continue
        if diet == "vegan" and d["diet_type"] != "vegan": continue
        if diet == "nonveg" and d["diet_type"] != "nonveg": continue
        if meal_type and meal_type != "all" and d["meal_type"] != meal_type: continue
        cal = int(recipe["calories"] or 0)
        if cal_min_val and cal < cal_min_val: continue
        if cal_max_val < 99999 and cal > cal_max_val: continue
        results.append(d)
    return render_template("user/recipes.html",
        recipes=results, active_diet=diet, active_meal_type=meal_type,
        cal_min=cal_min, cal_max=cal_max, search=search, total_count=len(results))

@app.route("/recipe/<int:recipe_id>")
def recipe_detail(recipe_id):
    conn = get_db_connection()
    try:
        recipe = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if recipe is None:
            return "Recipe not found", 404
        recipe_dict = dict(recipe)
        # Ingredients string → list
        recipe_dict["ingredients"] = [i.strip() for i in recipe_dict["ingredients"].replace(",", "\n").split("\n") if i.strip()]
        # Instructions handling (if exists)
        if "instructions" in recipe_dict and recipe_dict["instructions"]:
            steps = re.split(r'\d+\.\s*', recipe_dict["instructions"])
            recipe_dict["instructions"] = [s.strip() for s in steps if s.strip()]
        else:
            recipe_dict["instructions"] = []
        # Video link: use video_url if present, else empty
        recipe_dict["video_link"] = recipe_dict.get("video_url", "")
        # Image: use only the user-uploaded image; empty string = show placeholder in template
        recipe_dict["image_url"] = recipe_dict.get("image_url", "") or ""
        # Get similar recipes (simple category match)
        # FIX: reuse the same connection instead of opening a second one that could leak
        cat_keyword = (recipe_dict.get("category") or "").split("/")[0].strip()
        similar = conn.execute("""
            SELECT id, name, calories, protein, carbs, fat, image_url
            FROM recipes WHERE id != ? AND category LIKE ?
            ORDER BY ABS(calories - ?) ASC LIMIT 4
        """, (recipe_id, f"%{cat_keyword}%", recipe_dict["calories"])).fetchall()
        similar_recipes = [dict(r) for r in similar]
    finally:
        conn.close()
    return render_template("user/recipe_detail.html", recipe=recipe_dict, similar_recipes=similar_recipes)

@app.route("/add_recipe", methods=["GET", "POST"])
def add_recipe():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if request.method == "POST":
        name = request.form["name"]
        description = request.form.get("description", "")
        category = request.form.get("category") or "General"
        print("Category:", category)
        ingredients = request.form["ingredients"]
        calories = request.form.get("calories", 0)
        protein = request.form.get("protein", 0)
        carbs = request.form.get("carbs", 0)
        fat = request.form.get("fat", 0)
        video_url = request.form.get("video_link", "")
        instructions = request.form.get("instructions", "")
        # Handle recipe image upload
        image_url = ""
        recipe_img = request.files.get("recipe_image")
        if recipe_img and recipe_img.filename:
            import uuid
            from werkzeug.utils import secure_filename
            ext = os.path.splitext(secure_filename(recipe_img.filename))[1].lower()
            if ext in (".jpg", ".jpeg", ".png", ".webp"):
                img_filename = f"recipe_{uuid.uuid4().hex}{ext}"
                img_save_path = os.path.join(BASE_DIR, "static", "recipe_images", img_filename)
                recipe_img.save(img_save_path)
                image_url = f"/static/recipe_images/{img_filename}"
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO recipes
                (user_id, name, description, category, ingredients, calories, protein, carbs, fat, video_url, instructions, status, image_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session["user_id"], name, description, category, ingredients, calories, protein, carbs, fat, video_url, instructions, "pending", image_url))
        recipe_id = cursor.lastrowid
        # Save ingredients (optional)
        for ing in [i.strip() for i in ingredients.split(',') if i.strip()]:
            cursor.execute("INSERT INTO recipe_ingredients (recipe_id, ingredient) VALUES (?, ?)", (recipe_id, ing))
        conn.commit()
        conn.close()
        push_notification(session["user_id"], f'Your recipe "{name}" has been submitted and is awaiting dietitian review.',
                          notif_type="approval", link=f"/recipe/{recipe_id}")
        # FIX: redirect to recipes list — the new recipe has status='pending' so
        # recipe_detail (which filters status='active') would return 404.
        return redirect(url_for("recipes"))
    return render_template("user/add_recipe.html")

@app.route("/update_recipe_photo/<int:recipe_id>", methods=["POST"])
def update_recipe_photo(recipe_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db_connection()
    recipe = conn.execute("SELECT * FROM recipes WHERE id=? AND user_id=?",
                          (recipe_id, session["user_id"])).fetchone()
    if not recipe:
        conn.close()
        return "Not found or not your recipe", 404
    image_url = recipe["image_url"] or ""
    recipe_img = request.files.get("recipe_image")
    if recipe_img and recipe_img.filename:
        import uuid
        from werkzeug.utils import secure_filename
        ext = os.path.splitext(secure_filename(recipe_img.filename))[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            img_filename = f"recipe_{uuid.uuid4().hex}{ext}"
            img_save_path = os.path.join(BASE_DIR, "static", "recipe_images", img_filename)
            recipe_img.save(img_save_path)
            image_url = f"/static/recipe_images/{img_filename}"
    video_url = request.form.get("video_link", "").strip() or recipe["video_url"] or ""
    conn.execute("UPDATE recipes SET image_url=?, video_url=? WHERE id=? AND user_id=?",
                 (image_url, video_url, recipe_id, session["user_id"]))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("submitted_recipes"))


@app.route("/submitted_recipes")
def submitted_recipes():
    if "user_id" not in session:
        return redirect(url_for("login"))
    filter_status = request.args.get("filter", "all")
    conn = get_db_connection()
    if filter_status == "approved":
        recipes = conn.execute(
            "SELECT * FROM recipes WHERE user_id=? AND status IN ('approved','active') ORDER BY created_at DESC",
            (session["user_id"],)
        ).fetchall()
    elif filter_status in ("pending", "rejected"):
        recipes = conn.execute(
            "SELECT * FROM recipes WHERE user_id=? AND status=? ORDER BY created_at DESC",
            (session["user_id"], filter_status)
        ).fetchall()
    else:
        recipes = conn.execute(
            "SELECT * FROM recipes WHERE user_id=? ORDER BY created_at DESC",
            (session["user_id"],)
        ).fetchall()
    conn.close()
    return render_template("user/submitted_recipes.html", recipes=recipes, filter=filter_status)

@app.route("/save_recipe", methods=["POST"])
def save_recipe():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    recipe_id = data.get("recipe_id") or request.form.get("recipe_id")
    if not recipe_id:
        return jsonify({"error": "No recipe_id"}), 400
    conn = get_db_connection()
    existing = conn.execute("SELECT id FROM saved_recipes WHERE user_id=? AND recipe_id=?",
                            (session["user_id"], recipe_id)).fetchone()
    if existing:
        conn.execute("DELETE FROM saved_recipes WHERE user_id=? AND recipe_id=?",
                     (session["user_id"], recipe_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "unsaved"})
    try:
        conn.execute("INSERT INTO saved_recipes (user_id, recipe_id) VALUES (?,?)",
                     (session["user_id"], recipe_id))
        conn.commit()
    except Exception:
        pass
    conn.close()
    return jsonify({"status": "saved"})

@app.route("/saved_recipes")
def saved_recipes():
    if "user_id" not in session:
        return redirect("/login")
    conn = get_db_connection()
    recipes = conn.execute("""
        SELECT r.id, r.name, r.calories, r.protein, r.carbs, r.fat,
               r.description, r.ingredients, r.category, r.is_verified, r.is_recommended, sr.saved_at
        FROM saved_recipes sr JOIN recipes r ON sr.recipe_id = r.id
        WHERE sr.user_id = ? ORDER BY sr.saved_at DESC
    """, (session["user_id"],)).fetchall()
    conn.close()
    return render_template("user/saved_recipes.html", recipes=[dict(r) for r in recipes])

@app.route("/saved_recipe_ids")
def saved_recipe_ids():
    if "user_id" not in session:
        return jsonify([])
    conn = get_db_connection()
    rows = conn.execute("SELECT recipe_id FROM saved_recipes WHERE user_id=?",
                        (session["user_id"],)).fetchall()
    conn.close()
    return jsonify([r["recipe_id"] for r in rows])

# ---------------------------------------------------
# FOOD HISTORY
# ---------------------------------------------------
@app.route("/history")
def history():
    if "user_id" not in session:
        return redirect("/login")
    from datetime import datetime as dt_cls, timedelta
    selected_date = request.args.get("date", date.today().isoformat())
    # FIX: validate the date string — a bad ?date=abc would previously raise ValueError
    try:
        sel_dt = date.fromisoformat(selected_date)
    except ValueError:
        sel_dt = date.today()
        selected_date = sel_dt.isoformat()
    prev_date = (sel_dt - timedelta(days=1)).isoformat()
    next_date = (sel_dt + timedelta(days=1)).isoformat()
    now_time = dt_cls.now().strftime("%H:%M")
    today_str = date.today().isoformat()
    conn = get_db_connection()
    # FIX: removed redundant ALTER TABLE here — init_database() already handles these migrations
    logs = conn.execute("""
        SELECT id, name, calories, protein, carbs, fat, date, time,
               COALESCE(meal_type,'other') as meal_type, COALESCE(notes,'') as notes
        FROM meals WHERE user_id=? AND date=?
        ORDER BY time ASC
    """, (session["user_id"], selected_date)).fetchall()
    totals = conn.execute("""
        SELECT IFNULL(SUM(calories),0) as cal, IFNULL(SUM(protein),0) as prot,
               IFNULL(SUM(carbs),0) as carbs, IFNULL(SUM(fat),0) as fat
        FROM meals WHERE user_id=? AND date=?
    """, (session["user_id"], selected_date)).fetchone()
    # 7-day sparkline
    week_rows = conn.execute("""
        SELECT date, IFNULL(SUM(calories),0) as total
        FROM meals WHERE user_id=? AND date >= date(?,' -6 days') AND date <= ?
        GROUP BY date
    """, (session["user_id"], selected_date, selected_date)).fetchall()
    db_data = {r["date"]: int(r["total"]) for r in week_rows}
    week_labels, week_values = [], []
    for i in range(6, -1, -1):
        d = (sel_dt - timedelta(days=i)).isoformat()
        week_labels.append(d)
        week_values.append(db_data.get(d, 0))
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    calorie_goal = 2000
    if user and all([user["age"], user["gender"], user["height"], user["weight"], user["activity_level"]]):
        w, h, a, g = user["weight"], user["height"], user["age"], user["gender"]
        bmr = (10*w + 6.25*h - 5*a + 5) if g == "Male" else (10*w + 6.25*h - 5*a - 161)
        calorie_goal = round(bmr * {"Sedentary":1.2,"Moderate":1.55,"Active":1.725}.get(user["activity_level"], 1.2))
    meal_groups = {"breakfast":[], "lunch":[], "dinner":[], "snack":[], "other":[]}
    for log in logs:
        mt = log["meal_type"] if log["meal_type"] in meal_groups else "other"
        meal_groups[mt].append(dict(log))
    return render_template("user/history.html",
        logs=logs, meal_groups=meal_groups, totals=totals,
        calorie_goal=calorie_goal, selected_date=selected_date,
        today=today_str, prev_date=prev_date, next_date=next_date,
        now_time=now_time, week_labels=week_labels, week_values=week_values,
        user=user)

@app.route("/log_meal", methods=["POST"])
def log_meal():
    if "user_id" not in session:
        return redirect("/login")
    from datetime import datetime as dt_cls
    name = request.form.get("name", "").strip()
    calories = request.form.get("calories") or 0
    protein = request.form.get("protein") or 0
    carbs = request.form.get("carbs") or 0
    fat = request.form.get("fat") or 0
    meal_type = request.form.get("meal_type", "other")
    notes = request.form.get("notes", "").strip()
    log_date = request.form.get("date", date.today().isoformat())
    log_time = request.form.get("time", dt_cls.now().strftime("%H:%M:%S"))
    if not name:
        return redirect(f"/history?date={log_date}")
    conn = get_db_connection()
    # FIX: removed redundant ALTER TABLE here — init_database() handles it
    conn.execute("""
        INSERT INTO meals (user_id, name, calories, protein, carbs, fat, date, time, meal_type, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (session["user_id"], name, calories, protein, carbs, fat,
          log_date, log_time, meal_type, notes))
    conn.commit()
    conn.close()
    return redirect(f"/history?date={log_date}")

@app.route("/delete_meal/<int:meal_id>", methods=["POST"])
def delete_meal(meal_id):
    if "user_id" not in session:
        return redirect("/login")
    ref_date = request.form.get("date", date.today().isoformat())
    conn = get_db_connection()
    conn.execute("DELETE FROM meals WHERE id=? AND user_id=?", (meal_id, session["user_id"]))
    conn.commit()
    conn.close()
    return redirect(f"/history?date={ref_date}")

# ---------------------------------------------------
# WATER TRACKING
# ---------------------------------------------------
@app.route("/add_water", methods=["POST"])
def add_water():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db_connection()
    conn.execute("INSERT INTO water_logs (user_id, liters) VALUES (?, ?)", (session["user_id"], 0.25))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/save_water", methods=["POST"])
def save_water():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    liters = float(data.get("liters", 0.25))
    user_id = session["user_id"]
    conn = get_db_connection()
    conn.execute("DELETE FROM water_logs WHERE user_id=? AND DATE(logged_at)=DATE('now')", (user_id,))
    conn.execute("INSERT INTO water_logs (user_id, liters) VALUES (?, ?)", (user_id, liters))
    user = conn.execute("SELECT water_goal FROM users WHERE id=?", (user_id,)).fetchone()
    water_goal = user["water_goal"] if user and user["water_goal"] else 4.0
    existing = conn.execute(
        "SELECT id FROM notifications WHERE user_id=? AND type='water' AND DATE(created_at)=DATE('now')",
        (user_id,)
    ).fetchone()
    if not existing:
        if liters < water_goal:
            shortfall = round(water_goal - liters, 1)
            conn.execute(
                "INSERT INTO notifications (user_id, message, type, link) VALUES (?, ?, ?, ?)",
                (user_id, f"💧 You're {shortfall}L short of your daily water goal ({water_goal}L). Stay hydrated!", "water", "/dashboard")
            )
        else:
            conn.execute(
                "INSERT INTO notifications (user_id, message, type, link) VALUES (?, ?, ?, ?)",
                (user_id, f"🎉 Great job! You've reached your daily water goal of {water_goal}L. Keep it up!", "water", "/dashboard")
            )
    conn.commit()
    conn.close()
    return jsonify({"status": "saved"})


@app.route("/save_food", methods=["POST"])
def save_food():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    # FIX: use .get() with safe fallbacks; return 400 if required fields are missing
    food_name = data.get("food")
    if not food_name:
        return jsonify({"error": "Missing required field: food"}), 400
    try:
        calories = float(data.get("calories", 0))
        protein  = float(data.get("protein",  0))
        carbs    = float(data.get("carbs",    0))
        fat      = float(data.get("fat",      0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid numeric value in request"}), 400
    now = datetime.now()
    today = now.date().isoformat()
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO meals (user_id, name, calories, protein, carbs, fat, date, time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (session["user_id"], food_name, calories, protein, carbs, fat, today, now.strftime("%H:%M:%S")))
    conn.commit()
    total = conn.execute("""
        SELECT IFNULL(SUM(calories),0) FROM meals WHERE user_id=? AND date=?
    """, (session["user_id"], today)).fetchone()[0]
    user = conn.execute("""
        SELECT age, gender, height, weight, activity_level FROM users WHERE id=?
    """, (session["user_id"],)).fetchone()
    calorie_goal = 2000
    if user and all([user["age"], user["gender"], user["height"], user["weight"], user["activity_level"]]):
        bmr = (10*user["weight"] + 6.25*user["height"] - 5*user["age"] + 5) if user["gender"] == "Male" else (10*user["weight"] + 6.25*user["height"] - 5*user["age"] - 161)
        multiplier = {"Sedentary":1.2, "Moderate":1.55, "Active":1.725}.get(user["activity_level"], 1.2)
        calorie_goal = round(bmr * multiplier)
    if total > calorie_goal:
        existing = conn.execute("""
            SELECT id FROM notifications WHERE user_id=? AND type='calorie' AND DATE(created_at)=DATE('now')
        """, (session["user_id"],)).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)
            """, (session["user_id"], "You have exceeded your daily calorie goal.", "calorie"))
            conn.commit()
    conn.close()
    return jsonify({"status": "saved"})

# ---------------------------------------------------
# CHAT (User ↔ Dietitian)
# ---------------------------------------------------
@app.route("/dietitian")
def dietitian():
    if "user_id" not in session:
        return redirect("/login")
    conn = get_db_connection()
    result = conn.execute("SELECT dietitian_id FROM user_dietitian WHERE user_id=? AND payment_status='paid'",
                          (session["user_id"],)).fetchone()
    if result:
        conn.close()
        return redirect("/chat")
    dietitians = conn.execute("SELECT * FROM dietitians WHERE status='approved'").fetchall()
    conn.close()
    return render_template("dietitian/dietitian_list.html", dietitians=dietitians)

@app.route("/select_dietitian/<int:id>")
def select_dietitian(id):
    if "user_id" not in session:
        return redirect("/login")
    return render_template("user/payment.html", dietitian_id=id)

@app.route("/payment_success/<int:dietitian_id>")
def payment_success(dietitian_id):
    if "user_id" not in session:
        return redirect("/login")
    user_id = session["user_id"]
    conn = get_db_connection()
    existing = conn.execute("SELECT * FROM user_dietitian WHERE user_id=? AND dietitian_id=?",
                            (user_id, dietitian_id)).fetchone()
    if not existing:
        conn.execute("INSERT INTO user_dietitian (user_id, dietitian_id, payment_status) VALUES (?,?,?)",
                     (user_id, dietitian_id, "paid"))
        conn.commit()
    conn.close()
    return redirect("/chat")

@app.route("/chat")
def chat():
    if "user_id" not in session:
        return redirect("/login")
    conn = get_db_connection()
    dietitian = conn.execute("""
        SELECT d.dietitian_id, d.name FROM dietitians d
        JOIN user_dietitian ud ON d.dietitian_id = ud.dietitian_id
        WHERE ud.user_id=?
    """, (session["user_id"],)).fetchone()
    messages = conn.execute("""
        SELECT * FROM chat_messages WHERE user_id=? ORDER BY timestamp
    """, (session["user_id"],)).fetchall()
    conn.close()
    return render_template("user/chat.html", messages=messages, dietitian=dietitian)

@app.route("/send_message", methods=["POST"])
def send_message():
    if "user_id" not in session:
        return redirect("/login")
    dietitian_id = request.form["dietitian_id"]
    message = request.form["message"]
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO chat_messages (user_id, dietitian_id, sender, message) VALUES (?, ?, ?, ?)
    """, (session["user_id"], dietitian_id, "user", message))
    conn.commit()
    conn.close()
    return redirect("/chat")

# ---------------------------------------------------
# FEEDBACK & REVIEWS (USER)
# ---------------------------------------------------
@app.route("/feedback", methods=["GET", "POST"])
def user_feedback():
    if "user_id" not in session:
        return redirect("/login")
    success = error = None
    if request.method == "POST":
        rating = request.form.get("rating")
        category = request.form.get("category", "general")
        message = request.form.get("message", "").strip()
        if not rating or not message or len(message) < 10:
            error = "Please provide a rating and a message (at least 10 characters)."
        else:
            conn = get_db_connection()
            conn.execute("INSERT INTO user_feedback (user_id, rating, category, message) VALUES (?, ?, ?, ?)",
                         (session["user_id"], int(rating), category, message))
            conn.commit()
            conn.close()
            success = "Thank you! Your feedback has been submitted."
    conn = get_db_connection()
    my_feedback = conn.execute("""
        SELECT id, rating, category, message, status, admin_reply,
               strftime('%d %b %Y', submitted_at) as submitted_at
        FROM user_feedback WHERE user_id=? ORDER BY submitted_at DESC LIMIT 10
    """, (session["user_id"],)).fetchall()
    conn.close()
    return render_template("user/feedback.html", success=success, error=error, my_feedback=my_feedback)

# ---------------------------------------------------
# PROGRESS (USER)
# ---------------------------------------------------
@app.route("/progress")
def progress():
    if "user_id" not in session:
        return redirect("/login")
    from datetime import timedelta, datetime as dt_cls
    period = request.args.get("period", "weekly")
    user_id = session["user_id"]
    conn = get_db_connection()
    # Ensure notes column in weight_logs
    try:
        conn.execute("ALTER TABLE weight_logs ADD COLUMN notes TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    calorie_goal = 2000
    if user and all([user["age"], user["gender"], user["height"], user["weight"], user["activity_level"]]):
        w, h, a, g = user["weight"], user["height"], user["age"], user["gender"]
        bmr = (10*w + 6.25*h - 5*a + 5) if g == "Male" else (10*w + 6.25*h - 5*a - 161)
        calorie_goal = round(bmr * {"Sedentary":1.2,"Moderate":1.55,"Active":1.725}.get(user["activity_level"], 1.2))
    start_date = (date.today() - timedelta(days=6)).isoformat() if period == "weekly" else (date.today() - timedelta(days=29)).isoformat() if period == "monthly" else "2000-01-01"
    cal_rows = conn.execute("""
        SELECT date, IFNULL(SUM(calories),0) as cal, IFNULL(SUM(protein),0) as prot,
               IFNULL(SUM(carbs),0) as carbs, IFNULL(SUM(fat),0) as fat
        FROM meals WHERE user_id=? AND date>=? GROUP BY date ORDER BY date ASC
    """, (user_id, start_date)).fetchall()
    cal_by_date = {r["date"]: dict(r) for r in cal_rows}
    first_day = date.fromisoformat(cal_rows[0]["date"]) if (period=="all" and cal_rows) else date.fromisoformat(start_date)
    all_days = []
    for i in range((date.today() - first_day).days + 1):
        d = (first_day + timedelta(days=i)).isoformat()
        all_days.append(cal_by_date.get(d, {"date": d, "cal":0, "prot":0, "carbs":0, "fat":0}))
    cal_labels = [r["date"] for r in all_days]
    cal_values = [int(r["cal"]) for r in all_days]
    prot_values = [int(r["prot"]) for r in all_days]
    week_avg = conn.execute("""
        SELECT ROUND(AVG(dc),0) as avg_cal, ROUND(AVG(dp),0) as avg_prot,
               ROUND(AVG(dcarbs),0) as avg_carbs, COUNT(*) as days_logged
        FROM (SELECT SUM(calories) as dc, SUM(protein) as dp, SUM(carbs) as dcarbs
              FROM meals WHERE user_id=? AND date>=? GROUP BY date)
    """, (user_id, start_date)).fetchone()
    monthly_rows = conn.execute("""
        SELECT strftime('%Y-%W',date) as week_key,
               ROUND(AVG(dc),0) as avg_cal, SUM(dc) as total_cal
        FROM (SELECT date, SUM(calories) as dc FROM meals WHERE user_id=? AND date>=?
              GROUP BY date) GROUP BY week_key ORDER BY week_key
    """, (user_id, (date.today() - timedelta(days=89)).isoformat())).fetchall()
    weight_rows = conn.execute("""
        SELECT logged_at, weight, COALESCE(notes,'') as notes
        FROM weight_logs WHERE user_id=? ORDER BY logged_at ASC
    """, (user_id,)).fetchall()
    weight_labels = [r["logged_at"][:10] for r in weight_rows]
    weight_values = [float(r["weight"]) for r in weight_rows]
    current_weight = weight_values[-1] if weight_values else (user["weight"] if user else None)
    starting_weight = weight_values[0] if weight_values else current_weight
    target_weight = user["target_weight"] if user and user["target_weight"] else None
    weight_change = round(current_weight - starting_weight, 1) if (current_weight and starting_weight) else 0
    bmi = bmi_category = None
    if user and user["height"] and current_weight:
        h_m = user["height"] / 100
        bmi = round(current_weight / (h_m ** 2), 1)
        if bmi < 18.5: bmi_category = ("Underweight", "#3b82f6")
        elif bmi < 25: bmi_category = ("Normal", "#10b981")
        elif bmi < 30: bmi_category = ("Overweight", "#f59e0b")
        else: bmi_category = ("Obese", "#ef4444")
    streak = 0
    check = date.today()
    for row in conn.execute("SELECT DISTINCT date FROM meals WHERE user_id=? ORDER BY date DESC", (user_id,)).fetchall():
        if date.fromisoformat(row["date"]) == check:
            streak += 1
            check -= timedelta(days=1)
        elif date.fromisoformat(row["date"]) < check:
            # FIX: gap found — stop counting streak
            break
    conn.close()
    return render_template("user/progress.html",
        user=user, period=period, cal_labels=cal_labels, cal_values=cal_values,
        prot_values=prot_values, calorie_goal=calorie_goal, week_avg=week_avg,
        monthly_rows=[dict(r) for r in monthly_rows], weight_labels=weight_labels,
        weight_values=weight_values, current_weight=current_weight,
        starting_weight=starting_weight, target_weight=target_weight,
        weight_change=weight_change, bmi=bmi, bmi_category=bmi_category,
        streak=streak, goal_type=user["goal_type"] if user else None)

@app.route("/log_weight", methods=["POST"])
def log_weight():
    if "user_id" not in session:
        return redirect("/login")
    weight = request.form.get("weight", "").strip()
    notes = request.form.get("notes", "").strip()
    if not weight:
        return redirect("/progress")
    conn = get_db_connection()
    # FIX: removed redundant ALTER TABLE here — init_database() handles it
    conn.execute("INSERT INTO weight_logs (user_id, weight, notes) VALUES (?,?,?)",
                 (session["user_id"], float(weight), notes))
    conn.execute("UPDATE users SET weight=? WHERE id=?", (float(weight), session["user_id"]))
    conn.commit()
    conn.close()
    return redirect("/progress")

# ---------------------------------------------------
# SCHEDULED JOBS
# ---------------------------------------------------
def send_evening_water_reminders():
    """Runs at 8 PM every day. Notifies users who haven't met their water goal."""
    try:
        conn = get_db_connection()
        users = conn.execute("SELECT id, water_goal FROM users").fetchall()
        for user in users:
            user_id = user["id"]
            water_goal = user["water_goal"] if user["water_goal"] else 4.0
            total = conn.execute(
                "SELECT IFNULL(SUM(liters),0) FROM water_logs WHERE user_id=? AND DATE(logged_at)=DATE('now')",
                (user_id,)
            ).fetchone()[0]
            if total < water_goal:
                existing = conn.execute(
                    "SELECT id FROM notifications WHERE user_id=? AND type='water' AND DATE(created_at)=DATE('now')",
                    (user_id,)
                ).fetchone()
                if not existing:
                    shortfall = round(water_goal - total, 1)
                    conn.execute(
                        "INSERT INTO notifications (user_id, message, type, link) VALUES (?, ?, ?, ?)",
                        (user_id, f"💧 Evening reminder: You're {shortfall}L short of your {water_goal}L water goal today. Drink up!", "water", "/dashboard")
                    )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Scheduler] Water reminder error: {e}")

# ---------------------------------------------------
# RUN APP
# ---------------------------------------------------
if __name__ == "__main__":
    init_database()
    if SCHEDULER_AVAILABLE:
        scheduler = BackgroundScheduler()
        scheduler.add_job(send_evening_water_reminders, "cron", hour=20, minute=0)
        scheduler.start()
        print("[Scheduler] Evening water reminder scheduled at 8:00 PM daily.")
    app.run(debug=True)