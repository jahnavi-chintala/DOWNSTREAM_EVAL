import type { ProductDef, ProductKey } from "./types";

export const PRODUCTS: Record<ProductKey, ProductDef> = {
  risk: {
    key: "risk",
    label: "Risk Profile",
    prefix: "/risk",
    tagline: "Generator-side risk register validation.",
    description:
      "Compares the generator's risk register against the ground-truth risks and critical factors for the study. Reports recall, RPN tier agreement, critical-factor overlap and provenance defects.",
    metrics: "M1 recall · M2 RPN tier · M3 critical factors · M4 provenance",
    fields: [
      {
        name: "usdm",
        label: "USDM JSON",
        kind: "json",
        required: true,
        help: "Source-of-truth protocol in USDM JSON form. Study id must match the generator file.",
      },
      {
        name: "gen",
        label: "Generator (Risk Profile) JSON",
        kind: "json",
        required: true,
        help: "Output from the Risk Profile generator for the same study.",
      },
      {
        name: "risks",
        label: "risk_profile_ground_truth.csv",
        kind: "csv",
        required: false,
        help: "Auto-loaded from risk_profile_eval/data/ on the server. Upload only to override.",
      },
      {
        name: "factors",
        label: "critical_factors_ground_truth.csv",
        kind: "csv",
        required: false,
        help: "Auto-loaded from risk_profile_eval/data/ on the server. Upload only to override.",
      },
    ],
  },
  pipd: {
    key: "pipd",
    label: "PIPD",
    prefix: "/pipd",
    tagline: "Potential Important Protocol Deviations (PIPD).",
    description:
      "PIPD is the Potential Important Protocol Deviations deliverable. Evaluates the generator’s deviation register vs ground truth: M1–M6 metrics, signal verdicts (scenario 2), near-miss/semantic review, and stakeholder-ready reports.",
    metrics: "M1–M6 · signals · remediation notes",
    fields: [
      {
        name: "usdm",
        label: "USDM JSON",
        kind: "json",
        required: true,
      },
      {
        name: "gen",
        label: "PIPD generator JSON",
        kind: "json",
        required: true,
      },
      {
        name: "gt",
        label: "pipd_ground_truth CSV",
        kind: "csv",
        required: false,
        help: "Auto-loaded from ppid_py/data/ on the server. Upload only to override.",
      },
      {
        name: "dev",
        label: "deviation_subcategories CSV",
        kind: "csv",
        required: false,
        help: "Auto-loaded from ppid_py/data/ on the server. Upload only to override.",
      },
    ],
  },
  cmp: {
    key: "cmp",
    label: "CMP",
    prefix: "/cmp",
    tagline: "Central monitoring plan evaluation.",
    description:
      "Checks the CMP generator output against study-level KRIs, QTLs and metadata. Reports section-level scores and concrete improvement actions.",
    metrics: "M1–M4 · section scores · doc score",
    fields: [
      { name: "usdm", label: "USDM JSON", kind: "json", required: true },
      { name: "gen", label: "CMP JSON", kind: "json", required: true },
      {
        name: "kri",
        label: "KRI ground truth CSV",
        kind: "csv",
        required: false,
        help: "Auto-loaded from cmd_py/Data/ on the server. Upload only to override.",
      },
      {
        name: "qtl",
        label: "QTL ground truth CSV",
        kind: "csv",
        required: false,
        help: "Auto-loaded from cmd_py/Data/ on the server. Upload only to override.",
      },
      {
        name: "meta",
        label: "Study metadata CSV",
        kind: "csv",
        required: false,
        help: "Auto-loaded from cmd_py/Data/ on the server. Optional even then.",
      },
    ],
  },
  dmp: {
    key: "dmp",
    label: "DMP",
    prefix: "/dmp",
    tagline: "Data management plan evaluation.",
    description:
      "Hallucination detection and section scoring for DMP artifacts, with TA/phase-aware expectations and targeted improvement actions.",
    metrics: "M1–M4 · hallucinations · section scores",
    fields: [
      { name: "usdm", label: "USDM JSON", kind: "json", required: true },
      { name: "gen", label: "DMP JSON", kind: "json", required: true },
      {
        name: "dmpgt",
        label: "dmp_ground_truth JSON",
        kind: "json",
        required: false,
        help: "Auto-loaded from DMP_py/data/ on the server. Upload only to override.",
      },
      {
        name: "sds",
        label: "sds_non_crf CSV",
        kind: "csv",
        required: false,
        help: "Auto-loaded from DMP_py/data/ on the server. Upload only to override.",
      },
    ],
  },
};

export const PRODUCT_ORDER: ProductKey[] = ["risk", "pipd", "cmp", "dmp"];
