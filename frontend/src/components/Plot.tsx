import { useEffect, useRef } from "react";

export function Plot({ figure, height = 320 }: { figure: any; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let cancelled = false;
    const el = ref.current;
    if (!el || !figure) return;
    import("plotly.js-dist-min").then((mod) => {
      if (cancelled || !ref.current) return;
      const Plotly = (mod as any).default ?? mod;
      const layout = {
        ...(figure.layout || {}),
        height: figure.layout?.height ?? height,
        paper_bgcolor: "transparent",
        plot_bgcolor: "transparent",
        font: { color: getComputedStyle(document.body).color, family: "IBM Plex Sans, system-ui, sans-serif", size: 12 },
        margin: { l: 48, r: 16, t: 28, b: 40, ...(figure.layout?.margin || {}) },
      };
      Plotly.newPlot(ref.current, figure.data || [], layout, { displayModeBar: false, responsive: true });
    }).catch(() => {
      if (ref.current) ref.current.textContent = "This chart needs the frontend build (plotly).";
    });
    return () => { cancelled = true; };
  }, [figure, height]);
  return <div ref={ref} style={{ width: "100%", minHeight: height }} />;
}

export function Evidence({ data }: { data: any }) {
  if (!data) return <p className="muted">No results yet.</p>;
  if (data.error) return <p className="err">{data.error}</p>;
  return (
    <div className="stack">
      {(data.plotly || []).map((p: any, i: number) => (
        <div key={i}>{p.title ? <div className="eyebrow" style={{ marginBottom: 8 }}>{p.title}</div> : null}<Plot figure={p.figure} /></div>
      ))}
      {(data.captions || []).map((c: string, i: number) => <p key={i} className="muted small">{c}</p>)}
      {data.metrics?.length ? (
        <div className="facts">
          {data.metrics.map((m: any, i: number) => (
            <div key={i} title={m.help || ""}><b>{String(m.value)}</b><span>{m.label}</span></div>
          ))}
        </div>
      ) : null}
      {(data.tables || []).map((t: any, i: number) => <Rows key={i} title={t.title} rows={t.rows || []} />)}
      {(data.observations || []).map((obs: any, i: number) => (
        <div key={i} className="card">
          <div className="row"><b className="grow">{obs.title || `Field ${i + 1}`}</b></div>
          {obs.summary ? <div className="muted small" style={{ margin: "6px 0" }}>{obs.summary}</div> : null}
          <Evidence data={{ ...obs, observations: undefined }} />
        </div>
      ))}
    </div>
  );
}

function Rows({ title, rows }: { title?: string; rows: Record<string, unknown>[] }) {
  if (!rows.length) return null;
  const keys = Object.keys(rows[0]);
  return (
    <div>
      {title ? <div className="eyebrow" style={{ marginBottom: 8 }}>{title}</div> : null}
      <div className="tablewrap">
        <table>
          <thead><tr>{keys.map((k) => <th key={k}>{k}</th>)}</tr></thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>{keys.map((k) => <td key={k}>{row[k] == null ? "—" : String(row[k])}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
