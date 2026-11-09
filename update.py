#!/usr/bin/env python3
"""
Betting Lab data updater.

Runs on GitHub Actions every morning (see .github/workflows/update.yml).
Uses only the Python standard library, so nothing needs to be installed.

What it does each run:
  1. Refresh window: re-pulls games from the last few days (to catch final
     scores and closing lines) and the next week (current lines for
     upcoming games) for NFL, college football, NBA and men's college
     basketball.
  2. Backfill: works backward through past seasons, a chunk per run, until
     it reaches seasons where ESPN no longer has odds. Progress is saved,
     so a run that stops early just picks up where it left off next time.
  3. Rebuilds the NFL base file from nflverse (closing lines, juice,
     weather) and merges ESPN opening lines into it.
  4. Writes data/index.json, which the app reads to know what exists.

Sources: ESPN's public site/core JSON feeds (unofficial, undocumented;
we go slowly), and nflverse's games.csv.

Command-line options (all optional):
  --sports NFL,NCAAF,NBA,NCAAB   limit to some sports
  --no-backfill                  only do the refresh window
  --test                         small dry run: fetch a few games per sport,
                                 print what was parsed, write nothing
  --max-minutes N                time budget for this run (default 300)
"""
import argparse, csv, io, json, os, sys, time, urllib.request, urllib.error
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone(timedelta(hours=-5))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
DELAY = float(os.environ.get("REQUEST_DELAY", "0.35"))

SPORTS = {
    # app code: (espn sport, espn league, scoreboard extra params, team-name style)
    "NFL":   ("football",   "nfl",                    "",                     "abbr"),
    "NCAAF": ("football",   "college-football",       "&groups=80",           "location"),
    "NBA":   ("basketball", "nba",                    "",                     "short"),
    "NCAAB": ("basketball", "mens-college-basketball", "&groups=50",          "location"),
}
HOOPS = {"NBA", "NCAAB"}
# Oldest season the backfill will try. It also stops on its own once a
# season comes back with almost no odds.
START_SEASON = {"NFL": 2010, "NCAAF": 2010, "NBA": 2010, "NCAAB": 2010}

# ---------------------------------------------------------------- http
_last = [0.0]
STATS = {"requests": 0, "errors": 0}

def get_json(url, tries=4):
    wait = DELAY - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    for i in range(tries):
        _last[0] = time.time()
        STATS["requests"] += 1
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (betting-lab data updater; low volume)",
                "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429):
                print(f"  ! HTTP {e.code} from ESPN (possible rate limit/block), backing off", flush=True)
                time.sleep(30 * (i + 1))
            else:
                time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    STATS["errors"] += 1
    return None

def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")

# ---------------------------------------------------------------- parsing helpers
def num(v, kind="line"):
    """Parse ESPN number-ish values: -5.5, '+6', 'PK', 'EVEN', 'o221.5', {'american': '-110'}."""
    if v is None:
        return None
    if isinstance(v, dict):
        for k in ("american", "alternateDisplayValue", "displayValue"):
            if k in v:
                x = num(v[k], kind)
                if x is not None:
                    return x
        if kind == "line" and isinstance(v.get("value"), (int, float)):
            return float(v["value"])
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace("\u2212", "-")
    if s in ("", "-", "--", "off", "n/a", "na"):
        return None
    if s in ("pk", "pick", "pick'em", "pickem"):
        return 0.0 if kind == "line" else None
    if s in ("even", "ev"):
        return 100.0 if kind == "odds" else None
    if s[0] in "ou" and len(s) > 1:
        s = s[1:]
    s = s.replace("+", "").replace("½", ".5")
    try:
        return float(s)
    except ValueError:
        return None

def dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d

def clean(x):
    if x is None:
        return None
    x = round(float(x), 1)
    return int(x) if x.is_integer() else x

def to_ymd(iso):
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(t.astimezone(ET).strftime("%Y%m%d"))
    except Exception:
        return None

# ---------------------------------------------------------------- seasons
def season_of(sport, d):
    """Season label (start year) for a calendar date."""
    if sport in HOOPS:
        return d.year if d.month >= 8 else d.year - 1
    return d.year if d.month >= 6 else d.year - 1

def season_dates(sport, season, today):
    if sport == "NFL":
        a, b = date(season, 8, 25), date(season + 1, 2, 20)
    elif sport == "NCAAF":
        a, b = date(season, 8, 20), date(season + 1, 1, 25)
    elif sport == "NBA":
        a, b = date(season, 10, 1), date(season + 1, 6, 30)
    else:
        a, b = date(season, 10, 25), date(season + 1, 4, 12)
    b = min(b, today)
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)

