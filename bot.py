import os, requests, time, schedule, json, math, hashlib
from datetime import datetime
from difflib import SequenceMatcher
import pytz
from scipy.stats import poisson

# ─── Credenciales ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ODDS_API_KEY     = os.environ.get("ODDS_API_KEY")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")

# ─── Configuración general ───────────────────────────────────────────────────
TZ             = pytz.timezone("America/Argentina/Buenos_Aires")
MUNDIAL_START  = datetime(2026, 6, 11, 0, 0, 0, tzinfo=pytz.timezone("America/Argentina/Buenos_Aires"))
SPORT_KEY      = "soccer_fifa_world_cup"
LEAGUE_ID      = 1
SEASON         = 2026

# ─── Umbrales ────────────────────────────────────────────────────────────────
MIN_PROB       = 60
MIN_SAMPLE     = 3
CORNER_LINES   = [7.5, 8.5, 9.5, 10.5, 11.5]
CARD_LINES     = [1.5, 2.5, 3.5, 4.5]

# ─── Límites de API ──────────────────────────────────────────────────────────
MAX_APIF_DAY   = 90
MAX_ODDS_TOTAL = 480

# ─── Archivos persistentes ───────────────────────────────────────────────────
APIF_REQ_FILE  = "/tmp/apif_requests_mundial.json"
ODDS_REQ_FILE  = "/tmp/odds_requests_mundial.json"
SENT_FILE      = "/tmp/sent_picks_mundial.json"
CACHE_FILE     = "/tmp/api_cache_mundial.json"
REFEREE_FILE   = "/tmp/referee_history_mundial.json"
CACHE_TTL      = 3600
# ─── Contadores API-Football ──────────────────────────────────────────────────
def load_apif_req():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        with open(APIF_REQ_FILE) as f:
            d = json.load(f)
            return d.get("count", 0) if d.get("date") == today else 0
    except:
        return 0

def save_apif_req(n):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        with open(APIF_REQ_FILE, "w") as f:
            json.dump({"date": today, "count": n}, f)
    except:
        pass

def inc_apif_req():
    n = load_apif_req() + 1
    save_apif_req(n)
    return n

# ─── Contadores Odds API ──────────────────────────────────────────────────────
def load_odds_total():
    try:
        with open(ODDS_REQ_FILE) as f:
            d = json.load(f)
            return d.get("total", 0)
    except:
        return 0

def save_odds_total(n):
    try:
        with open(ODDS_REQ_FILE, "w") as f:
            json.dump({"total": n}, f)
    except:
        pass

def inc_odds_req():
    n = load_odds_total() + 1
    save_odds_total(n)
    return n

def odds_disponibles():
    total = load_odds_total()
    if total >= MAX_ODDS_TOTAL:
        print(f"[ODDS] Limite alcanzado: {total}/{MAX_ODDS_TOTAL}")
        return False
    return True

# ─── Cache en disco con TTL ───────────────────────────────────────────────────
def _cache_key(endpoint, params):
    raw = f"{endpoint}|{sorted(params.items())}"
    return hashlib.md5(raw.encode()).hexdigest()

def cache_get(key):
    try:
        with open(CACHE_FILE) as f:
            store = json.load(f)
        entry = store.get(key)
        if entry and time.time() - entry["ts"] < CACHE_TTL:
            return entry["data"]
    except:
        pass
    return None

def cache_set(key, data):
    try:
        try:
            with open(CACHE_FILE) as f:
                store = json.load(f)
        except:
            store = {}
        store[key] = {"data": data, "ts": time.time()}
        with open(CACHE_FILE, "w") as f:
            json.dump(store, f)
    except Exception as e:
        print(f"[CACHE ERROR] {e}")

# ─── Picks enviados ───────────────────────────────────────────────────────────
def load_sent():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        with open(SENT_FILE) as f:
            d = json.load(f)
            return set(d.get("picks", [])) if d.get("date") == today else set()
    except:
        return set()

