import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { apiGet, type Prefs } from "./api";
import Mission from "./pages/Mission";
import Missions from "./pages/Missions";
import Opportunities from "./pages/Opportunities";
import Settings from "./pages/Settings";
import Tonight from "./pages/Tonight";

const PrefsCtx = createContext<{ prefs: Prefs; setPrefs: (p: Prefs) => void } | null>(null);
export const usePrefs = () => {
  const v = useContext(PrefsCtx);
  if (!v) throw new Error("prefs");
  return v;
};

function loadPrefs(): Prefs {
  return {
    wording: (localStorage.getItem("astronomo.wording") as Prefs["wording"]) || "Simple",
    clock: (localStorage.getItem("astronomo.clock") as Prefs["clock"]) || "12-hour",
  };
}

function Mark() {
  return <span className="mark" aria-hidden="true">🔭</span>;
}

function Seg({ options, value, onChange }: { options: string[]; value: string; onChange: (v: string) => void }) {
  return (
    <span className="seg">
      {options.map((o) => (
        <button key={o} type="button" className={o === value ? "on" : ""} onClick={() => onChange(o)}>{o}</button>
      ))}
    </span>
  );
}

export default function App() {
  const [prefs, setPrefsState] = useState<Prefs>(loadPrefs);
  const [shell, setShell] = useState<any>(null);
  const loc = useLocation();

  const setPrefs = (p: Prefs) => {
    setPrefsState(p);
    localStorage.setItem("astronomo.wording", p.wording);
    localStorage.setItem("astronomo.clock", p.clock);
  };

  useEffect(() => {
    let on = true;
    const tick = () => apiGet("/api/v1/shell", prefs).then((s) => on && setShell(s)).catch(() => on && setShell(null));
    tick();
    const id = setInterval(tick, (shell?.poll_s || 3) * 1000);
    return () => { on = false; clearInterval(id); };
  }, [prefs.wording, prefs.clock]);

  const path = loc.pathname;
  const nav = [
    { to: "/", label: "Tonight", match: path === "/" },
    { to: "/opportunities", label: "Opportunities", match: path.startsWith("/opportunities") },
    { to: "/missions", label: "Missions", match: path.startsWith("/missions") },
    { to: "/settings", label: "Settings", match: path.startsWith("/settings") },
  ];
  const clock = prefs.clock === "24-hour" ? shell?.clock?.display_24 : shell?.clock?.display_12;
  const n = shell?.attention_count || 0;

  return (
    <PrefsCtx.Provider value={{ prefs, setPrefs }}>
      <div className="topnav">
        <NavLink to="/" className="brand"><Mark />Astronomo</NavLink>
        {nav.map((item) => (
          <NavLink key={item.to} to={item.to} className={`nav ${item.match ? "on" : ""}`}>
            {item.label}
            {item.to === "/" && n > 0 ? <span className="count">{n}</span> : null}
          </NavLink>
        ))}
      </div>
      <div className="util">
        <span>{shell?.site?.line || "Site not loaded"}</span>
        <span className="mono">{clock || "—"}</span>
        <Seg options={["12-hour", "24-hour"]} value={prefs.clock} onChange={(v) => setPrefs({ ...prefs, clock: v as Prefs["clock"] })} />
        <span className="live">Live · every {shell?.poll_s || 3} s</span>
        <span className="sp" />
        <span>Wording</span>
        <Seg options={["Simple", "Technical"]} value={prefs.wording} onChange={(v) => setPrefs({ ...prefs, wording: v as Prefs["wording"] })} />
      </div>
      <Routes>
        <Route path="/" element={<Tonight />} />
        <Route path="/opportunities" element={<Opportunities />} />
        <Route path="/missions" element={<Missions />} />
        <Route path="/missions/:id" element={<Mission />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </PrefsCtx.Provider>
  );
}
