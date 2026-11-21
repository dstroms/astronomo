export type Prefs = { wording: "Simple" | "Technical"; clock: "12-hour" | "24-hour" };

export async function apiGet(path: string, prefs?: Prefs, extra?: Record<string, string | boolean>): Promise<any> {
  const u = new URL(path, window.location.origin);
  if (prefs) {
    u.searchParams.set("wording", prefs.wording);
    u.searchParams.set("clock", prefs.clock);
  }
  if (extra) {
    for (const [k, v] of Object.entries(extra)) u.searchParams.set(k, String(v));
  }
  const r = await fetch(u.toString());
  const text = await r.text();
  let body: any = text;
  try { body = text ? JSON.parse(text) : null; } catch { /* keep text */ }
  if (!r.ok) {
    const detail = body?.detail ?? text ?? r.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

export async function apiSend(path: string, method: string, body: unknown = {}): Promise<any> {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await r.text();
  let data: any = text;
  try { data = text ? JSON.parse(text) : null; } catch { /* keep text */ }
  if (!r.ok) {
    const detail = data?.detail ?? text ?? r.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

export const apiPost = (path: string, body: unknown = {}) => apiSend(path, "POST", body);
export const apiPut = (path: string, body: unknown = {}) => apiSend(path, "PUT", body);
