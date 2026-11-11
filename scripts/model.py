"""
Crab Locks game model: shared by the training script and the daily update,
so the inputs used in testing are exactly the inputs used for today's games.

Everything is computed "as of" game day: a game's inputs only use games played
before it (plus last season, pulled toward average, as a starting point).
"""
import math
from collections import defaultdict

CFG = {
    "NFL":   {"cap": 28, "lam": 3, "prior": 0.55, "hfa": 1.8, "K": 250},
    "NCAAF": {"cap": 35, "lam": 3, "prior": 0.55, "hfa": 3.0, "K": 300},
}

def srs(games, prior, cfg, iters=30):
    """Score-based ratings. games: list of (home, away, hs, as_, neutral)."""
    teams = {t for g in games for t in g[:2]}
    r = {t: prior.get(t, 0.0) for t in teams}; H = cfg["hfa"]
    by = defaultdict(list)
    for i, g in enumerate(games): by[g[0]].append(i); by[g[1]].append(i)
    for _ in range(iters):
        for t in teams:
            s = n = 0
            for i in by[t]:
                h, a, hs, as_, neu = games[i]; m = max(-cfg["cap"], min(cfg["cap"], hs - as_)); hh = 0 if neu else H
                s += (m - hh + r[a]) if h == t else (-m + hh + r[h]); n += 1
            r[t] = (s + cfg["lam"] * prior.get(t, 0.0)) / (n + cfg["lam"])
        avg = sum(r.values()) / max(1, len(r))
        for t in r: r[t] -= avg
        hs_ = hn = 0
        for h, a, hs, as_, neu in games:
            if not neu: hs_ += max(-cfg["cap"], min(cfg["cap"], hs - as_)) - (r[h] - r[a]); hn += 1
        H = (hs_ + 20 * cfg["hfa"]) / (hn + 20)
    return r, H

def team_asof(sport, team_games, games_by_season):
    """
    team_games: list of dicts {season, order, game, team, side('o'/'d'), plays, epa, succ, pplays, pepa}
    games_by_season: {season: [ {game, order, home, away, hs, as, neu} ... ]} finished games, any order
    Returns ({(game, team): features}, {(game): rating features})
    """
    cfg = CFG[sport]; K = cfg["K"]
    # per team per season, chronological list of (order, game, o-stats, d-stats)
    tg = defaultdict(dict)
    for x in team_games:
        key = (x["season"], x["team"]); g = tg[key].setdefault(x["game"], {"order": x["order"], "o": None, "d": None})
        g[x["side"]] = x
    feats = {}; current = {}
    seasons = sorted({s for s, _ in tg})
    last_final = {}   # team -> final per-play values of the previous season
    for s in seasons:
        teams = [t for (ss, t) in tg if ss == s]
        season_final = {}
        for t in teams:
            gl = sorted(tg[(s, t)].items(), key=lambda kv: kv[1]["order"])
            pf = last_final.get(t, {})
            acc = defaultdict(float); hist = []
            for gid, g in gl:
                # features BEFORE this game
                def per(num, den, prior_key):
                    p = pf.get(prior_key, 0.0) * CFG[sport]["prior"]
                    return (acc[num] + p * K) / (acc[den] + K)
                last4 = hist[-4:]
                l4o = sum(h["oe"] for h in last4) / max(1, sum(h["op"] for h in last4)) if last4 else per("oe", "op", "oe")
                l4d = sum(h["de"] for h in last4) / max(1, sum(h["dp"] for h in last4)) if last4 else per("de", "dp", "de")
                feats[(gid, t)] = {"n": len(hist), "oe": per("oe", "op", "oe"), "de": per("de", "dp", "de"),
                                   "osr": per("os", "op", "osr"), "dsr": per("ds", "dp", "dsr"),
                                   "ope": per("ope", "opp", "ope"), "dpe": per("dpe", "dpp", "dpe"),
                                   "l4o": l4o, "l4d": l4d, "ppg": (acc["op"] / len(hist)) if hist else pf.get("ppg", 0.0)}
                o, d = g["o"], g["d"]
                if o: acc["op"] += o["plays"]; acc["oe"] += o["epa"]; acc["os"] += o["succ"]; acc["opp"] += o["pplays"]; acc["ope"] += o["pepa"]
                if d: acc["dp"] += d["plays"]; acc["de"] += d["epa"]; acc["ds"] += d["succ"]; acc["dpp"] += d["pplays"]; acc["dpe"] += d["pepa"]
                hist.append({"oe": o["epa"] if o else 0, "op": o["plays"] if o else 0, "de": d["epa"] if d else 0, "dp": d["plays"] if d else 0})
            # inputs going into the team's NEXT (not yet played) game
            last4 = hist[-4:]
            def perc(num, den, prior_key):
                p = pf.get(prior_key, 0.0) * CFG[sport]["prior"]
                return (acc[num] + p * K) / (acc[den] + K)
            current[(s, t)] = {"n": len(hist), "oe": perc("oe", "op", "oe"), "de": perc("de", "dp", "de"), "osr": perc("os", "op", "osr"), "dsr": perc("ds", "dp", "dsr"),
                               "ope": perc("ope", "opp", "ope"), "dpe": perc("dpe", "dpp", "dpe"),
                               "l4o": (sum(h["oe"] for h in last4) / max(1, sum(h["op"] for h in last4))) if last4 else perc("oe", "op", "oe"),
                               "l4d": (sum(h["de"] for h in last4) / max(1, sum(h["dp"] for h in last4))) if last4 else perc("de", "dp", "de"),
                               "ppg": (acc["op"] / len(hist)) if hist else pf.get("ppg", 0.0)}
            season_final[t] = {"oe": acc["oe"] / max(1, acc["op"]), "de": acc["de"] / max(1, acc["dp"]), "osr": acc["os"] / max(1, acc["op"]),
                               "dsr": acc["ds"] / max(1, acc["dp"]), "ope": acc["ope"] / max(1, acc["opp"]), "dpe": acc["dpe"] / max(1, acc["dpp"]),
                               "ppg": acc["op"] / max(1, len(hist))}
        # league-average centring per season so priors mean "vs average"
        for k in ("oe", "de", "osr", "dsr", "ope", "dpe", "ppg"):
            vals = [v[k] for v in season_final.values()]; m = sum(vals) / max(1, len(vals))
            for v in season_final.values(): v[k] -= m
        last_final = season_final
    # rating features as of each game date
    rfeat = {}
    prior = {}
    for s in sorted(games_by_season):
        gs = sorted(games_by_season[s], key=lambda g: g["order"])
        pri = {t: v * cfg["prior"] for t, v in prior.items()}
        done = []; cur_order = None; r, H = pri, cfg["hfa"]
        for g in gs:
            if g["order"] != cur_order:
                if done: r, H = srs(done, pri, cfg)
                cur_order = g["order"]
            rfeat[g["game"]] = {"rh": r.get(g["home"], 0.0), "ra": r.get(g["away"], 0.0), "H": H, "neu": g["neu"]}
            if g.get("hs") is not None: done.append((g["home"], g["away"], g["hs"], g["as"], g["neu"]))
        if done: prior, _ = srs(done, pri, cfg)
    team_asof.current = current
    return feats, rfeat

