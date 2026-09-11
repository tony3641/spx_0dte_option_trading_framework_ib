# SPX 0DTE Intraday Monte Carlo Simulator: Technical Report

> What the simulator does, why each model was chosen, and how the smile and VIX
> respond *path-by-path* to the simulated market. Every figure below comes from
> the simulator's own model code, calibrated on the committed 1-minute SPX
> fixture (`tests/fixtures/SPX_1min_10d.csv`). The numbers in the captions are
> real outputs, not schematics.

---

## 1. What the simulator does

A daily Monte Carlo tells you nothing useful about a 0DTE bull put spread. Theta
and gamma race each other, volatility follows a U-shape through the session, and
losses cluster in crashes. What happens during the day decides the trade, not the
closing price. The simulator therefore generates N *intraday* SPX paths from a
calibrated volatility model, marks a spread to market every bar under a vol-linked
implied-vol smile, runs the *same* strategy definitions the live engine uses (entry
gates, tick-rule fills, stop / take-profit / expiry), and reports the day-PnL
distribution plus tail risk.

<figure>
  <img src="figures/fig01_pipeline.png" alt="End-to-end simulator pipeline">
  <figcaption><b>Figure 1.</b> The pipeline. Three internal models (GJR-GARCH for
  returns, SVI for the smile, BSM for option value) are separate and only meet at
  the per-bar pricing call. Two hooks make the world *path-dependent*: the ATM-IV
  sigma cap in path generation, and the smile/VIX response in pricing.</figcaption>
</figure>

The simulator is a test harness, and it tries to be faithful to the live engine
without overselling what it shows.

It does not invent strategy semantics. Every entry condition and exit rule is read
from `config/strategies.json` and evaluated with the same defaults as the live
`strategy_engine`. A gate the sim cannot faithfully evaluate gets rejected loudly
(e.g. `trend` / `atm_iv`) rather than silently skipped.

Dynamics are closed-form. The SVI is fitted once, offline, and never refit at
runtime, so the path-dependent response is a closed-form level/tilt on top of the
captured snapshot.

The engine only reads `Strategy` objects. It never places an order or touches live
state.

---

## 2. Model 1: GJR-GARCH(1,1) with Student-t innovations

Returns follow GJR-GARCH(1,1) (Glosten–Jagannathan–Runkle, the asymmetric
extension of GARCH), with Student-t innovations:

```
σ²_t = ω + α·ε²_{t−1} + γ·ε²_{t−1}·1[ε_{t−1} < 0] + β·σ²_{t−1}     (volatility)
ε_t  = σ_t · u(minute-of-day) · z_t ,   z ~ standardized t(ν)       (returns)
```

### 2.1 Fitting (MLE with variance targeting)

The five parameters `(ω, α, γ, β, ν)` come from maximum likelihood via
`scipy.optimize.minimize` (Nelder–Mead, multi-start over `ν ∈ {ν₀, 4, 10}`, two
polish passes). The objective enforces two structural constraints:

- Stationarity: `α + γ/2 + β < 1`, or the variance path explodes.
- Variance targeting: `ω = σ̄²·(1 − α − γ/2 − β)`, so the model's unconditional
  variance equals the sample variance of the de-meaned log-returns.

Two guards cover the degenerate cases. If MLE fails to converge, the fit falls back
to a preset `(α=0.04, γ=0.10, β=0.85, ν=6.0)` and flags it in the UI. A flat or
zero-variance return series short-circuits straight to the variance-targeted preset,
because the Student-t likelihood is degenerate there and Nelder–Mead can "converge"
to a nonsense interior point instead of failing.

### 2.2 The leverage effect (asymmetry)

With `γ > 0`, a *negative* return raises next-bar variance by `(α+γ)·ε²` while a
positive one raises it by `α·ε²`. A down-move of a given size pushes volatility up
more than an up-move of the same size. This is the news-impact asymmetry.

<figure>
  <img src="figures/fig02_gjr_news_impact.png" alt="GJR news impact asymmetry">
  <figcaption><b>Figure 2.</b> One-step variance increment vs the shock ε, held at
  the unconditional state. The response is kinked at ε=0: downside gets `α+γ`,
  upside gets `α`. The stress dial `gamma_mult` scales γ (dashed) to amplify crash
  clustering. (On this calm fixture the fitted γ is small, so the kink is gentle.)</figcaption>
</figure>

### 2.3 Student-t innovations

Empirical equity returns have fatter tails than a Gaussian. The innovations are
Student-t with `ν` degrees of freedom, then variance-standardized by dividing by
`√(ν/(ν−2))` so that `E[z²] = 1` and the return scale stays interpretable. The
model requires `ν > 2`, otherwise the variance is infinite.

