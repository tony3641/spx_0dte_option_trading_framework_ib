"""Generate the figures for the simulator technical report — bilingual (EN + ZH).

Reproducible: run from the repo root
    python docs/figures/gen_sim_report_figs.py --lang en   # -> docs/figures/
    python docs/figures/gen_sim_report_figs.py --lang zh   # -> docs/figures/zh/

Figures are produced from the simulator's own model code (sim_calibrate /
sim_paths / sim_pricing) calibrated on the committed 1-minute SPX fixture, so
every curve is an *actual* model output — not a schematic — unless noted.
`--lang zh` switches the chart text to Chinese (Microsoft YaHei) and writes to a
docs/figures/zh/ sibling set referenced by the Chinese report.

Dataviz convention: single-hue sequential ramps for ordered magnitude, the
CVD-safe categorical slots (blue/orange/aqua) for the few multi-series
comparisons, near-white surface (#fcfcfb), recessive grid, ink in #0b0b0b.
"""
import math
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch
from scipy.stats import t as std_t, norm

# ---- language switch ------------------------------------------------------
LANG = "en"
_args = sys.argv[1:]
for i, a in enumerate(_args):
    if a.startswith("--lang"):
        LANG = a.split("=")[-1] if "=" in a else (_args[i + 1] if i + 1 < len(_args) else "en")
ZH = LANG == "zh"

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
_OUT = os.path.join(os.path.dirname(__file__), "zh" if ZH else "")
if ZH:
    os.makedirs(_OUT, exist_ok=True)

from sim_data import parse_csv  # noqa: E402
from sim_config import SimRunConfig  # noqa: E402
from sim_calibrate import calibrate, DEFAULT_SMILE, build_dynamics  # noqa: E402
from sim_pricing import smile_iv, bsm_put, bsm_put_delta, bar_year_frac  # noqa: E402
from sim_paths import simulate_chunk  # noqa: E402


_FRAGILE = str.maketrans({"₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5",
                          "₆": "6", "₇": "7", "₈": "8", "₉": "9", "ₜ": "t"})


def FX(s):
    """sub/superscript digits and the combining macron (v̄) are missing from the
    CJK font (Microsoft YaHei) and render as boxes — drop/ASCII-ify them for the zh set."""
    if not ZH:
        return s
    return s.translate(_FRAGILE).replace("̄", "")


def L(en, zh):
    """Return the language-picked label."""
    return FX(zh) if ZH else en


# ------ palette (dataviz reference, light mode) ------
INK = "#0b0b0b"; INK2 = "#52514e"; MUTED = "#898781"
GRID = "#e1e0d9"; SURF = "#fcfcfb"
BLUE = "#2a78d6"; ORANGE = "#eb6834"; AQUA = "#1baf7a"
YELLOW = "#eda100"; MAGENTA = "#e87ba4"; RED = "#e34948"; VIOLET = "#4a3aa7"
B_L = "#86b6ef"; B_M = "#3987e5"; B_D = "#1c5cab"; B_XD = "#104281"

_FONT = (["Segoe UI", "DejaVu Sans"] if not ZH
         else ["Microsoft YaHei", "SimHei", "DejaVu Sans"])
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": _FONT,
    "axes.unicode_minus": False,
    "figure.facecolor": SURF, "axes.facecolor": SURF,
    "axes.edgecolor": MUTED, "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.7, "grid.alpha": 0.9,
    "axes.spines.top": False, "axes.spines.right": False,
    "legend.frameon": False, "font.size": 10.5, "axes.titlesize": 12.5,
    "axes.titleweight": "bold", "savefig.dpi": 150,
    "figure.facecolor": SURF, "savefig.facecolor": SURF,
})

# load the fixture + fit the model once
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "SPX_1min_sample.csv")
CFG = SimRunConfig(strategy_name="T", source="csv", csv_path=FIXTURE, bar_size="1m")
BARS = parse_csv(FIXTURE, 60)
MODEL = calibrate(BARS, CFG)
STEPS = CFG.steps_per_day()        # 390


def _save(fig, name):
    """savefig with a small retry — Defender real-time scan briefly locks a freshly
    written PNG on Windows, so retry once."""
    import time
    path = os.path.join(_OUT, name)
    for attempt in range(6):
        try:
            fig.savefig(path, bbox_inches="tight", pad_inches=0.15)
            break
        except OSError:
            if attempt == 5:
                raise
            time.sleep(0.4)
    plt.close(fig)
    print("wrote", os.path.join("zh" if ZH else "", name))


