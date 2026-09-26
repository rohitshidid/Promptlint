/* Shared on every page: the phone menu, and tables that turn into cards on small screens. */
(function () {
  "use strict";

  /* ------------------------------------------------------------ phone menu */
  // Below 860px the page's own nav links hide; this button opens the whole site menu instead.
  const LINKS = [
    ["./", "Home"],
    ["app.html", "Try the router"],
    ["tester.html", "Tester"],
    ["docs.html", "API docs"],
    ["quiz.html", "Prompt quiz"],
    ["./#free", "Pricing"],
    ["account.html", "Account"],
  ];
  const links = document.querySelector(".nav-links");
  if (links) {
    const here = location.pathname.split("/").pop() || "index.html";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "menu-btn";
    btn.setAttribute("aria-label", "Menu");
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-controls", "site-menu");
    btn.innerHTML = '<span></span><span></span><span></span>';
    const panel = document.createElement("div");
    panel.className = "site-menu";
    panel.id = "site-menu";
    panel.hidden = true;
    panel.innerHTML = LINKS.map(([href, label]) => {
      const page = href.replace(/^\.\//, "").split("#")[0] || "index.html";
      const current = page === here && !href.includes("#");
      return `<a href="${href}"${current ? ' aria-current="page"' : ""}>${label}</a>`;
    }).join("");
    links.appendChild(btn);
    document.querySelector(".nav").appendChild(panel);
    const close = () => { panel.hidden = true; btn.setAttribute("aria-expanded", "false"); btn.classList.remove("open"); };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = panel.hidden;
      panel.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      btn.classList.toggle("open", open);
    });
    panel.addEventListener("click", (e) => { if (e.target.closest("a")) close(); });
    document.addEventListener("click", (e) => { if (!panel.hidden && !panel.contains(e.target)) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    window.addEventListener("resize", () => { if (window.innerWidth > 860) close(); });
  }

  /* --------------------------------------------------- tables → cards */
  // Copy each column's header onto its cells (data-label), so small screens can show them as cards.
  const SELECTOR = "table.ref, table.keys, table.cost, table.costs";
  function label(table) {
    const heads = [...table.querySelectorAll("thead th")].map((th) => th.textContent.trim());
    if (!heads.length) return;
    table.classList.add("stack");
    for (const row of table.querySelectorAll("tbody tr")) {
      [...row.children].forEach((td, i) => {
        if (heads[i] && !td.hasAttribute("data-label") && td.colSpan === 1) td.setAttribute("data-label", heads[i]);
      });
    }
  }
  const scan = () => document.querySelectorAll(SELECTOR).forEach(label);
  scan();
  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
})();
