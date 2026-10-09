# Efficiency of NHL betting markets (moneyline and totals) and known exploitable biases

Research notes. Search budget: about 20 tool calls. Pinnacle Betting Resources (TLS certificate error), Pyckio (DNS failure), the Aalto theses and the UNCO full-text PDF (HTTP 403) could not be fetched. Some points therefore rest on abstracts or search snippets, and they are marked as such. Most academic NHL evidence covers 1990–2010. Peer-reviewed NHL-specific work after 2018 was hard to find.

## 1. How NHL lines are formed and whether the closing line is the best probability estimate

### Takeaway
Sharp market makers such as Pinnacle post low-margin lines and do not limit winning bettors. They use the betting flow to sharpen prices, so the closing price is widely treated as the best available estimate of true probability. Buchdahl's large-sample work supports this. Some evidence suggests that NHL moneylines barely improve in predictive power from opening to close. CLV is a necessary signal of skill, but on its own it does not prove profit.

### Cited Findings
- Pinnacle "uses its customers' betting activity to continuously sharpen the accuracy of its markets". It accepts sharp players to tighten its lines, and Buchdahl calls Pinnacle's closing odds the most efficient lines ("everything is efficient in full aggregate"). — [Tradematesports interview with Buchdahl](https://www.tradematesports.com/en/blog/interview-betting-expert-joseph-buchdahl); [PinnacleOddsDropper on Buchdahl](https://www.pinnacleoddsdropper.com/blog/joseph-buchdahl's-betting-strategy)
- Buchdahl's "Wisdom of the Crowd" method treats Pinnacle's de-vigged price as fair and bets soft books only when expected value exceeds 102%. Across about 18,000 value bets, predicted profit was 4.0% and actual profit was 3.7%. This is out-of-sample evidence that the sharp price is close to calibrated. (Multi-sport, mostly football. The source gives no NHL-specific breakdown.) — [PinnacleOddsDropper / Buchdahl](https://www.pinnacleoddsdropper.com/blog/joseph-buchdahl's-betting-strategy)
- Pinnacle does not limit winners, so sharp money corrects mispriced lines almost immediately. Retail books (DraftKings, FanDuel) restrict winning accounts, which lets soft prices linger. — [betherosports (practitioner blog)](https://betherosports.com/blog/how-to-use-pinnacle)
- A thesis on NHL moneylines found "little difference in predictive power of NHL moneyline betting odds at different time points throughout the life cycle of betting events". Opening and closing odds were about equally predictive. (Search-snippet level only; the full text returned 403.) — [Aalto thesis "When do betting odds best represent the actual outcomes?"](https://aaltodoc.aalto.fi/bitstreams/670d744b-68ff-4443-91d6-392f7c3aa321/download)
- CLV caveat (Karl Whelan, economist): in 3,670 NBA games (2022/23–2024/25, 34,944 quotes), only the top decile of positive-CLV bets made meaningful profit (+11.4%). Three of the five positive-CLV deciles lost money, and the 9th decile had about 5% CLV but only +0.2% profit. CLV has to exceed the vig, about 4.5% at NBA retail books, before it turns into profit. (NBA, not NHL, but the mechanism carries over.) — [Karl Whelan, "The Truth about Closing Line Value"](https://www.karlwhelan.com/?p=2595)

### Inferences
- For a modelling project, the de-vigged Pinnacle close is the natural benchmark. If it is unavailable, the consensus close across sharp books can stand in. The model needs to match or beat its log-loss, and the model's bets need positive CLV against it.
- If NHL opening lines are already nearly as accurate as closing lines (Aalto snippet), the room for early-line value is smaller than in less liquid markets. That finding should be treated as weak until the full text is read.

### Gaps
- No primary source on Circa's role in NHL price discovery, or on how fast NHL lines move after sharp bets. No primary quantification of opening-to-closing NHL line movement.
- Pinnacle's own articles on market efficiency could not be fetched because of a certificate error ([URL](https://www.pinnacle.com/betting-resources/en/educational/how-to-solve-a-problem-like-efficiency-part-one/rn8j5rnqj2p8t68p)).

## 2. Documented biases in NHL markets and whether they persist

### Takeaway
The reverse favourite-longshot bias (underdogs beat their implied returns) is the best-documented NHL bias. It was profitable in the early 1990s and largely disappeared as the market matured. An "under" bias on totals was documented for older data. A 2018 dissertation found the totals market "largely efficient". For popular-team bias, back-to-back mispricing, goalie-news timing, early-season mispricing or a Vegas inaugural-season effect in 2018–2026, I found no rigorous recent evidence, only practitioner blogs.

### Cited Findings
**Reverse favourite-longshot bias (moneyline)**
- Woodland & Woodland (2001), "Market Efficiency and Profitable Wagering in the National Hockey League: Can Bettors Score on Longshots?". Using 1990–1996 data, they found the market inefficient, with profitable opportunities on underdogs that did not appear to be shrinking. Woodland & Woodland (2001), Gandar et al. (2004) and Paul & Weinbach (2012) all report a reverse FLB in the NHL. — [ResearchGate (title)](https://www.researchgate.net/publication/227577382_Market_Efficiency_and_Profitable_Wagering_in_the_National_Hockey_League_Can_Bettors_Score_on_Longshots); summary of the literature via [Aalto thesis](https://aaltodoc.aalto.fi/bitstreams/670d744b-68ff-4443-91d6-392f7c3aa321/download) and search-result synthesis
- Gandar, Zuber & Johnson (2004), *Journal of Sports Economics* 5(2):152–168. Actual returns on underdog bets consistently exceeded expected returns, which supports a reverse FLB. They also corrected the Woodlands' commission calculation for unchanged money lines, which "substantially lower[s] the commission". — [IDEAS/RePEc](https://ideas.repec.org/a/sae/jospec/v5y2004i2p152-168.html)
- Woodland & Woodland (2011), *Journal of Sports Economics* 12(1):106–117, "The Reverse Favorite-Longshot Bias in the NHL: Do Bettors Still Score on Longshots?". Ten more seasons of data: "the bias is sustained for the first three seasons but disappears in the last seven seasons" as the market converged to efficiency. Baseball was the contrast case, where the bias persisted. — [IDEAS/RePEc](https://ideas.repec.org/a/sae/jospec/v12y2011i1p106-117.html); [EMU Commons](https://commons.emich.edu/accfin_facsch/91)
- For comparison, the MLB reverse FLB "appears to be permanent" over 1990–1999 data. — [Woodland & Woodland 2003, Bulletin of Economic Research](https://ideas.repec.org/a/bla/buecrs/v55y2003i2p113-123.html)
- Practitioner view: the bookmaker margin is not spread evenly between favourite and underdog, which is one mechanism behind apparent underdog value. — [Core Sports Betting (blog)](https://www.coresportsbetting.com/how-to-bet-on-nhl-underdogs-for-profit/)

**Totals (over/under)**
- Woodland & Woodland (2010), *Economics Bulletin*, "Market Efficiency and the NHL Totals Betting Market: Is There an Under Bias?". The totals market is "found to be inefficient and simple wagering strategies are identified that result in profitable returns". The bias identified is toward unders. — [IDEAS/RePEc](https://ideas.repec.org/a/ebl/ecbull/eb-10-00729.html); [ResearchGate](https://www.researchgate.net/publication/227410468_Market_Efficiency_and_the_NHL_totals_betting_market_Is_there_an_under_bias). Note: the EMU Commons summary of this paper mixed in text from another paper ("1990–99", "reverse FLB virtually identical"), so its data window is unverified. — [EMU Commons](https://commons.emich.edu/accfin_facsch/92)
- Traugutt (2018, Univ. of Northern Colorado dissertation) used a larger dataset of closing totals and odds and tested five heuristic-based strategies. "The NHL totals market was largely efficient, with only one strategy yielding a marginal above-average return." — [UNCO Digital Scholarship](https://digscholarship.unco.edu/dissertations/502)

**Back-to-backs / goalies**
- Blog analysis: 1,409 back-to-back goalie starts against about 10,000 rested starts. Goals allowed were 2.79 vs 2.77, so no measurable "tired goalie tax". — [DataStreak (blog)](https://thedatastreak.com/insights/nhl-goalies-back-to-back). A contradicting blog claims save percentage drops 1.8–3.2 points on road back-to-backs, to .898 vs .912 when rested. Its methodology is unclear and it should be treated as low quality. — [MyHockeyBet (blog)](https://www.myhockeybet.com/articles/how-back-to-back-road-trips-alter-goalie-save-percentages-and-shift-nhl-totals-markets/)
- Practitioner consensus: books price announced starting goalies and back-to-backs, and any edge lies in situations where they are mispriced, not in knowing that they matter. — [ice-hockey-bets / Core Sports Betting (blogs)](https://www.coresportsbetting.com/goalie-workload-nhl-betting/)

**Vegas Golden Knights inaugural season (2017/18)**
- The Knights went 51-24-7 in the regular season. Preseason Cup odds were as high as 500-1 (Westgate). Las Vegas books estimated a loss of about $5M (other reports: up to $7M) if Vegas won the Cup, and the bettors who backed the Knights came out ahead over the season. This is anecdotal futures-market evidence, not a game-line efficiency study. — [Covers](https://www.covers.com/Editorial/Article/fc42a411-5dc0-11e8-a97a-0a9aa1523ac0/Las-Vegas-sportsbooks-estimate-a-5-million-loss-if-Golden-Knights-win-the-Stanley-Cup-sports-betting-NHL-betting-best-Vegas-picks-schedule); [Las Vegas Sun](https://m.lasvegassun.com/news/2018/nov/19/after-first-year-rush-at-vegas-sports-books-bettin/)

### Inferences
- In every studied window the pattern is the same: an NHL bias is documented in early data and fades within a few seasons (moneyline reverse FLB gone after about 1993; totals "largely efficient" by the 2018 study). Treat any static rule such as "bet all underdogs" or "bet all unders" as arbitraged away unless it is re-tested on recent closing lines.
- An expansion team with no history (Vegas 2017/18, Seattle 2021/22) is the obvious place for priors to be wrong. The Vegas episode suggests the market underrated the team early. This is anecdotal and not a measured game-line ROI.

### Gaps
- I found no peer-reviewed study of NHL biases using 2018–2026 data (popular-team bias for Toronto or Montreal, goalie-announcement timing, early-season mispricing, Vegas game lines). Paul & Weinbach (2012) is cited but was not fetched.
- I found no quantitative public-vs-sharp money study for NHL teams.

## 3. Bookmaker margin and de-vigging

### Takeaway
Pinnacle's NHL margin is about 2–3%. US retail books (DraftKings, FanDuel) run about 4.5–5%. Fair probabilities are obtained with multiplicative (proportional), power, Shin or margin-proportional-to-odds normalisation. The non-proportional methods account for the margin falling more heavily on longshots.

### Cited Findings
- Pinnacle's margin is about 2% vs 4–5% at retail books. A cited overall average vig is about 2.7% at Pinnacle, about 4.7% at DraftKings and about 4.8% at FanDuel. Pinnacle's hold is described as 2–3% vs 4–6% elsewhere. (These are practitioner or comparison-site figures across sports, not NHL-specific audited numbers.) — [betherosports](https://betherosports.com/blog/how-to-use-pinnacle); [jedibets](https://jedibets.com/sportsbooks/pinnacle-odds)
- The early literature found the Woodlands' NHL commission estimate too high for unchanged money lines. The corrected commission was "substantially" lower. — [Gandar et al. 2004, IDEAS](https://ideas.repec.org/a/sae/jospec/v5y2004i2p152-168.html)
- The R package `implied` (Opisthokonta) implements the methods below. **Margin weights proportional to odds** (Buchdahl, "Wisdom of the Crowds") assumes the margin on each outcome is proportional to its odds, which reflects the FLB: p_i = (n − M·O_i)/(n·O_i). The **power / "logarithmic" method** uses p_i = r_i^(1/k), with k chosen so the probabilities sum to 1. The **Shin method** (Shin 1992/93) models a fraction of insider trading and a profit-maximising bookmaker. Basic normalisation (multiplicative), additive, odds-ratio and other methods are also included. — [implied package vignette (CRAN)](https://cran.uni-muenster.de/web/packages/implied/vignettes/introduction.html)
- Buchdahl's Pinnacle article "Why the favourite-longshot bias is not a bias" argues that the apparent FLB comes from the margin being loaded onto longshots. — [Pinnacle](https://www.pinnacle.com/betting-resources/en/betting-strategy/why-the-favourite-longshot-bias-is-not-a-bias/qjb2prcq4q96nftd) (page not fetchable; title and gist from search results)

### Inferences
- NHL moneylines are two-way (OT included) and usually close to even. At 1.5–2.5 decimal odds, the differences between de-vig methods are small, typically well under one percentage point of probability. Multiplicative de-vig is a reasonable default. Power or Shin should be used as a robustness check, and they matter more for heavy favourites.
- A model that only matches the market loses the full margin: about −2 to −3% ROI at Pinnacle-like prices and about −4.5 to −5% at retail.

### Gaps
- No audited, NHL-specific margin figures by season and book. No source on typical totals juice (for example how often 5.5 is priced −130/+110 versus ±110).

## 4. Totals line structure and scoring changes 2022–2026

### Takeaway
NHL totals usually sit at 5.5 or 6.5, with 6.5 described as the most common recent number. Books often move the juice rather than the number. I found no source that counts how often each number is used, or the share of 6.0 lines.

### Cited Findings
- "NHL totals are typically set at 6.5 goals, with sportsbooks adjusting the odds rather than the number to reflect each game's scoring outlook." — [LegalSportsReport](https://www.legalsportsreport.com/how-to-bet/hockey/)
- Totals are "usually between 5.5 and 6.5". — [Covers](https://www.covers.com/nhl/over-under-betting-tips)
- The standard puck line is ±1.5 at almost every book, and the hockey market "leans on the moneyline because a standard 1.5-goal handicap is large relative to typical final scores". — [LegalSportsReport](https://www.legalsportsreport.com/how-to-bet/hockey/); [betherosports](https://betherosports.com/blog/how-to-use-pinnacle)

### Inferences
- Because the number barely moves and the juice does most of the work, a totals model should be scored on de-vigged over/under probabilities at the posted number, not on the number itself. Price-based de-vigging matters more for totals than for moneylines.

### Gaps
- No source found on how often 5.5, 6.0 and 6.5 are used, on over/under hit rates by season (2022–2026), or on how the shift in totals tracked the rise in scoring after 2021–22. This could be built from our own odds data if we collect it.

## 5. Realistic edges, sample sizes, Kelly, and evaluation methodology

### Takeaway
Public NHL models perform roughly on par with the market or slightly worse. MoneyPuck's log-loss is about 0.656–0.661 per season, against a closing-line baseline of about 0.671 in one hobbyist study, and the two figures are not directly comparable (different samples). The value-betting evidence that does exist (Buchdahl, multi-sport) shows only about 3–4% ROI, and only against soft books, using sharp prices as the reference. Separating a 2–3% edge from luck takes thousands of bets.

### Cited Findings
- MoneyPuck pre-game model log-loss by season: 2020-21 0.6596; 2021-22 0.648; 2022-23 0.656; 2023-24 0.661; 2024-25 0.658. The favourite won 60.1–64.1% of games. No market comparison is published. — [MoneyPuck About](https://moneypuck.com/about.htm)
- A hobbyist NHL model write-up built a baseline from closing-line win probabilities, with log-loss 0.6714. It found xG-based models comparable to the market but still short of it. Sample and seasons are unverified. — [Medium, "Building An NHL Game Prediction Model"](https://medium.com/analytics-vidhya/building-an-nhl-game-prediction-model-part-1-1ee7596ff91b) (via search snippet)
- Buchdahl, Pinnacle as truth vs soft books: about 18,000 bets, +3.7% realised vs +4.0% predicted, with a 2% EV threshold. — [PinnacleOddsDropper](https://www.pinnacleoddsdropper.com/blog/joseph-buchdahl's-betting-strategy)
- Practitioner rule: a t-stat of per-bet returns below about 2 cannot tell a strategy apart from no edge. — search synthesis citing [simplefunctions.dev](https://simplefunctions.dev/concepts/from-sports-betting-clv-to-pm-trade-quality) (weak source)
- CLV must exceed the vig to turn into profit (NBA evidence above). — [Karl Whelan](https://www.karlwhelan.com/?p=2595)

### Inferences
These are my own arithmetic and standard theory, not sourced findings.
- **Sample size.** At odds of about 2.0 (even money), one flat-stake bet's return has a standard deviation of about 1 unit. To detect a true ROI e at t ≈ 2, roughly N ≈ (2/e)² bets are needed: e = 5% → about 1,600 bets; e = 3% → about 4,400; e = 2% → about 10,000. One NHL regular season has 1,312 games, so even betting every game for a full season cannot confirm an edge below about 5%. CLV per bet is far less noisy than results, which is why CLV is the primary short-run metric.
- **Required edge.** For positive ROI the model's probability has to exceed the bookmaker's *vigged* implied probability. At a 2.5% margin and odds near 2.0 that means about 1.25 pp above fair. At a 5% retail margin it means about 2.5 pp.
- **Log-loss.** The two-way (OT-included) NHL baseline is about 0.67–0.68 for a coin flip near 55/45. The market close sits around 0.66–0.67. Real-world winning margins over the market are small (third decimal). A model whose log-loss is clearly above the de-vigged close on the same games should not be bet. Compare on identical game sets.
- **Kelly.** The Kelly fraction is f* = (b·p − q)/b, where b = decimal odds − 1. Because model probabilities are overconfident, practitioners use fractional Kelly (¼–½). Kelly is very sensitive to a mis-estimated p. With an edge of 1–3 pp, full Kelly stakes are about 2–6% of bankroll.
- **Recommended evaluation protocol for this project:** (1) a walk-forward backtest that uses only information available before puck drop; (2) log-loss and Brier score compared with the de-vigged closing line on the same games; (3) simulated ROI at the *opening* or bet-time price with the actual margin; (4) average CLV of the model's picks; (5) results split by favourite/underdog odds bucket and over/under, to detect a rediscovered reverse FLB or under bias.

### Gaps
- I found no academic NHL-specific ROI study for a model against closing lines after 2015. No Buchdahl or Pinnacle NHL-specific CLV-to-ROI figures could be fetched.
