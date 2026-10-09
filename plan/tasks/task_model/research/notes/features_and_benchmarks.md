# Pre-game NHL models: which features carry signal, and realistic benchmarks

Scope: pre-game prediction of NHL home win (OT/SO included) and, where sources exist, game totals. Research done 2026-10-09; about 20 tool calls. Some primary sources would not load: the Weissbock thesis PDF could not be parsed, and the Maclean's page returned 403. Those points rely on abstracts or snippets and are flagged.

## 1. Feature classes and their measured relative value

### Takeaway
The only published, quantified weighting from a top public model is MoneyPuck's. Scoring chances (talent-adjusted xG, shots, attempts) carry 54% of the weight, goaltending 29%, and win% / "ability to win" 17%. On top of that come home ice (about 54% home win rate) and rest (about -4 pp for a team on a back-to-back when the opponent is rested). Rest is the best-measured situational feature. I found no rigorous public ablations for travel/time zones, special teams or injuries.

### Cited Findings
- MoneyPuck pre-game model (rebuilt January 2025, trained on 2017-18 to 2023-24) has three components: **Ability to Win 17%** (team win%, recent games emphasized), **Scoring Chances 54%** (shooting-talent-adjusted xG, actual goals, shot attempts, shots on goal, defensive-zone giveaways), **Goaltending 29%**. The rebuild emphasized goaltending and *reduced* the weight on recent-game fluctuations. — [MoneyPuck about](https://moneypuck.com/about.htm)
- MoneyPuck: "Home teams win ~54% of NHL games, partially driven by refereeing bias"; having played the day before when the opponent did not "decrease[s] a team's chances of winning by ~4%." — [MoneyPuck about](https://moneypuck.com/about.htm)
- MoneyPuck goalie rating uses each team's current goalies over several past seasons, weighted toward recent play. GSAx/60 and save% are the largest factors, plus how often the goalie was recently pulled. Each goalie's contribution is proportional to his share of games played, so it is a team-level goalie blend rather than the confirmed starter. — [Japers' Rink interview with Peter Tanner, 2021](https://www.japersrink.com/an-interview-with-peter-tanner-founder-of-moneypuck-05-14-2021/), [MoneyPuck about](https://moneypuck.com/about.htm)
- Rest, salary-cap era 2005-06 to 2025-26 (50,568 team-games):
  - Win rate: 0 days rest **45.0%**, 1 day 50.8%, 2 days **52.1%**, 3+ days 50.1%.
  - Goal differential per game: 0 days rest **-0.274**, 1 day +0.038, 2 days +0.119, 3+ days +0.042.
  - Home on zero rest 50.9% vs road on zero rest **42.2%**.
  - Zero-rest games are about 15-17% of team schedules (13.7% in 2023-24).
  - — [nhlscraper vignette "How Costly Are Back-to-Backs in the Salary-Cap Era?"](https://ftp2.uib.no/cran/web/packages/nhlscraper/vignettes/back-to-back-tax.html)
- Older rest study: back-to-back road teams win 41.0% vs 44.0% for all road teams; back-to-back home teams 53.0% vs 56.0%. That is about a 3 pp drop when the opponent is rested. Older NHL.com analysis found that the rested-vs-tired goalie gap widened around 2011-13; follow-ups suggest a smaller effect on save%. — [NHL.com "Analytics say rest goalies on back-to-backs"](https://www.nhl.com/ice/m_news.htm?id=734778) (via search snippet)
- Home ice, betting-market sample 2007-08 to Nov 2022: home teams win **54.4%** (closing lines implied 54.8%). In the latest 5 seasons of that sample the home team won only **53.4%**. Low point was 2010-11, below 52%. — [hockey-statistics.com Betting Market Analysis (2024)](https://hockey-statistics.com/2024/03/22/betting-market-analysis/)
- FiveThirtyEight / Neil Paine NHL Elo:
  - +50 Elo home ice, which gives 57.1% for evenly matched teams.
  - K=6, with a margin-of-victory multiplier `0.6686·ln(MOV)+0.8048`.
  - No goalie, rest or travel adjustments.
  - — [Neil Paine, How my NHL Elo ratings work](https://neilpaine.substack.com/p/how-my-nhl-elo-ratings-and-forecast); [538 methodology](https://fivethirtyeight.com/methodology/how-our-nhl-predictions-work)
- Weissbock (U. Ottawa thesis, 2014): for single games, **traditional statistics proved more valuable than advanced metrics** for automated prediction (about 60% accuracy). Adding text and sentiment from NHL.com expert previews in a meta-classifier pushed accuracy "close to the upper bound." — [Weissbock thesis abstract](https://ruor.uottawa.ca/handle/10393/31553)
- Globe and Mail "Forecheck" model (2017-18) is a multilayer perceptron on about 2,000 team and player variables, with star players and recent data weighted more. Its weights are learned, and no per-feature importances are published. — [Globe and Mail methodology](https://theglobeandmail.com/sports/hockey/nhl-predictions-hockey-methodology/article37603494/?cmpid=rss1)
- HockeyViz retrospectives (2016-17, 2018-19, 2020-21) compare Magnus/Cordelia (McCurdy), MoneyPuck, Luszczyszyn, Evolving-Hockey, Shomer, Chernos, Barlowe and Pinnacle closing lines. Conclusion: "every model is very slightly better than the guessing benchmark … the differences between models in overall performance is extremely slight." Some models are deliberately conservative (probabilities near 50%) and others "extravagant." — [HockeyViz retro 2018-19](https://hockeyviz.com/txt/retro1819), [retro 2020-21](https://hockeyviz.com/txt/retro2021)

### Inferences
- The feature ranking implied by public practice is:
  1. xG-based team strength (5v5 and all-situations xG share, talent-adjusted).
  2. Goaltending (multi-season GSAx, regressed).
  3. Results-based strength (win% / Elo).
  4. Home ice (about +3.5-4 pp over 50%).
  5. Rest asymmetry (about 3-4 pp when only one side is on a back-to-back).
- Special teams, travel and injuries are second-order, or at least unquantified in public sources.
- The "traditional > advanced" result in Weissbock (2014, pre-xG era, Corsi/Fenwick/PDO) conflicts with MoneyPuck's 54% xG weight (2025). The likely reason is that modern xG models are much better than the 2014 shot-attempt proxies.

### Gaps
- I found no public ablation tables showing log-loss change per feature group (e.g. "+xG improves LL by X") from MoneyPuck, Evolving-Hockey, HockeyViz or The Athletic.
- Travel/time-zone effects, PP/PK value and injury/lineup value (e.g. Luszczyszyn's Game Score / Net Rating roster-based approach) have no quantified pre-game effect in the sources I reached.
- Totals: I found no rigorous public totals model or feature study. Only low-quality betting blogs (ParlayTools, Bettoredge) came up, and they make unquantified claims, so I excluded them.

## 2. Starting goalie effect on win probability and totals

### Takeaway
Goaltending is the second-largest component in MoneyPuck (29%). However, MoneyPuck historically predicted the night before *without* the confirmed starter, using a GP-share-weighted blend. I found no rigorous public number for the starter-vs-backup swing. Betting-industry sources claim about 3-10 pp of win probability and about 0.5 goal on the total, but those come from low-reliability blogs.

### Cited Findings
- MoneyPuck (2021): "all of our game predictions are made the night before the game and do not factor in starting goalies." They built a bot that detects journalist tweets about confirmed starters and planned to factor starters in later. — [Japers' Rink interview, 2021](https://www.japersrink.com/an-interview-with-peter-tanner-founder-of-moneypuck-05-14-2021/)
- MoneyPuck's goalie component weights each goalie by his share of games played. — [MoneyPuck about](https://moneypuck.com/about.htm)
- After the fact (not pre-game), crediting a goalie with 1.00 GSAx in a game is associated with roughly a 33% higher chance to win. This is a post-hoc, in-game quantity, not a pre-game effect. — [theScore "Ice Insanity" Part 3](https://www.thescore.com/nhl/news/2871892) (via search snippet)
- Industry claims (low reliability, no methodology shown):
  - A starter is "worth roughly 3 to 5 points of implied win probability."
  - Starter→backup can move the moneyline 30-50 cents and the total by half a goal.
  - "can swing a game's probability by 5-10%."
  - — [MyBookie guide](https://www.mybookie.ag/sports-betting-guide/goalie-confirmations-and-line-movement/), [SportBotAI blog](https://www.sportbotai.com/blog/nhl-backup-goalie-betting-ai-predictions-value)
- Arithmetic check from one of those blogs: on 30 shots, a .925 vs .900 save% gap is about 0.75 goals. — [SportBotAI blog](https://www.sportbotai.com/blog/nhl-backup-goalie-betting-ai-predictions-value)
- Goalie save% is noisy. At about 1,500 shots a 0.925 EV goalie is "performing near or at their skill level," and analysts often use far larger thresholds, so heavy regression is needed. — [Hockey-Graphs, The State of Save Percentage (2014)](https://hockey-graphs.com/2014/11/10/the-state-of-save-percentage/)

### Inferences
- In true-talent terms, the gap between regressed starter and backup GSAx is usually about 0.2-0.5 goals per game. Under the typical rule of thumb that 1 goal ≈ 15-20 pp near 50%, that gives about 3-8 pp. This is consistent with the industry range, but it is my estimate, not a sourced figure.
- The same gap raises expected total goals by about 0.2-0.5.
- Practical implication: unless the model uses confirmed starters near puck drop, it is structurally behind the closing line on exactly this feature. The market prices the confirmed starter, and MoneyPuck's night-before model historically did not.

### Gaps
- I found no peer-reviewed or methodology-backed measurement of starter vs backup on win probability or totals. Evolving-Hockey and The Athletic methodology pages were not reached; The Athletic is paywalled.

## 3. Early-season cold start

### Takeaway
Two philosophies exist:
- **Blank slate:** MoneyPuck and Sports Club Stats historically started each season from zero.
- **Prior-informed:** Luszczyszyn and McCurdy start from roster-based or prior-season priors.

The prior-informed models are clearly better in October; all models converge around the end of October. Elo-type systems carry over 70% of the prior rating.

### Cited Findings
- HockeyViz 2016-17: "In the earliest part of the season, Dominik Luszczyszyn's model is clearly best, mine is decent, and the others are barely above zero." MoneyPuck and SCS "begin from a 'zero information' start." All models "show a sharp change around the end of October." — [HockeyViz retro 2016-17](https://hockeyviz.com/txt/retro1617)
- 538/Paine NHL Elo carries 70% of the prior rating forward and reverts 30% toward 1505. — [Neil Paine](https://neilpaine.substack.com/p/how-my-nhl-elo-ratings-and-forecast)
- The Athletic's model (Luszczyszyn, Net Rating since 2023) is built on player-level ratings aggregated over projected rosters. That is the roster-weighted-prior approach. — search snippet referencing [Atlantic Hockey substack](https://atlantichockey.substack.com/p/week-0-wrap-up)
- Standings regression: "Add 74 games of .500 ball to the actual performance" to estimate talent. In the old NHL with ties, about 36 games were needed before talent mattered as much as randomness. — via [The Five Hohl](https://thefivehohl.substack.com/p/what-does-regression-to-the-mean) / [jfresh](https://jfresh.substack.com/p/percentage-luck-in-hockey-explained) (search snippet; original Tango/Birnbaum-style analysis)

### Inferences
- A sensible design is: prior-season rating shrunk about 30% to the mean (or roster-weighted), blended with season-to-date stats. The prior's weight should decay over about 20-40 games.
- With 5 seasons, the first ~10% of each season (~130 games/season) is where a no-prior model loses the most.

### Gaps
- I found no published optimal decay schedule or ablation for blending the prior with season-to-date stats.

## 4. Realistic benchmarks: accuracy, log-loss, closing line, randomness ceiling

### Takeaway
Realistic numbers for good public models:
- Accuracy is about **60-62%** (64% in the unusually lopsided 2021-22).
- Log-loss is about **0.648-0.661**, against 0.693 for a 50/50 guess and roughly 0.689-0.690 for a constant home-rate predictor.
- Closing lines are very well calibrated and reach about **0.642** log-loss in a favourable season.
- The theoretical single-game ceiling is about **62%** accuracy, with about 38% of standings variance attributed to luck.

### Cited Findings
- MoneyPuck by season (favorite win% / log loss): 2020-21 60.1% / 0.6596; 2021-22 64.1% / 0.648; 2022-23 60.6% / 0.656; 2023-24 61.1% / 0.661; 2024-25 60.4% / 0.658. — [MoneyPuck about](https://moneypuck.com/about.htm)
- Globe and Mail Forecheck, 2017-18 (to Jan 24, 2018): accuracy 62.9%, log loss 0.6479. This is a partial season. — [Globe and Mail](https://theglobeandmail.com/sports/hockey/nhl-predictions-hockey-methodology/article37603494/?cmpid=rss1)
- Betting market:
  - Sample: BetUS moneylines, 2007-08 to Nov 2022.
  - Calibration: favorites won 58.8% on closing lines vs 58.6% implied; on opening lines 58.6% vs 58.2% implied. Closing log loss is lower than opening.
  - 2021-22: favorite won 65%, closing log loss **0.6421**.
  - Playoffs have higher log loss, and opening lines beat closing there.
  - — [hockey-statistics.com](https://hockey-statistics.com/2024/03/22/betting-market-analysis/)
- Constant 50% baseline: log-loss ln 2 ≈ 0.693. "Most models are slightly better than the guessing benchmark… differences between models … quite small." HockeyViz included Pinnacle closing lines as the reference. — [HockeyViz retro 2020-21](https://hockeyviz.com/txt/retro2021)
- Weissbock (2014): about 60% accuracy (59.8% reported) and a "theoretical upper bound of approximately 62% for single game prediction in the NHL." Of standings variation, 37.6% is attributed to luck. — [Weissbock abstract](https://ruor.uottawa.ca/handle/10393/31553); 59.8% and 37.6% via [The Conversation](https://theconversation.com/why-luck-plays-such-a-big-role-in-hockey-224598) / search snippets
- About 38% of standings differences are luck (Tango-style variance decomposition), consistent with Weissbock. Another estimate puts luck at about 53% of a season record. — [PBS/The Conversation](https://www.pbs.org/newshour/science/why-does-luck-play-such-a-big-role-in-hockey-games) (search snippet)
- Amateur claims of 65-69% accuracy (e.g. "SPAM" model: 2023-24 68.7%, 2022-23 65.3%) are almost certainly not like-for-like. They are probably small or partial samples, or include playoffs. — [6on5 substack](https://6on5.substack.com/p/my-game-and-series-prediction-model)
- The academic outlier Gu et al. (2016) reports 77.5% on **89 playoff games**. That is a tiny sample and combines expert judgments, so it is not a credible benchmark. — [IJITDM via RePEc](https://ideas.repec.org/a/wsi/ijitdm/v15y2016i04ns0219622016400022.html)

### Inferences
- A constant home-win-rate predictor with p≈0.535-0.54 gives log-loss ≈ 0.690. Calculation: −[p ln p + (1−p) ln(1−p)] at p=0.535 is 0.6907. So the realistic "skill" headroom is about 0.03-0.04 nats, and the gap between a decent model and the closing line is about 0.005-0.015.
- Season-to-season variance is comparable to the gap between models: MoneyPuck ranges 0.648-0.661 across seasons. Any comparison must therefore be done on the *same games* against the closing line, not against published numbers from other seasons.
- Target for a well-built model on about 6,000 games: log-loss of about 0.655-0.665 and accuracy of about 59-61%. Beating the closing line consistently is not supported by any public source I found.

### Gaps
- Exact per-season log-loss for Pinnacle closing lines, Evolving-Hockey and The Athletic was not available in text. The HockeyViz retros present them only as charts, and The Athletic is paywalled.
- There is no public log-loss benchmark for totals (over/under) models.

## 5. Season-to-date vs rolling windows; number of rolling features

### Takeaway
The strongest practitioner signal comes from MoneyPuck's 2025 rebuild. It explicitly *reduced* the weight on recent-game fluctuations and moved toward longer-horizon, regressed measures. Elo-type evidence points the same way: a small K (6) means slow updating. Both suggest that short rolling windows mostly add noise.

### Cited Findings
- MoneyPuck's 2025 rebuild reduced the weight on "recent game fluctuations" and emphasized goaltending. — [MoneyPuck about](https://moneypuck.com/about.htm)
- 538 / Paine Elo uses an optimal K-factor of 6 (slow updates). — [Neil Paine](https://neilpaine.substack.com/p/how-my-nhl-elo-ratings-and-forecast)
- About 36 games are needed for talent to equal randomness in standings, so short windows (5-10 games) are dominated by luck. — [jfresh](https://jfresh.substack.com/p/percentage-luck-in-hockey-explained) (search snippet)
- The Globe and Mail weights recent data more but smooths outputs by averaging the prior 5 days' probabilities to reduce volatility. — [Globe and Mail](https://theglobeandmail.com/sports/hockey/nhl-predictions-hockey-methodology/article37603494/?cmpid=rss1)

### Inferences
- Total skill signal is only about 0.03-0.04 nats, and there are about 6,000 training games. So a compact feature set is appropriate:
  - season-to-date / exponentially-weighted xG share (5v5 and all situations);
  - regressed goalie GSAx;
  - Elo or win-based rating;
  - home flag;
  - rest differential.
- That is roughly 5-15 differential (home minus away) features. Many parallel rolling windows (5/10/20 games × many stats) are likely to overfit.
- An exponentially weighted average with a long half-life is preferable to several hard windows.

### Gaps
- I found no published ablation of rolling-window length vs season-to-date for NHL log-loss.
