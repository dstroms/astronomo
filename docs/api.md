# HTTP API

Astronomo is a **headless** pipeline. Missions run from the CLI (`python main.py`). Telemetry is SQLite.
This page is the contract for a frontend. The in-repo React client is served from this same process
when `frontend/dist` is present (it is shipped in the repository, so a checkout does not need Node).
Any other UI should talk to `/api/v1`.

The React sources live under `frontend/` for now; they may move out. Nothing in `core/` imports this
package.

Start the API from the project root:

```bash
python -m dashboard.api                 # http://localhost:8765 (opens the browser)
python -m dashboard.api --port 9000
```

`ASTRONOMO_API_HOST`, `ASTRONOMO_API_PORT` and `ASTRONOMO_API_CORS` (comma-separated origins, or `*`
by default) override the bind address. The process uses `ASTRONOMO_DATA_DIR` like every other
Astronomo command.

This same process serves `frontend/dist` at `/`. If that build is missing, `GET /` describes the API.
Interactive docs are at `/docs`. You only need Node to change the UI (`npm install && npm run build`
in `frontend/`).

## What this API will not do

- It does not talk to the telescope. A confirmed start runs `python main.py --from-plan`.
- It does not send upload files anywhere.
- A real start is two requests: the first returns `needs_confirm` and the warning; the second must
  send `{"confirm": true}`.

## Query flags

Most `GET` routes accept:

| Query | Values | Default |
|---|---|---|
| `wording` | `Simple` \| `Technical` | Simple |
| `clock` | `12-hour` \| `24-hour` | 12-hour |
| `simple` | `true` \| `false` | true (overridden by `wording`) |

Wording changes labels only. The underlying records are the same.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Liveness |
| GET | `/api/v1/shell` | Site, clock, attention count, poll interval |
| GET | `/api/v1/tonight` | Home: needs you, running, plans, conditions, files ready for review |
| GET | `/api/v1/missions` | Mission log |
| GET | `/api/v1/missions/{id}` | One mission, including plugin `evidence` JSON |
| GET | `/api/v1/missions/{id}/frames` | First-frame preview and later FITS frames |
| GET | `/api/v1/missions/{id}/frames/{i}` | Stretched PNG of that frame |
| GET | `/api/v1/missions/{id}/first-frame` | Live first-frame PNG |
| GET | `/api/v1/missions/{id}/live-frame` | Latest live PNG |
| GET | `/api/v1/missions/{id}/frame-watch` | Frame-watch figure |
| GET | `/api/v1/artifacts/{id}` | Download an upload file or mission log |
| GET | `/api/v1/opportunities` | Reachable targets (`?refresh=true` refetches lists) |
| POST | `/api/v1/opportunities/refresh` | Same as refresh=true |
| GET | `/api/v1/opportunities/{plugin}/{id}/track` | Altitude samples for a window (`?window_index=`) |
| POST | `/api/v1/opportunities/plan` | Save a plan (`plugin`, `opportunity_id`, `window_index`) |
| POST | `/api/v1/opportunities/forecast` | 7-day Open-Meteo hours (network) |
| POST | `/api/v1/plans/{id}/cancel` | Drop an open plan |
| POST | `/api/v1/plans/{id}/again` | Plan a finished window again |
| POST | `/api/v1/plans/{id}/rehearse` | `python main.py --from-plan --simulate` |
| POST | `/api/v1/plans/{id}/start` | Real start; requires `{"confirm": true}` |
| POST | `/api/v1/missions/{id}/plan-again` | Plan again from a mission that came from a plan |
| GET | `/api/v1/settings` | Data directory and observing site |
| PUT | `/api/v1/settings/site` | Write `site.json` (refused if the site is set from the environment) |
| POST | `/api/v1/settings/site/search` | Place search (`query`, optional `count`) |

Plugin Results are JSON from an optional `evidence()` hook (see [Writing a plugin](writing-a-plugin.md)).
Plotly figures travel as Plotly JSON under `plotly[].figure`.

## In-repo React client

`python -m dashboard.api` already serves the shipped build. To work on the UI:

```bash
cd frontend
npm install          # Node 18 or newer
npm run dev          # Vite on :5173, proxies /api to :8765
npm run build        # writes frontend/dist; commit it so a checkout still has a UI
```

You do not need Node to run Astronomo. Point any other UI at `/api/v1`.
