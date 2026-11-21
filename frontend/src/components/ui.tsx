import type { MouseEvent, ReactNode } from "react";

export function Badge({ slot, children }: { slot?: string; children: ReactNode }) {
  return <span className={`badge ${slot || "gray"}`}>{children}</span>;
}

export function Dot({ slot, filled = true }: { slot?: string; filled?: boolean }) {
  return <span className={`dot ${slot || "gray"} ${filled ? "" : "hollow"}`} />;
}

export function Bar({ fraction }: { fraction?: number | null }) {
  const pct = Math.max(0, Math.min(100, Math.round(Number(fraction || 0) * 100)));
  return <div className="bar grow"><i style={{ width: `${pct}%` }} /></div>;
}

export function Btn({
  kind, onClick, children, disabled,
}: { kind?: "primary" | "danger" | "ghost"; onClick?: (e: MouseEvent<HTMLButtonElement>) => void; children: ReactNode; disabled?: boolean }) {
  return <button type="button" className={`btn ${kind || ""}`} onClick={onClick} disabled={disabled}>{children}</button>;
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return <div className="eyebrow">{children}</div>;
}

export function Chips({ chips }: { chips?: { label: string; slot?: string; filled?: boolean }[] }) {
  if (!chips?.length) return null;
  return (
    <>
      {chips.map((c, i) => <Badge key={i} slot={c.slot}>{c.label}</Badge>)}
    </>
  );
}
