"""
content_scorer.py — CMP Content Scorer
Scores generated CMP JSON against cmp_kri_ground_truth.csv and cmp_qtl_ground_truth.csv
using rules from cmp_eval_config.yaml.
"""

import re
import pandas as pd
from typing import Optional
from utils.utils import (
    score_label_match,
    best_label_match,
    score_threshold,
    score_tier,
    validate_iqmp_risk_id,
    score_weight,
    score_forms_variables,
    parse_threshold_value,
    gt_iqmp_is_blank,
    iqmp_value_absent,
    jaccard_similarity,
)


def _is_relative_score_only_threshold(value: any) -> bool:
    text = str(value or "").strip().lower()
    return "relative" in text and "score" in text


def _raw_threshold_from_kri(kri: dict, level: str) -> any:
    thresholds = kri.get("thresholds", {})
    if isinstance(thresholds, dict):
        level_data = thresholds.get(level)
        if isinstance(level_data, dict):
            return level_data.get("absolute")
        if level_data is not None:
            return level_data
    return kri.get(f"{level}_threshold")


def _generated_kri_label(k: dict) -> str:
    """Generator may use ``kri_label`` or legacy ``label``."""
    if not isinstance(k, dict):
        return ""
    return (k.get("kri_label") or k.get("label") or "").strip()


# ─── Ground Truth Loader ──────────────────────────────────────────────────────

class GroundTruth:
    def __init__(
        self,
        kri_csv_path: str,
        qtl_csv_path: str,
        study_metadata_csv_path: Optional[str] = None,
    ):
        self.kri_df = pd.read_csv(kri_csv_path, dtype=str)
        self.qtl_df = pd.read_csv(qtl_csv_path, dtype=str)
        self.study_meta_df: Optional[pd.DataFrame] = None
        if study_metadata_csv_path:
            self.study_meta_df = pd.read_csv(study_metadata_csv_path, dtype=str)

    def get_study_metadata_row(self, study_id: str) -> Optional[dict]:
        """Return study metadata CSV row as dict, or None."""
        if self.study_meta_df is None or self.study_meta_df.empty:
            return None
        sid = str(study_id).strip()
        df = self.study_meta_df
        if "study_id" not in df.columns:
            return None
        m = df[df["study_id"].str.strip() == sid]
        if m.empty:
            return None
        return m.iloc[0].to_dict()

    def get_kris_for_study(self, study_id: str) -> pd.DataFrame:
        """Return KRI rows for a given study_id."""
        df = self.kri_df[self.kri_df["study_id"].str.strip() == study_id.strip()].copy()
        return df

    def get_qtls_for_study(self, study_id: str) -> pd.DataFrame:
        """Return QTL rows for a given study_id."""
        df = self.qtl_df[self.qtl_df["study_id"].str.strip() == study_id.strip()].copy()
        return df

    def get_active_kris(self, study_id: str, kri_section: str = None) -> pd.DataFrame:
        """Return active (non-retired) KRIs for a study, optionally filtered by section."""
        df = self.get_kris_for_study(study_id)
        # Exclude retired KRIs from scoring denominator
        df = df[~df["status"].str.strip().str.lower().isin(["retired", "not_applicable"])]
        if kri_section:
            df = df[df["kri_section"].str.strip().str.lower() == kri_section.lower()]
        return df


# ─── KRI Attribute Scorer ─────────────────────────────────────────────────────

