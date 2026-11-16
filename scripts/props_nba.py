"""
Crab Locks NBA player-prop projections (shared by the daily update and testing).
Everything for a game uses only games played before it.
"""
import math
from collections import defaultdict

STATS = ["pts", "reb", "ast", "fg3", "pra"]
DISP = {"pts": 1.9, "reb": 1.35, "ast": 1.45, "fg3": 1.15, "pra": 2.4}     # spread beyond a pure count model
K_MIN = 120.0                                                              # minutes of league-average production blended in

def pos_group(p):
    p = (p or "").upper()
    return "C" if p.startswith("C") else "G" if p.startswith("G") or p in ("PG", "SG") else "F"

def rows_from_box(records):
    """Box-score records -> clean player-game rows (played games only)."""
    out = []
    for r in records:
        if r.get("did_not_play") or not r.get("minutes") or r["minutes"] <= 0: continue
        d = int(str(r["game_date"])[:10].replace("-", ""))
        g = {"date": d, "game": str(r["game_id"]), "player": r["athlete_display_name"], "pid": str(r["athlete_id"]), "pos": pos_group(r.get("athlete_position_abbreviation")),
             "team": r["team_short_display_name"], "opp": r.get("opponent_team_name"), "home": r.get("home_away") == "home", "min": float(r["minutes"]),
             "pts": float(r.get("points") or 0), "reb": float(r.get("rebounds") or 0), "ast": float(r.get("assists") or 0), "fg3": float(r.get("three_point_field_goals_made") or 0),
             "starter": bool(r.get("starter")), "season": int(r.get("season") or 0)}
        g["pra"] = g["pts"] + g["reb"] + g["ast"]
        out.append(g)
    out.sort(key=lambda g: g["date"])
    return out

class State:
    """Running per-player and per-team totals, updated game by game, so projections only use past games."""
    def __init__(self):
        self.games = defaultdict(list)                       # pid -> list of past rows (this season)
        self.prev = {}                                       # pid -> last season per-minute rates + minutes
        self.allowed = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))   # opp team -> pos -> [stat sums...] per stat
        self.lg = defaultdict(lambda: [0.0, 0.0])            # (pos, stat) -> [stat, minutes] league totals
        self.team_games = defaultdict(set)
    def add(self, g):
        self.games[g["pid"]].append(g)
        for s in STATS:
            a = self.allowed[g["opp"]][(g["pos"], s)]; a[0] += g[s]; a[1] += g["min"]
            l = self.lg[(g["pos"], s)]; l[0] += g[s]; l[1] += g["min"]
        self.team_games[g["opp"]].add(g["game"])
    def lg_rate(self, pos, s):
        l = self.lg[(pos, s)]
        return l[0] / l[1] if l[1] > 0 else {"pts": .48, "reb": .2, "ast": .11, "fg3": .05, "pra": .79}[s]

def season_rates(rows):
    tot = defaultdict(float); m = 0.0
    for g in rows:
        m += g["min"]
        for s in STATS: tot[s] += g[s]
    return {s: tot[s] / m for s in STATS} if m > 0 else None, (m / len(rows) if rows else 0)

def project(st, pid, pos, team, opp, ctx):
    """ctx: {'b2b': bool, 'spread_abs': float, 'pace_f': float}. Returns dict or None."""
    G = st.games.get(pid, [])
    prev = st.prev.get(pid)
    if len(G) < 3 and not prev: return None
    l5, l10 = G[-5:], G[-10:]
    mp = lambda L: sum(g["min"] for g in L) / len(L) if L else None
    ms, m5, m10 = mp(G), mp(l5), mp(l10)
    if len(G) >= 3: mins = 0.45 * m5 + 0.30 * m10 + 0.25 * ms
    elif G: mins = 0.6 * mp(G) + 0.4 * prev["mpg"]
    else: mins = prev["mpg"]
    starter = mins >= 28
    if ctx.get("b2b") and starter: mins *= 0.96
    if (ctx.get("spread_abs") or 0) >= 12 and starter: mins *= 0.95
    out = {"min": round(mins, 1), "n": len(G)}
    tot = defaultdict(float); msum = sum(g["min"] for g in G)
    for g in G:
        for s in STATS: tot[s] += g[s]
    t10 = defaultdict(float); m10s = sum(g["min"] for g in l10)
    for g in l10:
        for s in STATS: t10[s] += g[s]
    for s in STATS:
        base = st.lg_rate(pos, s)
        prior = (prev["rate"][s] * 0.7 + base * 0.3) if prev else base
        k = K_MIN if prev else K_MIN * 0.4      # no NBA history (rookies): trust their own numbers sooner instead of the league average
        r_season = (tot[s] + k * prior) / (msum + k)
        r_l10 = (t10[s] + k * prior) / (m10s + k) if m10s else r_season
        rate = 0.65 * r_season + 0.35 * r_l10
        # opponent vs this position (per minute allowed relative to league), shrunk by sample size
        a = st.allowed[opp][(pos, s)]; ng = len(st.team_games[opp])
        of = (a[0] / a[1]) / base if a[1] > 0 and base > 0 else 1.0
        w = ng / (ng + 12.0); of = 1 + 0.5 * w * (of - 1)
        mean = mins * rate * ctx.get("pace_f", 1.0) * of
        # spread: the player's own game-to-game swings (scaled to minutes) blended with a count-model spread
        rec = G[-20:]
        if len(rec) >= 5:
            per = [g[s] / g["min"] * mins for g in rec if g["min"] > 0]
            mu = sum(per) / len(per); var_p = sum((x - mu) ** 2 for x in per) / (len(per) - 1)
        else:
            var_p = DISP[s] * mean
        sd = math.sqrt(max(0.25, 0.55 * var_p + 0.45 * DISP[s] * mean))
        out[s] = {"mean": round(mean, 2), "sd": round(sd, 2), "opp_f": round(of, 3)}
    return out

def p_over(mean, sd, line, stat):
    """Chance of finishing above the line (half-point lines), from the projection."""
    k = math.floor(line) + 1          # need at least k
    if stat == "fg3":                 # threes: count model
        lam = max(0.01, mean); p_le = sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k))
        return max(0.0, min(1.0, 1 - p_le))
    z = (k - 0.5 - mean) / sd
    return 0.5 * math.erfc(z / math.sqrt(2))

# Calibration from testing on 2025-26 (raw chance -> observed chance): keeps the app's percentages honest
CAL = {"pts": [0.0645, 0.8034], "reb": [0.014, 0.8921], "ast": [-0.0246, 0.9488], "fg3": [-0.0249, 1.0104], "pra": [0.079, 0.8004]}
def calibrated(p, stat):
    a, b = CAL.get(stat, [0.0, 1.0]); return max(0.02, min(0.98, a + b * p))

def no_vig(over, under):
    imp = lambda o: (-o / (-o + 100)) if o < 0 else (100 / (o + 100))
    a, b = imp(over), imp(under); return a / (a + b) if a + b > 0 else None
