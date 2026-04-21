interface Props {
  notes: string[];
  tone?: "warn" | "fail";
}

export function NotesList({ notes, tone = "warn" }: Props) {
  if (!notes || notes.length === 0) return null;
  return (
    <div className="notes">
      {notes.map((n, i) => (
        <div key={i} className={`note ${tone === "fail" ? "fail" : ""}`}>
          <div className="ix">{String(i + 1).padStart(2, "0")}</div>
          <div className="body">{n}</div>
        </div>
      ))}
    </div>
  );
}
