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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
PLAYBOOK_KEY = os.environ.get("PLAYBOOK_API_KEY", "").strip()
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

# ------------------------------------------------------------------ NBA catch-up from ESPN
# The Odds API's free plan only covers games that haven't started, so games
# played earlier this season are filled in from ESPN's public feeds (DraftKings
# opening and closing lines). ESPN blocks bursts of requests from cloud
# servers, so this goes slowly and only does a limited number per run.
ESPN_BUDGET = int(os.environ.get("ESPN_BUDGET", "250"))  # ESPN odds requests per morning run
ESPN_SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}"
ESPN_ODDS = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{e}/competitions/{c}/odds"

def _num(v, odds=False):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, dict):
        for k in ("american", "alternateDisplayValue", "displayValue"):
            if k in v:
                x = _num(v[k], odds)
                if x is not None:
                    return x
        return float(v["value"]) if not odds and isinstance(v.get("value"), (int, float)) else None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().lower().replace("\u2212", "-")
    if t in ("pk", "pick", "pickem"):
        return None if odds else 0.0
    if t in ("even", "ev"):
        return 100.0 if odds else None
    if t[:1] in ("o", "u"):
        t = t[1:]
    try:
        return float(t.replace("+", ""))
    except ValueError:
        return None

def _dig(d, *ks):
    for k in ks:
        d = d.get(k) if isinstance(d, dict) else None
    return d

def espn_lines(item):
    h, a = item.get("homeTeamOdds") or {}, item.get("awayTeamOdds") or {}
    def side(phase):
        sp = _num(_dig(h, phase, "pointSpread"))
        if sp is None:
            x = _num(_dig(a, phase, "pointSpread"))
            sp = -x if x is not None else None
        return {"sp": sp, "tot": _num(_dig(item, phase, "total")),
                "hml": _num(_dig(h, phase, "moneyLine"), True), "aml": _num(_dig(a, phase, "moneyLine"), True),
                "hso": _num(_dig(h, phase, "spread"), True), "aso": _num(_dig(a, phase, "spread"), True),
                "oo": _num(_dig(item, phase, "over"), True), "uo": _num(_dig(item, phase, "under"), True)}
    o, c = side("open"), side("close")
    flat = _num(item.get("spread"))
    if flat is not None and c["sp"] is None:
        c["sp"] = -abs(flat) if h.get("favorite") is True else abs(flat) if a.get("favorite") is True else flat
    if c["tot"] is None:
        c["tot"] = _num(item.get("overUnder"))
    if c["hml"] is None:
        c["hml"], c["aml"] = _num(h.get("moneyLine"), True), _num(a.get("moneyLine"), True)
    for d in (o, c):
        for k in ("hml", "aml", "hso", "aso", "oo", "uo"):
            if d.get(k) is not None and abs(d[k]) < 100:
                d[k] = None
    return o, c

class EspnRefused(Exception):
    pass

def espn_day(key):
    """Finished NBA games on one date (YYYYMMDD) from ESPN's scoreboard."""
    sb = http_json(ESPN_SB.format(d=key))
    time.sleep(1.5)
    out = []
    for ev in (sb or {}).get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        if _dig(comp, "status", "type", "completed") is not True or _dig(ev, "season", "type") not in (2, 3, 5):
            continue
        cs = comp.get("competitors", [])
        hc = next((c for c in cs if c.get("homeAway") == "home"), None)
        ac = next((c for c in cs if c.get("homeAway") == "away"), None)
        if not hc or not ac:
            continue
        try:
            hs, as_ = int(float(hc.get("score"))), int(float(ac.get("score")))
        except (TypeError, ValueError):
            continue
        out.append({"id": str(ev["id"]), "cid": str(comp.get("id", ev["id"])), "date": ymd(ev.get("date")),
                    "home": _dig(hc, "team", "shortDisplayName"), "away": _dig(ac, "team", "shortDisplayName"), "hs": hs, "as": as_,
                    "post": _dig(ev, "season", "type") != 2, "neu": bool(comp.get("neutralSite"))})
    return out

def espn_game_lines(g):
    """Best pregame provider's opening and closing lines for one game. Raises EspnRefused if ESPN says no."""
    try:
        js = http_json(ESPN_ODDS.format(e=g["id"], c=g["cid"]))
    except RuntimeError as e:
        raise EspnRefused(str(e))
    time.sleep(1.5)
    items = []
    for it in (js or {}).get("items", []):
        if isinstance(it, dict) and set(it) == {"$ref"}:
            try:
                it = http_json(it["$ref"].replace("http://", "https://")); time.sleep(1.5)
            except RuntimeError:
                continue
        items.append(it)
    best = None
    for it in items:
        if "live" in (_dig(it, "provider", "name") or "").lower():
            continue
        o, c = espn_lines(it)
        score = (c["sp"] is not None) + (o["sp"] is not None) + (c["tot"] is not None) + (o["tot"] is not None)
        if score and (best is None or score > best[0]):
            best = (score, o, c, _dig(it, "provider", "name"))
    return None if not best else (best[1], best[2], best[3])

def _season_dates(season, end):
    d = date(season, 10, 1)
    last = min(end, date(season + 1, 6, 30))
    while d <= last:
        yield d
        d += timedelta(days=1)

def nba_catchup(track, state, budget, test=False):
    """This season: add ESPN's DraftKings opening/closing lines for finished games.
    Games already tracked from The Odds API get ESPN's true opener and closer swapped in."""
    season = season_of("NBA", TODAY)
    done = set(state.setdefault("nba_catchup", {}).get(str(season), []))
    by_key = {}
    for eid, e in track["events"].items():
        by_key[(e.get("date"), e.get("home"))] = e
    used, added, upgraded = 0, 0, 0
    end = TODAY - timedelta(days=1)
    try:
        for d in _season_dates(season, end):
            key = d.strftime("%Y%m%d")
            if key in done:
                continue
            complete = True
            for g in espn_day(key):
                e = by_key.get((g["date"], g["home"]))
                if e and e.get("espn"):
                    continue
                if used >= budget:
                    complete = False
                    break
                lines = espn_game_lines(g)
                used += 1
                if test:
                    say(f"  sample this season {g['away']} @ {g['home']} {g['date']}: {json.dumps(lines)} final {g['as']}-{g['hs']}")
                    return 0
                if not lines:
                    continue
                o, c, book = lines
                o = {k: v for k, v in o.items() if v is not None}
                c = {k: v for k, v in c.items() if v is not None}
                if e:
                    if o:
                        e["open"] = o
                    if c:
                        e["last"] = {**(e.get("last") or {}), **c}
                    e["hs"], e["as"], e["espn"] = g["hs"], g["as"], True
                    upgraded += 1
                else:
                    track["events"][f"espn-{g['id']}"] = {"date": g["date"], "home": g["home"], "away": g["away"], "hs": g["hs"], "as": g["as"],
                        "open": o or None, "last": c, "espn": True, "src": f"ESPN ({book})"}
                    by_key[(g["date"], g["home"])] = track["events"][f"espn-{g['id']}"]
                    added += 1
            if not complete:
                break
            done.add(key)
    except EspnRefused as e:
        say(f"  ! ESPN refused a request ({e}); stopping for today, continuing tomorrow")
        used = budget
    state["nba_catchup"][str(season)] = sorted(done)
    say(f"  NBA this season from ESPN: {added} games added, {upgraded} given true opening/closing lines ({used} requests)")
    return max(0, budget - used)

