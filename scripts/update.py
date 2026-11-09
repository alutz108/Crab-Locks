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

# ------------------------------------------------------------------ index
def write_index(state):
    idx = {"updated": NOW.isoformat(timespec="seconds"), "sports": {}, "notes": state.get("notes", {})}
    for sport in ("NFL", "NCAAF", "NBA", "NCAAB"):
        ent = {"base": os.path.exists(os.path.join(DATA, "base", f"{sport}.json")), "api": [], "archive": []}
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

    save("state.json", state, pretty=True)
    write_index(state)
    say(f"Done. CFBD/CBBD calls this run: {STATS['cfbd_calls']}. Odds API credits left: {STATS['odds_credits_left']}")
    if failures and failures >= len(sports):
        sys.exit(1)

if __name__ == "__main__":
    main()
