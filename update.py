#!/usr/bin/env python3
"""
Crab Locks data updater. Runs on GitHub Actions (see .github/workflows/update.yml).
Standard-library Python only; nothing to install.

Sources
  NFL    nflverse games.csv (closing lines, juice, weather), no key.
         Opening lines: Sportsbook Review archive 2011-2021 (data/static),
         then captured going forward from The Odds API.
  NBA    Built-in history through 2025-26 (data/base/NBA.json).
         Current season: The Odds API (lines, captured openers, scores).
  NCAAF  CollegeFootballData.com /lines + /games (key: CFBD_API_KEY).
  NCAAB  CollegeBasketballData.com /lines + /games (same key).

Keys come from environment variables CFBD_API_KEY and ODDS_API_KEY
(GitHub repository secrets). A missing key just skips that source.

Options
  --sports NFL,NCAAF,NBA,NCAAB   limit to some sports
  --odds-only                    only take an Odds API snapshot (afternoon run)
  --test                         fetch a little from each source, print it, save nothing
"""
import argparse, csv, io, json, os, statistics, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-5))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CFBD_KEY = os.environ.get("CFBD_API_KEY", "").strip()
ODDS_KEY = os.environ.get("ODDS_API_KEY", "").strip()
NOW = datetime.now(timezone.utc)
TODAY = NOW.astimezone(ET).date()
LOG = []
STATS = {"cfbd_calls": 0, "odds_credits_left": None}

def say(msg):
    print(msg, flush=True)
    LOG.append(msg)

# ------------------------------------------------------------------ helpers
def http_json(url, headers=None, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "crab-locks-updater", "Accept": "application/json", **(headers or {})})
            with urllib.request.urlopen(req, timeout=60) as r:
                rem = r.headers.get("x-requests-remaining")
                if rem is not None:
                    STATS["odds_credits_left"] = rem
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (401, 403):
                body = e.read().decode("utf-8", "ignore")[:200]
                raise RuntimeError(f"{last} (check the API key): {body}")
            if e.code == 429:
                raise RuntimeError("HTTP 429: monthly or rate limit reached")
            time.sleep(3 * (i + 1))
        except Exception as e:
            last = str(e)
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"request failed: {last}")

def http_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "crab-locks-updater"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode("utf-8")

def med(vals, price=False):
    v = [x for x in vals if isinstance(x, (int, float))]
    if not v:
        return None
    if price:
        # median in probability space so -105 and +102 average sensibly
        v = [x for x in v if abs(x) >= 100]
        if not v:
            return None
        p = statistics.median([-x / (-x + 100) if x < 0 else 100 / (x + 100) for x in v])
        return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)
    m = statistics.median(v)
    return round(m * 2) / 2

def clean(x):
    if x is None:
        return None
    x = round(float(x), 1)
    return int(x) if x.is_integer() else x

def ymd(iso):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return int(t.astimezone(ET).strftime("%Y%m%d"))
    except Exception:
        return None