NBA_BACKFILL = (2025, 2024, 2023, 2022)  # 2025-26 back to 2022-23

def nba_backfill(state, budget, test=False):
    """Past seasons missing opening lines and moneylines: pull them from ESPN a few hundred games a day."""
    prog = state.setdefault("nba_backfill", {})
    used = 0
    for season in NBA_BACKFILL:
        info = prog.setdefault(str(season), {"done": [], "finished": False})
        if info.get("finished") and not test:
            continue
        path = f"archive/NBA/{season}.json"
        saved = load(path, {"games": []})
        games = {g["id"]: g for g in saved["games"]}
        done = set(info["done"])
        end = min(TODAY - timedelta(days=1), date(season + 1, 6, 30))
        try:
            for d in _season_dates(season, end):
                key = d.strftime("%Y%m%d")
                if key in done:
                    continue
                if used >= budget:
                    break
                complete = True
                for g in espn_day(key):
                    if g["id"] in games:
                        continue
                    if used >= budget:
                        complete = False
                        break
                    lines = espn_game_lines(g)
                    used += 1
                    if test:
                        say(f"  sample {season}-{str(season+1)[-2:]} {g['away']} @ {g['home']} {g['date']}: {json.dumps(lines)} final {g['as']}-{g['hs']}")
                        return
                    if not lines:
                        continue
                    o, c, book = lines
                    games[g["id"]] = {"id": g["id"], "date": g["date"], "season": season, "home": g["home"], "away": g["away"],
                        "hs": g["hs"], "as": g["as"], "post": g["post"], "neu": g["neu"],
                        "sp": c.get("sp"), "spO": o.get("sp"), "tot": c.get("tot"), "totO": o.get("tot"),
                        "hml": c.get("hml"), "aml": c.get("aml"), "hso": c.get("hso"), "aso": c.get("aso"), "oo": c.get("oo"), "uo": c.get("uo")}
                if not complete:
                    break
                done.add(key)
                if test:
                    continue
                if len(done) % 5 == 0:
                    save(path, {"sport": "NBA", "season": season, "source": "ESPN (DraftKings)", "updated": NOW.isoformat(timespec="seconds"),
                                "games": [{k: v for k, v in g.items() if v is not None and v is not False} for g in games.values()]})
            else:
                info["finished"] = True
        except EspnRefused as e:
            say(f"  ! ESPN refused a request ({e}); stopping for today, continuing tomorrow")
            used = budget
        info["done"] = sorted(done)
        if games and not test:
            save(path, {"sport": "NBA", "season": season, "source": "ESPN (DraftKings)", "updated": NOW.isoformat(timespec="seconds"),
                        "games": [{k: v for k, v in g.items() if v is not None and v is not False} for g in games.values()]})
        withO = sum(1 for g in games.values() if g.get("spO") is not None)
        if not test:
            say(f"  NBA {season}-{str(season+1)[-2:]} from ESPN: {len(games)} games saved, {withO} with opening lines" + (" (season complete)" if info.get("finished") else ""))
        if used >= budget:
            break
    return

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
        by.setdefault(s, []).append({"id": eid, "date": e["date"], "season": s, "home": e["home"], "away": e["away"],
            "hs": e.get("hs"), "as": e.get("as"), "sp": l.get("sp"), "spO": o.get("sp"), "tot": l.get("tot"), "totO": o.get("tot"),
            "hml": l.get("hml"), "aml": l.get("aml"), "hso": l.get("hso"), "aso": l.get("aso"), "oo": l.get("oo"), "uo": l.get("uo")})
    for s, games in by.items():
        save_season("NBA", s, games, "The Odds API (consensus of US books; opener = first daily snapshot) and ESPN (DraftKings) for games before tracking began")
        say(f"  NBA {s}: {len(games)} games saved")

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

# ------------------------------------------------------------------ Sportsbook Review archive (via archive.org)
# Sportsbook Review published season spreadsheets with opening and closing lines
# until 2023. The site took them down, but the Internet Archive kept copies.
# This one-time import downloads those copies, reads them, and saves games with
# opening and closing spreads and totals, moneylines and final scores.
import zipfile, re
import xml.etree.ElementTree as ET_XML

SBR_DIR = {"NCAAF": ("ncaafootball", "ncaa football"), "NCAAB": ("ncaabasketball", "ncaa basketball")}
SBR_SEASONS = range(2007, 2022)  # 2007-08 through 2021-22

def read_xlsx(blob):
    z = zipfile.ZipFile(io.BytesIO(blob))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET_XML.fromstring(z.read("xl/sharedStrings.xml")).iter(ns + "si"):
            shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
    sheets = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    root = ET_XML.fromstring(z.read(sheets[0]))
    out = []
    for row in root.iter(ns + "row"):
        cells = {}
        for c in row.iter(ns + "c"):
            ref = c.get("r", "")
            letters = re.match(r"[A-Z]+", ref)
            col = 0
            for ch in (letters.group(0) if letters else ""):
                col = col * 26 + (ord(ch) - 64)
            t, v = c.get("t"), c.find(ns + "v")
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(ns + "t"))
            elif v is not None and v.text is not None:
                try:
                    f = float(v.text); val = int(f) if f.is_integer() else f
                except ValueError:
                    val = v.text
            else:
                val = None
            cells[col - 1] = val
        if cells:
            out.append([cells.get(i) for i in range(max(cells) + 1)])
    return out

def sbr_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().lower().replace("½", ".5")
    if t in ("pk", "pick", "p"):
        return 0.0
    try:
        return float(t)
    except ValueError:
        return None

def tidy_name(n):
    n = str(n).strip()
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", n) if " " not in n else n

