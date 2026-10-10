# Goal-count models for NHL: P(home win incl. OT/SO) and P(total > 5.5)

Research pass: 2026-10-09, ~18 tool calls. Several PDFs (Buttrey/Washburn/Price 2011, Marek et al., Poisson Toolbox) could be downloaded but not text-extracted in this environment (no PDF tooling), so their claims below come from abstracts or search snippets, not full text. Formulas in "Inferences" are standard derivations, not quotes from sources.

## 1. Which distribution fits NHL goals: Poisson, Dixon-Coles, bivariate Poisson, NB, COM-Poisson, Skellam?

### Takeaway
The NHL literature consistently treats goals as close to Poisson. Team goals per game pass chi-square tests against Poisson. Overdispersion is mild at most and comes mostly from game-state effects (empty-net goals, score effects), not from the base scoring process. Hockey-specific refinements model correlation or excess ties (bivariate Poisson with tie inflation, as in Marek et al.), not heavy tails. Independent Poisson with attack/defence/home terms is a defensible baseline. The total-goals market is where its weaknesses (empty-net goals, OT settlement) matter most.

### Cited Findings
- Early NHL evidence: for 1973-74, goals for and against, home and away, followed a Poisson distribution for each of the 16 teams. Mullet (1977) first used a Poisson model for NHL game goals. A Poisson fit was also confirmed for Stanley Cup series winners over 438 games. — [Dejardine, "Poisson Processes and Applications in Hockey" (Lakehead)](https://lakeheadu.ca/sites/default/files/uploads/77/docs/DejardineFinal.pdf)
- Alan Ryder's "Poisson Toolbox" (hockeyanalytics.com) reports that NHL goals per game pass a chi-square test against Poisson "with flying colours", with very small deviations across multiple seasons. This comes from a search snippet; the full PDF could not be parsed. — [Poisson Toolbox, hockeyanalytics.com](http://hockeyanalytics.com/Research_files/Poisson_Toolbox.pdf)
- Buttrey, Washburn & Price (JQAS 7(3), 2011, "Estimating NHL Scoring Rates"): "goals occur as if from a Poisson process whose rate depends on the two teams playing, the home-ice advantage, and the manpower (power-play, short-handed) situation". Fitted on 2008-09. It "seems to perform adequately in prediction and should be useful for handicapping and for informing the decision as to when to pull the goalie". — [Buttrey et al. PDF](https://faculty.nps.edu/awashburn/docs/EstimatingNHLScoringRates.pdf); abstract via [search/studylib](https://studylib.net/doc/18916597/estimating-nhl-scoring-rates)
- Thomas (JQAS 3(3), 2007, "Inter-arrival Times of Goals in Ice Hockey"): scoring is better described as a semi-Markov process than as a pure Poisson process. A goal effectively shortens the rest of the game by about 20 s (faceoff reset). Over 4 seasons and 25,000+ goals, goals were evenly spread except for fewer in the first minutes of each period and significantly more at the end of the 3rd period. — [Thomas 2007 record](https://lida.sport-iat.de/twm/Record/4013457?lng=en); [full text link](http://hockeyanalytics.com/Research_files/Interarrival%20Times%20of%20Goals%20in%20Ice%20Hockey.pdf); summary in [LiveScience](https://livescience.com/3637-goal-hockey-raise-odds-winning.html)
- Marek, Šedivá & Ťoupal (JQAS 2014, "Modeling and Prediction of Ice Hockey Match Results") tested football models (Maher/Dixon-Coles style) on hockey. They proposed a new model based on an alternative definition of the bivariate Poisson distribution that raises the probability of ties (draws after regulation). Data: Czech Extraliga 1999-2012. — [ASA magazine JQAS highlights](https://magazine.amstat.org/blog/2014/10/01/jquas/); [author draft (ResearchGate, 403 to fetch)](https://www.researchgate.net/profile/Patrice-Marek/publication/272536864_Modeling_and_prediction_of_ice_hockey_match_results/links/5665b1f108ae192bbf925c19/Modeling-and-prediction-of-ice-hockey-match-results.pdf)
- General sports-score modelling: most past hockey research uses either Poisson or negative binomial. The common way to handle overdispersion is to replace each Poisson marginal with a negative binomial. The generalized Conway-Maxwell-Poisson (GCOM-P) nests NB and handles both over- and under-dispersion. — search synthesis over [ResearchGate "A Bayesian Approach to Predict the Number of Goals in Hockey"](https://www.researchgate.net/publication/332550462_A_Bayesian_Approach_to_Predict_the_Number_of_Goals_in_Hockey) and [arXiv 2103.07272](https://arxiv.org/pdf/2103.07272)
- Harris, Yang & Hardin (2012) proposed a generalized Poisson whose dispersion parameter can be positive (over-), zero, or negative (under-dispersion). COM-Poisson also covers both directions. — [Discrete distributions from a Markov chain, arXiv 2006.13766](https://arxiv.org/pdf/2006.13766) (search snippet)
- Dixon-Coles (1997) builds on Maher's model, with attack, defence and home advantage. Dependence between the two teams' scores is concentrated in low-scoring games, and independent Poisson underprices 0-0 and 1-1. — [penaltyblog docs overview](https://penaltyblog.readthedocs.io/en/latest/models/overview.html); [arXiv 2508.05891](https://arxiv.org/html/2508.05891v1)

### Inferences
- **Dispersion verdict for NHL.** Per-team regulation goals are near-equidispersed. Raw full-game totals (with empty-net goals and the "+1" OT/SO goal) are mildly overdispersed relative to a single Poisson. If you model goals excluding empty-net goals and OT, Poisson is likely adequate. NB adds value mainly when the raw final score is the target. This should be checked on our own data: compute var/mean of per-team regulation goals with and without empty-net goals over the 5 seasons in the DB. No source gave a current-era NHL dispersion index.
- **Skellam.** If home and away goals are independent Poisson(λh), Poisson(λa), the regulation goal difference D is Skellam:
  P(D=k) = e^{-(λh+λa)} (λh/λa)^{k/2} I_|k|(2√(λhλa)).
  This gives P(home wins in regulation) = P(D>0) and P(tie after 60) = P(D=0) directly via `scipy.stats.skellam`. It cannot give totals, so a full score grid is simpler and covers both markets.
- **Bivariate Poisson** (Karlis-Ntzoufras): X = Y1+Y3, Y = Y2+Y3, Y3 ~ Poisson(λ3) shared. The positive covariance λ3 increases P(tie) and the variance of the total, but leaves the goal-difference distribution the same as independent Poisson(λ1), Poisson(λ2). For hockey, the main need is *tie inflation* (Marek et al.) because ties feed OT. Dixon-Coles τ (which adjusts only 0-0, 1-0, 0-1, 1-1) is the cheap version, but in hockey 2-2 and 3-3 ties are common, so the original football τ is a poor fit. A diagonal-inflation factor on all k-k cells (one parameter, fitted by ML) is a better analogue. This is our inference; the exact Marek formulation was not read.
- COM-Poisson needs a normalizing constant without closed form and has no scipy/statsmodels implementation. Given the near-Poisson evidence, it is not worth the complexity here.

### Gaps
- No source found with a current (post-2015) NHL variance/mean ratio for team goals or totals.
- Marek et al. full results (log-likelihood comparison of models) not read (403).
- Buttrey et al. quantitative results vs betting lines not read (PDF not parseable here).

## 2. Overtime / shootout handling and totals settlement

### Takeaway
Win probability = P(reg win) + P(tie after 60) × P(win OT/SO). In the 3v3 era, about 20-24% of games reach OT; 2025-26 ran higher, about 27% early in the season. About a third to two-fifths of OT games historically went to a shootout. For betting settlement, OT counts fully and a shootout adds exactly 1 goal to the winner and to the total. So any regulation tie at 3-3 or higher guarantees Over 5.5, and a 2-2 tie guarantees exactly 5 goals (Under 5.5).

### Cited Findings
- Totals: "Once overtime starts, for official scoring purposes, the game is assured to have one more goal — and one more goal only". Shootout counts toward the total. Moneyline includes OT/SO. Puck line is decided at the end of regulation if tied (-1.5 loses, +1.5 wins). Some books offer "60-minute"/"regulation only" 3-way markets. Shootout performances do not count for player props. — [Action Network: NHL betting rules for OT & shootouts](https://www.actionnetwork.com/nhl/nhl-betting-rules-overtimes-shootouts)
- "In the event of a shootout, the winner of the shootout will have one (1) goal added to its score and one goal will be added to the game total, regardless of the number of shootout goals scored". Example: total 6.5 with 3-3 after regulation means Over has already won. Check house rules for regulation-only markets. — [Gila house rules (hockey)](https://help.playatgila.com/en/general-information/sports-house-rules/hockey-wager-types-and-rules); [Action Network team totals](https://www.actionnetwork.com/nhl/do-nhl-team-total-goals-count-after-overtime-shootouts-its-complicated)
- Share of games reaching OT/SO: 2016-17 23.5%, 2017-18 23.3%, 2018-19 21.3%, 2019-20 23.1%, 2020-21 22.5%, 2021-22 22%, 2022-23 23%, 2023-24 20.7%, 2024-25 20.5%. In 2025-26, 27.3% of the first 425 games went to OT/SO, which would be the highest since 2005-06 (previous high 25% in 2013-14). — [ESPN 2025-26 OT/shootout story](https://africa.espn.com/nhl/story/_/id/47210375/nhl-2025-26-games-shootout-theories-trends-stats-standings)
- A second source gives slightly different OT shares (2015-16 21%, 2016-17 24%, 2017-18 23%, 2018-19 21%, 2019-20 28%). Share of OT games ending in a shootout: 2015-16 42%, 2016-17 34%, 2017-18 35%, 2018-19 32%, 2019-20 37%. — [NBC Sports: 3-on-3 OT evolved over 5 seasons](https://www.nbcsports.com/nhl/news/3-on-3-overtime-in-nhl-has-evolved-over-past-5-seasons). **Conflict:** ESPN gives 23.1% for 2019-20 vs NBC's 28%. The ESPN fetch also gave "65.5% of tied-after-regulation games decided in shootout" and "108 shootouts" for 2025-26. Both look inconsistent with 27.3% × 425 ≈ 116 OT games and with historical 32-42%, so treat them as unreliable (likely a fetch/summary artefact).

### Inferences
- **Win probability formula.** From a regulation score grid P(i,j):
  - p_reg_home = Σ_{i>j} P(i,j)
  - p_tie = Σ_i P(i,i)
  - P(home win) = p_reg_home + p_tie · q_home, where q_home = P(home wins OT/SO).
  - Simple q_home: 3v3 OT is 5 minutes sudden death. With 3v3 scoring rates μh, μa per minute (much higher than 5v5), P(OT goal) = 1 − e^{-5(μh+μa)}, home share μh/(μh+μa). If no goal, the shootout is about 0.5. Empirically, ~60-68% of OT games end in OT (from the NBC shootout shares above), which pins μh+μa. A pragmatic choice is q_home = 0.5 + small home/strength tilt, fitted on the DB's OT/SO outcomes. With 5 seasons there are ~1,400 OT games, enough for 1-2 parameters.
- **Total > 5.5 (full game, as books settle).** Let T = regulation total. Then
  final total = T + 1·[tie after 60], so
  P(Over 5.5) = Σ_{i≠j, i+j≥6} P(i,j) + Σ_{i≥3} P(i,i) = P(non-tie with T≥6) + P(tie at 3-3 or higher).
  A 2-2 tie becomes 5 (Under). A 3-3 tie becomes 7 (Over). Ties never land on 6. Ignoring this "+1 on ties" rule biases P(Over 5.5) down. With p_tie ≈ 0.22 and roughly half the ties at 3-3 or higher at modern scoring rates, the error is several percentage points.
- If the grid is fitted on 60-minute goals **including** empty-net goals, the formula above is consistent with settlement. If fitted excluding empty-net goals, add them back (see §3), otherwise totals will be underestimated.

### Gaps
- No authoritative NHL-wide table found for OT/SO shares for 2020-21 to 2024-25. Compute this from our own DB (`game outcome type` REG/OT/SO), which is more reliable than press sources.
- No source on the empirical home win rate in 3v3 OT or the shootout home win rate. Estimate from the DB.

## 3. Empty-net goals and score effects

### Takeaway
Empty-net goals (ENG) are now a material and growing part of NHL scoring, about 7% of goals and ~0.4 per game. They are concentrated in the final minutes of games decided by 1-2 goals. They inflate totals and the margin of victory (turning 1-goal wins into 2-goal wins), which matters for the puck line and pushes totals above what a constant-rate Poisson predicts.

### Cited Findings
- 7.0% of all goals were empty-net through the first quarter of 2024-25 (1 in 14), vs 2.4% in 2005-06 (1 in 42). About 0.42 ENG per game in a recent sample. Teams averaged 13.8 ENG in 2023-24. — [Daily Faceoff: rise of empty-net goals](https://www.dailyfaceoff.com/news/how-empty-net-goals-have-changed-the-nhl)
- Coaches pull goalies earlier, often with 3-4+ minutes left, because research showed earlier pulls raise the tying probability. — [FiveThirtyEight](https://fivethirtyeight.com/features/nhl-coaches-are-pulling-goalies-earlier-than-ever); [Daily Faceoff](https://www.dailyfaceoff.com/news/how-empty-net-goals-have-changed-the-nhl)
- 2011-12 had 238 ENG, almost all in the final minutes of the 3rd period (the "end game effect"). — [Dejardine thesis](https://lakeheadu.ca/sites/default/files/uploads/77/docs/DejardineFinal.pdf)
- Goals are significantly more frequent at the end of the 3rd period (Thomas, 4 seasons, 25,000+ goals). — [LiveScience on Thomas](https://livescience.com/3637-goal-hockey-raise-odds-winning.html)
- Buttrey et al. model scoring rates by manpower state, and the paper is explicitly aimed at informing goalie-pull decisions. — [Buttrey et al. abstract](https://faculty.nps.edu/awashburn/docs/EstimatingNHLScoringRates.pdf)

### Inferences
- Practical approaches, in order of cost:
  1. **Ignore**: fit Poisson on 60-min goals including ENG. Strength estimates absorb an average ENG rate, but the grid shape is wrong. Too few 1-goal margins and too few 2-goal margins get created in the right places.
  2. **Two-stage (recommended)**: fit team rates on non-ENG regulation goals (cleaner signal of strength). Then post-process the grid: for each regulation state with |margin| = 1 or 2, apply an empirical ENG transition kernel estimated from our DB. For example, P(trailing team concedes an ENG | lead 1) ≈ p1, P(trailing team scores to tie | lead 1, goalie pulled) ≈ p_tie_back. This is a small Markov adjustment and preserves totals calibration.
  3. **Simulation**: minute-by-minute simulation with state-dependent rates (score effects plus pulled goalie). This is the most faithful approach (Buttrey-style) but more code.
- Score effects (trailing teams outshoot leaders) mostly cancel in goal totals but compress the margin distribution, which is another reason ties (OT) are more frequent than independent Poisson predicts. This supports a tie-inflation parameter.

### Gaps
- No published quantitative ENG transition probabilities by lead size and time found in this pass. Estimate from our play-by-play (NHL API has `emptyNet`/situationCode).

## 4. Time decay, home advantage, regularization / hierarchical Bayes, small data

### Takeaway
Use the Maher/Dixon-Coles log-linear structure log λ = μ + home + att_i − def_j, with exponential time-decay weights w = exp(−ξ·Δt) chosen by out-of-sample log-loss. Add L2 shrinkage (or a hierarchical prior à la Baio & Blangiardo) because 32 teams × 2 parameters on ~1,300 games per season is noisy early in the season.

### Cited Findings
- Dixon-Coles weights older matches by exp(−ξt). ξ is chosen by grid search on a validation period. One football study found ξ = 0.002/day (half-life ≈ 347 days). — [arXiv 2508.05891 / search synthesis](https://arxiv.org/html/2508.05891v1)
- penaltyblog's `dixon_coles_weights()`: ξ = 0 gives equal weights; larger ξ (e.g., 0.03) weights recent results heavily. — [penaltyblog models overview](https://penaltyblog.readthedocs.io/en/latest/models/overview.html)
- penaltyblog includes `BayesianGoalsModel` (MCMC) and `HierarchicalBayesianGoalsModel`, which learns league-wide priors for team strengths, i.e., the Baio-Blangiardo-style approach. — [penaltyblog models overview](https://penaltyblog.readthedocs.io/en/latest/models/overview.html)

### Inferences
- In hockey, regression to the mean is stronger than in football: talent spread is narrow, and 28 of 32 teams were at .500+ points% at one point in 2025-26 ([ESPN](https://africa.espn.com/nhl/story/_/id/47210375/nhl-2025-26-games-shootout-theories-trends-stats-standings)). So strong shrinkage (ridge α tuned by CV) and preseason priors carried over from last season with partial regression are likely more valuable than the exact ξ.
- The ridge-penalized Poisson GLM is the frequentist analogue of a hierarchical normal prior on att/def, so no MCMC dependency is needed.
- Home advantage: a single global parameter in log λ. No current NHL value was sourced in this pass. Estimate it from the DB, expecting a small value (a few % on the goal rate).
- Hockey ξ should be tuned with a per-game or per-day scale. Seasons have ~82 games per team, so a half-life measured in games is more interpretable. Cross-season decay should also include a roster-change discontinuity.

### Gaps
- No hockey-specific published optimal ξ found.
- No published NHL comparison of hierarchical Bayes vs ridge GLM found.

## 5. xG as rate input, ratings blend, GLM with covariates

### Takeaway
Shot-quality inputs (xG) are a better predictor of future scoring than past goals, and better xG models reduce log-loss. The cleanest architecture is a Poisson GLM on goals with log-link covariates: team xG-based ratings, rest/back-to-back, starting goalie quality, and home. This yields λh, λa, which then feed the score grid and the OT/ENG layer.

### Cited Findings
- A skill-adjusted xG model for NHL shooters and goaltenders improved log-loss, Brier and AUC by up to ~5% vs baseline xG models. — [arXiv 2511.07703](https://arxiv.org/html/2511.07703v2)
- Buttrey et al. combined models for goal scoring, goal yielding and penalty commission into victory probabilities. Where these differed substantially from market probabilities, simulated bets had positive, statistically significant ROI. — [search synthesis of amstat 2015 proceedings / Buttrey et al.](https://ww2.amstat.org/meetings/proceedings/2015/data/assets/pdf/233899.pdf) (attribution of this claim to a specific paper is uncertain; snippet only)

### Inferences
- Formulation: log E[G_home] = β0 + β_home + β1·xGF_rate_home(decayed) + β2·xGA_rate_away(decayed) + β3·rest_diff + β4·goalie_GSAx_away + …, and symmetrically for away. Fit one GLM on stacked team-games (2 rows per game). This replaces per-team dummies with continuous ratings and generalizes better on small data.
- Using xG directly as the λ for a Poisson ignores finishing/goalie skill and ENG. Better to use xG as a covariate in a goals GLM so the coefficients calibrate to actual goals.

### Gaps
- No NHL study found directly comparing goals-based vs xG-based Poisson rates for game-level totals pricing.

## 6. Python implementation without heavy dependencies

### Takeaway
Everything needed exists in scipy/statsmodels/sklearn/LightGBM. penaltyblog bundles Poisson, Dixon-Coles, bivariate Poisson, NB, ZIP, Weibull-copula and Bayesian models plus a probability grid with totals and handicap outputs. But it is football-oriented (home/draw/away, no OT/SO "+1" rule), so it would be a new dependency for little gain.

### Cited Findings
- penaltyblog models: PoissonGoalsModel, DixonColesGoalsModel, BivariatePoisson, ZeroInflatedPoisson, NegativeBinomial, WeibullCopula, BayesianGoalsModel, HierarchicalBayesianGoalsModel. Output is a `FootballProbabilityGrid` with `home_draw_away`, `total_goals("over"/"under", strike)`, `asian_handicap`, and goal expectations. — [penaltyblog docs](https://penaltyblog.readthedocs.io/en/latest/models/overview.html)
- penaltyblog depends on NumPy, SciPy and Cython (compiled extensions); MIT licence. — [GitHub martineastwood/penaltyblog](https://github.com/martineastwood/penaltyblog)

### Inferences (standard library knowledge, not sourced in this pass)
- **statsmodels**: `smf.glm("goals ~ home + C(att_team) + C(def_team) + covariates", family=sm.families.Poisson(), freq_weights=w)` for weighted Maher/Dixon-Coles. `.fit_regularized(alpha=…, L1_wt=0)` gives ridge. `sm.families.NegativeBinomial(alpha)` or `smf.negativebinomial` is available if overdispersion is confirmed. A Dixon-Coles/tie-inflation τ needs a custom likelihood: `scipy.optimize.minimize` on a ~70-parameter vector is fast.
- **sklearn `PoissonRegressor(alpha=…)`**: L2-penalized Poisson GLM with `sample_weight` for time decay. It is the simplest ridge Maher model when used with one-hot team columns.
- **LightGBM `objective="poisson"`**: predicts λ with nonlinear covariates. The output plugs into the same grid. It is weaker on small data and needs monotone/regularization care.
- **Grid**: `scipy.stats.poisson.pmf(np.arange(0,15), λ)` outer product gives a 15×15 grid. Apply tie inflation, renormalize, then use the OT/SO and "+1 on tie" formulas from §2. `scipy.stats.skellam` is useful as a check.

### Gaps
- None material.

## 7. Calibration / log-loss: goal models vs direct classifiers

### Takeaway
NHL outcomes are close to coin flips. Good game-winner models reach log-loss around 0.65-0.68 and accuracy around 59-63%. No head-to-head published comparison of Poisson-grid totals pricing vs a direct over/under classifier was found.

### Cited Findings
- An NHL game model reported log-loss 0.6660 (10-fold CV, 2010-2020) and 0.6526 on the 2021 test season, with accuracy of 59.07% and 63.20%. — [GitHub JNoel71/NHL-Game-Prediction-Model](https://github.com/JNoel71/NHL-Game-Prediction-Model)
- Another model tested on 2019-20 had log-loss ≈ 0.678. — [Predicting NHL Game Outcomes (Crowhurst)](https://joshcrowhurst.quarto.pub/nhl-writeup/)
- Elo-based NHL rating predictive power was studied in an Aalto thesis (details not read). — [Aalto](https://aaltodoc.aalto.fi/items/d163b9aa-bcdc-4d64-90d0-6efee35a3cf5)

### Inferences
- Since the coin-flip log-loss is 0.693, the achievable gain is small (~0.02-0.04). A goal model's advantage is *coherence*: one λ pair prices moneyline, total and puck line consistently. A direct classifier for Over 5.5 can calibrate better on that one market but ignores the structural "+1 on tie" and ENG effects unless they are fed in as features.
- A sensible project benchmark: compare the goal-grid P(Over 5.5) vs a direct LightGBM binary on the same features, by log-loss and reliability curve on held-out seasons, against de-vigged closing odds.

### Gaps
- No published log-loss for NHL totals (Over/Under) from either approach was found.
- No published comparison of Poisson vs NB vs bivariate Poisson on NHL totals calibration was found.
