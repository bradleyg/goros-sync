// Garmin → COROS Sync — front-end controller (vanilla JS, no build step)

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const WINDOW_CHOICES = [1, 3, 7, 14, 30, 90, 180, 365];
const WEEKDAYS = [
  ["mon", "M"], ["tue", "T"], ["wed", "W"], ["thu", "T"], ["fri", "F"], ["sat", "S"], ["sun", "S"],
];
const WEEKDAY_NAMES = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const PAGE = 20;

const ICONS = {
  success: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
  partial: '<svg viewBox="0 0 24 24"><path d="M12 8v5M12 16.5v.5"/><path d="M10.3 3.9 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>',
  failed: '<svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7 7 17"/></svg>',
  running: '<svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-3-6.25"/></svg>',
  chevron: '<svg class="chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>',
};

const SPORT = [
  [/trail/, "⛰️", "Trail run"],
  [/treadmill|indoor_running/, "🏃", "Treadmill"],
  [/run/, "🏃", "Run"],
  [/virtual_ride|indoor_cycling/, "🚲", "Indoor ride"],
  [/mountain_biking|gravel/, "🚵", "Ride"],
  [/cycl|bik|ride/, "🚴", "Ride"],
  [/open_water/, "🌊", "Open water"],
  [/swim/, "🏊", "Swim"],
  [/hik/, "🥾", "Hike"],
  [/walk/, "🚶", "Walk"],
  [/strength/, "🏋️", "Strength"],
  [/yoga|pilates/, "🧘", "Yoga"],
  [/row/, "🚣", "Row"],
  [/ski|snowboard/, "⛷️", "Ski"],
  [/multi_sport|triathlon/, "🏅", "Multisport"],
];

const state = {
  data: null,
  runs: [],
  totalRuns: 0,
  openRun: null,
  runDetails: new Map(),
  wasRunning: false,
  scheduleDirty: false,
  pollTimer: null,
  dialog: { service: null, step: "credentials" },
};

// ------------------------------------------------------------------ helpers
async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const msg = data?.error || (Array.isArray(data?.detail) ? data.detail.map((d) => d.msg).join(", ") : data?.detail) || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(message, { bad = false } = {}) {
  const el = document.createElement("div");
  el.className = `toast${bad ? " is-bad" : ""}`;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => { el.classList.add("is-leaving"); setTimeout(() => el.remove(), 220); }, bad ? 6000 : 3500);
}

const parseDate = (iso) => (iso ? new Date(iso) : null);

function relTime(iso) {
  const d = parseDate(iso);
  if (!d) return "";
  const diff = (d - Date.now()) / 1000;
  const abs = Math.abs(diff);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (abs < 45) return diff < 0 ? "just now" : "in a moment";
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 86400 * 7) return rtf.format(Math.round(diff / 86400), "day");
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function dayTime(iso) {
  const d = parseDate(iso);
  if (!d) return "—";
  const today = new Date();
  const tomorrow = new Date(); tomorrow.setDate(today.getDate() + 1);
  const yesterday = new Date(); yesterday.setDate(today.getDate() - 1);
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const same = (a, b) => a.toDateString() === b.toDateString();
  if (same(d, today)) return `Today, ${time}`;
  if (same(d, tomorrow)) return `Tomorrow, ${time}`;
  if (same(d, yesterday)) return `Yesterday, ${time}`;
  return `${d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}, ${time}`;
}

