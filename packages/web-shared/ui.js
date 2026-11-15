/* Shared UI helpers. Small, dependency-free, used by every app. */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** Parcel status -> badge colour class. Terminal-good green, terminal-bad red. */
export const STATUS_CLASS = {
  MANIFESTED: "",
  IN_TRANSIT: "info",
  ARRIVED_AT_FACILITY: "info",
  OUT_FOR_DELIVERY: "warn",
  DELIVERED: "ok",
  RTO: "bad",
  PENDING: "",
};

/** Human-readable status: OUT_FOR_DELIVERY -> "Out for delivery". */
export function pretty(status) {
  if (!status) return "—";
  const s = status.replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function badge(status) {
  const cls = STATUS_CLASS[status] ?? "";
  return `<span class="badge ${cls}">${pretty(status)}</span>`;
}

export function fmtDateTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

export function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit", minute: "2-digit",
  });
}

export function money(n) {
  return `₹${Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

/** Escape untrusted text before putting it in innerHTML. */
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

export function show(el, on = true) {
  if (el) el.hidden = !on;
}

/** Transient message at the bottom of the screen. */
export function toast(message, kind = "info", ms = 3200) {
  let host = $("#toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 250);
  }, ms);
}

/** Header connection dots. Polls both services. */
export function wireHealthDots(healthFn, intervalMs = 10000) {
  async function poll() {
    for (const svc of ["consignment", "dispatch"]) {
      const el = $(`#svc-${svc}`);
      if (!el) continue;
      el.className = `svc ${(await healthFn(svc)) ? "up" : "down"}`;
    }
  }
  poll();
  setInterval(poll, intervalMs);
}

/** Run an async action while showing a button as busy. Always restores it. */
export async function withBusy(btn, label, fn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

/**
 * Signature pad on a <canvas>. Pointer events cover mouse, touch and stylus
 * with one code path, which matters because drivers use phones.
 */
export function signaturePad(canvas) {
  const ctx = canvas.getContext("2d");
  let drawing = false;
  let dirty = false;

  function resize() {
    // Match the backing store to the CSS size x DPR, or strokes look blurry
    // and land offset from the finger on high-density screens.
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    ctx.lineWidth = 2.2;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue("--text").trim() || "#000";
  }

  const pos = (e) => {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  canvas.addEventListener("pointerdown", (e) => {
    drawing = true;
    dirty = true;
    canvas.setPointerCapture(e.pointerId);
    const { x, y } = pos(e);
    ctx.beginPath();
    ctx.moveTo(x, y);
  });

  canvas.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    e.preventDefault();          // stop the page scrolling under the finger
    const { x, y } = pos(e);
    ctx.lineTo(x, y);
    ctx.stroke();
  });

  const stop = () => { drawing = false; };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointercancel", stop);
  canvas.addEventListener("pointerleave", stop);

  resize();
  window.addEventListener("resize", resize);

  return {
    isEmpty: () => !dirty,
    clear() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      dirty = false;
    },
    // JPEG at 0.6: a signature is line art, and this keeps the payload well
    // under the 200 KB the API accepts.
    toDataURL: () => canvas.toDataURL("image/jpeg", 0.6),
  };
}