class KRIAttributeScorer:
    def __init__(self, config: dict):
        self.config = config
        self.attr_cfg = config.get("attributes", {})

    def score_kri_pair(self, generated_kri: dict, gt_row: pd.Series) -> dict:
        """
        Score one generated KRI against one ground truth KRI row.
        Returns dict of {attribute: score, ...} and total weighted score.
        """
        results = {}
        total_weight = 0.0
        total_weighted_score = 0.0

        # ── kri_label ──────────────────────────────────────────────────────────
        label_cfg = self.attr_cfg.get("kri_label", {})
        gen_lbl = _generated_kri_label(generated_kri)
        label_score, label_match_type = score_label_match(
            gen_lbl,
            str(gt_row.get("kri_label", "")),
            label_cfg,
        )
        w = label_cfg.get("weight", 0.35)
        results["kri_label"] = {
            "score": label_score,
            "match_type": label_match_type,
            "generated": gen_lbl,
            "ground_truth": str(gt_row.get("kri_label", "")),
            "weight": w,
        }
        total_weight += w
        total_weighted_score += label_score * w

        # ── moderate_threshold ────────────────────────────────────────────────
        mod_cfg = self.attr_cfg.get("moderate_threshold", {})
        raw_gen_mod = _raw_threshold_from_kri(generated_kri, "moderate")
        gen_mod = _extract_threshold_from_kri(generated_kri, "moderate")
        gt_mod = gt_row.get("moderate_threshold")
        if _is_relative_score_only_threshold(gt_mod) and _is_relative_score_only_threshold(raw_gen_mod):
            # The CMP PDF uses absolute threshold = n/a and standard relative
            # score thresholds 1.3 / 3. Some generators place those relative
            # values in moderate_threshold/high_threshold; score them against
            # the standard relative threshold instead of treating them as an
            # absolute-threshold mismatch.
            mod_score = score_threshold(gen_mod, "1.3", mod_cfg.get("tolerance_bands", {}))
        else:
            mod_score = score_threshold(gen_mod, gt_mod, mod_cfg.get("tolerance_bands", {}))
        w = mod_cfg.get("weight", 0.20)
        results["moderate_threshold"] = {
            "score": mod_score,
            "generated": gen_mod,
            "ground_truth": str(gt_mod) if gt_mod else None,
            "weight": w,
        }
        total_weight += w
        total_weighted_score += mod_score * w

        # ── high_threshold ────────────────────────────────────────────────────
        high_cfg = self.attr_cfg.get("high_threshold", {})
        raw_gen_high = _raw_threshold_from_kri(generated_kri, "high")
        gen_high = _extract_threshold_from_kri(generated_kri, "high")
        gt_high = gt_row.get("high_threshold")
        if _is_relative_score_only_threshold(gt_high) and _is_relative_score_only_threshold(raw_gen_high):
            high_score = score_threshold(gen_high, "3", high_cfg.get("tolerance_bands", {}))
        else:
            high_score = score_threshold(gen_high, gt_high, high_cfg.get("tolerance_bands", {}))
        w = high_cfg.get("weight", 0.15)
        results["high_threshold"] = {
            "score": high_score,
            "generated": gen_high,
            "ground_truth": str(gt_high) if gt_high else None,
            "weight": w,
        }
        total_weight += w
        total_weighted_score += high_score * w

        # ── iqmp_risk_id (accept asrp_risk_ids as alias per CMP v4+ convention) ─
        iqmp_cfg = self.attr_cfg.get("iqmp_risk_id", {})
        gt_iqmp = gt_row.get("iqmp_risk_id")

        gen_iqmp_raw = generated_kri.get("iqmp_risk_id")
        if not gen_iqmp_raw or not str(gen_iqmp_raw).strip():
            asrp_list = generated_kri.get("asrp_risk_ids")
            if isinstance(asrp_list, list) and asrp_list:
                gen_iqmp_raw = ", ".join(str(x) for x in asrp_list)

        iqmp_score = validate_iqmp_risk_id(gen_iqmp_raw, gt_iqmp)
        w = iqmp_cfg.get("weight", 0.15)
        gt_blank = gt_iqmp_is_blank(gt_iqmp)
        gen_iqmp_empty = iqmp_value_absent(gen_iqmp_raw)
        results["iqmp_risk_id"] = {
            "score": iqmp_score,
            "generated": gen_iqmp_raw,
            "ground_truth": str(gt_row.get("iqmp_risk_id", "")),
            "weight": w,
            "is_hallucination": (
                not gt_blank
                and iqmp_score == 0.0
                and not gen_iqmp_empty
            ),
            "is_missing": (
                not gt_blank
                and gen_iqmp_empty
            ),
        }
        total_weight += w
        total_weighted_score += iqmp_score * w

        # ── confidence_tier ───────────────────────────────────────────────────
        # Ground truth doesn't have confidence_tier — skip if not in GT
        tier_cfg = self.attr_cfg.get("confidence_tier", {})
        w = tier_cfg.get("weight", 0.10)
        tier_score = 1.0  # No GT for tier — don't penalize (full credit)
        results["confidence_tier"] = {
            "score": tier_score,
            "generated": generated_kri.get("confidence_tier"),
            "weight": w,
            "note": "no_ground_truth",
        }
        total_weight += w
        total_weighted_score += tier_score * w

        # ── weight_field ──────────────────────────────────────────────────────
        wf_cfg = self.attr_cfg.get("weight_field", {})
        w = wf_cfg.get("weight", 0.05)
        wt_score = score_weight(
            generated_kri.get("weight"),
            gt_row.get("weight"),
            wf_cfg.get("vocabulary", ["Low", "Moderate", "High"])
        )
        results["weight_field"] = {
            "score": wt_score,
            "generated": generated_kri.get("weight"),
            "ground_truth": str(gt_row.get("weight", "")),
            "weight": w,
        }
        total_weight += w
        total_weighted_score += wt_score * w

        # ── forms_variables ───────────────────────────────────────────────────
        fv_cfg = self.attr_cfg.get("forms_variables", {})
        w = fv_cfg.get("weight", 0.0)
        if w > 0:
            gt_fv = gt_row.get("forms_variables")
            gt_fv_str = str(gt_fv) if pd.notna(gt_fv) and str(gt_fv).strip().lower() not in ("nan", "") else ""
            gen_fv = (
                generated_kri.get("forms_variables")
                or generated_kri.get("form_names_variables")
                or generated_kri.get("source_form_names_variables")
            )
            if isinstance(gen_fv, list):
                fv_score = score_forms_variables(gen_fv, gt_fv_str)
            elif isinstance(gen_fv, str) and gen_fv.strip():
                fv_score = score_forms_variables([gen_fv], gt_fv_str)
            elif not gt_fv_str:
                fv_score = 1.0
            else:
                fv_score = 0.0
            results["forms_variables"] = {
                "score": fv_score,
                "generated": gen_fv,
                "ground_truth": gt_fv_str or None,
                "weight": w,
            }
            total_weight += w
            total_weighted_score += fv_score * w

        # ── logic_summary ────────────────────────────────────────────────────
        ls_cfg = self.attr_cfg.get("logic_summary", {})
        w = ls_cfg.get("weight", 0.0)
        if w > 0:
            jac_thr = ls_cfg.get("jaccard_thresholds", {})
            full_t = jac_thr.get("full_credit", 0.15)
            partial_t = jac_thr.get("partial_credit", 0.08)
            gt_ls = gt_row.get("logic_summary")
            gt_ls_str = str(gt_ls) if pd.notna(gt_ls) and str(gt_ls).strip().lower() not in ("nan", "") else ""
            gen_ls = generated_kri.get("logic_summary") or generated_kri.get("description_of_kri_logic") or ""
            if gt_ls_str and gen_ls:
                jac = jaccard_similarity(gen_ls, gt_ls_str)
                ls_score = 1.0 if jac >= full_t else (0.5 if jac >= partial_t else 0.0)
            elif not gt_ls_str and not gen_ls:
                ls_score = 1.0
            elif not gt_ls_str:
                ls_score = 0.5
            else:
                ls_score = 0.0
            results["logic_summary"] = {
                "score": ls_score,
                "generated": gen_ls[:200] if gen_ls else None,
                "ground_truth": gt_ls_str[:200] if gt_ls_str else None,
                "weight": w,
                "jaccard": round(jac, 3) if gt_ls_str and gen_ls else None,
            }
            total_weight += w
            total_weighted_score += ls_score * w

        # ── corrective_action ────────────────────────────────────────────────
        ca_cfg = self.attr_cfg.get("corrective_action", {})
        w = ca_cfg.get("weight", 0.0)
        if w > 0:
            jac_thr = ca_cfg.get("jaccard_thresholds", {})
            full_t = jac_thr.get("full_credit", 0.15)
            partial_t = jac_thr.get("partial_credit", 0.08)
            gt_ca = gt_row.get("corrective_action")
            gt_ca_str = str(gt_ca) if pd.notna(gt_ca) and str(gt_ca).strip().lower() not in ("nan", "") else ""
            gen_ca = generated_kri.get("corrective_action") or generated_kri.get("suggested_corrective_actions") or ""
            if gt_ca_str and gen_ca:
                jac_ca = jaccard_similarity(gen_ca, gt_ca_str)
                ca_score = 1.0 if jac_ca >= full_t else (0.5 if jac_ca >= partial_t else 0.0)
            elif not gt_ca_str and not gen_ca:
                ca_score = 1.0
            elif not gt_ca_str:
                ca_score = 0.5
            else:
                ca_score = 0.0
            results["corrective_action"] = {
                "score": ca_score,
                "generated": gen_ca[:200] if gen_ca else None,
                "ground_truth": gt_ca_str[:200] if gt_ca_str else None,
                "weight": w,
                "jaccard": round(jac_ca, 3) if gt_ca_str and gen_ca else None,
            }
            total_weight += w
            total_weighted_score += ca_score * w

        # ── Overall KRI score ─────────────────────────────────────────────────
        overall = (total_weighted_score / total_weight * 100) if total_weight > 0 else 0.0
        results["_kri_score"] = round(overall, 1)

        return results


