/*
 * Display three saved date-fidelity cases and their deterministic verdicts.
 * The exporter calls fixtures.checks.py and pins the fixture/source hashes.
 * The interactive date check calls a bounded endpoint that runs the source guard.
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

  const guardForm = $("guard-form");
  const guardCase = $("guard-case");
  const requestDate = $("guard-request-date");
  const responseDate = $("guard-response-date");
  const guardStatus = $("guard-status");
  const guardResult = $("guard-result");
  data.cases.forEach((item, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = item.id.replaceAll("-", " ");
    guardCase.append(option);
  });

  let guardRevision = 0;
  let guardPending;
  function clearGuard() {
    guardRevision++;
    guardPending?.abort();
    guardResult.textContent = "Inputs changed. Run the guard again.";
    guardResult.className = "guard-result";
  }
  guardForm.addEventListener("input", clearGuard);
  function loadGuardCase() {
    clearGuard();
    const item = data.cases[Number(guardCase.value)];
    requestDate.value = item.requestDate;
    responseDate.value = item.responseDate;
    guardStatus.value = String(item.status);
    guardResult.textContent = "Values loaded. Run the date check to evaluate them.";
    guardResult.className = "guard-result";
  }
  guardCase.addEventListener("change", loadGuardCase);
  guardForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const current = ++guardRevision;
    guardPending?.abort();
    const controller = new AbortController();
    guardPending = controller;
    const timer = setTimeout(() => controller.abort(), 12000);
    const status = Number(guardStatus.value);
    if (!Number.isInteger(status) || status < 100 || status > 599) {
      guardResult.textContent = "Enter an HTTP status from 100 to 599.";
      guardResult.className = "guard-result is-error";
      clearTimeout(timer);
      return;
    }
    guardResult.textContent = "Checking the source guard...";
    guardResult.className = "guard-result is-neutral";
    try {
      const response = await fetch("/api/date_guard", {
        method: "POST",
        signal: controller.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          requestDate: requestDate.value,
          responseDate: responseDate.value,
          status,
        }),
      });
      const result = await response.json();
      if (current !== guardRevision) return;
      if (!response.ok) throw new Error(result.error || "The date check could not evaluate this input.");
      if (result.finding) {
        guardResult.textContent = `Guard finding: ${result.finding}`;
        guardResult.className = "guard-result is-finding";
      } else if (!result.checked) {
        guardResult.textContent = "Not checked: this rule only reports a date mismatch on HTTP 200.";
        guardResult.className = "guard-result is-neutral";
      } else {
        guardResult.textContent = "No mismatch found for this input. One matching response does not establish a general guarantee.";
        guardResult.className = "guard-result is-match";
      }
    } catch (error) {
      if (current !== guardRevision) return;
      guardResult.textContent = error.message;
      guardResult.className = "guard-result is-error";
    } finally {
      clearTimeout(timer);
    }
  });
  guardCase.value = "0";
  loadGuardCase();

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
      ? `All ${results.length} fixture, rule, guard, and explanation files match the pinned SHA256 values.`
      : "Some source files are unavailable or do not match their pinned SHA256 values.";
  }
  await verifySources();
}());
