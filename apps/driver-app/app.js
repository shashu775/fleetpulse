/* FleetPulse Driver App.
 *
 * Flow:  pick driver -> pick runsheet -> work the stops
 *          -> "Out for delivery" scan  (consignment: ARRIVED_AT_FACILITY -> OUT_FOR_DELIVERY)
 *          -> POD capture              (dispatch:    DELIVERED | RTO)
 *
 * GPS runs on a timer while a runsheet is open, mirroring a real driver app
 * that reports position continuously.
 */

import { consignment, dispatch, health, session, ApiError } from "./api.js";
import {
  $, badge, esc, fmtDateTime, money, pretty, show, signaturePad, toast,
  withBusy, wireHealthDots,
} from "./ui.js";

const GPS_INTERVAL_MS = 15000;

const state = {
  driver: null,       // { driver_id, driver_name, vehicle_id }
  runsheet: null,     // full runsheet detail
  gpsTimer: null,
  pod: { awb: null, outcome: "DELIVERED", type: "OTP" },
};

let sigPad = null;

// ---------------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------------
function showView(name) {
  for (const v of ["login", "runsheets", "stops"]) {
    show($(`#view-${v}`), v === name);
  }
  show($("#switch-driver"), name !== "login");
}

// ---------------------------------------------------------------------------
// 1. Driver picker (stands in for authentication)
// ---------------------------------------------------------------------------
async function loadDrivers() {
  const host = $("#driver-list");
  host.innerHTML = '<div class="spinner"></div>';
  try {
    const { drivers } = await dispatch.drivers();
    show($("#no-drivers"), drivers.length === 0);

    // Grouped by hub: the roster is 18 drivers across 6 hubs, and a flat list
    // of 18 names makes a driver hunt for their own. The API already returns
    // them ordered by hub_id, so a single pass builds the groups.
    const byHub = new Map();
    for (const d of drivers) {
      if (!byHub.has(d.hub_id)) byHub.set(d.hub_id, []);
      byHub.get(d.hub_id).push(d);
    }

    host.innerHTML = [...byHub.entries()].map(([hub, list]) => `
      <div class="section-label">${esc(hub)}</div>
      ${list.map((d) => `
        <button class="driver-btn" data-id="${esc(d.driver_id)}"
                data-name="${esc(d.driver_name)}" data-vehicle="${esc(d.vehicle_id)}">
          <span>
            ${esc(d.driver_name)}
            <div class="meta">${esc(d.driver_id)} · ${esc(d.vehicle_id)}</div>
          </span>
          <span class="badge">${d.runsheets} runsheet${d.runsheets === 1 ? "" : "s"}</span>
        </button>`).join("")}
    `).join("");

    host.querySelectorAll(".driver-btn").forEach((b) =>
      b.addEventListener("click", () =>
        signIn({
          driver_id: b.dataset.id,
          driver_name: b.dataset.name,
          vehicle_id: b.dataset.vehicle,
        })
      )
    );
  } catch (e) {
    host.innerHTML = `<div class="alert">Could not load drivers — ${esc(e.message)}</div>`;
  }
}

function signIn(driver) {
  state.driver = driver;
  session.set("driver", driver);
  $("#driver-label").textContent = `${driver.driver_name} · ${driver.vehicle_id}`;
  showView("runsheets");
  loadRunsheets();
}

$("#switch-driver").addEventListener("click", () => {
  stopGps();
  // The POD modal is not one of the views showView() toggles, so it outlives a
  // view change. Left open over the driver picker it offers a Confirm button
  // that dereferences the state.runsheet being nulled on the next line.
  closePod();
  state.driver = null;
  state.runsheet = null;
  session.clear("driver");
  $("#driver-label").textContent = "Not signed in";
  showView("login");
  loadDrivers();
});

