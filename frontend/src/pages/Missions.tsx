import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiGet } from "../api";
import { usePrefs } from "../App";
import { Badge, Dot } from "../components/ui";

const FILTERS = ["All", "Active", "Needs you", "Done", "Failed"] as const;

export default function Missions() {
  const { prefs } = usePrefs();
  const [data, setData] = useState<any>(null);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("All");
  const nav = useNavigate();

  useEffect(() => {
    const load = () => apiGet("/api/v1/missions", prefs).then(setData).catch(() => setData(null));
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [prefs.wording, prefs.clock]);

  if (!data) return <div className="full muted">Loading…</div>;
  const rows = (data.missions || []).filter((m: any) => {
    if (filter === "Active") return m.active;
    if (filter === "Needs you") return m.pinned || m.lifecycle === "attention";
    if (filter === "Done") return m.state === "COMPLETE";
    if (filter === "Failed") return m.state === "FAILED";
    return true;
  });
  const c = data.counts || {};
  return (
    <div className="full stack">
      <div className="row">
        <h1 className="grow">Missions</h1>
        <span className="muted small">{c.all || 0} missions · {c.needs_you || 0} needs you · {c.active || 0} active · {c.finished || 0} finished</span>
      </div>
      <div className="chips">
        {FILTERS.map((f) => (
          <button key={f} type="button" className={`chip ${filter === f ? "on" : ""}`} onClick={() => setFilter(f)}>{f}</button>
        ))}
      </div>
      <div className="tablewrap">
        <table>
          <thead>
            <tr><th style={{ width: 130 }}>Status</th><th>Mission</th><th>Outcome</th><th style={{ width: 62 }}>Visits</th><th style={{ width: 150 }}>Deliverables</th><th style={{ width: 84 }}>Updated</th></tr>
          </thead>
          <tbody>
            {rows.map((m: any) => (
              <tr key={m.id} className={`clickable ${m.pinned ? "pin" : ""}`} onClick={() => nav(`/missions/${m.id}`)}>
                <td><Dot slot={m.look?.slot} filled={m.look?.filled} />{m.state_label}</td>
                <td>
                  <b>{m.title}</b>
                  {m.chips?.filter((c: any) => c.slot === "violet").map((c: any, i: number) => (
                    <span key={i}> <Badge slot="violet">{c.label}</Badge></span>
                  ))}
                </td>
                <td className="muted">{m.outcome}</td>
                <td className="mono">{m.visits_done} / {m.visits_total}</td>
                <td className="muted">{m.deliverables}</td>
                <td className="muted">{m.updated_ago}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="muted small">Attention rows stay at the top. Click a row to open the mission.</div>
    </div>
  );
}
