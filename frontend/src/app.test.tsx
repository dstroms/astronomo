import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "./App";

vi.mock("plotly.js-dist-min", () => ({
  default: { newPlot: vi.fn() },
}));

const soon = new Date(Date.now() + 60 * 60 * 1000).toISOString();

const SHELL = {
  site: { name: "Test Site", line: "Test Site 40.00N 105.00W" },
  clock: { display_12: "7:00 AM MST", display_24: "07:00 MST" },
  poll_s: 60,
  attention_count: 0,
  have_db: true,
};

const TONIGHT = {
  have_db: true,
  date: "19 Nov",
  empty: "",
  counts: { needs_you: 0, running: 0, waiting: 0, planned: 0 },
  attention: [],
  running: [],
  plans: { live: [], earlier: [] },
};

const MISSIONS = {
  missions: [
    {
      id: "m_done",
      title: "Field B",
      state: "COMPLETE",
      state_label: "Done",
      outcome: "1 unidentified",
      visits_done: 1,
      visits_total: 1,
      deliverables: "—",
      updated_ago: "1h",
      chips: [],
      look: { slot: "green", filled: true },
    },
  ],
  counts: { all: 1, needs_you: 0, active: 0, finished: 1 },
};

const MISSION = {
  row: {
    title: "Field B",
    chips: [],
    id: "m_done",
    state: "COMPLETE",
    describe_state: "Done",
    progress_text: "1 / 1 visits",
    fraction: 1,
  },
  mission: {
    id: "m_done",
    plugin_label: "Asteroid search",
    updated_ago: "1h",
    description: "Look for movers",
    hypothesis: "A rock may be moving",
  },
  summary: { verdict: "attention", found: "1 candidate", meaning: "not a discovery" },
  artifacts: { submissions: [], mission_logs: [], notice: "" },
  plan: { caption: "", text: "Watch Field B" },
  targets: [{ position: 1, name: "Field B", status: "OBSERVED", status_label: "Observed" }],
  evidence: {},
  activity: { logs: [], llm: { calls: 0, usage: null, items: [], provider: "none" } },
};

const OPPORTUNITIES = {
  kinds: [{ plugin: "transit_finder", label: "Transits", reachable: 1, n_candidates: 1 }],
  task_labels: { timing: "Transit timing" },
  opportunities: [
    {
      plugin: "transit_finder",
      id: "hatp23",
      name: "HAT-P-23 b",
      reachable: true,
      plugin_label: "Transits",
      task: "timing",
      task_label: "Transit timing",
      score: 1,
      impact_slot: "green",
      impact_label: "High",
      windows: [{ mid: soon, window_start: soon, label: "in an hour" }],
    },
  ],
};

const SETTINGS = {
  site: { name: "Test Site", lat: 40, lon: -105, elev_m: 1600, horizon: [], locked: false },
  data_dir: "/tmp/data",
  db_path: "/tmp/data/astronomo.sqlite",
};