# ---------------------------------------------------------------- Fig 1
def fig_pipeline():
    fig, ax = plt.subplots(figsize=(11.6, 4.9))
    ax.axis("off")

    def box(x, y, w, h, title, lines, tc=INK):
        ax.add_patch(plt.Rectangle((x, y), w, h, facecolor="#ffffff", edgecolor=MUTED,
                                   linewidth=1.2, zorder=2))
        ax.text(x + w / 2, y + h - 0.08, title, ha="center", va="top",
                fontsize=11, fontweight="bold", color=tc, zorder=3)
        for i, ln in enumerate(lines):
            ax.text(x + w / 2, y + h - 0.34 - 0.26 * i, ln, ha="center", va="top",
                    fontsize=8.5, color=INK2, zorder=3)

    def arrow(x0, y0, x1, y1):
        ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), mutation_scale=13,
                                     arrowstyle="-|>", color=INK2, linewidth=1.2,
                                     zorder=1))

    box(0.00, 1.05, 1.42, 1.55, L("Market data", "市场数据"),
        [L("intraday SPX bars", "盘中 SPX bar"),
         L("CSV / yfinance / IB", "CSV / yfinance / IB"),
         L("1m, 5m, 5s… RTH-filtered", "1m、5m、5s… RTH 过滤"),
         L("VIX closes (optional)", "VIX 收盘（可选）")])
    box(1.72, 1.05, 1.60, 1.55, L("Calibration (offline)", "标定（离线）"),
        ["GJR-GARCH(1,1)-t  — MLE",
         "σ² = ω + α·ε² + γ·ε²·1[ε<0] + β·σ²",
         L("U-shape u(minute-of-day)", "U 型 u(当日分钟)"),
         L("SVI smile IV(m)  (snapshot)", "SVI 微笑 IV(m)（快照）"),
         L("VIX map: VIX₀ · σ/σ₀", "VIX 映射：VIX₀ · σ/σ₀")])
    box(3.62, 1.05, 1.60, 1.55, L("Path generation (per chunk)", "路径生成（每块）"),
        [L("z ~ Standardized t(ν)", "z ~ 标准化 t(ν)"),
         L("σ_t = min(√σ²_t·u(t), cap)", "σ_t = min(√σ²_t·u(t), 上限)"),
         L("ε_t = σ_t·z_t ; S_t += ε_t", "ε_t = σ_t·z_t；S_t += ε_t"),
         L("GJR recursion → σ²_{t+1}", "GJR 递推 → σ²_{t+1}"),
         L("ATM-IV per-bar sigma cap", "ATM-IV 单 bar sigma 上限")])
    box(5.52, 1.05, 1.72, 1.55, L("Pricing & execution (per bar)", "定价与执行（每 bar）"),
        [L("SVI base + vol-level + skew tilt", "SVI 基础 + 波动率水平 + 偏度倾斜"),
         L("IV_t(m) = clip(…, 0.01, 5)", "IV_t(m) = clip(…, 0.01, 5)"),
         L("BSM put → bid/ask (½-spread)", "BSM put → bid/ask（½ 价差）"),
         L("Entry / stop / TP / expiry fills", "入场 / 止损 / 止盈 / 到期成交"),
         L("tick-rule combo credit", "tick 规则组合 credit")])
    box(7.54, 1.05, 1.50, 1.55, L("Risk & report", "风险与报告"),
        [L("day-PnL distribution", "当日 PnL 分布"),
         L("CVaR₁ / max-DD / ruin prob", "CVaR₁ / 最大回撤 / 爆仓概率"),
         L("SPX percentile fan", "SPX 百分位扇形"),
         L("spread MTM quantile fan", "价差 MTM 分位扇形")])
    arrow(1.45, 1.82, 1.70, 1.82)
    arrow(3.35, 1.82, 3.60, 1.82)
    arrow(5.25, 1.82, 5.50, 1.82)
    arrow(7.27, 1.82, 7.52, 1.82)

    ax.text(0.0, 0.60, L("Per-run dials —— ν, γ×, λ, ATM-IV %, vol-cap ×, skew β, t^γ, budget",
                         "每次运行的旋钮 —— ν、γ×、λ、ATM-IV %、vol-cap ×、skew β、t^γ、budget"),
            fontsize=9, color=INK2)
    ax.text(0.0, 0.36,
            L("Honesty rule: the sim never refits SVI at runtime (closed-form responses only) and "
              "never invents strategy semantics — every gate comes from config/strategies.json.",
              "诚实规则：模拟器运行期从不重拟合 SVI（只做闭式响应），也从不发明策略语义——"
              "每个门槛都来自 config/strategies.json。"),
            fontsize=8.5, color=INK2)
    ax.text(0.0, 0.12,
            L("0DTE put, expiry at 16:00 ET; time-to-expiry decays bar-by-bar via bar_year_frac = bar_seconds/(252·6.5h).",
              "0DTE put，16:00 ET 到期；到期时间逐 bar 衰减：bar_year_frac = bar_seconds/(252·6.5h)。"),
            fontsize=8.5, color=MUTED)
    ax.set_xlim(0, 9.05); ax.set_ylim(0, 2.9); ax.set_aspect("auto")
    ax.set_title(L("End-to-end simulator pipeline — the three internal models (GARCH, SVI, BSM) "
                   "and where the path-dependent smile/VIX hooks in",
                   "端到端模拟器管线 —— 三个内部模型（GARCH、SVI、BSM）与路径依赖的 smile/VIX 钩子所在"),
                 loc="left")
    fig.tight_layout()
    _save(fig, "fig01_pipeline.png")