def sbr_games(rows, sport, start):
    hdr = [str(x or "").strip().lower() for x in rows[0]]
    col = lambda name: hdr.index(name) if name in hdr else None
    iD, iVH, iT, iF, iO, iC, iML = col("date"), col("vh"), col("team"), col("final"), col("open"), col("close"), col("ml")
    if None in (iD, iVH, iT, iF, iO, iC):
        raise RuntimeError(f"unexpected columns: {hdr}")
    games, body, i = [], rows[1:], 0
    get = lambda r, k: r[k] if k is not None and k < len(r) else None
    while i + 1 < len(body):
        v, h = body[i], body[i + 1]
        vh1, vh2 = str(get(v, iVH) or "").upper(), str(get(h, iVH) or "").upper()
        if vh1 not in ("V", "N") or vh2 not in ("H", "N"):
            i += 1
            continue
        i += 2
        d = sbr_num(get(v, iD))
        vf, hf = sbr_num(get(v, iF)), sbr_num(get(h, iF))
        if d is None or vf is None or hf is None:
            continue
        mm, dd = int(d) // 100, int(d) % 100
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            continue
        yr = start if mm >= 7 else start + 1
        vml, hml = sbr_num(get(v, iML)), sbr_num(get(h, iML))
        if vml is not None and abs(vml) < 100: vml = None
        if hml is not None and abs(hml) < 100: hml = None
        g = {"date": yr * 10000 + mm * 100 + dd, "season": start, "home": tidy_name(get(h, iT)), "away": tidy_name(get(v, iT)),
             "hs": int(hf), "as": int(vf), "neu": vh1 == "N", "hml": hml, "aml": vml}
        for ph, k in (("", iC), ("O", iO)):
            a, b = sbr_num(get(v, k)), sbr_num(get(h, k))
            sp = tot = None
            if a is not None and b is not None:
                by_ml = hml is not None and vml is not None and hml != vml
                home_fav = (hml < vml) if by_ml else abs(b) <= abs(a)
                mag, tot = (abs(b), abs(a)) if home_fav else (abs(a), abs(b))
                if by_ml or tot > mag:
                    sp = -mag if home_fav else mag
                else:
                    tot = None
            g["sp" + ph], g["tot" + ph] = sp, tot
        if g["sp"] is not None and g.get("spO") is not None and abs(g["sp"] - g["spO"]) > 14:
            g["spO"] = None
        if g["tot"] is not None and g.get("totO") is not None and abs(g["tot"] - g["totO"]) > 20:
            g["totO"] = None
        games.append(g)
    return games

def wayback_raw(orig):
    q = urllib.parse.quote(orig, safe=":/")
    try:
        js = http_json("https://archive.org/wayback/available?url=" + urllib.parse.quote(q, safe=""))
        snap = (js or {}).get("archived_snapshots", {}).get("closest") or {}
        if snap.get("available") and snap.get("timestamp"):
            return f"https://web.archive.org/web/{snap['timestamp']}id_/{q}"
    except RuntimeError:
        pass
    try:
        rows = http_json(f"https://web.archive.org/cdx/search/cdx?url={urllib.parse.quote(q, safe='')}&output=json&filter=statuscode:200&limit=-1")
        if rows and len(rows) > 1:
            return f"https://web.archive.org/web/{rows[-1][1]}id_/{q}"
    except RuntimeError:
        pass
    return None

def http_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "crab-locks-updater"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

def import_sbr_archive(sports, state, test=False):
    done = state.setdefault("sbr_archive", {})
    for sport in ("NCAAF", "NCAAB"):
        if sport not in sports:
            continue
        folder, stem = SBR_DIR[sport]
        for start in SBR_SEASONS:
            if str(start) in done.get(sport, {}) and not test:
                continue
            label = f"{start}-{str(start + 1)[-2:]}"
            found = None
            for ext in ("xlsx", "xls"):
                orig = f"https://www.sportsbookreviewsonline.com/scoresoddsarchives/{folder}/{stem} {label}.{ext}"
                raw = wayback_raw(orig) or wayback_raw(orig.replace("https://www.", "https://"))
                time.sleep(2)
                if raw:
                    found = (ext, raw)
                    break
            if not found:
                say(f"  {sport} {label}: no archived copy found")
                done.setdefault(sport, {})[str(start)] = "missing"
                continue
            ext, raw = found
            if ext != "xlsx":
                say(f"  {sport} {label}: only an old .xls copy exists; skipping (can't read that format)")
                done.setdefault(sport, {})[str(start)] = "xls"
                continue
            try:
                blob = http_bytes(raw)
                games = sbr_games(read_xlsx(blob), sport, start)
            except Exception as e:
                say(f"  ! {sport} {label}: couldn't read the archived file ({e})")
                if test:
                    return
                continue
            time.sleep(2)
            op = sum(1 for g in games if g.get("spO") is not None)
            agree = sum(1 for g in games if g.get("sp") and g["hs"] != g["as"] and (g["sp"] < 0) == (g["hs"] > g["as"]))
            n = sum(1 for g in games if g.get("sp") and g["hs"] != g["as"])
            say(f"  {sport} {label}: {len(games)} games, {op} with opening spreads; favorites won {agree}/{n}")
            if test:
                say(f"  sample: {json.dumps(games[len(games)//2]) if games else 'none'}")
                return
            if n and agree / n < 0.55:
                say(f"  ! {sport} {label}: spreads don't line up with results; not saving this season")
                done.setdefault(sport, {})[str(start)] = "bad"
                continue
            for k, g in enumerate(games):
                g["id"] = f"sbr-{start}-{k}"
            save(f"archive/{sport}/{start}.json", {"sport": sport, "season": start, "source": "Sportsbook Review archive via archive.org",
                                                    "updated": NOW.isoformat(timespec="seconds"), "games": games})
            done.setdefault(sport, {})[str(start)] = len(games)

# ------------------------------------------------------------------ public betting splits (Playbook API)
# Free plan: 500 units a month, 5 per call. Each call returns every upcoming game in one league,
# so we pull each league once on days it plays, as close to game time as the schedule allows.
# Snapshots for a game are overwritten until it starts, so what's kept is the last pregame read.
PB_LEAGUE = {"NFL": ["nfl"], "NBA": ["nba"], "NCAAF": ["ncaaf", "cfb"], "NCAAB": ["ncaab", "cbb"]}
PB_SPORTKEY = {"NFL": "americanfootball_nfl", "NBA": "basketball_nba", "NCAAF": "americanfootball_ncaaf", "NCAAB": "basketball_ncaab"}
STATS["pb_left"] = None

def games_today(sport):
    """Our own list of today's games for a sport: (home, away) names as used in the app's data."""
    today = int(TODAY.strftime("%Y%m%d")); out = []
    if sport in ("NFL", "NBA"):
        for e in load(f"odds/{sport}.json", {"events": {}})["events"].values():
            if e.get("date") == today: out.append((e["home"], e["away"]))
    else:
        for g in load(f"api/{sport}/{season_of(sport, TODAY)}.json", {"games": []})["games"]:
            if g.get("date") == today and g.get("hs") is None: out.append((g["home"], g["away"]))
    return out