function duration(sec) {
  if (sec == null) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function runDuration(run) {
  if (!run.finished_at) return "";
  const s = (parseDate(run.finished_at) - parseDate(run.started_at)) / 1000;
  return s < 60 ? `${Math.max(1, Math.round(s))}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

function distance(m) {
  if (!m) return "";
  return m >= 1000 ? `${(m / 1000).toFixed(m >= 100000 ? 0 : 2)} km` : `${Math.round(m)} m`;
}

function sport(typeKey) {
  const key = (typeKey || "").toLowerCase();
  for (const [re, icon, label] of SPORT) if (re.test(key)) return { icon, label };
  return { icon: "⏱️", label: key ? key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : "Activity" };
}

function activityDate(local) {
  if (!local) return "";
  const d = new Date(local.replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return local;
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) + ", " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function setLoading(btn, on) {
  btn.classList.toggle("is-loading", on);
  btn.disabled = on;
}

// -------------------------------------------------------------------- state
async function refreshState() {
  try {
    const data = await api("/api/state");
    state.data = data;
    renderAll();
    const running = !!data.sync?.running;
    if (state.wasRunning && !running) {
      // A scheduled run with nothing new is discarded server-side; don't toast an older run.
      if (data.last_run && data.last_run.id === data.sync.last_run_id) onSyncFinished(data.last_run);
    }
    if (running || state.wasRunning) await refreshRuns({ keep: true });
    state.wasRunning = running;
    schedulePoll(running ? 1500 : 15000);
  } catch (err) {
    schedulePoll(5000);
    console.error(err);
  }
}

function schedulePoll(ms) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(refreshState, ms);
}

function onSyncFinished(run) {
  if (!run) return;
  state.runDetails.delete(run.id);
  if (run.status === "success") {
    toast(run.uploaded ? `Synced ${plural(run.uploaded, "activity", "activities")} to COROS` : "Everything is up to date");
  } else if (run.status === "partial") {
    toast(`Synced ${run.uploaded}, ${run.failed} failed`, { bad: true });
  } else if (run.status === "failed") {
    toast(run.message || "Sync failed", { bad: true });
  }
}

// ------------------------------------------------------------------- render
function renderAll() {
  renderConnections();
  renderSync();
  if (!state.scheduleDirty) renderSchedule();
}

function renderConnections() {
  const { garmin, coros } = state.data;
  const cfg = {
    garmin: {
      ok: garmin.connected,
      bad: garmin.connected && garmin.status && !garmin.status.ok,
      detail: garmin.connected ? (garmin.display_name ? `${garmin.display_name} · ${garmin.email}` : garmin.email) : "Not connected",
      error: garmin.status && !garmin.status.ok ? garmin.status.error : null,
    },
    coros: {
      ok: coros.connected,
      bad: coros.connected && coros.status && !coros.status.ok,
      detail: coros.connected
        ? [coros.nickname, coros.email, coros.region ? `${coros.region.toUpperCase()} region` : null].filter(Boolean).join(" · ")
        : "Not connected",
      error: coros.status && !coros.status.ok ? coros.status.error : null,
    },
  };
  for (const [svc, c] of Object.entries(cfg)) {
    const row = $(`#conn-${svc}`);
    row.classList.toggle("is-ok", c.ok && !c.bad);
    $(".conn-detail", row).textContent = c.detail;
    const err = $(".conn-error", row);
    err.hidden = !c.error;
    err.textContent = c.error || "";
    $(`[data-open="${svc}"]`, row).textContent = c.ok ? "Reconnect" : "Connect";
    $(`[data-disconnect="${svc}"]`, row).hidden = !c.ok;

    const pill = $(`#pill-${svc}`);
    pill.classList.toggle("is-ok", c.ok && !c.bad);
    pill.classList.toggle("is-bad", !!c.bad);
    pill.title = c.ok ? (c.bad ? `Problem: ${c.error}` : "Connected") : "Not connected";
  }
}

