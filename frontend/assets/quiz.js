/* Prompt quiz: five rewrites, graded on the server, with a leaderboard. */
(function () {
  "use strict";
  document.documentElement.classList.remove("no-js");
  const $ = (id) => document.getElementById(id);
  const { esc, VERDICT } = window.PL;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const nav = $("nav");
  const onScroll = () => nav.classList.toggle("stuck", window.scrollY > 4);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  let quiz = null, i = 0, answers = {}, period = "all", me = null;
  $("nickname").value = store.get("pl_quiz_nick") || "";

  function show(id) { ["start", "play", "grading", "results"].forEach((x) => { $(x).hidden = x !== id; }); }

  /* ---------------------------------------------------------- leaderboard */
  async function loadBoard() {
    const ol = $("leaderboard");
    try {
      const r = await fetch(`api/quiz/leaderboard?period=${period}`);
      const d = await r.json();
      ol.innerHTML = d.entries.length
        ? d.entries.map((e) => `<li class="${me && e.nickname.toLowerCase() === me.toLowerCase() ? "me" : ""}">
            <span class="r">${e.rank}</span><span class="nm" title="${esc(e.nickname)}">${esc(e.nickname)}</span>
            <span class="g">${esc(e.grade)}</span><span class="s">${e.score}</span></li>`).join("")
        : `<li class="empty-lb">No ranked scores ${period === "week" ? "this week" : "yet"}. Be the first.</li>`;
    } catch {
      ol.innerHTML = `<li class="empty-lb">Couldn't load the leaderboard.</li>`;
    }
  }
  document.querySelectorAll(".lb-tabs button").forEach((b) => b.onclick = () => {
    period = b.dataset.period;
    document.querySelectorAll(".lb-tabs button").forEach((x) => x.setAttribute("aria-selected", x === b));
    loadBoard();
  });

  /* --------------------------------------------------------------- rounds */
  function renderRound() {
    const r = quiz.rounds[i];
    $("progress").innerHTML = quiz.rounds.map((_, k) => `<i class="${k < i ? "done" : k === i ? "now" : ""}"></i>`).join("");
    $("round-k").textContent = `Round ${i + 1} of ${quiz.rounds.length}`;
    $("round-title").textContent = r.title;
    $("round-scenario").textContent = r.scenario;
    $("round-weak").textContent = r.weak;
    $("rewrite").value = answers[r.id] || "";
    $("back").hidden = i === 0;
    $("next").innerHTML = i === quiz.rounds.length - 1 ? "Submit for grading" : 'Next round <span class="arrow">→</span>';
    $("play-msg").textContent = "";
    count();
    $("rewrite").focus();
  }
  function count() { $("rewrite-count").textContent = `${$("rewrite").value.length.toLocaleString()} / 4,000`; }
  $("rewrite").addEventListener("input", () => { answers[quiz.rounds[i].id] = $("rewrite").value; count(); });

  $("begin").onclick = async () => {
    const nick = $("nickname").value.trim().replace(/\s+/g, " ");
    if (!/^[A-Za-z0-9 _.\-]{2,24}$/.test(nick)) {
      $("start-msg").textContent = "Pick a nickname of 2–24 letters, numbers, spaces, dots, dashes or underscores.";
      return;
    }
    store.set("pl_quiz_nick", nick);
    me = nick;
    try {
      quiz = quiz || (await (await fetch("api/quiz")).json());
    } catch {
      $("start-msg").textContent = "Couldn't load the quiz. Please try again in a moment.";
      return;
    }
    i = 0; answers = {};
    show("play"); renderRound();
  };
  $("back").onclick = () => { if (i > 0) { i -= 1; renderRound(); } };
  $("next").onclick = () => {
    const text = $("rewrite").value.trim();
    if (text.length < 10) { $("play-msg").textContent = "Write a real rewrite first (at least a sentence)."; return; }
    answers[quiz.rounds[i].id] = text;
    if (i < quiz.rounds.length - 1) { i += 1; renderRound(); return; }
    submit();
  };

  /* --------------------------------------------------------------- submit */
  async function submit() {
    show("grading");
    window.scrollTo({ top: 0, behavior: "smooth" });
    let res, data = null;
    try {
      res = await fetch("api/quiz/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nickname: me, answers: quiz.rounds.map((r) => ({ id: r.id, prompt: answers[r.id] })) }),
      });
      data = await res.json();
    } catch { /* handled below */ }
    if (!res || !res.ok) {
      show("play");
      $("play-msg").textContent = (data && (data.error?.message || data.error || data.detail)) ||
        "Grading failed. Please try again in a moment.";
      return;
    }
    renderResults(data);
  }

  function renderResults(d) {
    show("results");
    const g = $("grade");
    g.textContent = d.grade;
    g.className = "grade" + (d.score >= 70 ? "" : d.score >= 50 ? " mid" : " low");
    $("grade-title").textContent = d.title;
    $("grade-sub").textContent = `You scored ${d.score} out of 100 across ${d.rounds.length} rewrites.`;
    const pills = [];
    if (d.ranked && d.rank) pills.push(`<span class="pill accent">#${d.rank} on the leaderboard</span>`);
    else if (d.ranked) pills.push(`<span class="pill">Ranked. Keep going to reach the top 10</span>`);
    else pills.push(`<span class="pill warn" title="The AI judge was unavailable, so rules graded this run">Unranked: graded by simple rules</span>`);
    if (d.top_score && d.ranked) pills.push(`<span class="pill accent">New top score!</span>`);
    $("grade-pills").innerHTML = pills.join("");
    $("round-results").innerHTML = d.rounds.map((r) => {
      const v = VERDICT[r.verdict] || VERDICT.needs_work;
      return `<div class="rres"><div class="sc">${r.score}<small>/100</small></div>
        <div><b>${esc(r.title)}</b> <span class="verdict ${r.verdict}" style="font-size:.72rem;padding:2px 9px 2px 7px;margin-left:6px">${v.icon}${v.label}</span>
        <p>${esc(r.feedback)}</p></div></div>`;
    }).join("");
    loadBoard();
  }
  $("again").onclick = () => { show("start"); $("nickname").focus(); };

  loadBoard();
})();