<figure>
  <img src="figures/fig03_student_t.png" alt="Student-t vs normal density">
  <figcaption><b>Figure 3.</b> Standardized Student-t (unit variance) vs N(0,1) on a
  log axis. Tails decay like `|z|^-(ν+1)` instead of `e^{−z²/2}`, so large shocks
  are far more likely, which is what generates the crash clusters and the low-ν
  stress dial. Lower ν → fatter tails (the `nu_override` stress dial).</figcaption>
</figure>

### 2.4 Intraday U-shape volatility

SPX intraday volatility is not flat. It is high at the open, decays through the
morning, troughs around midday, and ramps into the close. A nonparametric
multiplicative profile `u(minute-of-day)` captures this, estimated from the
normalized mean `|ε|` per minute-of-day bucket and then smoothed. No functional
form is assumed.

<figure>
  <img src="figures/fig04_ushape.png" alt="Intraday U-shape volatility profile">
  <figcaption><b>Figure 4.</b> The calibrated U-shape from the 1m fixture. Mean 1.0
  (so it scales the fitted vol up/down), clipped to `[0.25, 4.0]`. Applied as
  `σ_t = √σ²_t · u(t)`. The open spike and the close ramp are why a 0DTE spread
  needs an *intraday* sim rather than a daily one.</figcaption>
</figure>

### 2.5 The near-integrated ratchet and the ATM-IV cap

This is the most important modeling detail in the report, and it has nothing to do
with Student-t. A GARCH fit on calm data can be near-integrated: `α + γ/2 + β ≈
0.99` (this fixture gives 0.9895). The conditional variance then has a heavy-tailed
stationary distribution, so a small subset of simulated paths ratchet the vol state
up to 5-10x the fitted `σ₀`. The fan's center (p25-p75) is already consistent with
the market. Only the tails are broken: a 1-day p0 of −25% or worse, running all the
way into the ±50% spot guard band. Two attempted fixes failed. Normal innovations
instead of t reproduce the problem, and rescaling the level of vol blows up to NaN
through the same ratchet.

What works is capping the per-bar sigma rather than shifting the level:

```
σ_t = min( √σ²_t · u(t),  vol_cap_mult · (atm_iv / √252) / √steps )
```

Set `atm_iv` to the market's current ATM IV (annual decimal) and leave
`vol_cap_mult` at its default 2.0, and each per-bar move is bounded by the
IV-implied vol. The median session still looks like the data, while the tails match
what the options market prices.

<figure>
  <img src="figures/fig08_fan_cap.png" alt="SPX path fan with and without the ATM-IV cap">
  <figcaption><b>Figure 8.</b> The SPX percentile fan (p0…p95). Left: no cap, and the
  unwrapped GARCH ratchet drives the worst path into the −50% guard band (p5 already
  −5.2%). Right: with `atm_iv = 16.5%` the fan is consistent with the market (p0
  −4.4%, p5 −1.8%, p95 +1.8%). Median (±1.3%) is unchanged; only the pathological
  tails are trimmed. p0 is a single worst path.</figcaption>
</figure>

> The ATM-IV anchor reconciles two things that otherwise sit far apart: a calm
> *realized* vol (this fixture: 6.3% annual, about 0.4%/day) and the *implied* vol
> the options market actually trades (here about 16.5%, consistent with a ~15 VIX).
> Without the anchor, the fan's tails are an artifact of a near-integrated fit
> rather than a statement about the market.

---

## 3. Model 2: the SVI smile

Options are not priced at a single vol. IV varies with moneyness: higher on the put
side (negative skew), a shallow dip around ATM, a slight firm-up on far calls. The
sim uses an SVI (Stochastic Volatility Inspired) parameterization in log-moneyness
`m = ln(K/S)`:

```
IV(m) = a + b·( ρ·(m − m₀) + √((m − m₀)² + σ²) )
```

`a` is the level, `b` the wing steepness, `ρ` the skew (negative → put skew), `m₀`
the vertex, `σ` the curvature width.

### 3.1 Why SVI replaced the legacy quadratic

The older quadratic fit `IV(m) = c₀ + c₁·m + c₂·m²` works within the observed ±5-6%
moneyness band. But the sim prices puts out to ±15% (`ladder_range_pct`), so the
smile gets *extrapolated*. A parabola shoots past ~100% IV in the wings and produces
credits that fail no-arbitrage. SVI's square root makes the wings grow roughly
linearly, which keeps them bounded.