def pb_fetch(sport):
    known = load("splits/_params.json", {})
    tries = ([known[sport]] if sport in known else []) + [{"league": l} for l in PB_LEAGUE[sport]] + [{"sportKey": PB_SPORTKEY[sport]}]
    last = None
    for q in tries:
        try:
            js = http_json("https://api.playbook-api.com/v1/splits?" + urllib.parse.urlencode({**q, "api_key": PLAYBOOK_KEY}))
        except RuntimeError as e:
            last = e
            if "429" in str(e) or "401" in str(e) or "403" in str(e):
                raise
            continue
        if isinstance(js, dict):
            STATS["pb_left"] = js.get("requestsRemaining", STATS["pb_left"])
            sk = js.get("sportKey") or ((js.get("data") or [{}])[0].get("sportKey") if js.get("data") else None)
            if js.get("data") is not None and sk in (None, PB_SPORTKEY[sport]):
                if known.get(sport) != q:
                    known[sport] = q; save("splits/_params.json", known)
                return js.get("data") or []
    if last: raise last
    return []

def pb_name(sport, full, candidates):
    """Map a Playbook team name to the name our data uses for that sport."""
    if sport == "NFL": return NFL_ABBR.get(full, full)
    if sport == "NBA": return NBA_SHORT.get(full, full)
    # college: our names are school names ("Alabama"); Playbook's add mascots ("Alabama Crimson Tide").
    best = None
    for c in candidates:
        if full == c or full.startswith(c + " "):
            if best is None or len(c) > len(best): best = c
    return best

def pull_splits(sport, test=False):
    if not PLAYBOOK_KEY:
        say(f"  {sport} public betting: no PLAYBOOK_API_KEY, skipping"); return
    if STATS["pb_left"] is not None and STATS["pb_left"] < 15:
        say(f"  {sport} public betting: only {STATS['pb_left']} Playbook units left this month; skipping to stay under the free limit"); return
    rows = pb_fetch(sport)
    store = load(f"splits/{sport}.json", {"games": {}})
    ours = games_today(sport) if sport in ("NCAAF", "NCAAB") else []
    names = sorted({n for pair in ours for n in pair} | {k.split("|")[1] for k in store["games"]} | {k.split("|")[2] for k in store["games"]})
    if sport in ("NCAAF", "NCAAB"):
        for g in load(f"api/{sport}/{season_of(sport, TODAY)}.json", {"games": []})["games"]:
            names.append(g["home"]); names.append(g["away"])
        names = sorted(set(names), key=len, reverse=True)
    saved = unmatched = 0
    for x in rows:
        try:
            start = datetime.fromisoformat(str(x.get("startTime")).replace("Z", "+00:00"))
        except Exception:
            continue
        if start <= NOW:
            continue  # game started: keep the last pregame snapshot we already have
        h, a = pb_name(sport, x.get("homeTeamName", ""), names), pb_name(sport, x.get("awayTeamName", ""), names)
        if not h or not a:
            unmatched += 1; continue
        sp = (x.get("splits") or {}); s_, m_, t_ = sp.get("spread") or {}, sp.get("moneyline") or {}, sp.get("total") or {}
        rec = {"hb": _dig(s_, "bets", "homePercent"), "hm": _dig(s_, "money", "homePercent"),
               "mlb": _dig(m_, "bets", "homePercent"), "mlm": _dig(m_, "money", "homePercent"),
               "ob": _dig(t_, "bets", "overPercent"), "om": _dig(t_, "money", "overPercent"),
               "books": _dig(s_, "source", "booksUsed"), "at": NOW.isoformat(timespec="minutes")}
        if all(rec[k] is None for k in ("hb", "hm", "ob", "om")):
            continue
        key = f"{ymd(x.get('startTime'))}|{h}|{a}"
        if test and saved < 2:
            say(f"  sample {sport}: {x.get('awayTeamName')} @ {x.get('homeTeamName')} -> {a} @ {h}: {json.dumps(rec)}")
        store["games"][key] = rec; saved += 1
    say(f"  {sport} public betting: {len(rows)} games from Playbook, {saved} saved, {unmatched} couldn’t be matched to our games. Units left: {STATS['pb_left']}")
    if not test:
        save(f"splits/{sport}.json", store)

def splits_plan(morning):
    """Which leagues to pull this run, to stay inside the free plan."""
    wd = TODAY.weekday()  # Mon=0 ... Sat=5, Sun=6
    plan = []
    m = TODAY.month
    for sport in ("NFL", "NCAAF", "NBA", "NCAAB"):
        if sport == "NCAAB":
            # Lines for tonight's college games often aren't in our data yet, so go by the calendar.
            if not (m in (11, 12, 1, 2, 3) or (m == 4 and TODAY.day <= 8)): continue
        elif sport == "NCAAF":
            if not (m in (9, 10, 11, 12) or (m == 8 and TODAY.day >= 20) or (m == 1 and TODAY.day <= 20)) or wd in (0, 6): continue
        elif not games_today(sport): continue
        weekend_football = sport in ("NFL", "NCAAF") and wd in (5, 6)
        if morning == weekend_football:  # weekend football early; everything else in the afternoon
            plan.append(sport)
    return plan

# ------------------------------------------------------------------ advanced metrics + injuries
import gzip
NFLV_PBP = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{y}.csv.gz"
NFLV_INJ = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{y}.csv"
NBA_BOX = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_team_boxscores/team_box_{y}.csv"

def _f(v):
    try:
        x = float(v); return None if x != x else x
    except (TypeError, ValueError):
        return None