# ---------------------------------------------------------------- Fig 2
def fig_gjr_news_impact():
    g = MODEL.garch
    f_mult = float(g.gamma)
    a_mult = 1.5
    eps = np.linspace(-0.004, 0.004, 600)
    up_f = g.alpha * eps ** 2
    dn_f = (g.alpha + f_mult) * eps ** 2
    dn_a = (g.alpha + f_mult * a_mult) * eps ** 2

    fig, ax = plt.subplots(figsize=(7.4, 4.9))
    ax.plot(eps, up_f, color=BLUE, lw=2, label=L("up-shock  α=0.0585", "上涨冲击  α=0.0585"))
    ax.plot(eps, dn_f, color=ORANGE, lw=2, ls="-",
            label=L(f"down-shock  α+γ = {g.alpha+g.gamma:.4f} (fitted γ)",
                    f"下跌冲击  α+γ = {g.alpha+g.gamma:.4f}（拟合 γ）"))
    ax.plot(eps, dn_a, color=RED, lw=2, ls="--",
            label=L(f"down-shock, γ ×1.5  α+1.5γ = {g.alpha+g.gamma*1.5:.4f} (stress)",
                    f"下跌冲击，γ ×1.5  α+1.5γ = {g.alpha+g.gamma*1.5:.4f}（压力）"))
    ax.axvline(0, color=INK2, lw=1)
    ax.set_xlabel(L("shock ε_t (bar return)", "冲击 ε_t（bar 收益）"))
    ax.set_ylabel(L("one-step variance increment  Δσ²_{t+1}", "单步方差增量  Δσ²_{t+1}"))
    ax.set_title(L("GJR leverage effect (news impact) — asymmetry in the variance response",
                   "GJR 杠杆效应（新闻冲击）——方差响应的非对称性"))
    ax.legend(loc="upper left", fontsize=9)
    ax.annotate(L("kink at ε=0: gains\n(α+γ) vs α on 1[ε<0]",
                  "在 ε=0 处折拐：\n下跌侧 (α+γ) vs 上涨侧 α"),
                xy=(0, 0), xytext=(-0.0036, max(dn_f) * 0.55), fontsize=8.5, color=INK2)
    fig.tight_layout()
    _save(fig, "fig02_gjr_news_impact.png")


