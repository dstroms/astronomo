import { useEffect, useState } from "react";
import { apiGet, apiPost, apiPut } from "../api";
import { Btn } from "../components/ui";

export default function Settings() {
  const [data, setData] = useState<any>(null);
  const [name, setName] = useState("");
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [elev, setElev] = useState("0");
  const [horizon, setHorizon] = useState("");
  const [query, setQuery] = useState("");
  const [places, setPlaces] = useState<any[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const apply = (s: any) => {
    setData(s);
    const site = s.site || {};
    setName(site.name || "");
    setLat(String(site.lat ?? ""));
    setLon(String(site.lon ?? ""));
    setElev(String(site.elev_m ?? 0));
    setHorizon((site.horizon || []).map((p: number[]) => p.join(" ")).join("\n"));
  };

  useEffect(() => { apiGet("/api/v1/settings").then(apply).catch((e) => setErr(e.message)); }, []);

  const parseHorizon = () => horizon.trim().split("\n").filter(Boolean).map((line) => line.trim().split(/[\s,]+/).map(Number));

  return (
    <div className="full stack">
      <h1>Settings</h1>
      <p className="muted">The observing site is stored in this data directory as site.json. Environment variables, if set, win and cannot be edited here.</p>
      {err ? <div className="callout red small">{err}</div> : null}
      {msg ? <div className="callout green small">{msg}</div> : null}
      <div className="card stack">
        <div className="eyebrow">Observing site</div>
        <div className="field"><label>Name</label><input type="text" value={name} onChange={(e) => setName(e.target.value)} disabled={data?.site?.locked} /></div>
        <div className="row">
          <div className="field grow"><label>Latitude</label><input type="number" value={lat} onChange={(e) => setLat(e.target.value)} disabled={data?.site?.locked} /></div>
          <div className="field grow"><label>Longitude (east positive)</label><input type="number" value={lon} onChange={(e) => setLon(e.target.value)} disabled={data?.site?.locked} /></div>
          <div className="field grow"><label>Elevation (m)</label><input type="number" value={elev} onChange={(e) => setElev(e.target.value)} disabled={data?.site?.locked} /></div>
        </div>
        <div className="field">
          <label>Horizon (optional): one azimuth altitude pair per line</label>
          <textarea className="select" rows={4} value={horizon} onChange={(e) => setHorizon(e.target.value)} disabled={data?.site?.locked} />
        </div>
        <div className="row">
          <Btn kind="primary" disabled={data?.site?.locked} onClick={async () => {
            setErr(null); setMsg(null);
            try {
              await apiPut("/api/v1/settings/site", { name, lat: Number(lat), lon: Number(lon), elev_m: Number(elev), horizon: parseHorizon() });
              setMsg("Site saved.");
              apply(await apiGet("/api/v1/settings"));
            } catch (e: any) { setErr(e.message); }
          }}>Save site</Btn>
        </div>
        <div className="field">
          <label>Search for a place</label>
          <div className="row">
            <input type="text" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Town or observatory name" />
            <Btn onClick={async () => {
              setErr(null);
              try { setPlaces((await apiPost("/api/v1/settings/site/search", { query, count: 5 })).places || []); }
              catch (e: any) { setErr(e.message); }
            }}>Search</Btn>
          </div>
        </div>
        {places.map((p) => (
          <div key={p.label} className="row">
            <span className="grow">{p.label}</span>
            <Btn onClick={() => { setName(p.name); setLat(String(p.lat)); setLon(String(p.lon)); setElev(String(p.elev_m)); }}>Use this</Btn>
          </div>
        ))}
      </div>
      <div className="muted small">Data directory: {data?.data_dir} · database: {data?.db_path}</div>
      <p className="muted small">Astronomo is headless. This page is one client of the HTTP API.</p>
    </div>
  );
}
