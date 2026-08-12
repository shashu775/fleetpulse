/* FleetPulse console.
 *
 * Vanilla JS on purpose -- no framework, no build step, no node_modules.
 * All requests are same-origin because nginx proxies both APIs under /api/,
 * so there is no CORS configuration anywhere in this project.
 */

const CONSIGNMENT = "/api/consignment/v1";
const DISPATCH = "/api/dispatch/v1";

const HUBS = ["HUB-BLR-01", "HUB-CHN-02", "HUB-HYD-01",
              "HUB-DEL-03", "HUB-MUM-01", "HUB-KOL-02"];

const STATUSES = ["MANIFESTED", "IN_TRANSIT", "ARRIVED_AT_FACILITY",
                  "OUT_FOR_DELIVERY", "DELIVERED", "RTO"];

// Which colour each status gets. Terminal-good is green, terminal-bad is red.
const BADGE = {
  DELIVERED: "ok",
  RTO: "bad",
  OUT_FOR_DELIVERY: "warn",
};

const $ = (id) => document.getElementById(id);

/** fetch + JSON with a readable error. Every call in this file goes through it. */
async function api(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body.detail) {
        // FastAPI 422 returns a list of field errors; flatten it.
        detail = Array.isArray(body.detail)
          ? body.detail.map((d) => `${d.loc?.slice(-1)}: ${d.msg}`).join(", ")
          : body.detail;
      }
    } catch { /* non-JSON body, keep the status line */ }
    throw new Error(detail);
  }
  return res.json();
}

const fmtDate = (iso) =>
  new Date(iso).toLocaleString(undefined, {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });

const pretty = (s) => s.replace(/_/g, " ");

function show(el, on = true) { el.hidden = !on; }

// ---------------------------------------------------------------------------
// Service health -- the dots in the header
// ---------------------------------------------------------------------------
async function pollHealth() {
  for (const name of ["consignment", "dispatch"]) {
    const el = $(`svc-${name}`);
    try {
      const r = await fetch(`/health/${name}`);
      el.className = r.ok ? "svc up" : "svc down";
    } catch {
      el.className = "svc down";
    }
  }
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------
async function loadStats() {
  try {
    const s = await api(`${CONSIGNMENT}/stats`);
    $("s-total").textContent = s.total;
    $("s-inflight").textContent = s.in_flight;
    $("s-today").textContent = s.booked_today;
    $("s-rate").textContent =
      s.delivery_success_rate === null ? "—" : `${s.delivery_success_rate}%`;
  } catch {
    // A stats failure must not blank the whole page.
    ["s-total", "s-inflight", "s-today", "s-rate"].forEach(
      (id) => ($(id).textContent = "—")
    );
  }
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("is-active"));
    tab.classList.add("is-active");
    $(`panel-${tab.dataset.panel}`).classList.add("is-active");

    if (tab.dataset.panel === "fleet") loadFleet();
    if (tab.dataset.panel === "recent") loadRecent();
  });
});

// ---------------------------------------------------------------------------
// Track
// ---------------------------------------------------------------------------
$("track-form").addEventListener("submit", (e) => {
  e.preventDefault();
  trackParcel($("track-awb").value.trim().toUpperCase());
});

async function trackParcel(awb) {
  if (!awb) return;
  show($("track-error"), false);
  show($("track-result"), false);

  try {
    // Two calls in parallel -- the parcel and its history.
    const [p, h] = await Promise.all([
      api(`${CONSIGNMENT}/waybills/${encodeURIComponent(awb)}`),
      api(`${CONSIGNMENT}/waybills/${encodeURIComponent(awb)}/history`),
    ]);

    $("r-awb").textContent = p.awb;
    $("r-route").textContent = `${p.origin_hub}  →  ${p.destination_hub}`;

    const badge = $("r-status");
    badge.textContent = pretty(p.current_status);
    badge.className = `badge ${BADGE[p.current_status] || ""}`;

    // Surfacing the cache result in the UI makes the Redis layer visible --
    // reload and watch MISS become HIT.
    $("r-cache").textContent = `cache: ${p._cache}`;

    $("r-merchant").textContent = p.merchant_name;
    $("r-consignee").textContent = p.consignee_name;
    $("r-addr").textContent = p.consignee_addr;
    $("r-phone").textContent = p.consignee_phone;
    $("r-weight").textContent = `${p.weight_grams} g`;
    $("r-payment").textContent =
      p.payment_mode === "COD" ? `COD ₹${p.cod_amount.toFixed(2)}` : "PREPAID";

    $("r-label").href = `${CONSIGNMENT}/waybills/${encodeURIComponent(p.awb)}/label`;

    $("r-timeline").innerHTML = h.scans
      .map(
        (s, i) => `
        <li class="${i === 0 ? "latest" : ""}">
          <div class="t-status">${pretty(s.status)}</div>
          <div class="t-meta">
            ${fmtDate(s.scanned_at)}${s.hub_id ? ` · ${s.hub_id}` : ""}
            ${s.remarks ? ` · ${s.remarks}` : ""}
          </div>
        </li>`
      )
      .join("");

    show($("track-result"));
    $("track-awb").value = p.awb;
  } catch (err) {
    $("track-error").textContent = `Could not track ${awb} — ${err.message}`;
    show($("track-error"));
  }
}

// ---------------------------------------------------------------------------
// Book
// ---------------------------------------------------------------------------
HUBS.forEach((h, i) => {
  $("origin-hub").add(new Option(h, h, i === 0, i === 0));
  $("dest-hub").add(new Option(h, h, i === 3, i === 3));
});