# ---------------------------------------------------------------- Fig 3
def fig_student_t_vs_normal():
    x = np.linspace(-5, 5, 800)
    fig, ax = plt.subplots(figsize=(7.4, 4.9))
    for nu, c in [(4, BLUE), (7, ORANGE), (12, AQUA)]:
        scale = math.sqrt(nu / (nu - 2.0))
        ax.plot(x, std_t.pdf(x / scale, nu) / scale, color=c, lw=2,
                label=f"Student-t ν={nu}")
    ax.plot(x, norm.pdf(x), color=INK2, lw=2, ls="--", label="N(0,1)")
    ax.set_yscale("log"); ax.set_ylim(1e-4, 1.2)
    ax.set_xlabel(L("standardized innovation z", "标准化扰动 z"))
    ax.set_ylabel(L("density (log)", "密度（对数）"))
    ax.set_title(L("Heavy tails: standardized Student-t vs the normal (both unit variance)",
                   "厚尾：标准化 Student-t 对比正态（皆为单位方差）"))
    ax.legend(loc="lower left", fontsize=9)
    ax.annotate(L("tails decay like |z|^-(ν+1),\nnot exp(−z²/2)",
                  "尾部按 |z|^-(ν+1) 衰减\n而非 exp(−z²/2)"),
                xy=(3.2, 1e-2), xytext=(1.0, 1e-3), fontsize=9, color=INK2)
    fig.tight_layout()
    _save(fig, "fig03_student_t.png")


# ---------------------------------------------------------------- Fig 4
def fig_ushape():
    u = MODEL.ushape
    t = np.arange(STEPS)
    mod = (9 * 60 + 30) + t
    hm = [f"{((m)//60):02d}:{(m%60):02d}" for m in mod]
    fig, ax = plt.subplots(figsize=(9.2, 4.4))
    ax.plot(t, u, color=BLUE, lw=1.6)
    ax.plot(t, np.full_like(u, u.mean()), color=INK2, lw=1.0, ls="--",
            label=L("mean = 1.0 (multiplicative)", "均值 = 1.0（乘性）"))
    ax.fill_between(t, 0.25, 4.0, color=GRID, alpha=0.5, label=L("clip band [0.25, 4.0]", "截断带 [0.25, 4.0]"))
    ax.set_xlabel(L("bar of day (1m @ 09:30–16:00)", "当日 bar（1m @ 09:30–16:00）"))
    ax.set_ylabel(L("per-bar vol multiplier  u(t)", "单 bar 波动率乘子  u(t)"))
    ax.set_title(L("Intraday U-shape — nonparametric profile from the calibrated 1m fixture",
                   "日内 U 型——由标定 1m 数据得到的非参数剖面"))
    ax.legend(loc="upper left", fontsize=9)
    for mi in [0, 90, 180, 270, 360]:
        ax.axvline(mi, color=GRID, lw=0.6)
        ax.text(mi, 0.35, hm[mi], ha="center", va="top", fontsize=8, color=MUTED)
    fig.tight_layout()
    _save(fig, "fig04_ushape.png")


# ---------------------------------------------------------------- Fig 5
def fig_svi_vs_quadratic():
    m = np.linspace(-0.30, 0.30, 400)
    svi = DEFAULT_SMILE.iv(m)
    core = m[(m >= -0.06) & (m <= 0.06)]
    coef = np.polyfit(core, DEFAULT_SMILE.iv(core), 2)
    quad = np.polyval(coef, m)
    fig, ax = plt.subplots(figsize=(7.4, 4.9))
    ax.plot(m, svi, color=BLUE, lw=2.2, label="SVI  IV(m)=a+b(ρ·x+√(x²+σ²))")
    ax.plot(m, quad, color=ORANGE, lw=1.8, ls="--",
            label=L("legacy quadratic (extrapolated)", "旧二次型（外推）"))
    ax.axhline(1.0, color=MUTED, lw=0.8, ls=":")
    ax.axvspan(-0.15, 0.15, color=GRID, alpha=0.45,
               label=L("sim ladder ±15% (SVI_EDGE)", "模拟阶梯 ±15%（SVI_EDGE）"))
    ax.set_ylim(0, 1.4)
    ax.set_xlabel(L("log-moneyness  m = ln(K/S)", "对数 moneyness  m = ln(K/S)"))
    ax.set_ylabel(L("implied vol (decimal)", "隐含波动率（小数）"))
    ax.set_title(L("SVI keeps far wings bounded — the legacy quadratic exploded past 100% IV",
                   "SVI 让远翼保持有界——旧二次型冲过 100% IV"))
    ax.legend(loc="upper center", fontsize=9)
    ax.annotate(L("SVI wing\nbounded ~flat", "SVI 翼部\n有界 ~平坦"),
                xy=(0.29, svi[-1]), xytext=(0.10, 0.95), fontsize=8.5, color=BLUE,
                arrowprops=dict(arrowstyle="-|>", color=BLUE, lw=1))
    fig.tight_layout()
    _save(fig, "fig05_svi_vs_quadratic.png")