// ---------------------------------------------------------------------------
// 2. Runsheets
// ---------------------------------------------------------------------------
async function loadRunsheets() {
  const host = $("#runsheet-list");
  host.innerHTML = '<div class="spinner"></div>';
  try {
    const { runsheets } = await dispatch.runsheets({ driver_id: state.driver.driver_id });
    show($("#no-runsheets"), runsheets.length === 0);

    host.innerHTML = runsheets
      .map((r) => {
        const s = r.stops;
        // One bar segment per stop: an at-a-glance progress read.
        const bars = Array.from({ length: s.total }, (_, i) => {
          if (i < s.delivered) return '<span class="done"></span>';
          if (i < s.delivered + s.rto) return '<span class="rto"></span>';
          return "<span></span>";
        }).join("");

        return `
          <button class="rs-card" data-id="${esc(r.runsheet_id)}">
            <div class="card-head">
              <div>
                <div class="rs-id">${esc(r.runsheet_id)}</div>
                <div class="rs-meta">${esc(r.hub_id)} · ${fmtDateTime(r.created_at)}</div>
              </div>
              ${badge(s.pending === 0 && s.total > 0 ? "DELIVERED" : "OUT_FOR_DELIVERY")}
            </div>
            <div class="progress">${bars || '<span></span>'}</div>
            <div class="counts">
              <span><b>${s.pending}</b> pending</span>
              <span class="muted"><b>${s.delivered}</b> delivered</span>
              <span class="muted"><b>${s.rto}</b> RTO</span>
            </div>
          </button>`;
      })
      .join("");

    host.querySelectorAll(".rs-card").forEach((c) =>
      c.addEventListener("click", () => openRunsheet(c.dataset.id))
    );
  } catch (e) {
    host.innerHTML = `<div class="alert">Could not load runsheets — ${esc(e.message)}</div>`;
  }
}

$("#refresh-runsheets").addEventListener("click", loadRunsheets);
$("#back-to-runsheets").addEventListener("click", () => {
  stopGps();
  showView("runsheets");
  loadRunsheets();
});

// ---------------------------------------------------------------------------
// 3. Stops
// ---------------------------------------------------------------------------
async function openRunsheet(id) {
  showView("stops");
  $("#stop-list").innerHTML = '<div class="spinner"></div>';
  try {
    state.runsheet = await dispatch.runsheet(id);
    renderRunsheet();
  } catch (e) {
    $("#stop-list").innerHTML = `<div class="alert">Could not open runsheet — ${esc(e.message)}</div>`;
  }
}

function renderRunsheet() {
  const r = state.runsheet;
  const pending = r.stops.filter((s) => s.status === "PENDING").length;

  $("#runsheet-summary").innerHTML = `
    <div class="card-head">
      <div>
        <div class="rs-id">${esc(r.runsheet_id)}</div>
        <div class="rs-meta">${esc(r.driver_name)} · ${esc(r.vehicle_id)} · ${esc(r.hub_id)}</div>
      </div>
      <span class="badge ${pending === 0 ? "ok" : "warn"}">${pending} left</span>
    </div>`;

  $("#stop-list").innerHTML = r.stops.length
    ? r.stops.map(renderStop).join("")
    : '<div class="empty">This runsheet has no stops.</div>';

  // Wire up per-stop buttons after the HTML is in the DOM.
  $("#stop-list").querySelectorAll("[data-action]").forEach((btn) => {
    const { action, awb } = btn.dataset;
    if (action === "out") btn.addEventListener("click", () => markOutForDelivery(awb, btn));
    if (action === "pod") btn.addEventListener("click", () => openPod(awb));
  });
}