def save_sent(picks_set):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        with open(SENT_FILE, "w") as f:
            json.dump({"date": today, "picks": list(picks_set)}, f)
    except:
        pass
def load_referee_history():
    try:
        with open(REFEREE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_referee_history(data):
    try:
        with open(REFEREE_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass
def update_referee(name, yellows, reds):
    if not name:
        return
    data = load_referee_history()
    if name not in data:
        data[name] = {"matches": [], "total_yellows": 0, "total_reds": 0, "count": 0}
    data[name]["matches"].append({"yellows": yellows, "reds": reds})
    data[name]["total_yellows"] += yellows
    data[name]["total_reds"] += reds
    data[name]["count"] += 1
    save_referee_history(data)

def get_referee_stats(name):
    if not name:
        return None
    data = load_referee_history()
    if name not in data or data[name]["count"] < 3:
        return None
    d = data[name]
    return {
        "name":         name,
        "avg_yellows":  round(d["total_yellows"] / d["count"], 1),
        "avg_reds":     round(d["total_reds"] / d["count"], 1),
        "matches":      d["count"],
    }
# ─── Telegram con reintentos ──────────────────────────────────────────────────
def send_telegram(msg, retries=3, delay=3):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for part in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(
                    url,
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": part, "parse_mode": "HTML"},
                    timeout=10
                )
                if r.status_code == 200:
                    break
                print(f"[TG WARN] Intento {attempt} fallido: HTTP {r.status_code}")
            except Exception as e:
                print(f"[TG ERROR] Intento {attempt}: {e}")
            if attempt < retries:
                time.sleep(delay)
        else:
            print(f"[TG FATAL] No se pudo enviar tras {retries} intentos")
        time.sleep(1)
      # ─── API-Football ─────────────────────────────────────────────────────────────
def apif(endpoint, params):
    if load_apif_req() >= MAX_APIF_DAY:
        print(f"[APIF] Limite diario alcanzado ({MAX_APIF_DAY})")
        return []
    key = _cache_key(endpoint, params)
    cached = cache_get(key)
    if cached is not None:
        print(f"[CACHE HIT] {endpoint}")
        return cached
    try:
        r = requests.get(
            f"https://v3.football.api-sports.io/{endpoint}",
            headers={"x-apisports-key": API_FOOTBALL_KEY},
            params=params, timeout=10
        )
        n = inc_apif_req()
        print(f"[APIF #{n}] {endpoint}")
        data = r.json().get("response", [])
        cache_set(key, data)
        return data
    except Exception as e:
        print(f"[APIF ERROR] {e}")
        return []

# ─── Utilidades ───────────────────────────────────────────────────────────────
def normalize(name):
    return name.lower().strip()

def similar(a, b, threshold=0.75):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio() >= threshold

def find_best_line(values, lines, label):
    if len(values) < MIN_SAMPLE:
        return None
    n = len(values)
    best, best_prob = None, 0
    for line in lines:
        over_prob  = int(sum(1 for v in values if v > line) / n * 100)
        under_prob = int(sum(1 for v in values if v < line) / n * 100)
        if over_prob >= MIN_PROB and under_prob >= MIN_PROB:
            print(f"[WARN] Conflicto estadistico en {label} linea {line}: "
                  f"over={over_prob}% under={under_prob}% | muestra={values}")
            continue
        for prob, bet in [
            (over_prob,  f"Mas de {line} {label}"),
            (under_prob, f"Menos de {line} {label}"),
        ]:
            if prob >= MIN_PROB and prob > best_prob:
                best_prob, best = prob, {"bet": bet, "prob": prob}
    return best

def poisson_prob_over(lam, threshold):
    if lam <= 0:
        return 0
    k = math.floor(threshold)
    prob_under_eq = sum(poisson.pmf(i, lam) for i in range(0, k + 1))
    return max(0, min(int((1 - prob_under_eq) * 100), 95))

def poisson_prob_under(lam, threshold):
    if lam <= 0:
        return 95
    k = math.floor(threshold)
    prob_under = sum(poisson.pmf(i, lam) for i in range(0, k + 1))
    return max(0, min(int(prob_under * 100), 95))

def is_useful_hour():
    hour = datetime.now(TZ).hour
    return 3 <= hour <= 23

def mundial_iniciado():
    return datetime.now(TZ) >= MUNDIAL_START

# ─── Análisis de equipos ──────────────────────────────────────────────────────
def team_id_fuzzy(name):
    data = apif("teams", {"name": name, "league": LEAGUE_ID, "season": SEASON})
    if data:
        return data[0]["team"]["id"]
    all_teams = apif("teams", {"league": LEAGUE_ID, "season": SEASON})
    name_n = normalize(name)
    for t in all_teams:
        tname = normalize(t["team"]["name"])
        if similar(name_n, tname):
            return t["team"]["id"]
        if name_n in tname or tname in name_n:
            return t["team"]["id"]
        words = [w for w in name_n.split() if len(w) >= 3]
        if any(w in tname for w in words):
            return t["team"]["id"]
    return None

def team_form(tid):
    fx = apif("fixtures", {"team": tid, "last": 8, "season": SEASON, "status": "FT"})
    if not fx:
        return None
    wins, gf, ga = 0, [], []
    for f in fx:
        hid     = f["teams"]["home"]["id"]
        hg      = f["goals"]["home"] or 0
        ag      = f["goals"]["away"] or 0
        is_home = hid == tid
        gf.append(hg if is_home else ag)
        ga.append(ag if is_home else hg)
        if f["teams"]["home" if is_home else "away"]["winner"]:
            wins += 1
    n = len(fx)
    return {
        "win_rate":      round(wins / n * 100),
        "avg_for":       round(sum(gf) / n, 2),
        "avg_against":   round(sum(ga) / n, 2),
        "goals_list":    gf,
        "conceded_list": ga,
        "sample":        n,
    }

def fixture_corners_cards(tid):
    if load_apif_req() >= 80:
        return [], [], [], []
    fx = apif("fixtures", {"team": tid, "last": 8, "season": SEASON, "status": "FT"})
    corners, cards, corner_totals, card_totals = [], [], [], []
    for f in fx[:8]:
        if load_apif_req() >= 85:
            break
        home_corners, away_corners = None, None
        home_cards,   away_cards   = None, None
        tid_is_home = f["teams"]["home"]["id"] == tid
        for ts in apif("fixtures/statistics", {"fixture": f["fixture"]["id"]}):
            is_home_team = ts.get("team", {}).get("id") == f["teams"]["home"]["id"]
            for s in ts.get("statistics", []):
                val = s.get("value")
                if val is None:
                    continue
                try:
                    v = int(val)
                    if s["type"] == "Corner Kicks":
                        if is_home_team: home_corners = v
                        else:            away_corners = v
                    elif s["type"] == "Yellow Cards":
                        if is_home_team: home_cards = v
                        else:            away_cards = v
                except:
                    pass
        if home_corners is not None and away_corners is not None:
            corner_totals.append(home_corners + away_corners)
            corners.append(home_corners if tid_is_home else away_corners)
        if home_cards is not None and away_cards is not None:
            card_totals.append(home_cards + away_cards)
            cards.append(home_cards if tid_is_home else away_cards)
    return corners, cards, corner_totals, card_totals
# ─── Análisis de goles ────────────────────────────────────────────────────────
def analyze_goals(home, away):
    hid = team_id_fuzzy(home)
    aid = team_id_fuzzy(away)
    h2h = []
    if hid and aid:
        h2h = apif("fixtures/headtohead", {"h2h": f"{hid}-{aid}", "last": 10, "status": "FT"})
    if len(h2h) >= MIN_SAMPLE:
        goals, ht_goals, btts, hg_list, ag_list = [], [], [], [], []
        for g in h2h:
            hg  = g["goals"]["home"] or 0
            ag  = g["goals"]["away"] or 0
            hht = (g.get("score", {}).get("halftime", {}) or {}).get("home") or 0
            aht = (g.get("score", {}).get("halftime", {}) or {}).get("away") or 0
            goals.append(hg + ag)
            ht_goals.append(hht + aht)
            btts.append(1 if hg > 0 and ag > 0 else 0)
            if g["teams"]["home"]["id"] == hid:
                hg_list.append(hg); ag_list.append(ag)
            else:
                hg_list.append(ag); ag_list.append(hg)
        n = len(h2h)
        return {
            "over25_prob":    int(sum(1 for g in goals if g > 2.5) / n * 100),
            "under25_prob":   int(sum(1 for g in goals if g < 2.5) / n * 100),
            "over15_prob":    int(sum(1 for g in goals if g > 1.5) / n * 100),
            "btts_prob":      int(sum(btts) / n * 100),
            "ht_over15_prob": int(sum(1 for g in ht_goals if g > 1.5) / n * 100),
            "home_goals":     hg_list,
            "away_goals":     ag_list,
            "avg_goals":      round(sum(goals) / n, 1),
            "sample":         n,
            "source":         "h2h",
        }
    hf = team_form(hid) if hid else None
    af = team_form(aid) if aid else None
    if not hf or not af:
        return None
    lam_home  = (hf["avg_for"] + af["avg_against"]) / 2
    lam_away  = (af["avg_for"] + hf["avg_against"]) / 2
    lam_total = lam_home + lam_away
    lam_ht    = (hf.get("avg_ht_for", 0.0) + af.get("avg_ht_for", 0.0)) / 2 * 2
    p_home_scores = int((1 - poisson.pmf(0, lam_home)) * 100)
    p_away_scores = int((1 - poisson.pmf(0, lam_away)) * 100)
    return {
        "over25_prob":    poisson_prob_over(lam_total, 2.5),
        "under25_prob":   poisson_prob_under(lam_total, 2.5),
        "over15_prob":    poisson_prob_over(lam_total, 1.5),
        "btts_prob":      int(p_home_scores * p_away_scores / 100),
        "ht_over15_prob": poisson_prob_over(lam_ht, 1.5),
        "home_goals":     hf.get("goals_list", []),
        "away_goals":     af.get("goals_list", []),
        "home_form":      hf["win_rate"],
        "away_form":      af["win_rate"],
        "avg_goals":      round(lam_total, 1),
        "sample":         min(hf["sample"], af["sample"]),
        "source":         "form",
    }

# ─── Análisis de corners y tarjetas ──────────────────────────────────────────
def analyze_cc(home, away):
    res = {k: None for k in [
        "corners_total", "corners_home", "corners_away",
        "cards_total",   "cards_home",   "cards_away",
        "corners_avg",   "cards_avg",    "source",
    ]}
    res["source"] = "none"
    hid = team_id_fuzzy(home)
    aid = team_id_fuzzy(away)
    if not hid or not aid or load_apif_req() >= 80:
        return res
    hc_list, hk_list, h_corner_totals, h_card_totals = fixture_corners_cards(hid)
    ac_list, ak_list, a_corner_totals, a_card_totals = fixture_corners_cards(aid)
    if len(hc_list) >= MIN_SAMPLE and len(ac_list) >= MIN_SAMPLE:
        avg_c_home  = sum(hc_list) / len(hc_list)
        avg_c_away  = sum(ac_list) / len(ac_list)
        avg_total   = avg_c_home + avg_c_away
        corner_totals = h_corner_totals if len(h_corner_totals) >= MIN_SAMPLE else \
                        [avg_total] * max(len(hc_list), len(ac_list))
        res["corners_total"] = find_best_line(corner_totals, CORNER_LINES, "corners")
        res["corners_home"]  = find_best_line(hc_list, [3.5, 4.5, 5.5, 6.5], f"corners ({home})")
        res["corners_away"]  = find_best_line(ac_list, [3.5, 4.5, 5.5, 6.5], f"corners ({away})")
        res["corners_avg"]   = round(avg_total, 1)
        res["source"]        = "api_football"
    if len(hk_list) >= MIN_SAMPLE and len(ak_list) >= MIN_SAMPLE:
        avg_k_home  = sum(hk_list) / len(hk_list)
        avg_k_away  = sum(ak_list) / len(ak_list)
        card_totals = h_card_totals if len(h_card_totals) >= MIN_SAMPLE else \
                      [round(avg_k_home + avg_k_away)] * max(len(hk_list), len(ak_list))
        res["cards_total"] = find_best_line(card_totals, CARD_LINES, "tarjetas")
        res["cards_home"]  = find_best_line(hk_list, [0.5, 1.5, 2.5], f"tarjetas ({home})")
        res["cards_away"]  = find_best_line(ak_list, [0.5, 1.5, 2.5], f"tarjetas ({away})")
        res["cards_avg"]   = round(avg_k_home + avg_k_away, 1)
        res["source"]      = "api_football"
    return res

# ─── Value bets contra cuotas ─────────────────────────────────────────────────
def best_odds_pick(home, away, bookmakers, stats):
    if not stats:
        return None
    candidates = []
    MAP = [
        ("totals", "Over 2.5",  "over25_prob", "Mas de 2.5 goles"),
        ("totals", "Under 2.5", "under25_prob","Menos de 2.5 goles"),
        ("totals", "Over 1.5",  "over15_prob", "Mas de 1.5 goles"),
        ("btts",   "Yes",       "btts_prob",   "Ambos equipos marcan"),
        ("btts",   "No",        None,          "No ambos marcan"),
    ]
    for bm in bookmakers:
        for market in bm.get("markets", []):
            mk = market["key"]
            for outcome in market["outcomes"]:
                name, odd = outcome["name"], float(outcome["price"])
                implied   = round(1 / odd * 100, 1)
                for (mk_key, target, stat_key, label) in MAP:
                    if mk != mk_key:
                        continue
                    if not similar(name, target):
                        continue
                    if stat_key is None:
                        btts = stats.get("btts_prob")
                        sp   = 100 - btts if btts is not None else None
                    else:
                        sp = stats.get(stat_key)
                    if sp is None:
                        break
                    value = sp - implied
                    if 1.30 <= odd <= 2.60 and sp >= 55 and value >= 2:
                        candidates.append({
                            "bet":   label,
                            "odd":   odd,
                            "prob":  sp,
                            "value": round(value, 1),
                        })
                    break
    candidates.sort(key=lambda x: (x["value"], x["prob"]), reverse=True)
    return candidates[0] if candidates else None
  # ─── Picks por franja horaria ─────────────────────────────────────────────────
def get_picks_by_window(hour_from, hour_to, turno_label):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    print(f"\n{'='*55}")
    print(f"Mundial - {turno_label} | {today} | "
          f"APIF: {load_apif_req()}/{MAX_APIF_DAY} | "
          f"Odds: {load_odds_total()}/{MAX_ODDS_TOTAL}")
    print(f"{'='*55}")
    odds_picks, stats_picks = [], []
    analyzed = 0
    if not odds_disponibles():
        send_telegram(
            f"IvanPicks Mundial\nLimite de Odds API alcanzado "
            f"({load_odds_total()}/{MAX_ODDS_TOTAL})."
        )
        return [], []
    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds",
            params={
                "apiKey":     ODDS_API_KEY,
                "regions":    "eu",
                "markets":    "h2h,totals",
                "oddsFormat": "decimal",
            },
            timeout=10,
        )
        odds_used = inc_odds_req()
        print(f"[ODDS #{odds_used}] Partidos obtenidos")
        if r.status_code != 200:
            print(f"[ODDS ERROR] HTTP {r.status_code}: {r.text[:100]}")
            return [], []
        for game in r.json():
            ct_utc = game.get("commence_time", "")
            if today not in ct_utc:
                continue
            try:
                ct_dt    = datetime.strptime(ct_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.utc)
                ct_local = ct_dt.astimezone(TZ)
                ct_hour  = ct_local.hour
            except:
                continue
            if not (hour_from <= ct_hour < hour_to):
                continue
            if load_apif_req() >= MAX_APIF_DAY:
                break
            home, away = game["home_team"], game["away_team"]
            bm         = game.get("bookmakers", [])
            if not bm:
                continue
            hora_str = ct_local.strftime("%H:%M")
            print(f"[{turno_label}] {hora_str} - {home} vs {away}")
            hid = team_id_fuzzy(home)
            aid = team_id_fuzzy(away)
            referee_name = None
            ref_stats = None
            if hid and aid:
                today_str = datetime.now(TZ).strftime("%Y-%m-%d")
                fx_data = apif("fixtures", {"team": hid, "date": today_str, "season": SEASON})
                for fx in fx_data:
                    teams = fx.get("teams", {})
                    home_id = teams.get("home", {}).get("id")
                    away_id = teams.get("away", {}).get("id")
                    if set([home_id, away_id]) == set([hid, aid]):
                        referee_raw = fx.get("fixture", {}).get("referee") or ""
                        referee_name = referee_raw.split(",")[0].strip() if referee_raw else None
                        ref_stats = get_referee_stats(referee_name)
                        break
                    
            stats = analyze_goals(home, away)
            pick  = best_odds_pick(home, away, bm, stats)
            if pick:
                odds_picks.append({"match": f"{home} vs {away}", "hora": hora_str, "referee": ref_stats, **pick})
            if stats:
                for goal_list, label in [
                    (stats.get("home_goals"), home),
                    (stats.get("away_goals"), away),
                ]:
                    if goal_list and len(goal_list) >= MIN_SAMPLE:
                        p = find_best_line(goal_list, [0.5, 1.5], f"goles ({label})")
                        if p:
                            stats_picks.append({
                                "match":  f"{home} vs {away}",
                                "hora":   hora_str,
                                "bet":    p["bet"],
                                "prob":   p["prob"],
                                "avg":    stats["avg_goals"],
                                "sample": stats["sample"],
                            })
            cc = analyze_cc(home, away)
            if ref_stats and cc.get("cards_avg") is not None:
                ref_factor = ref_stats["avg_yellows"] / 3.5
                cc["cards_avg"] = round((cc["cards_avg"] + ref_stats["avg_yellows"]) / 2, 1)
                if cc.get("cards_total") and cc["cards_total"].get("prob"):
                    adj = min(10, int((ref_factor - 1) * 15))
                    cc["cards_total"]["prob"] = min(95, cc["cards_total"]["prob"] + adj)
                    cc["cards_total"]["bet"] = cc["cards_total"]["bet"] + f" (Árbitro: {ref_stats['name']} {ref_stats['avg_yellows']}AM/p)"

            for field in ["corners_total", "corners_home", "corners_away",
                          "cards_total",   "cards_home",   "cards_away"]:
                p = cc.get(field)
                if p:
                    avg = cc.get("corners_avg") if "corner" in field else cc.get("cards_avg")
                    stats_picks.append({
                        "match":  f"{home} vs {away}",
                        "hora":   hora_str,
                        "bet":    p["bet"],
                        "prob":   p["prob"],
                        "avg":    avg,
                        "sample": "ultimos partidos",
                    })
            analyzed += 1
    except Exception as e:
        print(f"[ERROR] {e}")
    print(f"[Resumen {turno_label}] Partidos: {analyzed} | "
          f"APIF: {load_apif_req()}/{MAX_APIF_DAY} | "
          f"Odds total: {load_odds_total()}/{MAX_ODDS_TOTAL}")
    odds_picks.sort(key=lambda x: (x["value"], x["prob"]), reverse=True)
    stats_picks.sort(key=lambda x: x["prob"], reverse=True)
    seen = set()
    def dedup(lst):
        out = []
        for p in lst:
            k = f"{p['match']}-{p['bet']}"
            if k not in seen:
                seen.add(k)
                out.append(p)
        return out
    return dedup(odds_picks)[:10], dedup(stats_picks)[:8]

# ─── Envio de picks ───────────────────────────────────────────────────────────
def send_picks(odds_picks, stats_picks, title):
    sent  = load_sent()
    new_o = [p for p in odds_picks  if f"{p['match']}-{p['bet']}" not in sent]
    new_s = [p for p in stats_picks if f"{p['match']}-{p['bet']}" not in sent]
    if not new_o and not new_s:
        print(f"[{title}] Sin picks nuevos")
        return
    casa = "STAKE" if len(new_o) >= 2 else "1XBET"
    msg  = f"IVANPICKS MUNDIAL - {title}\n"
    msg += f"{datetime.now(TZ).strftime('%d/%m/%Y %H:%M')}\n"
    msg += f"Odds API usados: {load_odds_total()}/{MAX_ODDS_TOTAL}\n\n"
    if new_o:
        msg += f"Casa: {casa}\n\n"
        for i, p in enumerate(new_o, 1):
            msg += f"Pick {i} | {p.get('hora', '')}\n{p['match']}\nCopa Mundial FIFA 2026\n{p['bet']}\nCuota: {p['odd']} | Prob: {p['prob']}% | Valor: +{p['value']}%\n"
            if p.get("referee"):
                msg += f"Árbitro: {p['referee']['name']} | AM/p: {p['referee']['avg_yellows']} | Rojas/p: {p['referee']['avg_reds']}\n"
            msg += "\n"
    if new_s:
        msg += "ANALISIS ESTADISTICO\n"
        msg += "Busca estas lineas en tu casa de apuestas\n\n"
        for p in new_s:
            msg += f"{p.get('hora', '')} - {p['match']}\n{p['bet']}\nProb: {p['prob']}%"
            if p.get("avg"):
                msg += f" | Prom: {p['avg']}"
            msg += f" | Muestra: {p['sample']}\n\n"
    msg += "Apostá con responsabilidad."
    send_telegram(msg)
    for p in new_o + new_s:
        sent.add(f"{p['match']}-{p['bet']}")
    save_sent(sent)

# ─── Jobs del scheduler ───────────────────────────────────────────────────────
def analisis_manana():
    if not mundial_iniciado():
        print("[05:00] Mundial no iniciado, esperando al 11 de junio...")
        return
    print("\n[03:00] Analisis diario...")
    o, s = get_picks_by_window(0, 24, "Picks del dia")
    send_picks(o, s, "Picks del dia")

def revision_oportunidades():
    if not mundial_iniciado():
        return
    if not is_useful_hour():
        return
    if not odds_disponibles():
        return
    now_hour = datetime.now(TZ).hour
    print(f"\n[REVISION {now_hour:02d}:00] Buscando oportunidades nuevas...")
    o, s = get_picks_by_window(0, 24, f"Revision {now_hour:02d}:00")
    sent = load_sent()
    no   = [x for x in o if f"{x['match']}-{x['bet']}" not in sent]
    ns   = [x for x in s if f"{x['match']}-{x['bet']}" not in sent]
    if no or ns:
        send_picks(no[:3], ns[:3], f"Nueva oportunidad {now_hour:02d}:00")
    else:
        print(f"[REVISION {now_hour:02d}:00] Sin picks nuevos")

# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Bot IvanPicks Mundial iniciando...")
    send_telegram(
        f"Bot IvanPicks Mundial iniciado\n"
        f"Copa Mundial FIFA 2026\n"
        f"Analisis disponible desde el 11 de junio\n"
        f"Odds API: {load_odds_total()}/{MAX_ODDS_TOTAL} creditos usados"
    )
    schedule.every().day.at("15:00").do(analisis_manana)
    schedule.every(2).hours.do(revision_oportunidades)
    print("Scheduler activo. Esperando...")
    while True:
        schedule.run_pending()
        time.sleep(60)
