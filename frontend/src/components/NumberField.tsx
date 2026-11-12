import { useEffect, useState } from "react";

interface FieldProps {
  value: number | null;
  onCommit: (value: number | null) => void;
  label: string;
  testid: string;
  min?: number;
  max?: number;
  integer?: boolean;
  placeholder?: string;
}

/** A number typed freely and applied on Enter or when the field is left: every value applied
 *  costs a computation on the server, the digits typed on the way must not. */
export function NumberField({ value, onCommit, label, testid, min, max, integer, placeholder }: FieldProps) {
  const [draft, setDraft] = useState(value === null ? "" : String(value));
  useEffect(() => setDraft(value === null ? "" : String(value)), [value]);
  const commit = () => {
    const text = draft.trim().replace(",", ".");
    let next: number | null = text === "" ? null : Number(text);
    if (next !== null) {
      if (!Number.isFinite(next) || next <= 0) next = value; // not a usable number: keep the previous one
      else {
        if (integer) next = Math.round(next);
        if (min !== undefined) next = Math.max(min, next);
        if (max !== undefined) next = Math.min(max, next);
      }
    }
    setDraft(next === null ? "" : String(next));
    if (next !== value) onCommit(next);
  };
  return (
    <input inputMode="decimal" value={draft} placeholder={placeholder} aria-label={label} data-testid={testid}
      onChange={(e) => setDraft(e.target.value)} onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && commit()} />
  );
}
