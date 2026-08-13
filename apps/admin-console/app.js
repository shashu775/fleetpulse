/* FleetPulse Admin Console.
 *
 * The operations view across the whole network, and the one place a runsheet
 * can be created without the simulator or a raw API call -- which was the only
 * break in the otherwise-clickable parcel lifecycle.
 *
 * Uses no endpoints of its own: everything here is already served by the two
 * services, which is a good sign the API surface was right.
 */

import { consignment, dispatch, health } from "./api.js";
import {
  $, badge, esc, fmtDateTime, money, pretty, show, toast, withBusy, wireHealthDots,
} from "./ui.js";

const HUBS = ["HUB-BLR-01", "HUB-CHN-02", "HUB-HYD-01",
              "HUB-DEL-03", "HUB-MUM-01", "HUB-KOL-02"];

const PIPELINE = ["MANIFESTED", "IN_TRANSIT", "ARRIVED_AT_FACILITY",
                  "OUT_FOR_DELIVERY", "DELIVERED", "RTO"];

// .localhost, not .local: RFC 6762 reserves .local for mDNS/Bonjour, so those
// lookups can bypass the hosts file entirely. RFC 6761 reserves .localhost for
// loopback, and browsers resolve *.localhost internally.
const APPS = [
  ["Launcher",          "fleetpulse.localhost",          "/",          "80"],
  ["Merchant Portal",   "merchant.fleetpulse.localhost", "/merchant/", "3001"],
  ["Hub Scanner",       "hub.fleetpulse.localhost",      "/hub/",      "3003"],
  ["Driver App",        "driver.fleetpulse.localhost",   "/driver/",   "3002"],
  ["Customer Tracking", "track.fleetpulse.localhost",    "/track/",    "3004"],
  ["Admin Console",     "admin.fleetpulse.localhost",    "/admin/",    "3005"],
];

const state = { parcels: [], selected: new Set() };

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("is-active"));
    tab.classList.add("is-active");
    $(`#panel-${tab.dataset.panel}`).classList.add("is-active");

    const load = {
      overview: loadOverview, parcels: loadParcels,
      runsheets: loadRunsheetTab, fleet: loadFleet,
    }[tab.dataset.panel];
    load?.();
  });
});

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------
async function loadOverview() {
  try {
    const [s, v] = await Promise.all([consignment.stats(), dispatch.vehicles()]);

    $("#s-total").textContent = s.total;
    $("#s-inflight").textContent = s.in_flight;
    $("#s-today").textContent = s.booked_today;
    $("#s-rate").textContent = s.delivery_success_rate === null ? "—" : `${s.delivery_success_rate}%`;
    $("#s-vehicles").textContent = v.count;

    // Bars are scaled to the biggest bucket, not the total -- otherwise with
    // 90% delivered every other bar is a sliver and tells you nothing.
    const max = Math.max(1, ...PIPELINE.map((k) => s.by_status[k] || 0));
    $("#pipeline").innerHTML = PIPELINE.map((k) => {
      const n = s.by_status[k] || 0;
      const cls = k === "DELIVERED" ? "ok" : k === "RTO" ? "bad"
                : k === "OUT_FOR_DELIVERY" ? "warn" : "";
      return `
        <div class="pipe-row">
          <span class="pipe-name">${esc(pretty(k))}</span>
          <div class="pipe-bar"><div class="pipe-fill ${cls}" style="width:${(n / max) * 100}%"></div></div>
          <span class="pipe-n">${n}</span>
        </div>`;
    }).join("");

    renderAttention(s);
  } catch (e) {
    toast(`Could not load overview — ${e.message}`, "bad");
  }
}

function renderAttention(s) {
  const items = [];
  const ready = s.by_status["ARRIVED_AT_FACILITY"] || 0;
  const rto = s.by_status["RTO"] || 0;
  const ofd = s.by_status["OUT_FOR_DELIVERY"] || 0;

  if (ready > 0) {
    items.push(`<div class="alert warn">
      <b>${ready}</b> parcel${ready === 1 ? "" : "s"} waiting at a facility with no driver assigned.
      <button class="btn ghost sm" style="margin-left:8px" onclick="document.querySelector('[data-panel=runsheets]').click()">
        Create a runsheet
      </button></div>`);
  }
  if (ofd > 0) {
    items.push(`<div class="alert">${ofd} out for delivery right now.</div>`);
  }
  if (rto > 0) {
    items.push(`<div class="alert">${rto} returning to sender (RTO).</div>`);
  }
  if (!items.length) {
    items.push(`<div class="alert ok">Nothing needs attention — every parcel is moving or complete.</div>`);
  }
  $("#attention").innerHTML = items.join("");
}