function ok(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function pathnameOf(input: RequestInfo | URL): string {
  const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  return new URL(raw, "http://localhost").pathname;
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = pathnameOf(input);
      if (path === "/api/v1/shell") return ok(SHELL);
      if (path === "/api/v1/tonight") return ok(TONIGHT);
      if (path === "/api/v1/missions") return ok(MISSIONS);
      if (path === "/api/v1/missions/m_done") return ok(MISSION);
      if (path === "/api/v1/missions/m_done/frames") return ok({ frames: [] });
      if (path === "/api/v1/opportunities") return ok(OPPORTUNITIES);
      if (path.startsWith("/api/v1/opportunities/")) return ok({ altitude: [], photometry: null });
      if (path === "/api/v1/settings") return ok(SETTINGS);
      return new Response(JSON.stringify({ detail: path }), { status: 404 });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("Tonight renders without throwing", async () => {
  renderAt("/");
  expect(await screen.findByRole("heading", { name: "Tonight" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Astronomo/ })).toBeInTheDocument();
  expect(screen.getByText("Test Site 40.00N 105.00W")).toBeInTheDocument();
  expect(screen.getByText("Nothing is running.")).toBeInTheDocument();
});

test("Opportunities renders without throwing", async () => {
  renderAt("/opportunities");
  expect(await screen.findByRole("heading", { name: "Opportunities" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("HAT-P-23 b")).toBeInTheDocument());
});

test("Missions renders without throwing", async () => {
  renderAt("/missions");
  expect(await screen.findByRole("heading", { name: "Missions" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("Field B")).toBeInTheDocument());
});

test("a mission page renders without throwing", async () => {
  renderAt("/missions/m_done");
  expect(await screen.findByRole("heading", { name: "Field B" })).toBeInTheDocument();
  expect(screen.getByText("1 candidate")).toBeInTheDocument();
  expect(screen.getByText("The plan")).toBeInTheDocument();
});

test("Documents shows the findings summary with its caveats and checks", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = pathnameOf(input);
      if (path === "/api/v1/shell") return ok(SHELL);
      if (path === "/api/v1/missions/m_done") {
        return ok({
          ...MISSION,
          artifacts: {
            findings: [{
              id: 7, title: "Findings, 2026-11-21: variable_stars", status: "written", downloadable: true,
              filename: "findings_2026-11-21.md",
              rows: [{ target: "V1234 Lac", measured: "11.300 mag", error: "+/- 0.031 mag", compared: "eclipse on time" }],
              caveats: ["The magnitude is a camera green magnitude."],
              checks: ["Open each upload file and read it."],
            }],
            submissions: [], mission_logs: [], notice: "",
          },
        });
      }
      if (path === "/api/v1/missions/m_done/frames") return ok({ frames: [] });
      return new Response(JSON.stringify({ detail: path }), { status: 404 });
    }),
  );
  renderAt("/missions/m_done");
  expect(await screen.findByRole("heading", { name: "Field B" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "findings summary" })).toHaveAttribute("href", "/api/v1/artifacts/7");
  fireEvent.click(screen.getByRole("button", { name: "Documents" }));
  expect(screen.getByText("Findings summary")).toBeInTheDocument();
  expect(screen.getByText("+/- 0.031 mag")).toBeInTheDocument();
  expect(screen.getByText("The magnitude is a camera green magnitude.")).toBeInTheDocument();
  expect(screen.getByText("Open each upload file and read it.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Download findings_2026-11-21\.md/ })).toBeInTheDocument();
});

test("Process shows language-model traffic", async () => {
  renderAt("/missions/m_done");
  expect(await screen.findByRole("heading", { name: "Field B" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Process" }));
  expect(screen.getByText("Language model")).toBeInTheDocument();
  expect(screen.getByText(/No language-model calls/)).toBeInTheDocument();
});

test("Process lists each language-model prompt and response", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = pathnameOf(input);
      if (path === "/api/v1/shell") return ok(SHELL);
      if (path === "/api/v1/missions/m_done") {
        return ok({
          ...MISSION,
          activity: {
            logs: [],
            llm: {
              calls: 1,
              provider: "ollama",
              model: "llama3.1",
              usage: { calls: 1, prompt_tokens: 40, completion_tokens: 12, cached_calls: 0, max_prompt_chars: 80 },
              items: [{
                node: "generate_hypothesis", ok: true, cached: false,
                prompt: "DIGEST: field summary", response: "A mover may be present.",
                prompt_tokens: 40, completion_tokens: 12, latency_s: 0.4,
              }],
            },
          },
        });
      }
      if (path === "/api/v1/missions/m_done/frames") return ok({ frames: [] });
      return new Response(JSON.stringify({ detail: path }), { status: 404 });
    }),
  );
  renderAt("/missions/m_done");
  expect(await screen.findByRole("heading", { name: "Field B" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Process" }));
  expect(screen.getByText("Calls (paid)")).toBeInTheDocument();
  fireEvent.click(screen.getByText(/generate_hypothesis · ok/));
  expect(screen.getByText("DIGEST: field summary")).toBeInTheDocument();
  expect(screen.getByText("A mover may be present.")).toBeInTheDocument();
});

test("Settings renders without throwing", async () => {
  renderAt("/settings");
  expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
  expect(screen.getByDisplayValue("Test Site")).toBeInTheDocument();
  expect(screen.getByText(/one client of the HTTP API/)).toBeInTheDocument();
});
