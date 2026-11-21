import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiGet, apiPost } from "../api";
import { usePrefs } from "../App";
import { Evidence, Plot } from "../components/Plot";
import { Badge, Bar, Btn, Chips, Dot } from "../components/ui";

const TABS = ["Summary", "Evidence", "Documents", "Process"] as const;

export default function Mission() {
  const { id } = useParams();
  const { prefs } = usePrefs();
  const [data, setData] = useState<any>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Summary");
  const [err, setErr] = useState<string | null>(null);
  const [frames, setFrames] = useState<any[]>([]);
  const [frameIndex, setFrameIndex] = useState(0);

  const load = () => {
    if (!id) return;
    apiGet(`/api/v1/missions/${id}`, prefs).then(setData).catch((e) => setErr(e.message));
    apiGet(`/api/v1/missions/${id}/frames`).then((r) => setFrames(r.frames || [])).catch(() => setFrames([]));
  };
  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [id, prefs.wording, prefs.clock]);

  if (err && !data) return <div className="full"><p className="err">{err}</p></div>;
  if (!data) return <div className="full muted">Loading…</div>;
  const row = data.row || {};
  const m = data.mission || {};
  const summary = data.summary || {};
  const arts = data.artifacts || {};
  const verdictSlot = summary.verdict === "good" ? "green" : summary.verdict === "attention" ? "orange" : "blue";

  return (
    <div className="full stack">
      <div className="row small muted">
        <Link to="/missions">Missions</Link> › <span>{row.title}</span>
      </div>
      <div className="row"><h1 className="grow">{row.title}</h1></div>
      <div className="row" style={{ gap: 8 }}>
        <Chips chips={row.chips} />
        <span className="muted small mono">{m.id}</span>
        <span className="muted small">· {m.updated_ago}</span>
        {data.plan?.caption ? <span className="muted small">· {data.plan.caption}</span> : null}
      </div>
      <div className="muted">{m.description}</div>
      <Lead data={data} />

      <div className="tabs">
        {TABS.map((t) => (
          <button key={t} type="button" className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>

      {tab === "Summary" && (
        <div className="row top" style={{ gap: 24 }}>
          <div className="grow" style={{ flex: 3 }}>
            <div className="eyebrow" style={{ marginBottom: 6 }}>The plan</div>
            <div className="small" style={{ marginBottom: 10 }}>{data.plan?.text || m.hypothesis || "—"}</div>
            <div className="tablewrap">
              <table>
                <thead><tr><th style={{ width: 36 }}>#</th><th>Target</th><th>Status</th></tr></thead>
                <tbody>
                  {(data.targets || []).map((t: any) => (
                    <tr key={t.position}><td className="mono">{t.position}</td><td>{t.name}{t.distance ? ` · ${t.distance}` : ""}</td><td><Dot slot={t.status === "OBSERVED" ? "green" : "gray"} filled={t.status === "OBSERVED"} />{t.status_label}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          <div className="grow" style={{ flex: 2 }}>
            <div className="eyebrow" style={{ marginBottom: 6 }}>Also in this mission</div>
            <div className="small stack">
              <div><button type="button" className="btn ghost" onClick={() => setTab("Documents")}>Mission log</button> <span className="muted">in Documents</span></div>
              <div><button type="button" className="btn ghost" onClick={() => setTab("Evidence")}>Frames and results</button> <span className="muted">in Evidence</span></div>
              <div><button type="button" className="btn ghost" onClick={() => setTab("Process")}>Pipeline, weather and model calls</button> <span className="muted">in Process</span></div>
            </div>
          </div>
        </div>
      )}

      {tab === "Evidence" && (
        <div className="stack">
          {collectPlots(data.evidence).map((p, i) => (
            <div key={i}>
              {p.title ? <div className="eyebrow" style={{ marginBottom: 8 }}>{p.title}</div> : null}
              <Plot figure={p.figure} />
            </div>
          ))}
          {frames.length > 0 && (
            <div className="card">
              <div className="eyebrow">Frames</div>
              <p className="muted small">
                {frames.length} {frames.length === 1 ? "frame" : "frames"} saved. The picture is stretched so faint structure can be seen. It is a view of the file, not a measurement.
              </p>
              <div className="row" style={{ flexWrap: "wrap", marginTop: 8 }}>
                {frames.map((f: any) => (
                  <button
                    key={f.index}
                    type="button"
                    className={`chip ${frameIndex === f.index ? "on" : ""}`}
                    onClick={() => setFrameIndex(f.index)}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            </div>
          )}
          {frames[frameIndex] ? (
            <div className="card">
              <div className="eyebrow">{frames[frameIndex].label}</div>
              <img src={frames[frameIndex].url} alt={frames[frameIndex].label} style={{ maxWidth: "100%" }} />
            </div>
          ) : data.live_frame?.latest_url ? (
            <div className="card">
              <div className="eyebrow">{data.live_frame.title}</div>
              <p className="muted small">{data.live_frame.caption}</p>
              <img src={data.live_frame.latest_url} alt="Live frame" style={{ maxWidth: "100%" }} />
            </div>
          ) : data.first_frame?.preview_url ? (
            <div className="card">
              <div className="eyebrow">First frame</div>
              <p className="muted small">{data.first_frame.caption}</p>
              <img src={data.first_frame.preview_url} alt="First frame" style={{ maxWidth: "100%" }} />
            </div>
          ) : null}
          <Evidence data={stripPlots(data.evidence)} />
        </div>
      )}

      {tab === "Documents" && (
        <div className="stack">
          <p className="muted small">{arts.notice}</p>
          {(arts.findings || []).map((a: any) => (
            <div key={a.id} className="card stack">
              <div className="row">
                <div className="grow">
                  <div className="eyebrow">Findings summary</div>
                  <b>{a.title}</b>
                </div>
                {a.downloadable && <a className="btn primary" href={`/api/v1/artifacts/${a.id}`}>Download {a.filename}</a>}
              </div>
              {(a.rows || []).length > 0 && (
                <div className="tablewrap">
                  <table>
                    <thead><tr><th>Target</th><th>Measured</th><th>Quoted error</th><th>Compared with</th></tr></thead>
                    <tbody>
                      {a.rows.map((r: any, i: number) => (
                        <tr key={i}><td>{r.target}</td><td>{r.measured}</td><td>{r.error}</td><td>{r.compared}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {(a.caveats || []).length > 0 && (
                <div>
                  <div className="eyebrow">What the numbers mean</div>
                  <ul className="small">{a.caveats.map((c: string, i: number) => <li key={i}>{c}</li>)}</ul>
                </div>
              )}
              {(a.checks || []).length > 0 && (
                <div>
                  <div className="eyebrow">Check before sending</div>
                  <ul className="small">{a.checks.map((c: string, i: number) => <li key={i}>{c}</li>)}</ul>
                </div>
              )}
            </div>
          ))}
          {["submissions", "mission_logs"].map((k) => (
            (arts[k] || []).map((a: any) => (
              <div key={a.id} className="card">
                <div className="row">
                  <div className="grow">
                    <b>{a.title}</b>
                    <div className="small"><Dot slot={a.status === "ready_for_review" ? "green" : "gray"} />{a.status_label}</div>
                    {(a.blockers || []).map((b: string, i: number) => <div key={i} className="small err">{b}</div>)}
                  </div>
                  {a.downloadable && <a className="btn primary" href={`/api/v1/artifacts/${a.id}`}>Download {a.filename}</a>}
                </div>
              </div>
            ))
          ))}
        </div>
      )}

      {tab === "Process" && (
        <div className="stack">
          <div className="card">
            <div className="eyebrow">Pipeline</div>
            <div className="row" style={{ flexWrap: "wrap", marginTop: 8 }}>
              {(data.pipeline || []).map((n: any) => (
                <span key={n.node} className="small"><Dot slot={n.slot} filled={n.filled} />{n.name} · {n.label}</span>
              ))}
            </div>
          </div>
          {data.weather?.latest && (
            <div className="card">
              <div className="eyebrow">Weather</div>
              <div>{data.weather.verdict}</div>
            </div>
          )}
          <div className="card">
            <div className="eyebrow">Activity</div>
            {(data.activity?.logs || []).slice(0, 40).map((l: any, i: number) => (
              <div key={i} className="small mono">{l.ts} {l.level} {l.node} {l.message}</div>
            ))}
          </div>
          <LlmTraffic llm={data.activity?.llm} />
        </div>
      )}
      {summary.verdict && tab === "Summary" ? null : null}
      {data.plan?.can_plan_again && (
        <Btn onClick={() => apiPost(`/api/v1/missions/${m.id}/plan-again`).then(load)}>Plan again</Btn>
      )}
    </div>
  );
}

function LlmTraffic({ llm }: { llm?: any }) {
  const usage = llm?.usage;
  const items = llm?.items || [];
  const provider = llm?.provider || "none";
  const model = llm?.model;
  const hasCalls = items.length > 0 || (usage && (usage.calls || usage.cached_calls || usage.failed_calls));
  if (!hasCalls) {
    return (
      <div className="card">
        <div className="eyebrow">Language model</div>
        <p className="muted small">
          No language-model calls (provider: {provider}). With the default provider none every sentence in the report is deterministic and zero tokens are spent.
        </p>
      </div>
    );
  }
  return (
    <div className="card stack">
      <div className="eyebrow">Language model</div>
      <div className="facts">
        <div><b>{usage?.calls ?? llm?.calls ?? 0}</b><span>Calls (paid)</span></div>
        <div><b>{usage?.prompt_tokens ?? 0} / {usage?.completion_tokens ?? 0}</b><span>Tokens (in / out)</span></div>
        <div><b>{usage?.cached_calls ?? 0}</b><span>Cache hits</span></div>
        <div><b>{usage?.max_prompt_chars ?? 0}</b><span>Largest prompt (chars)</span></div>
      </div>
      <p className="muted small">
        {provider}{model ? `: ${model}` : ""}. The model only receives concise digests (never FITS data or tables); below is exactly what was sent.
      </p>
      {items.map((c: any, i: number) => {
        const label = c.cached ? "cached" : (!c.ok ? "FAILED" : "ok");
        const latency = c.latency_s != null && c.latency_s !== "" ? ` · ${Number(c.latency_s).toFixed(1)}s` : "";
        return (
          <details key={i} className="why">
            <summary>{c.node || "—"} · {label} · {c.prompt_tokens ?? 0}+{c.completion_tokens ?? 0} tokens{latency}</summary>
            <div className="body">
              {c.error ? <p className="err">{c.error}</p> : null}
              <div className="eyebrow">Prompt sent</div>
              <pre className="cmd">{c.prompt || "—"}</pre>
              <div className="eyebrow">Response</div>
              <pre className="cmd">{c.response || "—"}</pre>
            </div>
          </details>
        );
      })}
    </div>
  );
}

function collectPlots(data: any): { title?: string; figure: any }[] {
  if (!data) return [];
  const out = [...(data.plotly || [])];
  for (const obs of data.observations || []) {
    for (const p of obs.plotly || []) {
      const title = [obs.title, p.title].filter(Boolean).join(" · ");
      out.push({ ...p, title: title || p.title });
    }
  }
  return out;
}

function stripPlots(data: any) {
  if (!data) return data;
  return {
    ...data,
    plotly: undefined,
    observations: (data.observations || []).map((o: any) => ({ ...o, plotly: undefined })),
  };
}

function Lead({ data }: { data: any }) {
  const row = data.row || {};
  const summary = data.summary || {};
  const arts = (data.artifacts?.submissions || [])[0];
  const findings = (data.artifacts?.findings || [])[0];
  const slot = summary.verdict === "good" ? "green" : summary.verdict === "attention" ? "orange" : "blue";
  if (row.state === "ORPHANED" || row.state === "PAUSED") {
    return (
      <div className="lead attn">
        <div className="eyebrow">Needs you · {row.state_label}</div>
        <div style={{ fontSize: "1.05rem", fontWeight: 500, margin: "6px 0" }}>{row.describe_state}</div>
        {row.resume_command ? <div className="cmd mono small">{row.resume_command}</div> : null}
        <div className="small muted" style={{ marginTop: 8 }}>{row.progress_text}</div>
      </div>
    );
  }
  if (row.state === "FAILED") {
    return (
      <div className="lead fail">
        <div className="eyebrow">Failed</div>
        <div style={{ fontSize: "1.05rem", fontWeight: 500, margin: "6px 0" }}>{row.describe_state}</div>
        {row.resume_command ? <div className="small muted" style={{ marginTop: 8 }}>Resume: <span className="mono">{row.resume_command}</span></div> : null}
      </div>
    );
  }
  return (
    <div className="row top" style={{ gap: 14 }}>
      <div className="card grow" style={{ flex: 3 }}>
        <div className="eyebrow">Result</div>
        <div style={{ fontSize: "1.1rem", fontWeight: 500, margin: "6px 0 10px" }}>{summary.found}</div>
        <div className={`callout ${slot}`}><b>What that means.</b> {summary.meaning}</div>
        <div className="row small" style={{ marginTop: 12 }}>
          <span className="muted">{row.progress_text}</span>
          <Bar fraction={row.fraction} />
        </div>
      </div>
      <div className="card grow" style={{ flex: 2 }}>
        <div className="eyebrow">Deliverables</div>
        {arts ? (
          <>
            <div style={{ margin: "8px 0 2px" }}><b>{arts.title}</b></div>
            <div className="small"><Dot slot={arts.status === "ready_for_review" ? "green" : "gray"} />{arts.status_label}</div>
            {arts.downloadable ? <div style={{ margin: "10px 0 8px" }}><a className="btn primary" href={`/api/v1/artifacts/${arts.id}`}>Download {arts.filename}</a></div> : null}
          </>
        ) : <div className="muted small" style={{ marginTop: 8 }}>None yet.</div>}
        {findings?.downloadable ? (
          <div className="small" style={{ margin: "0 0 8px" }}>Read the <a href={`/api/v1/artifacts/${findings.id}`}>findings summary</a> before you send anything.</div>
        ) : null}
        <div className="small muted">Nothing is sent anywhere: you review each file and upload it yourself.</div>
      </div>
    </div>
  );
}
