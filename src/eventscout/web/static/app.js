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
      status.textContent = `Added as ${r.kind} — ${r.events_found} upcoming events found.`;
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
          if (!confirm("Remove this source?")) return;
          await post(`/api/sources/${id}`, null, "DELETE");
        } else {
          await post(`/api/sources/${id}/status`, { status: b.dataset.status });
        }
        location.reload();
      } catch (err) {
        alert(err.message);
      }
    });
  });
})();