<figure>
  <img src="figures/fig05_svi_vs_quadratic.png" alt="SVI vs legacy quadratic IV">
  <figcaption><b>Figure 5.</b> SVI (blue) vs the legacy quadratic (dashed) fitted
  to the same +/-6% put band and extrapolated. Beyond the sim ladder (±15%, shaded)
  the quadratic shoots well past 100% IV; SVI stays bounded and monotone. The shaded
  ±15% band is where the sim actually prices.</figcaption>
</figure>

### 3.2 Fit and guards

`(a, b, ρ, m₀, σ)` are fitted to the live chain's put IVs from several initial seeds
(a deterministic quadratic seeding plus wing-slope estimates). The guards are written
into the optimization as constraints (SLSQP). On a real 0DTE chain this matters: the
skew is so steep that the unconstrained optimum sits on the `b` bound with σ → 0 and
its wings blow past the cap. Every seed was thrown away and the capture failed, even
though a perfectly good bounded fit existed. Now the guards assert the result
afterward; a fit that still cannot satisfy them falls back to the default snapshot,
and the capture reports which guard rejected it.

| Guard | Purpose |
|---|---|
| `SVI_WING_CAP = 1.0`, floor `0.005` | No >100% IV, no non-finite values on `[−15%, +15%]` |
| Monotone put wing on `[−15%, 0]` | No butterfly / vertical-arbitrage flip |
| No negative IV outside `±30%` | Extrapolation sanity |
| Put skew ≥ `SVI_MIN_PUT_SKEW` (1 vol pt) | Reject a flat, information-free smile |
| RMSE within `1.4826 × MAD` of the chain | Reject a fit that ignores part of the chain. The spread is robust, so one wild quote cannot inflate the baseline enough to admit a bad fit |

IV(m) is convex in m, so the envelope collapses to a handful of scalar inequalities:
the two endpoint caps, the band minimum at the clamped vertex, and the wing-slope sign
at m = 0. The solver can satisfy these directly.

Most of a live 0DTE chain is vega-less quotes. Far-OTM puts have no bid, with the ask
pinned on the 0.05 tick. Some strikes have their mid stuck on the tick floor, and a
constant price across a dozen strikes produces a fake IV hump that no convex SVI can
match. Deep-ITM puts have a mid that is nearly pure intrinsic. `smile_capture_points`
drops all of these before the fit sees them (it requires a bid of at least two ticks,
`0.10`, plus `0.005 ≤ |Δ| ≤ 0.75`), and maps moneyness with the chain snapshot's own
spot so `m` and IV describe the same instant.

The snapshot comes from a live chain capture (`config/sim_smile.json`) when the IB
quote cache is available, otherwise a bundled default (`config/sim_smile_default.json`,
ATM IV ~20%), otherwise built-in constants. The calibration panel shows whether the
current snapshot is captured or defaulted.

---

## 4. Model 3: BSM pricing and the spread

0DTE SPXW settles at the 16:00 ET close. Every contract is a 0DTE put, and
time-to-expiry decays bar by bar, `T_t = (steps−1−t) · bar_seconds/(252·6.5h)`.
Value comes from Black–Scholes, the same math the live `gex_calculator` uses:

```
d1 = [ln(S/K) + (r + ½σ²)·T] / (σ√T),  d2 = d1 − σ√T
put = K·e^{−rT}·Φ(−d2) − S·Φ(−d1)
```

<figure>
  <img src="figures/fig10_bsm.png" alt="BSM put value and delta">
  <figcaption><b>Figure 10.</b> BSM put price and delta on the strike ladder.
  Delta drives the entry `short_delta` band; the price curve drives credit. The
  ladder is a fixed 5-pt grid over ±15% of the entry spot.</figcaption>
</figure>

The *mid* is `put(S,K,T,σ_iv)`. Entry fills follow a deliberately conservative rule:
never better than the natural price, and floored to a tick,
`fill = min(floor_to_tick(mid), S_bid − L_ask)`, with a minimum of one tick. Synthetic
bid/ask is mid ± ½ · `half_spread(m)`, where the half-spread widens with moneyness
(`0.05·(1 + 8|m|)`).

---

## 5. Path-dependent smile dynamics

This is the part that makes the world respond to the path. The smile is not fixed;
it moves with each simulated path's own volatility state. All of it lives behind one
value object (`SmileDynamics`), evaluated per bar `t`:

```
IV_t(m) = clip( base(m) + level_t + tilt_t ,  0.01, 5.0 )
```

- `base(m)`: the captured SVI snapshot.
- `level_t`: scales the whole curve with the path's vol state.
- `tilt_t`: skews the curve with the path's vol state, tilting it put-rich on a vol shock.