# ─── Section Scorer ───────────────────────────────────────────────────────────

class SectionScorer:
    def __init__(self, config: dict, ground_truth: GroundTruth):
        self.config = config
        self.gt = ground_truth
        self.attr_scorer = KRIAttributeScorer(config)
        self.scoring_cfg = config.get("scoring", {})

    def score_global_kris(self, study_id: str, cmp_json: dict) -> dict:
        """Score global KRIs section."""
        gt_globals = self.gt.get_active_kris(study_id, "global")
        gen_globals = cmp_json.get("global_kris", [])
        return self._score_kri_section(gen_globals, gt_globals, "global")

    def score_ss_kris(self, study_id: str, cmp_json: dict) -> dict:
        """Score study-specific KRIs section."""
        gt_ss = self.gt.get_active_kris(study_id, "study_specific")
        gen_ss = cmp_json.get("study_specific_kris", [])
        # Sister KRIs support QTL interpretation but are not part of the
        # generated study_specific_kris section. The actual CMP metadata counts
        # C5091017 as 10 active study-specific KRIs; including sister rows here
        # creates false misses.
        return self._score_kri_section(gen_ss, gt_ss, "study_specific")

    def score_qtls(self, study_id: str, cmp_json: dict) -> dict:
        """Score QTLs section."""
        gt_qtls = self.gt.get_qtls_for_study(study_id)
        # Filter active QTLs
        if "status" in gt_qtls.columns:
            gt_qtls = gt_qtls[~gt_qtls["status"].str.strip().str.lower().isin(["retired"])]
        gen_qtls = cmp_json.get("qtls", [])
        return self._score_qtl_section(gen_qtls, gt_qtls)

    def score_metadata(self, study_id: str, cmp_json: dict) -> dict:
        """Score metadata section (analysis_frequency / analysis_schedule aliases)."""
        af = cmp_json.get("analysis_frequency") or cmp_json.get("analysis_schedule") or {}
        has_frequency = bool(
            af and (
                af.get("enrollment_phase")
                or af.get("first_kri_trigger")
                or af.get("first_analysis_trigger")
                or af.get("kri_frequency")
            )
        )
        if study_id.strip().upper() == "C5091017" and has_frequency:
            first_trigger = str(af.get("first_analysis_trigger") or af.get("first_kri_trigger") or "").lower()
            kri_text = " ".join(
                str(af.get(k) or "") for k in [
                    "kri_frequency",
                    "kri_frequency_during_enrollment",
                    "kri_frequency_post_enrollment",
                ]
            ).lower()
            dqa_text = " ".join(
                str(af.get(k) or "") for k in [
                    "dqa_frequency",
                    "dqa_frequency_during_enrollment",
                    "dqa_frequency_post_enrollment",
                ]
            ).lower()
            checks = {
                "first_kri_analysis_trigger": "10 unique sites" in first_trigger and "screened" in first_trigger,
                "kri_during_enrollment_monthly": "monthly" in kri_text,
                "kri_after_enrollment_monthly": "monthly" in kri_text and "every other" not in kri_text,
                "dqa_during_enrollment_monthly": "monthly" in dqa_text,
                "dqa_after_enrollment_quarterly": "quarterly" in dqa_text,
            }
            score = 40.0 + 10.0 * sum(checks.values())
        else:
            checks = {}
            score = 90.0 if has_frequency else 50.0
        return {
            "section_score": score,
            "has_analysis_frequency": has_frequency,
            "checks": checks,
            "details": af,
        }

    # ── Internal: KRI section matching ───────────────────────────────────────

    def _score_kri_section(self, gen_kris: list, gt_df: pd.DataFrame, section_type: str) -> dict:
        """
        Match generated KRIs to ground truth and score.
        Uses max(generated, ground_truth) as denominator.
        """
        if gt_df.empty and not gen_kris:
            return {"section_score": 100.0, "kri_count_gen": 0, "kri_count_gt": 0,
                    "matched_kris": [], "missed_kris": [], "hallucinated_kris": []}

        gt_labels = [str(r["kri_label"]) for _, r in gt_df.iterrows() if pd.notna(r.get("kri_label"))]
        gen_labels = [_generated_kri_label(k) for k in gen_kris if isinstance(k, dict)]

        label_cfg = self.config.get("attributes", {}).get("kri_label", {})
        kri_scoring_cfg = self.scoring_cfg.get("kri_scoring", {})
        denominator = max(len(gen_kris), len(gt_df)) if (gen_kris or len(gt_df) > 0) else 1

        matched = []
        missed = []
        hallucinated_labels = []
        gt_matched_indices = set()

        # For each generated KRI, find best GT match
        gen_kri_scores = []
        for gen_kri in gen_kris:
            if not isinstance(gen_kri, dict):
                continue
            gen_label = _generated_kri_label(gen_kri)
            best_score = 0.0
            best_gt_row = None
            best_gt_idx = -1

            for idx, (_, gt_row) in enumerate(gt_df.iterrows()):
                if idx in gt_matched_indices:
                    continue
                gt_label = str(gt_row.get("kri_label", ""))
                s, _ = score_label_match(gen_label, gt_label, label_cfg)
                if s > best_score:
                    best_score = s
                    best_gt_row = gt_row
                    best_gt_idx = idx

            if best_score >= 0.30 and best_gt_row is not None:
                # Matched — score all attributes
                gt_matched_indices.add(best_gt_idx)
                attr_scores = self.attr_scorer.score_kri_pair(gen_kri, best_gt_row)
                kri_score = attr_scores["_kri_score"]
                is_hallucination = attr_scores.get("iqmp_risk_id", {}).get("is_hallucination", False)
                matched.append({
                    "generated_label": gen_label,
                    "gt_label": str(best_gt_row.get("kri_label", "")),
                    "kri_score": kri_score,
                    "attribute_scores": attr_scores,
                    "is_hallucination": is_hallucination,
                })
                gen_kri_scores.append(kri_score / 100.0)
            else:
                # Not matched — potential hallucination
                hallucinated_labels.append(gen_label)
                gen_kri_scores.append(kri_scoring_cfg.get("hallucination_score", 0.0))

        # GT rows not matched = missed KRIs
        for idx, (_, gt_row) in enumerate(gt_df.iterrows()):
            if idx not in gt_matched_indices:
                missed.append({
                    "gt_label": str(gt_row.get("kri_label", "")),
                    "kri_id": str(gt_row.get("kri_code", gt_row.get("kri_number", ""))),
                    "score": kri_scoring_cfg.get("miss_score", 0.0),
                })
                gen_kri_scores.append(kri_scoring_cfg.get("miss_score", 0.0))

        # Pad to denominator
        while len(gen_kri_scores) < denominator:
            gen_kri_scores.append(0.0)

        section_score = (sum(gen_kri_scores) / denominator * 100) if denominator > 0 else 0.0

        return {
            "section_score": round(section_score, 1),
            "kri_count_gen": len(gen_kris),
            "kri_count_gt": len(gt_df),
            "matched_count": len(matched),
            "missed_count": len(missed),
            "hallucinated_count": len(hallucinated_labels),
            "matched_kris": matched,
            "missed_kris": missed,
            "hallucinated_kris": hallucinated_labels,
        }

    # ── Internal: QTL section matching ────────────────────────────────────────

    def _score_qtl_section(self, gen_qtls: list, gt_df: pd.DataFrame) -> dict:
        """Score QTLs against ground truth."""
        label_cfg = self.config.get("attributes", {}).get("kri_label", {})
        qtl_num_cfg = self.scoring_cfg.get("qtl_numeric_scoring", {})
        exp_tol = qtl_num_cfg.get("expectation_tolerance_pct", 3)
        lim_tol = qtl_num_cfg.get("tolerance_limit_tolerance_pct", 5)

        gt_qtl_names = [str(r["qtl_name"]) for _, r in gt_df.iterrows() if pd.notna(r.get("qtl_name"))]
        denominator = max(len(gen_qtls), len(gt_df)) if (gen_qtls or not gt_df.empty) else 1
        matched, missed = [], []
        gt_matched = set()
        qtl_scores = []

        for gen_qtl in gen_qtls:
            if not isinstance(gen_qtl, dict):
                continue
            gen_name = gen_qtl.get("name", gen_qtl.get("qtl_name", ""))
            best_s, best_type, best_gt_name = best_label_match(gen_name, gt_qtl_names, label_cfg)

            # Substring / prefix boost for truncated GT names (PDF extraction fragments)
            if best_s < 0.35:
                gn_l = gen_name.lower()
                for candidate_gt in gt_qtl_names:
                    if not candidate_gt:
                        continue
                    cl = candidate_gt.lower()
                    # GT is prefix of generated
                    if gen_name.lower().startswith(cl):
                        best_s = max(best_s, 0.80)
                        best_gt_name = candidate_gt
                    # Full GT text appears inside longer generated title
                    elif len(candidate_gt) >= 8 and cl in gn_l:
                        best_s = max(best_s, 0.88)
                        best_gt_name = candidate_gt
                    # Key fragment: "percentage of subjects" vs "% of subjects …"
                    elif "subject" in cl and "subject" in gn_l and len(candidate_gt) >= 12:
                        from utils.utils import jaccard_similarity as _jac_sim

                        jac = _jac_sim(gen_name, candidate_gt)
                        if jac >= 0.35:
                            best_s = max(best_s, 0.82)
                            best_gt_name = candidate_gt
                    elif len(candidate_gt) >= 10:
                        from utils.utils import jaccard_similarity as _jac_sim2

                        jac = _jac_sim2(gen_name, candidate_gt)
                        if jac >= 0.50:
                            best_s = max(best_s, 0.55)
                            best_gt_name = candidate_gt
            if best_s >= 0.35:
                # Find GT row
                gt_row = None
                for idx, (_, row) in enumerate(gt_df.iterrows()):
                    if str(row.get("qtl_name", "")) == best_gt_name and idx not in gt_matched:
                        gt_row = row
                        gt_matched.add(idx)
                        break

                if gt_row is None:
                    # Name is similar to an already-claimed GT QTL; count as extra generated QTL.
                    missed.append(
                        {
                            "generated_name": gen_name,
                            "reason": "no_gt_match",
                        }
                    )
                    qtl_scores.append(0.0)
                    continue

                qtl_score = best_s  # name match contributes
                exp_score = 1.0
                tol_score = 1.0

                if gt_row is not None:
                    # Score expectation_pct
                    gt_exp = parse_threshold_value(gt_row.get("expectation"))
                    gen_exp = parse_threshold_value(gen_qtl.get("expectation_pct"))
                    if gt_exp is not None and gen_exp is not None:
                        diff = abs(gen_exp - gt_exp)
                        exp_score = 1.0 if diff <= exp_tol else (0.5 if diff <= exp_tol * 3 else 0.0)
                    elif gt_exp is None and gen_exp is None:
                        exp_score = 1.0

                    # Score tolerance_limit_pct
                    gt_tol_raw = gt_row.get("tolerance_limit")
                    # tolerance_limit in CSV might be ">38%" etc
                    gt_tol = parse_threshold_value(str(gt_tol_raw) if gt_tol_raw else None)
                    gen_tol = parse_threshold_value(gen_qtl.get("tolerance_limit_pct"))
                    if gt_tol is not None and gen_tol is not None:
                        diff = abs(gen_tol - gt_tol)
                        tol_score = 1.0 if diff <= lim_tol else (0.5 if diff <= lim_tol * 3 else 0.0)
                    elif gt_tol is None and gen_tol is None:
                        tol_score = 1.0

                    # Has sister KRI?
                    gt_has_sister = str(gt_row.get("has_sister_kri", "")).lower() == "true"
                    gen_has_sister = bool(gen_qtl.get("sister_kri"))
                    sister_score = 1.0 if (gen_has_sister == gt_has_sister) else 0.5

                    overall = (best_s * 0.5 + exp_score * 0.25 + tol_score * 0.15 + sister_score * 0.10)
                else:
                    overall = best_s * 0.5  # only name match

                qtl_scores.append(overall)
                matched.append({
                    "generated_name": gen_name,
                    "gt_name": best_gt_name,
                    "name_match_type": best_type,
                    "name_score": round(best_s * 100, 1),
                    "expectation_score": round(exp_score * 100, 1),
                    "tolerance_score": round(tol_score * 100, 1),
                    "qtl_score": round(overall * 100, 1),
                })
            else:
                missed.append({"generated_name": gen_name, "reason": "no_gt_match"})
                qtl_scores.append(0.0)

        # GT QTLs not matched
        for idx, (_, row) in enumerate(gt_df.iterrows()):
            if idx not in gt_matched:
                missed.append({"gt_name": str(row.get("qtl_name", "")), "reason": "not_generated"})
                qtl_scores.append(0.0)

        while len(qtl_scores) < denominator:
            qtl_scores.append(0.0)

        section_score = (sum(qtl_scores) / denominator * 100) if denominator > 0 else 0.0

        return {
            "section_score": round(section_score, 1),
            "qtl_count_gen": len(gen_qtls),
            "qtl_count_gt": len(gt_df),
            "matched_count": len(matched),
            # Unique GT rows that received at least one generator match (use for M3 recall)
            "gt_matched_count": len(gt_matched),
            "matched_qtls": matched,
            "missed_qtls": missed,
        }


