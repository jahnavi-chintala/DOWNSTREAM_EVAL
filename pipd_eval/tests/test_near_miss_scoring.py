"""Regression tests for near-miss classification and category score."""
from core.eval_scenario1 import (
    classify_near_miss,
    compute_category_score,
    find_paraphrase_pairs,
    precision_recall_f1_score,
)


def test_same_criterion_excl_format_tier_a():
    """GT shorthand vs generated full citation — same Excl index → CRITERION_FORMAT, Tier A credit."""
    assert classify_near_miss(
        "Subject met exclusion criteria 5 [Ongoing Long COVID or PASC diagnosis.]",
        "Excl 5 met: Ongoing Long COVID or PASC diagnosis.",
    ) == ("CRITERION_FORMAT", 0.99)


def test_bug1_numbering_error_exclusion_criteria():
    assert classify_near_miss(
        "Subject met exclusion criteria 5 [Hepatic dysfunction as defined in protocol.]",
        "Subject met exclusion criteria 12 [Hepatic dysfunction as defined in protocol.]",
    ) == ("NUMBERING_ERROR", 0.99)


def test_bug2_truncation_prefix():
    assert classify_near_miss(
        "Dosing/Administration error",
        "Dosing/Administration error: overdose.",
    ) == ("TRUNCATION", 0.60)


def test_bug3_compute_category_score():
    assert abs(compute_category_score(net=1, gt_count=1, generated_count=5) - 33.33) < 0.1
    assert abs(compute_category_score(net=11, gt_count=11, generated_count=12) - 95.65) < 0.1
    assert abs(compute_category_score(net=6, gt_count=21, generated_count=21) - 28.57) < 0.1


def test_bug3_edge_zero_net():
    assert compute_category_score(net=0, gt_count=0, generated_count=7) == 0.0
    assert compute_category_score(net=0, gt_count=3, generated_count=8) == 0.0


def test_f1_matches_harmonic_of_pr():
    p, r, f1 = precision_recall_f1_score(1.0, 1, 5)
    assert abs(p - 0.2) < 1e-9
    assert abs(r - 1.0) < 1e-9
    assert abs(compute_category_score(1.0, 1, 5) - 100.0 * f1) < 1e-9
    assert abs(f1 - 2.0 * 1.0 / (1 + 5)) < 1e-9  # 2·net/(gt+gen)


def test_paraphrase_pairs_long_vs_short_ccmed():
    """Long generated line vs short GT label for same deviation topic."""
    missed = [
        "Took prohibited concomitant medication/vaccine — not generated.",
    ]
    extras = [
        "Participant took a prohibited concomitant medication, such as a strong or moderate CYP3A inhibitor or inducer, during the study.",
    ]
    pairs, m_left, e_left = find_paraphrase_pairs(missed, extras)
    assert len(pairs) == 1
    assert pairs[0]["root_cause"] == "PARAPHRASE"
    assert pairs[0]["tier"] == "P"
    assert float(pairs[0]["credit"]) == 0.75
    assert len(m_left) == 0
    assert len(e_left) == 0
