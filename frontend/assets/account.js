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
      <div class="tile"><small>Plan</small><b style="text-transform:capitalize">${esc(p.name)} · $0</b><span>${me.active_keys} of ${me.max_keys} keys active · limits rising soon</span></div>`;
  }

  let showRevoked = false;
  async function loadKeys() {
    const { keys } = await api("/v1/keys");
    const revoked = keys.filter((k) => k.revoked).length;
    const shown = showRevoked ? keys : keys.filter((k) => !k.revoked);
    const toggle = revoked ? `<tr><td colspan="5"><button class="linkbtn show-revoked" id="toggle-revoked" type="button">${showRevoked ? "Hide" : "Show"} ${revoked} revoked key${revoked === 1 ? "" : "s"}</button></td></tr>` : "";
    $("key-rows").innerHTML = (shown.length
      ? shown.map((k) => `<tr class="${k.revoked ? "revoked-row" : ""}">
          <td>${esc(k.name)}</td>
          <td><code>${esc(k.masked)}</code></td>
          <td class="muted">${day(k.created_at)}</td>
          <td class="muted">${k.last_used_at ? day(k.last_used_at) : "Never"}</td>
          <td style="text-align:right">${k.revoked ? '<span class="tag-revoked">Revoked</span>' : `<button class="linkbtn" data-revoke="${k.id}" data-name="${esc(k.name)}">Revoke</button>`}</td>
        </tr>`).join("")
      : `<tr><td colspan="5" class="muted">No active keys. Create one above.</td></tr>`) + toggle;
    const t = $("toggle-revoked");
    if (t) t.onclick = () => { showRevoked = !showRevoked; loadKeys(); };
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

  /* -------------------------------------------------- connected providers */
  const PROVIDER_LABEL = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Google Gemini", openai_compatible: "Custom endpoint" };
  function syncProviderForm() {
    const custom = $("prov-kind").value === "openai_compatible";
    $("prov-custom").hidden = !custom;
    $("prov-key-opt").hidden = !custom;
  }
  $("prov-kind").addEventListener("change", syncProviderForm);

  async function loadProviders() {
    const d = await api("/v1/providers");
    const off = $("prov-disabled");
    off.hidden = d.storage_enabled;
    if (!d.storage_enabled) { off.className = "msg err"; off.textContent = "This server isn't set up to store provider keys (PROVIDER_KEY_SECRET is missing). You can still send keys per request."; }
    $("prov-rows").innerHTML = d.providers.length
      ? d.providers.map((p) => `<tr>
          <td>${esc(PROVIDER_LABEL[p.provider] || p.provider)}${p.provider === "openai_compatible" ? `<br><span class="muted">${esc(p.label)}</span>` : ""}</td>
          <td>${p.model ? `<code>${esc(p.model)}</code> <span class="muted">· ${esc(p.tier)}</span><br>` : ""}${p.has_key ? `<code>${esc(p.key_hint)}</code>` : '<span class="muted">no key</span>'}</td>
          <td class="muted">${day(p.created_at)}</td>
          <td class="muted">${p.last_used_at ? day(p.last_used_at) : "Never"}</td>
          <td style="text-align:right"><button class="linkbtn" data-del-prov="${p.id}">Remove</button></td></tr>`).join("")
      : `<tr><td colspan="5" class="muted">No providers yet. Add one to let /v1/route call models for you.</td></tr>`;
  }

  $("prov-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    show($("prov-msg"), "");
    const kind = $("prov-kind").value;
    const body = { provider: kind, api_key: $("prov-key").value.trim() || null };
    if (kind === "openai_compatible") {
      Object.assign(body, {
        label: $("prov-label").value.trim() || null, base_url: $("prov-url").value.trim(), model: $("prov-model").value.trim(),
        tier: $("prov-tier").value, input_price: Number($("prov-in").value || 0), output_price: Number($("prov-out").value || 0),
      });
    }
    try {
      await api("/v1/providers", { method: "POST", headers: CSRF, body: JSON.stringify(body) });
      $("prov-key").value = "";
      ["prov-label", "prov-url", "prov-model"].forEach((id) => { $(id).value = ""; });
      show($("prov-msg"), `Added ${PROVIDER_LABEL[kind]}.${kind !== "openai_compatible" ? " It replaces any earlier key for the same provider." : ""}`, "ok");
      await loadProviders();
    } catch (err) { show($("prov-msg"), err.message, "err"); }
  });

  $("prov-rows").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-del-prov]");
    if (!b) return;
    if (b.dataset.armed !== "1") {
      b.dataset.armed = "1"; b.textContent = "Confirm remove";
      setTimeout(() => { if (b.isConnected) { b.dataset.armed = ""; b.textContent = "Remove"; } }, 4000);
      return;
    }
    try { await api(`/v1/providers/${b.dataset.delProv}`, { method: "DELETE", headers: CSRF }); await loadProviders(); }
    catch (err) { show($("prov-msg"), err.message, "err"); }
  });

  /* ------------------------------------------------------- routing stats */
  function usd(x) {
    if (!x) return "$0";
    if (x >= 1) return "$" + x.toFixed(2);
    if (x >= 0.01) return "$" + x.toFixed(3);
    return "$" + x.toFixed(Math.min(6, Math.max(2, -Math.floor(Math.log10(x)) + 1)));
  }
  function routingStats(u) {
    const t = u.totals, box = $("routing-stats");
    if (!t.checks) {
      box.innerHTML = `<p class="sub" style="margin:0">No routed prompts yet. Every <code>/v1/score</code> and <code>/v1/route</code> call with your keys shows up here.</p>`;
      return;
    }
    const base = u.baselines[0];
    const baseName = base ? (u.baselines.length > 1 ? `your priciest model (mostly ${base.name})` : base.name) : "the priciest model";
    const pctOf = (saved, cost) => (saved + cost > 0 ? Math.round((saved / (saved + cost)) * 100) : 0);
    let html = `<div class="saved">
      <div class="save-hero"><small>Money saved on real LLM calls</small><b>${usd(t.saved_usd)}</b>
        <span>${t.sent ? `You paid <b>${usd(t.spent_usd)}</b> for ${fmt(t.sent)} answer${t.sent === 1 ? "" : "s"} through <code>/v1/route</code>. The same tokens on ${esc(baseName)} would have cost <b>${usd(t.spent_usd + t.saved_usd)}</b>${t.saved_usd > 0 ? ` (${pctOf(t.saved_usd, t.spent_usd)}% less)` : ""}.`
          : "No prompts were sent to your LLMs yet. Turn on execute in <code>/v1/route</code> (or the tester) with your provider keys connected."}</span></div>
      <div><small>Estimated savings on every routed prompt</small><b>${usd(t.est_saved_usd)}</b>
        <span>${fmt(t.checks)} prompt${t.checks === 1 ? "" : "s"} routed. Picks cost about <b>${usd(t.est_cost_usd)}</b> vs <b>${usd(t.est_baseline_usd)}</b> if every one went to ${esc(baseName)} (expected cost, recommendations included).</span></div>
    </div>
    <div class="rt-facts">
      <div><small>Prompts routed</small><b>${fmt(t.checks)}</b></div>
      <div><small>Sent to your LLMs</small><b>${fmt(t.sent)}</b></div>
      <div><small>Failed calls</small><b>${fmt(t.failed)}</b></div>
      <div><small>"Clarify first"</small><b>${fmt(t.clarify_first)}</b></div>
    </div>`;
    const top = Math.max(...u.models.map((m) => Math.max(m.recommended + (m.used_instead || 0), m.sent)), 1);
    html += `<h3>By model</h3><p class="sub" style="margin:0 0 6px">Dark = sent to your LLM, light = recommended only. "Not connected" means it was the best fit, but you have no key for it, so your best connected model was used instead.</p>
      ${u.models.map((m) => {
        const recOnly = Math.max(0, m.recommended + (m.used_instead || 0) - m.sent);
        return `<div class="mbar"><span class="nm" title="${esc(m.name)}">${esc(m.name)}</span>
          <div class="track" title="${fmt(m.recommended)} recommended · ${fmt(m.sent)} sent"><i class="sent" style="width:${(m.sent / top) * 100}%"></i><i class="rec" style="width:${(recOnly / top) * 100}%"></i></div>
          <em>${[
            m.recommended ? `${fmt(m.recommended)} picked${m.not_connected ? ` <span class="nc-tag" title="Best fit for these prompts, but you haven't connected this provider, so another model was used">(${m.not_connected === m.recommended ? "" : fmt(m.not_connected) + " "}not connected)</span>` : ""}` : "",
            m.used_instead ? `${fmt(m.used_instead)} used instead` : "",
            `${fmt(m.sent)} sent${m.spent_usd ? ` · ${usd(m.spent_usd)}` : ""}`,
          ].filter(Boolean).join(" · ")}</em></div>`;
      }).join("")}
      <details class="data"><summary>Show as a table</summary><div class="table-scroll"><table class="keys"><thead><tr><th>Model</th><th>Picked</th><th>Sent</th><th>Tokens in / out</th><th>Spent</th></tr></thead><tbody>
        ${u.models.map((m) => `<tr><td>${esc(m.name)}<br><span class="muted">${esc(m.provider || "")}</span></td><td>${fmt(m.recommended)}</td><td>${fmt(m.sent)}</td>
          <td class="muted">${fmt(m.input_tokens)} / ${fmt(m.output_tokens)}</td><td>${usd(m.spent_usd)}</td></tr>`).join("")}</tbody></table></div></details>`;
    const keyCard = (k) => {
      const used = k.sent || k.failed;
      const status = k.revoked ? `<span class="kpill off">Revoked</span>`
        : used ? `<span class="kpill on">Uses your LLM keys</span>`
        : k.checks ? `<span class="kpill">Recommendations only</span>` : `<span class="kpill">Not used yet</span>`;
      const chips = (list, label) => (list.length
        ? `<div class="kc-row"><span class="kc-label">${label}</span><div class="kc-chips">${list.join("")}</div></div>` : "");
      return `<article class="kcard">
        <header><div class="kc-name"><b>${esc(k.name)}</b><code>${esc(k.masked)}</code></div>${status}</header>
        <div class="kc-stats">
          <div><small>Routed</small><b>${fmt(k.checks)}</b></div>
          <div><small>Sent</small><b>${fmt(k.sent)}</b>${k.failed ? `<span class="bad">${fmt(k.failed)} failed</span>` : ""}</div>
          <div><small>Spent</small><b>${usd(k.spent_usd)}</b></div>
          <div class="save"><small>Saved</small><b>${usd(k.saved_usd)}</b><span>est. ${usd(k.est_saved_usd)}</span></div>
        </div>
        ${chips(k.recommended.slice(0, 6).map((x) => x.not_connected
          ? `<span class="chipx nc" title="Best fit, but you haven't connected this provider">${esc(x.name)}<i>×${fmt(x.count)}</i><em>not connected${x.instead.length ? ` → ${x.instead.map((y) => esc(y.name)).join(", ")} instead` : ""}</em></span>`
          : `<span class="chipx">${esc(x.name)}<i>×${fmt(x.count)}</i></span>`), "Router picked")}
        ${chips(k.sent_to.map((x) => `<span class="chipx sent">${esc(x.name)}<i>×${fmt(x.calls)}</i></span>`), "Sent to")}
        ${k.providers_used.length ? `<p class="kc-foot">Providers used: ${esc(k.providers_used.join(", "))}</p>` : ""}
      </article>`;
    };
    html += `<h3>By API key</h3>
      ${u.keys.length ? `<div class="kcards">${u.keys.map(keyCard).join("")}</div>` : `<p class="sub">No keys yet.</p>`}
      <p class="sub" style="margin:14px 0 0">"Saved" compares what your real calls cost with the same tokens on the comparison model, at list prices. "Est." compares expected costs for every routed prompt, including recommendation-only checks.</p>`;
    box.innerHTML = html;
  }

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
    const active = days.filter((d) => d.requests || d.prompts).reverse();
    $("usage-table").innerHTML = active.length
      ? `<div class="data-scroll"><table><thead><tr><th>Day (UTC)</th><th>Requests</th><th>Prompts</th><th>Fallback</th></tr></thead><tbody>${
          active.map((d) => `<tr><td>${d.day}</td><td>${fmt(d.requests)}</td><td>${fmt(d.prompts)}</td><td>${fmt(d.degraded)}</td></tr>`).join("")
        }</tbody></table></div><p class="muted" style="margin:6px 0 0">Days with no checks are left out.</p>`
      : `<p class="muted" style="margin:8px 0 0">No checks in the last 30 days.</p>`;
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

<span class="c"># 3. Let PromptLint pick the model and call it with your connected keys</span>
curl ${esc(host)}/v1/route \\
  -H <span class="s">"Authorization: Bearer $PQS_KEY"</span> \\
  -H <span class="s">"Content-Type: application/json"</span> \\
  -d <span class="s">'{"prompt": "Summarize this email in 3 bullets: …", "routing": {"strategy": "balanced"}}'</span>

<span class="c"># 4. Your usage</span>
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
      $("subtitle").textContent = "Route every prompt to the cheapest model that can handle it, call it with your own LLM keys, and see what you saved. Prompt scores come with every call.";
      return;
    }
    $("title").textContent = "Your API";
    $("subtitle").innerHTML = `Signed in as <b>${esc(me.email)}</b>`;
    tiles(me);
    quickstart();
    syncProviderForm();
    // Each section loads on its own, so one failure can't leave the others stuck on "Loading…".
    const failed = (el, what, err) => {
      console.error(`account: ${what} failed`, err);
      el.innerHTML = `<p class="sub" style="margin:0">Couldn't load ${what}${err && err.message ? `: ${esc(String(err.message).replace(/\.$/, ""))}` : ""}. <a href="" onclick="location.reload();return false">Try again</a></p>`;
    };
    await Promise.all([
      loadKeys().catch((e) => console.error("account: keys failed", e)),
      loadProviders().catch((e) => console.error("account: providers failed", e)),
      api("/v1/usage?days=30").then((u) => chart(u.days)).catch((e) => failed($("chart"), "usage", e)),
      api("/v1/usage/routing?days=30").then(routingStats).catch((e) => failed($("routing-stats"), "routing stats", e)),
    ]);
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