# ─── Metric Calculators ───────────────────────────────────────────────────────

def calculate_m1_kri_recall(section_results: dict, config: dict) -> dict:
    """M1: KRI Recall — fraction of GT KRIs that were generated (label match)."""
    global_r = section_results.get("global_kris", {})
    ss_r = section_results.get("study_specific_kris", {})

    gt_total = global_r.get("kri_count_gt", 0) + ss_r.get("kri_count_gt", 0)
    matched = global_r.get("matched_count", 0) + ss_r.get("matched_count", 0)
    recall = matched / gt_total if gt_total > 0 else 0.0

    target = config.get("scoring", {}).get("metric_targets", {}).get("m1_kri_recall", 0.80)
    return {
        "metric": "M1 KRI Recall",
        "score": round(recall, 3),
        "score_pct": f"{recall*100:.0f}%",
        "target": target,
        "passed": recall >= target,
        "matched": matched,
        "gt_total": gt_total,
        "detail": f"{matched}/{gt_total} GT KRIs matched",
    }


def calculate_m2_threshold_accuracy(section_results: dict, config: dict) -> dict:
    """M2: Threshold Accuracy — % of thresholds within 5% of ground truth."""
    total_checks, correct_checks = 0, 0
    for section in ["global_kris", "study_specific_kris"]:
        for kri_match in section_results.get(section, {}).get("matched_kris", []):
            for thr in ["moderate_threshold", "high_threshold"]:
                thr_result = kri_match.get("attribute_scores", {}).get(thr, {})
                total_checks += 1
                if thr_result.get("score", 0) >= 1.0:  # within exact_pct band
                    correct_checks += 1

    # Default 0.0 when no KRI matches exist — not N/A, but genuinely not evaluated
    accuracy = correct_checks / total_checks if total_checks > 0 else 0.0
    target = config.get("scoring", {}).get("metric_targets", {}).get("m2_threshold_accuracy", 0.90)
    return {
        "metric": "M2 Threshold Accuracy",
        "score": round(accuracy, 3),
        "score_pct": f"{accuracy*100:.0f}%",
        "target": target,
        "passed": accuracy >= target,
        "correct": correct_checks,
        "total_checks": total_checks,
        "detail": f"{correct_checks}/{total_checks} thresholds within ±5% of ground truth",
    }


