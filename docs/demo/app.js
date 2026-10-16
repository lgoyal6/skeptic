/*
 * Display three saved date-fidelity cases and their deterministic verdicts.
 * The exporter calls fixtures.checks.py and pins the fixture/source hashes.
 * This page only presents those results; it makes no API or model calls.
 */
(async function () {
  "use strict";

  const data = await fetch("evidence.json").then((response) => {
    if (!response.ok) throw new Error("The saved evidence could not be loaded.");
    return response.json();
  }).catch((error) => {
    document.querySelector("main").textContent = error.message;
    return null;
  });
  if (!data) return;

  const $ = (id) => document.getElementById(id);
  const labels = {
    contradicts_doc: "Mismatch detected",
    unobservable: "Cannot tell from this trace",
    supports_doc: "Supports the claim in this case",
  };
  let selected = 0;
  $("case-count").textContent = data.cases.length;
  $("outcome-count").textContent = new Set(data.cases.map((item) => item.outcome)).size;
  $("source-commit").textContent = `Source ${data.sourceCommit.slice(0, 12)}`;
  $("source-link").href = `https://github.com/lgoyal6/skeptic/tree/${encodeURIComponent(data.sourceCommit)}/docs/ONE_MISMATCH.md`;

  const tabs = $("case-tabs");
  const detail = $("case-detail");
  function renderCase(index) {
    selected = index;
    tabs.querySelectorAll("button").forEach((button, buttonIndex) => {
      button.setAttribute("aria-selected", String(buttonIndex === selected));
      button.tabIndex = buttonIndex === selected ? 0 : -1;
    });
    const item = data.cases[selected];
    detail.setAttribute("aria-labelledby", `case-tab-${selected}`);
    detail.replaceChildren();
    const top = document.createElement("div");
    top.className = "case-topline";
    const heading = document.createElement("h3");
    heading.textContent = item.id.replaceAll("-", " ");
    const outcome = document.createElement("span");
    outcome.className = `outcome ${item.outcome}`;
    outcome.textContent = labels[item.outcome] ?? item.outcome;
    top.append(heading, outcome);
    const grid = document.createElement("div");
    grid.className = "trace-grid";
    const cells = [
      ["REQUESTED DATE", item.requestDate, ""],
      ["RESPONSE DATE", item.responseDate, item.requestDate === item.responseDate ? "good-date" : "bad-date"],
      ["HTTP STATUS", String(item.status), ""],
    ];
    for (const [label, value, valueClass] of cells) {
      const cell = document.createElement("div");
      cell.className = "trace-cell";
      const caption = document.createElement("span");
      caption.textContent = label;
      const text = document.createElement("strong");
      text.className = valueClass;
      text.textContent = value;
      cell.append(caption, text);
      grid.append(cell);
    }
    const path = document.createElement("p");
    path.className = "path-line";
    path.textContent = `GET ${item.path}`;
    const verdict = document.createElement("p");
    verdict.className = "verdict-detail";
    verdict.textContent = item.detail;
    const key = document.createElement("p");
    key.className = "key-line";
    key.textContent = `Fixture exchange ${item.evidenceKey} · ${item.version}`;
    detail.append(top, grid, path, verdict, key);
  }

  for (const [index, item] of data.cases.entries()) {
    const button = document.createElement("button");
    button.className = "case-tab";
    button.type = "button";
    button.id = `case-tab-${index}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", "case-detail");
    button.setAttribute("aria-selected", String(index === selected));
    button.textContent = item.id.replaceAll("-", " ");
    button.tabIndex = index === selected ? 0 : -1;
    button.addEventListener("click", () => renderCase(index));
    button.addEventListener("keydown", (event) => {
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        const step = event.key === "ArrowRight" ? 1 : -1;
        const next = (index + step + data.cases.length) % data.cases.length;
        renderCase(next);
        tabs.querySelectorAll("button")[next].focus();
      }
    });
    tabs.append(button);
  }
  renderCase(0);

  const limits = $("limits-list");
  data.limits.forEach((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    limits.append(item);
  });

  async function verifySources() {
    const results = await Promise.all(Object.entries(data.sourceHashes).map(async ([path, expected]) => {
      try {
        const response = await fetch(`../../${path}`);
        if (!response.ok) return false;
        const digest = await crypto.subtle.digest("SHA-256", await response.arrayBuffer());
        const actual = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
        return actual === expected;
      } catch {
        return false;
      }
    }));
    const matches = results.length > 0 && results.every(Boolean);
    $("integrity-status").textContent = matches
      ? "All seven fixture, rule, and explanation files match the pinned SHA256 values."
      : "Some source files are unavailable or do not match their pinned SHA256 values.";
  }
  await verifySources();
}());
