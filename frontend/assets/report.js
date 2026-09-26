/* PromptLint report card renderer. Pure DOM + inline SVG, no dependencies.
 * window.PL.renderReport(el, report, opts) draws the full card from an /api/analyze response. */
(function () {
  "use strict";

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (x) => Math.round(x * 100) + "%";

  const ICON = {
    check: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    cross: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4.5 4.5l7 7m0-7l-7 7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>',
    info: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 7v4.5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/><circle cx="8" cy="4.3" r="1.3" fill="currentColor"/></svg>',
    warn: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 4.5v4.5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/><circle cx="8" cy="12" r="1.3" fill="currentColor"/></svg>',
    up: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 13V3m0 0L3.5 7.5M8 3l4.5 4.5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    down: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 3v10m0 0l-4.5-4.5M8 13l4.5-4.5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  const VERDICT = {
    ready_to_send: { label: "Ready to send", icon: ICON.check, status: "good", color: "var(--good)", soft: "var(--good-soft)" },
    needs_work: { label: "Needs work", icon: ICON.warn, status: "warn", color: "var(--warn)", soft: "var(--warn-soft)" },
    likely_to_fail: { label: "Likely to fail", icon: ICON.cross, status: "bad", color: "var(--bad)", soft: "var(--bad-soft)" },
  };

  const TASK_LABEL = {
    coding: "Coding", writing: "Writing", analysis: "Analysis", math: "Math", factual_qa: "Factual Q&A",
    brainstorming: "Brainstorming", extraction_transformation: "Extract & transform", conversation: "Conversation",
    other: "Other",
  };
  const TIER_LABEL = { small: "a small model is enough", mid: "a mid-tier model fits", frontier: "needs a frontier model" };

  /* ------------------------------------------------------------ money */
  function usd(x) {
    if (x === 0) return "$0";
    if (x >= 1) return "$" + x.toFixed(2);
    if (x >= 0.01) return "$" + x.toFixed(3);
    // Tiny per-request costs: two significant figures, never scientific notation.
    const digits = Math.max(2, -Math.floor(Math.log10(x)) + 1);
    return "$" + x.toFixed(Math.min(digits, 8));
  }
  function usdRange(lo, hi) { return usd(lo) + "–" + usd(hi).slice(1); }
  const int = (n) => Number(n).toLocaleString("en-US");

  /* ------------------------------------------------------------ gauge */
  // 180° arc meter. Track = light step of the verdict's own status hue; ticks at the verdict thresholds.
  function gaugeSVG(score, verdict) {
    const v = VERDICT[verdict] || VERDICT.needs_work;
    const cx = 88, cy = 92, r = 74, sw = 13;
    const pt = (t) => { const a = Math.PI * (1 - t); return [cx + r * Math.cos(a), cy - r * Math.sin(a)]; };
    const arc = (t0, t1) => { const [x0, y0] = pt(t0), [x1, y1] = pt(t1); return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 0 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`; };
    const t = Math.max(0.001, Math.min(1, score / 100));
    const tick = (s) => { const a = Math.PI * (1 - s / 100); const i = r - sw / 2 - 3, o = r + sw / 2 + 3; return `<line x1="${(cx + i * Math.cos(a)).toFixed(1)}" y1="${(cy - i * Math.sin(a)).toFixed(1)}" x2="${(cx + o * Math.cos(a)).toFixed(1)}" y2="${(cy - o * Math.sin(a)).toFixed(1)}" stroke="#fff" stroke-width="2.5"/>`; };
    return `<svg class="gauge" viewBox="0 0 176 112" role="img" aria-label="Lint score ${score} out of 100, ${v.label}">
      <path d="${arc(0, 1)}" style="stroke:${v.soft}" stroke-width="${sw}" fill="none" stroke-linecap="round"/>
      <path class="gauge-fill" d="${arc(0, t)}" style="stroke:${v.color}" stroke-width="${sw}" fill="none" stroke-linecap="round"/>
      ${tick(50)}${tick(75)}
    </svg>`;
  }

  /* ------------------------------------------------------------ radar */
  // series: [{ name, color, values: {id: 0..1} }]; dims: [{id, label}]
  function radarSVG(dims, series, { size = 300 } = {}) {
    const c = size / 2, r = size / 2 - 72, n = dims.length;
    const ang = (i) => -Math.PI / 2 + (2 * Math.PI * i) / n;
    const at = (i, v) => [c + r * v * Math.cos(ang(i)), c + r * v * Math.sin(ang(i))];
    const poly = (v) => dims.map((_, i) => at(i, v).map((x) => x.toFixed(1)).join(",")).join(" ");
    let s = `<svg class="radar" viewBox="0 0 ${size} ${size}" role="img" aria-label="Dimension scores: ${dims.map((d) => d.label + " " + series.map((x) => pct(x.values[d.id] ?? 0)).join(" vs ")).join(", ")}">`;
    for (const ring of [0.25, 0.5, 0.75, 1]) s += `<polygon class="ring" points="${poly(ring)}"/>`;
    dims.forEach((_, i) => { const [x, y] = at(i, 1); s += `<line class="spoke" x1="${c}" y1="${c}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}"/>`; });
    series.forEach((sr) => {
      const pts = dims.map((d, i) => at(i, Math.max(0.02, sr.values[d.id] ?? 0)).map((x) => x.toFixed(1)).join(",")).join(" ");
      s += `<polygon class="shape" points="${pts}" style="fill:${sr.color};stroke:${sr.color}" fill-opacity="${series.length > 1 ? 0.1 : 0.14}"/>`;
    });
    series.forEach((sr) => {
      dims.forEach((d, i) => {
        const v = sr.values[d.id] ?? 0;
        const [x, y] = at(i, Math.max(0.02, v));
        const tip = `<b>${esc(d.label)}</b>${series.length > 1 ? " · " + esc(sr.name) : ""}: ${pct(v)}`;
        s += `<circle class="dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4.5" style="fill:${sr.color}"/>`;
        s += `<circle class="hit" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="13" data-tip="${esc(tip)}"/>`;
      });
    });
    dims.forEach((d, i) => {
      const [x, y] = at(i, 1.2);
      const anchor = Math.abs(x - c) < 4 ? "middle" : x > c ? "start" : "end";
      const dy = y < c - r * 0.9 ? -8 : y > c + r * 0.5 ? 12 : 0;
      const vals = series.map((sr) => pct(sr.values[d.id] ?? 0)).join(" · ");
      s += `<text class="axis-label" x="${x.toFixed(1)}" y="${(y + dy).toFixed(1)}" text-anchor="${anchor}">${esc(d.label)}</text>`;
      s += `<text class="axis-val" x="${x.toFixed(1)}" y="${(y + dy + 15).toFixed(1)}" text-anchor="${anchor}">${vals}</text>`;
    });
    return s + "</svg>";
  }

  /* ------------------------------------------------------------ pieces */
  function statusFor(p) { return p >= 0.7 ? "good" : p >= 0.4 ? "warn" : "bad"; }
  const STATUS_WORD = { good: "Likely", warn: "Uncertain", bad: "Unlikely" };
  const STATUS_ICON = { good: ICON.check, warn: ICON.warn, bad: ICON.cross };

  function firstTryBlock(p) {
    const st = statusFor(p);
    return `<div class="rc-block" data-tip="Jev's probability that a capable model answers completely on the first attempt, with no follow-up needed.">
      <p class="rc-label"><span>First-try success</span></p>
      <div class="meter-value"><b>${pct(p)}</b><span class="status ${st}">${STATUS_ICON[st]}${STATUS_WORD[st]}</span></div>
      <div class="meter ${st}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(p * 100)}" aria-label="First-try success"><i style="width:${(p * 100).toFixed(1)}%"></i></div>
      <div class="meter-scale"><span>0%</span><span>100%</span></div>
    </div>`;
  }

  function specificityBlock(spec) {
    const ticks = [1 / 3, 2 / 3].map((t) => `<span class="tick" style="left:${t * 100}%"></span>`).join("");
    return `<div class="rc-block">
      <p class="rc-label"><span>Generic ↔ Specific</span></p>
      <div class="meter-value"><b style="font-size:1.35rem">${esc(spec.label)}</b></div>
      <div class="slider" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(spec.value * 100)}" aria-label="Specificity: ${esc(spec.label)}">
        ${ticks}<span class="knob" style="left:${(spec.value * 100).toFixed(1)}%" data-tip="Specificity ${pct(spec.value)}"></span>
      </div>
      <div class="slider-ends"><span>Anyone could send it</span><span>Only you could send it</span></div>
    </div>`;
  }

  function checklist(checks) {
    return `<ul class="checks">${checks.map((c) => {
      let cls, icon, word;
      if (c.informational) {
        const flagged = c.id === "needs_current_info" && !c.passed;
        cls = flagged ? "flag" : "info"; icon = flagged ? ICON.warn : ICON.info; word = flagged ? "Heads-up" : "Info";
      } else {
        cls = c.passed ? "pass" : "fail"; icon = c.passed ? ICON.check : ICON.cross; word = c.passed ? "Pass" : "Fail";
      }
      const tip = `Jev probability: <b>${pct(c.probability)}</b>${c.informational ? " · doesn't affect the score" : ""}`;
      return `<li class="${cls}" tabindex="0" data-tip="${esc(tip)}">
        <span class="ci" role="img" aria-label="${word}">${icon}</span>
        <span><b>${esc(c.label)}</b><small>${esc(c.detail)}</small></span>
        <span class="p">${pct(c.probability)}</span>
      </li>`;
    }).join("")}</ul>`;
  }

  function costTable(report, modelNames) {
    const [olo, ohi] = report.tokens.output_range;
    const rows = report.costs.map((c) => {
      const sug = c.model === report.suggested_model;
      const tokens = c.input_exact ? int(c.input_tokens)
        : `<span class="approx" title="Approximate: no exact tokenizer available for this model here">≈ ${int(c.input_tokens)}</span>`;
      return `<tr class="${sug ? "suggested" : ""}">
        <td class="model"><b>${esc(c.name)}${sug ? '<span class="tier-tag">Suggested</span>' : ""}</b><small>${esc(c.provider)} · ${esc(c.tier)}${c.note ? " · " + esc(c.note) : ""}</small></td>
        <td class="r">${tokens}</td>
        <td class="r">${usdRange(c.low_usd, c.high_usd)}</td>
        <td class="r">${usdRange(c.low_usd * 1000, c.high_usd * 1000)}</td>
      </tr>`;
    }).join("");
    const sugName = report.suggested_model && !report.costs.some((c) => c.model === report.suggested_model)
      ? ` Suggested for this prompt: <b>${esc(modelNames?.[report.suggested_model] || report.suggested_model)}</b> (not selected).` : "";
    return `<div class="table-scroll"><table class="costs">
      <thead><tr><th>Model</th><th class="r">Input tokens</th><th class="r">Per request</th><th class="r">Per 1,000</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <p class="table-note">Output estimated at ${int(olo)}–${int(ohi)} tokens from the expected answer length, so costs are a range, never a single number.
      Prices from each provider's official page, last updated ${esc(report.meta.prices_last_updated)}.${sugName}</p>`;
  }

  function routingBlock(r) {
    const rt = r.routing;
    if (!rt || !rt.recommended) return "";
    const rec = rt.recommended, fb = rt.fallback, base = rt.baseline;
    const clarify = rt.action === "clarify_first"
      ? `<div class="route-clarify" role="note">${ICON.warn}<span><b>Clarify first.</b> ${esc(rt.clarify_reason)}</span></div>` : "";
    const saves = base && base.id !== rec.id && rt.savings_percent > 0
      ? `<span class="route-save">${ICON.down} ${Math.round(rt.savings_percent)}% cheaper than ${esc(base.name)}</span>` : "";
    const alts = rt.alternatives.slice(0, 5).map((m, i) => `<tr class="${m.id === rec.id ? "suggested" : ""}">
        <td class="model"><b>${i + 1}. ${esc(m.name)}</b><small>${esc(m.provider)} · ${esc(m.tier)}${m.capable ? "" : " · too weak for this prompt"}</small></td>
        <td class="r">${usd(m.est_cost_usd_p50)}</td></tr>`).join("");
    return `<div class="rc-block">
      <p class="rc-label"><span>Recommended model</span><span>${esc(rt.strategy)} routing</span></p>
      ${clarify}
      <div class="route-pick">
        <div><div class="route-name">${esc(rec.name)}</div><div class="route-meta">${esc(rec.provider)} · ${esc(rec.tier)} tier · about ${usd(rec.est_cost_usd_p50)} per request</div></div>
        ${saves}
      </div>
      <p class="route-reason">${esc(rt.reason)}${fb ? ` If it fails, fall back to <b>${esc(fb.name)}</b>.` : ""}</p>
      <details class="route-alts"><summary>How the ${rt.alternatives.length} models ranked</summary>
        <div class="table-scroll"><table class="costs" style="min-width:0"><tbody>${alts}</tbody></table></div></details>
    </div>`;
  }

  function tipsBlock(tips) {
    if (!tips.length) {
      return `<div class="tips-empty">${ICON.check}Nothing to fix. This prompt covers the basics.</div>`;
    }
    return `<ol class="tips">${tips.map((t) => `<li><span>${esc(t.text)}</span><span class="gain" title="Score points this could recover">+${Math.max(1, Math.round(t.impact * 100))}</span></li>`).join("")}</ol>`;
  }

  /* ------------------------------------------------------------ report */
  function headline(r) {
    const task = TASK_LABEL[r.task_type.choice] || r.task_type.choice;
    const v = VERDICT[r.verdict];
    const nFail = r.checks.filter((c) => !c.informational && !c.passed).length;
    const lead = r.verdict === "ready_to_send" ? "Clear enough to send as is."
      : r.verdict === "needs_work" ? `${nFail} ${nFail === 1 ? "thing" : "things"} would make this stronger.`
      : "An LLM will probably have to guess what you want.";
    return { task, v, lead };
  }

  function summaryBlock(r, { modelNames } = {}) {
    const { task, v, lead } = headline(r);
    const sug = r.suggested_model ? (modelNames?.[r.suggested_model] || r.costs.find((c) => c.model === r.suggested_model)?.name || r.suggested_model) : null;
    return `<div class="rc-block rc-summary">
      <div class="gauge-wrap">${gaugeSVG(r.lint_score, r.verdict)}<div class="gauge-num">${r.lint_score}<small>/100</small></div></div>
      <div>
        <span class="verdict ${r.verdict}">${v.icon}${v.label}</span>
        <p class="rc-headline"><strong>${esc(lead)}</strong> ${r.meta.caps_applied.length ? `Capped from ${r.meta.uncapped_score} because ${r.meta.caps_applied.includes("conflicting") ? "instructions conflict" : "the task is unclear"}.` : ""}</p>
        <div class="pills">
          <span class="pill" title="Jev confidence ${pct(r.task_type.confidence)}">Task: ${esc(task)}</span>
          <span class="pill accent">${esc(TIER_LABEL[r.tier_hint])}${sug ? " · try " + esc(sug) : ""}</span>
          ${r.pqs_score != null ? `<span class="pill" title="PQS composite (prompt-quality-scorer.md §9.5): clarity, specificity, completeness and re-ask risk, computed from the same answers">PQS score ${r.pqs_score}</span>` : ""}
          ${r.meta.low_confidence && r.meta.backend !== "heuristic" ? '<span class="pill warn" title="Average Jev confidence on the scales was below 50%">Low confidence</span>' : ""}
        </div>
      </div>
    </div>`;
  }

  function backendNotice(r) {
    if (r.meta.backend !== "heuristic") return "";
    const why = r.meta.degraded
      ? "Jev was unavailable, so this report comes from the rule-based fallback."
      : "You chose the rule-based heuristic backend.";
    return `<div class="rc-block rc-notice" role="note">${ICON.info}<span><b>Heuristic report.</b> ${why} It's a transparent baseline: expect rougher scores than Jev.</span></div>`;
  }

  function renderReport(el, r, opts = {}) {
    const dims = r.dimensions.map((d) => ({ id: d.id, label: d.label }));
    const values = Object.fromEntries(r.dimensions.map((d) => [d.id, d.value]));
    const radarSeries = opts.radarSeries || [{ name: "This prompt", color: "var(--accent-mid)", values }];
    const legend = radarSeries.length > 1
      ? `<div class="legend">${radarSeries.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : "";
    el.innerHTML = `<div class="rc">
      ${backendNotice(r)}
      ${summaryBlock(r, opts)}
      <div class="rc-two">${firstTryBlock(r.first_try_success)}${specificityBlock(r.specificity)}</div>
      <div class="rc-dims">
        <div class="rc-block"><p class="rc-label"><span>Dimensions</span></p>${radarSVG(dims, radarSeries)}${legend}</div>
        <div class="rc-block"><p class="rc-label"><span>Checklist</span><span>hover for probability</span></p>${checklist(r.checks)}</div>
      </div>
      ${opts.hideRouting ? "" : routingBlock(r)}
      ${opts.hideCosts ? "" : `<div class="rc-block"><p class="rc-label"><span>Tokens &amp; cost</span><span>${r.tokens.input_exact ? "" : "≈ approx."}</span></p>${costTable(r, opts.modelNames)}</div>`}
      <div class="rc-block"><p class="rc-label"><span>Fix-it tips</span>${r.tips.length ? "<span>+points it could recover</span>" : ""}</p>${tipsBlock(r.tips)}</div>
      <div class="rc-meta">
        <span>Judged by <code>${esc(r.meta.jev_model)}</code> in ${int(r.meta.latency_ms)} ms${r.meta.cached ? " (cached)" : ""}</span>
        <span>${r.meta.backend === "heuristic" ? "0 Jev calls" : "1 Jev call"} · 0 LLM calls</span>
        <span>Prompt hash <code>${esc(r.meta.prompt_hash)}</code> · not stored</span>
      </div>
    </div>`;
  }

  /* ------------------------------------------------------------ tooltips */
  let tipEl = null;
  function ensureTip() {
    if (tipEl) return tipEl;
    tipEl = document.createElement("div");
    tipEl.className = "pl-tip";
    tipEl.setAttribute("role", "tooltip");
    document.body.appendChild(tipEl);
    return tipEl;
  }
  function placeTip(target, x, y) {
    const t = ensureTip();
    t.innerHTML = target.getAttribute("data-tip");
    t.classList.add("show");
    const w = t.offsetWidth, h = t.offsetHeight;
    let left = x + 14, top = y - h - 10;
    if (left + w > window.innerWidth - 8) left = x - w - 14;
    if (top < 8) top = y + 16;
    t.style.left = Math.max(8, left) + "px";
    t.style.top = top + "px";
  }
  function hideTip() { if (tipEl) tipEl.classList.remove("show"); }
  document.addEventListener("pointermove", (e) => {
    const target = e.target.closest?.("[data-tip]");
    if (target) placeTip(target, e.clientX, e.clientY); else hideTip();
  });
  document.addEventListener("focusin", (e) => {
    const target = e.target.closest?.("[data-tip]");
    if (!target) return hideTip();
    const b = target.getBoundingClientRect();
    placeTip(target, b.left + 20, b.top);
  });
  document.addEventListener("focusout", hideTip);
  window.addEventListener("scroll", hideTip, { passive: true });

  window.PL = { renderReport, radarSVG, gaugeSVG, usd, usdRange, esc, ICON, VERDICT, TASK_LABEL };
})();