# ---------------------------------------------------------------- conference names
CONF = {}

def load_conferences(sport):
    s, lg, _, _ = SPORTS[sport]
    if sport not in ("NCAAF", "NCAAB") or sport in CONF:
        return
    CONF[sport] = {}
    js = get_json(f"https://site.api.espn.com/apis/site/v2/sports/{s}/{lg}/groups")
    def walk(o):
        if isinstance(o, dict):
            gid = o.get("groupId") or o.get("id")
            name = o.get("abbreviation") or o.get("shortName") or o.get("name")
            if gid and name and isinstance(name, str):
                CONF[sport].setdefault(str(gid), name)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(js)

# ---------------------------------------------------------------- scoreboard
def team_name(team, style):
    if style == "abbr":
        return team.get("abbreviation") or team.get("shortDisplayName") or team.get("displayName")
    if style == "short":
        return team.get("shortDisplayName") or team.get("name") or team.get("displayName")
    return team.get("location") or team.get("shortDisplayName") or team.get("displayName")

def score_of(c):
    v = c.get("score")
    if isinstance(v, dict):
        v = v.get("value", v.get("displayValue"))
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None

def scoreboard(sport, d):
    s, lg, extra, style = SPORTS[sport]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{s}/{lg}/scoreboard?dates={d:%Y%m%d}&limit=1000{extra}"
    js = get_json(url)
    out = []
    for ev in (js or {}).get("events", []) or []:
        try:
            comp = ev["competitions"][0]
            cs = comp.get("competitors", [])
            h = next(c for c in cs if c.get("homeAway") == "home")
            a = next(c for c in cs if c.get("homeAway") == "away")
            st = dig(comp, "status", "type") or dig(ev, "status", "type") or {}
            done = bool(st.get("completed"))
            if st.get("name") in ("STATUS_CANCELED", "STATUS_POSTPONED"):
                continue
            ymd = to_ymd(ev.get("date") or comp.get("date") or "")
            if not ymd:
                continue
            stype = dig(ev, "season", "type")
            g = {
                "id": str(ev["id"]), "cid": str(comp.get("id", ev["id"])),
                "date": ymd,
                "season": season_of(sport, datetime.strptime(str(ymd), "%Y%m%d").date()),
                "week": dig(ev, "week", "number") if sport in ("NFL", "NCAAF") else None,
                "post": stype not in (None, 2, 1),
                "home": team_name(h.get("team", {}), style),
                "away": team_name(a.get("team", {}), style),
                "hs": score_of(h) if done else None,
                "as": score_of(a) if done else None,
                "neu": bool(comp.get("neutralSite")),
                "div": bool(comp.get("conferenceCompetition")),
                "done": done,
            }
            if sport in ("NCAAF", "NCAAB"):
                hc, ac = dig(h, "team", "conferenceId"), dig(a, "team", "conferenceId")
                g["hc"] = CONF.get(sport, {}).get(str(hc)) if hc else None
                g["ac"] = CONF.get(sport, {}).get(str(ac)) if ac else None
            if stype == 1:  # preseason
                continue
            # scoreboard sometimes carries odds for upcoming games; keep as a fallback
            g["_sbodds"] = comp.get("odds")
            out.append(g)
        except Exception:
            continue
    return out

# ---------------------------------------------------------------- odds
PREFERRED = ("draftkings", "espn bet", "consensus", "caesars", "fanduel", "betmgm")