function renderStop(s) {
  const done = s.status !== "PENDING";
  const isOut = s.parcel_status === "OUT_FOR_DELIVERY";

  // Only offer the action that is actually legal from the parcel's current
  // state -- the backend enforces this with a 409, but showing an impossible
  // button and then erroring is a poor experience.
  const actions = done
    ? ""
    : `<div class="stop-actions">
         ${isOut
           ? ""
           : `<button class="btn" data-action="out" data-awb="${esc(s.awb)}">Scan out for delivery</button>`}
         <button class="btn ${isOut ? "ok" : "ghost"}" data-action="pod" data-awb="${esc(s.awb)}"
                 ${isOut ? "" : "disabled title='Scan out for delivery first'"}>
           Complete delivery
         </button>
       </div>`;

  const cod =
    s.payment_mode === "COD"
      ? `<div class="cod-flag">COLLECT ${money(s.cod_amount)}</div>`
      : "";

  const phone = s.consignee_phone
    ? ` · <a class="tel" href="tel:${esc(s.consignee_phone)}">${esc(s.consignee_phone)}</a>`
    : "";

  return `
    <div class="stop ${done ? "is-done" : ""}">
      <div class="stop-head">
        <div style="display:flex;gap:10px;min-width:0">
          <span class="stop-seq">${s.sequence}</span>
          <div style="min-width:0">
            <div class="stop-awb">${esc(s.awb)}</div>
            <div class="stop-name">${esc(s.consignee_name || "—")}</div>
            <div class="stop-addr">${esc(s.consignee_addr || "Address unavailable")}${phone}</div>
          </div>
        </div>
        ${badge(done ? s.status : s.parcel_status || "PENDING")}
      </div>
      ${cod}
      ${s.enrichment_error ? `<div class="small muted" style="margin-top:6px">⚠ ${esc(s.enrichment_error)}</div>` : ""}
      ${actions}
    </div>`;
}

/** ARRIVED_AT_FACILITY -> OUT_FOR_DELIVERY, via consignment (the state owner). */
async function markOutForDelivery(awb, btn) {
  await withBusy(btn, "Scanning…", async () => {
    try {
      await consignment.setStatus(awb, {
        status: "OUT_FOR_DELIVERY",
        hub_id: state.runsheet.hub_id,
        remarks: `Loaded by ${state.driver.driver_name} (${state.runsheet.runsheet_id})`,
      });
      toast(`${awb} out for delivery`, "ok");
      await openRunsheet(state.runsheet.runsheet_id);
    } catch (e) {
      // A 409 here means the parcel is not where the driver thinks it is --
      // show the reason verbatim, it is the most useful thing we know.
      toast(e instanceof ApiError && e.status === 409 ? e.message : `Failed: ${e.message}`, "bad");
    }
  });
}

// ---------------------------------------------------------------------------
// Proof of delivery
// ---------------------------------------------------------------------------
function openPod(awb) {
  state.pod = { awb, outcome: "DELIVERED", type: "OTP" };
  $("#pod-awb").textContent = awb;
  $("#pod-otp").value = "";
  $("#pod-receiver").value = "";
  show($("#pod-error"), false);
  syncPodUi();
  show($("#pod-modal"));

  // The canvas must be visible before sizing it, or getBoundingClientRect
  // returns zero and strokes land in the wrong place.
  if (!sigPad) sigPad = signaturePad($("#sig-canvas"));
  sigPad.clear();
}

function closePod() { show($("#pod-modal"), false); }

function syncPodUi() {
  const delivered = state.pod.outcome === "DELIVERED";
  show($("#pod-delivered"), delivered);
  show($("#pod-rto"), !delivered);
  show($("#pod-otp-block"), delivered && state.pod.type === "OTP");
  show($("#pod-sig-block"), delivered && state.pod.type === "SIGNATURE");
  $("#pod-submit").className = `btn ${delivered ? "ok" : "bad"}`;
  $("#pod-submit").textContent = delivered ? "Confirm delivery" : "Confirm return";
}

$("#pod-outcome").addEventListener("click", (e) => {
  const b = e.target.closest(".seg-btn");
  if (!b) return;
  $("#pod-outcome").querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("is-active"));
  b.classList.add("is-active");
  state.pod.outcome = b.dataset.outcome;
  syncPodUi();
});

$("#pod-type").addEventListener("click", (e) => {
  const b = e.target.closest(".seg-btn");
  if (!b) return;
  $("#pod-type").querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("is-active"));
  b.classList.add("is-active");
  state.pod.type = b.dataset.type;
  syncPodUi();
});

$("#sig-clear").addEventListener("click", () => sigPad?.clear());
$("#pod-cancel").addEventListener("click", closePod);
$("#pod-modal").addEventListener("click", (e) => {
  if (e.target.id === "pod-modal") closePod();   // click the backdrop to dismiss
});

