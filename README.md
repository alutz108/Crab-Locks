# 🦀 Crab Locks

A betting-system backtester (like Action Network's Bet Labs) for the NFL, college football, NBA and men's college basketball.

## How it works
- `index.html` is the app, served by GitHub Pages.
- `data/` holds the games and lines. GitHub Actions refreshes it every day (6 AM and 4:30 PM Eastern).
- `scripts/update.py` does the refresh. It needs two repository secrets:
  - `CFBD_API_KEY` (free key from collegefootballdata.com/key; it also works for CollegeBasketballData.com)
  - `ODDS_API_KEY` (free key from the-odds-api.com)

## Sources
| Sport | Closing lines | Opening lines |
|---|---|---|
| NFL | nflverse | Sportsbook Review archive 2011–2021; The Odds API from now on |
| College football | CollegeFootballData.com (plus a bundled 2006–2025 archive) | CollegeFootballData.com |
| NBA | Sportsbook Review archive 2011–2021, sportsdataverse 2022–2025, The Odds API from now on | Sportsbook Review 2011–2021; The Odds API from now on |
| College basketball | CollegeBasketballData.com, 2013-14 on | CollegeBasketballData.com |

Run it by hand from **Actions → Update Crab Locks data → Run workflow** (`test` prints samples without saving).