function renderSync() {
  const { sync, last_run: last, last_check: check, last_success: lastOk, garmin, coros, schedule, totals, settings } = state.data;
  const ready = garmin.connected && coros.connected;
  const running = !!sync.running;
  // Scheduled runs that find nothing new aren't kept in history, only recorded as the latest check.
  const lastTime = last ? Date.parse(last.finished_at || last.started_at) : 0;
  const checkedSinceLast = !!check && Date.parse(check.at) > lastTime;
  const btn = $("#sync-now");
  btn.disabled = !ready || running;
  btn.classList.toggle("is-running", running);
  $("span", btn).textContent = running ? "Syncing…" : "Sync now";
  $("#sync-window").disabled = running;

  // Window select default follows the saved setting unless user picked one.
  const sel = $("#sync-window");
  if (!sel.dataset.touched) sel.value = String(settings.lookback_days);
  if (!sel.value) sel.value = "7";

  // Headline
  let headline, sub;
  if (!ready) {
    headline = "Connect your accounts to start syncing";
    sub = "Activities recorded on Garmin will be imported into COROS Training Hub.";
  } else if (running) {
    headline = "Syncing your activities…";
    sub = sync.trigger === "scheduled" ? "Started automatically by your schedule." : "Started manually.";
  } else if (!last && !check) {
    headline = "Ready for your first sync";
    sub = "Pick how far back to look, then hit Sync now.";
  } else if (checkedSinceLast) {
    headline = `Last synced ${relTime(check.at)}`;
    sub = "No new activities — you're up to date.";
  } else if (last.status === "failed") {
    headline = "Last sync didn't complete";
    sub = `${relTime(last.finished_at || last.started_at)} · ${last.message || "Unknown error"}`;
  } else {
    headline = lastOk ? `Last synced ${relTime(lastOk.finished_at)}` : "Synced";
    sub = last.uploaded
      ? `${plural(last.uploaded, "activity", "activities")} sent to COROS${last.failed ? `, ${last.failed} failed` : ""}.`
      : "No new activities — you're up to date.";
  }
  $("#sync-title").textContent = headline;
  $("#sync-sub").textContent = sub;

  // Onboarding
  const hasSynced = !!(last || check);
  $("#onboarding").hidden = ready && hasSynced;
  $("#step-garmin").classList.toggle("is-done", garmin.connected);
  $("#step-coros").classList.toggle("is-done", coros.connected);
  $("#step-sync").classList.toggle("is-done", hasSynced);

  // Progress
  const prog = $("#progress");
  prog.hidden = !running;
  if (running) {
    const total = sync.total || 0;
    const done = sync.done || 0;
    const determinate = sync.phase === "Syncing" && total > 0;
    prog.classList.toggle("is-indeterminate", !determinate);
    $("#progress-phase").textContent = sync.phase || "Working";
    $("#progress-count").textContent = total ? `${Math.min(done + (determinate ? 1 : 0), total)} / ${total}` : "";
    $("#progress-bar").style.width = determinate ? `${Math.max(4, (done / total) * 100)}%` : "";
    $("#progress-current").textContent = sync.current ? `Uploading “${sync.current}”` : "";
  }

  // Stats
  $("#stat-next").textContent = schedule.enabled && schedule.next_run ? dayTime(schedule.next_run) : "Off";
  $("#stat-total").textContent = totals.synced_activities.toLocaleString();
  $("#stat-runs").textContent = totals.runs.toLocaleString();
}

function intervalLabel(minutes) {
  if (minutes === 1) return "minute";
  if (minutes < 60) return `${minutes} minutes`;
  if (minutes === 60) return "hour";
  return plural(minutes / 60, "hour");
}

function scheduleSummary(s) {
  if (!s.enabled) return "Automatic sync is off.";
  if (s.mode === "interval") return `Runs every ${intervalLabel(s.interval_minutes)}.`;
  const days = s.days.length === 7 ? "every day"
    : s.days.join() === "mon,tue,wed,thu,fri" ? "on weekdays"
    : s.days.join() === "sat,sun" ? "on weekends"
    : `on ${s.days.map((d) => WEEKDAY_NAMES[d]).join(", ")}`;
  return `Runs ${days} at ${s.time}.`;
}

function renderSchedule() {
  const s = state.data.schedule;
  $("#sched-enabled").checked = s.enabled;
  $(`#mode-${s.mode}`).checked = true;
  const interval = $("#sched-hours");
  if (!interval.options.length) {
    interval.innerHTML = s.interval_choices
      .map((m) => {
        const label = m === 1440 ? "24 hours (daily)" : m === 1 ? "1 minute" : m === 60 ? "1 hour" : intervalLabel(m);
        return `<option value="${m}">${label}</option>`;
      })
      .join("");
  }
  interval.value = String(s.interval_minutes);
  $("#sched-time").value = s.time;
  for (const cb of $$("#sched-days input")) cb.checked = s.days.includes(cb.value);
  $("#lookback").value = state.data.settings.lookback_days;
  updateModeVisibility();
  $("#schedule-form").classList.toggle("is-off", !s.enabled);
  const next = s.enabled && s.next_run ? ` Next: ${dayTime(s.next_run)}.` : "";
  $("#schedule-summary").textContent = scheduleSummary(s) + next;
  setScheduleDirty(false);
}

function updateModeVisibility() {
  const mode = $('input[name="mode"]:checked').value;
  for (const el of $$("[data-mode]")) el.hidden = el.dataset.mode !== mode;
}

function setScheduleDirty(on) {
  state.scheduleDirty = on;
  $("#sched-save").disabled = !on;
  $("#schedule-dirty").hidden = !on;
}