def calculate_m3_qtl_recall(section_results: dict, config: dict) -> dict:
    """M3: QTL Recall — fraction of GT QTLs that have at least one accepted generator match."""
    qtl_r = section_results.get("qtls", {})
    gt_count = qtl_r.get("qtl_count_gt", 0)
    # matched_count counts generator-side rows; it can exceed gt_count when many gen QTLs fire.
    gt_hit = qtl_r.get("gt_matched_count")
    if gt_hit is not None:
        numer = int(gt_hit)
    else:
        gen_matched = qtl_r.get("matched_count", 0)
        numer = min(int(gen_matched), int(gt_count)) if gt_count else 0
    recall = (numer / gt_count) if gt_count > 0 else 1.0
    recall = min(1.0, max(0.0, float(recall)))

    target = config.get("scoring", {}).get("metric_targets", {}).get("m3_qtl_recall", 0.85)
    return {
        "metric": "M3 QTL Recall",
        "score": round(recall, 3),
        "score_pct": f"{recall*100:.0f}%",
        "target": target,
        "passed": recall >= target,
        "matched": numer,
        "gt_total": gt_count,
        "detail": f"{numer}/{gt_count} GT QTLs matched",
    }


def calculate_m4_hallucinations(section_results: dict, cmp_json: dict, config: dict) -> dict:
    """
    M4: Hallucinations.
    Counts:
      1) Matched KRIs where IQMP maps to substantive generated value scoring 0 vs GT (`is_hallucination`).
         Placeholder strings (`N/A`, ``tbd`` …) count as absent (`is_missing`).
      2) Extra KRIs (generated with no GT label match)
      3) Extra QTLs (generated with no GT QTL match)
    Missing / placeholder IQMP values are tracked separately from conflicting IDs.
    """
    hallucinated_items = []
    missing_iqmp_items = []
    for section in ["global_kris", "study_specific_kris"]:
        for kri_match in section_results.get(section, {}).get("matched_kris", []):
            iqmp_info = kri_match.get("attribute_scores", {}).get("iqmp_risk_id", {})
            if iqmp_info.get("is_hallucination"):
                hallucinated_items.append({
                    "kri_label": kri_match.get("generated_label"),
                    "generated_iqmp": iqmp_info.get("generated"),
                    "gt_iqmp": iqmp_info.get("ground_truth"),
                    "type": "conflicting_or_invalid_iqmp_id",
                })
            elif iqmp_info.get("is_missing"):
                missing_iqmp_items.append({
                    "kri_label": kri_match.get("generated_label"),
                    "gt_iqmp": iqmp_info.get("ground_truth"),
                    "type": "missing_iqmp_id",
                })
        for extra_label in section_results.get(section, {}).get("hallucinated_kris", []):
            hallucinated_items.append(
                {
                    "kri_label": extra_label,
                    "generated_iqmp": None,
                    "gt_iqmp": None,
                    "type": "extra_kri_no_gt_match",
                }
            )

    for miss in section_results.get("qtls", {}).get("missed_qtls", []):
        if miss.get("reason") == "no_gt_match":
            hallucinated_items.append(
                {
                    "qtl_name": miss.get("generated_name"),
                    "type": "extra_qtl_no_gt_match",
                }
            )

    count = len(hallucinated_items)
    target = config.get("scoring", {}).get("metric_targets", {}).get("m4_hallucination", 0)
    return {
        "metric": "M4 Hallucinations",
        "score": count,
        "target": target,
        "passed": count == 0,
        "hallucinated_items": hallucinated_items,
        "missing_iqmp_items": missing_iqmp_items,
        "missing_iqmp_count": len(missing_iqmp_items),
        "detail": f"{count} hallucinations detected + {len(missing_iqmp_items)} missing IQMP IDs",
        "note": (
            "Hallucinations = extra KRI/QTL rows or substantive IQMP/ASRP values that score 0 against "
            "GT IQMP — not placeholders (N/A, tbd…). Missing IQMP = absent or placeholder when GT expects a substantive id."
        ),
    }