def parse_provider(item):
    """Return home-perspective lines from one provider's odds object."""
    h = item.get("homeTeamOdds") or {}
    a = item.get("awayTeamOdds") or {}
    r = {}
    for phase, key in (("open", "O"), ("close", "C"), ("current", "N")):
        r["sp" + key] = num(dig(h, phase, "pointSpread"))
        if r["sp" + key] is None:
            aw = num(dig(a, phase, "pointSpread"))
            r["sp" + key] = -aw if aw is not None else None
        r["tot" + key] = num(dig(item, phase, "total"))
        r["hml" + key] = num(dig(h, phase, "moneyLine"), "odds")
        r["aml" + key] = num(dig(a, phase, "moneyLine"), "odds")
        r["hso" + key] = num(dig(h, phase, "spread"), "odds")
        r["aso" + key] = num(dig(a, phase, "spread"), "odds")
        r["oo" + key] = num(dig(item, phase, "over"), "odds")
        r["uo" + key] = num(dig(item, phase, "under"), "odds")
    # flat fields (older games / scoreboard odds)
    flat_sp = num(item.get("spread"))
    if flat_sp is not None:
        hf, af = h.get("favorite"), a.get("favorite")
        if hf is True or af is False:
            flat_sp = -abs(flat_sp)
        elif af is True or hf is False:
            flat_sp = abs(flat_sp)
    r["spF"] = flat_sp
    r["totF"] = num(item.get("overUnder"))
    r["hmlF"] = num(h.get("moneyLine"), "odds")
    r["amlF"] = num(a.get("moneyLine"), "odds")
    r["hsoF"] = num(h.get("spreadOdds"), "odds")
    r["asoF"] = num(a.get("spreadOdds"), "odds")
    r["ooF"] = num(item.get("overOdds"), "odds")
    r["uoF"] = num(item.get("underOdds"), "odds")
    return r

def pick(r, f, done):
    """Closing value for finished games, current value for upcoming ones."""
    order = ("C", "N", "F") if done else ("N", "F", "C")
    for k in order:
        v = r.get(f + k)
        if v is not None:
            return v
    return None

def resolve_items(js):
    items = (js or {}).get("items", []) or []
    out = []
    for it in items:
        if isinstance(it, dict) and set(it.keys()) <= {"$ref"} and it.get("$ref"):
            it = get_json(it["$ref"].replace("http://", "https://")) or {}
        if isinstance(it, dict):
            out.append(it)
    return out

def provider_rank(it):
    name = (dig(it, "provider", "name") or "").lower()
    if "live" in name:
        return 99
    for i, p in enumerate(PREFERRED):
        if p in name:
            return i
    if "live" in name:
        return 99
    pr = dig(it, "provider", "priority")
    return 50 + (pr if isinstance(pr, (int, float)) else 0)

def fetch_odds(sport, g):
    s, lg, _, _ = SPORTS[sport]
    url = f"https://sports.core.api.espn.com/v2/sports/{s}/leagues/{lg}/events/{g['id']}/competitions/{g['cid']}/odds?limit=50"
    items = resolve_items(get_json(url))
    if not items and g.get("_sbodds"):
        items = g["_sbodds"] if isinstance(g["_sbodds"], list) else []
    best = None
    ranked = []
    for it in items:
        r = parse_provider(it)
        if not any(r.get(k) is not None for k in ("spC", "spN", "spF", "totC", "totN", "totF")):
            continue
        rich = any(r.get(k) is not None for k in ("spO", "spC", "totO", "totC"))
        ranked.append((0 if rich else 1, provider_rank(it), len(ranked), it, r))
    if ranked:
        ranked.sort(key=lambda x: x[:3])
        best = (ranked[0][3], ranked[0][4])
    lines = {"oddsChecked": 1}
    if not best:
        return lines
    it, r = best
    done = g["done"]
    lines.update({
        "sp": pick(r, "sp", done), "spO": r.get("spO"),
        "tot": pick(r, "tot", done), "totO": r.get("totO"),
        "hml": pick(r, "hml", done), "aml": pick(r, "aml", done),
        "hmlO": r.get("hmlO"), "amlO": r.get("amlO"),
        "hso": pick(r, "hso", done), "aso": pick(r, "aso", done),
        "oo": pick(r, "oo", done), "uo": pick(r, "uo", done),
        "book": dig(it, "provider", "name"),
    })
    # keep only real moneylines
    for k in ("hml", "aml", "hmlO", "amlO", "hso", "aso", "oo", "uo"):
        v = lines.get(k)
        if v is not None and abs(v) < 100:
            lines[k] = None
    for k in list(lines):
        if isinstance(lines[k], float):
            lines[k] = clean(lines[k])
    return lines

# ---------------------------------------------------------------- season files
KEEP = ["id", "date", "season", "week", "post", "home", "away", "hs", "as", "neu", "div", "hc", "ac",
        "sp", "spO", "tot", "totO", "hml", "aml", "hmlO", "amlO", "hso", "aso", "oo", "uo", "book", "oddsChecked"]

def season_path(sport, season):
    return os.path.join(DATA, "espn", sport, f"{season}.json")

def load_season(sport, season):
    p = season_path(sport, season)
    if os.path.exists(p):
        with open(p) as f:
            js = json.load(f)
        return {g["id"]: g for g in js.get("games", [])}
    return {}