def nfl_metrics():
    y = season_of("NFL", TODAY)
    req = urllib.request.Request(NFLV_PBP.format(y=y), headers={"User-Agent": "crab-locks-updater"})
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read()
    rows = csv.DictReader(io.StringIO(gzip.decompress(raw).decode("utf-8")))
    agg = {}
    games = {}
    for x in rows:
        if x.get("season_type") not in ("REG", "POST"): continue
        if x.get("pass") != "1" and x.get("rush") != "1": continue
        epa, suc = _f(x.get("epa")), _f(x.get("success"))
        if epa is None: continue
        for team, side in ((x.get("posteam"), "o"), (x.get("defteam"), "d")):
            if not team: continue
            a = agg.setdefault(team, {"o": [0, 0, 0, 0, 0, 0, 0], "d": [0, 0, 0, 0, 0, 0, 0], "games": {}})
            v = a[side]; v[0] += 1; v[1] += epa; v[2] += suc or 0
            if x.get("pass") == "1": v[3] += 1; v[4] += epa
            else: v[5] += 1; v[6] += epa
            g = a["games"].setdefault(x.get("game_id"), {"o": [0, 0], "d": [0, 0], "wk": int(x.get("week") or 0)})
            g[side][0] += 1; g[side][1] += epa
    out = {}
    for team, a in agg.items():
        o, d = a["o"], a["d"]
        last = sorted(a["games"].values(), key=lambda g: g["wk"])[-4:]
        l4o = sum(g["o"][1] for g in last) / max(1, sum(g["o"][0] for g in last))
        l4d = sum(g["d"][1] for g in last) / max(1, sum(g["d"][0] for g in last))
        out[team] = {"games": len(a["games"]), "plays": o[0],
                     "off_epa": round(o[1] / max(1, o[0]), 3), "off_sr": round(o[2] / max(1, o[0]), 3),
                     "off_pass_epa": round(o[4] / max(1, o[3]), 3), "off_rush_epa": round(o[6] / max(1, o[5]), 3),
                     "def_epa": round(d[1] / max(1, d[0]), 3), "def_sr": round(d[2] / max(1, d[0]), 3),
                     "def_pass_epa": round(d[4] / max(1, d[3]), 3), "def_rush_epa": round(d[6] / max(1, d[5]), 3),
                     "l4_off_epa": round(l4o, 3), "l4_def_epa": round(l4d, 3)}
    # Pro Football Reference charting stats (pressures, bad throws, drops), as published by nflverse
    try:
        pf = list(csv.DictReader(io.StringIO(http_text(f"https://github.com/nflverse/nflverse-data/releases/download/pfr_advstats/advstats_week_pass_{y}.csv"))))
        tot = {}
        for x in pf:
            pr, pct = _f(x.get("times_pressured")), _f(x.get("times_pressured_pct"))
            db = pr / pct if pr and pct else None
            bt, btp = _f(x.get("passing_bad_throws")), _f(x.get("passing_bad_throw_pct"))
            att = bt / btp if bt and btp else None
            dr, drp = _f(x.get("passing_drops")), _f(x.get("passing_drop_pct"))
            att2 = dr / drp if dr and drp else None
            for team, side in ((x.get("team"), "o"), (x.get("opponent"), "d")):
                if not team: continue
                t = tot.setdefault(team, {"o": [0, 0, 0, 0, 0, 0], "d": [0, 0]})
                if side == "o":
                    if db: t["o"][0] += pr; t["o"][1] += db
                    if att: t["o"][2] += bt; t["o"][3] += att
                    if att2: t["o"][4] += dr; t["o"][5] += att2
                elif db:
                    t["d"][0] += pr; t["d"][1] += db
        for team, t in tot.items():
            e = out.setdefault(team, {})
            if t["o"][1]: e["pressure_allowed"] = round(t["o"][0] / t["o"][1], 3)
            if t["o"][3]: e["bad_throw_pct"] = round(t["o"][2] / t["o"][3], 3)
            if t["o"][5]: e["drop_pct"] = round(t["o"][4] / t["o"][5], 3)
            if t["d"][1]: e["pressure_rate"] = round(t["d"][0] / t["d"][1], 3)
    except Exception as e:
        say(f"  ! NFL Pro Football Reference stats skipped: {e}")
    save("metrics/NFL.json", {"updated": NOW.isoformat(timespec="seconds"), "season": y, "source": "nflverse play-by-play and Pro Football Reference advanced stats (via nflverse)", "teams": out})
    say(f"  NFL advanced metrics: {len(out)} teams from nflverse play-by-play")

def nfl_injuries():
    y = season_of("NFL", TODAY)
    text = http_text(NFLV_INJ.format(y=y))
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows: return
    latest = {}
    for x in rows:
        try: wk = int(x.get("week") or 0)
        except ValueError: continue
        latest[x["team"]] = max(latest.get(x["team"], 0), wk)
    out = {}
    for x in rows:
        t = x.get("team")
        if int(x.get("week") or 0) != latest.get(t): continue
        st = (x.get("report_status") or "").strip()
        if st not in ("Out", "Doubtful", "Questionable"): continue
        out.setdefault(t, []).append({"name": x.get("full_name"), "pos": x.get("position"), "status": st,
                                     "injury": x.get("report_primary_injury") or x.get("practice_primary_injury") or ""})
    order = {"Out": 0, "Doubtful": 1, "Questionable": 2}
    for t in out: out[t].sort(key=lambda p: (order.get(p["status"], 3), p["pos"] or ""))
    save("injuries/NFL.json", {"updated": NOW.isoformat(timespec="seconds"), "season": y, "week": max(latest.values()), "source": "nflverse (official NFL injury reports)", "teams": out})
    say(f"  NFL injuries: week {max(latest.values())}, {sum(len(v) for v in out.values())} players listed")

def nba_metrics():
    y = season_of("NBA", TODAY) + 1  # ESPN labels NBA seasons by their ending year
    rows = list(csv.DictReader(io.StringIO(http_text(NBA_BOX.format(y=y)))))
    by_game = {}
    for x in rows:
        if x.get("season_type") not in ("2", "3", "5"): continue
        by_game.setdefault(x["game_id"], []).append(x)
    agg = {}
    for gid, pair in by_game.items():
        if len(pair) != 2: continue
        for me, op in ((pair[0], pair[1]), (pair[1], pair[0])):
            g = lambda r, k: _f(r.get(k)) or 0
            poss_me = g(me, "field_goals_attempted") - g(me, "offensive_rebounds") + g(me, "turnovers") + 0.44 * g(me, "free_throws_attempted")
            poss_op = g(op, "field_goals_attempted") - g(op, "offensive_rebounds") + g(op, "turnovers") + 0.44 * g(op, "free_throws_attempted")
            poss = (poss_me + poss_op) / 2
            if poss <= 0: continue
            a = agg.setdefault(me["team_short_display_name"], {"g": [], "pts": 0, "opp": 0, "poss": 0, "fgm": 0, "fg3": 0, "fga": 0, "fta": 0, "tov": 0, "orb": 0, "odrb": 0,
                                                              "ofgm": 0, "ofg3": 0, "ofga": 0, "ofta": 0, "otov": 0, "oorb": 0, "drb": 0})
            a["g"].append((me["game_date"], g(me, "team_score"), g(op, "team_score"), poss))
            a["pts"] += g(me, "team_score"); a["opp"] += g(op, "team_score"); a["poss"] += poss
            a["fgm"] += g(me, "field_goals_made"); a["fg3"] += g(me, "three_point_field_goals_made"); a["fga"] += g(me, "field_goals_attempted"); a["fta"] += g(me, "free_throws_attempted")
            a["tov"] += g(me, "turnovers"); a["orb"] += g(me, "offensive_rebounds"); a["odrb"] += g(op, "defensive_rebounds"); a["drb"] += g(me, "defensive_rebounds")
            a["ofgm"] += g(op, "field_goals_made"); a["ofg3"] += g(op, "three_point_field_goals_made"); a["ofga"] += g(op, "field_goals_attempted"); a["ofta"] += g(op, "free_throws_attempted")
            a["otov"] += g(op, "turnovers"); a["oorb"] += g(op, "offensive_rebounds")
    out = {}
    for t, a in agg.items():
        n = len(a["g"]); P = a["poss"] or 1
        last = sorted(a["g"])[-10:]; lp = sum(x[3] for x in last) or 1
        out[t] = {"games": n, "pace": round(P / n, 1), "ortg": round(a["pts"] / P * 100, 1), "drtg": round(a["opp"] / P * 100, 1),
                  "net": round((a["pts"] - a["opp"]) / P * 100, 1), "l10_net": round((sum(x[1] for x in last) - sum(x[2] for x in last)) / lp * 100, 1),
                  "efg": round((a["fgm"] + 0.5 * a["fg3"]) / max(1, a["fga"]), 3), "tov": round(a["tov"] / P, 3),
                  "orb": round(a["orb"] / max(1, a["orb"] + a["odrb"]), 3), "ftr": round(a["fta"] / max(1, a["fga"]), 3),
                  "oefg": round((a["ofgm"] + 0.5 * a["ofg3"]) / max(1, a["ofga"]), 3), "otov": round(a["otov"] / P, 3),
                  "drb": round(a["drb"] / max(1, a["drb"] + a["oorb"]), 3), "oftr": round(a["ofta"] / max(1, a["ofga"]), 3)}
    save("metrics/NBA.json", {"updated": NOW.isoformat(timespec="seconds"), "season": season_of("NBA", TODAY), "source": "ESPN box scores via sportsdataverse", "teams": out})
    say(f"  NBA advanced metrics: {len(out)} teams")