# ─── Document Score Calculator ────────────────────────────────────────────────

def calculate_document_score(section_results: dict, structure_score: float, config: dict) -> dict:
    """
    Compute final document score 0-100 from section scores × weights.
    """
    section_cfg = config.get("sections", {})
    weighted_total = 0.0
    section_contributions = {}

    for section_key, cfg in section_cfg.items():
        weight = cfg.get("weight", 0.0)
        if section_key == "global_kris":
            score = section_results.get("global_kris", {}).get("section_score", 0.0)
        elif section_key == "study_specific_kris":
            score = section_results.get("study_specific_kris", {}).get("section_score", 0.0)
        elif section_key == "qtls":
            score = section_results.get("qtls", {}).get("section_score", 0.0)
        elif section_key == "section_metadata":
            score = section_results.get("metadata", {}).get("section_score", 0.0)
        else:
            score = 0.0

        contribution = score * weight
        weighted_total += contribution
        section_contributions[section_key] = {
            "score": round(score, 1),
            "weight": weight,
            "weighted": round(contribution, 2),
        }

    # Structure score penalty (small — structure is prerequisite)
    structure_factor = structure_score / 100.0
    final = weighted_total * (0.9 + 0.1 * structure_factor)
    final = round(min(100.0, max(0.0, final)), 1)

    pass_threshold = config.get("scoring", {}).get("document_pass_threshold", 75)
    target = config.get("scoring", {}).get("document_target", 80)

    return {
        "document_score": final,
        "passed": final >= pass_threshold,
        "pass_threshold": pass_threshold,
        "target": target,
        "pre_structure_score": round(weighted_total, 2),
        "structure_factor": round(0.9 + 0.1 * structure_factor, 3),
        "section_contributions": section_contributions,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_threshold_from_kri(kri: dict, level: str) -> Optional[float]:
    """
    Extract numeric threshold value from various JSON threshold structures.
    Handles: thresholds.moderate.absolute, thresholds.moderate (flat),
    moderate_threshold (flat field), etc.
    """
    thresholds = kri.get("thresholds", {})
    if isinstance(thresholds, dict):
        level_data = thresholds.get(level, {})
        if isinstance(level_data, dict):
            abs_val = level_data.get("absolute")
            if abs_val is not None:
                return parse_threshold_value(abs_val)
        elif level_data is not None:
            return parse_threshold_value(level_data)

    # Flat fields
    flat_key = f"{level}_threshold"
    flat = kri.get(flat_key)
    if flat is not None:
        return parse_threshold_value(flat)

    return None