# ---------------------------------------------------------------- Fig 6
def fig_smile_tilt():
    m = np.linspace(-0.15, 0.15, 200)
    cfg = SimRunConfig(strategy_name="T", source="csv", csv_path=FIXTURE,
                       bar_size="1m", skew_beta=1.0, skew_t_gamma=0.4)
    dyn = build_dynamics(MODEL, cfg)
    t = int(0.9 * STEPS)
    s0 = MODEL.sigma0
    fig, ax = plt.subplots(figsize=(7.6, 4.9))
    for mult, c in [(0.5, B_L), (1.0, B_M), (1.8, B_D)]:
        iv = smile_iv(m, MODEL.smile, np.array([[s0 * mult]]), dyn, t)[0]
        lab = L(f"σ = {mult}·σ₀  ({mult*0.0628*100:.1f}% ann.)",
                f"σ = {mult}·σ0  ({mult*0.0628*100:.1f}% 年化)")
        ax.plot(m, iv * 100, color=c, lw=2, label=lab)
    ax.axvline(0.0, color=MUTED, lw=0.8, ls=":")
    ax.set_xlabel(L("log-moneyness  m", "对数 moneyness  m"))
    ax.set_ylabel(L("implied vol (%)", "隐含波动率（%）"))
    ax.set_title(L("Path-dependent skew tilt — smile responds to the path's own GARCH σ",
                   "路径依赖的偏度倾斜——微笑随路径自身 GARCH σ 响应"))
    ax.legend(loc="upper center", fontsize=9)
    ax.annotate(L("vol up → put wing richer,\ncall wing cheaper (tilt ∝ −m·Δσ)",
                  "波动率上升 → put 翼更贵\ncall 翼更便宜（tilt ∝ −m·Δσ）"),
                xy=(0.13, max(iv * 100) * 0.7), xytext=(-0.15, max(iv * 100) * 0.9),
                fontsize=8.5, color=INK2)
    fig.tight_layout()
    _save(fig, "fig06_smile_tilt.png")


# ---------------------------------------------------------------- Fig 7
def fig_budget_level():
    def level_at(cfg, sigma_path):
        dyn = build_dynamics(MODEL, cfg)
        return np.array([smile_iv(np.array([0.0]), MODEL.smile, np.array([[sp]]), dyn, t)[0]
                         for t, sp in enumerate(sigma_path)])

    v_bar = MODEL.garch.omega / (1 - (MODEL.garch.alpha + MODEL.garch.gamma / 2 + MODEL.garch.beta))
    s_quiet = np.full(STEPS, np.sqrt(v_bar))
    s_stress = np.full(STEPS, 1.5 * np.sqrt(v_bar))
    cfg_budget = SimRunConfig(strategy_name="T", source="csv", csv_path=FIXTURE,
                              bar_size="1m", atm_budget=True)
    cfg_legacy = SimRunConfig(strategy_name="T", source="csv", csv_path=FIXTURE, bar_size="1m")
    lvl_budget = level_at(cfg_budget, s_quiet)
    lvl_stress = level_at(cfg_budget, s_stress)
    lvl_legacy = level_at(cfg_legacy, s_quiet)
    iv0 = float(MODEL.smile.iv(0.0))
    t = np.arange(STEPS)
    fig, ax = plt.subplots(figsize=(8.6, 4.7))
    ax.plot(t, lvl_legacy * 100, color=INK2, lw=1.6, ls="--",
            label=L("legacy level  vol_beta·(σ−σ₀)  (flat)", "旧水平  vol_beta·(σ−σ₀)（平坦）"))
    ax.plot(t, lvl_budget * 100, color=BLUE, lw=2,
            label=L("variance-budget anchor, quiet σ=√v̄", "方差预算锚，平静态 σ=√v̄"))
    ax.plot(t, lvl_stress * 100, color=ORANGE, lw=2,
            label=L("variance-budget anchor, stress 1.5·√v̄", "方差预算锚，压力态 1.5·√v̄"))
    ax.axhline(iv0 * 100, color=MUTED, lw=0.9, ls=":",
               label=L(f"ATM anchor iv₀ = {iv0*100:.1f}%",
                       f"ATM 锚 iv0 = {iv0*100:.1f}%"))
    ax.set_xlabel(L("bar of day (1m)", "当日 bar（1m）"))
    ax.set_ylabel(L("ATM implied vol (%)", "ATM 隐含波动率（%）"))
    ax.set_title(L("Variance-budget ATM anchor — re-anchors IV to the path's remaining variance",
                   "方差预算 ATM 锚——把 IV 重新锚定到路径的剩余方差"))
    ax.legend(loc="lower left", fontsize=8.5)
    ax.text(0.52, 0.06,
            L("quiet path: early burn-off dip → re-firms into the close\n"
              "stress path: richer throughout, rises into expiry",
              "平静路径：早段烧掉下探 → 临近收盘抬回\n"
              "压力路径：全程更贵，向到期上行"),
            transform=ax.transAxes, va="bottom", fontsize=8.5, color=INK2)
    ax.set_xlim(0, 386)
    fig.tight_layout()
    _save(fig, "fig07_budget_level.png")


