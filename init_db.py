import sqlite3

conn = sqlite3.connect("nutrilens.db")
cursor = conn.cursor()

# ---------------- USERS TABLE ----------------
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")

# ---------------- MEALS TABLE ----------------
cursor.execute("""
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    calories REAL DEFAULT 0,
    protein REAL DEFAULT 0,
    carbs REAL DEFAULT 0,
    fat REAL DEFAULT 0,
    image TEXT,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
""")

# ---------------- GOALS TABLE ----------------
cursor.execute("""
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    daily_calorie_goal INTEGER DEFAULT 2000,
    protein_goal REAL DEFAULT 75,
    carbs_goal REAL DEFAULT 220,
    fat_goal REAL DEFAULT 70,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
""")

# ---------------- WATER LOGS TABLE ----------------
cursor.execute("""
CREATE TABLE IF NOT EXISTS water_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    liters REAL NOT NULL,
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
""")

# ---------------- ADMIN TABLE ----------------
cursor.execute("""
CREATE TABLE IF NOT EXISTS admin (
    admin_id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL
);
""")

# ---------------- DIETITIANS TABLE (UPDATED) ----------------
cursor.execute("""
CREATE TABLE IF NOT EXISTS dietitians (
    dietitian_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    license_number TEXT,
    specialization TEXT,
    experience_years INTEGER DEFAULT 0,
    bio TEXT,
    status TEXT DEFAULT 'pending',  -- pending, approved, rejected
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMP,
    approved_by INTEGER,  -- admin who approved
    rejection_reason TEXT
);
""")

# ---------------- NUTRITION MASTER TABLE ----------------
cursor.execute("""
CREATE TABLE IF NOT EXISTS nutrition_master (
    food_id INTEGER PRIMARY KEY AUTOINCREMENT,
    food_name TEXT UNIQUE NOT NULL,
    calories_100g REAL,
    protein_100g REAL,
    carbs_100g REAL,
    fat_100g REAL
);
""")

# -------- INSERT DEFAULT ADMIN (ONLY ONCE) --------
cursor.execute("""
INSERT OR IGNORE INTO admin (email, password)
VALUES ('nutrilensadmin@gmail.com', 'admin123')
""")


conn.commit()
conn.close()

print("✅ Database created successfully with all tables!")
print("📊 Tables created: users, meals, goals, water_logs, admin, dietitians, nutrition_master")
print("👤 Default admin: nutrilensadmin@gmail.com / admin123")
print("🥼 Sample dietitians added: 2 approved, 2 pending, 1 rejected")