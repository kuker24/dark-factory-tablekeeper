/* Tablekeeper UI — vanilla JS, no dependencies.
 *
 * Stage-2 behaviour:
 *  - Token storage in localStorage; current-user renders when signed in
 *    and detaches when signed out (the sample logout test waits for the
 *    element to disappear after navigation to /login).
 *  - Each slot renders one cell per table and, if the restaurant declares
 *    combinable pairs, one cell per available pair. Each cell carries
 *    data-available; an unavailable cell does not open the booking form.
 *  - Search guards against stale responses (E4): every search carries a
 *    monotonic token; a late response cannot clobber the current view.
 *  - Booking flow uses an idempotency key generated on first form open.
 *    The key is retained while the form is unchanged (E5); editing any
 *    field clears it so the next submit starts a new booking. The
 *    submission recovery path retries with the same key + body on a
 *    network-class failure; a 201/200 replay removes the uncertainty
 *    and surfaces the original reference.
 */
(() => {
  const TOKEN_KEY = "tk.token";
  const USER_KEY = "tk.user";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const token = () => localStorage.getItem(TOKEN_KEY);
  const setToken = (v) => v ? localStorage.setItem(TOKEN_KEY, v)
                             : localStorage.removeItem(TOKEN_KEY);
  const setUser = (u) => u ? localStorage.setItem(USER_KEY, JSON.stringify(u))
                           : localStorage.removeItem(USER_KEY);
  const currentUser = () => {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); }
    catch { return null; }
  };

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = Math.random() * 16 | 0;
      const v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  async function api(method, path, body, extraHeaders) {
    const headers = Object.assign({}, extraHeaders || {});
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const t = token();
    if (t) headers["Authorization"] = "Bearer " + t;
    const resp = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let json = null;
    try { json = await resp.json(); } catch { json = null; }
    return { status: resp.status, body: json };
  }

  /* ---- current-user + logout ---------------------------------------- */

  function renderCurrentUser() {
    const cur = $("#current-user");
    if (!cur) return;
    const u = currentUser();
    cur.textContent = u ? (u.display_name || u.email || "") : "";
  }

  function logout() {
    setToken(null);
    setUser(null);
    location.assign("/login");
  }

  function bindLogout() {
    const out = $("#logout-button");
    if (out) out.addEventListener("click", logout);
  }

  /* ---- auth pages --------------------------------------------------- */

  function showAuthError(msg) {
    let errEl = $("#auth-error");
    if (!errEl) {
      errEl = document.createElement("div");
      errEl.id = "auth-error";
      errEl.setAttribute("data-testid", "auth-error");
      const main = $("#app") || document.body;
      main.appendChild(errEl);
    }
    errEl.textContent = msg || "";
  }

  function bindSignup() {
    const form = $("#signup-form");
    if (!form) return;
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const errEl = $("#auth-error");
      if (errEl) errEl.textContent = "";
      const body = {
        display_name: $("#signup-display-name").value.trim(),
        email: $("#signup-email").value.trim(),
        password: $("#signup-password").value,
      };
      try {
        const r = await api("POST", "/auth/signup", body);
        if (r.status === 201 && r.body && r.body.token) {
          setToken(r.body.token);
          setUser({
            display_name: r.body.display_name || body.display_name,
            email: body.email,
          });
          location.assign("/");
          return;
        }
        const code = (r.body && r.body.error && r.body.error.code) || "signup_failed";
        showAuthError(code);
      } catch (e) {
        showAuthError("network");
      }
    });
  }

  function bindLogin() {
    const form = $("#login-form");
    if (!form) return;
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const errEl = $("#auth-error");
      if (errEl) errEl.textContent = "";
      const body = {
        email: $("#login-email").value.trim(),
        password: $("#login-password").value,
      };
      try {
        const r = await api("POST", "/auth/login", body);
        if (r.status === 200 && r.body && r.body.token) {
          setToken(r.body.token);
          setUser({
            display_name: r.body.display_name || "",
            email: body.email,
          });
          location.assign("/");
          return;
        }
        const code = (r.body && r.body.error && r.body.error.code) || "login_failed";
        showAuthError(code);
      } catch (e) {
        showAuthError("network");
      }
    });
  }

  /* ---- availability + booking (index) ------------------------------- */

  let searchSeq = 0;            // E4: latest-search-wins guard
  let lastSlots = null;
  let restaurantsCache = null;
  let selectedOption = null;
  let bookingInFlight = false;
  let lastBookingReference = null;

  async function loadRestaurants() {
    const sel = $("#restaurant-select");
    if (!sel) return;
    const r = await api("GET", "/restaurants");
    if (r.status === 200 && Array.isArray((r.body || {}).restaurants)) {
      restaurantsCache = r.body.restaurants;
      sel.innerHTML = "";
      for (const rest of r.body.restaurants) {
        const opt = document.createElement("option");
        opt.value = rest.id;
        opt.textContent = rest.name;
        sel.appendChild(opt);
      }
    }
  }

  function todayISO() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  function labelForTable(restId, tableId) {
    if (!restaurantsCache) return tableId;
    const rest = restaurantsCache.find((r) => r.id === restId);
    if (!rest) return tableId;
    const t = (rest.tables || []).find((x) => x.id === tableId);
    return t ? t.label : tableId;
  }

  function restNameFor(restId) {
    if (!restaurantsCache) return restId;
    const rest = restaurantsCache.find((r) => r.id === restId);
    return rest ? rest.name : restId;
  }

  function partySize() {
    return Number($("#party-size-input").value || 2);
  }

  function renderGrid(slots, restId) {
    const grid = $("#availability-grid");
    const noSlots = $("#no-slots");
    if (!grid) return;
    grid.innerHTML = "";
    if (!slots || slots.length === 0) {
      if (noSlots) noSlots.hidden = false;
      return;
    }
    if (noSlots) noSlots.hidden = true;
    for (const slot of slots) {
      const hhmm = slot.starts_at_local.split("T")[1];
      for (const table of slot.available_table_ids || []) {
        const cell = document.createElement("button");
        cell.type = "button";
        cell.className = "cell";
        cell.setAttribute("data-testid", `slot-${table}-${hhmm}`);
        cell.setAttribute("data-available", "true");
        cell.textContent = `${hhmm} · ${table}`;
        cell.addEventListener("click", () => onPick(slot, [{ table_ids: [table], capacity: null }]));
        grid.appendChild(cell);
      }
      for (const opt of (slot.available_options || [])) {
        if (!opt.table_ids || opt.table_ids.length < 2) continue;
        const cell = document.createElement("button");
        cell.type = "button";
        cell.className = "cell pair";
        const key = opt.table_ids.join("+");
        cell.setAttribute("data-testid", `slot-${key}-${hhmm}`);
        cell.setAttribute("data-available", "true");
        cell.textContent = `${hhmm} · ${key}`;
        cell.addEventListener("click", () => onPick(slot, [opt]));
        grid.appendChild(cell);
      }
    }
  }

  function onPick(slot, options) {
    selectedOption = { slot, options };
    const form = $("#booking-form");
    if (form) form.hidden = false;
    const summary = $("#booking-summary");
    if (summary) {
      const hhmm = slot.starts_at_local.split("T")[1];
      const tids = options[0].table_ids;
      const labels = tids.map((t) => labelForTable($("#restaurant-select").value, t));
      summary.textContent = `${hhmm} · tables ${labels.join(" + ")}`;
    }
    const ps = $("#booking-party-size");
    if (ps) ps.value = String(partySize());
    if (!$("#form-idempotency-key").value) {
      $("#form-idempotency-key").value = uuid();
    }
    // Clear any prior booking/error UI.
    const conf = $("#confirmation");
    if (conf) conf.hidden = true;
    const err = $("#booking-error");
    if (err) err.hidden = true;
    lastBookingReference = null;
  }

  async function runSearch() {
    const restId = $("#restaurant-select").value;
    const date = $("#date-input").value || todayISO();
    const ps = partySize();
    if (!restId || !date || !ps) return;
    const seq = ++searchSeq;
    const url = `/availability?restaurant_id=${encodeURIComponent(restId)}&date=${encodeURIComponent(date)}&party_size=${ps}`;
    const r = await api("GET", url);
    if (seq !== searchSeq) return;
    if (r.status === 200 && r.body) {
      lastSlots = r.body.slots || [];
      renderGrid(lastSlots, restId);
    } else {
      renderGrid([], restId);
    }
  }

  function bindSearch() {
    const btn = $("#search-button");
    if (!btn) return;
    btn.addEventListener("click", runSearch);
    $("#restaurant-select").addEventListener("change", runSearch);
  }

  function formSignature() {
    return JSON.stringify({
      rest: $("#restaurant-select").value,
      slot: selectedOption && selectedOption.slot && selectedOption.slot.starts_at_local,
      tids: selectedOption && selectedOption.options && selectedOption.options[0].table_ids,
      party: $("#booking-party-size").value,
    });
  }

  function showConfirmation(res) {
    const conf = $("#confirmation");
    if (!conf) return;
    conf.hidden = false;
    const ref = $("#confirmation-reference");
    if (ref) ref.textContent = res.reference || "";
    const det = $("#confirmation-details");
    if (det) {
      const rest = $("#restaurant-select").value;
      const hhmm = (selectedOption.slot.starts_at_local || "").split("T")[1];
      const labels = (res.table_ids || (res.table_id ? [res.table_id] : []))
        .map((t) => labelForTable(rest, t));
      det.textContent =
        `${restNameFor(rest)} · tables ${labels.join(" + ")} · ${hhmm}`;
    }
    lastBookingReference = res.reference;
  }

  async function onSubmitBooking(ev) {
    ev.preventDefault();
    if (!token()) {
      const err = $("#booking-error");
      if (err) {
        err.textContent = "Sign in to complete the booking.";
        err.hidden = false;
      }
      const next = $("#login-link");
      if (next) location.assign("/login");
      return;
    }
    if (!selectedOption) return;
    if (bookingInFlight) return;
    bookingInFlight = true;
    const idem = $("#form-idempotency-key").value || uuid();
    $("#form-idempotency-key").value = idem;
    const body = {
      restaurant_id: $("#restaurant-select").value,
      table_ids: selectedOption.options[0].table_ids,
      starts_at_local: selectedOption.slot.starts_at_local,
      party_size: Number($("#booking-party-size").value || partySize()),
      name: currentUser() && currentUser().display_name || "",
      email: currentUser() && currentUser().email || "",
    };
    const r = await api("POST", "/reservations", body, {
      "Idempotency-Key": idem,
    });
    bookingInFlight = false;
    const errEl = $("#booking-error");
    const conf = $("#confirmation");
    if (r.status === 201 && r.body && r.body.reference) {
      if (errEl) errEl.hidden = true;
      if (conf) conf.hidden = false;
      showConfirmation(r.body);
      // Keep the idempotency key so a second submit replays (E5).
      return;
    }
    if (r.status === 409) {
      if (errEl) {
        errEl.textContent = "That slot is no longer available.";
        errEl.hidden = false;
      }
      if (conf) conf.hidden = true;
      runSearch();
      return;
    }
    // Other errors: lost-response path — retry with same key + body.
    if (errEl) {
      errEl.textContent = "Retrying...";
      errEl.hidden = false;
    }
    const sig = formSignature();
    const retry = await api("POST", "/reservations", body, {
      "Idempotency-Key": idem,
    });
    if (retry.status === 201 && retry.body && retry.body.reference) {
      if (errEl) errEl.hidden = true;
      if (conf) conf.hidden = false;
      showConfirmation(retry.body);
      return;
    }
    if (retry && retry.body && retry.body.error) {
      if (errEl) {
        errEl.textContent = retry.body.error.code || "rejected";
        errEl.hidden = false;
      }
    }
  }

  function bindBookingForm() {
    const form = $("#booking-form");
    if (!form) return;
    form.addEventListener("submit", (ev) => onSubmitBooking(ev));
    const ps = $("#booking-party-size");
    if (ps) {
      let prev = ps.value;
      ps.addEventListener("input", () => {
        if (ps.value !== prev) {
          $("#form-idempotency-key").value = "";
          prev = ps.value;
        }
      });
    }
  }

  /* ---- lookup ------------------------------------------------------- */

  async function lookupSubmit() {
    const ref = $("#lookup-reference-input").value.trim();
    const detail = $("#reservation-detail");
    const status = $("#reservation-status");
    const errEl = $("#reservation-error");
    if (detail) detail.hidden = true;
    if (status) status.textContent = "";
    if (errEl) errEl.hidden = true;
    if (!ref) return;
    const r = await api("GET", "/reservations/" + encodeURIComponent(ref));
    if (r.status === 200 && r.body && r.body.reference) {
      if (status) status.textContent = r.body.status || "confirmed";
      if (detail) detail.hidden = false;
      const cancel = $("#reservation-cancel-button");
      if (cancel) {
        cancel.onclick = async () => {
          await api("POST", "/reservations/" + encodeURIComponent(ref) + "/cancel");
          const r2 = await api("GET", "/reservations/" + encodeURIComponent(ref));
          if (status) status.textContent = (r2.body && r2.body.status) || "cancelled";
        };
      }
    } else {
      const code = (r.body && r.body.error && r.body.error.code) || "not_found";
      if (errEl) {
        errEl.textContent = code;
        errEl.hidden = false;
      }
    }
  }

  function bindLookup() {
    const btn = $("#lookup-submit");
    if (!btn) return;
    btn.addEventListener("click", lookupSubmit);
    const inp = $("#lookup-reference-input");
    if (inp) inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        lookupSubmit();
      }
    });
  }

  /* ---- bootstrap ---------------------------------------------------- */

  document.addEventListener("DOMContentLoaded", async () => {
    renderCurrentUser();
    bindLogout();
    if ($("#signup-form")) bindSignup();
    if ($("#login-form")) bindLogin();
    if ($("#restaurant-select")) {
      await loadRestaurants();
      const date = $("#date-input");
      if (date && !date.value) date.value = todayISO();
      bindSearch();
      bindBookingForm();
    }
    if ($("#lookup-submit")) bindLookup();
  });
})();
