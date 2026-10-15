"""NutriFit · física metabólica pre-entreno.

Funciones puras (sin Flask ni SQLite) para estimar cuánta energía "útil" tiene el atleta
al empezar a entrenar: Índice de Disponibilidad Nutricional (NAI).

    F_buffer = 1 − min(0.5, (grasas·1.5 + proteínas·0.5) / (carbohidratos + 1))
    NAI      = [ C_fast·k_fast(t) + C_slow·k_slow(t)·F_buffer ] · (1 + efecto_cafeína(t))

Notas de modelado (heurísticas, no medidas clínicas):
  · Solo cuentan ingestas entre 15 y 180 min antes de empezar la sesión.
  · Los carbohidratos asimilables se recortan a un techo de saturación de glucógeno (≈1,5 g/kg).
  · Tolerancia a estimulantes: «baja» = más sensible a la cafeína (efecto ×1,25); «alta» = efecto ×0,6.
  · El modificador agudo sobre la carga es  mod = β·(NAI − NAI_base), acotado a ±5 %.
"""
import math
from datetime import datetime, timedelta

WINDOW_MIN, WINDOW_MAX = 15, 180               # minutos previos a la sesión que se computan
CARB_TYPES = ("FAST", "SLOW")
TOLERANCES = {"baja": 1.25, "media": 1.0, "alta": 0.6}
MEAL_HOURS = {"desayuno": "08:00", "almuerzo": "14:00", "merienda": "17:30", "cena": "21:00", "suplementos": "09:00"}

CEILING_G_PER_KG = 1.5                         # techo de carbohidratos asimilables (g por kg)
CEILING_RANGE = (60.0, 150.0)
BASELINE_G_PER_KG = 0.5                        # NAI de referencia: ~0,5 g/kg bien asimilados
BASELINE_RANGE = (25.0, 80.0)
CAFFEINE_PER_MG_KG = 0.04                      # +4 % de efecto por cada mg/kg activo (tolerancia media)
CAFFEINE_CAP = 0.15
BETA = 0.0003                                  # fracción de carga por cada g-equivalente de NAI sobre la base
MOD_LIMIT = 0.05

# umbrales sobre r = NAI / NAI_base
R_DEPLETED, R_LOW, R_MAX = 0.25, 0.6, 1.8
R_FASTED = 0.10


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _curve(t, lo, hi, s_rise, s_fall):
    """Meseta (=1) entre lo y hi minutos; caída gaussiana asimétrica fuera de ella."""
    if lo <= t <= hi:
        return 1.0
    if t < lo:
        return math.exp(-0.5 * ((t - lo) / s_rise) ** 2)
    return math.exp(-0.5 * ((t - hi) / s_fall) ** 2)


def k_fast(t):
    """Carbohidratos rápidos: pico de asimilación a los 35-40 min."""
    return _curve(t, 35, 40, 15, 55)


def k_slow(t, f_buf=1.0):
    """Carbohidratos lentos: meseta 70-90 min, desplazada hasta +20 min según el buffer digestivo."""
    shift = (1.0 - f_buf) * 20.0
    return _curve(t, 70 + shift, 90 + shift, 30, 60)


def k_caffeine(t):
    """Cafeína: pico plasmático a los 45 min y caída lenta (semivida ~5 h)."""
    return _curve(t, 45, 45, 20, 120)


def f_buffer(fat, protein, carbs):
    """Coeficiente de retraso gástrico: grasas y proteínas aplanan la curva de los carbohidratos."""
    return 1.0 - min(0.5, (fat * 1.5 + protein * 0.5) / (carbs + 1.0))


def norm_timestamp(value, day, meal):
    """Normaliza a 'AAAA-MM-DDTHH:MM'. Acepta 'HH:MM' o un datetime ISO; si falta, usa la hora típica de la comida."""
    s = str(value or "").strip()
    if s:
        try:
            if len(s) <= 5:
                return f"{day}T{datetime.strptime(s, '%H:%M').strftime('%H:%M')}"
            return datetime.fromisoformat(s.replace(" ", "T")[:16]).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            raise ValueError("Hora no válida (use HH:MM)")
    return f"{day}T{MEAL_HOURS.get(meal, '12:00')}"


def eaten_at(food):
    ts = food.get("timestamp") or f"{food['day']}T{MEAL_HOURS.get(food.get('meal'), '12:00')}"
    return datetime.fromisoformat(ts[:16])


def session_start(day, at):
    """Instante de inicio de la sesión: día + 'HH:MM'."""
    return datetime.fromisoformat(f"{day}T{at}")


def ceiling(weight):
    return _clip(CEILING_G_PER_KG * weight, *CEILING_RANGE)


def baseline(weight):
    return _clip(BASELINE_G_PER_KG * weight, *BASELINE_RANGE)


def compute_nai(foods, start, weight, tolerance="media"):
    """foods: lista de dicts (day, meal, timestamp, carbs, protein, fat, carb_type, caffeine_mg)."""
    weight = weight if weight and weight > 0 else 75.0
    tol = TOLERANCES.get(tolerance, 1.0)

    # El buffer se calcula por comida completa (grasas y proteínas del plato frenan todos sus carbohidratos).
    meals = {}
    for f in foods:
        m = meals.setdefault((f["day"], f["meal"]), {"c": 0.0, "p": 0.0, "f": 0.0})
        m["c"] += f["carbs"]; m["p"] += f["protein"]; m["f"] += f["fat"]

    fast = slow = caf_mg = 0.0
    used = set()
    buf_acc = buf_w = 0.0
    for f in foods:
        t = (start - eaten_at(f)).total_seconds() / 60.0
        if not (WINDOW_MIN <= t <= WINDOW_MAX):
            continue                                   # fuera de la ventana útil de ingesta
        m = meals[(f["day"], f["meal"])]
        fb = f_buffer(m["f"], m["p"], m["c"])
        used.add((f["day"], f["meal"]))
        if f["carbs"] > 0:
            if (f.get("carb_type") or "SLOW") == "FAST":
                fast += f["carbs"] * k_fast(t)
            else:
                slow += f["carbs"] * k_slow(t, fb) * fb
                buf_acc += fb * f["carbs"]; buf_w += f["carbs"]
        caf_mg += (f.get("caffeine_mg") or 0.0) * k_caffeine(t)

    raw = fast + slow
    cap = ceiling(weight)
    carbs_eff = min(raw, cap)                          # techo de saturación de glucógeno
    caf_effect = min(CAFFEINE_CAP, tol * CAFFEINE_PER_MG_KG * caf_mg / weight)
    nai = carbs_eff * (1.0 + caf_effect)

    base = baseline(weight)
    ratio = nai / base
    mod = _clip(BETA * (nai - base), -MOD_LIMIT, MOD_LIMIT)
    if ratio < R_DEPLETED:
        state = "depleted"
    elif ratio < R_LOW:
        state = "low"
    elif ratio >= R_MAX:
        state = "max"
    else:
        state = "optimal"
    return {
        "nai": round(nai, 1), "baseline": round(base, 1), "ratio": round(ratio, 3), "mod": round(mod, 4),
        "state": state, "carbs_eff": round(carbs_eff, 1), "carbs_raw": round(raw, 1), "ceiling": round(cap, 1),
        "saturated": raw > cap, "caffeine_effect": round(caf_effect, 3), "caffeine_mg_active": round(caf_mg, 1),
        "f_buffer": round(buf_acc / buf_w, 3) if buf_w else 1.0, "meals": len(used),
        "fasted": ratio < R_FASTED, "tolerance": tolerance if tolerance in TOLERANCES else "media",
    }