# ---------------------------------------------------------------- Fig 8
def fig_fan_cap():
    def fan(atm_iv, vol_cap, n=3000):
        cfg = SimRunConfig(strategy_name="T", source="csv", csv_path=FIXTURE,
                           bar_size="1m", n_paths=n, seed=42, atm_iv=atm_iv,
                           vol_cap_mult=vol_cap)
        paths = simulate_chunk(MODEL, cfg, float(BARS.closes[-1]), n,
                               np.random.SeedSequence(entropy=42))
        return np.quantile(paths.spots, [0.0, 0.05, 0.25, 0.5, 0.75, 0.95], axis=0)

    s0 = float(BARS.closes[-1])
    fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.6), sharey=True)
    for ax, (atm, cap, ttl, fillc) in zip(
            axes,
            [(None, 2.0, L("No ATM-IV cap — GARCH vol-ratchet inflates the tails",
                           "无 ATM-IV 上限——GARCH 棘轮吹大尾部"), B_L),
             (0.165, 2.0, L("ATM-IV cap — per-bar σ clamped to the market's implied vol",
                            "ATM-IV 上限——单 bar σ 被钳制到市场隐含波动率"), B_D)]):
        mat = fan(atm, cap)
        t = np.arange(STEPS)
        p0, p5, p25, p50, p75, p95 = mat
        ax.fill_between(t, p5, p95, color=fillc, alpha=0.16)
        ax.fill_between(t, p25, p75, color=fillc, alpha=0.28)
        ax.plot(t, p50, color=B_M, lw=1.8, label=L("median (p50)", "中位数（p50）"))
        ax.plot(t, p5, color=B_M, lw=1.0, alpha=0.7)
        ax.plot(t, p95, color=B_M, lw=1.0, alpha=0.7)
        ax.plot(t, p0, color=RED, lw=1.4, ls="--", label=L("p0 (worst path)", "p0（最差路径）"))
        ax.axhline(s0, color=MUTED, lw=0.8, ls=":")
        ax.set_xlabel(L("bar of day (1m)", "当日 bar（1m）"))
        ax.set_title(ttl, fontsize=10.5)
        if ax is axes[0]:
            ax.set_ylabel(L("SPX close", "SPX 收盘"))
        ax.legend(loc="lower left", fontsize=8)
        ax.text(0.98, 0.04, f"p0 = {(p0[-1]/s0-1)*100:+.1f}%",
                transform=ax.transAxes, ha="right", fontsize=9, color=RED)
    fig.suptitle(L("The ATM-IV fan anchor: capping per-bar sigma, not rescaling the level — keeping "
                   "the fan consistent with the options market's implied distribution",
                   "ATM-IV 扇形锚：给单 bar sigma 设上限而非缩放水平——使扇形与期权市场隐含分布一致"),
                 fontsize=11.5, y=1.02)
    fig.tight_layout()
    _save(fig, "fig08_fan_cap.png")


