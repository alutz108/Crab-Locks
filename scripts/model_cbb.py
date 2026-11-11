"""
Crab Locks college basketball model inputs (shared by training and the daily update).
Possession-based efficiency, adjusted for opponent and home court, computed as of each game day.
"""
from collections import defaultdict
from datetime import date

CAP = 30
def _d(n): return date(n // 10000, n // 100 % 100, n % 100)

def match_box(games, box_rows):
    """Attach ESPN box-score stats to our games by date (±1 day) and final score."""
    idx = defaultdict(list)
    for g in games.values():
        if g.get("hs") is None: continue
        idx[(g["hs"], g["as"])].append(g)
    pairs = defaultdict(list)
    for r in box_rows: pairs[r["game_id"]].append(r)
    out = {}
    for gid, two in pairs.items():
        if len(two) != 2: continue
        a, b = two
        d = int(str(a["game_date"])[:10].replace("-", ""))
        done = False
        for x, y in ((a, b), (b, a)):
            for g in idx.get((x["team_score"], y["team_score"]), []):
                if g["game"] not in out and abs((_d(g["order"]) - _d(d)).days) <= 1:
                    out[g["game"]] = (x, y)   # x = our home team, y = our away team
                    done = True; break
            if done: break
    return out

def weekly_ratings(games_by_season, srs, cfg, step_days=7):
    """Score-based ratings as of each game, re-solved weekly (fast enough for college basketball)."""
    out = {}; prior = {}
    for s in sorted(games_by_season):
        gs = sorted(games_by_season[s], key=lambda g: g["order"])
        pri = {t: v * cfg["prior"] for t, v in prior.items()}
        done = []; r, H = pri, cfg["hfa"]; last = None
        for g in gs:
            if done and (last is None or (_d(g["order"]) - _d(last)).days >= step_days):
                r, H = srs(done, pri, cfg, iters=15); last = g["order"]
            out[g["game"]] = (0 if g.get("neu") else H) + r.get(g["home"], 0.0) - r.get(g["away"], 0.0)
            if g.get("hs") is not None: done.append((g["home"], g["away"], g["hs"], g["as"], g.get("neu")))
        if done: prior, _ = srs(done, pri, cfg, iters=15)
    return out

def team_lines(games, matched):
    """One record per team per matched game: possessions, points, four factors (offense and defense)."""
    recs = []
    for gid, (h, a) in matched.items():
        g = games[gid]
        def poss(t): return (t["field_goals_attempted"] or 0) - (t["offensive_rebounds"] or 0) + (t["turnovers"] or 0) + 0.475 * (t["free_throws_attempted"] or 0)
        P = (poss(h) + poss(a)) / 2
        if P <= 40: continue
        for me, op, home in ((h, a, 1), (a, h, -1)):
            fga, ofga = me["field_goals_attempted"] or 0, op["field_goals_attempted"] or 0
            recs.append({"game": gid, "season": g["season"], "order": g["order"], "team": g["home"] if home == 1 else g["away"], "opp": g["away"] if home == 1 else g["home"],
                         "site": 0 if g.get("neu") else home, "poss": P, "pts": me["team_score"], "opp_pts": op["team_score"],
                         "efg": ((me["field_goals_made"] or 0) + 0.5 * (me["three_point_field_goals_made"] or 0)) / max(1, fga),
                         "oefg": ((op["field_goals_made"] or 0) + 0.5 * (op["three_point_field_goals_made"] or 0)) / max(1, ofga),
                         "tov": (me["turnovers"] or 0) / P, "otov": (op["turnovers"] or 0) / P,
                         "orb": (me["offensive_rebounds"] or 0) / max(1, (me["offensive_rebounds"] or 0) + (op["defensive_rebounds"] or 0)),
                         "drb": (me["defensive_rebounds"] or 0) / max(1, (me["defensive_rebounds"] or 0) + (op["offensive_rebounds"] or 0)),
                         "ftr": (me["free_throws_attempted"] or 0) / max(1, fga), "oftr": (op["free_throws_attempted"] or 0) / max(1, ofga)})
    return recs

def solve_eff(lines, prior_o, prior_d, prior_w=4.0, hca=3.0, iters=25):
    """Additive opponent/site-adjusted offense and defense (points per 100 possessions)."""
    if not lines: return {}, {}, 100.0
    lg = sum(l["pts"] / l["poss"] * 100 for l in lines) / len(lines)
    teams = {l["team"] for l in lines} | {l["opp"] for l in lines}
    o = {t: prior_o.get(t, 0.0) for t in teams}; d = {t: prior_d.get(t, 0.0) for t in teams}
    by = defaultdict(list)
    for l in lines: by[l["team"]].append(l)
    for _ in range(iters):
        no, nd = {}, {}
        for t in teams:
            L = by.get(t, [])
            so = sum((l["pts"] / l["poss"] * 100 - lg) - d.get(l["opp"], 0.0) - l["site"] * hca / 2 for l in L)
            sd = sum((l["opp_pts"] / l["poss"] * 100 - lg) - o.get(l["opp"], 0.0) + l["site"] * hca / 2 for l in L)
            no[t] = (so + prior_w * prior_o.get(t, 0.0)) / (len(L) + prior_w)
            nd[t] = (sd + prior_w * prior_d.get(t, 0.0)) / (len(L) + prior_w)
        mo = sum(no.values()) / len(no); md = sum(nd.values()) / len(nd)
        o = {t: v - mo for t, v in no.items()}; d = {t: v - md for t, v in nd.items()}
    return o, d, lg

def asof(lines, step_days=7, regress=0.6):
    """
    For every game, each team's inputs before that game. Returns ({(game, team): feats}, current {(season, team): feats}, league info).
    """
    by_season = defaultdict(list)
    for l in lines: by_season[l["season"]].append(l)
    feats, current = {}, {}
    prev_o, prev_d, prev_ff = {}, {}, {}
    for s in sorted(by_season):
        L = sorted(by_season[s], key=lambda l: l["order"])
        po = {t: v * regress for t, v in prev_o.items()}; pdd = {t: v * regress for t, v in prev_d.items()}
        done = []; o, d, lg = po, pdd, 100.0; last_solve = None
        acc = defaultdict(lambda: defaultdict(float)); hist = defaultdict(list)
        dates = sorted({l["order"] for l in L})
        by_date = defaultdict(list)
        for l in L: by_date[l["order"]].append(l)
        for dt in dates:
            if done and (last_solve is None or (_d(dt) - _d(last_solve)).days >= step_days):
                o, d, lg = solve_eff(done, po, pdd); last_solve = dt
            for l in by_date[dt]:
                t = l["team"]; a = acc[t]; n = a["n"]; pf = prev_ff.get(t, {})
                def ff(k, dflt):
                    w = 3.0; p = dflt + (pf.get(k, dflt) - dflt) * regress
                    return (a[k] + w * p) / (n + w)
                last5 = hist[t][-5:]
                feats[(l["game"], t)] = {"n": n, "o": o.get(t, po.get(t, 0.0)), "d": d.get(t, pdd.get(t, 0.0)), "lg": lg,
                    "pace": (a["poss"] / n) if n else pf.get("pace", 68.0),
                    "efg": ff("efg", 0.5), "oefg": ff("oefg", 0.5), "tov": ff("tov", 0.18), "otov": ff("otov", 0.18),
                    "orb": ff("orb", 0.29), "drb": ff("drb", 0.71), "ftr": ff("ftr", 0.33), "oftr": ff("oftr", 0.33),
                    "l5": (sum(x for x in last5) / len(last5)) if last5 else 0.0}
            for l in by_date[dt]:
                t = l["team"]; a = acc[t]; a["n"] += 1; a["poss"] += l["poss"]
                for k in ("efg", "oefg", "tov", "otov", "orb", "drb", "ftr", "oftr"): a[k] += l[k]
                hist[t].append((l["pts"] - l["opp_pts"]) / l["poss"] * 100)
                done.append(l)
        o, d, lg = solve_eff(done, po, pdd)
        for t, a in acc.items():
            n = a["n"]; last5 = hist[t][-5:]
            current[(s, t)] = {"n": n, "o": o.get(t, 0.0), "d": d.get(t, 0.0), "lg": lg, "pace": a["poss"] / max(1, n),
                               **{k: a[k] / max(1, n) for k in ("efg", "oefg", "tov", "otov", "orb", "drb", "ftr", "oftr")},
                               "l5": (sum(last5) / len(last5)) if last5 else 0.0}
        prev_o, prev_d = o, d
        prev_ff = {t: {**{k: a[k] / max(1, a["n"]) for k in ("efg", "oefg", "tov", "otov", "orb", "drb", "ftr", "oftr")}, "pace": a["poss"] / max(1, a["n"])} for t, a in acc.items()}
    return feats, current

ATS = ["eff_edge", "r_edge", "ff_efg", "ff_tov", "ff_orb", "ff_ftr", "l5_d", "sp", "rest_d", "neu", "conf"]
TOT = ["tot_edge", "pace", "tot", "efg_sum", "neu"]

def features(g, fh, fa, rating_margin, hca=3.0):
    pace = (fh["pace"] + fa["pace"]) / 2
    site = 0 if g.get("neu") else 1
    m_eff = ((fh["o"] + fa["d"]) - (fa["o"] + fh["d"])) * pace / 100 + site * hca * pace / 100
    lg = fh["lg"]
    pts_h = (lg + fh["o"] + fa["d"] + site * hca / 2) * pace / 100
    pts_a = (lg + fa["o"] + fh["d"] - site * hca / 2) * pace / 100
    sp, tot = g.get("sp") or 0, g.get("tot") or 0
    return {"eff_edge": m_eff + sp, "r_edge": rating_margin + sp,
            "ff_efg": (fh["efg"] - fh["oefg"]) - (fa["efg"] - fa["oefg"]), "ff_tov": (fa["tov"] - fa["otov"]) - (fh["tov"] - fh["otov"]),
            "ff_orb": (fh["orb"] + fh["drb"]) - (fa["orb"] + fa["drb"]), "ff_ftr": (fh["ftr"] - fh["oftr"]) - (fa["ftr"] - fa["oftr"]),
            "l5_d": fh["l5"] - fa["l5"], "sp": sp, "rest_d": max(-7, min(7, (g.get("rest_h") or 3) - (g.get("rest_a") or 3))), "neu": 1 - site, "conf": 1 if g.get("conf") else 0,
            "tot_edge": (pts_h + pts_a) - tot, "pace": pace, "tot": tot, "efg_sum": fh["efg"] + fa["efg"] + fh["oefg"] + fa["oefg"],
            "m_eff": m_eff, "pts_proj": pts_h + pts_a}