def cfb_metrics():
    y = season_of("NCAAF", TODAY)
    adv = cfbd(CFB, "/stats/season/advanced", {"year": y, "excludeGarbageTime": "true"})
    sp = cfbd(CFB, "/ratings/sp", {"year": y})
    out = {}
    for x in adv or []:
        o, d = x.get("offense") or {}, x.get("defense") or {}
        out[x.get("team")] = {"off_ppa": o.get("ppa"), "off_sr": o.get("successRate"), "off_expl": o.get("explosiveness"),
                              "def_ppa": d.get("ppa"), "def_sr": d.get("successRate"), "def_expl": d.get("explosiveness"),
                              "havoc": (d.get("havoc") or {}).get("total")}
    for x in sp or []:
        t = x.get("team")
        if not t or t == "nationalAverages": continue
        e = out.setdefault(t, {})
        e.update({"sp": x.get("rating"), "sp_rank": x.get("ranking"), "sp_off": (x.get("offense") or {}).get("rating"), "sp_def": (x.get("defense") or {}).get("rating"),
                  "sp_off_rank": (x.get("offense") or {}).get("ranking"), "sp_def_rank": (x.get("defense") or {}).get("ranking")})
    save("metrics/NCAAF.json", {"updated": NOW.isoformat(timespec="seconds"), "season": y, "source": "CollegeFootballData.com (advanced stats, SP+)", "teams": out})
    say(f"  NCAAF advanced metrics: {len(out)} teams")

def cbb_metrics():
    start = season_of("NCAAB", TODAY)
    adj, stats, used = [], [], None
    for y in (start + 1, start):  # CBBD labels seasons by ending year; fall back just in case
        adj = cfbd(CBB, "/ratings/adjusted", {"season": y})
        if adj: used = y; break
    if used: stats = cfbd(CBB, "/stats/team/season", {"season": used})
    out = {}
    for x in adj or []:
        r = x.get("rankings") or {}
        out[x.get("team")] = {"adj_off": x.get("offensiveRating"), "adj_def": x.get("defensiveRating"), "adj_net": x.get("netRating"),
                              "off_rank": r.get("offense"), "def_rank": r.get("defense"), "net_rank": r.get("net")}
    for x in stats or []:
        e = out.setdefault(x.get("team"), {}); ts, os_ = x.get("teamStats") or {}, x.get("opponentStats") or {}
        tf, of = ts.get("fourFactors") or {}, os_.get("fourFactors") or {}
        e.update({"pace": x.get("pace"), "games": x.get("games"), "efg": tf.get("effectiveFieldGoalPct"), "tov": tf.get("turnoverRatio"), "orb": tf.get("offensiveReboundPct"),
                  "ftr": tf.get("freeThrowRate"), "oefg": of.get("effectiveFieldGoalPct"), "otov": of.get("turnoverRatio"), "drb": (100 - of["offensiveReboundPct"]) if isinstance(of.get("offensiveReboundPct"), (int, float)) else None, "oftr": of.get("freeThrowRate")})
    save("metrics/NCAAB.json", {"updated": NOW.isoformat(timespec="seconds"), "season": start, "source": "CollegeBasketballData.com (adjusted efficiency, four factors)", "teams": out})
    say(f"  NCAAB advanced metrics: {len(out)} teams (season label {used})")

# ------------------------------------------------------------------ Crab Locks model (NFL, NCAAF)
def _team_games_nfl(y):
    req = urllib.request.Request(NFLV_PBP.format(y=y), headers={"User-Agent": "crab-locks-updater"})
    with urllib.request.urlopen(req, timeout=240) as r:
        rows = csv.DictReader(io.StringIO(gzip.decompress(r.read()).decode("utf-8")))
    agg = {}
    for x in rows:
        if x.get("pass") != "1" and x.get("rush") != "1": continue
        epa = _f(x.get("epa"))
        if epa is None: continue
        for team, side in ((x.get("posteam"), "o"), (x.get("defteam"), "d")):
            if not team: continue
            k = (x["game_id"], team, side)
            a = agg.setdefault(k, {"season": int(x["season"]), "order": int(x["game_date"].replace("-", "")), "game": x["game_id"], "team": team, "side": side,
                                   "plays": 0, "epa": 0.0, "succ": 0.0, "pplays": 0, "pepa": 0.0})
            a["plays"] += 1; a["epa"] += epa; a["succ"] += _f(x.get("success")) or 0
            if x.get("pass") == "1": a["pplays"] += 1; a["pepa"] += epa
    return list(agg.values())

