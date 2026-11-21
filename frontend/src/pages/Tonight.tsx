import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet, apiPost } from "../api";
import { usePrefs } from "../App";
import { Badge, Bar, Btn, Chips, Dot, Eyebrow } from "../components/ui";

export default function Tonight() {
  const { prefs } = usePrefs();
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ask, setAsk] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = () => {
    apiGet("/api/v1/tonight", prefs).then(setData).catch((e) => setErr(String(e.message || e)));
  };
  useEffect(() => {
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [prefs.wording, prefs.clock]);

  const act = async (fn: () => Promise<any>) => {
    setBusy(true); setErr(null); setNotice(null);
    try {
      const out = await fn();
      setNotice(out?.notice || out?.error || null);
      if (out?.needs_confirm) return;
      setAsk(null);
      load();
    } catch (e: any) {
      setErr(e.message || String(e));
    } finally { setBusy(false); }
  };

  if (err && !data) return <div className="full"><p className="err">{err}</p></div>;
  if (!data) return <div className="full muted">Loading…</div>;
  const c = data.counts || {};
  return (
    <div className="full stack">
      <div className="row">
        <h1 className="grow">Tonight</h1>
        <span className="muted small">{data.date} · {c.needs_you || 0} needs you · {c.running || 0} running · {c.waiting || 0} waiting · {c.planned || 0} planned</span>
      </div>
      {notice ? <div className="callout blue small">{notice}</div> : null}
      {err ? <div className="callout red small">{err}</div> : null}
      {!data.have_db ? <p className="muted">{data.empty}</p> : null}

      {(data.attention || []).length > 0 && (
        <>
          <Eyebrow>Needs you · {data.attention.length}</Eyebrow>
          {data.attention.map((row: any) => (
            <div key={row.id} className="card attn">
              <div className="row top">
                <Dot slot={row.look?.slot} filled={row.look?.filled} />
                <div className="grow">
                  <b>{row.title}</b> <Chips chips={row.chips} />
                  <div className="muted small" style={{ marginTop: 3 }}>{row.describe_state}</div>
                  {row.resume_command ? <div className="cmd mono small">{row.resume_command}</div> : null}
                </div>
                <Link className="btn" to={`/missions/${row.id}`}>Open</Link>
              </div>
            </div>
          ))}
        </>
      )}

      <Eyebrow>Running · {(data.running || []).length}</Eyebrow>
      {(data.running || []).length === 0 ? <p className="muted small">Nothing is running.</p> : (
        <div className="card stack">
          {data.running.map((row: any) => (
            <div key={row.id} className="row">
              <Dot slot={row.look?.slot} filled={row.look?.filled} />
              <b>{row.title}</b>
              <Chips chips={row.chips} />
              <Bar fraction={row.fraction} />
              <span className="mono small muted">{row.progress_text}</span>
              <Link className="btn" to={`/missions/${row.id}`}>Open</Link>
            </div>
          ))}
        </div>
      )}

      <Eyebrow>Up next · {(data.plans?.live || []).length}</Eyebrow>
      {(data.plans?.live || []).map((plan: any) => (
        <PlanCard key={plan.id} plan={plan} ask={ask} setAsk={setAsk} busy={busy} act={act} />
      ))}

      {data.conditions && (
        <>
          <Eyebrow>Conditions</Eyebrow>
          <div className="row small" style={{ gap: 26 }}>
            <Badge slot={data.conditions.slot}>{data.conditions.verdict}</Badge>
            {data.conditions.cloud_cover_pct != null && <span><span className="muted">Cloud cover</span> <b className="mono">{data.conditions.cloud_cover_pct} %</b></span>}
            {data.conditions.wind_kmh != null && <span><span className="muted">Wind</span> <b className="mono">{data.conditions.wind_kmh} km/h</b></span>}
            {data.conditions.seeing_arcsec != null && <span><span className="muted">Air steadiness</span> <b className="mono">{data.conditions.seeing_arcsec}″</b></span>}
            <span className="muted">last reading {data.conditions.age}</span>
          </div>
        </>
      )}

      {(data.ready_for_review || []).length > 0 && (
        <>
          <Eyebrow>Ready for review · {data.ready_for_review.length}</Eyebrow>
          {data.ready_for_review.map((a: any) => (
            <div key={a.artifact_id} className="card">
              <div className="row">
                <div className="grow">
                  <b>{a.title}</b> <span className="muted small" style={{ marginLeft: 6 }}>{a.finished_ago}</span>
                  <div className="small" style={{ marginTop: 3 }}>{a.file_title} <span className="muted">· {a.status_label}</span></div>
                </div>
                {a.downloadable !== false && a.artifact_id != null && (
                  <a className="btn" href={`/api/v1/artifacts/${a.artifact_id}`}>Download {a.filename}</a>
                )}
                <Link className="btn" to={`/missions/${a.mission_id}`}>Open mission</Link>
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

function PlanCard({ plan, ask, setAsk, busy, act }: any) {
  const confirming = ask === plan.id;
  return (
    <div className="card">
      <div className="row top">
        <div className="grow">
          <b style={{ fontSize: "1.05rem" }}>{plan.target_name}</b>{" "}
          <Badge slot={plan.look?.slot}>{plan.status_label}</Badge>{" "}
          <span className="muted small" style={{ marginLeft: 6 }}>{plan.plugin_label}</span>
          <div className="small" style={{ marginTop: 4 }}>
            {(plan.timing_lines || []).map((t: string, i: number) => <div key={i}>{t}</div>)}
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div className="mono" style={{ fontSize: "1.25rem", fontWeight: 500 }}>{plan.countdown?.value}</div>
          <div className="small muted">{plan.countdown?.hint}</div>
        </div>
      </div>
      {plan.rehearsal && (
        <div className="callout blue small" style={{ marginTop: 8 }}>
          <b>Rehearsal</b> <span className="mono">{plan.rehearsal.mission_id}</span>
          {plan.rehearsal.describe_state ? ` · ${plan.rehearsal.describe_state}` : ""} {plan.rehearsal.note}
          {plan.rehearsal.mission_id ? <Link style={{ marginLeft: 6 }} to={`/missions/${plan.rehearsal.mission_id}`}>Open</Link> : null}
        </div>
      )}
      {plan.busy && (
        <div style={{ marginTop: 10 }}>
          <p className="small muted">{plan.busy_kind || "Mission"} process {plan.busy_pid} is running. Started with:</p>
          <pre className="cmd mono">{plan.started_command || plan.commands?.run}</pre>
          <p className="small muted">Stop it with this. On a real telescope it parks the mount and the mission will not resume.</p>
          <pre className="cmd mono">{plan.commands?.stop}</pre>
        </div>
      )}
      {!plan.busy && plan.mission_running && plan.commands?.stop && (
        <div style={{ marginTop: 10 }}>
          <p className="small muted">Mission {plan.mission_id} is running. Stop it with this. It parks the telescope.</p>
          <pre className="cmd mono">{plan.commands.stop}</pre>
        </div>
      )}
      {confirming && (
        <>
          <div className="callout orange small" style={{ marginTop: 10 }}>{plan.confirm_warning}</div>
          {plan.commands?.run ? <pre className="cmd mono">{plan.commands.run}</pre> : null}
        </>
      )}
      <div className="row" style={{ marginTop: 10 }}>
        {plan.actions?.rehearse && <Btn kind="primary" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/plans/${plan.id}/rehearse`))}>Rehearse on simulated sky</Btn>}
        {plan.actions?.start && !confirming && (
          <Btn kind="danger" disabled={busy} onClick={() => act(async () => {
            const out = await apiPost(`/api/v1/plans/${plan.id}/start`, { confirm: false });
            if (out.needs_confirm) setAsk(plan.id);
            return out;
          })}>{plan.actions.start_word === "Resume" ? "Resume on the telescope…" : "Start on the telescope…"}</Btn>
        )}
        {confirming && (
          <>
            <Btn kind="danger" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/plans/${plan.id}/start`, { confirm: true }))}>Confirm start</Btn>
            <Btn disabled={busy} onClick={() => setAsk(null)}>Back</Btn>
          </>
        )}
        <span className="grow" />
        {plan.actions?.cancel && <Btn kind="ghost" disabled={busy} onClick={() => act(() => apiPost(`/api/v1/plans/${plan.id}/cancel`))}>Cancel plan</Btn>}
      </div>
      {(plan.actions?.rehearse || plan.actions?.start) && plan.commands && (
        <details className="why">
          <summary>Commands</summary>
          <div className="body">
            <p className="muted small">Rehearse on the simulated sky. The plan is left as it was.</p>
            <pre className="cmd mono">{plan.commands.rehearse}</pre>
            <p className="muted small">The real run. The same check as the button, from a terminal.</p>
            <pre className="cmd mono">{plan.commands.run}</pre>
            <p className="muted small">Stop. On a real telescope this parks the mount.</p>
            <pre className="cmd mono">{plan.commands.stop}</pre>
          </div>
        </details>
      )}
      {plan.log_tail ? (
        <details className="why">
          <summary>What that process printed</summary>
          <pre className="cmd mono">{plan.log_tail}</pre>
        </details>
      ) : null}
    </div>
  );
}