The three channels are orthogonal and can be dialed independently. Each defaults to a
neutral value that reproduces the legacy formula bit-for-bit, validated by a pinned
regression chain.

### 5.1 Vol-level link (legacy λ)

The first channel: `level = vol_beta · (σ_path − σ₀)`, a linear shift of the whole
smile with the path's per-bar sigma. `vol_beta` defaults to 0.75; `0` gives a static
smile. The nudge is subtle, since the level mostly comes from the snapshot.

### 5.2 Skew tilt (σ-driven, with expiry amplification)

With `skew_beta > 0`, the IV curve tilts whenever the path's GARCH sigma deviates
from its calibrated mean: put wings get richer, call wings cheaper, and ATM stays
put. `skew_t_gamma` (0..1, with ~0.4 the usual literature value) amplifies the tilt
toward expiry via `(T₀/T)^γ`:

```
tilt_t = −skew_beta · (t_scale_t)^{skew_t_gamma} · clamp(σ/σ₀ − 1, −1, +3) · m
t_scale_t = T₀ / max(T_t, ½·bar)
```

The vol ratio is clamped to `[−1, +3]`, which keeps an unwrapped tail from throwing
the wing into absurdity. All of it is closed-form; the SVI is never refit at runtime.

<figure>
  <img src="figures/fig06_smile_tilt.png" alt="Path-dependent skew tilt">
  <figcaption><b>Figure 6.</b> The tilt at a late bar (t_scale amplified): higher
  path vol (dark) makes the put wing richer and the call wing cheaper; ATM is
  pinned at the anchor (~20%). The annualized labels use the fixture's realized vol
  (6.3%); the *skew shape*, not the level, is what the tilt moves.</figcaption>
</figure>

### 5.3 Variance-budget ATM anchor

With `atm_budget = true`, a closed-form GJR conditional expectation replaces the flat
level. Each bar's ATM IV re-anchors to the model's *annualized remaining expected
variance* given the path's sigma state, weighted by the intraday U-shape and
normalized so that the first bar matches the captured snapshot exactly:

```
p_eff = α + γ·γ_mult/2 + β                    (persistence; /2 from E[1[ε<0]·ε²] = ε²/2)
v̄    = ω / (1 − p_eff)                         (unconditional variance)
u2_k  = u(k)² · bar_frac
S(t) = Σ_{k>t} u2_k ,  P(t) = Σ_{k>t} p_eff^{k−t} · u2_k
A(t) = v̄·(S(t) − P(t)) ,  B(t) = P(t)
σ²_t = v̄ + budget_beta·(σ²_path − v̄)
v_t  = A(t) + B(t)·σ²_t
level = iv₀·(√(v_t/v₀ · t_scale_t) − 1)   ,  v₀ = v̄·S(0)
```

Quiet paths then show the model's *intraday IV profile*: early burn-off and a
progressive firm-up into the close, with the trough's depth and timing tracking the
close-bucket weight. The legacy formula gives a flat level.

<figure>
  <img src="figures/fig07_budget_level.png" alt="Variance-budget ATM anchor level">
  <figcaption><b>Figure 7.</b> ATM IV across the day. The legacy level (grey dashed)
  is flat. The budget anchor (blue, quiet state σ=√v̄) burns off to ~15% midday then
  firms back to the anchor into the close; a stressed state (orange, 1.5·√v̄) is
  richer throughout and *rises* into expiry. `budget_beta` is the A/B dial for how
  strongly the anchor tracks the GARCH state.</figcaption>
</figure>

> The anchor is a *theory* value. Remaining variance scales with the persistent GARCH
> state, which makes it materially stronger than the linear `vol_beta` link. The model
> therefore expects quiet-day *late* IV to sit higher than reality, because the
> variance risk premium burns off; that gap is an accepted residual and is not modeled.

### 5.4 VIX mapping

There is no simulated VIX process. The `volatility` entry condition tests a proxy
instead, `VIX_t = clip(VIX₀ · σ_path,t / σ₀, 5, 100)`, where σ₀ is the calibrated mean
per-bar vol and VIX₀ its matching VIX mean (the last 20 closes when available,
otherwise 20.0). The `volatility` condition reads this proxy and never a real VIX
index.

<figure>
  <img src="figures/fig09_vix_map.png" alt="VIX mapping along the expected vol state">
  <figcaption><b>Figure 9.</b> The VIX proxy along the *expected* vol state
  `σ_t = √v̄·u(t)` anchored at VIX₀=15 (illustration; the fixture has no VIX series).
  Day-average ≈ VIX₀; the U-shape modulates it ±, a vol-state shock shifts it up.
  The clip `[5,100]` is the guard. A real GARCH path's vol state can push the proxy
  far higher than this, the near-integrated story of Fig 8 again.</figcaption>