def _team_games_cfb(y, order_of):
    import pyarrow.parquet as pq
    url = f"https://github.com/sportsdataverse/sportsdataverse-data/releases/download/cfbfastR_cfb_pbp/play_by_play_{y}.parquet"
    path = os.path.join(ROOT, f".cfb_pbp_{y}.parquet")
    urllib.request.urlretrieve(url, path)
    t = pq.read_table(path, columns=["game_id", "season", "pos_team", "def_pos_team", "EPA", "success", "pass", "rush"]).to_pylist()
    os.remove(path)
    agg = {}
    for x in t:
        if (x["pass"] != 1 and x["rush"] != 1) or x["EPA"] is None: continue
        gid = str(x["game_id"])
        if gid not in order_of: continue
        for team, side in ((x["pos_team"], "o"), (x["def_pos_team"], "d")):
            if not team: continue
            a = agg.setdefault((gid, team, side), {"season": int(x["season"]), "order": order_of[gid], "game": gid, "team": team, "side": side, "plays": 0, "epa": 0.0, "succ": 0.0, "pplays": 0, "pepa": 0.0})
            a["plays"] += 1; a["epa"] += x["EPA"]; a["succ"] += x["success"] or 0
            if x["pass"] == 1: a["pplays"] += 1; a["pepa"] += x["EPA"]
    return list(agg.values())

