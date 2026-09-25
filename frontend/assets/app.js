/* PromptLint analyzer page: editor, live token counter, model picker, single + compare modes. */
(function () {
  "use strict";
  document.documentElement.classList.remove("no-js");
  const $ = (id) => document.getElementById(id);
  const { esc, renderReport, radarSVG, ICON } = window.PL;

  const EXAMPLES = [
    { label: "Vague", text: "write me a poem" },
    {
      label: "Specific",
      text: "I'm a high-school chemistry teacher. Write a 12-line rhyming poem for my Year 10 class that explains ionic vs covalent bonding. Use one simple analogy per bond type, keep the vocabulary at a 15-year-old's level, and end with a two-line summary they can memorize. Return only the poem.",
    },
    { label: "Contradictory", text: "Write a detailed 2,000-word essay on climate policy. Keep it under 100 words. Use bullet points only, no lists." },
    { label: "Coding", text: "fix my code it doesnt work" },
  ];
  const COMPARE_PAIR = [EXAMPLES[3].text, "My Python 3.12 function below should return the median of a list of ints, but it returns the wrong value for even-length lists (e.g. [1, 2, 3, 4] gives 3 instead of 2.5). Find the bug and return the corrected function only, with a one-line comment explaining the fix.\n\ndef median(xs):\n    xs = sorted(xs)\n    return xs[len(xs) // 2]"];

  let MAX = 20000;
  let modelNames = {};
  let mode = "single";
  let busy = false;

  /* -------------------------------------------------------------- nav */
  const nav = $("nav");
  const onScroll = () => nav.classList.toggle("stuck", window.scrollY > 4);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  /* ------------------------------------------------------------- models */
  async function loadModels() {
    const box = $("models");
    try {
      const res = await fetch("api/models");
      if (!res.ok) throw new Error();
      const data = await res.json();
      MAX = data.max_prompt_chars || MAX;
      modelNames = Object.fromEntries(data.models.map((m) => [m.id, m.name]));
      box.innerHTML = data.models.map((m) => `
        <label title="${esc(m.provider)} · $${m.input}/M in · $${m.output}/M out${m.note ? " · " + esc(m.note) : ""}">
          <input type="checkbox" value="${esc(m.id)}" ${data.default_models.includes(m.id) ? "checked" : ""}>${esc(m.name)}
        </label>`).join("");
    } catch {
      box.innerHTML = '<span class="counter">Couldn\'t load the model list. Is the server running?</span>';
    }
    updateCounter("a"); updateCounter("b");
  }
  const selectedModels = () => [...document.querySelectorAll("#models input:checked")].map((i) => i.value);
  const selectedBackend = () => document.querySelector("#backend input:checked")?.value || "auto";

  /* ------------------------------------------------------- live counter */
  // Instant local estimate (chars / 4), refined by the server's tiktoken count after a short pause.
  const timers = {};
  const exact = {};
  function updateCounter(slot) {
    const ta = $("prompt-" + slot), out = $("counter-" + slot);
    const text = ta.value, n = text.length;
    const est = n ? Math.max(1, Math.ceil(n / 4)) : 0;
    const known = exact[slot] && exact[slot].text === text ? exact[slot].tokens : null;
    const tokens = known ?? est;
    out.innerHTML = `<span>${known != null ? "" : "≈ "}${tokens.toLocaleString()} input tokens</span>
      <span class="${n > MAX ? "over" : ""}">${n.toLocaleString()} / ${MAX.toLocaleString()} chars</span>`;
    clearTimeout(timers[slot]);
    if (!n || n > MAX || known != null) return;
    timers[slot] = setTimeout(async () => {
      try {
        const res = await fetch("api/tokens", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: text }) });
        if (!res.ok) return;
        const data = await res.json();
        exact[slot] = { text, tokens: data.tokens };
        if ($("prompt-" + slot).value === text) updateCounter(slot);
      } catch { /* the estimate stays */ }
    }, 350);
  }
  ["a", "b"].forEach((s) => $("prompt-" + s).addEventListener("input", () => updateCounter(s)));

  /* ----------------------------------------------------------- examples */
  const exBox = $("examples");
  function renderExamples() {
    exBox.innerHTML = "<span>Try:</span>" + (mode === "single"
      ? EXAMPLES.map((e, i) => `<button class="chip" data-ex="${i}">${esc(e.label)}</button>`).join("")
      : '<button class="chip" data-pair="1">Vague vs specific bug report</button><button class="chip" data-pair="2">Vague vs specific poem</button>');
  }
  exBox.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.ex) { $("prompt-a").value = EXAMPLES[+b.dataset.ex].text; updateCounter("a"); $("prompt-a").focus(); }
    if (b.dataset.pair) {
      const [a, bb] = b.dataset.pair === "1" ? COMPARE_PAIR : [EXAMPLES[0].text, EXAMPLES[1].text];
      $("prompt-a").value = a; $("prompt-b").value = bb; updateCounter("a"); updateCounter("b");
    }
  });

  /* ---------------------------------------------------------------- mode */
  function setMode(m) {
    mode = m;
    $("tab-single").setAttribute("aria-selected", m === "single");
    $("tab-compare").setAttribute("aria-selected", m === "compare");
    $("layout").classList.toggle("compare", m === "compare");
    $("slot-b").hidden = m !== "compare";
    $("label-a").innerHTML = m === "compare" ? '<i style="background:var(--series-1)"></i>Version A' : "Your prompt";
    $("analyze-label").textContent = m === "compare" ? "Compare" : "Analyze";
    renderExamples();
    showEmpty();
  }
  $("tab-single").addEventListener("click", () => setMode("single"));
  $("tab-compare").addEventListener("click", () => setMode("compare"));

  /* ------------------------------------------------------------- results */
  const results = $("results");
  function showEmpty() {
    results.innerHTML = mode === "single"
      ? `<div class="empty"><img src="assets/icon.svg" alt=""><h2>Your report card shows up here</h2><p>Score, first-try odds, what's missing, fix-it tips, and what it will cost on each model.</p></div>`
      : `<div class="empty"><img src="assets/icon.svg" alt=""><h2>Compare two versions</h2><p>Paste your original prompt as A and your rewrite as B to see the score change, side by side.</p></div>`;
  }
  function showLoading() {
    results.innerHTML = `<div class="skeleton" aria-label="Analyzing"><div style="height:150px"></div><div style="height:120px"></div><div style="height:300px"></div><div style="height:180px"></div></div>`;
  }
  function showError(message, detail) {
    results.innerHTML = `<div class="err" role="alert">${ICON.cross}<div><b>${esc(message)}</b>${detail ? `<p>${esc(detail)}</p>` : ""}</div></div>`;
  }

  async function analyzeOne(prompt, models) {
    let res;
    try {
      res = await fetch("api/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt, models, backend: selectedBackend() }) });
    } catch {
      throw { message: "Couldn't reach the PromptLint server.", detail: "Check your connection, or start the backend (see the README)." };
    }
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON error page */ }
    if (!res.ok) throw { message: data?.error || `Request failed (HTTP ${res.status}).`, detail: data?.detail || null };
    return data;
  }

  function validate(text, name) {
    if (!text.trim()) return `Paste ${name} first.`;
    if (text.length > MAX) return `${name[0].toUpperCase() + name.slice(1)} is ${text.length.toLocaleString()} characters; the limit is ${MAX.toLocaleString()}.`;
    return null;
  }

  async function run() {
    if (busy) return;
    const a = $("prompt-a").value, b = $("prompt-b").value;
    const models = selectedModels();
    const problem = mode === "single" ? validate(a, "a prompt") : validate(a, "version A") || validate(b, "version B");
    if (problem) { $("status").textContent = problem; return; }
    if (!models.length) { $("status").textContent = "Pick at least one model to price."; return; }

    busy = true;
    const btn = $("analyze");
    btn.disabled = true; btn.classList.add("loading");
    $("status").textContent = mode === "single" ? "Asking Jev…" : "Asking Jev about both versions…";
    showLoading();
    const t0 = performance.now();
    try {
      if (mode === "single") {
        const r = await analyzeOne(a, models);
        renderReport(results, r, { modelNames });
      } else {
        const [ra, rb] = await Promise.all([analyzeOne(a, models), analyzeOne(b, models)]);
        renderCompare(ra, rb);
      }
      $("status").textContent = `Done in ${Math.round(performance.now() - t0).toLocaleString()} ms`;
      if (window.innerWidth <= 980) results.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      showError(e.message || "Something went wrong.", e.detail);
      $("status").textContent = "";
    } finally {
      busy = false; btn.disabled = false; btn.classList.remove("loading");
    }
  }

  function renderCompare(ra, rb) {
    const d = rb.lint_score - ra.lint_score;
    const cls = d > 0 ? "up" : d < 0 ? "down" : "same";
    const chg = d === 0 ? "No change" : `${d > 0 ? "+" : "−"}${Math.abs(d)} points`;
    const dims = ra.dimensions.map((x) => ({ id: x.id, label: x.label }));
    const vals = (r) => Object.fromEntries(r.dimensions.map((x) => [x.id, x.value]));
    const series = [
      { name: "Version A", color: "var(--series-1)", values: vals(ra) },
      { name: "Version B", color: "var(--series-2)", values: vals(rb) },
    ];
    const ft = Math.round((rb.first_try_success - ra.first_try_success) * 100);
    results.innerHTML = `
      <div class="delta" role="group" aria-label="Score change">
        <div class="side"><small style="color:var(--series-1)">Version A</small><b>${ra.lint_score}</b></div>
        <span class="arrow-big" aria-hidden="true">→</span>
        <div class="side"><small style="color:var(--series-2)">Version B</small><b>${rb.lint_score}</b></div>
        <span class="chg ${cls}">${d > 0 ? ICON.up : d < 0 ? ICON.down : ""}${chg}</span>
        <span class="pill">First-try ${ft >= 0 ? "+" : "−"}${Math.abs(ft)} pts</span>
      </div>
      <div class="rc-block cmp-radar"><p class="rc-label"><span>Dimensions, A vs B</span></p>
        ${radarSVG(dims, series, { size: 320 })}
        <div class="legend"><span><i style="background:var(--series-1)"></i>Version A</span><span><i style="background:var(--series-2)"></i>Version B</span></div>
      </div>
      <div class="cmp-grid">
        <div class="cmp-col"><h3 style="color:var(--series-1)"><i style="background:var(--series-1)"></i>Version A</h3><div id="rep-a"></div></div>
        <div class="cmp-col"><h3 style="color:var(--series-2)"><i style="background:var(--series-2)"></i>Version B</h3><div id="rep-b"></div></div>
      </div>`;
    renderReport($("rep-a"), ra, { modelNames });
    renderReport($("rep-b"), rb, { modelNames });
  }

  $("analyze").addEventListener("click", run);
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); run(); }
  });

  // Deep link: app.html#compare opens compare mode.
  setMode(location.hash === "#compare" ? "compare" : "single");
  loadModels();
})();
