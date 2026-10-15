"""NutriFit - backend ligero en Flask + SQLite para la PWA de nutrición y entrenamiento."""
import functools
import hmac
import math
import os
import sqlite3
import struct
import zlib
from datetime import date, datetime, timedelta

from flask import Flask, Response, g, jsonify, request, send_from_directory

import metabolic

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "nutrifit.db"))
# Protección opcional (muy recomendable si lo publicas en Internet):
#   APP_PASSWORD=tu_clave  APP_USER=tu_usuario
APP_USER = os.environ.get("APP_USER", "nutrifit")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

app = Flask(__name__, static_folder=None)

MEALS = ("desayuno", "almuerzo", "merienda", "cena", "suplementos")
LEVELS = ("principiante", "intermedio", "avanzado")
KINDS = ("compuesto", "aislamiento")
TOLERANCES = tuple(metabolic.TOLERANCES)          # baja / media / alta (tolerancia a estimulantes)
DEFAULT_SETTINGS = {"kcal": "2400", "protein": "150", "carbs": "280", "fat": "70", "level": "intermedio",
                    "tolerance": "media"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS foods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL, meal TEXT NOT NULL, name TEXT NOT NULL,
    grams REAL NOT NULL DEFAULT 0,
    kcal REAL NOT NULL, protein REAL NOT NULL, carbs REAL NOT NULL, fat REAL NOT NULL,
    carb_type TEXT NOT NULL DEFAULT 'SLOW',      -- FAST (IG alto) | SLOW (IG bajo / fibra)
    caffeine_mg REAL NOT NULL DEFAULT 0,
    timestamp TEXT                               -- AAAA-MM-DDTHH:MM de la ingesta
);
CREATE INDEX IF NOT EXISTS idx_foods_day ON foods(day);
CREATE TABLE IF NOT EXISTS routine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL,            -- 0 = lunes ... 6 = domingo
    muscle TEXT NOT NULL, name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'compuesto',
    sets INTEGER NOT NULL DEFAULT 3,
    target_reps INTEGER NOT NULL DEFAULT 10,
    target_rir INTEGER NOT NULL DEFAULT 2,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    exercise_id INTEGER NOT NULL REFERENCES routine(id) ON DELETE CASCADE,
    set_no INTEGER NOT NULL,
    kg REAL NOT NULL, reps INTEGER NOT NULL, rir INTEGER NOT NULL,
    nai_mod REAL NOT NULL DEFAULT 0,             -- modificador nutricional agudo (β·ΔNAI) vigente al guardar
    UNIQUE(day, exercise_id, set_no)
);
CREATE INDEX IF NOT EXISTS idx_sets_ex ON sets(exercise_id, day);
CREATE TABLE IF NOT EXISTS weights (day TEXT PRIMARY KEY, kg REAL NOT NULL);
"""

# Rutina de ejemplo (lunes / miércoles / viernes). Se puede editar desde la app.
SEED_ROUTINE = [
    (0, "Pecho", "Press banca con barra", "compuesto", 4, 8, 2),
    (0, "Hombro", "Press militar con mancuernas", "compuesto", 3, 10, 2),
    (0, "Pecho", "Aperturas en polea", "aislamiento", 3, 12, 2),
    (0, "Tríceps", "Extensión de tríceps en polea", "aislamiento", 3, 12, 2),
    (2, "Espalda", "Jalón al pecho", "compuesto", 4, 8, 2),
    (2, "Espalda", "Remo con barra", "compuesto", 3, 10, 2),
    (2, "Bíceps", "Curl con barra Z", "aislamiento", 3, 12, 2),
    (4, "Cuádriceps", "Sentadilla", "compuesto", 4, 8, 2),
    (4, "Femoral", "Peso muerto rumano", "compuesto", 3, 10, 2),
    (4, "Gemelo", "Elevación de gemelos", "aislamiento", 4, 15, 2),
]


# ----------------------------------------------------------------- base de datos
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def add_column(conn, table, column, ddl):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    add_column(conn, "foods", "carb_type", "TEXT NOT NULL DEFAULT 'SLOW'")
    add_column(conn, "foods", "caffeine_mg", "REAL NOT NULL DEFAULT 0")
    add_column(conn, "foods", "timestamp", "TEXT")
    add_column(conn, "sets", "nai_mod", "REAL NOT NULL DEFAULT 0")
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    if conn.execute("SELECT COUNT(*) FROM routine").fetchone()[0] == 0:
        pos = {}
        for wd, muscle, name, kind, sets, reps, rir in SEED_ROUTINE:
            pos[wd] = pos.get(wd, 0) + 1
            conn.execute(
                "INSERT INTO routine(weekday, muscle, name, kind, sets, target_reps, target_rir, position) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (wd, muscle, name, kind, sets, reps, rir, pos[wd]),
            )
    conn.commit()
    conn.close()


# ----------------------------------------------------------------- utilidades
def to_float(v, lo=0.0, hi=100000.0):
    try:
        x = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError("Número no válido")
    if math.isnan(x) or not (lo <= x <= hi):
        raise ValueError("Valor fuera de rango")
    return x


def to_int(v, lo=0, hi=1000):
    return int(round(to_float(v, lo, hi)))


def valid_day(s):
    try:
        datetime.strptime(str(s), "%Y-%m-%d")
    except ValueError:
        raise ValueError("Fecha no válida (use AAAA-MM-DD)")
    return str(s)


def text(v, field, maxlen=80):
    s = str(v or "").strip()
    if not s:
        raise ValueError(f"Falta el campo «{field}»")
    return s[:maxlen]


def e1rm(kg, reps, rir):
    """Epley (1985) incluyendo RIR: e1RM = peso × (1 + (reps + RIR) / 30)."""
    return kg * (1 + (reps + rir) / 30.0)


def row(r):
    return dict(r) if r is not None else None


def carb_type(v):
    v = str(v or "SLOW").upper()
    if v not in metabolic.CARB_TYPES:
        raise ValueError("Tipo de carbohidrato no válido (FAST o SLOW)")
    return v


def valid_at(s):
    """Hora de inicio de la sesión (HH:MM). Si falta, la hora actual del servidor."""
    s = str(s or "").strip()
    if not s:
        return datetime.now().strftime("%H:%M")
    try:
        return datetime.strptime(s, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError("Hora no válida (use HH:MM)")


def food_params(f, day, meal):
    """Campos de cinética comunes a alta individual y masiva: (carb_type, caffeine_mg, timestamp)."""
    return (
        carb_type(f.get("carb_type")),
        to_float(f.get("caffeine_mg", 0), 0, 2000),
        metabolic.norm_timestamp(f.get("timestamp"), day, meal),
    )


def body_weight(conn, day):
    r = conn.execute("SELECT kg FROM weights WHERE day<=? ORDER BY day DESC LIMIT 1", (day,)).fetchone()
    if r is None:
        r = conn.execute("SELECT kg FROM weights ORDER BY day DESC LIMIT 1").fetchone()
    return r["kg"] if r else 75.0


def nai_for(conn, day, at):
    """NAI de la sesión que empieza a las `at` del día `day` (considera también la víspera por si cruza medianoche)."""
    prev = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    foods = [dict(r) for r in conn.execute("SELECT * FROM foods WHERE day IN (?, ?)", (day, prev))]
    tol = conn.execute("SELECT value FROM settings WHERE key='tolerance'").fetchone()
    res = metabolic.compute_nai(foods, metabolic.session_start(day, at), body_weight(conn, day),
                                tol["value"] if tol else "media")
    res["at"] = at
    return res


@app.errorhandler(ValueError)
def handle_value_error(err):
    return jsonify(error=str(err)), 400


@app.before_request
def guard():
    if not APP_PASSWORD:
        return None
    auth = request.authorization
    if (
        auth
        and hmac.compare_digest(auth.username or "", APP_USER)
        and hmac.compare_digest(auth.password or "", APP_PASSWORD)
    ):
        return None
    return Response("Acceso restringido", 401, {"WWW-Authenticate": 'Basic realm="NutriFit"'})


# ----------------------------------------------------------------- ficheros PWA
def static_file(name, mimetype=None):
    resp = send_from_directory(BASE_DIR, name, mimetype=mimetype)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
@app.get("/index.html")
def index():
    return static_file("index.html")


@app.get("/manifest.json")
def manifest():
    return static_file("manifest.json", "application/manifest+json")


@app.get("/service-worker.js")
def service_worker():
    return static_file("service-worker.js", "application/javascript")


@functools.lru_cache(maxsize=4)
def make_icon(n):
    """PNG sin dependencias externas: anillo verde esmeralda sobre fondo oscuro (apto 'maskable')."""
    bg, fg = (11, 15, 13), (29, 245, 143)
    c = (n - 1) / 2.0
    r_out, r_in, r_dot = n * 0.30, n * 0.20, n * 0.075
    raw = bytearray()
    for y in range(n):
        raw.append(0)
        for x in range(n):
            d = math.hypot(x - c, y - c)
            outer = min(1.0, max(0.0, r_out - d + 0.5))
            inner = min(1.0, max(0.0, r_in - d + 0.5))
            dot = min(1.0, max(0.0, r_dot - d + 0.5))
            cov = max(outer * (1 - inner), dot)
            raw.extend(int(bg[i] + (fg[i] - bg[i]) * cov) for i in range(3))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


@app.get("/icon-<int:size>.png")
def icon(size):
    if size not in (192, 512):
        return Response(status=404)
    resp = Response(make_icon(size), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


# ----------------------------------------------------------------- ajustes
@app.get("/api/settings")
def get_settings():
    s = {r["key"]: r["value"] for r in db().execute("SELECT key, value FROM settings")}
    return jsonify(
        kcal=float(s["kcal"]), protein=float(s["protein"]),
        carbs=float(s["carbs"]), fat=float(s["fat"]), level=s["level"],
        tolerance=s.get("tolerance", "media"),
    )


@app.post("/api/settings")
def save_settings():
    data = request.get_json(force=True, silent=True) or {}
    updates = {}
    for key in ("kcal", "protein", "carbs", "fat"):
        if key in data:
            updates[key] = str(to_float(data[key], 1, 20000))
    if "level" in data:
        if data["level"] not in LEVELS:
            raise ValueError("Nivel no válido")
        updates["level"] = data["level"]
    if "tolerance" in data:
        if data["tolerance"] not in TOLERANCES:
            raise ValueError("Tolerancia no válida")
        updates["tolerance"] = data["tolerance"]
    for k, v in updates.items():
        db().execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    db().commit()
    return get_settings()


# ----------------------------------------------------------------- nutrición
@app.get("/api/foods")
def list_foods():
    day = valid_day(request.args.get("day", date.today().isoformat()))
    rows = db().execute("SELECT * FROM foods WHERE day=? ORDER BY id", (day,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/foods")
def add_food():
    d = request.get_json(force=True, silent=True) or {}
    meal = str(d.get("meal", "")).lower()
    if meal not in MEALS:
        raise ValueError("Comida no válida")
    day = valid_day(d.get("day"))
    cur = db().execute(
        "INSERT INTO foods(day, meal, name, grams, kcal, protein, carbs, fat, carb_type, caffeine_mg, timestamp) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            day, meal, text(d.get("name", "Alimento"), "nombre", 80),
            to_float(d.get("grams", 0), 0, 20000),
            to_float(d.get("kcal"), 0, 20000), to_float(d.get("protein"), 0, 2000),
            to_float(d.get("carbs"), 0, 3000), to_float(d.get("fat"), 0, 2000),
            *food_params(d, day, meal),
        ),
    )
    db().commit()
    return jsonify(row(db().execute("SELECT * FROM foods WHERE id=?", (cur.lastrowid,)).fetchone())), 201


@app.delete("/api/foods/<int:food_id>")
def delete_food(food_id):
    db().execute("DELETE FROM foods WHERE id=?", (food_id,))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/foods/history")
def foods_history():
    """Alimentos ya registrados (valores por 100 g) para reutilizarlos al buscar."""
    q = request.args.get("q", "").strip()
    rows = db().execute(
        "SELECT f.* FROM foods f JOIN ("
        "  SELECT MAX(id) mid FROM foods WHERE name LIKE ? AND grams > 0 GROUP BY lower(name)"
        ") m ON f.id = m.mid ORDER BY f.id DESC LIMIT 8",
        (f"%{q}%",),
    ).fetchall()
    out = []
    for r in rows:
        k = 100.0 / r["grams"]
        out.append({
            "name": r["name"], "kcal": round(r["kcal"] * k, 1), "protein": round(r["protein"] * k, 1),
            "carbs": round(r["carbs"] * k, 1), "fat": round(r["fat"] * k, 1),
            "carb_type": r["carb_type"], "caffeine_mg": round(r["caffeine_mg"] * k, 1),
        })
    return jsonify(out)


@app.get("/api/nutrition/weeks")
def nutrition_weeks():
    n = min(max(int(request.args.get("n", 8)), 1), 52)
    try:
        today = date.fromisoformat(request.args.get("today", ""))
    except ValueError:
        today = date.today()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=n - 1)
    weeks = {}
    for i in range(n):
        ws = (start + timedelta(weeks=i)).isoformat()
        weeks[ws] = {"week_start": ws, "days": 0, "kcal": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0,
                     "_w": []}
    rows = db().execute(
        "SELECT day, SUM(kcal) k, SUM(protein) p, SUM(carbs) c, SUM(fat) f FROM foods "
        "WHERE day >= ? GROUP BY day", (start.isoformat(),)
    ).fetchall()
    for r in rows:
        d = date.fromisoformat(r["day"])
        w = weeks.get((d - timedelta(days=d.weekday())).isoformat())
        if w:
            w["days"] += 1
            w["kcal"] += r["k"]; w["protein"] += r["p"]; w["carbs"] += r["c"]; w["fat"] += r["f"]
    for r in db().execute("SELECT day, kg FROM weights WHERE day >= ?", (start.isoformat(),)):
        d = date.fromisoformat(r["day"])
        w = weeks.get((d - timedelta(days=d.weekday())).isoformat())
        if w:
            w["_w"].append(r["kg"])
    out = []
    for w in weeks.values():
        days = w["days"]
        for k in ("kcal", "protein", "carbs", "fat"):
            w[k] = round(w[k] / days, 1) if days else 0
        ws_ = w.pop("_w")
        w["weight"] = round(sum(ws_) / len(ws_), 1) if ws_ else None
        out.append(w)
    return jsonify(out)


@app.get("/api/weights")
def list_weights():
    rows = db().execute("SELECT day, kg FROM weights ORDER BY day DESC LIMIT 120").fetchall()
    return jsonify([dict(r) for r in reversed(rows)])


@app.post("/api/weights")
def save_weight():
    d = request.get_json(force=True, silent=True) or {}
    db().execute(
        "INSERT INTO weights(day, kg) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET kg=excluded.kg",
        (valid_day(d.get("day")), to_float(d.get("kg"), 20, 400)),
    )
    db().commit()
    return jsonify(ok=True)


# ----------------------------------------------------------------- rutina
def routine_payload(d):
    kind = d.get("kind", "compuesto")
    if kind not in KINDS:
        raise ValueError("Tipo de ejercicio no válido")
    return {
        "weekday": to_int(d.get("weekday"), -1, 6),   # -1 = ejercicio extra (sin día fijo)
        "muscle": text(d.get("muscle"), "grupo muscular", 40),
        "name": text(d.get("name"), "ejercicio", 80),
        "kind": kind,
        "sets": to_int(d.get("sets", 3), 1, 12),
        "target_reps": to_int(d.get("target_reps", 10), 1, 50),
        "target_rir": to_int(d.get("target_rir", 2), 0, 6),
    }


def next_position(weekday):
    r = db().execute("SELECT COALESCE(MAX(position), 0) + 1 FROM routine WHERE weekday=?", (weekday,)).fetchone()
    return r[0]


@app.get("/api/routine")
def list_routine():
    rows = db().execute("SELECT * FROM routine ORDER BY weekday, position, id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/routine")
def add_exercise():
    p = routine_payload(request.get_json(force=True, silent=True) or {})
    cur = db().execute(
        "INSERT INTO routine(weekday, muscle, name, kind, sets, target_reps, target_rir, position) "
        "VALUES (:weekday,:muscle,:name,:kind,:sets,:target_reps,:target_rir,:position)",
        {**p, "position": next_position(p["weekday"])},
    )
    db().commit()
    return jsonify(row(db().execute("SELECT * FROM routine WHERE id=?", (cur.lastrowid,)).fetchone())), 201


@app.put("/api/routine/<int:ex_id>")
def update_exercise(ex_id):
    old = db().execute("SELECT * FROM routine WHERE id=?", (ex_id,)).fetchone()
    if old is None:
        return jsonify(error="Ejercicio no encontrado"), 404
    p = routine_payload(request.get_json(force=True, silent=True) or {})
    pos = old["position"] if p["weekday"] == old["weekday"] else next_position(p["weekday"])
    db().execute(
        "UPDATE routine SET weekday=:weekday, muscle=:muscle, name=:name, kind=:kind, sets=:sets, "
        "target_reps=:target_reps, target_rir=:target_rir, position=:position WHERE id=:id",
        {**p, "position": pos, "id": ex_id},
    )
    db().commit()
    return jsonify(ok=True)


@app.delete("/api/routine/<int:ex_id>")
def delete_exercise(ex_id):
    db().execute("DELETE FROM routine WHERE id=?", (ex_id,))  # borra también su historial (CASCADE)
    db().commit()
    return jsonify(ok=True)


@app.post("/api/routine/<int:ex_id>/move")
def move_exercise(ex_id):
    direction = -1 if (request.get_json(force=True, silent=True) or {}).get("dir", 0) < 0 else 1
    cur = db().execute("SELECT * FROM routine WHERE id=?", (ex_id,)).fetchone()
    if cur is None:
        return jsonify(error="Ejercicio no encontrado"), 404
    ids = [r["id"] for r in db().execute(
        "SELECT id FROM routine WHERE weekday=? ORDER BY position, id", (cur["weekday"],))]
    i = ids.index(ex_id)
    j = i + direction
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for pos, rid in enumerate(ids, 1):
            db().execute("UPDATE routine SET position=? WHERE id=?", (pos, rid))
        db().commit()
    return jsonify(ok=True)


# ----------------------------------------------------------------- disponibilidad nutricional
@app.get("/api/nai")
def get_nai():
    """NAI pre-entreno: ?day=AAAA-MM-DD&at=HH:MM (inicio de la sesión)."""
    day = valid_day(request.args.get("day", date.today().isoformat()))
    return jsonify(nai_for(db(), day, valid_at(request.args.get("at"))))


# ----------------------------------------------------------------- entrenamiento
@app.get("/api/workout")
def workout():
    """Ejercicios del día + series de hoy + series de la sesión de referencia (semana anterior)."""
    day = valid_day(request.args.get("day", date.today().isoformat()))
    d = date.fromisoformat(day)
    conn = db()
    out = []
    base = conn.execute("SELECT * FROM routine WHERE weekday=? ORDER BY position, id", (d.weekday(),)).fetchall()
    # Extras: ejercicios añadidos hoy fuera de la rutina (?extra=1,2) o que ya tienen series registradas hoy.
    wanted = {int(x) for x in request.args.get("extra", "").split(",") if x.strip().isdigit()}
    wanted |= {r[0] for r in conn.execute("SELECT DISTINCT exercise_id FROM sets WHERE day=?", (day,))}
    wanted -= {r["id"] for r in base}
    extras = [r for r in (conn.execute("SELECT * FROM routine WHERE id=?", (i,)).fetchone() for i in sorted(wanted))
              if r is not None]
    for ex in list(base) + extras:
        item = dict(ex)
        item["today"] = [dict(r) for r in conn.execute(
            "SELECT * FROM sets WHERE exercise_id=? AND day=? ORDER BY set_no", (ex["id"], day))]
        week_ago = (d - timedelta(days=7)).isoformat()
        has_week = conn.execute(
            "SELECT 1 FROM sets WHERE exercise_id=? AND day=?", (ex["id"], week_ago)).fetchone()
        if has_week:
            prev_day = week_ago
        else:
            r = conn.execute("SELECT MAX(day) FROM sets WHERE exercise_id=? AND day<?", (ex["id"], day)).fetchone()
            prev_day = r[0]
        item["prev_day"] = prev_day
        item["prev"] = [dict(r) for r in conn.execute(
            "SELECT * FROM sets WHERE exercise_id=? AND day=? ORDER BY set_no", (ex["id"], prev_day))] if prev_day else []
        out.append(item)
    s = conn.execute("SELECT value FROM settings WHERE key='level'").fetchone()
    return jsonify(exercises=out, level=s["value"] if s else "intermedio",
                   nai=nai_for(conn, day, valid_at(request.args.get("at"))))


@app.post("/api/sets")
def save_set():
    d = request.get_json(force=True, silent=True) or {}
    ex_id = to_int(d.get("exercise_id"), 1, 10**9)
    if db().execute("SELECT 1 FROM routine WHERE id=?", (ex_id,)).fetchone() is None:
        return jsonify(error="Ejercicio no encontrado"), 404
    day = valid_day(d.get("day"))
    # Aislamiento de variables: se guarda junto a la serie el modificador nutricional agudo (β·ΔNAI) de la sesión;
    # progreso y scores lo restan, de modo que el histórico refleje la fuerza pura.
    nai_mod = nai_for(db(), day, valid_at(d.get("at")))["mod"]
    vals = (
        day, ex_id, to_int(d.get("set_no"), 1, 50),
        to_float(d.get("kg"), 0, 1500), to_int(d.get("reps"), 1, 200), to_int(d.get("rir"), 0, 10), nai_mod,
    )
    db().execute(
        "INSERT INTO sets(day, exercise_id, set_no, kg, reps, rir, nai_mod) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(day, exercise_id, set_no) DO UPDATE SET kg=excluded.kg, reps=excluded.reps, rir=excluded.rir, "
        "nai_mod=excluded.nai_mod",
        vals,
    )
    db().commit()
    saved = db().execute(
        "SELECT * FROM sets WHERE day=? AND exercise_id=? AND set_no=?", (vals[0], vals[1], vals[2])).fetchone()
    return jsonify(dict(saved)), 201


@app.delete("/api/sets/<int:set_id>")
def delete_set(set_id):
    db().execute("DELETE FROM sets WHERE id=?", (set_id,))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/progress/<int:ex_id>")
def progress(ex_id):
    """Mejor e1RM (puro: sin el modificador nutricional agudo) y carga máxima por sesión."""
    out = {}
    for r in db().execute("SELECT * FROM sets WHERE exercise_id=? ORDER BY day, set_no", (ex_id,)):
        e = e1rm(r["kg"], r["reps"], r["rir"]) / (1 + r["nai_mod"])
        cur = out.setdefault(r["day"], {"day": r["day"], "e1rm": 0.0, "kg": 0.0})
        cur["e1rm"] = max(cur["e1rm"], round(e, 1))
        cur["kg"] = max(cur["kg"], r["kg"])
    return jsonify(list(out.values()))


@app.get("/api/scores")
def scores():
    """Suma diaria del score de rendimiento puro: Σ (kg × reps) × (1 + máx(0, 5 − RIR) / 5) / (1 + β·ΔNAI)."""
    a = valid_day(request.args.get("from"))
    b = valid_day(request.args.get("to"))
    rows = db().execute(
        "SELECT day, SUM(kg * reps * (1 + MAX(0, 5 - rir) / 5.0) / (1 + nai_mod)) AS score FROM sets "
        "WHERE day BETWEEN ? AND ? GROUP BY day", (a, b)
    ).fetchall()
    return jsonify([{"day": r["day"], "score": round(r["score"], 1)} for r in rows])


@app.post("/api/reset")
def reset_data():
    """Borra registros de dietas / entrenamientos / peso en un rango de fechas (o todo si no hay rango)."""
    d = request.get_json(force=True, silent=True) or {}
    if d.get("from") or d.get("to"):
        a, b = valid_day(d.get("from")), valid_day(d.get("to"))
    else:
        a, b = "0000-01-01", "9999-12-31"
    out = {}
    for key, table in {"foods": "foods", "sets": "sets", "weights": "weights"}.items():
        if d.get(key):
            out[key] = db().execute(f"DELETE FROM {table} WHERE day BETWEEN ? AND ?", (a, b)).rowcount
        else:
            out[key] = 0
    db().commit()
    return jsonify(out)


@app.post("/api/bulk")
def bulk_import():
    """Inserta de golpe alimentos, series y pesos (datos de ejemplo). Todo o nada."""
    d = request.get_json(force=True, silent=True) or {}
    foods, sets_, weights = d.get("foods") or [], d.get("sets") or [], d.get("weights") or []
    if len(foods) > 5000 or len(sets_) > 5000 or len(weights) > 1000:
        raise ValueError("Demasiados datos")
    conn = db()
    ex_ids = {r[0] for r in conn.execute("SELECT id FROM routine")}
    for f in foods:
        meal = str(f.get("meal", "")).lower()
        if meal not in MEALS:
            raise ValueError("Comida no válida")
        fday = valid_day(f.get("day"))
        conn.execute(
            "INSERT INTO foods(day, meal, name, grams, kcal, protein, carbs, fat, carb_type, caffeine_mg, timestamp) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fday, meal, text(f.get("name", "Alimento"), "nombre", 80),
             to_float(f.get("grams", 0), 0, 20000), to_float(f.get("kcal"), 0, 20000),
             to_float(f.get("protein"), 0, 2000), to_float(f.get("carbs"), 0, 3000), to_float(f.get("fat"), 0, 2000),
             *food_params(f, fday, meal)),
        )
    for s in sets_:
        ex_id = to_int(s.get("exercise_id"), 1, 10**9)
        if ex_id not in ex_ids:
            raise ValueError("Ejercicio no encontrado")
        conn.execute(
            "INSERT INTO sets(day, exercise_id, set_no, kg, reps, rir) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(day, exercise_id, set_no) DO UPDATE SET kg=excluded.kg, reps=excluded.reps, rir=excluded.rir",
            (valid_day(s.get("day")), ex_id, to_int(s.get("set_no"), 1, 50),
             to_float(s.get("kg"), 0, 1500), to_int(s.get("reps"), 1, 200), to_int(s.get("rir"), 0, 10)),
        )
    for w in weights:
        conn.execute(
            "INSERT INTO weights(day, kg) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET kg=excluded.kg",
            (valid_day(w.get("day")), to_float(w.get("kg"), 20, 400)),
        )
    conn.commit()
    return jsonify(foods=len(foods), sets=len(sets_), weights=len(weights)), 201


init_db()

if __name__ == "__main__":
    # host 0.0.0.0 para poder abrirla desde el móvil en la misma red Wi-Fi.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)