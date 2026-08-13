/* FleetPulse Customer Tracking Portal.
 *
 * Public, read-only, no identity. The AWB number is the only credential --
 * exactly like every real courier tracking page.
 *
 * Deep-linkable: /track/?awb=FP123... loads straight into a result, so the
 * URL can go in a shipping-confirmation email.
 */

import { consignment, ApiError } from "./api.js";
import { $, badge, esc, fmtDateTime, money, pretty, show } from "./ui.js";

/* The rail the customer sees. Deliberately simpler than the internal state
   machine: multiple hub hops all collapse into one "In transit" step, because
   a customer does not care how many sort centres their parcel passed through. */
const RAIL = [
  { key: "MANIFESTED",          label: "Booked",           icon: "✓" },
  { key: "IN_TRANSIT",          label: "In transit",       icon: "→" },
  { key: "ARRIVED_AT_FACILITY", label: "At local facility", icon: "⚑" },
  { key: "OUT_FOR_DELIVERY",    label: "Out for delivery", icon: "⚡" },
  { key: "DELIVERED",           label: "Delivered",        icon: "✓" },
];

const HEADLINE = {
  MANIFESTED:          ["\u{1F4E6}", "Shipment booked",        "We have received the booking. Your parcel will be picked up soon."],
  IN_TRANSIT:          ["\u{1F69B}", "On the move",            "Your parcel is travelling between our facilities."],
  ARRIVED_AT_FACILITY: ["\u{1F3E2}", "At the local facility",  "Your parcel has arrived and is being prepared for delivery."],
  OUT_FOR_DELIVERY:    ["\u{1F6F5}", "Out for delivery",       "Your parcel is with a delivery executive today."],
  DELIVERED:           ["✅",     "Delivered",             "Your parcel has been delivered."],
  RTO:                 ["↩️", "Returning to sender", "Delivery was unsuccessful and the parcel is on its way back."],
};

const TONE = { DELIVERED: "ok", RTO: "bad", OUT_FOR_DELIVERY: "warn" };

const RECENT_KEY = "fleetpulse.recentAwbs";

// ---------------------------------------------------------------------------
async function track(awb, { pushUrl = true } = {}) {
  awb = awb.trim().toUpperCase();
  if (!awb) return;

  show($("#result"), false);
  show($("#error-box"), false);
  show($("#loading"), true);

  try {
    const [parcel, history] = await Promise.all([
      consignment.track(awb),
      consignment.history(awb),
    ]);
    render(parcel, history);
    rememberAwb(awb);
    if (pushUrl) {
      history_pushAwb(awb);
    }
  } catch (e) {
    const box = $("#error-box");
    box.textContent =
      e instanceof ApiError && e.status === 404
        ? `We could not find shipment ${awb}. Check the number and try again.`
        : `Something went wrong — ${e.message}`;
    show(box, true);
  } finally {
    show($("#loading"), false);
  }
}

function history_pushAwb(awb) {
  const url = new URL(location.href);
  url.searchParams.set("awb", awb);
  window.history.replaceState({}, "", url);
}

// ---------------------------------------------------------------------------
function render(p, h) {
  const status = p.current_status;
  const [icon, title, sub] = HEADLINE[status] || ["\u{1F4E6}", pretty(status), ""];
  const tone = TONE[status] || "";

  $("#status-hero").className = `status-hero ${tone}`;
  $("#status-icon").textContent = icon;
  $("#status-title").textContent = title;
  $("#status-sub").textContent = sub;

  renderRail(status);

  $("#r-awb").textContent = p.awb;
  $("#r-route").textContent = `${p.origin_hub}  →  ${p.destination_hub}`;
  $("#r-badge").innerHTML = badge(status);

  $("#r-details").innerHTML = `
    <div class="kv"><span>Sent by</span><b>${esc(p.merchant_name)}</b></div>
    <div class="kv"><span>Deliver to</span><b>${esc(p.consignee_name)}</b></div>
    <div class="kv"><span>Address</span><b>${esc(p.consignee_addr)}</b></div>
    <div class="kv"><span>Weight</span><b>${p.weight_grams} g</b></div>
    <div class="kv"><span>Payment</span><b>${
      p.payment_mode === "COD" ? `Cash on delivery · ${money(p.cod_amount)}` : "Prepaid"
    }</b></div>
    <div class="kv"><span>Booked</span><b>${fmtDateTime(p.created_at)}</b></div>`;

  $("#r-timeline").innerHTML = h.scans
    .map(
      (s, i) => `
      <li class="${i === 0 ? "latest" : "done"}">
        <div class="t-title">${esc(pretty(s.status))}</div>
        <div class="t-meta">
          ${fmtDateTime(s.scanned_at)}${s.hub_id ? ` · ${esc(s.hub_id)}` : ""}
          ${s.remarks ? `<br>${esc(s.remarks)}` : ""}
        </div>
      </li>`
    )
    .join("");

  $("#r-updated").textContent = fmtDateTime(p.updated_at);
  show($("#result"), true);
}

function renderRail(status) {
  // RTO leaves the happy path: show progress up to the failed delivery attempt
  // and mark that step red rather than pretending the parcel is still moving.
  const rto = status === "RTO";
  const idx = rto
    ? RAIL.findIndex((s) => s.key === "OUT_FOR_DELIVERY")
    : RAIL.findIndex((s) => s.key === status);

  $("#rail").innerHTML = RAIL.map((step, i) => {
    let cls = "";
    if (rto && i === idx) cls = "failed";
    else if (i < idx) cls = "done";
    else if (i === idx) cls = "current";

    const glyph = cls === "failed" ? "✕" : cls === "done" ? "✓" : step.icon;
    return `
      <li class="${cls}">
        <span class="dot">${glyph}</span>
        <span class="rail-label">${step.label}</span>
      </li>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// Recently tracked -- convenience only, stored locally, never sent anywhere.
// ---------------------------------------------------------------------------
function rememberAwb(awb) {
  let list = [];
  try {
    list = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
  } catch { /* corrupt or unavailable storage; start fresh */ }

  list = [awb, ...list.filter((a) => a !== awb)].slice(0, 5);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch { /* private browsing */ }
  renderRecent(list);
}

function renderRecent(list) {
  if (!list?.length) return show($("#recent-hint"), false);
  $("#recent-links").innerHTML = list
    .map((a) => `<button data-awb="${esc(a)}">${esc(a)}</button>`)
    .join("");
  $("#recent-links").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      $("#awb-input").value = b.dataset.awb;
      track(b.dataset.awb);
    })
  );
  show($("#recent-hint"), true);
}

// ---------------------------------------------------------------------------
$("#track-form").addEventListener("submit", (e) => {
  e.preventDefault();
  track($("#awb-input").value);
});

$("#refresh").addEventListener("click", () => track($("#awb-input").value, { pushUrl: false }));

// Boot: deep link first, then recent list.
try {
  renderRecent(JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"));
} catch { /* ignore */ }

const deepLink = new URLSearchParams(location.search).get("awb");
if (deepLink) {
  $("#awb-input").value = deepLink.toUpperCase();
  track(deepLink, { pushUrl: false });
}