// COD amount only matters for COD shipments.
$("payment-mode").addEventListener("change", (e) => {
  $("cod-wrap").style.display = e.target.value === "COD" ? "" : "none";
});

$("book-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  show($("book-error"), false);
  show($("book-result"), false);

  const fd = new FormData(e.target);
  const body = Object.fromEntries(fd.entries());
  body.weight_grams = Number(body.weight_grams);
  body.cod_amount = body.payment_mode === "COD" ? Number(body.cod_amount || 0) : 0;

  if (body.origin_hub === body.destination_hub) {
    $("book-error").textContent = "Origin and destination hubs must differ.";
    show($("book-error"));
    return;
  }

  btn.disabled = true;
  btn.textContent = "Booking…";
  try {
    const r = await api(`${CONSIGNMENT}/waybills`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    $("book-result").innerHTML = `
      <div class="parcel-head" style="border:0;margin:0;padding:0">
        <div>
          <div class="awb">${r.awb}</div>
          <div class="route">Booked · ${pretty(r.status)}</div>
        </div>
        <div class="head-right">
          <button class="btn-ghost" onclick="goTrack('${r.awb}')">Track it</button>
          <a class="btn-ghost" target="_blank" rel="noopener"
             href="${CONSIGNMENT}/waybills/${r.awb}/label">Label ↗</a>
        </div>
      </div>`;
    show($("book-result"));
    loadStats();
  } catch (err) {
    $("book-error").textContent = `Booking failed — ${err.message}`;
    show($("book-error"));
  } finally {
    btn.disabled = false;
    btn.textContent = "Book shipment";
  }
});

/** Jump to the Track tab with an AWB pre-loaded. Used by buttons and table rows. */
window.goTrack = function (awb) {
  document.querySelector('.tab[data-panel="track"]').click();
  $("track-awb").value = awb;
  trackParcel(awb);
};

// ---------------------------------------------------------------------------
// Fleet
// ---------------------------------------------------------------------------
async function loadFleet() {
  const list = $("fleet-list");
  try {
    const { vehicles } = await api(`${DISPATCH}/vehicles`);
    show($("fleet-empty"), vehicles.length === 0);

    list.innerHTML = vehicles
      .map(
        (v) => `
        <div class="card">
          <h4>${v.vehicle_id}</h4>
          <div class="kv"><span>Latitude</span><b>${v.lat.toFixed(5)}</b></div>
          <div class="kv"><span>Longitude</span><b>${v.lon.toFixed(5)}</b></div>
          <div class="kv"><span>Speed</span><b>${v.speed_kmph} km/h</b></div>
          <div class="kv"><span>Runsheet</span><b>${v.runsheet_id || "—"}</b></div>
          <div class="kv"><span>Reported</span><b>${fmtDate(v.recorded_at)}</b></div>
          <div style="margin-top:10px">
            <a class="btn-ghost" target="_blank" rel="noopener"
               href="https://www.openstreetmap.org/?mlat=${v.lat}&mlon=${v.lon}#map=13/${v.lat}/${v.lon}">
              View on map ↗
            </a>
          </div>
        </div>`
      )
      .join("");
  } catch (err) {
    list.innerHTML = "";
    $("fleet-empty").innerHTML = `Could not load vehicles — ${err.message}`;
    show($("fleet-empty"));
  }
}
$("fleet-refresh").addEventListener("click", loadFleet);

// ---------------------------------------------------------------------------
// Recent
// ---------------------------------------------------------------------------
let activeStatus = null;

const filters = $("status-filters");
["All", ...STATUSES].forEach((s, i) => {
  const b = document.createElement("button");
  b.className = "chip" + (i === 0 ? " is-active" : "");
  b.textContent = i === 0 ? "All" : pretty(s);
  b.addEventListener("click", () => {
    filters.querySelectorAll(".chip").forEach((c) => c.classList.remove("is-active"));
    b.classList.add("is-active");
    activeStatus = i === 0 ? null : s;
    loadRecent();
  });
  filters.appendChild(b);
});

async function loadRecent() {
  const body = $("recent-body");
  const url = new URL(`${CONSIGNMENT}/waybills`, location.origin);
  url.searchParams.set("limit", "25");
  if (activeStatus) url.searchParams.set("status", activeStatus);

  try {
    const { waybills } = await api(url.pathname + url.search);
    show($("recent-empty"), waybills.length === 0);

    body.innerHTML = waybills
      .map(
        (w) => `
        <tr onclick="goTrack('${w.awb}')">
          <td class="awb-cell">${w.awb}</td>
          <td>${w.origin_hub} → ${w.destination_hub}</td>
          <td>${w.merchant_name}</td>
          <td>${w.consignee_name}</td>
          <td>${w.payment_mode === "COD" ? `COD ₹${w.cod_amount.toFixed(0)}` : "PREPAID"}</td>
          <td><span class="badge ${BADGE[w.current_status] || ""}">${pretty(w.current_status)}</span></td>
        </tr>`
      )
      .join("");
  } catch (err) {
    body.innerHTML = "";
    $("recent-empty").innerHTML = `Could not load parcels — ${err.message}`;
    show($("recent-empty"));
  }
}
$("recent-refresh").addEventListener("click", loadRecent);

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
pollHealth();
loadStats();
setInterval(pollHealth, 10000);
setInterval(loadStats, 15000);
