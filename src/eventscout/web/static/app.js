(() => {
  const post = async (url, body, method = "POST") => {
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  };

  // Phones: the filter form folds away behind a Filters button.
  const side = document.getElementById("side");
  side?.querySelector(".filters-toggle")?.addEventListener("click", (e) => {
    const open = side.classList.toggle("open");
    e.currentTarget.setAttribute("aria-expanded", String(open));
  });

  // Event feedback / starring
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".act[data-action]");
    if (!btn) return;
    const card = btn.closest(".event");
    const id = card.dataset.id;
    const action = btn.dataset.action;
    try {
      if (action === "star") {
        const on = !btn.classList.contains("on");
        await post(`/api/events/${id}/star`, { starred: on });
        btn.classList.toggle("on", on);
        card.classList.toggle("is-starred", on);
        if (on) card.querySelector(".act.up")?.classList.add("on");
      } else {
        const want = action === "up" ? 1 : -1;
        const value = btn.classList.contains("on") ? 0 : want;
        await post(`/api/events/${id}/feedback`, { value });
        card.querySelectorAll(".act.up, .act.down").forEach((b) => b.classList.remove("on"));
        if (value !== 0) btn.classList.add("on");
        card.classList.toggle("is-hidden", value === -1);
      }
    } catch (err) {
      alert(`Couldn't save: ${err.message}`);
    }
  });

  // Run now / discover
  const runState = document.getElementById("runstate");
  const runBtn = document.getElementById("run-now");
  const discoverBtn = document.getElementById("discover-now");
  const watch = () => {
    const timer = setInterval(async () => {
      const st = await fetch("/api/status").then((r) => r.json()).catch(() => null);
      if (st && !st.running) {
        clearInterval(timer);
        location.reload();
      }
    }, 4000);
  };
  const start = async (discover) => {
    try {
      await post(`/api/run?discover=${discover}`);
      if (runBtn) runBtn.disabled = true;
      if (discoverBtn) discoverBtn.disabled = true;
      if (runState) runState.textContent = discover ? "Discovering sources and running…" : "Running…";
      watch();
    } catch (err) {
      alert(err.message);
    }
  };
  runBtn?.addEventListener("click", () => start(false));
  discoverBtn?.addEventListener("click", () => start(true));
  if (runBtn?.disabled) watch();

  // Sources page
  const addForm = document.getElementById("add-source");
  addForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = document.getElementById("add-status");
    const data = Object.fromEntries(new FormData(addForm));
    status.textContent = "Checking source… (may take a minute if the page needs the LLM)";
    try {
      const r = await post("/api/sources", { url: data.url, name: data.name || null });
      status.textContent = `Added as ${r.kind} — ${r.events_found} upcoming events found.`
        + (r.saved_to_config ? " Saved to config.yaml." : ` ${r.warning}`);
      setTimeout(() => location.reload(), 1200);
    } catch (err) {
      status.textContent = `Not added: ${err.message}`;
    }
  });
  document.querySelectorAll("table.sources button").forEach((b) => {
    b.addEventListener("click", async () => {
      const id = b.closest("tr").dataset.id;
      try {
        if (b.hasAttribute("data-delete")) {
          if (!confirm("Remove this source? Sources from config.yaml are removed from the file too.")) return;
          await post(`/api/sources/${id}`, null, "DELETE");
        } else {
          const r = await post(`/api/sources/${id}/status`, { status: b.dataset.status });
          if (r.warning) alert(r.warning);
        }
        location.reload();
      } catch (err) {
        alert(err.message);
      }
    });
  });

  // Settings: tabs
  const tabs = document.querySelectorAll(".tabs .tab");
  if (tabs.length) {
    const show = () => {
      const name = (location.hash || "#profile").slice(1);
      tabs.forEach((t) => t.classList.toggle("on", t.dataset.tab === name));
      document.querySelectorAll(".tabpanel").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
    };
    window.addEventListener("hashchange", show);
    show();
  }

  const setStatus = (el, msg, kind) => { el.textContent = msg; el.className = `status ${kind || ""}`; };
  const lines = (v) => v.split("\n").map((s) => s.trim()).filter(Boolean);

  // Settings: profile form
  const profileForm = document.getElementById("profile-form");
  if (profileForm) {
    const list = document.getElementById("interests");
    document.getElementById("add-interest").addEventListener("click", () => {
      list.appendChild(document.getElementById("interest-template").content.cloneNode(true));
      list.lastElementChild.querySelector("input").focus();
    });
    list.addEventListener("click", (e) => { if (e.target.closest(".remove")) e.target.closest(".interest-row").remove(); });
    profileForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = document.getElementById("profile-status");
      const f = profileForm.elements;
      const body = {
        interests: [...list.querySelectorAll(".interest-row")]
          .map((r) => ({ topic: r.querySelector("[name=topic]").value.trim(), weight: Number(r.querySelector("[name=weight]").value) }))
          .filter((i) => i.topic),
        dislikes: lines(f.dislikes.value),
        exclude_keywords: lines(f.exclude_keywords.value),
        notes: f.notes.value,
        min_score: Number(f.min_score.value || 0),
      };
      setStatus(status, "Saving…");
      try {
        const r = await post("/api/profile", body);
        setStatus(status, r.rescore ? "Saved. Upcoming events will be re-scored on the next run." : "Saved.", "ok");
      } catch (err) {
        setStatus(status, `Not saved: ${err.message}`, "err");
      }
    });
  }

  // Settings: raw config.yaml
  const configForm = document.getElementById("config-form");
  configForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = document.getElementById("config-status");
    setStatus(status, "Validating and saving…");
    try {
      const r = await post("/api/config", { text: configForm.elements.text.value, base_hash: configForm.elements.base_hash.value });
      configForm.elements.base_hash.value = r.hash;
      setStatus(status, r.restart_needed.length
        ? `Saved. Restart the container to apply: ${r.restart_needed.join(", ")}.` : "Saved and applied.", "ok");
    } catch (err) {
      setStatus(status, `Not saved: ${err.message}`, "err");
    }
  });

  // Trips: plan a trip
  const tripForm = document.getElementById("trip-form");
  tripForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = document.getElementById("trip-status");
    const f = tripForm.elements;
    setStatus(status, "Looking up the destination…");
    try {
      const r = await post("/api/trips", {
        destination: f.destination.value, start_date: f.start_date.value, end_date: f.end_date.value,
        radius_miles: Number(f.radius_miles.value || 10), near: f.near.value, notes: f.notes.value,
        discover: f.discover ? f.discover.checked : false,
      });
      location.href = `/trips/${r.id}`;
    } catch (err) {
      setStatus(status, err.message, "err");
    }
  });

  // Trips: open a city's sources page (before or without any trip there)
  const cityForm = document.getElementById("city-form");
  cityForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = document.getElementById("city-status");
    setStatus(status, "Looking up the city…");
    try {
      location.href = (await post("/api/cities", { destination: cityForm.elements.destination.value })).url;
    } catch (err) { setStatus(status, err.message, "err"); }
  });

  // A city's trip sources (on a trip's page or the city's own): add, save what the web search found,
  // switch on/off, remove.
  const cityPanel = document.getElementById("city-sources");
  if (cityPanel) {
    const api = cityPanel.dataset.api;
    const act = (sel, fn) => cityPanel.querySelectorAll(sel).forEach((b) => b.addEventListener("click", async () => {
      try { if ((await fn(b)) !== false) location.reload(); } catch (err) { alert(err.message); }
    }));
    const srcForm = document.getElementById("city-source-form");
    srcForm?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = document.getElementById("city-source-status");
      setStatus(status, `Checking the source for events ${cityPanel.dataset.window}…`);
      try {
        const r = await post(`${api}/sources`, { url: srcForm.elements.url.value, name: srcForm.elements.name.value });
        setStatus(status, `Saved as ${r.kind}: ${r.events_found} events ${cityPanel.dataset.window}. ${cityPanel.dataset.after}`, "ok");
        setTimeout(() => location.reload(), 1500);
      } catch (err) { setStatus(status, `Not added: ${err.message}`, "err"); }
    });
    act("[data-save-found]", (b) => post(`${api}/sources`, JSON.parse(b.dataset.saveFound)));
    act("[data-builtin]", (b) => post(`${api}/builtins/${b.dataset.builtin}`, { enabled: b.dataset.enabled === "true" }));
    act("[data-saved]", (b) => post(`${api}/sources/${b.dataset.saved}`, { enabled: b.dataset.enabled === "true" }));
    act("[data-saved-delete]", (b) => confirm("Remove this source from every trip to this city?")
      && post(`${api}/sources/${b.dataset.savedDelete}`, null, "DELETE"));
  }

  // Trips: one report (live progress while building; rebuild / delete)
  const trip = document.getElementById("trip");
  if (trip) {
    const id = trip.dataset.id;
    if (["queued", "running"].includes(trip.dataset.status)) {
      const logEl = document.getElementById("trip-log");
      const timer = setInterval(async () => {
        const st = await fetch(`/api/trips/${id}`).then((r) => r.json()).catch(() => null);
        if (!st) return;
        if (logEl) logEl.textContent = st.log.join("\n");
        if (!["queued", "running"].includes(st.status)) { clearInterval(timer); location.reload(); }
      }, 3000);
    }
    trip.querySelectorAll("[data-trip-action]").forEach((b) => b.addEventListener("click", async () => {
      try {
        if (b.dataset.tripAction === "delete") {
          if (!confirm("Delete this trip and its report?")) return;
          await post(`/api/trips/${id}`, null, "DELETE");
          location.href = "/trips";
        } else {
          await post(`/api/trips/${id}/rebuild`);
          location.reload();
        }
      } catch (err) { alert(err.message); }
    }));
  }
})();