def model_predictions_cbb():
    import model as M, model_cbb as CB
    import pyarrow.parquet as pq
    params = load("static/model_params.json", None)
    if not params or "NCAAB" not in params["sports"]: return
    P = params["sports"]["NCAAB"]; y = season_of("NCAAB", TODAY)
    games = {}
    for s in (y - 1, y):
        for x in load(f"api/NCAAB/{s}.json", {"games": []})["games"]:
            gid = str(x["id"])
            games[gid] = dict(game=gid, season=s, order=x["date"], home=x["home"], away=x["away"], hs=x.get("hs"), sp=x.get("sp"), tot=x.get("tot"), neu=bool(x.get("neu")), conf=bool(x.get("div")))
            games[gid]["as"] = x.get("as")
    last = {}
    for g in sorted(games.values(), key=lambda g: g["order"]):
        d = date(g["order"] // 10000, g["order"] // 100 % 100, g["order"] % 100)
        for side, t in (("rest_h", g["home"]), ("rest_a", g["away"])):
            L = last.get((g["season"], t)); g[side] = (d - L).days if L else 7; last[(g["season"], t)] = d
    cols = ["game_id", "game_date", "team_score", "field_goals_made", "field_goals_attempted", "three_point_field_goals_made", "free_throws_attempted", "offensive_rebounds", "defensive_rebounds", "turnovers"]
    box = []
    for es in (y, y + 1):   # ESPN labels college basketball seasons by their ending year
        path = os.path.join(ROOT, f".mbb_box_{es}.parquet")
        try:
            urllib.request.urlretrieve(f"https://raw.githubusercontent.com/sportsdataverse/hoopR-mbb-data/main/mbb/team_box/parquet/team_box_{es}.parquet", path)
            box += pq.read_table(path, columns=cols).to_pylist()
        finally:
            if os.path.exists(path): os.remove(path)
    lines = CB.team_lines(games, CB.match_box(games, box))
    feats, cur = CB.asof(lines)
    gbs = {}
    for g in games.values(): gbs.setdefault(g["season"], []).append(g)
    M.CFG["NCAAB"] = {"cap": 30, "lam": 6, "prior": 0.6, "hfa": 3.2, "K": 300}
    rm = CB.weekly_ratings(gbs, M.srs, M.CFG["NCAAB"])
    out = {}; today = int(TODAY.strftime("%Y%m%d"))
    for g in games.values():
        if g["season"] != y or g.get("hs") is not None or g["order"] < today or g.get("sp") is None or g.get("tot") is None: continue
        fh, fa = cur.get((y, g["home"])), cur.get((y, g["away"]))
        if not fh or not fa or g["game"] not in rm: continue
        x = CB.features(g, fh, fa, rm[g["game"]])
        ats = M.predict(P, x, "ats"); tot = M.predict(P, x, "tot") - P["tot"]["intercept"]
        out[f"{g['order']}|{g['home']}|{g['away']}"] = {"ats": round(ats, 2), "tot": round(tot, 2), "sp": g["sp"], "line_tot": g["tot"], "thin": int(fh["n"] < 4 or fa["n"] < 4 or g.get("neu"))}
    save("model/NCAAB.json", {"updated": NOW.isoformat(timespec="seconds"), "season": y, "tested": P["tested"], "record": P["record"], "lean": P["lean"], "games": out})
    say(f"  NCAAB model: predictions for {len(out)} upcoming games")

def model_predictions(sport):
    import model as M
    params = load("static/model_params.json", None)
    if not params or sport not in params["sports"]: return
    P = params["sports"][sport]
    y = season_of(sport, TODAY)
    games = {}
    if sport == "NFL":
        for x in csv.DictReader(io.StringIO(http_text(NFLV))):
            s = int(x["season"])
            if s not in (y - 1, y): continue
            n = lambda k: None if x.get(k) in ("", "NA", None) else float(x[k])
            sl = n("spread_line")
            games[x["game_id"]] = dict(game=x["game_id"], season=s, order=int(x["gameday"].replace("-", "")), home=x["home_team"], away=x["away_team"],
                hs=n("home_score"), sp=-sl if sl is not None else None, tot=n("total_line"), neu=x["location"] == "Neutral",
                rest_h=n("home_rest"), rest_a=n("away_rest"), conf=x["div_game"] == "1", wind=n("wind"), temp=n("temp"), dome=x["roof"] in ("dome", "closed"))
            games[x["game_id"]]["as"] = n("away_score")
        tg = _team_games_nfl(y - 1) + _team_games_nfl(y)
    else:
        last = {}
        for s in (y - 1, y):
            for x in load(f"api/NCAAF/{s}.json", {"games": []})["games"]:
                games[str(x["id"])] = dict(game=str(x["id"]), season=s, order=x["date"], home=x["home"], away=x["away"], hs=x.get("hs"), sp=x.get("sp"), tot=x.get("tot"),
                                           neu=bool(x.get("neu")), conf=bool(x.get("div")))
                games[str(x["id"])]["as"] = x.get("as")
        for g in sorted(games.values(), key=lambda g: g["order"]):
            d = date(g["order"] // 10000, g["order"] // 100 % 100, g["order"] % 100)
            for side, t in (("rest_h", g["home"]), ("rest_a", g["away"])):
                L = last.get((g["season"], t)); g[side] = (d - L).days if L else 14; last[(g["season"], t)] = d
        order_of = {k: g["order"] for k, g in games.items()}
        tg = _team_games_cfb(y - 1, order_of) + _team_games_cfb(y, order_of)
    gbs = {}
    for g in games.values(): gbs.setdefault(g["season"], []).append(g)
    feats, rfeat = M.team_asof(sport, tg, gbs); cur = M.team_asof.current
    pts = M.points_asof(gbs, sport)
    out = {}
    today = int(TODAY.strftime("%Y%m%d"))
    for g in games.values():
        if g["season"] != y or g.get("hs") is not None or g["order"] < today or g.get("sp") is None or g.get("tot") is None: continue
        fh, fa = cur.get((y, g["home"])), cur.get((y, g["away"]))
        if not fh or not fa or g["game"] not in rfeat: continue
        ph, pa = pts.get((g["game"], g["home"])), pts.get((g["game"], g["away"]))
        if ph and pa: g["pts_proj"] = (ph[0] + pa[1] + pa[0] + ph[1]) / 2
        if sport == "NFL" and not g.get("dome"):
            if g.get("wind") is None: g["wind"] = 7.7   # typical outdoor wind until a forecast is known
        x = M.game_features(g, fh, fa, rfeat[g["game"]])
        ats = M.predict(P, x, "ats")
        tot = M.predict(P, x, "tot") - P["tot"]["intercept"]   # centred: games finish above the total on average (shootouts), which isn't an over lean
        out[f"{g['order']}|{g['home']}|{g['away']}"] = {"ats": round(ats, 2), "tot": round(tot, 2), "sp": g["sp"], "line_tot": g["tot"]}
    save(f"model/{sport}.json", {"updated": NOW.isoformat(timespec="seconds"), "season": y, "tested": P["tested"], "record": P["record"], "lean": P["lean"], "games": out})
    say(f"  {sport} model: predictions for {len(out)} upcoming games")

# ------------------------------------------------------------------ index
def write_index(state):
    idx = {"updated": NOW.isoformat(timespec="seconds"), "sports": {}, "notes": state.get("notes", {})}
    for sport in ("NFL", "NCAAF", "NBA", "NCAAB"):
        ent = {"base": os.path.exists(os.path.join(DATA, "base", f"{sport}.json")), "api": [], "archive": [],
               "splits": os.path.exists(os.path.join(DATA, "splits", f"{sport}.json")),
               "metrics": os.path.exists(os.path.join(DATA, "metrics", f"{sport}.json")),
               "injuries": os.path.exists(os.path.join(DATA, "injuries", f"{sport}.json")),
               "model": os.path.exists(os.path.join(DATA, "model", f"{sport}.json"))}
        for kind in ("api", "archive"):
            d = os.path.join(DATA, kind, sport)
            if os.path.isdir(d):
                ent[kind] = sorted(int(f[:-5]) for f in os.listdir(d) if f.endswith(".json"))
        idx["sports"][sport] = ent
    save("index.json", idx, pretty=True)

# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sports", default="NFL,NCAAF,NBA,NCAAB")
    ap.add_argument("--odds-only", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--archive", action="store_true", help="one-time import of the Sportsbook Review college archive")
    args = ap.parse_args()
    sports = [s.strip().upper() for s in args.sports.split(",")]
    state = load("state.json", {"done": {}, "notes": {}})
    state.setdefault("done", {}); state.setdefault("notes", {})
    say(f"Crab Locks update {NOW.astimezone(ET):%Y-%m-%d %H:%M} ET. Keys: CFBD {'yes' if CFBD_KEY else 'MISSING'}, Odds API {'yes' if ODDS_KEY else 'MISSING'}")
    failures = 0
    if args.archive:
        try:
            import_sbr_archive(sports, state, test=args.test)
        finally:
            if not args.test:
                save("state.json", state, pretty=True)
                write_index(state)
        say("Archive import finished." + (" Nothing was saved (test)." if args.test else ""))
        return

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
        for sp in [x for x in ("NFL", "NBA", "NCAAF", "NCAAB") if x in sports][:2]:
            guard(f"{sp} public betting test", lambda sp=sp: pull_splits(sp, test=True))
        if "NBA" in sports:
            guard("NBA catch-up test", lambda: nba_catchup(tracks.get("NBA") or {"events": {}}, {}, budget=5, test=True))
            guard("NBA past-season test", lambda: nba_backfill({}, budget=5, test=True))
        if "NCAAF" in sports:
            guard("NCAAF test", lambda: pull_cfb_season(season_of("NCAAF", TODAY), test=True))
        if "NCAAB" in sports:
            guard("NCAAB test", lambda: pull_cbb_season(season_of("NCAAB", TODAY), test=True))
        say(f"Test finished. Nothing was saved. CFBD/CBBD calls used: {STATS['cfbd_calls']}")
        return
    if "NBA" in sports and not args.odds_only:
        if tracks.get("NBA") is None:
            tracks["NBA"] = load("odds/NBA.json", {"events": {}})
        left = guard("NBA catch-up", lambda: nba_catchup(tracks["NBA"], state, budget=ESPN_BUDGET))
        save("odds/NBA.json", tracks["NBA"])
        if left:
            guard("NBA past seasons", lambda: nba_backfill(state, budget=left))
    if tracks.get("NBA"):
        guard("NBA season files", lambda: nba_from_odds(tracks["NBA"]))
    if "NFL" in sports and not args.odds_only:
        guard("NFL rebuild", lambda: build_nfl_base(tracks.get("NFL")))

    for sp, pull, first in (("NCAAF", pull_cfb_season, 2013), ("NCAAB", pull_cbb_season, 2013)):
            if sp not in sports:
                continue
            if args.odds_only and sp != "NCAAB":
                continue
            if not CFBD_KEY:
                say(f"  {sp}: no CFBD_API_KEY, skipping")
                continue
            cur = season_of(sp, TODAY)
            todo = [cur] + ([] if args.odds_only else [s for s in range(cur - 1, first - 1, -1) if s not in state["done"].get(sp, [])][:4])
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

    if not args.odds_only:
        for sp, fn in (("NFL", nfl_metrics), ("NFL", nfl_injuries), ("NBA", nba_metrics), ("NCAAF", cfb_metrics), ("NCAAB", cbb_metrics)):
            if sp in sports and (sp not in ("NCAAF", "NCAAB") or CFBD_KEY):
                guard(f"{sp} {fn.__name__}", fn)
        for sp in ("NFL", "NCAAF"):
            if sp in sports:
                guard(f"{sp} model", lambda sp=sp: model_predictions(sp))
        if "NCAAB" in sports:
            guard("NCAAB model", model_predictions_cbb)
    for sp in [x for x in splits_plan(morning=not args.odds_only) if x in sports or args.odds_only]:
        guard(f"{sp} public betting", lambda sp=sp: pull_splits(sp))
    save("state.json", state, pretty=True)
    write_index(state)
    say(f"Done. CFBD/CBBD calls this run: {STATS['cfbd_calls']}. Odds API credits left: {STATS['odds_credits_left']}")
    if failures and failures >= len(sports):
        sys.exit(1)

if __name__ == "__main__":
    main()