</figure>

### 5.5 Validation methodology

The dynamics are closed-form and gated, so validation uses a pinned bit-identical
chain. With neutral dials (`skew_beta = skew_t_gamma = 0`, `atm_budget = false`),
every output must equal the committed baselines via `np.array_equal` rather than
`allclose`. The chains run `legacy → A → AB → ABC`. Each gate test proves that
turning a channel off reproduces the previous stage exactly, and that the previous
stage's output matches its `.npz` byte for byte. Adding a dial therefore cannot
silently change behavior an old run relied on.

---

## 6. Risk analytics and experiment surface

The per-cell payload (`sim_risk.py`) computes:

- Day-PnL distribution: mean, median, σ, win rate, CVaR₅ / CVaR₁, worst day.
- Exit reasons: expired / stopped / take-profit / never-entered.
- Intraday max drawdown, from each trial's per-minute MTM series.
- Bootstrap equity curves: sample day PnLs into sequences of `bootstrap_len` days,
  giving a max-DD distribution and a ruin probability `P(max DD ≥ threshold)` as a
  fraction of account equity (default 20%).

There are three experiment surfaces: stop-loss multiplier sweeps; fixed versus
dynamic short-strike distance (`dynamic_k`); and a set of stress dials (ν, γ×,
λ / σ, skew β, t^γ, budget) for A/B against a baseline. The report also emits the
SPX percentile fan (Fig 8), which is a property of the market sim and identical
across sweep rows, plus a spread MTM quantile fan.

---

## 7. Limitations

- Implied and realized vol are not tied together. Options are priced at the smile
  snapshot's IV (ATM ~20% by default, ~16.5% when dialed), while the underlying moves
  at the *data's* realized vol (6.3% here). A calm CSV plus a high smile means far-OTM
  short puts around 2-2.5% OTM are rarely reached, so win rates come out near 100%.
  That reflects the mismatch between the two vol worlds rather than any edge. To
  mitigate: capture a live smile, use realistic data, read sweep rows *relatively*,
  and set the ATM-IV anchor.
- Stops are checked on bar closes. An intra-bar spike-and-revert through the trigger
  is invisible at 1m. Finer bars (CSV 5s) shrink that blind spot.
- One entry per strategy per day. There is no re-entry beyond the family child
  mechanism, and family mode does not support SL/k sweeps.
- trend, pmove, RSI and atm_iv gates are not simulated. A strategy that enables one
  is rejected rather than mis-simulated.
- bear_call is not simulated. Non-`bull_put` strategies are refused.
- `state.vix` is a proxy, not a VIX process. The `volatility` condition tests it.

---

## 8. Symbols and constants quick-reference

| Symbol | Meaning | Value here (1m fixture) |
|---|---|---|
| `α, γ, β` | GJR coefficient / leverage / persistence | 0.0585 / 0.00946 / 0.92629 |
| `ν` | Student-t dof | 7.38 (fit) |
| `Σ = α+γ/2+β` | persistence (near-integrated if ~0.99) | 0.9895 |
| `ω` | GARCH intercept (variance-targeted) | 4.76e-10 |
| `σ₀` | mean per-bar vol (calibration) | 2.00e-4 → 6.3% ann |
| `√v̄` | unconditional vol | 2.13e-4 |
| `iv₀` | ATM IV from snapshot | 20.1% |
| `bar_time` | 1m year fraction `60/(252·6.5h)` | 2.93e-5 yr |
| `r` | risk-free rate | 4.3% |
| `vol_cap_mult` | per-bar cap multiple of IV-implied | 2.0 |
| `atm_budget, budget_beta` | variance-budget anchor | off / 1.0 (theory) |
| `vol_beta` | legacy vol-level link (λ) | 0.75 |
| `skew_beta, skew_t_gamma` | tilt strength / expiry exponent | 0 / 0 (→ 0.4 lit) |
| `VIX₀` | VIX anchor (fallback / illustration) | 20.0 / 15 |
| RTH day | bars at 1m / 5m | 390 / 78 |

*Figures generated by `docs/figures/gen_sim_report_figs.py`; rerun it from the repo
root to regenerate. Reproduce the calibration with:*

```python
from sim_data import parse_csv
from sim_config import SimRunConfig
from sim_calibrate import calibrate
cfg  = SimRunConfig(strategy_name="T", source="csv",
                    csv_path="tests/fixtures/SPX_1min_10d.csv", bar_size="1m")
model = calibrate(parse_csv(cfg.csv_path, 60), cfg)
```
