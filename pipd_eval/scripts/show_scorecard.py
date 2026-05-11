"""Print category scorecard (Weight, Weighted, Score, Subcats, Status) — same as reference §2."""
import json
import sys
from pathlib import Path

from core.eval_scenario1 import compute_category_score


def _load_eval_results(p: Path) -> dict:
    if p.is_file() and p.suffix.lower() == ".json":
        return json.loads(p.read_text("utf-8"))
    if p.is_dir():
        named = p / f"pipd_eval_{p.name}.json"
        if named.is_file():
            return json.loads(named.read_text("utf-8"))
        matches = sorted(p.glob("pipd_eval_*.json"))
        if matches:
            return json.loads(matches[0].read_text("utf-8"))
    raise SystemExit(f"No pipd_eval_*.json found: {p}")


_base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    r"C:\Users\jahna\OneDrive\Desktop\Pfizer\protocol_eval_hub\protocol_bundles\C4891002\eval_outputs\C4891002"
)
data = _load_eval_results(_base)
per = data.get("per_category") or {}
near_by_cat: dict = {}
for nm in data.get("near_misses") or []:
    c = int(nm.get("category_num") or 0)
    near_by_cat.setdefault(c, []).append(nm)

_total_gt = sum(
    int((per.get(cn) or per.get(str(cn)) or {}).get("m1_gt_total") or 0)
    for cn in range(1, 12)
)
print(f"Total GT subcategories: {_total_gt}\n")
hdr = f"{'Cat':<4}{'Weight':>10}{'Weighted':>10}{'Score':>16}{'Subcats':>10}  Status"
print(hdr)
print("-" * 120)
for cn in range(1, 12):
    cb = per.get(cn) or per.get(str(cn)) or {}
    matched = int(cb.get("m1_matched") or 0)
    gt_tot = int(cb.get("m1_gt_total") or 0)
    nm_cnt = int(cb.get("m1_near_misses") or 0)
    near_credit = 0.0
    for _nm in near_by_cat.get(cn, []):
        try:
            near_credit += float(_nm.get("credit") or 0.0)
        except (TypeError, ValueError):
            pass
    missed = cb.get("missed_subcats") or []
    hall = cb.get("hallucinated_subcats") or []
    n_extra = len(hall)
    m3ok = bool(cb.get("m3_none_identified_correct", True))
    if _total_gt > 0 and gt_tot > 0:
        w_pct = round(100.0 * gt_tot / _total_gt, 2)
    else:
        w_pct = 0.0
    if gt_tot == 0:
        if m3ok and not hall:
            score_pct = 100.0
            status = "None identified — both agree"
        elif m3ok and hall:
            score_pct = 0.0
            status = f"{n_extra} extra generated — none expected in GT"
        else:
            score_pct = 0.0
            status = "none_identified mismatch — see Section 3"
        weighted = 0.0
        w_cell = "—"
        sub_txt = "—"
    else:
        net = matched + near_credit
        gen_tot = int(cb.get("m1_generated_total") or 0)
        score_pct = round(compute_category_score(float(net), gt_tot, gen_tot), 1)
        weighted = max(0.0, round(w_pct * score_pct / 100.0, 2))
        w_cell = f"{w_pct:.2f}%"
        sub_pass = matched + nm_cnt
        sub_txt = f"{sub_pass}/{gt_tot}"
        if not missed and not hall:
            status = "All subcategories matched"
        else:
            parts = []
            if matched:
                parts.append(f"{matched} verbatim")
            if nm_cnt:
                parts.append(f"{nm_cnt} near miss")
            if missed:
                n = len(missed)
                parts.append(f"{n} miss" + ("" if n == 1 else "es"))
            if hall:
                parts.append(f"{n_extra} extra (precision risk)")
            status = ", ".join(parts) + " — see Section 3"
    score_cell = f"{score_pct:.1f} / 100"
    print(f"{cn:<4}{w_cell:>10}{weighted:>10.2f}{score_cell:>16}{sub_txt:>10}  {status[:80]}")
print("-" * 120)