# ---------------------------------------------------------------- Fig 9
def fig_vix_map():
    vix0 = 15.0
    v_bar = MODEL.garch.omega / (1 - (MODEL.garch.alpha + MODEL.garch.gamma / 2 + MODEL.garch.beta))
    u = MODEL.ushape
    t = np.arange(STEPS)
    fig, ax = plt.subplots(figsize=(8.4, 4.5))
    for mult, c, lab in [(1.0, B_M, L("expected vol state  σ=√v̄·u(t)", "期望波动率状态  σ=√v̄·u(t)")),
                         (1.5, B_D, L("vol-state shock  1.5·√v̄·u(t)", "波动率状态冲击  1.5·√v̄·u(t)"))]:
        sig = np.sqrt(v_bar) * mult * u
        vix = np.clip(vix0 * sig / MODEL.sigma0, 5.0, 100.0)
        ax.plot(t, vix, color=c, lw=2, label=lab)
    ax.axhline(vix0, color=INK2, lw=1.0, ls="--",
               label=L(f"VIX₀ = {vix0:.1f} (anchor)", f"VIX0 = {vix0:.1f}（锚）"))
    ax.axhline(5.0, color=RED, lw=1.0, ls=":", label=L("clip [5, 100]", "截断 [5, 100]"))
    ax.set_ylim(0, 40)
    ax.set_xlabel(L("bar of day (1m)", "当日 bar（1m）"))
    ax.set_ylabel(L("mapped VIX", "映射 VIX"))
    ax.set_title(L("VIX mapping — VIX_t = clip(VIX₀ · σ_t / σ₀, 5, 100); σ_t = √v̄·u(t)",
                   "VIX 映射——VIX_t = clip(VIX₀ · σ_t / σ₀, 5, 100)；σ_t = √v̄·u(t)"))
    ax.legend(loc="upper left", fontsize=8.5)
    ax.text(0.98, 0.05,
            L("day-average ≈ VIX₀;\nU-shape modulation (±)\nand vol-state shocks\nshift it around the anchor",
              "日内平均 ≈ VIX₀；\nU 型调制（±）\n与波动率状态冲击\n使其绕锚点漂动"),
            transform=ax.transAxes, ha="right", va="bottom", fontsize=8.5, color=INK2)
    fig.tight_layout()
    _save(fig, "fig09_vix_map.png")


# ---------------------------------------------------------------- Fig 10
def fig_bsm():
    s0 = float(BARS.closes[-1])
    ladder = np.arange(int(s0 * 0.9), int(s0 * 1.1), 5.0)
    sigma = 0.20
    T = bar_year_frac(60) * 60.0
    put = bsm_put(s0, ladder, T, 0.043, sigma)
    delta = bsm_put_delta(s0, ladder, T, 0.043, sigma)
    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.2))
    axes[0].plot(ladder, put, color=BLUE, lw=2)
    axes[0].axvline(s0, color=MUTED, lw=0.8, ls=":")
    axes[0].set_xlabel(L("strike K", "行权价 K")); axes[0].set_ylabel(L("put mid value", "put 中间值"))
    axes[0].set_title(L("BSM put price", "BSM put 价格"), fontsize=10.5)
    axes[1].plot(ladder, delta, color=ORANGE, lw=2)
    axes[1].axhline(0, color=MUTED, lw=0.8); axes[1].axhline(-1, color=MUTED, lw=0.8, ls=":")
    axes[1].axvline(s0, color=MUTED, lw=0.8, ls=":")
    axes[1].set_xlabel(L("strike K", "行权价 K")); axes[1].set_ylabel(L("put delta", "put delta"))
    axes[1].set_title(L("BSM put delta (drives the entry delta band)", "BSM put delta（驱动入场 delta 带）"),
                      fontsize=10.5)
    fig.suptitle(L("Black–Scholes puts on the strike ladder — the pricing primitive marking every bar",
                   "行权阶梯上的 Black–Scholes put——每根 bar 定价的原始语汇"), y=1.03, fontsize=11.5)
    fig.tight_layout()
    _save(fig, "fig10_bsm.png")


def main():
    print(f"lang={LANG} calibrated model: {MODEL.garch}")
    print(f"p_eff = {MODEL.garch.alpha + MODEL.garch.gamma/2 + MODEL.garch.beta:.4f}")
    print(f"sigma0/day = {MODEL.sigma0*np.sqrt(STEPS):.4f}  ann = {MODEL.sigma_annual(CFG):.4f}")
    print(f"ATM IV = {MODEL.smile.iv(0.0):.4f}, vix0 fallback = {MODEL.vix0}")
    fig_pipeline()
    fig_gjr_news_impact()
    fig_student_t_vs_normal()
    fig_ushape()
    fig_svi_vs_quadratic()
    fig_smile_tilt()
    fig_budget_level()
    fig_fan_cap()
    fig_vix_map()
    fig_bsm()


if __name__ == "__main__":
    main()