// ---------------------------------------------------------------------------
// Parcels
// ---------------------------------------------------------------------------
async function loadParcels() {
  const status = $("#parcel-status").value;
  try {
    const { waybills, total } = await consignment.list({ limit: 100, status });
    state.parcels = waybills;
    renderParcels();
    $("#parcel-count").textContent =
      `Showing ${waybills.length} of ${total}${status ? ` with status ${pretty(status)}` : ""}.`;
  } catch (e) {
    $("#parcel-body").innerHTML = "";
    $("#parcel-empty").textContent = `Could not load parcels — ${e.message}`;
    show($("#parcel-empty"), true);
  }
}

function renderParcels() {
  const q = $("#parcel-search").value.trim().toLowerCase();
  const rows = state.parcels.filter((w) =>
    !q ||
    w.awb.toLowerCase().includes(q) ||
    w.merchant_name.toLowerCase().includes(q) ||
    w.consignee_name.toLowerCase().includes(q)
  );

  show($("#parcel-empty"), rows.length === 0);
  if (!rows.length) $("#parcel-empty").textContent = "No parcels match that filter.";

  $("#parcel-body").innerHTML = rows
    .map((w) => `
      <tr>
        <td class="mono"><b>${esc(w.awb)}</b></td>
        <td>${esc(w.origin_hub)} → ${esc(w.destination_hub)}</td>
        <td>${esc(w.merchant_name)}</td>
        <td>${esc(w.consignee_name)}</td>
        <td>${w.payment_mode === "COD" ? money(w.cod_amount) : "Prepaid"}</td>
        <td>${badge(w.current_status)}</td>
        <td>
          <a class="btn ghost sm" href="/track/?awb=${esc(w.awb)}" target="_blank" rel="noopener">Track</a>
          <a class="btn ghost sm" href="${consignment.labelUrl(w.awb)}" target="_blank" rel="noopener">Label</a>
        </td>
      </tr>`)
    .join("");
}

$("#parcel-search").addEventListener("input", renderParcels);
$("#parcel-status").addEventListener("change", loadParcels);
$("#parcels-refresh").addEventListener("click", loadParcels);

// ---------------------------------------------------------------------------
// Runsheets -- creation is the point of this console
// ---------------------------------------------------------------------------
function initRunsheetForm() {
  if ($("#rs-hub").options.length) return;
  HUBS.forEach((h, i) => $("#rs-hub").add(new Option(h, h, i === 3, i === 3)));
}

async function loadRunsheetTab() {
  initRunsheetForm();
  await Promise.all([loadDriverOptions(), loadAvailableParcels(), loadRunsheetList()]);
}

async function loadDriverOptions() {
  const sel = $("#rs-driver");
  const keep = sel.value;
  try {
    const { drivers } = await dispatch.drivers();
    sel.innerHTML = drivers
      .map((d) => `<option value="${esc(d.driver_id)}"
                    data-name="${esc(d.driver_name)}" data-vehicle="${esc(d.vehicle_id)}">
                    ${esc(d.driver_name)} · ${esc(d.vehicle_id)}</option>`)
      .join("") + `<option value="__new">+ New driver…</option>`;
    if (keep) sel.value = keep;
  } catch {
    sel.innerHTML = `<option value="__new">+ New driver…</option>`;
  }
  toggleNewDriver();
}

function toggleNewDriver() {
  const isNew = $("#rs-driver").value === "__new";
  show($("#rs-newdriver-wrap"), isNew);
  show($("#rs-newvehicle-wrap"), isNew);
}
$("#rs-driver").addEventListener("change", toggleNewDriver);

