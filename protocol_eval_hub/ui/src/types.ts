export type ProductKey = "risk" | "pipd" | "cmp" | "dmp";

export interface MetricRow {
  metric: string;
  detail: string;
  pass: boolean | null;
  tooltip?: string;
  hero?: boolean;
  na?: boolean;
}

export interface PreviewHeadline {
  verdict?: string | null;
  ta?: string | null;
  therapeutic_area?: string | null;
  phase?: string | null;
  document_score?: string | number | null;
  overall_score_percent?: number | null;
  rag_traffic_light?: string | null;
  [k: string]: unknown;
}

export type TracingStatus =
  | "id_match"
  | "name_match"
  | "type_only"
  | "unresolved"
  | "no_usdm";

export interface TracingCandidate {
  id: string;
  name: string;
}

export interface TracingRow {
  field_path?: string;
  entity?: string;
  signal?: string;
  usdm_id?: string;
  status: TracingStatus;
  reason?: string;
  matched_node_id?: string | null;
  candidates?: TracingCandidate[];
}

export interface TracingBlock {
  usdm_loaded: boolean;
  usdm_node_count: number;
  usdm_instance_types?: string[];
  status_counts: Record<TracingStatus, number>;
  rows: TracingRow[];
  truncated?: boolean;
}

/** PIPD: M1 near-miss pair (algorithmic numbering / truncation / paraphrase tiers). */
export interface PipdAlgoNearMissPair {
  gt_text?: string | null;
  generated_text?: string | null;
  credit?: number | null;
  root_cause?: string | null;
  tier?: string | null;
  distance?: number | null;
  source: "algorithmic";
}

/** PIPD: LLM semantic-review pairing (optional). */
export interface PipdSemanticNearMissPair {
  gt_text?: string | null;
  generated_text?: string | null;
  credit?: number | null;
  verdict?: string | null;
  reason?: string;
  source: "semantic_review";
}

export interface PipdNearMissCategoryBlock {
  category_num: number;
  category_name: string;
  algorithmic_near_misses: PipdAlgoNearMissPair[];
  semantic_review_pairs: PipdSemanticNearMissPair[];
  total_pairs: number;
}

export interface EvalPreview {
  product?: string;
  scenario?: number;
  headline?: PreviewHeadline;
  metric_rows?: MetricRow[];
  counts?: Record<string, unknown>;
  failure_notes?: string[];
  doc_hint?: string;
  section_scores?: Array<{ section: string; score?: number | string; pass?: boolean }>;
  improvement_actions?: string[];
  tracing?: TracingBlock;
  /** PIPD scenario 1: GT vs generated near-miss rows for all 11 categories. */
  near_misses_by_category?: PipdNearMissCategoryBlock[];
  [k: string]: unknown;
}

export interface EvalDownloads {
  zip: string;
  json: string;
  yaml: string;
  docx: string;
}

export interface EvalResponse {
  study_id: string;
  verdict: string;
  preview: EvalPreview | null;
  session_token: string;
  downloads: EvalDownloads;
  product: ProductKey;
  ran_at: string;
}

export interface RecentRun {
  product: ProductKey;
  study_id: string;
  verdict: string;
  session_token: string;
  downloads: EvalDownloads;
  ran_at: string;
  headline_score?: string | number | null;
}

export type FieldKind = "json" | "csv" | "any";

export interface FieldDef {
  name: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  help?: string;
}

export interface ProductDef {
  key: ProductKey;
  label: string;
  prefix: string;
  tagline: string;
  description: string;
  metrics: string;
  fields: FieldDef[];
}