// ------------------------------------------------------------------ history
async function refreshRuns({ keep = false, append = false } = {}) {
  const offset = append ? state.runs.length : 0;
  const limit = keep ? Math.max(PAGE, state.runs.length) : PAGE;
  const data = await api(`/api/runs?limit=${limit}&offset=${offset}`);
  state.runs = append ? [...state.runs, ...data.runs] : data.runs;
  state.totalRuns = data.total;
  if (state.openRun) {
    const open = state.runs.find((r) => r.id === state.openRun);
    if (open && (open.status === "running" || !state.runDetails.has(open.id))) {
      state.runDetails.set(open.id, await api(`/api/runs/${open.id}`));
    }
  }
  renderRuns();
}

function runSummary(run) {
  if (run.status === "running") {
    return `<b>${run.uploaded}</b> uploaded so far · ${run.found} found`;
  }
  if (run.status === "failed" && !run.uploaded && !run.found) {
    return `<span class="err">${esc(run.message || "Failed")}</span>`;
  }
  const parts = [];
  parts.push(`<b>${run.uploaded}</b> uploaded`);
  if (run.failed) parts.push(`<span class="err">${run.failed} failed</span>`);
  if (run.skipped) parts.push(`${run.skipped} skipped`);
  if (run.already) parts.push(`${run.already} already synced`);
  if (!run.found) return "No activities in window";
  return parts.join(" · ");
}

function renderRuns() {
  const list = $("#runs");
  $("#runs-empty").hidden = state.runs.length > 0;
  $("#clear-history").hidden = state.runs.length === 0;
  $("#runs-more").hidden = state.runs.length >= state.totalRuns;

  list.innerHTML = state.runs.map((run) => {
    const open = state.openRun === run.id;
    const title = run.status === "running" ? "In progress" : dayTime(run.started_at);
    return `
      <li class="run${open ? " is-open" : ""}" data-run="${run.id}">
        <button class="run-row" type="button" aria-expanded="${open}" aria-controls="run-detail-${run.id}">
          <span class="run-status st-${run.status}" title="${esc(run.status)}">${ICONS[run.status] || ICONS.failed}</span>
          <span class="run-main">
            <span class="run-title">${esc(title)} <span class="tag${run.trigger === "scheduled" ? " tag-scheduled" : ""}">${run.trigger === "scheduled" ? "Scheduled" : "Manual"}</span></span>
            <span class="run-summary">${runSummary(run)}</span>
          </span>
          <span class="run-meta"><span class="mono">${esc(runDuration(run))}</span>since ${esc(shortDate(run.window_start))}</span>
          ${ICONS.chevron}
        </button>
        ${open ? `<div class="run-detail" id="run-detail-${run.id}">${renderRunDetail(run)}</div>` : ""}
      </li>`;
  }).join("");
}

