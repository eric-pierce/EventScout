(() => {
  const el = document.getElementById("map");
  if (!el || !window.L) return;
  const { places, home, tiles, min_score: minScore } = JSON.parse(document.getElementById("map-data").textContent);

  const map = L.map(el, { scrollWheelZoom: true });
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  if (tiles.style && L.maplibreGL) {
    // Vector basemap drawn by MapLibre; its credit line comes from the style.
    L.maplibreGL({ style: dark ? tiles.dark_style : tiles.style }).addTo(map);
    if (tiles.attribution) map.attributionControl.addAttribution(tiles.attribution);
  } else if (tiles.url) {
    // Raster tiles. Dark mode: the provider's dark tiles if set, otherwise the light ones dimmed with a CSS filter.
    if (dark && !tiles.dark_url) el.classList.add("dim-tiles");
    L.tileLayer(dark && tiles.dark_url ? tiles.dark_url : tiles.url,
      { maxZoom: 19, subdomains: tiles.subdomains || "abc", attribution: tiles.attribution }).addTo(map);
  }

  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  const tier = (score) => (score >= 80 ? "hi" : score >= minScore ? "mid" : "lo");
  const fill = { hi: color("--accent"), mid: color("--star"), lo: color("--muted") };

  // Home and the search radius.
  const homeLatLng = [home.lat, home.lon];
  L.circle(homeLatLng, {
    radius: home.radius_miles * 1609.34, color: color("--accent"), weight: 1.5, dashArray: "6 6",
    fill: false, interactive: false,
  }).addTo(map);
  // Home: an accent ring with a white centre, so it can't be mistaken for an event pin.
  L.circleMarker(homeLatLng, { radius: 8, color: color("--accent"), weight: 4, fillColor: "#fff", fillOpacity: 1 })
    .bindTooltip(`Home · ${home.label}`, { permanent: false }).addTo(map);

  // Text from event sources is untrusted: popups are built from DOM nodes with textContent, never innerHTML.
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const safeHref = (url) => (/^https?:\/\//i.test(url || "") ? url : null);

  const popup = (place) => {
    const box = node("div", "map-pop");
    const head = node("div", "map-pop-head");
    head.append(node("strong", null, place.name));
    if (place.miles != null) head.append(node("span", "muted", ` · ${place.miles.toFixed(1)} mi`));
    box.append(head);
    const list = node("ul");
    place.events.forEach((e) => {
      const li = node("li");
      if (e.image && safeHref(e.image)) {
        const img = node("img");
        img.src = e.image; img.alt = ""; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
        img.onerror = () => img.remove();
        li.append(img);
      }
      const body = node("div");
      const href = safeHref(e.url);
      const title = href ? node("a", null, e.title) : node("span", null, e.title);
      if (href) { title.href = href; title.target = "_blank"; title.rel = "noopener"; }
      const line = node("div", "map-pop-title");
      if (e.score != null) line.append(node("span", `score ${tier(e.score)}`, String(e.score)), " ");
      if (e.starred) line.append(node("span", "star-on", "★ "));
      line.append(title);
      body.append(line, node("div", "muted", `${e.when} · ${e.cost}`));
      li.append(body);
      list.append(li);
    });
    box.append(list);
    return box;
  };

  const bounds = [homeLatLng];
  places
    .sort((a, b) => (a.best ?? -1) - (b.best ?? -1)) // draw the best last, so they sit on top
    .forEach((p) => {
      const t = p.best == null ? "lo" : tier(p.best);
      L.circleMarker([p.lat, p.lon], {
        radius: Math.min(7 + 3 * Math.sqrt(p.events.length - 1), 18),
        color: "#fff", weight: 1.5, fillColor: fill[t], fillOpacity: 0.9,
      })
        .bindTooltip(`${p.name} · ${p.events.length} event${p.events.length === 1 ? "" : "s"}`)
        .bindPopup(() => popup(p), { maxWidth: 340, maxHeight: 320 })
        .addTo(map);
      bounds.push([p.lat, p.lon]);
    });

  if (bounds.length > 1) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 14 });
  else map.setView(homeLatLng, 11);
})();
