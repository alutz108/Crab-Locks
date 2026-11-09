# Betting Lab

A Bet Labs–style system backtester for NFL, college football, NBA and men's college basketball.

- `index.html` – the app (served by GitHub Pages)
- `data/` – game data the app loads. Updated automatically.
  - `base/` – historical lines bundled at setup (NFL is rebuilt from nflverse on every run)
  - `espn/<SPORT>/<season>.json` – games, scores, opening and closing lines pulled from ESPN
  - `index.json` – what data exists and how complete it is
  - `state.json` – backfill progress
- `scripts/update.py` – the updater (standard-library Python, nothing to install)
- `.github/workflows/update.yml` – runs the updater at 6 AM and 4 PM Eastern

Run it by hand from the **Actions** tab → **Update betting data** → **Run workflow**.
Modes: `test` prints a few parsed games without saving; `refresh` updates recent and upcoming games only; `full` also backfills past seasons.

Data sources: ESPN's public JSON feeds (unofficial; the updater keeps requests slow) and nflverse.