function shortDate(d) {
  if (!d) return "";
  const date = new Date(`${d}T00:00:00`);
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function renderRunDetail(run) {
  const detail = state.runDetails.get(run.id);
  if (!detail) return `<p class="items-empty">Loading…</p>`;
  const msg = detail.message && detail.status !== "partial"
    ? `<p class="run-message${detail.status === "failed" ? " is-bad" : ""}">${esc(detail.message)}</p>` : "";
  if (!detail.items.length) {
    const why = detail.status === "running" ? "Working on it…"
      : detail.found ? `All ${plural(detail.found, "activity", "activities")} in this window were already synced.`
      : "Garmin returned no activities for this window.";
    return `${msg}<p class="items-empty">${why}</p>`;
  }
  const items = detail.items.map((it) => {
    const sp = sport(it.activity_type);
    const meta = [activityDate(it.start_time), sp.label, distance(it.distance_m), duration(it.duration_s)].filter(Boolean);
    const label = { uploaded: "Uploaded", pending: "Processing", skipped: "Skipped", failed: "Failed" }[it.status] || it.status;
    const showDetail = it.detail && it.status !== "uploaded";
    const canRetry = it.status === "uploaded" || it.status === "pending" || it.status === "skipped";
    return `
      <li class="item">
        <span class="item-ico" aria-hidden="true">${sp.icon}</span>
        <div style="min-width:0">
          <div class="item-name">${esc(it.name)}</div>
          <div class="item-meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}</div>
          ${showDetail ? `<div class="item-detail${it.status === "failed" ? " is-bad" : ""}">${esc(it.detail)}</div>` : ""}
        </div>
        <div class="item-side">
          <span class="chip chip-${it.status}">${esc(label)}</span>
          ${canRetry ? `<button class="link-btn resync" type="button" data-resync="${esc(it.garmin_activity_id)}" title="Upload this activity again on the next sync">Re-sync next time</button>` : ""}
          ${it.status === "failed" ? `<span class="hint">Retries next sync</span>` : ""}
        </div>
      </li>`;
  }).join("");
  return `${msg}<ul class="items">${items}</ul>`;
}

async function toggleRun(id) {
  state.openRun = state.openRun === id ? null : id;
  history.replaceState(null, "", state.openRun ? `#run-${state.openRun}` : location.pathname);
  renderRuns();
  if (state.openRun && !state.runDetails.has(id)) {
    try {
      state.runDetails.set(id, await api(`/api/runs/${id}`));
    } catch (err) {
      toast(err.message, { bad: true });
    }
    renderRuns();
  }
}

// ------------------------------------------------------------------- dialog
const DIALOG_COPY = {
  garmin: { title: "Connect Garmin", sub: "Sign in with your Garmin Connect account.", logo: "G" },
  coros: { title: "Connect COROS", sub: "Sign in with your COROS Training Hub account.", logo: "C" },
};

function openDialog(service) {
  const dlg = $("#connect-dialog");
  const copy = DIALOG_COPY[service];
  state.dialog = { service, step: "credentials" };
  $("#dlg-title").textContent = copy.title;
  $("#dlg-sub").textContent = copy.sub;
  const logo = $("#dlg-logo");
  logo.textContent = copy.logo;
  logo.className = `conn-logo ${service}`;
  $("#connect-form").reset();
  const existing = state.data?.[service]?.email;
  if (existing) $("#dlg-email").value = existing;
  showDialogStep("credentials");
  showDialogError(null);
  dlg.showModal();
  (existing ? $("#dlg-password") : $("#dlg-email")).focus();
}

function showDialogStep(step) {
  state.dialog.step = step;
  $("#dlg-credentials").hidden = step !== "credentials";
  $("#dlg-mfa").hidden = step !== "mfa";
  $("#dlg-email").required = step === "credentials";
  $("#dlg-password").required = step === "credentials";
  $("#dlg-code").required = step === "mfa";
  $("#dlg-submit").textContent = step === "mfa" ? "Verify" : "Connect";
  if (step === "mfa") {
    $("#dlg-sub").textContent = "Two-step verification is on for this account.";
    $("#dlg-code").focus();
  }
}

function showDialogError(msg) {
  const el = $("#dlg-error");
  el.hidden = !msg;
  el.textContent = msg || "";
}

async function submitDialog(e) {
  e.preventDefault();
  const { service, step } = state.dialog;
  const btn = $("#dlg-submit");
  showDialogError(null);
  setLoading(btn, true);
  try {
    let res;
    if (step === "mfa") {
      res = await api("/api/garmin/mfa", { method: "POST", body: { code: $("#dlg-code").value.trim() } });
    } else {
      res = await api(`/api/${service}/connect`, {
        method: "POST",
        body: { email: $("#dlg-email").value.trim(), password: $("#dlg-password").value },
      });
    }
    if (res.status === "needs_mfa") {
      setLoading(btn, false);
      showDialogStep("mfa");
      return;
    }
    $("#connect-dialog").close();
    toast(`${service === "garmin" ? "Garmin" : "COROS"} connected`);
    await refreshState();
  } catch (err) {
    showDialogError(err.message);
  } finally {
    setLoading(btn, false);
  }
}

async function disconnect(service) {
  const name = service === "garmin" ? "Garmin" : "COROS";
  if (!confirm(`Disconnect ${name}? Saved credentials and tokens will be removed from this machine.`)) return;
  try {
    await api(`/api/${service}/disconnect`, { method: "POST" });
    toast(`${name} disconnected`);
    await refreshState();
  } catch (err) {
    toast(err.message, { bad: true });
  }
}

// ---------------------------------------------------------------- actions
async function syncNow() {
  const btn = $("#sync-now");
  btn.disabled = true;
  try {
    const days = Number($("#sync-window").value);
    const { run_id: runId } = await api("/api/sync", { method: "POST", body: { lookback_days: days } });
    state.openRun = runId;
    state.wasRunning = true;
    await refreshState();
  } catch (err) {
    toast(err.message, { bad: true });
    btn.disabled = false;
  }
}

function readScheduleForm() {
  return {
    enabled: $("#sched-enabled").checked,
    mode: $('input[name="mode"]:checked').value,
    interval_minutes: Number($("#sched-hours").value),
    time: $("#sched-time").value || "07:00",
    days: $$("#sched-days input:checked").map((cb) => cb.value),
  };
}

async function saveSchedule(e) {
  e?.preventDefault();
  const btn = $("#sched-save");
  const body = readScheduleForm();
  const lookback = Number($("#lookback").value);
  if (!(lookback >= 1 && lookback <= 365)) {
    toast("Look-back must be between 1 and 365 days", { bad: true });
    return;
  }
  setLoading(btn, true);
  try {
    await api("/api/settings", { method: "PUT", body: { lookback_days: lookback } });
    await api("/api/schedule", { method: "PUT", body });
    setScheduleDirty(false);
    delete $("#sync-window").dataset.touched;
    toast(body.enabled ? "Schedule saved" : "Settings saved");
    await refreshState();
  } catch (err) {
    toast(err.message, { bad: true });
  } finally {
    setLoading(btn, false);
    $("#sched-save").disabled = !state.scheduleDirty;
  }
}

async function clearHistory() {
  if (!confirm("Clear all sync history? Already-synced activities will still be remembered so they aren't uploaded twice.")) return;
  try {
    await api("/api/runs", { method: "DELETE" });
    state.runDetails.clear();
    state.openRun = null;
    await refreshRuns();
    await refreshState();
  } catch (err) {
    toast(err.message, { bad: true });
  }
}

async function resync(activityId, btn) {
  try {
    await api(`/api/activities/${encodeURIComponent(activityId)}/resync`, { method: "POST" });
    btn.textContent = "Queued for next sync";
    btn.disabled = true;
    toast("Will be uploaded again on the next sync");
  } catch (err) {
    toast(err.message, { bad: true });
  }
}

// ------------------------------------------------------------------- init
function buildStaticControls() {
  $("#sync-window").innerHTML = WINDOW_CHOICES
    .map((d) => `<option value="${d}">${d === 1 ? "Today" : `Last ${d} days`}</option>`).join("");
  $("#sched-days").innerHTML = WEEKDAYS.map(([v, l]) => `
    <label class="day" title="${WEEKDAY_NAMES[v]}"><input type="checkbox" value="${v}" aria-label="${WEEKDAY_NAMES[v]}" /><span>${l}</span></label>`).join("");
}

function bindEvents() {
  document.addEventListener("click", (e) => {
    const open = e.target.closest("[data-open]");
    if (open) return openDialog(open.dataset.open);
    const disc = e.target.closest("[data-disconnect]");
    if (disc) return disconnect(disc.dataset.disconnect);
    const rs = e.target.closest("[data-resync]");
    if (rs) return resync(rs.dataset.resync, rs);
    const row = e.target.closest(".run-row");
    if (row) return toggleRun(Number(row.closest(".run").dataset.run));
  });

  $("#sync-now").addEventListener("click", syncNow);
  $("#sync-window").addEventListener("change", (e) => { e.target.dataset.touched = "1"; });
  $("#connect-form").addEventListener("submit", submitDialog);
  $("#dlg-cancel").addEventListener("click", () => $("#connect-dialog").close());
  $("#connect-dialog").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });

  const form = $("#schedule-form");
  form.addEventListener("input", () => { setScheduleDirty(true); updateModeVisibility(); });
  form.addEventListener("submit", saveSchedule);
  $("#sched-enabled").addEventListener("change", (e) => {
    form.classList.toggle("is-off", !e.target.checked);
    setScheduleDirty(true);
    saveSchedule(); // toggling on/off applies immediately
  });

  $("#runs-more").addEventListener("click", () => refreshRuns({ append: true }));
  $("#clear-history").addEventListener("click", clearHistory);

  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshState(); });
  // Keep relative times fresh.
  setInterval(() => { if (state.data && !state.data.sync.running) renderSync(); }, 30000);
}

buildStaticControls();
bindEvents();
const hashRun = /^#run-(\d+)$/.exec(location.hash);
if (hashRun) state.openRun = Number(hashRun[1]);
await refreshState();
await refreshRuns();
