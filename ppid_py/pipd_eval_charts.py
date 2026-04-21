"""
Optional charts for PIPD eval reports (PNG). Requires matplotlib.

Used when building scenario 1 / composite Markdown → Word. If matplotlib is
missing, callers get an empty list (no failure).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from eval_scenario1 import compute_category_score

# Reference-spec palette
_COL_BAR_PASS = "#186A5A"   # dark teal — score >= 75%
_COL_BAR_FAIL = "#9C5200"   # amber    — score <  75%
_COL_MISS = "#E85D75"
_COL_EXTRA = "#2FA36B"
_GRID = "#DDE4EE"


def _apply_chart_style() -> None:
    import matplotlib as mpl

    mpl.rcParams.update(
        {
            "axes.facecolor": "white",
            "axes.edgecolor": "#CCCCCC",
            "axes.grid": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
            "axes.spines.bottom": False,
            "font.size": 9,
            "figure.facecolor": "white",
        }
    )


def try_write_eval_charts(
    scenario1: Dict[str, Any],
    study_id: str,
    output_dir: Path,
    *,
    prefix: str | None = None,
) -> List[Path]:
    """
    Write one or more PNGs under ``output_dir``. Returns absolute paths that exist.
    Filenames: ``{prefix or study_id}_m1_recall_by_category.png``, etc.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []

    _apply_chart_style()

    base = (prefix or study_id).strip() or "report"
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    per = scenario1.get("per_category") or {}
    cat_nums = list(range(1, 12))
    recalls: List[float] = []
    extras: List[int] = []
    misses: List[int] = []
    for cn in cat_nums:
        b = per.get(cn) or per.get(str(cn)) or {}
        recalls.append(100.0 * float(b.get("m1_recall") or 0.0))
        extras.append(len(b.get("hallucinated_subcats") or []))
        misses.append(len(b.get("missed_subcats") or []))

    # 1) Horizontal recall
    p1 = out_dir / f"{base}_m1_recall_by_category.png"
    fig, ax = plt.subplots(figsize=(8, 4.8))
    y_pos = [f"Cat {c}" for c in cat_nums]
    ax.barh(y_pos[::-1], recalls[::-1], color=_COL_PRIMARY, height=0.65, edgecolor="white", linewidth=0.5)
    ax.set_xlabel("M1 recall (%)")
    ax.set_xlim(0, 100)
    ax.set_title(f"{study_id} — M1 subcategory recall by category", color="#1a1a1a")
    ax.axvline(85.0, color=_COL_TARGET, linestyle="--", linewidth=1.2, label="85% target")
    ax.legend(loc="lower right", fontsize=8, framealpha=0.95)
    fig.tight_layout()
    fig.savefig(p1, dpi=140)
    plt.close(fig)
    written.append(p1.resolve())

    # 2) Misses vs extras per category
    p2 = out_dir / f"{base}_m1_misses_vs_extras.png"
    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = list(range(len(cat_nums)))
    w = 0.38
    ax.bar(
        [i - w / 2 for i in x],
        misses,
        w,
        label="Missed (in GT, not generated)",
        color=_COL_MISS,
        edgecolor="white",
        linewidth=0.5,
    )
    ax.bar(
        [i + w / 2 for i in x],
        extras,
        w,
        label="Extras (generated, not in GT)",
        color=_COL_EXTRA,
        edgecolor="white",
        linewidth=0.5,
    )
    ax.set_xticks(x, [str(c) for c in cat_nums])
    ax.set_xlabel("Category #")
    ax.set_ylabel("Line count")
    ax.set_title(f"{study_id} — GT misses vs generated extras (M1, per category)", color="#1a1a1a")
    ax.legend(loc="upper right", fontsize=8, framealpha=0.95)
    fig.tight_layout()
    fig.savefig(p2, dpi=140)
    plt.close(fig)
    written.append(p2.resolve())

    return written


def write_score_per_category_chart(
    scenario1: Dict[str, Any],
    study_id: str,
    output_dir: Path,
    *,
    prefix: str | None = None,
    cat_names: Dict[int, str] | None = None,
) -> Optional[Path]:
    """
    Horizontal bar chart: Score (%) per category, only showing categories that have
    ground-truth data or an empty-category verdict. Score label annotated at the bar end.
    Returns absolute path of the PNG, or None if matplotlib is unavailable.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    _apply_chart_style()

    base = (prefix or study_id).strip() or "report"
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    per = scenario1.get("per_category") or {}
    cat_labels: List[str] = []
    cat_scores: List[float] = []

    for cn in range(1, 12):
        b = per.get(cn) or per.get(str(cn)) or {}
        gt_tot = b.get("m1_gt_total")
        recall_raw = b.get("m1_recall")

        if gt_tot is not None:
            gt_tot = int(gt_tot)
        matched_raw = b.get("m1_matched")
        gen_tot = int(b.get("m1_generated_total") or 0)

        # Plot only categories present in GT for this protocol.
        if gt_tot is None or gt_tot <= 0:
            continue
        if recall_raw is not None:
            net = float(recall_raw) * gt_tot
            score = round(compute_category_score(net, gt_tot, gen_tot), 1)
        elif matched_raw is not None:
            net = float(int(matched_raw))
            score = round(compute_category_score(net, gt_tot, gen_tot), 1)
        else:
            score = 0.0

        cname = (cat_names or {}).get(cn, "")
        short = (cname[:22] + "…") if len(cname) > 22 else cname
        label = f"Cat {cn}. {short}" if short else f"Cat {cn}"
        cat_labels.append(label)
        cat_scores.append(score)

    if not cat_labels:
        return None

    visible = list(zip(cat_labels, cat_scores))

    labels_v = [v[0] for v in visible]
    scores_v = [v[1] for v in visible]

    bar_colors = [_COL_BAR_PASS if sc >= 75 else _COL_BAR_FAIL for sc in scores_v]

    fig_h = max(3.0, len(labels_v) * 0.55 + 1.2)
    fig, ax = plt.subplots(figsize=(8.5, fig_h))

    bars = ax.barh(
        labels_v[::-1], scores_v[::-1],
        color=bar_colors[::-1], height=0.58, edgecolor="none",
    )

    # Score labels at bar ends, coloured to match bar
    for bar, sc, col in zip(bars, scores_v[::-1], bar_colors[::-1]):
        ax.text(
            bar.get_width() + 0.8,
            bar.get_y() + bar.get_height() / 2.0,
            f"{sc:.1f}",
            ha="left", va="center", fontsize=9,
            color=col, fontweight="bold",
        )

    ax.set_xlabel("Score (%)", color="#444444", fontsize=9)
    ax.set_xlim(0, 118)
    ax.tick_params(axis="y", colors="#003087", labelsize=9)
    ax.tick_params(axis="x", colors="#666666", labelsize=8)
    ax.set_title("Category scores (weighted contribution)", color="#003087",
                 fontsize=11, fontweight="bold", pad=10)

    fig.tight_layout()
    p = out_dir / f"{base}_score_per_category.png"
    fig.savefig(p, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return p.resolve()
