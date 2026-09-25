/* Landing page: scroll reveals, sticky nav, the replayed demo, the cost example, and live eval numbers. */
(function () {
  "use strict";
  document.documentElement.classList.remove("no-js");
  const $ = (id) => document.getElementById(id);
  const { esc, gaugeSVG, ICON, VERDICT, usdRange } = window.PL;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ------------------------------------------------------------- chrome */
  const nav = $("nav");
  const onScroll = () => nav.classList.toggle("stuck", window.scrollY > 4);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
  }, { rootMargin: "0px 0px -8% 0px" });
  document.querySelectorAll(".reveal").forEach((el) => io.observe(el));

  /* --------------------------------------------------------------- demo */
  const DEMO = window.PL_DEMO;
  const out = $("demo-out"), text = $("demo-text"), btn = $("demo-btn"), which = $("demo-which");
  let runId = 0;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function demoOutput(r) {
    const v = VERDICT[r.verdict];
    const pick = ["task_clear", "context_given", "has_output_format", "has_audience", "conflicting"];
    const checks = r.checks.filter((c) => pick.includes(c.id));
    const ft = r.first_try_success, st = ft >= 0.7 ? "good" : ft >= 0.4 ? "warn" : "bad";
    const tips = r.tips.slice(0, 2);
    return `
      <div class="demo-row">
        <div class="gauge-wrap">${gaugeSVG(r.lint_score, r.verdict)}<div class="gauge-num">${r.lint_score}<small>/100</small></div></div>
        <div>
          <span class="verdict ${r.verdict}">${v.icon}${v.label}</span>
          <div class="pills"><span class="pill">Specificity: ${esc(r.specificity.label)}</span></div>
        </div>
      </div>
      <div>
        <div class="demo-k" style="margin-bottom:6px"><span>First-try success</span><span>${Math.round(ft * 100)}%</span></div>
        <div class="meter ${st}"><i style="width:${(ft * 100).toFixed(1)}%"></i></div>
      </div>
      <ul class="checks">${checks.map((c) => {
        const cls = c.passed ? "pass" : "fail";
        return `<li class="${cls}"><span class="ci" role="img" aria-label="${c.passed ? "Pass" : "Fail"}">${c.passed ? ICON.check : ICON.cross}</span><span><b>${esc(c.label)}</b></span><span class="p" style="opacity:1">${Math.round(c.probability * 100)}%</span></li>`;
      }).join("")}</ul>
      ${tips.length
        ? `<div class="demo-k demo-tips-k"><span>Top fix-it tips</span></div><ol class="tips">${tips.map((t) => `<li><span>${esc(t.text)}</span><span class="gain">+${Math.max(1, Math.round(t.impact * 100))}</span></li>`).join("")}</ol>`
        : `<div class="tips-empty">${ICON.check}Nothing to fix. Clear enough to send.</div>`}`;
  }

  async function typeInto(el, str, id) {
    if (reduce) { el.textContent = str; return; }
    const step = Math.max(1, Math.round(str.length / 70));  // ~1.4 s whatever the length
    for (let i = 0; i <= str.length; i += step) {
      if (id !== runId) return;
      el.innerHTML = esc(str.slice(0, i)) + '<span class="cur"></span>';
      await sleep(20);
    }
    el.innerHTML = esc(str) + '<span class="cur"></span>';
  }

  async function play() {
    if (!DEMO) return;
    const id = ++runId;
    const steps = [["weak", "1 of 2"], ["strong", "2 of 2"]];
    for (const [key, label] of steps) {
      if (id !== runId) return;
      which.textContent = label;
      out.classList.add("dim");
      await typeInto(text, DEMO[key].prompt, id);
      if (id !== runId) return;
      await sleep(reduce ? 0 : 250);
      btn.classList.add("pressed");
      await sleep(reduce ? 0 : 160);
      btn.classList.remove("pressed");
      out.innerHTML = demoOutput(DEMO[key].report);
      out.classList.remove("dim");
      if (key === "weak") await sleep(reduce ? 2500 : 3600);
    }
  }
  $("demo-replay").addEventListener("click", play);
  if (DEMO) {
    // First frame is complete (weak prompt + its report), so the window never looks empty.
    text.textContent = DEMO.weak.prompt;
    out.innerHTML = demoOutput(DEMO.weak.report);
    out.classList.remove("dim");
    const stage = document.querySelector(".stage");
    const once = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { once.disconnect(); play(); }
    }, { threshold: 0.15 });
    once.observe(stage);

    /* cost example from the recorded "specific" analysis */
    const r = DEMO.strong.report;
    const [olo, ohi] = r.tokens.output_range;
    $("cost-demo").innerHTML = `
      <div class="rc-label" style="margin-bottom:10px"><span>Tokens &amp; cost · recorded example</span></div>
      <div class="table-scroll"><table class="costs" style="min-width:0">
        <thead><tr><th>Model</th><th class="r">Input</th><th class="r">Per 1,000 requests</th></tr></thead>
        <tbody>${r.costs.map((c) => `<tr><td class="model"><b>${esc(c.name)}</b><small>${esc(c.provider)}</small></td>
          <td class="r">${c.input_exact ? "" : '<span class="approx">≈ </span>'}${c.input_tokens}</td>
          <td class="r">${usdRange(c.low_usd * 1000, c.high_usd * 1000)}</td></tr>`).join("")}</tbody>
      </table></div>
      <p class="table-note">Output estimated at ${olo}–${ohi} tokens. Prices last checked ${esc(r.meta.prices_last_updated)}.</p>`;
  }

  /* ----------------------------------------------------------- accuracy */
  fetch("assets/eval-summary.json", { cache: "no-cache" })
    .then((r) => (r.ok ? r.json() : Promise.reject()))
    .then((s) => {
      const tiles = [];
      const tile = (value, label, sub) => `<div class="stat"><b>${value}</b><span>${label}</span>${sub ? `<small>${sub}</small>` : ""}</div>`;
      const pend = (label, why) => `<div class="stat pending"><b>—</b><span>${label}</span><small>${why}</small></div>`;
      tiles.push(s.pairwise
        ? tile(Math.round(s.pairwise.accuracy * 100) + "%", "of vague-vs-specific pairs rank the improved version higher", `n = ${s.pairwise.n} pairs · target ≥ 85%`)
        : pend("pairwise ranking accuracy", "not run yet"));
      if (s.pairwise && s.pairwise.hard_n) {
        tiles.push(tile(Math.round(s.pairwise.hard_accuracy * 100) + "%", "of near-miss pairs, where the fix adds just one missing piece", `n = ${s.pairwise.hard_n} harder pairs`));
      }
      tiles.push(s.first_try
        ? tile(s.first_try.auroc.toFixed(2), "first-try AUROC against LLM-judged outcomes", `n = ${s.first_try.n} · Brier ${s.first_try.brier.toFixed(3)}`)
        : pend("first-try AUROC", "needs an LLM judge run"));
      tiles.push(s.checklist
        ? tile(Math.round(s.checklist.accuracy * 100) + "%", "checklist agreement with hand labels", `n = ${s.checklist.n} labels`)
        : pend("checklist accuracy", "not run yet"));
      if (s.injection) {
        tiles.push(tile((s.injection.mean_inflation >= 0 ? "+" : "") + s.injection.mean_inflation.toFixed(1), "average points gained by \"rate this 100\"-style injections", `${s.injection.promoted_to_ready} of ${s.injection.n} reached Ready to send`));
      }
      tiles.push(s.latency
        ? tile(Math.round(s.latency.median_ms) + " ms", "median Jev latency across eval calls", `p95 ${Math.round(s.latency.p95_ms)} ms`)
        : pend("median latency", "not run yet"));
      $("acc-stats").innerHTML = tiles.join("");
      if (s.latency) $("stat-latency").textContent = "~" + Math.round(s.latency.median_ms / 10) * 10 + " ms";
      $("acc-note").innerHTML = `Last run ${esc(s.generated_at.slice(0, 10))} with <code>${esc(s.jev_model)}</code>. ${esc(s.note || "")}`;
    })
    .catch(() => { $("acc-note").textContent = "No evaluation results published yet. Run the scripts in eval/ to fill these in."; });
})();