def points_asof(games_by_season, sport):
    """Points scored/allowed per game before each game, with last season (pulled to average) as a start."""
    cfg = CFG[sport]; out = {}; prev = {}
    for s in sorted(games_by_season):
        gs = sorted(games_by_season[s], key=lambda g: g["order"])
        lg = [g["hs"] + g["as"] for g in gs if g.get("hs") is not None]
        avg = (sum(lg) / len(lg) / 2) if lg else 22.0
        acc = defaultdict(lambda: [0.0, 0.0, 0])
        for g in gs:
            for t, o in ((g["home"], g["away"]), (g["away"], g["home"])):
                a = acc[t]; p = prev.get(t, (avg, avg)); w = 3
                out[(g["game"], t)] = ((a[0] + w * (avg + (p[0] - avg) * cfg["prior"])) / (a[2] + w), (a[1] + w * (avg + (p[1] - avg) * cfg["prior"])) / (a[2] + w))
            if g.get("hs") is not None:
                acc[g["home"]][0] += g["hs"]; acc[g["home"]][1] += g["as"]; acc[g["home"]][2] += 1
                acc[g["away"]][0] += g["as"]; acc[g["away"]][1] += g["hs"]; acc[g["away"]][2] += 1
        prev = {t: (a[0] / a[2], a[1] / a[2]) for t, a in acc.items() if a[2]}
    return out

ATS_FEATURES = ["r_edge", "epa_d", "l4_d", "sr_d", "pass_d", "sp", "rest_d", "neu", "conf"]
TOT_FEATURES = ["tot_epa", "pace", "pts_edge", "tot", "wind", "cold", "dome"]

def game_features(g, fh, fa, rf):
    """One game's model inputs, home team's point of view."""
    margin = (0 if rf["neu"] else rf["H"]) + rf["rh"] - rf["ra"]
    x = {
        "r_edge": margin + (g.get("sp") or 0),
        "epa_d": (fh["oe"] - fh["de"]) - (fa["oe"] - fa["de"]),
        "l4_d": (fh["l4o"] - fh["l4d"]) - (fa["l4o"] - fa["l4d"]),
        "sr_d": (fh["osr"] - fh["dsr"]) - (fa["osr"] - fa["dsr"]),
        "pass_d": (fh["ope"] - fa["dpe"]) - (fa["ope"] - fh["dpe"]),
        "sp": g.get("sp") or 0,
        "rest_d": max(-7, min(7, (g.get("rest_h") or 7) - (g.get("rest_a") or 7))),
        "neu": 1 if rf["neu"] else 0,
        "conf": 1 if g.get("conf") else 0,
        "tot_epa": fh["oe"] + fa["oe"] + fh["de"] + fa["de"],
        "pace": fh["ppg"] + fa["ppg"],
        "pts_edge": (g.get("pts_proj") or g.get("tot") or 0) - (g.get("tot") or 0),
        "tot": g.get("tot") or 0,
        "wind": min(30, g.get("wind") or 0),
        "cold": 1 if (g.get("temp") is not None and g["temp"] < 40) else 0,
        "dome": 1 if g.get("dome") else 0,
    }
    return x

def predict(params, x, which):
    m = params[which]
    z = sum(m["coef"][i] * (x[f] - m["mean"][i]) / (m["std"][i] or 1) for i, f in enumerate(m["features"]))
    return m["intercept"] + z
