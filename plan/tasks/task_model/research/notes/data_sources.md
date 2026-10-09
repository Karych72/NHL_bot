# External data sources for an NHL pre-match model vs bookmaker (odds, starting goalies, xG, injuries)

Status as of 2026-10-09. Items marked "verified by probe" were checked directly with curl against the live endpoint on 2026-10-09; everything else is from provider pages / search results and may change.

## Historical NHL odds (closing moneyline + totals with prices, 2021-22 → 2025-26 and ongoing)

### Takeaway
No single free source covers 2021-22 through 2025-26 with closing ML and totals+prices. Free SBRO covers 2021-22 fully but stops in late Nov 2022. The cleanest paid option is The Odds API historical endpoint (snapshots since June 2020, h2h + totals + spreads, $30/month plan is enough for a one-off backfill). An undocumented free alternative is ESPN's core API, which returns one bookmaker's ML + total + prices per game (verified for Jan 2022 and Jan 2025), with unclear ToS.

### Cited Findings
**Sportsbook Reviews Online (SBRO)** — free, HTML tables / Excel
- Columns per team row: Date, Rot, VH, Team, 1st/2nd/3rd/Final, Open ML, Close ML, PuckLine (line + price), OpenOU (line + price), CloseOU (line + price). So it does include totals with the line and price. Verified by probe — [SBRO NHL 2022-23](https://www.sportsbookreviewsonline.com/scoresoddsarchives/nhl-odds-2022-23/)
- 2021-22 page is complete: ~2,800 table rows ≈ 1,400 games (regular season + playoffs), page last modified 2023-04-09. Verified by probe — [SBRO NHL 2021-22](https://www.sportsbookreviewsonline.com/scoresoddsarchives/nhl-odds-2021-22/)
- The 2022-23 page **ends on 11/27 (2022)** (~342 games), so the season is truncated. Verified by probe — [SBRO NHL 2022-23](https://www.sportsbookreviewsonline.com/scoresoddsarchives/nhl-odds-2022-23/)
- URLs for 2023-24, 2024-25 and 2025-26 return 301 redirects to the site homepage (`/?nfr=1`), meaning no archive exists. The old index page `/scoresoddsarchives/nhl/nhloddsarchives.htm` returns 404. Verified by probe.
- Which sportsbook the lines come from and the licensing terms are not stated on the season page (Gap).

**The Odds API** — paid, JSON REST
- Historical endpoint has snapshots from 2020-06-06. They are at 10-minute intervals, and at 5-minute intervals since Sept 2022. Cost per request = 10 × markets × regions (1 market, 1 region = 10 credits). Markets for `icehockey_nhl` are h2h, spreads and totals. Empty responses are free. The docs say "Data errors aren't common but they can occur" — [The Odds API v4 docs](https://the-odds-api.com/liveapi/guides/v4/)
- Plans: 20K credits $30/mo; 100K $59/mo; 5M $119/mo; 15M $249/mo. Starter (free) is 500 credits/month — [The Odds API pricing](https://the-odds-api.com/)
- Conflict: the docs say historical is paid-only ([docs](https://the-odds-api.com/liveapi/guides/v4/)), but the pricing page lists "Historical Odds" under Starter ([pricing](https://the-odds-api.com/)). Assume paid-only until tested.
- ToS: showing the data in a UI, website or app "including for commercial use" is allowed. Reselling or redistributing it "as a standalone data product" is not — [The Odds API T&C](https://the-odds-api.com/terms-and-conditions.html)
- The Odds API says historical NHL featured markets (ML, spreads, totals) are available from late 2020 — [The Odds API NHL page](https://the-odds-api.com/sports-odds-data/nhl-odds.html)

**ESPN core API (undocumented, free)** — JSON. Verified by probe:
- `https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/events/{id}/competitions/{id}/odds`, with event ids from `site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates=YYYYMMDD`.
- For a 2025-01-15 game it returned provider "ESPN BET" with `overUnder` 5.5, `overOdds` -130, `underOdds` +110, team `moneyLine`, plus `open`/`close` sub-objects (the close holds the puck line with its price). A second item is in-game "Live Odds", which must be filtered out.
- For a 2022-01-15 game it returned provider "Bet365" with `overUnder`/`overOdds`/`underOdds`/`moneyLine`, but no `open`/`close` objects, so it is one snapshot (probably near close).
- No published ToS for this API. It is single-book (not Pinnacle), and the provider changes between seasons.

**OddsPortal** — scraping only
- OddsPortal's ToS restricts commercial reuse of its data. Scraping is a "legal gray area". Private research models are described as low-risk — [ScrapeHero article](https://www.scrapehero.com/scraping-odds-portal/) (secondary source; the OddsPortal ToS itself was not read)
- Open-source scraper OddsHarvester (Playwright; historical and upcoming odds; JSON/CSV output) — [GitHub OddsHarvester](https://github.com/jordantete/OddsHarvester)

**Other APIs / vendors**
- OddsPapi: free developer tier, 350+ books including Pinnacle and Betfair Exchange, a `/historical-odds` endpoint with timestamped price changes for CLV. Season depth and commercial terms are not stated — [OddsPapi blog](https://oddspapi.io/blog/?p=2892) (vendor's own marketing)
- BigDataBall: NHL datasets in Excel, seasons 2017-18 to 2025-26, $40 per season. The odds columns and the license are not specified on the page — [BigDataBall NHL](https://www.bigdataball.com/datasets/nhl-data/)
- SharpAPI ([sharpapi.io](https://sharpapi.io/nhl-api)), SportsGameOdds ([sportsgameodds.com](https://sportsgameodds.com/nhl-odds-api/)) and Apify odds scrapers ([Apify](https://apify.com/seemuapps/sports-odds-scraper)) also exist. Historical depth and pricing were not verified.
- Kaggle: the search did not find a maintained Kaggle dataset with NHL closing ML + totals for 2021-26 (Gap).

### Inferences
- Practical backfill plan: (a) SBRO 2021-22 for free; (b) The Odds API for 2022-23 → today. Cost estimate: about 1 request per game-day snapshot near puck drop × ~190 game days/season × 5 seasons ≈ 950 requests. For h2h+totals in one region that is 20 credits each ≈ 19K credits, which roughly fits one month of the $30 (20K) plan. This assumes one pre-first-puck snapshot per date, not per game. Multiple start times per day need more snapshots. (c) ESPN core API as a free cross-check or fallback.
- For "closing line" quality, a sharp book (Pinnacle) is preferable. Only The Odds API (region `eu` includes Pinnacle per its bookmaker list; not verified here) and OddsPapi claim Pinnacle.
- Commercial (premium bot): The Odds API ToS explicitly permits commercial display of derived analytics. ESPN, OddsPortal and SBRO have no grant, so treat them as research-only.

### Gaps
- SBRO's source book and license terms. Whether SBRO will resume (the redirect suggests no).
- Whether The Odds API free Starter can call historical (conflicting pages).
- Covers.com, Action Network and SportsDataIO historical NHL odds pricing were not researched within the budget.
- OddsPortal's own ToS text was not fetched.

## Live / current odds for daily comparison

### Takeaway
The Odds API Starter (500 credits/month free) is enough for a small daily NHL pull. Live endpoints cost 1 credit per market per region, compared with 10 for historical, so a daily h2h+totals call in 1 region ≈ 2 credits ≈ 60/month. The ESPN scoreboard/core odds give a free single-book alternative.

### Cited Findings
- Free Starter: 500 credits/month, all sports, all markets — [The Odds API](https://the-odds-api.com/)
- Historical costs 10× the live endpoints per market-region, which implies live = 1 per market-region — [docs](https://the-odds-api.com/liveapi/guides/v4/)
- Commercial display allowed; standalone redistribution banned — [T&C](https://the-odds-api.com/terms-and-conditions.html)
- OddsPapi free tier rate limit ≈ 0.88 s between calls to the same endpoint — [OddsPapi](https://oddspapi.io/blog/?p=2892)

### Inferences
- Even polling 3×/day (open, morning after goalie news, ~1 h pre-game) stays under 500 credits/month for NHL h2h+totals in one region.

### Gaps
- Exact live-endpoint credit formula was inferred, not quoted.

## Starting goalie data

### Takeaway
The NHL API does **not** expose a confirmed/probable starter before the game. The pre-game `landing` only lists each team's goalies with season stats. The post-game `boxscore` has a `starter: true/false` flag, which is perfect for historical labels. Pre-game starters come from DailyFaceoff, which uses Confirmed/Expected/Probable tiers, often tied to morning skate and aimed to be settled before 7 PM ET. There is no public licence for scraping DailyFaceoff.

### Cited Findings
- Verified by probe (game 2026020070, state FUT, 2026-10-10): `landing` keys include `matchup.goalieComparison`, which has `teamTotals` and `leaders` (each team's goalies with GP, record, GAA, sv%). There is no starter or probable field. The pre-game `boxscore` has no `playerByGameStats`. `right-rail` has only gameInfo, last10Record, seasonSeries and teamSeasonStats.
- Verified by probe (game 2025020500, final): `boxscore.playerByGameStats.<team>.goalies[]` has fields `starter` (True for Shesterkin, False for Quick), `toi`, `decision`, `shotsAgainst`, `saves`, `evenStrength/powerPlay/shorthanded…Against`, `savePctg`.
- Verified by probe: every play-by-play shot event has `goalieInNetId`, so the actual goalie faced per shot is known historically.
- DailyFaceoff tiers: "Confirmed" = team source or beat writer confirms; "Expected/Likely" = a source expects it, usually not from morning skate; "Probable" = best guess from surrounding games. They aim to confirm all starters before 7 PM ET. Some teams don't hold a morning skate, so not all goalies get confirmed — [DailyFaceoff starting goalies (example page)](https://www.dailyfaceoff.com/news/starting-goalies-sunday-march-21-2010); dated daily archive exists, e.g. [dailyfaceoff.com/archive/2024-03-25](https://www.dailyfaceoff.com/archive/2024-03-25)
- DailyFaceoff states it is "a news site with no direct affiliation to the NHL, or NHLPA" — [DailyFaceoff](https://www.dailyfaceoff.com/news/starting-goalies-sunday-march-21-2010)

### Inferences
- Historical modelling: use the boxscore `starter` flag as ground truth. A model trained on the actual starter is "leaky" relative to bets placed before confirmation. Either bet only after confirmation, or simulate uncertainty (e.g. a probability that the No. 1 goalie starts, conditioned on back-to-backs).
- Live: scrape or read DailyFaceoff (or beat-writer feeds) on game day. Using it in a paid bot without permission is a ToS/IP risk.

### Gaps
- DailyFaceoff's ToS/terms page URL (`/terms-of-use`) returned 404, so the actual scraping clauses are unknown.
- LeftWingLock and other goalie-confirmation sources were not researched within the budget.
- No source found for historical *pre-game* confirmation timestamps, which would be needed to backtest "bet after confirmation" timing.

## NHL API play-by-play for xG; public xG data (MoneyPuck, NST, Evolving-Hockey)

### Takeaway
api-web.nhle.com play-by-play has everything needed for a basic xG model (coordinates, shot type, strength via situationCode, zone, shooter, goalie, miss reason, time), but no xG value itself. MoneyPuck gives free shot-level xG (124 attributes, 2007-08 → 2025-26) but only for non-commercial use, so a paid bot needs written permission. Evolving-Hockey is $5/month. NST blocks automated fetch, and its terms were not found.

### Cited Findings
- Verified by probe (`/v1/gamecenter/2025020500/play-by-play`, 133 shot attempts). Each event has `situationCode` (e.g. "1551" = away goalie, away skaters, home skaters, home goalie), `homeTeamDefendingSide`, `periodDescriptor`, `timeInPeriod`, `timeRemaining`, `typeDescKey`. The `details` by event type:
  - shot-on-goal: `xCoord`, `yCoord`, `zoneCode`, `shotType`, `shootingPlayerId`, `goalieInNetId`, `eventOwnerTeamId`, `awaySOG`, `homeSOG`
  - goal: the same plus `scoringPlayerId`, assists, scores and highlight clips
  - missed-shot: adds `reason` (e.g. wide)
  - blocked-shot: `blockingPlayerId`, `reason`, coordinates; **no shotType**
  - The JSON contains no `xG`/`expected` field.
- Zmalski/NHL-API-Reference is an unofficial, MIT-licensed endpoint reference. It lists `/v1/gamecenter/{id}/play-by-play` and `/landing` but does not document the fields — [GitHub Zmalski/NHL-API-Reference](https://github.com/Zmalski/NHL-API-Reference)
- MoneyPuck: shot data has 2,079,359 shots for 2007-08 → 2025-26 with 124 attributes including xGoals, as CSV in zip. Season-level (skaters, goalies, lines, teams) and game-by-game data cover 2008-09 → 2026-27. Licence: "free to use for non-commercial purposes and by journalists for ad-hoc use", credit MoneyPuck.com, commercial use by email inquiry, and unapproved web scraping is prohibited — [MoneyPuck data](https://moneypuck.com/data.htm)
- Evolving-Hockey: $5/month subscription, with data downloads and a PBP Query tool — [Evolving-Hockey overview](https://evolving-hockey.com/evolving-hockey-overview/) (via search snippet)
- Natural Stat Trick: the homepage returned HTTP 403 to automated fetch. No terms were found — [naturalstattrick.com](https://www.naturalstattrick.com/)
- Python `hockey-scraper` scrapes NHL PBP and shift data since 2007-08 — [PyPI hockey-scraper](https://pypi.org/project/hockey-scraper/1.36.3/)

### Inferences
- A self-built xG from api-web PBP avoids all licensing issues. The NHL API itself has no published ToS for this unofficial endpoint. Derivable features: distance/angle from coordinates (normalise with `homeTeamDefendingSide`), shot type, strength state from `situationCode`, rebound/rush from previous-event time and position, and empty net from situationCode goalie digits.
- MoneyPuck xG is suitable for prototyping and validating your own xG. It must not ship in the premium bot without permission.
- Blocked shots lack shotType, so a Fenwick-based xG (unblocked attempts) is the natural choice, as public models do.

### Gaps
- Evolving-Hockey and NST commercial-use terms were not verified.
- Known NHL coordinate quality issues (rink-scorer bias) were not researched here.

## Historical injuries / lineups

### Takeaway
Historical lineups are effectively solved by the NHL API: post-game boxscore player lists, plus shift data via hockey-scraper. Historical *pre-game* injury status is poorly served by free sources. ESPN's public API gives current injury reports. Sportradar (official) has injuries but is enterprise-priced.

### Cited Findings
- Sportradar, the NHL official data partner, offers injuries, depth charts and in-game substitutions. Authentication is required — [Sportradar NHL v7](https://developer.sportradar.com/docs/read/hockey/NHL_v7)
- An Apify actor gives NHL injury reports as normalised JSON from ESPN's public API (athlete, team, status, injury type, comments), with no key needed — [Apify sports-injury-reports](https://apify.com/ichigowa/sports-injury-reports)
- hockey-scraper gives PBP plus shifts since 2007-08, which can reconstruct who actually played — [PyPI hockey-scraper](https://pypi.org/project/hockey-scraper/1.36.3/)
- Verified by probe: the post-game NHL boxscore lists all dressed goalies including the backup, with TOI 00:00. Skater lists are in the same structure.

### Inferences
- "Missing key player" features can be built historically as "played last game but absent from this boxscore" without any injury feed. Live use needs the day's projected lineup (DailyFaceoff line combinations, or ESPN injuries).

### Gaps
- No free archive of historical *daily* injury reports (status as of the morning of each game) was found.
- Sportradar pricing is not public. The ESPN injury endpoint's history depth is unknown.
