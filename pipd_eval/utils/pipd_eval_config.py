"""
Load ``eval_config/pipd_eval_config.yaml`` for eval report display.

Weights drive the category scorecard (§2). Thresholds drive document pass/target
and optional Summary Metrics targets when YAML is present.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_PKG_DIR = Path(__file__).resolve().parent
_DEFAULT_YAML = _PKG_DIR / "eval_config" / "pipd_eval_config.yaml"


def default_eval_config_path() -> Path:
    return _DEFAULT_YAML


def resolve_eval_config_path(explicit: Optional[str] = None) -> Path:
    env = (explicit or os.environ.get("PIPD_EVAL_CONFIG_PATH") or "").strip()
    if env:
        p = Path(env)
        if not p.is_absolute():
            p = _PKG_DIR / p
        return p
    return _DEFAULT_YAML


def load_pipd_eval_config(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    p = path or resolve_eval_config_path()
    if not p.is_file():
        return None
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def category_weights_and_names(
    cfg: Optional[Dict[str, Any]],
) -> Tuple[Dict[int, float], Dict[int, str]]:
    """
    Returns normalized weights (sum 1.0) and display names per category_num 1..11.
    Falls back to equal weights if cfg missing or invalid.
    """
    equal = 1.0 / 11.0
    default_w = {i: equal for i in range(1, 12)}
    default_n: Dict[int, str] = {}
    if not cfg:
        return default_w, default_n

    cats = cfg.get("categories") or {}
    raw_w: Dict[int, float] = {}
    names: Dict[int, str] = {}
    for key, block in cats.items():
        if not isinstance(block, dict):
            continue
        if not str(key).lower().startswith("cat_"):
            continue
        try:
            num = int(str(key).split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if num < 1 or num > 11:
            continue
        w = block.get("weight")
        try:
            raw_w[num] = float(w)
        except (TypeError, ValueError):
            continue
        nm = block.get("name")
        if nm:
            names[num] = str(nm).strip()

    if len(raw_w) < 11:
        return default_w, names

    s = sum(raw_w.values())
    if s <= 0:
        return default_w, names
    norm = {k: v / s for k, v in sorted(raw_w.items())}
    return norm, names


def scoring_block(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not cfg:
        return {}
    s = cfg.get("scoring") or {}
    return s if isinstance(s, dict) else {}


def metric_targets_from_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, float]:
    sb = scoring_block(cfg)
    mt = sb.get("metric_targets") or {}
    if not isinstance(mt, dict):
        return {}
    out: Dict[str, float] = {}
    for k, v in mt.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def document_thresholds_from_config(
    cfg: Optional[Dict[str, Any]],
) -> Tuple[Optional[float], Optional[float]]:
    sb = scoring_block(cfg)
    th = sb.get("document_pass_threshold")
    tg = sb.get("document_target")
    try:
        t_pass = float(th) if th is not None else None
    except (TypeError, ValueError):
        t_pass = None
    try:
        t_tgt = float(tg) if tg is not None else None
    except (TypeError, ValueError):
        t_tgt = None
    return t_pass, t_tgt


def config_label_for_report(cfg_path: Path, loaded: bool) -> str:
    if not loaded:
        return os.environ.get("PIPD_EVAL_CONFIG_LABEL", "composite+scenario1")
    try:
        rel = cfg_path.resolve().relative_to(_PKG_DIR.resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return cfg_path.name