def season_of(sport, d):
    if isinstance(d, int):
        d = date(d // 10000, d // 100 % 100, d % 100)
    if sport in ("NBA", "NCAAB"):
        return d.year if d.month >= 8 else d.year - 1
    return d.year if d.month >= 6 else d.year - 1

def load(path, default):
    p = os.path.join(DATA, path)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return default

def save(path, obj, pretty=False):
    p = os.path.join(DATA, path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(obj, f, indent=1 if pretty else None, separators=None if pretty else (",", ":"))

KEEP = ["id", "date", "season", "week", "post", "home", "away", "hs", "as", "neu", "div", "hc", "ac",
        "sp", "spO", "tot", "totO", "hml", "aml", "hso", "aso", "oo", "uo"]

def save_season(sport, season, games, source):
    rows = []
    for g in sorted(games, key=lambda g: (g["date"], str(g.get("id")))):
        r = {}
        for k in KEEP:
            v = g.get(k)
            if v is None or v is False:
                continue
            r[k] = clean(v) if isinstance(v, float) else v
        rows.append(r)
    save(f"api/{sport}/{season}.json", {"sport": sport, "season": season, "source": source,
                                        "updated": NOW.isoformat(timespec="seconds"), "games": rows})

def check_sign(games, label):
    """Make sure spreads are from the home team's side (negative = home favored)."""
    pts = [(g["sp"], g["hs"] - g["as"]) for g in games if g.get("sp") is not None and g.get("hs") is not None]
    if len(pts) < 50:
        return games
    agree = sum(1 for s, m in pts if s != 0 and m != 0 and (s < 0) == (m > 0))
    tot = sum(1 for s, m in pts if s != 0 and m != 0)
    if tot and agree / tot < 0.4:
        say(f"  ! {label}: spreads looked flipped ({agree}/{tot} favorites won); flipping signs")
        for g in games:
            for k in ("sp", "spO"):
                if g.get(k) is not None:
                    g[k] = -g[k]
    return games

# ------------------------------------------------------------------ CFBD / CBBD
def cfbd(base, path, params):
    if not CFBD_KEY:
        raise RuntimeError("no CFBD_API_KEY")
    STATS["cfbd_calls"] += 1
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    return http_json(url, {"Authorization": f"Bearer {CFBD_KEY}"}) or []

def lines_to_game(x, sport, extra=None):
    ls = x.get("lines") or []
    sp = med([l.get("spread") for l in ls])
    spO = med([l.get("spreadOpen") for l in ls])
    g = {
        "id": x.get("id", x.get("gameId")),
        "date": ymd(x.get("startDate")),
        "week": x.get("week") if sport == "NCAAF" else None,
        "post": str(x.get("seasonType", "regular")).lower() not in ("regular", "1"),
        "home": x.get("homeTeam"), "away": x.get("awayTeam"),
        "hs": x.get("homeScore", x.get("homePoints")), "as": x.get("awayScore", x.get("awayPoints")),
        "hc": x.get("homeConference"), "ac": x.get("awayConference"),
        "sp": sp, "spO": spO,
        "tot": med([l.get("overUnder") for l in ls]), "totO": med([l.get("overUnderOpen") for l in ls]),
        "hml": med([l.get("homeMoneyline") for l in ls if abs(l.get("homeMoneyline") or 0) >= 100], price=True),
        "aml": med([l.get("awayMoneyline") for l in ls if abs(l.get("awayMoneyline") or 0) >= 100], price=True),
    }
    if extra:
        g["neu"] = bool(extra.get("neutralSite"))
        g["div"] = bool(extra.get("conferenceGame"))
        if g["hs"] is None:
            g["hs"] = extra.get("homePoints", extra.get("homeScore"))
            g["as"] = extra.get("awayPoints", extra.get("awayScore"))
    elif g["hc"] and g["ac"]:
        g["div"] = g["hc"] == g["ac"]
    if g["hs"] is None or g["as"] is None:
        g["hs"] = g["as"] = None
    if g["date"]:
        g["season"] = season_of(sport, g["date"])
    return g

CFB = "https://api.collegefootballdata.com"
CBB = "https://api.collegebasketballdata.com"

def pull_cfb_season(season, test=False, current=False):
    games = {}
    types = ("regular", "postseason")
    if current and TODAY.month in (6, 7, 8, 9, 10, 11):
        types = ("regular",)  # bowls start in December
    for st in types:
        info = {x.get("id"): x for x in cfbd(CFB, "/games", {"year": season, "seasonType": st})}
        lines = cfbd(CFB, "/lines", {"year": season, "seasonType": st})
        for x in lines:
            g = lines_to_game(x, "NCAAF", info.get(x.get("id")))
            if g["date"] and (g["sp"] is not None or g["tot"] is not None):
                g["season"] = season
                games[g["id"]] = g
        if test:
            say(f"  NCAAF {season} {st}: {len(lines)} games with lines. Sample: {json.dumps(lines[:1])[:600]}")
            return list(games.values())
    return check_sign(list(games.values()), f"NCAAF {season}")

def cbb_windows(season):
    """Date windows (each well under the 3,000-row cap) covering a season."""
    y = season
    edges = [date(y, 10, 25), date(y, 12, 1), date(y + 1, 1, 1), date(y + 1, 2, 1), date(y + 1, 3, 1), date(y + 1, 4, 15)]
    return [(edges[i], edges[i + 1] - timedelta(days=1)) for i in range(len(edges) - 1)]

def pull_cbb_range(a, b, depth=0):
    p = {"startDateRange": f"{a:%Y-%m-%d}", "endDateRange": f"{b:%Y-%m-%d}T23:59:59"}
    lines = cfbd(CBB, "/lines", p)
    if len(lines) >= 2990 and (b - a).days > 1 and depth < 4:
        mid = a + (b - a) / 2
        return pull_cbb_range(a, mid, depth + 1) + pull_cbb_range(mid + timedelta(days=1), b, depth + 1)
    info = {}
    try:
        gl = cfbd(CBB, "/games", p)
        if len(gl) < 2990:
            info = {x.get("id"): x for x in gl}
    except RuntimeError as e:
        say(f"  ! CBBD games lookup skipped: {e}")
    out = []
    for x in lines:
        g = lines_to_game(x, "NCAAB", info.get(x.get("gameId", x.get("id"))))
        if g["date"] and (g["sp"] is not None or g["tot"] is not None):
            out.append(g)
    return out

def pull_cbb_recent(season):
    """Current season: only re-pull the last few days and the next ten, merged into the saved file."""
    games = {g["id"]: g for g in load(f"api/NCAAB/{season}.json", {"games": []})["games"]}
    if not games:
        return pull_cbb_season(season)
    for g in pull_cbb_range(TODAY - timedelta(days=5), TODAY + timedelta(days=10)):
        if g["season"] == season:
            games[g["id"]] = g
    return check_sign(list(games.values()), f"NCAAB {season}")

def pull_cbb_season(season, test=False):
    games = {}
    for a, b in cbb_windows(season):
        if a > TODAY + timedelta(days=10):
            break
        for g in pull_cbb_range(a, min(b, TODAY + timedelta(days=10))):
            if g["season"] == season:
                games[g["id"]] = g
        if test:
            say(f"  NCAAB {season} {a}..{b}: {len(games)} games with lines. Sample: {json.dumps(next(iter(games.values()), {}))[:500]}")
            return list(games.values())
    return check_sign(list(games.values()), f"NCAAB {season}")

# ------------------------------------------------------------------ The Odds API (NFL, NBA)
ODDS_SPORT = {"NFL": "americanfootball_nfl", "NBA": "basketball_nba"}
NFL_ABBR = {"Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LA", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS"}
NBA_SHORT = {"Atlanta Hawks": "Hawks", "Boston Celtics": "Celtics", "Brooklyn Nets": "Nets", "Charlotte Hornets": "Hornets",
    "Chicago Bulls": "Bulls", "Cleveland Cavaliers": "Cavaliers", "Dallas Mavericks": "Mavericks", "Denver Nuggets": "Nuggets",
    "Detroit Pistons": "Pistons", "Golden State Warriors": "Warriors", "Houston Rockets": "Rockets", "Indiana Pacers": "Pacers",
    "Los Angeles Clippers": "Clippers", "LA Clippers": "Clippers", "Los Angeles Lakers": "Lakers", "Memphis Grizzlies": "Grizzlies",
    "Miami Heat": "Heat", "Milwaukee Bucks": "Bucks", "Minnesota Timberwolves": "Timberwolves", "New Orleans Pelicans": "Pelicans",
    "New York Knicks": "Knicks", "Oklahoma City Thunder": "Thunder", "Orlando Magic": "Magic", "Philadelphia 76ers": "76ers",
    "Phoenix Suns": "Suns", "Portland Trail Blazers": "Trail Blazers", "Sacramento Kings": "Kings", "San Antonio Spurs": "Spurs",
    "Toronto Raptors": "Raptors", "Utah Jazz": "Jazz", "Washington Wizards": "Wizards"}

def short_name(sport, full):
    if sport == "NFL":
        return NFL_ABBR.get(full, full)
    return NBA_SHORT.get(full, full.split()[-1] if full else full)

def odds_snapshot(sport, ev):
    """Consensus (median across US books) for one event, home team's side."""
    home, away = ev["home_team"], ev["away_team"]
    sp, hso, aso, tot, oo, uo, hml, aml = [], [], [], [], [], [], [], []
    for bk in ev.get("bookmakers", []):
        for m in bk.get("markets", []):
            oc = {o.get("name"): o for o in m.get("outcomes", [])}
            if m.get("key") == "spreads" and home in oc:
                sp.append(oc[home].get("point")); hso.append(oc[home].get("price"))
                if away in oc:
                    aso.append(oc[away].get("price"))
            elif m.get("key") == "totals" and "Over" in oc:
                tot.append(oc["Over"].get("point")); oo.append(oc["Over"].get("price"))
                if "Under" in oc:
                    uo.append(oc["Under"].get("price"))
            elif m.get("key") == "h2h" and home in oc and away in oc:
                hml.append(oc[home].get("price")); aml.append(oc[away].get("price"))
    snap = {"sp": med(sp), "tot": med(tot), "hml": med(hml, price=True), "aml": med(aml, price=True), "hso": med(hso, price=True), "aso": med(aso, price=True),
            "oo": med(oo, price=True), "uo": med(uo, price=True), "books": len(ev.get("bookmakers", [])), "at": NOW.isoformat(timespec="minutes")}
    return snap if any(snap[k] is not None for k in ("sp", "tot", "hml")) else None

def odds_update(sport, with_scores, test=False):
    if not ODDS_KEY:
        say(f"  {sport}: no ODDS_API_KEY, skipping The Odds API")
        return None
    key = ODDS_SPORT[sport]
    track = load(f"odds/{sport}.json", {"events": {}})
    evs = http_json(f"https://api.the-odds-api.com/v4/sports/{key}/odds?" + urllib.parse.urlencode(
        {"apiKey": ODDS_KEY, "regions": "us", "markets": "h2h,spreads,totals", "oddsFormat": "american", "dateFormat": "iso"}))
    n_new = 0
    for ev in evs or []:
        snap = odds_snapshot(sport, ev)
        if not snap:
            continue
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if start <= NOW:
            continue  # game already started: these are live odds, not pregame
        e = track["events"].setdefault(ev["id"], {"start": ev["commence_time"], "date": ymd(ev["commence_time"]),
                                                   "home": short_name(sport, ev["home_team"]), "away": short_name(sport, ev["away_team"])})
        e["start"], e["date"] = ev["commence_time"], ymd(ev["commence_time"])
        if "open" not in e:
            e["open"] = snap
            n_new += 1
        e["last"] = snap
    if with_scores:
        sc = http_json(f"https://api.the-odds-api.com/v4/sports/{key}/scores?" + urllib.parse.urlencode(
            {"apiKey": ODDS_KEY, "daysFrom": 3, "dateFormat": "iso"}))
        for ev in sc or []:
            if not ev.get("completed") or ev["id"] not in track["events"]:
                continue
            s = {x.get("name"): x.get("score") for x in (ev.get("scores") or [])}
            try:
                track["events"][ev["id"]]["hs"] = int(s[ev["home_team"]])
                track["events"][ev["id"]]["as"] = int(s[ev["away_team"]])
            except (KeyError, TypeError, ValueError):
                pass
    say(f"  {sport}: {len(evs or [])} upcoming games priced, {n_new} new openers captured. Odds API credits left: {STATS['odds_credits_left']}")
    if test:
        if evs:
            say(f"  sample: {json.dumps(odds_snapshot(sport, evs[0]))}  ({evs[0]['away_team']} @ {evs[0]['home_team']})")
        return track
    # drop events from finished seasons we no longer need to track (keep ~ 1 year)
    cutoff = int((TODAY - timedelta(days=400)).strftime("%Y%m%d"))
    track["events"] = {k: v for k, v in track["events"].items() if (v.get("date") or 0) >= cutoff}
    save(f"odds/{sport}.json", track)
    return track

def nba_from_odds(track):
    """Turn tracked NBA events into season files (current season and on)."""
    by = {}
    for eid, e in track["events"].items():
        if not e.get("date"):
            continue
        s = season_of("NBA", e["date"])
        if s < 2026:
            continue
        o, l = e.get("open") or {}, e.get("last") or {}
        by.setdefault(s, []).append({"id": eid, "date": e["date"], "home": e["home"], "away": e["away"],
            "hs": e.get("hs"), "as": e.get("as"), "sp": l.get("sp"), "spO": o.get("sp"), "tot": l.get("tot"), "totO": o.get("tot"),
            "hml": l.get("hml"), "aml": l.get("aml"), "hso": l.get("hso"), "aso": l.get("aso"), "oo": l.get("oo"), "uo": l.get("uo")})
    for s, games in by.items():
        save_season("NBA", s, games, "The Odds API (consensus of US books; opener = first daily snapshot)")
        say(f"  NBA {s}: {len(games)} games saved from The Odds API")

# ------------------------------------------------------------------ NFL base
NFLV = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
EMB = ['season','week','post','date','home','away','hs','as','sp','spO','tot','totO','hml','aml','hso','aso','oo','uo','div','neu','temp','wind','hc','ac']

def pack(games):
    teams = sorted({g["home"] for g in games} | {g["away"] for g in games})
    ti = {t: i for i, t in enumerate(teams)}
    rows = []
    for g in games:
        r = []
        for f in EMB:
            v = g.get(f)
            if f in ("home", "away"):
                v = ti[v]
            elif f in ("hc", "ac"):
                v = None
            elif isinstance(v, bool):
                v = 1 if v else 0
            elif isinstance(v, float):
                v = clean(v)
            r.append(v)
        while r and r[-1] is None:
            r.pop()
        rows.append(r)
    return {"teams": teams, "confs": [], "rows": rows}

def shift(d, k):
    t = date(d // 10000, d // 100 % 100, d % 100) + timedelta(days=k)
    return int(t.strftime("%Y%m%d"))

def build_nfl_base(track):
    try:
        text = http_text(NFLV)
    except Exception as e:
        say(f"  ! couldn't download nflverse games.csv ({e}); keeping the existing NFL file")
        return
    sbr = {}
    for d, hs, as_, spO, totO, _ in load("static/nfl_openers_sbr.json", {"rows": []})["rows"]:
        sbr[(d, hs, as_)] = (spO, totO)
    cap = {}
    for e in (track or load("odds/NFL.json", {"events": {}}))["events"].values():
        if e.get("open") and e.get("date"):
            cap[(e["date"], e["home"])] = e
    games, n_sbr, n_cap = [], 0, 0
    for x in csv.DictReader(io.StringIO(text)):
        if int(x["season"]) < 2006:
            continue
        n = lambda k: None if x.get(k) in ("", "NA", None) else float(x[k])
        sp = n("spread_line")
        g = dict(season=int(x["season"]), week=int(x["week"]), post=x["game_type"] != "REG",
                 date=int(x["gameday"].replace("-", "")), home=x["home_team"], away=x["away_team"],
                 hs=n("home_score"), sp=-sp if sp is not None else None, tot=n("total_line"),
                 hml=n("home_moneyline"), aml=n("away_moneyline"), hso=n("home_spread_odds"),
                 aso=n("away_spread_odds"), oo=n("over_odds"), uo=n("under_odds"),
                 div=x["div_game"] == "1", neu=x["location"] == "Neutral", temp=n("temp"), wind=n("wind"))
        g["as"] = n("away_score")
        hit = None
        if g["hs"] is not None:
            for k in (0, -1, 1):
                hit = sbr.get((shift(g["date"], k), int(g["hs"]), int(g["as"])))
                if hit:
                    break
        if hit:
            g["spO"], g["totO"] = hit
            n_sbr += 1
        else:
            for k in (0, -1, 1):
                e = cap.get((shift(g["date"], k), g["home"]))
                if e:
                    o, l = e.get("open") or {}, e.get("last") or {}
                    g["spO"], g["totO"] = o.get("sp"), o.get("tot")
                    for f in ("sp", "tot", "hml", "aml"):
                        if g.get(f) is None and l.get(f) is not None:
                            g[f] = l[f]
                    n_cap += 1
                    break
        games.append(g)
    save("base/NFL.json", pack(games))
    say(f"  NFL rebuilt from nflverse: {len(games)} games; opening lines from the SBR archive on {n_sbr}, captured on {n_cap}")

# ------------------------------------------------------------------ index
def write_index(state):
    idx = {"updated": NOW.isoformat(timespec="seconds"), "sports": {}, "notes": state.get("notes", {})}
    for sport in ("NFL", "NCAAF", "NBA", "NCAAB"):
        ent = {"base": os.path.exists(os.path.join(DATA, "base", f"{sport}.json")), "api": []}
        d = os.path.join(DATA, "api", sport)
        if os.path.isdir(d):
            ent["api"] = sorted(int(f[:-5]) for f in os.listdir(d) if f.endswith(".json"))
        idx["sports"][sport] = ent
    save("index.json", idx, pretty=True)

# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sports", default="NFL,NCAAF,NBA,NCAAB")
    ap.add_argument("--odds-only", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    sports = [s.strip().upper() for s in args.sports.split(",")]
    state = load("state.json", {"done": {}, "notes": {}})
    state.setdefault("done", {}); state.setdefault("notes", {})
    say(f"Crab Locks update {NOW.astimezone(ET):%Y-%m-%d %H:%M} ET. Keys: CFBD {'yes' if CFBD_KEY else 'MISSING'}, Odds API {'yes' if ODDS_KEY else 'MISSING'}")
    failures = 0

    def guard(label, fn):
        nonlocal failures
        try:
            return fn()
        except Exception as e:
            failures += 1
            say(f"  ! {label} failed: {e}")
            return None

    # The Odds API: NFL + NBA snapshots
    tracks = {}
    for sp in ("NFL", "NBA"):
        if sp in sports:
            tracks[sp] = guard(f"{sp} odds", lambda sp=sp: odds_update(sp, with_scores=(sp == "NBA" and not args.odds_only), test=args.test))
    if args.test:
        if "NCAAF" in sports:
            guard("NCAAF test", lambda: pull_cfb_season(season_of("NCAAF", TODAY), test=True))
        if "NCAAB" in sports:
            guard("NCAAB test", lambda: pull_cbb_season(season_of("NCAAB", TODAY), test=True))
        say(f"Test finished. Nothing was saved. CFBD/CBBD calls used: {STATS['cfbd_calls']}")
        return
    if tracks.get("NBA"):
        guard("NBA season files", lambda: nba_from_odds(tracks["NBA"]))
    if "NFL" in sports and not args.odds_only:
        guard("NFL rebuild", lambda: build_nfl_base(tracks.get("NFL")))

    if not args.odds_only:
        for sp, pull, first in (("NCAAF", pull_cfb_season, 2013), ("NCAAB", pull_cbb_season, 2013)):
            if sp not in sports:
                continue
            if not CFBD_KEY:
                say(f"  {sp}: no CFBD_API_KEY, skipping")
                continue
            cur = season_of(sp, TODAY)
            todo = [cur] + [s for s in range(cur - 1, first - 1, -1) if s not in state["done"].get(sp, [])][:4]
            for s in todo:
                if s == cur:
                    fn = (lambda: pull_cfb_season(s, current=True)) if sp == "NCAAF" else (lambda: pull_cbb_recent(s))
                else:
                    fn = lambda s=s: pull(s)
                games = guard(f"{sp} {s}", fn)
                if games is None:
                    break
                save_season(sp, s, games, "CollegeFootballData.com" if sp == "NCAAF" else "CollegeBasketballData.com")
                op = sum(1 for g in games if g.get("spO") is not None)
                say(f"  {sp} {s}: {len(games)} games with lines, {op} with opening spreads")
                if s != cur:
                    state["done"].setdefault(sp, []).append(s)

    save("state.json", state, pretty=True)
    write_index(state)
    say(f"Done. CFBD/CBBD calls this run: {STATS['cfbd_calls']}. Odds API credits left: {STATS['odds_credits_left']}")
    if failures and failures >= len(sports):
        sys.exit(1)

if __name__ == "__main__":
    main()
