import { useMemo } from "react";
import clsx from "clsx";
import type { ProductDef } from "../types";

interface Props {
  product: ProductDef;
  files: Record<string, File | null>;
  onFileChange: (name: string, file: File | null) => void;
  onRun: () => void;
  running: boolean;
  canRun: boolean;
  error: string | null;
}

function humanSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function DropField({
  name,
  label,
  required,
  help,
  accept,
  file,
  onChange,
}: {
  name: string;
  label: string;
  required: boolean;
  help?: string;
  accept: string;
  file: File | null;
  onChange: (f: File | null) => void;
}) {
  return (
    <div className="file-field">
      <label htmlFor={`f-${name}`} className="label">
        {label}
        {required && <span className="req">*</span>}
      </label>
      <div className={clsx("drop", file && "has")}>
        <span className="icon" aria-hidden>
          {file ? "✓" : "+"}
        </span>
        <div className="info">
          <div className="name">{file ? file.name : "Choose file or drop here"}</div>
          <div className="size">
            {file ? humanSize(file.size) : accept.replace(/,/g, " / ")}
          </div>
        </div>
        <input
          id={`f-${name}`}
          type="file"
          accept={accept}
          onChange={(e) => onChange(e.target.files?.[0] ?? null)}
        />
        {file && (
          <button
            type="button"
            className="clear"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onChange(null);
            }}
            aria-label={`Clear ${label}`}
          >
            remove
          </button>
        )}
      </div>
      {help && <div className="help">{help}</div>}
    </div>
  );
}

export function UploadPanel({
  product,
  files,
  onFileChange,
  onRun,
  running,
  canRun,
  error,
}: Props) {
  const required = product.fields.filter((f) => f.required);
  const optional = product.fields.filter((f) => !f.required);

  const accept = useMemo(
    () => ({
      json: ".json,application/json",
      csv: ".csv,text/csv",
      any: "*/*",
    }),
    []
  );

  return (
    <section className="panel">
      <div className="panel-header">
        <h3>Intake</h3>
        <span className="hint">{product.metrics}</span>
      </div>
      <div className="panel-body">
        <div className="section-sub">Required inputs</div>
        <div className="upload-grid">
          {required.map((f) => (
            <DropField
              key={f.name}
              name={f.name}
              label={f.label}
              required
              help={f.help}
              accept={accept[f.kind]}
              file={files[f.name] ?? null}
              onChange={(file) => onFileChange(f.name, file)}
            />
          ))}
        </div>

        {optional.length > 0 && (
          <>
            <div className="section-sub">Optional overrides</div>
            <div className="upload-grid">
              {optional.map((f) => (
                <DropField
                  key={f.name}
                  name={f.name}
                  label={f.label}
                  required={false}
                  help={f.help}
                  accept={accept[f.kind]}
                  file={files[f.name] ?? null}
                  onChange={(file) => onFileChange(f.name, file)}
                />
              ))}
            </div>
          </>
        )}

        <div className="run-bar">
          <div className="run-hint">
            Upload files stay on the server only for this session. Study id is validated
            across the USDM and generator JSON before the eval runs.
          </div>
          <button
            type="button"
            className="btn primary"
            onClick={onRun}
            disabled={running || !canRun}
          >
            {running && <span className="spinner" aria-hidden />}
            {running ? "Running evaluation…" : "Run evaluation"}
          </button>
        </div>

        {running && (
          <div className="banner working">
            <span className="spinner" aria-hidden />
            Evaluating — this can take a minute for larger protocols.
          </div>
        )}
        {error && <div className="banner err">{error}</div>}
      </div>
    </section>
  );
}