/** Only ARRIVED_AT_FACILITY parcels can legally go OUT_FOR_DELIVERY. */
async function loadAvailableParcels() {
  const hub = $("#rs-hub").value;
  const host = $("#rs-available");
  state.selected.clear();
  updateSelectedCount();

  host.innerHTML = '<div class="spinner"></div>';
  try {
    const { waybills } = await consignment.list({ limit: 100, status: "ARRIVED_AT_FACILITY" });
    // The API filters by status; the destination hub is filtered here because
    // there is no hub query parameter yet.
    const ready = waybills.filter((w) => w.destination_hub === hub);

    show($("#rs-avail-empty"), ready.length === 0);
    $("#rs-empty-hub").textContent = hub;
    $("#rs-avail-label").textContent =
      `${ready.length} parcel${ready.length === 1 ? "" : "s"} ready at ${hub}`;

    host.innerHTML = ready
      .map((w) => `
        <label class="pick" data-awb="${esc(w.awb)}">
          <input type="checkbox" value="${esc(w.awb)}">
          <span class="info">
            <span class="a">${esc(w.awb)}</span>
            <span class="b">${esc(w.consignee_name)} · ${esc(w.consignee_addr)}</span>
          </span>
          ${w.payment_mode === "COD" ? `<span class="badge warn">${money(w.cod_amount)}</span>` : ""}
        </label>`)
      .join("");

    host.querySelectorAll('input[type=checkbox]').forEach((cb) =>
      cb.addEventListener("change", () => {
        cb.checked ? state.selected.add(cb.value) : state.selected.delete(cb.value);
        cb.closest(".pick").classList.toggle("is-on", cb.checked);
        updateSelectedCount();
      })
    );
  } catch (e) {
    host.innerHTML = `<div class="alert">Could not load parcels — ${esc(e.message)}</div>`;
  }
}

function updateSelectedCount() {
  const n = state.selected.size;
  $("#rs-selected-count").textContent = `${n} selected`;
  $("#rs-create").disabled = n === 0;
}

$("#rs-hub").addEventListener("change", loadAvailableParcels);
$("#rs-all").addEventListener("click", () => {
  $("#rs-available").querySelectorAll('input[type=checkbox]').forEach((cb) => {
    cb.checked = true;
    state.selected.add(cb.value);
    cb.closest(".pick").classList.add("is-on");
  });
  updateSelectedCount();
});
$("#rs-none").addEventListener("click", () => {
  $("#rs-available").querySelectorAll('input[type=checkbox]').forEach((cb) => {
    cb.checked = false;
    cb.closest(".pick").classList.remove("is-on");
  });
  state.selected.clear();
  updateSelectedCount();
});

$("#rs-create").addEventListener("click", async (e) => {
  const sel = $("#rs-driver");
  const isNew = sel.value === "__new";
  const opt = sel.selectedOptions[0];

  const payload = {
    driver_id: isNew ? `DRV-${Math.floor(1000 + Math.random() * 9000)}` : sel.value,
    driver_name: isNew ? $("#rs-driver-name").value.trim() : opt.dataset.name,
    vehicle_id: isNew ? $("#rs-vehicle").value.trim().toUpperCase() : opt.dataset.vehicle,
    hub_id: $("#rs-hub").value,
    awbs: [...state.selected],
  };

  if (!payload.driver_name || !payload.vehicle_id) {
    return toast("Enter a driver name and vehicle number", "bad");
  }

  await withBusy(e.target, "Creating…", async () => {
    try {
      const r = await dispatch.createRunsheet(payload);
      const failed = r.failed.length;
      $("#rs-result").innerHTML = `
        <div class="alert ${failed ? "warn" : "ok"}">
          Created <b class="mono">${esc(r.runsheet_id)}</b> for ${esc(r.driver)} —
          <b>${r.assigned.length}</b> assigned${failed ? `, <b>${failed}</b> failed` : ""}.
          ${failed ? r.failed.map((f) => `<div class="small">${esc(f.awb)}: ${esc(f.error)}</div>`).join("") : ""}
          <div class="small" style="margin-top:6px">
            Open the <a href="/driver/" target="_blank" rel="noopener">Driver app</a> to work it.
          </div>
        </div>`;
      show($("#rs-result"), true);
      toast(`Runsheet ${r.runsheet_id} created`, "ok");
      await Promise.all([loadAvailableParcels(), loadRunsheetList(), loadDriverOptions()]);
    } catch (ex) {
      $("#rs-result").innerHTML = `<div class="alert">${esc(ex.message)}</div>`;
      show($("#rs-result"), true);
    }
  });
});

