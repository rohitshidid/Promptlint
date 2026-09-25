/* Account page: sign-up / log-in, API keys (shown once), usage chart, account deletion. */
(function () {
  "use strict";
  document.documentElement.classList.remove("no-js");
  const $ = (id) => document.getElementById(id);
  const { esc, ICON } = window.PL;
  const CSRF = { "X-PL-CSRF": "1", "Content-Type": "application/json" };
  const fmt = (n) => (n == null ? "∞" : Number(n).toLocaleString("en-US"));
  const day = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "—");

  const nav = $("nav");
  const onScroll = () => nav.classList.toggle("stuck", window.scrollY > 4);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: "same-origin", ...opts });
    let data = null;
    if (res.status !== 204) { try { data = await res.json(); } catch { /* empty */ } }
    if (!res.ok) throw { status: res.status, ...(data?.error || { message: `Request failed (HTTP ${res.status}).` }) };
    return data;
  }
  function show(el, text, kind) { el.textContent = text || ""; el.className = "msg" + (text ? " " + kind : ""); }

  /* -------------------------------------------------------------- auth */
  let mode = "signup";
  let captchaToken = null;
  let siteKey = null;

  function setMode(m) {
    mode = m;
    $("tab-signup").setAttribute("aria-selected", m === "signup");
    $("tab-login").setAttribute("aria-selected", m === "login");
    $("auth-submit").textContent = m === "signup" ? "Create free account" : "Log in";
    $("password").autocomplete = m === "signup" ? "new-password" : "current-password";
    $("pw-hint").hidden = m !== "signup";
    $("captcha").hidden = m !== "signup";
    $("perks").hidden = m !== "signup";
    show($("auth-msg"), "");
  }
  $("tab-signup").onclick = () => setMode("signup");
  $("tab-login").onclick = () => setMode("login");

  // Optional Cloudflare Turnstile, only when the server has a site key configured.
  window.plTurnstileReady = () => {
    window.turnstile.render("#captcha", { sitekey: siteKey, callback: (t) => { captchaToken = t; } });
  };

  $("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = $("email").value.trim(), password = $("password").value;
    if (!email || !password) return show($("auth-msg"), "Enter your email and password.", "err");
    const btn = $("auth-submit");
    btn.disabled = true;
    try {
      await api(`/v1/account/${mode}`, { method: "POST", headers: CSRF, body: JSON.stringify({ email, password, captcha_token: captchaToken }) });
      $("password").value = "";
      await load();
    } catch (err) {
      show($("auth-msg"), err.message, "err");
      if (window.turnstile && siteKey) { window.turnstile.reset(); captchaToken = null; }
    } finally {
      btn.disabled = false;
    }
  });

  $("logout").onclick = async () => {
    try { await api("/v1/account/logout", { method: "POST", headers: CSRF }); } catch { /* signed out anyway */ }
    await load();
  };

  /* --------------------------------------------------------- dashboard */
  function tiles(me) {
    const p = me.plan;
    const quota = p.daily != null ? { used: me.used_today, limit: p.daily, label: "Used today" }
      : p.monthly != null ? { used: me.used_this_month, limit: p.monthly, label: "Used this month" }
      : { used: me.used_today, limit: null, label: "Used today" };
    const share = quota.limit ? Math.min(1, quota.used / quota.limit) : 0;
    const st = share < 0.7 ? "good" : share < 0.9 ? "warn" : "bad";
    $("tiles").innerHTML = `
      <div class="tile"><small>${quota.label}</small><b>${fmt(quota.used)}</b><span>of ${fmt(quota.limit)} prompts</span>
        ${quota.limit ? `<div class="meter ${st}" role="meter" aria-valuemin="0" aria-valuemax="${quota.limit}" aria-valuenow="${quota.used}" aria-label="${quota.label}"><i style="width:${(share * 100).toFixed(1)}%"></i></div>` : ""}</div>
      <div class="tile"><small>This month</small><b>${fmt(me.used_this_month)}</b><span>prompts scored</span></div>
      <div class="tile"><small>Rate limit</small><b>${fmt(p.rpm)}/min</b><span>per key · batches up to ${fmt(p.batch_max)}</span></div>
      <div class="tile"><small>Plan</small><b style="text-transform:capitalize">${esc(p.name)}</b><span>${me.active_keys} of ${me.max_keys} keys active</span></div>`;
  }

  async function loadKeys() {
    const { keys } = await api("/v1/keys");
    $("key-rows").innerHTML = keys.length
      ? keys.map((k) => `<tr>
          <td>${esc(k.name)}</td>
          <td><code>${esc(k.masked)}</code></td>
          <td class="muted">${day(k.created_at)}</td>
          <td class="muted">${k.last_used_at ? day(k.last_used_at) : "Never"}</td>
          <td style="text-align:right">${k.revoked ? '<span class="tag-revoked">Revoked</span>' : `<button class="linkbtn" data-revoke="${k.id}" data-name="${esc(k.name)}">Revoke</button>`}</td>
        </tr>`).join("")
      : `<tr><td colspan="5" class="muted">No keys yet. Create one above.</td></tr>`;
  }

  $("key-rows").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-revoke]");
    if (!b) return;
    // Two-step confirm in place, instead of a blocking browser dialog.
    if (b.dataset.armed !== "1") {
      b.dataset.armed = "1"; b.textContent = "Confirm revoke";
      setTimeout(() => { if (b.isConnected) { b.dataset.armed = ""; b.textContent = "Revoke"; } }, 4000);
      return;
    }
    try {
      await api(`/v1/keys/${b.dataset.revoke}`, { method: "DELETE", headers: CSRF });
      show($("key-msg"), `Revoked "${b.dataset.name}". Requests using it now get 401.`, "ok");
      await Promise.all([loadKeys(), refreshMe()]);
    } catch (err) { show($("key-msg"), err.message, "err"); }
  });

  $("new-key").addEventListener("submit", async (e) => {
    e.preventDefault();
    show($("key-msg"), "");
    try {
      const k = await api("/v1/keys", { method: "POST", headers: CSRF, body: JSON.stringify({ name: $("key-name").value.trim() || "Default key" }) });
      $("key-name").value = "";
      $("full-key").textContent = k.key;
      quickstart(k.key);
      $("reveal-key").classList.add("show");
      $("copy-key").textContent = "Copy";
      await Promise.all([loadKeys(), refreshMe()]);
    } catch (err) { show($("key-msg"), err.message, "err"); }
  });
  // Hide the key and remove it from the page: after "Done" it can't be shown again.
  function dismissKey() {
    $("reveal-key").classList.remove("show");
    $("full-key").textContent = "";
    $("copy-key").textContent = "Copy";
    quickstart();
  }
  $("key-done").onclick = () => { dismissKey(); $("key-name").focus(); };
  $("copy-key").onclick = async () => {
    try { await navigator.clipboard.writeText($("full-key").textContent); $("copy-key").textContent = "Copied"; }
    catch { $("copy-key").textContent = "Select and copy"; }
  };

  function chart(days) {
    const W = 560, H = 170, L = 34, R = 6, T = 10, B = 22;
    const max = Math.max(4, ...days.map((d) => d.prompts));
    const nice = Math.pow(10, Math.floor(Math.log10(max)));
    const top = Math.ceil(max / nice) * nice;
    const bw = (W - L - R) / days.length;
    const y = (v) => T + (H - T - B) * (1 - v / top);
    let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Prompts per day for the last ${days.length} days">`;
    for (const t of [0, top / 2, top]) {
      s += `<line class="${t === 0 ? "base" : "grid"}" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/>`;
      s += `<text class="lbl" x="${L - 6}" y="${y(t) + 3.5}" text-anchor="end">${fmt(t)}</text>`;
    }
    days.forEach((d, i) => {
      const x = L + i * bw + 1, w = Math.max(1, bw - 2);
      const jev = d.prompts - d.degraded;
      const hJev = (H - T - B) * (jev / top), hDeg = (H - T - B) * (d.degraded / top);
      const base = y(0);
      if (jev > 0) s += `<rect class="bar" x="${x}" y="${base - hJev}" width="${w}" height="${hJev}" rx="${Math.min(3, w / 2)}"/>`;
      if (d.degraded > 0) s += `<rect class="bar deg" x="${x}" y="${base - hJev - hDeg - (jev > 0 ? 2 : 0)}" width="${w}" height="${hDeg}" rx="${Math.min(3, w / 2)}"/>`;
      const label = new Date(d.day + "T00:00:00Z").toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
      s += `<rect class="hit" x="${L + i * bw}" y="${T}" width="${bw}" height="${H - T - B}" data-tip="<b>${esc(label)}</b>: ${fmt(d.prompts)} prompts in ${fmt(d.requests)} requests${d.degraded ? ` · ${fmt(d.degraded)} via fallback` : ""}"/>`;
      if (i === 0 || i === days.length - 1 || i === Math.floor(days.length / 2)) {
        const anchor = i === 0 ? "start" : i === days.length - 1 ? "end" : "middle";
        const lx = i === 0 ? L + i * bw : i === days.length - 1 ? L + (i + 1) * bw : L + i * bw + bw / 2;
        s += `<text class="lbl" x="${lx}" y="${H - 6}" text-anchor="${anchor}">${esc(label)}</text>`;
      }
    });
    $("chart").innerHTML = s + "</svg>";
    $("usage-table").innerHTML = `<table><thead><tr><th>Day (UTC)</th><th>Requests</th><th>Prompts</th><th>Fallback</th></tr></thead><tbody>${
      days.slice().reverse().map((d) => `<tr><td>${d.day}</td><td>${fmt(d.requests)}</td><td>${fmt(d.prompts)}</td><td>${fmt(d.degraded)}</td></tr>`).join("")
    }</tbody></table>`;
  }

  function quickstart(key) {
    const host = location.origin;
    $("quickstart").innerHTML =
`<span class="c"># 1. Save your key in a variable (paste the whole key, no $ in front)</span>
export PQS_KEY=${key ? esc(key) : "pqs_live_…"}

<span class="c"># 2. Score one prompt</span>
curl ${esc(host)}/v1/score \\
  -H <span class="s">"Authorization: Bearer $PQS_KEY"</span> \\
  -H <span class="s">"Content-Type: application/json"</span> \\
  -d <span class="s">'{"prompt": "write me a poem", "models": ["claude-sonnet-5"]}'</span>

<span class="c"># 3. Your usage</span>
curl ${esc(host)}/v1/usage -H <span class="s">"Authorization: Bearer $PQS_KEY"</span>`;
  }

  async function refreshMe() {
    const me = await api("/v1/account/me");
    tiles(me);
    return me;
  }

  $("delete-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/v1/account/delete", { method: "POST", headers: CSRF, body: JSON.stringify({ password: $("delete-password").value }) });
      await load();
      show($("auth-msg"), "Your account and all its data were deleted.", "ok");
    } catch (err) { show($("delete-msg"), err.message, "err"); }
  });

  /* -------------------------------------------------------------- load */
  async function load() {
    let me = null;
    try { me = await api("/v1/account/me"); } catch { /* signed out */ }
    $("signed-in").hidden = !me;
    $("signed-out").hidden = !!me;
    $("logout").hidden = !me;
    dismissKey();
    if (!me) {
      $("title").textContent = "Get a free API key";
      $("subtitle").textContent = "Score prompts from your own code: both scores, every signal, and cost estimates in one JSON reply.";
      return;
    }
    $("title").textContent = "Your API";
    $("subtitle").innerHTML = `Signed in as <b>${esc(me.email)}</b>`;
    tiles(me);
    quickstart();
    const [, usage] = await Promise.all([loadKeys(), api("/v1/usage?days=30")]);
    chart(usage.days);
  }

  api("/v1/account/config").then((cfg) => {
    siteKey = cfg.turnstile_site_key;
    if (siteKey) {
      const s = document.createElement("script");
      s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=plTurnstileReady&render=explicit";
      s.async = true;
      document.head.appendChild(s);
    }
  }).catch(() => {});
  setMode(location.hash === "#login" ? "login" : "signup");
  load();
})();
