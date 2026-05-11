#!/usr/bin/env python3
"""
Run PIPD, Risk Profile, and CMP evals for **one protocol** using a **single input bundle folder**.

Convention
----------
  protocol_bundles/<STUDY_ID>/
    <STUDY_ID>_PIPD.json              (or *_PIPD.json)
    <STUDY_ID>_RiskProfile.json       (or *RiskProfile*.json)
    <STUDY_ID>_CMP.json               (or *CMP.json)
    optional: USDM_*.json, other protocol-specific JSON (ID cross-check only)
    optional: protocol_manifest.yaml  — explicit filenames (see example in repo)

Ground truth CSVs / eval configs stay in each project (pipd_eval, risk_profile_eval, cmp_eval, dmp_eval)
unless you override paths with CLI flags.

USDM protocol JSON
------------------
  • **PIPD:** Optional but recommended. Scenario 1 M4 checks non-null ``usdm_entity_id`` values
    against the protocol graph when USDM JSON loads. Scenario 2 and USDM provenance blocks use
    ``pipd_usdm_support.resolve_usdm_protocol_path`` (``PIPD_USDM_JSON``, ``data/usdm_protocol_*.json``).
    The bundle runner passes ``--usdm_json`` to ``pipd_eval/scripts/run_eval.py`` when it finds
    ``*USDM*.json`` / ``usdm*.json`` in the bundle or ``files.usdm`` in ``protocol_manifest.yaml``.
  • **Risk Profile / CMP:** Eval CLIs do not take a separate USDM file; traceability is inside
    the generator JSON (``usdm_sources``, ``usdm_drivers``, etc.).

Study / protocol ID
-------------------
  • Prefer ``--study-id``; else the bundle folder name must look like B7981027 / C4891023.
  • Each generator JSON is loaded and scanned for identifiers; they must all match ``study_id``
    (unless ``--loose-id-check``).

Outputs
-------
  protocol_bundles/<STUDY_ID>/eval_outputs/<STUDY_ID>/
    pipd_eval_<STUDY_ID>.json
    pipd_eval_<STUDY_ID>.yaml / .docx              (Scenario 1: combined config+report YAML, Word)
    pipd_eval_<STUDY_ID>_near_misses.csv          (Scenario 1, if any)
    pipd_eval_<STUDY_ID>_hallucination_report.json
    risk_profile_eval_<STUDY_ID>.json
    risk_profile_eval_<STUDY_ID>.yaml / .docx      (unless --no-* in child CLIs)
    cmp_eval_<STUDY_ID>.json / .yaml / .docx
    dmp_eval_<STUDY_ID>.json / .yaml / .docx        (when ``*_DMP.json`` in bundle)

  By default, PIPD is run with ``--scenario1-only`` (no Scenario 2). Use
  ``--allow-pipd-scenario2`` on the bundle runner to evaluate all studies with proxy signals too.

  Exit code
  ---------
  By default this hub exits **0** after all eval subprocesses **finish** (whether scores pass or not),
  because your job is to **produce eval artifacts**, not to make generator output pass. Child tools
  often return non-zero for NO-GO / failed thresholds while still writing JSON/YAML/Word. Use
  ``--fail-on-metric-fail`` if you need a non-zero exit when any child reports metric failure.

  Run every protocol under a parent folder::

    python run_protocol_eval_bundle.py --bundles-parent protocol_bundles
    python run_protocol_eval_bundle.py --all-protocols protocol_bundles   # same as --bundles-parent

Examples
--------
  cd protocol_eval_hub
  python run_protocol_eval_bundle.py --bundle protocol_bundles/B7981027

  python run_protocol_eval_bundle.py --bundle D:/work/B7981027 --study-id B7981027 --dry-run

  python run_protocol_eval_bundle.py --bundles-parent protocol_bundles --dry-run

  python run_protocol_eval_bundle.py --all-protocols protocol_bundles
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_STUDY_ID_RE = re.compile(r"^([BCbc]\d{7})$")


def _hub_root() -> Path:
    return Path(__file__).resolve().parent


def _pfizer_root() -> Path:
    return _hub_root().parent


def _repo_paths() -> Dict[str, Path]:
    root = _pfizer_root()
    return {
        "pipd_eval": root / "pipd_eval",
        "risk_profile_eval": root / "risk_profile_eval",
        "cmp_eval": root / "cmp_eval",
        "dmp_eval": root / "dmp_eval",
    }


def infer_study_id(bundle_dir: Path) -> Optional[str]:
    name = bundle_dir.name.strip()
    if _STUDY_ID_RE.match(name):
        return name[:1].upper() + name[1:].upper()
    return None


def load_manifest(bundle_dir: Path) -> Optional[Dict[str, Any]]:
    for name in ("protocol_manifest.yaml", "protocol_manifest.yml"):
        p = bundle_dir / name
        if p.is_file():
            try:
                import yaml  # type: ignore

                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                print(f"[WARN] Could not read manifest {p}: {exc}", file=sys.stderr)
    return None


def _collect_ids_from_obj(obj: Any, max_depth: int = 4) -> Set[str]:
    """Pull likely protocol/study id strings from nested dict/list (bounded depth)."""
    out: Set[str] = set()
    keys = frozenset(
        {
            "study_id",
            "studyId",
            "protocol_id",
            "protocolId",
            "id",
            "nct_id",
            "NCTId",
        }
    )

    def walk(x: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k)
                if lk in keys and v is not None:
                    s = str(v).strip()
                    if s and _STUDY_ID_RE.match(s.upper().replace(" ", "")):
                        # normalise B/c prefix
                        su = s.upper()
                        if su[0] in "BC":
                            out.add(su[0] + su[1:])
                walk(v, depth + 1)
        elif isinstance(x, list):
            for it in x[:200]:
                walk(it, depth + 1)

    walk(obj, 0)
    return out


def ids_from_json_file(path: Path) -> Set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    out: Set[str] = set()
    if not isinstance(data, dict):
        return out
    for k in ("study_id", "study_folder"):
        v = data.get(k)
        if v is not None:
            s = str(v).strip()
            if s:
                out.add(s.upper() if len(s) == 8 and s[0].upper() in "BC" else s)
    meta = data.get("metadata")
    if isinstance(meta, dict):
        for k in ("study_id", "protocol_id"):
            v = meta.get(k)
            if v is not None:
                s = str(v).strip()
                if s:
                    out.add(s.upper() if len(s) == 8 and s[0].upper() in "BC" else s)
    so = data.get("study_overview")
    if isinstance(so, dict):
        v = so.get("study_id") or so.get("protocol_id")
        if v is not None:
            s = str(v).strip()
            if s:
                out.add(s.upper() if len(s) == 8 and s[0].upper() in "BC" else s)
    out |= _collect_ids_from_obj(data, max_depth=5)
    return {x for x in out if _STUDY_ID_RE.match(str(x).upper()) or len(str(x)) >= 6}


def resolve_bundle_files(
    bundle_dir: Path,
    study_id: str,
    manifest: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"pipd": None, "risk_profile": None, "cmp": None, "dmp": None, "usdm": []}

    def pick(patterns: List[str]) -> Optional[Path]:
        for pat in patterns:
            for p in sorted(bundle_dir.glob(pat)):
                if p.is_file() and p.suffix.lower() == ".json":
                    return p
        return None

    if manifest and isinstance(manifest.get("files"), dict):
        files = manifest["files"]
        for key, rel in files.items():
            if not rel:
                continue
            p = bundle_dir / str(rel)
            if key == "pipd" and p.is_file():
                out["pipd"] = p
            elif key in ("risk_profile", "riskprofile") and p.is_file():
                out["risk_profile"] = p
            elif key == "cmp" and p.is_file():
                out["cmp"] = p
            elif key == "dmp" and p.is_file():
                out["dmp"] = p
            elif key == "usdm" and p.is_file():
                out["usdm"] = [p]
        # Allow list of usdm
        raw_usdm = files.get("usdm_files") or files.get("usdm_json")
        if isinstance(raw_usdm, list):
            out["usdm"] = [bundle_dir / x for x in raw_usdm if (bundle_dir / x).is_file()]
    else:
        sid = study_id
        out["pipd"] = pick([f"{sid}_PIPD.json", "*_PIPD.json"])
        out["risk_profile"] = pick([f"{sid}_RiskProfile.json", "*RiskProfile*.json"])
        out["cmp"] = pick([f"{sid}_CMP.json", "*_CMP.json", "*CMP.json"])
        out["dmp"] = pick([f"{sid}_DMP.json", "*_DMP.json"])
        out["usdm"] = [p for p in bundle_dir.glob("*USDM*.json") if p.is_file()]
        out["usdm"].extend([p for p in bundle_dir.glob("usdm*.json") if p.is_file()])
        # de-dupe
        seen = set()
        u = []
        for p in out["usdm"]:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                u.append(p)
        out["usdm"] = u

    return out


def resolve_primary_usdm(
    files: Dict[str, Any],
    override: Optional[Path],
) -> Optional[Path]:
    """Single USDM protocol JSON for PIPD (PIPD_USDM_JSON)."""
    if override is not None and override.is_file():
        return override.resolve()
    usdm_list = files.get("usdm") or []
    if not usdm_list:
        return None
    if len(usdm_list) > 1:
        names = ", ".join(p.name for p in usdm_list)
        print(
            f"[WARN] Multiple USDM JSON files in bundle ({names}); using first for PIPD_USDM_JSON.",
            file=sys.stderr,
        )
    p = usdm_list[0]
    return p.resolve() if p.is_file() else None


def validate_protocol_ids(
    study_id: str,
    files: Dict[str, Any],
    *,
    loose: bool,
) -> List[str]:
    """Return list of error messages (empty if OK)."""
    canon = study_id.strip().upper()
    errs: List[str] = []
    for label, path in [
        ("PIPD", files.get("pipd")),
        ("Risk Profile", files.get("risk_profile")),
        ("CMP", files.get("cmp")),
        ("DMP", files.get("dmp")),
    ]:
        if path is None:
            continue
        ids = ids_from_json_file(path)
        if not ids:
            errs.append(f"[{label}] Could not read protocol id from {path.name} — check JSON.")
            continue
        if canon not in {i.upper() for i in ids}:
            errs.append(
                f"[{label}] Protocol id mismatch: expected {canon}, found {sorted(ids)} in {path.name}"
            )
    for path in files.get("usdm") or []:
        ids = ids_from_json_file(path)
        if ids and canon not in {i.upper() for i in ids}:
            errs.append(f"[USDM] Expected {canon}, found {sorted(ids)} in {path.name}")
    if errs and loose:
        print("[WARN] --loose-id-check: continuing despite:", file=sys.stderr)
        for e in errs:
            print(f"  {e}", file=sys.stderr)
        return []
    return errs


def _is_protocol_bundle_dir(d: Path) -> bool:
    """True if ``d`` looks like a single-protocol input folder (study id name or contains *_PIPD.json)."""
    if not d.is_dir():
        return False
    name = d.name
    if name.startswith("_") or name.startswith("."):
        return False
    if infer_study_id(d):
        return True
    if any(d.glob("*_PIPD.json")):
        return True
    return any(d.glob("*_DMP.json"))


def discover_bundle_dirs(parent: Path) -> List[Path]:
    """Sorted list of immediate subdirectories that qualify as protocol bundles."""
    return sorted(p for p in parent.iterdir() if _is_protocol_bundle_dir(p))


def run_one_bundle(bundle_dir: Path, args: argparse.Namespace, *, multi_bundle: bool) -> int:
    """
    Run PIPD / Risk / CMP evals for one bundle folder.

    Writes under ``bundle_dir/eval_outputs/<study_id>/`` with stems
    ``pipd_eval_*``, ``risk_profile_eval_*``, ``cmp_eval_*``.
    """
    repos = _repo_paths()
    if not bundle_dir.is_dir():
        print(f"[ERROR] Not a directory: {bundle_dir}", file=sys.stderr)
        return 2

    study_id = (
        infer_study_id(bundle_dir)
        if multi_bundle
        else (args.study_id or infer_study_id(bundle_dir))
    )
    if not study_id:
        print(
            "[ERROR] Could not determine study id — rename folder to e.g. B7981027 or pass --study-id",
            file=sys.stderr,
        )
        return 2
    study_id = study_id.strip().upper()

    manifest = load_manifest(bundle_dir)
    files = resolve_bundle_files(bundle_dir, study_id, manifest)

    id_errs = validate_protocol_ids(study_id, files, loose=bool(args.loose_id_check))
    if id_errs:
        for e in id_errs:
            print(e, file=sys.stderr)
        return 1

    out_root = bundle_dir / "eval_outputs"
    proto_out = out_root / study_id
    proto_out.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    ppid = repos["pipd_eval"]
    risk = repos["risk_profile_eval"]
    cmd = repos["cmp_eval"]
    dmp = repos["dmp_eval"]

    pipd_gt = args.pipd_ground_truth or (ppid / "data" / "pipd_ground_truth.csv")
    _bench_clean = ppid / "data" / "deviation_subcategories_clean.csv"
    _bench_raw = ppid / "data" / "deviation_subcategories.csv"
    pipd_bench = args.pipd_deviation_benchmarks or (
        _bench_clean if _bench_clean.is_file() else _bench_raw
    )
    risk_gt_r = args.risk_ground_truth_risks or (risk / "data" / "risk_profile_ground_truth.csv")
    risk_gt_f = args.risk_ground_truth_factors or (risk / "data" / "critical_factors_ground_truth.csv")
    cmp_cfg = args.cmp_config or (cmd / "config" / "cmp_eval_config.yaml")
    dmp_cfg = args.dmp_config or (dmp / "eval_config" / "dmp_eval_config.yaml")
    dmp_gt_json = args.dmp_ground_truth or (dmp / "data" / "dmp_ground_truth_clean.json")
    dmp_sds_csv = args.dmp_sds_ground_truth or (dmp / "data" / "sds_non_crf_ground_truth_clean.csv")

    plan: List[Tuple[str, List[str], Path]] = []
    stem_pipd = f"pipd_eval_{study_id}"
    stem_risk = f"risk_profile_eval_{study_id}"
    stem_cmp = f"cmp_eval_{study_id}"
    stem_dmp = f"dmp_eval_{study_id}"

    if not args.skip_pipd and files.get("pipd"):
        out_json = proto_out / f"{stem_pipd}.json"
        argv = [
            py,
            str(ppid / "scripts" / "run_eval.py"),
            "--generator_json",
            str(files["pipd"]),
            "--ground_truth",
            str(pipd_gt),
            "--deviation_benchmarks",
            str(pipd_bench),
            "--study_id",
            study_id,
            "--output",
            str(out_json),
            "--output_dir",
            str(proto_out),
            "--artifact_stem",
            stem_pipd,
        ]
        usdm_primary = resolve_primary_usdm(files, None)
        if usdm_primary:
            argv.extend(["--usdm_json", str(usdm_primary)])
        if not getattr(args, "allow_pipd_scenario2", False):
            argv.append("--scenario1-only")
        plan.append(("PIPD", argv, ppid))

    if not args.skip_risk and files.get("risk_profile"):
        argv = [
            py,
            str(risk / "scripts" / "run_eval.py"),
            "--generator_json",
            str(files["risk_profile"]),
            "--ground_truth_risks",
            str(risk_gt_r),
            "--ground_truth_factors",
            str(risk_gt_f),
            "--study_id",
            study_id,
            "--output_dir",
            str(proto_out),
            "--artifact-stem",
            stem_risk,
            "--force-scenario1-when-no-gt",
        ]
        plan.append(("Risk Profile", argv, risk))

    if not args.skip_cmp and files.get("cmp"):
        argv = [
            py,
            str(cmd / "core" / "eval_d3_cmp.py"),
            "--input",
            str(files["cmp"]),
            "--study-id",
            study_id,
            "--config",
            str(cmp_cfg),
            "--kri-gt",
            str(cmd / "data" / "cmp_kri_ground_truth.csv"),
            "--qtl-gt",
            str(cmd / "data" / "cmp_qtl_ground_truth.csv"),
            "--study-meta",
            str(cmd / "data" / "cmp_study_metadata.csv"),
            "--output-dir",
            str(proto_out),
            "--artifact-stem",
            stem_cmp,
        ]
        plan.append(("CMP", argv, cmd))

    if not args.skip_dmp and files.get("dmp") and dmp.is_dir():
        out_json = proto_out / f"{stem_dmp}.json"
        argv = [
            py,
            str(dmp / "core" / "eval_d4_dmp.py"),
            "--input",
            str(files["dmp"]),
            "--study-id",
            study_id,
            "--config",
            str(dmp_cfg),
            "--dmp-gt",
            str(dmp_gt_json),
            "--sds-gt",
            str(dmp_sds_csv),
            "--output-json",
            str(out_json),
            "--output-dir",
            str(proto_out),
            "--artifact-stem",
            stem_dmp,
        ]
        plan.append(("DMP", argv, dmp))

    print(f"[INFO] Bundle: {bundle_dir}")
    print(f"[INFO] Study id: {study_id}")
    print(f"[INFO] Output dir: {proto_out}")
    print(f"[INFO] Detected files:")
    for k in ("pipd", "risk_profile", "cmp", "dmp"):
        print(f"       {k}: {files.get(k)}")
    print(f"       usdm (reference only): {files.get('usdm')}")

    if not plan:
        print("[WARN] Nothing to run — place JSONs in bundle or relax --skip-* flags")
        return 0

    if args.dry_run:
        for name, argv, cwd in plan:
            print(f"\n--- {name} (dry-run) ---\ncwd={cwd}\n" + " ".join(argv))
        return 0

    rc_all = 0
    strict = bool(getattr(args, "fail_on_metric_fail", False))
    for name, argv, cwd in plan:
        print(f"\n======== {name} ========")
        rc = run_cmd(argv, cwd)
        if rc != 0:
            if strict:
                print(f"[ERROR] {name} exited {rc}", file=sys.stderr)
                rc_all = rc
            else:
                print(
                    f"[INFO] {name} subprocess exit {rc} — scores may be NO-GO/FAIL; "
                    f"check eval_outputs/ for JSON/YAML/Word (normal when inputs are not gold-standard).",
                    file=sys.stderr,
                )
    return rc_all


def run_cmd(argv: List[str], cwd: Path, extra_env: Optional[Dict[str, str]] = None) -> int:
    print(f"[EXEC] {' '.join(argv)}")
    if extra_env:
        for k, v in extra_env.items():
            print(f"[ENV] {k}={v}")
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(argv, cwd=str(cwd), env=env)
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Run PIPD / Risk / CMP evals from one or more protocol bundle folders")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--bundle", type=Path, default=None, help="Single folder with per-protocol generator JSONs")
    src.add_argument(
        "--bundles-parent",
        "--all-protocols",
        type=Path,
        default=None,
        dest="bundles_parent",
        metavar="PARENT_DIR",
        help="Eval every protocol: run each qualifying subfolder under this path "
        "(folder name like B7981027 / C4891023, or any subfolder containing *_PIPD.json). "
        "Skips names starting with '_' (e.g. _template). --study-id is ignored. "
        "Synonym: --all-protocols.",
    )
    ap.add_argument("--study-id", default=None, help="Protocol id for --bundle only (default: folder name)")
    ap.add_argument("--dry-run", action="store_true", help="Print plan only")
    ap.add_argument(
        "--loose-id-check",
        action="store_true",
        help="Warn on id mismatch but do not fail before eval",
    )
    ap.add_argument("--skip-pipd", action="store_true")
    ap.add_argument("--skip-risk", action="store_true")
    ap.add_argument("--skip-cmp", action="store_true")
    ap.add_argument("--skip-dmp", action="store_true", help="Skip D4 DMP eval")
    ap.add_argument(
        "--allow-pipd-scenario2",
        action="store_true",
        help="Run PIPD Scenario 2 when the study is not in verify ground truth. "
        "Default: pass --scenario1-only to pipd_eval/scripts/run_eval.py (skip PIPD for those studies).",
    )
    ap.add_argument("--pipd-ground-truth", type=Path, default=None)
    ap.add_argument("--pipd-deviation-benchmarks", type=Path, default=None)
    ap.add_argument("--risk-ground-truth-risks", type=Path, default=None)
    ap.add_argument("--risk-ground-truth-factors", type=Path, default=None)
    ap.add_argument("--cmp-config", type=Path, default=None)
    ap.add_argument("--dmp-config", type=Path, default=None, help="dmp_eval_config.yaml")
    ap.add_argument("--dmp-ground-truth", type=Path, default=None, help="dmp_ground_truth_clean.json")
    ap.add_argument("--dmp-sds-ground-truth", type=Path, default=None, help="sds_non_crf_ground_truth_clean.csv")
    ap.add_argument(
        "--fail-on-metric-fail",
        action="store_true",
        help="Exit non-zero if any child eval returns non-zero (metric NO-GO/FAIL). "
        "Default: exit 0 when all eval steps ran — scoring outcome is informational, not a runner failure.",
    )

    args = ap.parse_args()

    if args.bundles_parent is not None:
        parent = args.bundles_parent.resolve()
        if not parent.is_dir():
            print(f"[ERROR] Not a directory: {parent}", file=sys.stderr)
            return 2
        bundle_dirs = discover_bundle_dirs(parent)
        if not bundle_dirs:
            print(f"[WARN] No protocol bundle subfolders under {parent}", file=sys.stderr)
            return 0
        rc_final = 0
        for bd in bundle_dirs:
            print(f"\n{'#' * 60}\n# Protocol bundle: {bd.name}\n{'#' * 60}")
            rc = run_one_bundle(bd, args, multi_bundle=True)
            if rc != 0:
                rc_final = rc
        return rc_final

    if args.bundle is None:
        print("[ERROR] Internal: --bundle missing", file=sys.stderr)
        return 2
    return run_one_bundle(args.bundle.resolve(), args, multi_bundle=False)


if __name__ == "__main__":
    raise SystemExit(main())
