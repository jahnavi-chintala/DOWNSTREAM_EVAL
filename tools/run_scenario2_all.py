#!/usr/bin/env python3
"""
Run Scenario 2 evaluation across Risk Profile, PIPD, CMP, and DMP.

Resolves artefact paths from ``--inputs-root`` (recommended) or explicit
paths. Each evaluator loads with an isolated ``sys.modules`` purge so shared
names like ``core.eval_scenario2`` never clash between products.

Example::

    python tools/run_scenario2_all.py --study-id C5091017 \\
        --inputs-root "C:\\Users\\me\\Downloads\\new_C509017"
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Iterator, Optional


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _purge_shared_eval_modules() -> None:
    for key in list(sys.modules.keys()):
        if (
            key == "core"
            or key.startswith("core.")
            or key == "utils"
            or key.startswith("utils.")
            or key.startswith("reports.")
            or key.startswith("scripts.")
        ):
            sys.modules.pop(key, None)


@contextlib.contextmanager
def _pkg_on_syspath(pkg_root: Path) -> Iterator[None]:
    root_str = str(pkg_root.resolve())
    try:
        sys.path.insert(0, root_str)
        yield
    finally:
        try:
            sys.path.remove(root_str)
        except ValueError:
            pass


def _import_in_pkg(pkg_root: Path, module: str):
    """Import ``module`` (dotted) with ``pkg_root`` first on ``sys.path``."""
    with _pkg_on_syspath(pkg_root):
        return importlib.import_module(module)


def _first_existing(candidates: list[Optional[Path]]) -> Optional[Path]:
    for p in candidates:
        if p is None:
            continue
        if p.is_file():
            return p
    return None


def _resolve_inputs(inputs_root: Path, study_id: str) -> dict[str, Path]:
    ir = inputs_root.resolve()
    recursive_usdm = [p for p in ir.rglob(f"*{study_id}*USDM*.json")][:5]
    usdm = _first_existing(
        [
            ir / f"USDM_{study_id}_final.json",
            *[p for p in ir.glob(f"USDM*{study_id}*.json")],
            *recursive_usdm,
        ]
    )
    if usdm is None:
        raise FileNotFoundError(
            f"Could not find USDM JSON under {ir} for study {study_id}. "
            "Pass --usdm-json explicitly."
        )
    rp = ir / "RISK_PROFILE" / f"{study_id}_RiskProfile.json"
    pipd = ir / "PIPD" / f"{study_id}_PIPD.json"
    cmpj = ir / "CMP" / f"{study_id}_CMP.json"
    dmp = ir / "DMP" / f"{study_id}_DMP.json"
    out = {"usdm": usdm}
    missing = []
    for key, p in ("rp", rp), ("pipd", pipd), ("cmp", cmpj), ("dmp", dmp):
        if p.is_file():
            out[key] = p
        else:
            missing.append(str(p))
    if missing:
        raise FileNotFoundError(
            "Missing expected generator JSON paths:\n"
            + "\n".join(missing)
            + "\nPass explicit --*-json overrides if your layout differs."
        )
    return out


def save_json(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
    shutil.move(str(tmp), str(path))
    print(f"  [OK] {path}")


def run_rp(base: Path, study_id: str, paths: dict[str, Path], out_root: Path) -> None:
    print("\n=== Risk Profile - Scenario 2 ===")
    pkg = base / "risk_profile_eval"
    _purge_shared_eval_modules()
    m = _import_in_pkg(pkg, "core.eval_scenario2")
    results = m.run_scenario2_eval(str(paths["rp"]), study_id)
    out_dir = out_root / "RISK_PROFILE"
    save_json(results, out_dir / f"risk_profile_eval_s2_{study_id}.json")
    try:
        _purge_shared_eval_modules()
        rep_mod = _import_in_pkg(pkg, "reports.rp_scenario2_report")
        payload = rep_mod.build_scenario2_payload(
            results, str(paths["rp"]), None, None, study_id, usdm_json_path=str(paths["usdm"])
        )
        docx_path = str(out_dir / f"Risk_Profile_Eval_S2_{study_id}.docx")
        rep_mod.write_scenario2_report(payload, docx_path)
        print(f"  [OK] Word report: {docx_path}")
    except Exception as exc:  # noqa: BLE001 — batch helper
        print(f"  [WARN] Word report skipped: {exc}")
    finally:
        _purge_shared_eval_modules()


def run_pipd(base: Path, study_id: str, paths: dict[str, Path], benchmarks: Path, out_root: Path) -> None:
    print("\n=== PIPD - Scenario 2 ===")
    pkg = base / "pipd_eval"
    _purge_shared_eval_modules()
    m = _import_in_pkg(pkg, "core.eval_scenario2")
    results = m.run_scenario2_eval(str(paths["pipd"]), str(benchmarks), study_id, usdm_json_path=str(paths["usdm"]))
    out_dir = out_root / "PIPD"
    save_json(results, out_dir / f"pipd_eval_s2_{study_id}.json")
    try:
        _purge_shared_eval_modules()
        rep = _import_in_pkg(pkg, "reports.pipd_scenario2_report")
        payload = rep.build_scenario2_report_payload(
            results, str(paths["pipd"]), str(benchmarks), study_id, usdm_json_path=str(paths["usdm"])
        )
        extra = rep.write_scenario2_yaml_and_word(
            payload,
            str(out_dir),
            study_id,
            artifact_stem=f"pipd_eval_s2_{study_id}",
            write_yaml=True,
            write_docx=True,
        )
        for k, v in extra.items():
            print(f"  [INFO] {k}: {v}")
    except Exception:
        print("  [WARN] YAML/Word report skipped")
        traceback.print_exc()
    finally:
        _purge_shared_eval_modules()


def run_cmp(base: Path, study_id: str, paths: dict[str, Path], out_root: Path) -> None:
    print("\n=== CMP - Scenario 2 ===")
    pkg = base / "cmp_eval"
    os.environ["CMP_USDM_JSON_PATH"] = str(paths["usdm"])
    raw = json.loads(paths["cmp"].read_text(encoding="utf-8"))
    try:
        _purge_shared_eval_modules()
        m = _import_in_pkg(pkg, "core.eval_scenario2")
        results = m.run_scenario2_eval(raw, study_id)
        out_dir = out_root / "CMP"
        save_json(results, out_dir / f"cmp_eval_s2_{study_id}.json")
        _purge_shared_eval_modules()
        rep_mod = _import_in_pkg(pkg, "reports.cmp_scenario2_report")
        docx_path = str(out_dir / f"CMP_Eval_Report_S2_{study_id}.docx")
        out_path = rep_mod.write_cmp_scenario2_docx(results, docx_path)
        print(f"  [OK] Word report: {out_path}")
    finally:
        os.environ.pop("CMP_USDM_JSON_PATH", None)
        _purge_shared_eval_modules()


def run_dmp(base: Path, study_id: str, paths: dict[str, Path], out_root: Path) -> None:
    print("\n=== DMP - Scenario 2 ===")
    pkg = base / "dmp_eval"
    os.environ["DMP_USDM_JSON_PATH"] = str(paths["usdm"])
    raw = json.loads(paths["dmp"].read_text(encoding="utf-8"))
    try:
        _purge_shared_eval_modules()
        m = _import_in_pkg(pkg, "core.eval_scenario2")
        results = m.run_scenario2_eval(raw, study_id)
        out_dir = out_root / "DMP"
        save_json(results, out_dir / f"dmp_eval_s2_{study_id}.json")
        _purge_shared_eval_modules()
        rep_mod = _import_in_pkg(pkg, "reports.dmp_scenario2_report")
        docx_path = str(out_dir / f"DMP_Eval_Report_S2_{study_id}.docx")
        out_path = rep_mod.write_dmp_scenario2_docx(results, docx_path)
        print(f"  [OK] Word report: {out_path}")
    finally:
        os.environ.pop("DMP_USDM_JSON_PATH", None)
        _purge_shared_eval_modules()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Run Scenario 2 evaluators for all four deliverables.")
    p.add_argument("--study-id", default="C5091017", help="Protocol / study identifier (matches JSON stem).")
    p.add_argument(
        "--base",
        type=Path,
        default=None,
        help="Repo root containing *eval folders (defaults to PFIZER_ROOT or parent of tools/).",
    )
    p.add_argument(
        "--inputs-root",
        type=Path,
        default=None,
        help=(
            "Folder with USDM + RISK_PROFILE/ PIPD/ CMP/ DMP/ subfolders "
            '(see README). If omitted, pass explicit "--*-json" paths.'
        ),
    )
    p.add_argument(
        "--out-root",
        type=Path,
        default=None,
        help="Where to write outputs (default: <base>/scenario2_<study-id>).",
    )
    p.add_argument("--benchmarks", type=Path, default=None, help="PIPD deviation_subcategories CSV (optional).")
    p.add_argument("--usdm-json", type=Path, default=None)
    p.add_argument("--rp-json", type=Path, default=None)
    p.add_argument("--pipd-json", type=Path, default=None)
    p.add_argument("--cmp-json", type=Path, default=None)
    p.add_argument("--dmp-json", type=Path, default=None)
    args = p.parse_args(argv)

    if args.base is not None:
        base = args.base.resolve()
    else:
        base_env = os.environ.get("PFIZER_ROOT", "").strip()
        base = Path(base_env).resolve() if base_env else _repo_root()

    if args.benchmarks:
        benchmarks = Path(args.benchmarks).resolve()
    else:
        benchmarks = (
            base / "pipd_eval" / "data" / "deviation_subcategories.csv"
        ).resolve()
        if not benchmarks.is_file():
            alt = (
                base / "pipd_eval" / "data" / "deviation_subcategories_clean.csv"
            ).resolve()
            if alt.is_file():
                benchmarks = alt

    resolved: dict[str, Path]
    if args.inputs_root is not None:
        ir = Path(args.inputs_root).resolve()
        resolved = _resolve_inputs(ir, args.study_id)
        if args.usdm_json is not None:
            resolved["usdm"] = Path(args.usdm_json).resolve()
        for key, cli in (
            ("rp", args.rp_json),
            ("pipd", args.pipd_json),
            ("cmp", args.cmp_json),
            ("dmp", args.dmp_json),
        ):
            if cli is not None:
                resolved[key] = Path(cli).resolve()
    else:
        if args.usdm_json is None:
            raise SystemExit(
                "--inputs-root is required unless you pass explicit JSON paths "
                "(--usdm-json, --rp-json, --pipd-json, --cmp-json, --dmp-json)."
            )
        resolved = {"usdm": Path(args.usdm_json).resolve()}
        for key, cli in (
            ("rp", args.rp_json),
            ("pipd", args.pipd_json),
            ("cmp", args.cmp_json),
            ("dmp", args.dmp_json),
        ):
            if cli is None:
                raise SystemExit(
                    "When --inputs-root is omitted, supply "
                    "--rp-json --pipd-json --cmp-json --dmp-json alongside --usdm-json."
                )
            resolved[key] = Path(cli).resolve()

    out_root = (
        Path(args.out_root).resolve()
        if args.out_root is not None
        else base / f"scenario2_{args.study_id}"
    )

    errors: list[tuple[str, BaseException]] = []
    runners = (
        ("Risk Profile", lambda: run_rp(base, args.study_id, resolved, out_root)),
        ("PIPD", lambda: run_pipd(base, args.study_id, resolved, benchmarks, out_root)),
        ("CMP", lambda: run_cmp(base, args.study_id, resolved, out_root)),
        ("DMP", lambda: run_dmp(base, args.study_id, resolved, out_root)),
    )
    for label, fn in runners:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append((label, exc))
            print(f"\n[ERROR] {label} failed: {exc}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    if errors:
        print(f"Completed with {len(errors)} error(s):")
        for name, exc in errors:
            print(f"  {name}: {exc}")
    else:
        print("All 4 Scenario 2 evals completed successfully.")
    print(f"Output folder: {out_root}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
