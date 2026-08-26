/* FleetPulse shared API client.
 *
 * Used by all four apps. Copied into each image at build time rather than
 * fetched at runtime, so no app depends on another being up.
 *
 * Every app is served through the gateway, which proxies both services under
 * one origin -- so these are same-origin requests and there is no CORS
 * anywhere in this project.
 */

export const API = {
  consignment: "/api/consignment/v1",
  dispatch: "/api/dispatch/v1",
};

/** Thrown for any non-2xx response. `status` lets callers branch on 404 etc. */
export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * fetch + JSON with readable errors.
 * FastAPI returns 422 as a list of field errors; we flatten that into a
 * sentence rather than showing the user a raw JSON blob.
 */
export async function request(url, options = {}) {
  let res;
  try {
    res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
  } catch (e) {
    throw new ApiError("Network error — is the backend running?", 0);
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body.detail) {
        detail = Array.isArray(body.detail)
          ? body.detail.map((d) => `${d.loc?.slice(-1)}: ${d.msg}`).join(", ")
          : body.detail;
      }
    } catch { /* non-JSON error body; keep the status line */ }
    throw new ApiError(detail, res.status);
  }

  return res.status === 204 ? null : res.json();
}

const get = (url) => request(url);
const post = (url, body) => request(url, { method: "POST", body: JSON.stringify(body) });
const patch = (url, body) => request(url, { method: "PATCH", body: JSON.stringify(body) });

const qs = (params) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, v);
  }
  const s = p.toString();
  return s ? `?${s}` : "";
};

// ---------------------------------------------------------------------------
// Consignment & Hub Service -- parcels, scans, labels
// ---------------------------------------------------------------------------
export const consignment = {
  book: (payload) => post(`${API.consignment}/waybills`, payload),
  track: (awb) => get(`${API.consignment}/waybills/${encodeURIComponent(awb)}`),
  history: (awb) => get(`${API.consignment}/waybills/${encodeURIComponent(awb)}/history`),
  list: (params = {}) => get(`${API.consignment}/waybills${qs(params)}`),
  stats: () => get(`${API.consignment}/stats`),
  scan: (payload) => post(`${API.consignment}/scans`, payload),
  setStatus: (awb, payload) =>
    patch(`${API.consignment}/waybills/${encodeURIComponent(awb)}/status`, payload),
  labelUrl: (awb) => `${API.consignment}/waybills/${encodeURIComponent(awb)}/label`,
};

// ---------------------------------------------------------------------------
// Fleet & Dispatch Service -- runsheets, GPS, delivery
// ---------------------------------------------------------------------------
export const dispatch = {
  // { hub_id, status } -- both optional. status defaults to ACTIVE server-side;
  // pass status: "ALL" to include drivers who have left. Not "" -- qs() below
  // strips empty values, so a blank never reaches the server.
  drivers: (params = {}) => get(`${API.dispatch}/drivers${qs(params)}`),
  hubs: () => get(`${API.dispatch}/hubs`),
  runsheets: (params = {}) => get(`${API.dispatch}/runsheets${qs(params)}`),
  runsheet: (id) => get(`${API.dispatch}/runsheets/${encodeURIComponent(id)}`),
  createRunsheet: (payload) => post(`${API.dispatch}/runsheets`, payload),
  gpsPing: (payload) => post(`${API.dispatch}/gps`, payload),
  vehicles: () => get(`${API.dispatch}/vehicles`),
  vehicleLocation: (id) => get(`${API.dispatch}/vehicles/${encodeURIComponent(id)}/location`),
  deliver: (payload) => post(`${API.dispatch}/delivery`, payload),
};

// ---------------------------------------------------------------------------
// Health -- powers the connection indicator in each app's header
// ---------------------------------------------------------------------------
export async function health(service) {
  try {
    const r = await fetch(`/health/${service}`);
    return r.ok;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Session
//
// NOT authentication. There is no login, no password and no token -- this only
// remembers which driver or hub you selected, so the app does not ask on every
// page load. Anyone can pick any identity.
//
// Real auth (OIDC, or JWTs issued by the backend with a role claim) is the
// natural next step; it belongs on the backend first, not here.
// ---------------------------------------------------------------------------
export const session = {
  get(key, fallback = null) {
    try {
      const v = localStorage.getItem(`fleetpulse.${key}`);
      return v ? JSON.parse(v) : fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`fleetpulse.${key}`, JSON.stringify(value));
    } catch { /* private browsing; degrade to per-page-load state */ }
  },
  clear(key) {
    try {
      localStorage.removeItem(`fleetpulse.${key}`);
    } catch { /* ignore */ }
  },
};
