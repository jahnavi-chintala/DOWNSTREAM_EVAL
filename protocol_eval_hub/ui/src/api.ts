import type { EvalResponse, ProductDef, ProductKey } from "./types";

export async function runEval(
  product: ProductDef,
  files: Record<string, File | null>
): Promise<EvalResponse> {
  const fd = new FormData();
  for (const f of product.fields) {
    const file = files[f.name];
    if (file) fd.append(f.name, file);
    else if (f.required) {
      throw new Error(`Missing required file: ${f.label}`);
    }
  }
  const res = await fetch(`${product.prefix}/eval/upload-session`, {
    method: "POST",
    body: fd,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail =
      (body && (body.detail || body.error)) ||
      `HTTP ${res.status} ${res.statusText}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return {
    ...body,
    product: product.key as ProductKey,
    ran_at: new Date().toISOString(),
  };
}

export async function fetchJsonArtifact(url: string): Promise<unknown> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`JSON HTTP ${r.status}`);
  return r.json();
}

export async function fetchTextArtifact(url: string): Promise<string> {
  const r = await fetch(url);
  if (!r.ok) {
    const err = new Error(`HTTP ${r.status}`);
    (err as unknown as { status: number }).status = r.status;
    throw err;
  }
  return r.text();
}

export async function fetchDocxHtml(
  productPrefix: string,
  token: string
): Promise<string> {
  const url = `${productPrefix}/eval/session/${encodeURIComponent(token)}/preview/docx-html`;
  const r = await fetch(url);
  if (!r.ok) {
    const err = new Error(`DOCX HTML HTTP ${r.status}`);
    (err as unknown as { status: number }).status = r.status;
    throw err;
  }
  return r.text();
}

export async function fetchDocxArrayBuffer(url: string): Promise<ArrayBuffer> {
  const r = await fetch(url);
  if (!r.ok) {
    const err = new Error(`DOCX HTTP ${r.status}`);
    (err as unknown as { status: number }).status = r.status;
    throw err;
  }
  return r.arrayBuffer();
}