$("#pod-submit").addEventListener("click", async (e) => {
  const { awb, outcome, type } = state.pod;
  const err = $("#pod-error");
  show(err, false);

  // Also reachable when openRunsheet() failed and left state.runsheet unset.
  // Without this the next line throws inside an async listener, which surfaces
  // as an unhandled rejection -- a Confirm button that silently does nothing.
  if (!state.runsheet) {
    err.textContent = "No runsheet open — reopen it and try again.";
    return show(err);
  }

  const payload = { awb, runsheet_id: state.runsheet.runsheet_id, outcome };

  if (outcome === "DELIVERED") {
    payload.pod_type = type;
    payload.pod_receiver = $("#pod-receiver").value.trim() || null;
    payload.reason = "Handed to consignee";

    if (type === "OTP") {
      const otp = $("#pod-otp").value.trim();
      if (!/^\d{4,6}$/.test(otp)) {
        err.textContent = "Enter the 4–6 digit OTP the consignee gave you.";
        return show(err);
      }
      payload.pod_data = otp;
    } else {
      if (sigPad.isEmpty()) {
        err.textContent = "Ask the consignee to sign in the box.";
        return show(err);
      }
      payload.pod_data = sigPad.toDataURL();
    }
  } else {
    payload.reason = $("#rto-reason").value;
    payload.pod_type = null;
  }

  await withBusy(e.target, "Saving…", async () => {
    try {
      await dispatch.deliver(payload);
      toast(`${awb} marked ${pretty(outcome)}`, "ok");
      closePod();
      await openRunsheet(state.runsheet.runsheet_id);
    } catch (ex) {
      // 207 = the attempt saved but consignment could not be updated. The
      // driver's work IS recorded, so say that rather than implying failure.
      if (ex instanceof ApiError && ex.status === 207) {
        toast("Saved locally — parcel status will reconcile shortly", "info");
        closePod();
        await openRunsheet(state.runsheet.runsheet_id);
        return;
      }
      err.textContent = ex.message;
      show(err);
    }
  });
});

// ---------------------------------------------------------------------------
// GPS
// ---------------------------------------------------------------------------
$("#gps-toggle").addEventListener("change", (e) =>
  e.target.checked ? startGps() : stopGps()
);

function startGps() {
  sendPing();
  state.gpsTimer = setInterval(sendPing, GPS_INTERVAL_MS);
  $("#gps-status").textContent = "On — reporting every 15s";
}

function stopGps() {
  if (state.gpsTimer) clearInterval(state.gpsTimer);
  state.gpsTimer = null;
  const t = $("#gps-toggle");
  if (t) t.checked = false;
  const s = $("#gps-status");
  if (s) s.textContent = "Off — positions are not being sent";
}

async function sendPing() {
  if (!state.driver || !state.runsheet) return;

  // Use the real device position when the browser allows it; otherwise fall
  // back to a jittered hub location so the feature is still demonstrable on a
  // desktop with geolocation denied.
  const send = async (lat, lon, speed) => {
    try {
      await dispatch.gpsPing({
        vehicle_id: state.driver.vehicle_id,
        lat, lon,
        speed_kmph: Math.round(speed * 10) / 10,
        runsheet_id: state.runsheet.runsheet_id,
      });
      $("#gps-status").textContent =
        `On — last ping ${new Date().toLocaleTimeString()} (${lat.toFixed(4)}, ${lon.toFixed(4)})`;
    } catch (e) {
      $("#gps-status").textContent = `Ping failed — ${e.message}`;
    }
  };

  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(
      (p) => send(p.coords.latitude, p.coords.longitude, (p.coords.speed || 0) * 3.6),
      () => send(12.9716 + rand(), 77.5946 + rand(), 20 + Math.random() * 25),
      { timeout: 5000, maximumAge: 10000 }
    );
  } else {
    send(12.9716 + rand(), 77.5946 + rand(), 20 + Math.random() * 25);
  }
}

const rand = () => (Math.random() - 0.5) * 0.05;

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
wireHealthDots(health);

const saved = session.get("driver");
if (saved?.driver_id) {
  signIn(saved);
} else {
  showView("login");
  loadDrivers();
}