async function loadRunsheetList() {
  const host = $("#rs-list");
  try {
    const { runsheets } = await dispatch.runsheets({ limit: 30 });
    show($("#rs-list-empty"), runsheets.length === 0);
    host.innerHTML = runsheets
      .map((r) => {
        const s = r.stops;
        return `
        <div class="rs-row">
          <div>
            <div class="id">${esc(r.runsheet_id)}</div>
            <div class="meta">${esc(r.driver_name)} · ${esc(r.vehicle_id)} · ${esc(r.hub_id)} · ${fmtDateTime(r.created_at)}</div>
          </div>
          <div class="chips">
            <span class="badge">${s.total} stops</span>
            ${s.pending ? `<span class="badge warn">${s.pending} pending</span>` : ""}
            ${s.delivered ? `<span class="badge ok">${s.delivered} delivered</span>` : ""}
            ${s.rto ? `<span class="badge bad">${s.rto} RTO</span>` : ""}
          </div>
        </div>`;
      })
      .join("");
  } catch (e) {
    host.innerHTML = `<div class="alert">Could not load runsheets — ${esc(e.message)}</div>`;
  }
}
$("#rs-refresh").addEventListener("click", loadRunsheetList);

// ---------------------------------------------------------------------------
// Fleet
// ---------------------------------------------------------------------------
async function loadFleet() {
  const host = $("#fleet-cards");
  try {
    const { vehicles } = await dispatch.vehicles();
    show($("#fleet-empty"), vehicles.length === 0);
    host.innerHTML = vehicles
      .map((v) => `
        <div class="card">
          <h4 class="mono" style="margin:0 0 10px">${esc(v.vehicle_id)}</h4>
          <div class="kv"><span>Latitude</span><b>${v.lat.toFixed(5)}</b></div>
          <div class="kv"><span>Longitude</span><b>${v.lon.toFixed(5)}</b></div>
          <div class="kv"><span>Speed</span><b>${v.speed_kmph} km/h</b></div>
          <div class="kv"><span>Runsheet</span><b class="mono small">${esc(v.runsheet_id || "—")}</b></div>
          <div class="kv"><span>Reported</span><b>${fmtDateTime(v.recorded_at)}</b></div>
          <a class="btn ghost sm" style="margin-top:10px" target="_blank" rel="noopener"
             href="https://www.openstreetmap.org/?mlat=${v.lat}&mlon=${v.lon}#map=13/${v.lat}/${v.lon}">Map ↗</a>
        </div>`)
      .join("");
  } catch (e) {
    host.innerHTML = "";
    $("#fleet-empty").textContent = `Could not load vehicles — ${e.message}`;
    show($("#fleet-empty"), true);
  }
}
$("#fleet-refresh").addEventListener("click", loadFleet);

// ---------------------------------------------------------------------------
// Apps tab
// ---------------------------------------------------------------------------
$("#apps-body").innerHTML = APPS.map(([name, host, path, port]) => `
  <tr>
    <td><b>${esc(name)}</b></td>
    <td><a href="http://${esc(host)}/" target="_blank" rel="noopener" class="mono">${esc(host)}</a></td>
    <td><a href="${esc(path)}" class="mono">${esc(path)}</a></td>
    <td><a href="http://localhost:${esc(port)}/" target="_blank" rel="noopener" class="mono">:${esc(port)}</a></td>
  </tr>`).join("");

const HOSTS_CMD =
  `Add-Content -Path $env:WINDIR\\System32\\drivers\\etc\\hosts -Value @"\n` +
  APPS.map(([, h]) => `127.0.0.1 ${h}`).join("\n") +
  `\n"@`;
$("#hosts-cmd").textContent = HOSTS_CMD;

$("#copy-hosts").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(HOSTS_CMD);
    toast("Copied — run it in an Administrator PowerShell", "ok");
  } catch {
    toast("Copy failed; select the text manually", "bad");
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
wireHealthDots(health);
loadOverview();
setInterval(() => {
  if ($("#panel-overview").classList.contains("is-active")) loadOverview();
}, 15000);