def save_season(sport, season, games):
    os.makedirs(os.path.dirname(season_path(sport, season)), exist_ok=True)
    rows = sorted(games.values(), key=lambda g: (g["date"], g["id"]))
    rows = [{k: g.get(k) for k in KEEP if g.get(k) is not None and g.get(k) is not False} for g in rows]
    with open(season_path(sport, season), "w") as f:
        json.dump({"sport": sport, "season": season,
                   "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "games": rows}, f, separators=(",", ":"))

def merge(old, new_meta, lines):
    g = dict(old or {})
    for k, v in new_meta.items():
        if k.startswith("_") or k in ("cid", "done"):
            continue
        g[k] = v
    if lines:
        for k, v in lines.items():
            # never wipe a stored opening line with a blank
            if v is None and k in ("spO", "totO", "hmlO", "amlO") and g.get(k) is not None:
                continue
            if v is None and g.get(k) is not None and new_meta.get("done"):
                continue
            g[k] = v
    return g

def coverage(games):
    n = len(games)
    fin = [g for g in games.values() if g.get("hs") is not None]
    return {"games": n, "final": len(fin),
            "close": sum(1 for g in fin if g.get("sp") is not None),
            "open": sum(1 for g in fin if g.get("spO") is not None)}

# ---------------------------------------------------------------- work units
class OutOfTime(Exception):
    pass

def check_time(deadline):
    if time.time() > deadline:
        raise OutOfTime()

def process_dates(sport, dates, deadline, refetch_done_odds, test=False):
    """Pull scoreboards for dates, then odds for each game. Returns touched seasons."""
    touched = {}
    for d in dates:
        check_time(deadline)
        for g in scoreboard(sport, d):
            season = g["season"]
            if season not in touched:
                touched[season] = load_season(sport, season)
            store = touched[season]
            old = store.get(g["id"])
            need = refetch_done_odds or not old or not old.get("oddsChecked") \
                or not g["done"] or (old and old.get("hs") is None)
            lines = None
            if need:
                check_time(deadline)
                lines = fetch_odds(sport, g)
            store[g["id"]] = merge(old, g, lines)
            if test:
                print(json.dumps(store[g["id"]]))
                if sum(len(v) for v in touched.values()) >= 4:
                    return touched
        if test and touched:
            return touched
    return touched

def refresh_window(sport, today, deadline, test=False):
    dates = [today + timedelta(days=i) for i in range(-4, 8)]
    if test:
        dates = [today - timedelta(days=i) for i in range(1, 30)]
    touched = process_dates(sport, dates, deadline, refetch_done_odds=True, test=test)
    if not test:
        for season, games in touched.items():
            save_season(sport, season, games)
    return touched

def backfill_one(sport, season, today, deadline):
    games = load_season(sport, season)
    seen_dates = {g["date"] for g in games.values() if g.get("oddsChecked")}
    todo = [d for d in season_dates(sport, season, today) if int(d.strftime("%Y%m%d")) not in seen_dates]
    print(f"  {sport} {season}: {len(todo)} dates to pull ({len(games)} games already saved)", flush=True)
    try:
        for i in range(0, len(todo), 7):
            chunk = todo[i:i + 7]
            touched = process_dates(sport, chunk, deadline, refetch_done_odds=False)
            for s, g in touched.items():
                if s == season:
                    games.update(g)
                else:  # spill-over dates belonging to a neighbour season
                    save_season(sport, s, g)
            save_season(sport, season, games)
    except OutOfTime:
        save_season(sport, season, games)
        raise
    return games

# ---------------------------------------------------------------- NFL base (nflverse + ESPN openers)
NFLV = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
ESPN_TO_NFLV = {"LAR": "LA", "WSH": "WAS"}
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

def build_nfl_base():
    try:
        text = get_text(NFLV)
    except Exception as e:
        print(f"  ! couldn't download nflverse games.csv ({e}); keeping existing NFL base")
        return None
    openers = {}
    espn_dir = os.path.join(DATA, "espn", "NFL")
    if os.path.isdir(espn_dir):
        for fn in os.listdir(espn_dir):
            with open(os.path.join(espn_dir, fn)) as f:
                for g in json.load(f).get("games", []):
                    h = ESPN_TO_NFLV.get(g.get("home"), g.get("home"))
                    openers[(g["date"], h)] = g
    games, matched = [], 0
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
        e = None
        for dd in (0, -1, 1):
            e = openers.get((g["date"] + dd, g["home"]))
            if e:
                break
        if e:
            matched += 1
            g["spO"], g["totO"] = e.get("spO"), e.get("totO")
            if g["sp"] is None and e.get("sp") is not None:
                g["sp"] = e["sp"]
            if g["tot"] is None and e.get("tot") is not None:
                g["tot"] = e["tot"]
        games.append(g)
    with open(os.path.join(DATA, "base", "NFL.json"), "w") as f:
        json.dump(pack(games), f, separators=(",", ":"))
    print(f"  NFL base rebuilt: {len(games)} games, {matched} matched to ESPN for opening lines")
    return True

# ---------------------------------------------------------------- index
def write_index():
    idx = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sports": {}}
    for sport in SPORTS:
        ent = {"base": os.path.exists(os.path.join(DATA, "base", f"{sport}.json")), "espn": [], "coverage": {}}
        d = os.path.join(DATA, "espn", sport)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".json"):
                    s = int(fn[:-5])
                    with open(os.path.join(d, fn)) as f:
                        games = {g["id"]: g for g in json.load(f).get("games", [])}
                    ent["espn"].append(s)
                    ent["coverage"][str(s)] = coverage(games)
        idx["sports"][sport] = ent
    with open(os.path.join(DATA, "index.json"), "w") as f:
        json.dump(idx, f, indent=1)
    return idx

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sports", default="NFL,NCAAF,NBA,NCAAB")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--max-minutes", type=float, default=float(os.environ.get("MAX_MINUTES", "300")))
    args = ap.parse_args()
    sports = [s.strip().upper() for s in args.sports.split(",") if s.strip().upper() in SPORTS]
    today = datetime.now(ET).date()
    deadline = time.time() + args.max_minutes * 60
    state_path = os.path.join(DATA, "state.json")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {"done": {}, "floor": {}}

    print(f"Betting Lab update, {today}, sports: {', '.join(sports)}", flush=True)
    for sp in sports:
        load_conferences(sp)

    if args.test:
        for sp in sports:
            print(f"\n=== TEST {sp}: a few recent games as parsed ===", flush=True)
            refresh_window(sp, today, deadline, test=True)
        print(f"\nTest done. Requests: {STATS['requests']}, failed: {STATS['errors']}. Nothing was saved.")
        return

    try:
        # 1. refresh recent + upcoming
        for sp in sports:
            print(f"Refreshing {sp}...", flush=True)
            touched = refresh_window(sp, today, deadline)
            n = sum(len(v) for v in touched.values())
            print(f"  {n} games in the refresh window", flush=True)

        # 2. backfill, newest seasons first, rotating sports
        if not args.no_backfill:
            current = {sp: season_of(sp, today) for sp in sports}
            for back in range(1, 40):
                for sp in sports:
                    season = current[sp] - back
                    if season < START_SEASON[sp] or season < state["floor"].get(sp, 0):
                        continue
                    if season in state["done"].get(sp, []):
                        continue
                    check_time(deadline)
                    games = backfill_one(sp, season, today, deadline)
                    cov = coverage(games)
                    state["done"].setdefault(sp, []).append(season)
                    print(f"  {sp} {season} done: {cov}", flush=True)
                    if cov["final"] and cov["close"] / cov["final"] < 0.15:
                        state["floor"][sp] = season + 1
                        print(f"  {sp}: ESPN odds run out before {season + 1}; backfill stops there", flush=True)
                    json.dump(state, open(state_path, "w"), indent=1)
    except OutOfTime:
        print("Time budget used up; the next run continues from here.", flush=True)
    finally:
        json.dump(state, open(state_path, "w"), indent=1)
        if "NFL" in sports:
            build_nfl_base()
        idx = write_index()
        print(f"Requests: {STATS['requests']}, failed: {STATS['errors']}")
        for sp, ent in idx["sports"].items():
            cov = ent["coverage"]
            if cov:
                tot = {k: sum(c[k] for c in cov.values()) for k in ("final", "close", "open")}
                print(f"  {sp}: seasons {min(ent['espn'])}-{max(ent['espn'])}, finished games {tot['final']}, "
                      f"with closing spread {tot['close']}, with opening spread {tot['open']}")
    if STATS["requests"] > 20 and STATS["errors"] / STATS["requests"] > 0.5:
        print("More than half the requests failed. ESPN may be blocking GitHub's servers.")
        sys.exit(1)

if __name__ == "__main__":
    main()
