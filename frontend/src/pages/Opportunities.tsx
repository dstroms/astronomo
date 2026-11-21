import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet, apiPost, type Prefs } from "../api";
import { usePrefs } from "../App";
import { Plot } from "../components/Plot";
import { Badge, Btn } from "../components/ui";

function rowKey(o: { plugin: string; id: string | number }) {
  return `${o.plugin}:${o.id}`;
}

export default function Opportunities() {
  const { prefs } = usePrefs();
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [view, setView] = useState<"List" | "Nights">("List");
  const [kindsOn, setKindsOn] = useState<Record<string, boolean>>({});
  const [tasksOn, setTasksOn] = useState<Record<string, boolean>>({});
  const [horizon, setHorizon] = useState(30);
  const [tonightOnly, setTonightOnly] = useState(false);
  const [sort, setSort] = useState("best");
  const [sel, setSel] = useState<string | null>(null);
  const [windowIndex, setWindowIndex] = useState(0);
  const [track, setTrack] = useState<any>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = (refresh = false) => {
    setErr(null);
    const req = refresh
      ? apiPost("/api/v1/opportunities/refresh")
      : apiGet("/api/v1/opportunities", prefs, refresh ? { refresh: true } : undefined);
    req.then((d) => {
      setData(d);
      setKindsOn((prev) => {
        const next = { ...prev };
        for (const k of d.kinds || []) if (next[k.plugin] === undefined) next[k.plugin] = true;
        return next;
      });
      setTasksOn((prev) => {
        const next = { ...prev };
        for (const k of Object.keys(d.task_labels || {})) if (next[k] === undefined) next[k] = true;
        return next;
      });
    }).catch((e) => setErr(e.message));
  };

  useEffect(() => { load(false); }, [prefs.wording, prefs.clock]);

  const rows = useMemo(() => {
    if (!data) return [];
    const now = Date.now();
    const horizonMs = horizon * 86400000;
    let list = (data.opportunities || []).filter((o: any) => {
      if (!o.reachable) return false;
      if (kindsOn[o.plugin] === false) return false;
      if (o.task && tasksOn[o.task] === false) return false;
      const w = o.windows?.[0];
      if (!w) return false;
      const mid = Date.parse(w.mid || w.window_start || w.start || "") || 0;
      if (mid && mid - now > horizonMs) return false;
      if (tonightOnly) {
        const start = Date.parse(w.window_start || w.start || w.mid || "") || 0;
        if (start - now > 18 * 3600000) return false;
      }
      return true;
    });
    if (sort === "best") list = [...list].sort((a, b) => (b.score || 0) - (a.score || 0));
    else if (sort === "soon") list = [...list].sort((a, b) => String(a.windows?.[0]?.mid || "").localeCompare(String(b.windows?.[0]?.mid || "")));
    return list;
  }, [data, kindsOn, tasksOn, horizon, tonightOnly, sort]);

  const selected = rows.find((o: any) => rowKey(o) === sel) || null;

  const open = (o: any, win = 0) => {
    setSel(rowKey(o));
    setWindowIndex(win);
    setTrack(null);
  };
  const close = () => { setSel(null); setTrack(null); };

  useEffect(() => {
    if (!selected) { setTrack(null); return; }
    apiGet(`/api/v1/opportunities/${selected.plugin}/${selected.id}/track`, prefs, { window_index: String(windowIndex) })
      .then(setTrack).catch(() => setTrack(null));
  }, [selected?.plugin, selected?.id, windowIndex, prefs.wording, prefs.clock]);

  const kinds = data?.kinds || [];
  const reachable = (data?.opportunities || []).filter((o: any) => o.reachable).length;
  const listed = kinds.reduce((n: number, k: any) => n + (k.n_candidates || 0), 0);

  const plan = async (o: any, idx: number) => {
    setBusy(true); setErr(null);
    try {
      const r = await apiPost("/api/v1/opportunities/plan", { plugin: o.plugin, opportunity_id: String(o.id), window_index: idx });
      setNotice(r.notice);
      load(false);
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  };

  return (
    <div className="layout">
      <aside>
        <div className="field">
          <label className="eyebrow">Kind of observation</label>
          {kinds.map((k: any) => (
            <label key={k.plugin} className="check">
              <input type="checkbox" checked={kindsOn[k.plugin] !== false} onChange={(e) => setKindsOn({ ...kindsOn, [k.plugin]: e.target.checked })} />
              <span>{k.label} <span className="muted">{k.reachable}</span></span>
            </label>
          ))}
        </div>
        <div className="field">
          <label className="eyebrow">Look ahead</label>
          <span className="seg">
            {[7, 14, 30].map((d) => (
              <button key={d} type="button" className={horizon === d ? "on" : ""} onClick={() => setHorizon(d)}>{d} d</button>
            ))}
          </span>
        </div>
        <div className="field">
          <label className="eyebrow">What the networks want</label>
          {Object.entries(data?.task_labels || {}).map(([k, label]) => (
            <label key={k} className="check">
              <input type="checkbox" checked={tasksOn[k] !== false} onChange={(e) => setTasksOn({ ...tasksOn, [k]: e.target.checked })} />
              <span>{String(label)}</span>
            </label>
          ))}
          <label className="check" style={{ marginTop: 12 }}>
            <input type="checkbox" checked={tonightOnly} onChange={(e) => setTonightOnly(e.target.checked)} />
            <span>Only tonight</span>
          </label>
        </div>
        <div className="field">
          <label className="eyebrow">Sort by</label>
          <select className="select" value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="best">Best (what the networks rank highest)</option>
            <option value="soon">Soonest window</option>
          </select>
        </div>
        <div className="field">
          <Btn onClick={async () => {
            setBusy(true); setErr(null);
            try {
              const r = await apiPost("/api/v1/opportunities/forecast");
              setNotice(`Loaded ${r.n} forecast hours${r.source ? ` (${r.source})` : ""}.`);
            } catch (e: any) { setErr(e.message); }
            finally { setBusy(false); }
          }}>Load 7-day forecast</Btn>
        </div>
      </aside>
      <main className="page stack">
        <div className="row top">
          <div className="grow">
            <h1>Opportunities</h1>
            <div className="muted" style={{ marginTop: 2 }}>What the networks ask for, what this telescope can reach, and when.</div>
          </div>
          <div style={{ textAlign: "right" }}>
            <Btn disabled={busy} onClick={() => { setBusy(true); load(true); setBusy(false); }}>Refresh from networks</Btn>
          </div>
        </div>
        {err ? <div className="callout red small">{err}</div> : null}
        {notice ? <div className="callout blue small">{notice}</div> : null}
        {data?.site_banner ? <div className="muted small">{data.site_banner} <Link to="/settings">Change in Settings</Link></div> : null}
        <div className="funnel">
          <span><b>{listed.toLocaleString()}</b><span className="l">on a network list</span></span>
          <span className="ar">→</span>
          <span><b>{reachable.toLocaleString()}</b><span className="l">reachable</span></span>
          <span className="ar">→</span>
          <span><b>{rows.length.toLocaleString()}</b><span className="l">showing</span></span>
        </div>
        <div className="row">
          <div className="tabs grow" style={{ borderBottom: 0 }}>
            <button type="button" className={view === "List" ? "on" : ""} onClick={() => setView("List")}>List</button>
            <button type="button" className={view === "Nights" ? "on" : ""} onClick={() => setView("Nights")}>Nights</button>
          </div>
        </div>
        {!data ? <p className="muted">Loading lists from this computer…</p> : view === "List" ? (
          <>
            <p className="muted small">Click a row for altitude, the expected light curve, and the rest of the target.</p>
            <div className="tablewrap">
              <table>
                <thead>
                  <tr><th>Target</th><th>Kind</th><th>What it is for</th><th>Value</th><th>Next window</th><th /></tr>
                </thead>
                <tbody>
                  {rows.slice(0, 50).map((o: any) => (
                    <tr key={rowKey(o)} className={`clickable ${sel === rowKey(o) ? "sel" : ""}`} onClick={() => open(o)}>
                      <td><b>{o.name}</b></td>
                      <td className="muted">{o.plugin_label}</td>
                      <td className="muted">{o.task_label}</td>
                      <td><Badge slot={o.impact_slot}>{o.impact_label}</Badge></td>
                      <td>{o.windows?.[0]?.label || "—"}</td>
                      <td>{o.plan_status ? <Badge slot="blue">{o.plan_status}</Badge> : null}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <NightTable rows={rows} selected={sel} onOpen={open} onPlan={(o) => plan(o, 0)} busy={busy} />
        )}
        {selected ? (
          <OpportunityModal
            op={selected}
            track={track}
            windowIndex={windowIndex}
            prefs={prefs}
            busy={busy}
            onWindow={(i) => setWindowIndex(i)}
            onClose={close}
            onPlan={() => plan(selected, windowIndex)}
            onCancel={async () => {
              if (!selected.planned_id) return;
              setBusy(true); setErr(null);
              try {
                const r = await apiPost(`/api/v1/plans/${selected.planned_id}/cancel`);
                setNotice(r.notice);
                load(false);
              } catch (e: any) { setErr(e.message); }
              finally { setBusy(false); }
            }}
          />
        ) : null}
      </main>
    </div>
  );
}

function NightTable({
  rows, selected, onOpen, onPlan, busy,
}: { rows: any[]; selected: string | null; onOpen: (o: any) => void; onPlan: (o: any) => void; busy: boolean }) {
  const byDay: Record<string, any[]> = {};
  for (const o of rows) {
    const w = o.windows?.[0];
    const day = (w?.mid || w?.window_start || "").slice(0, 10) || "unknown";
    (byDay[day] ||= []).push(o);
  }
  return (
    <div className="stack">
      <p className="muted small">Click a row for altitude, the expected light curve, and the rest of the target.</p>
      {Object.entries(byDay).slice(0, 14).map(([day, list]) => (
        <div key={day}>
          <div className="eyebrow">Windows {day} · {list.length}</div>
          <div className="tablewrap">
            <table>
              <thead><tr><th>Target</th><th>Kind</th><th>Window</th><th>Value</th><th /></tr></thead>
              <tbody>
                {list.map((o) => (
                  <tr key={rowKey(o)} className={`clickable ${selected === rowKey(o) ? "sel" : ""}`} onClick={() => onOpen(o)}>
                    <td><b>{o.name}</b></td>
                    <td className="muted">{o.plugin_label}</td>
                    <td>{o.windows?.[0]?.label}</td>
                    <td><Badge slot={o.impact_slot}>{o.impact_label}</Badge></td>
                    <td>{o.planning !== false && !o.planned_id ? (
                      <Btn kind="primary" disabled={busy} onClick={(e) => { e.stopPropagation(); onPlan(o); }}>Plan this</Btn>
                    ) : o.plan_status ? <Badge slot="blue">{o.plan_status}</Badge> : null}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}

function OpportunityModal({
  op, track, windowIndex, prefs, busy, onWindow, onClose, onPlan, onCancel,
}: {
  op: any; track: any; windowIndex: number; prefs: Prefs; busy: boolean;
  onWindow: (i: number) => void; onClose: () => void; onPlan: () => void; onCancel: () => void;
}) {
  const chosen = op.windows?.[windowIndex] || op.windows?.[0];
  const facts = factItems(op, track);
  const altFig = altitudeFigure(track, prefs.clock === "12-hour");
  const lc = lightCurveFigure(op.facts || {});
  const timing = chosen?.timing_lines || [];

  useEffect(() => {
    const prev = document.body.classList.contains("modal-open");
    document.body.classList.add("modal-open");
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    globalThis.addEventListener("keydown", onKey);
    return () => {
      globalThis.removeEventListener("keydown", onKey);
      if (!prev) document.body.classList.remove("modal-open");
    };
  }, [onClose]);

  return (
    <div className="modal-back" onClick={onClose} role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="opp-modal-title" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div className="grow">
            <div className="chips">
              <Badge slot={op.impact_slot}>{op.impact_label}</Badge>
              {op.task_label ? <Badge slot="gray">{op.task_label}</Badge> : null}
              {op.first_light ? <Badge slot="violet">verification target</Badge> : null}
              {op.host ? <span className="muted small">{op.host}</span> : null}
            </div>
            <h2 id="opp-modal-title">{op.name}</h2>
            {op.headline ? <div className="muted" style={{ marginTop: 4 }}>{op.headline}</div> : null}
          </div>
          <Btn onClick={onClose}>Close</Btn>
        </div>
        {op.first_light?.why?.length ? (
          <p className="muted small">Chosen to check the telescope and software: {op.first_light.why.join("; ")}.</p>
        ) : null}
        {op.schedule?.headline ? (
          <p className="small">{op.schedule.headline}</p>
        ) : null}
        {op.reason ? (
          <p className="muted small">{op.reason}{op.source_line ? ` (from ${op.source_line})` : ""}</p>
        ) : null}
        {op.detail ? <p className="small">{op.detail}</p> : null}
        {facts.length ? (
          <div className="facts">
            {facts.map((f) => <div key={f.label}><b>{f.value}</b><span>{f.label}</span></div>)}
          </div>
        ) : null}
        <div className="modal-charts">
          <div className={lc ? "" : "full"}>
            <div className="eyebrow">Altitude through the window</div>
            {altFig ? <Plot figure={altFig} height={220} /> : <p className="muted small">Loading altitude…</p>}
            {track?.altitude_line ? <p className="chart-cap">{track.altitude_line}{track.now_line ? ` ${track.now_line}` : ""}</p> : null}
          </div>
          {lc ? (
            <div>
              <div className="eyebrow">{lc.title}</div>
              <Plot figure={lc.figure} height={220} />
              <p className="chart-cap">{lc.caption}</p>
            </div>
          ) : null}
        </div>
        <div className="field" style={{ marginTop: 10, maxWidth: 420 }}>
          <label>Window</label>
          <select className="select" value={windowIndex} onChange={(e) => onWindow(Number(e.target.value))}>
            {(op.windows || []).map((w: any, i: number) => (
              <option key={i} value={i}>{w.label}</option>
            ))}
          </select>
        </div>
        {timing.length ? (
          <div className="timing">
            {timing.map((line: string, i: number) => <p key={i}>{line}</p>)}
          </div>
        ) : null}
        {op.planning === false ? (
          <p className="muted small">Cannot be planned yet: this plugin lists what is worth observing but cannot run a mission.</p>
        ) : op.planned_id && op.plan_status === "PLANNED" ? (
          <div className="row">
            <Badge slot="green">Planned</Badge>
            <Link to="/">View on Tonight</Link>
            <Btn onClick={onCancel} disabled={busy}>Cancel plan</Btn>
          </div>
        ) : op.planned_id ? (
          <p className="muted small">See Tonight. This window can be planned again once that mission finishes.</p>
        ) : (
          <Btn kind="primary" disabled={busy} onClick={onPlan}>{op.earlier ? "Plan again" : "Plan this"}</Btn>
        )}
        {op.earlier ? <p className="small muted" style={{ marginTop: 8 }}>{op.earlier}</p> : null}
        {(op.assumptions?.length || op.blockers?.length || op.terms) ? (
          <details className="why">
            <summary>Why this target</summary>
            <div className="body">
              {op.assumptions?.length ? (
                <>
                  <div className="eyebrow">Assumptions</div>
                  <ul>{op.assumptions.map((a: string, i: number) => <li key={i} className="small muted">{a}</li>)}</ul>
                </>
              ) : null}
              {op.blockers?.length ? (
                <>
                  <div className="eyebrow">Blockers</div>
                  <ul>{op.blockers.map((a: string, i: number) => <li key={i} className="small muted">{a}</li>)}</ul>
                </>
              ) : null}
              {op.terms ? <p className="small muted">rank score {op.score} · terms {op.terms}</p> : null}
              {op.figures ? <p className="small muted">{Object.entries(op.figures).map(([k, v]) => `${k} ${v}`).join(" · ")}</p> : null}
            </div>
          </details>
        ) : null}
      </div>
    </div>
  );
}

function factItems(op: any, track: any): { label: string; value: string }[] {
  const f = op.facts || {};
  const items: { label: string; value: string }[] = [];
  const vmag = f.v_mag ?? f.vmag;
  if (vmag != null && Number.isFinite(Number(vmag))) items.push({ label: "V mag", value: Number(vmag).toFixed(1) });
  if (f.depth_pct != null && Number.isFinite(Number(f.depth_pct))) items.push({ label: "dip", value: `${Number(f.depth_pct).toFixed(1)} %` });
  if (f.duration_h != null && Number.isFinite(Number(f.duration_h))) items.push({ label: "duration", value: `${Number(f.duration_h).toFixed(1)} h` });
  if (f.period_d != null && Number.isFinite(Number(f.period_d))) {
    const p = Number(f.period_d);
    items.push({ label: "period", value: `${p.toFixed(p >= 10 ? 1 : 3)} d` });
  }
  if (f.v_max_mag != null && f.v_min_mag != null) {
    items.push({ label: "range", value: `${Number(f.v_max_mag).toFixed(1)}–${Number(f.v_min_mag).toFixed(1)}` });
  }
  if (f.var_type) items.push({ label: "type", value: String(f.var_type) });
  if (track?.peak_alt_deg != null) items.push({ label: "highest", value: `${Math.round(track.peak_alt_deg)}°` });
  const n = (op.windows || []).length;
  if (n) items.push({ label: n === 1 ? "chance" : "chances", value: String(n) });
  return items;
}

function cssVar(name: string, fallback: string) {
  if (typeof document === "undefined") return fallback;
  return getComputedStyle(document.body).getPropertyValue(name).trim() || fallback;
}

function siteClock(iso: string | null | undefined) {
  if (!iso) return "";
  return iso.replace("T", " ").replace(/[+-]\d{2}:\d{2}$/, "").replace(/Z$/, "").slice(0, 19);
}

function altitudeFigure(track: any, hour12: boolean) {
  const samples = track?.samples;
  if (!samples?.length) return null;
  const blue = cssVar("--blue", "#5BB9E8");
  const green = cssVar("--green", "#4DBB8A");
  const orange = cssVar("--orange", "#E48A4B");
  const muted = cssVar("--muted", "#99A1AB");
  const x = samples.map((s: any) => siteClock(s.at));
  const y = samples.map((s: any) => s.alt_deg);
  const data: any[] = [{
    x, y, mode: "lines", name: "Altitude",
    line: { color: blue, width: 2 },
    hovertemplate: "%{x}<br>%{y:.0f} deg<extra></extra>",
  }];
  if (track.peak_at != null && track.peak_alt_deg != null) {
    data.push({
      x: [siteClock(track.peak_at)], y: [track.peak_alt_deg], mode: "markers+text", name: "Highest",
      marker: { size: 9, color: green },
      text: [`${Math.round(track.peak_alt_deg)} deg`],
      textposition: "top center",
      textfont: { color: green },
      hovertemplate: "Highest<br>%{y:.0f} deg<extra></extra>",
    });
  }
  if (track.now_on_curve && track.now_alt_deg != null) {
    data.push({
      x: [siteClock(track.now_at) || x[Math.floor(x.length / 2)]],
      y: [track.now_alt_deg],
      mode: "markers+text", name: "Now",
      marker: { size: 11, symbol: "diamond", color: orange },
      text: ["now"],
      textposition: "top center",
      textfont: { color: orange },
      hovertemplate: "Now<br>%{y:.0f} deg<extra></extra>",
    });
  }
  const floor = Math.min(0, Math.min(...y) - 2);
  return {
    data,
    layout: {
      height: 220,
      showlegend: false,
      yaxis: { title: "Altitude (deg)", range: [floor, 96], dtick: 15 },
      xaxis: { type: "date", title: "Site time", tickformat: hour12 ? "%-I:%M %p" : "%H:%M" },
      shapes: [{
        type: "line", xref: "paper", x0: 0, x1: 1, y0: 70, y1: 70,
        line: { color: muted, width: 1, dash: "dot" },
      }],
      annotations: [{
        xref: "paper", x: 1, y: 70, xanchor: "right", yanchor: "bottom",
        text: "70 deg", showarrow: false, font: { color: muted, size: 11 },
      }],
    },
  };
}

function lightCurveFigure(facts: any): { title: string; caption: string; figure: any } | null {
  const blue = cssVar("--blue", "#5BB9E8");
  const depth = Number(facts.depth_pct);
  const duration = Number(facts.duration_h);
  if (Number.isFinite(depth) && depth > 0 && Number.isFinite(duration) && duration > 0) {
    const dip = depth / 100;
    const half = duration / 2;
    const ingress = Math.max(duration * 0.12, 0.02);
    const x: number[] = [];
    const y: number[] = [];
    const span = duration * 1.6;
    for (let i = 0; i <= 80; i++) {
      const t = -span / 2 + (span * i) / 80;
      const a = Math.abs(t);
      let flux = 1;
      if (a <= half - ingress) flux = 1 - dip;
      else if (a < half) flux = 1 - dip * (half - a) / ingress;
      x.push(t);
      y.push(flux);
    }
    return {
      title: "Expected transit",
      caption: "Catalogue depth and duration, not a measurement. The real dip will look like this if the predicted time is right.",
      figure: {
        data: [{ x, y, mode: "lines", line: { color: blue, width: 2 }, hovertemplate: "%{x:.2f} h<br>%{y:.5f}<extra></extra>" }],
        layout: {
          height: 220,
          showlegend: false,
          xaxis: { title: "Hours from mid-transit" },
          yaxis: { title: "Relative flux", range: [1 - dip * 1.8, 1.005] },
        },
      },
    };
  }
  const vmax = Number(facts.v_max_mag);
  const vmin = Number(facts.v_min_mag ?? facts.faintest_mag);
  if (Number.isFinite(vmax) && Number.isFinite(vmin) && vmax !== vmin) {
    const x: number[] = [];
    const y: number[] = [];
    for (let i = 0; i <= 80; i++) {
      const phase = i / 80;
      x.push(phase);
      y.push(vmax + (vmin - vmax) * (0.5 - 0.5 * Math.cos(2 * Math.PI * phase)));
    }
    return {
      title: "Expected brightness range",
      caption: "Catalogue magnitude range drawn as a smooth cycle. This is not a prediction of tonight's phase.",
      figure: {
        data: [{ x, y, mode: "lines", line: { color: blue, width: 2 }, hovertemplate: "phase %{x:.2f}<br>%{y:.2f} mag<extra></extra>" }],
        layout: {
          height: 220,
          showlegend: false,
          xaxis: { title: facts.period_d ? `Phase (period ${Number(facts.period_d).toFixed(3)} d)` : "Phase" },
          yaxis: { title: "Magnitude", autorange: "reversed" },
        },
      },
    };
  }
  return null;
}
