/* VarMap front-end.  Plain JS + Leaflet; no build step. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const S = {
    stations: new Map(), markers: new Map(), since: null, own: null, health: null, config: null,
    selected: null, hover: null, track: null, gridRect: null, ownMarker: null, offsetMs: 0, units: "MI",
    pollMs: 10000, listDirty: true, bands: new Set(), tab: "stations",
  };

  // ---------------------------------------------------------------- utils
  async function api(path, body, method) {
    const opts = { method: method || (body ? "POST" : "GET"), headers: { "X-Requested-With": "VarMap" } };
    if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }
  const nowServer = () => Date.now() + S.offsetMs;
  function parseT(s) { return s ? Date.parse(s) : NaN; }
  function ageStr(iso) {
    const t = parseT(iso); if (isNaN(t)) return "never";
    let s = Math.max(0, Math.round((nowServer() - t) / 1000));
    if (s < 60) return s + " s"; if (s < 3600) return Math.floor(s / 60) + " min";
    if (s < 86400) return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " m";
    return Math.floor(s / 86400) + " d " + Math.floor((s % 86400) / 3600) + " h";
  }
  function ageSec(iso) { const t = parseT(iso); return isNaN(t) ? null : (nowServer() - t) / 1000; }
  function stateOf(iso) {
    const a = ageSec(iso); if (a === null) return "none";
    const c = (S.config && S.config.staleness) || { fresh_minutes: 30, recent_hours: 2, stale_hours: 24, hide_after_days: 30 };
    if (a < c.fresh_minutes * 60) return "fresh"; if (a < c.recent_hours * 3600) return "recent";
    if (a < c.stale_hours * 3600) return "stale"; if (a < c.hide_after_days * 86400) return "old"; return "historic";
  }
  const RANK = { fresh: 4, recent: 3, stale: 2, old: 1, historic: 0, none: -1 };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function fmtFreq(hz) { if (!hz) return ""; const s = String(hz); return s.replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
  function dist(st) { return st.distance_display || ""; }
  function isAprs(st) { return st.last_frame_kind === "aprs" || st.position_source === "aprs"; }
  // One symbol vocabulary for the map marker, the list and the station panel.
  function dotClasses(st) {
    const nopos = st.lat == null;
    return ["dot", nopos ? "none" : st.state, st.is_own ? "own" : "", isAprs(st) ? "aprs" : "", st.is_object ? "object" : "",
      st.position_suspect ? "suspect" : "", st.is_emcomm ? "emcomm" : ""].filter(Boolean).join(" ");
  }
  function dotHtml(st) { return `<i class="${dotClasses(st)}"></i>`; }
  function bandsStr(st) {
    const b = st.bands_recent || [];
    if (b.length > 1) return b.join("·");
    return st.last_band || "";
  }
  function lsGet(k, d) { try { const v = localStorage.getItem("varmap." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } }
  function lsSet(k, v) { try { localStorage.setItem("varmap." + k, JSON.stringify(v)); } catch (e) { /* ignore */ } }

  // ---------------------------------------------------------------- map
  const map = L.map("map", { zoomControl: false, preferCanvas: false });
  L.control.zoom({ position: "bottomleft" }).addTo(map);
  L.control.scale({ position: "bottomleft", imperial: true }).addTo(map);
  const NOTILE = "data:image/svg+xml;utf8," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#e3e8ee"/><path d="M0 0L256 256M256 0L0 256" stroke="#cfd6de" stroke-width="1"/></svg>');
  const tiles = L.tileLayer("/tiles/{z}/{x}/{y}.png", { maxZoom: 17, errorTileUrl: NOTILE, attribution: "&copy; OpenStreetMap contributors" }).addTo(map);
  // Clustering presets; the group is rebuilt when the option changes (markercluster cannot change options live).
  const CLUSTER_PRESETS = {
    off: null,
    overlap: { disableClusteringAtZoom: 7, maxClusterRadius: 26 },
    normal: { disableClusteringAtZoom: 8, maxClusterRadius: 45 },
    aggressive: { disableClusteringAtZoom: 10, maxClusterRadius: 80 },
  };
  function clusterIcon(c) {
    let best = -1; c.getAllChildMarkers().forEach((m) => { best = Math.max(best, RANK[m.options.stateName] ?? -1); });
    const cls = best >= 4 ? "c-fresh" : best >= 3 ? "c-recent" : best >= 2 ? "c-stale" : "c-old";
    return L.divIcon({ html: `<div><span>${c.getChildCount()}</span></div>`, className: "marker-cluster " + cls, iconSize: L.point(36, 36) });
  }
  function makeCluster(mode) {
    const p = CLUSTER_PRESETS[mode] || CLUSTER_PRESETS.overlap;
    if (!p) return L.featureGroup();   // plain layer group: no clustering at all
    return L.markerClusterGroup({ ...p, spiderfyOnMaxZoom: true, showCoverageOnHover: false, iconCreateFunction: clusterIcon });
  }
  let cluster = makeCluster(lsGet("map.cluster", "overlap")).addTo(map);
  function rebuildCluster(mode) {
    const markers = [...S.markers.values()];
    map.removeLayer(cluster);
    cluster = makeCluster(mode).addTo(map);
    markers.forEach((m) => cluster.addLayer(m));
  }
  const trackLayer = L.layerGroup().addTo(map);
  const ownTrackLayer = L.layerGroup().addTo(map);

  // Legend (bottom-right, collapsible, remembers its state)
  const Legend = L.Control.extend({
    options: { position: "topright" },
    onAdd() {
      const div = L.DomUtil.create("div", "legend-ctl");
      L.DomEvent.disableClickPropagation(div); L.DomEvent.disableScrollPropagation(div);
      const thr = () => (S.config && S.config.staleness) || { fresh_minutes: 30, recent_hours: 2, stale_hours: 24, hide_after_days: 30 };
      const render = () => {
        const c = thr();
        const open = div.classList.contains("open");
        div.innerHTML = `<button class="legend-toggle" title="${open ? "Hide the legend" : "Show what the map symbols and tags mean"}">Legend <span class="caret"></span></button>
        <div class="legend-body">
          <h5>Position age</h5>
          <div class="lg"><span class="stn fresh sym"><span class="dot"></span></span> fresh, &lt; ${c.fresh_minutes} min</div>
          <div class="lg"><span class="stn recent sym"><span class="dot"></span></span> recent, &lt; ${c.recent_hours} h</div>
          <div class="lg"><span class="stn stale sym"><span class="dot"></span></span> stale, &lt; ${c.stale_hours} h (half-filled)</div>
          <div class="lg"><span class="stn old sym"><span class="dot"></span></span> old, &lt; ${c.hide_after_days} d (hollow)</div>
          <div class="lg"><span class="stn historic sym"><span class="dot"></span></span> historic, older</div>
          <h5>Variants</h5>
          <div class="lg"><span class="own-marker sym"><span class="ring"></span></span> my station</div>
          <div class="lg"><span class="stn fresh suspect sym"><span class="dot"></span></span> dashed: implausible jump from last position</div>
          <div class="lg"><span class="stn fresh emcomm sym"><span class="dot"></span></span> red halo: EmComm beacon</div>
          <div class="lg"><span class="stn fresh aprs sym"><span class="dot"></span></span> diamond: APRS station via Graywolf (square: APRS object)</div>
          <div class="lg"><span class="sym"><i class="dot none"></i></span> heard, no position (list only)</div>
          <div class="lg"><span class="sym"><span class="lg-cluster">12</span></span> cluster; colour = freshest member</div>
          <div class="lg"><span class="sym"><span class="lg-rect"></span></span> grid square of a grid-derived position (±4 km)</div>
          <div class="lg"><span class="sym"><span class="lg-track"></span></span> 7-day track</div>
          <h5>Station tags</h5>
          <div class="lg chips"><span class="chip em">EMCOMM</span> EmComm <span class="chip">BBS</span> public BBS <span class="chip">EMAIL</span> email gateway <span class="chip">AI</span> AI gateway <span class="chip">DIPL</span> diploma programme</div>
          <div class="lg chips"><span class="chip pota">POTA</span> CQ tag (POTA, DX, FD…) <span class="chip away">⌛ AWAY</span> operator away <span class="chip fav">★</span> favourite <span class="chip">?</span> suspect position</div>
          <div class="lg chips"><span class="chip aprs">APRS</span> heard on APRS via Graywolf <span class="chip consent-y">APRS:Y</span> station allows relay to APRS <span class="chip consent-n">APRS:N</span> relay refused</div>
          <h5>Station list line</h5>
          <div class="lg">last heard · distance bearing · band · SNR</div>
        </div>`;
        div.querySelector(".legend-toggle").onclick = () => { div.classList.toggle("open"); lsSet("legend", div.classList.contains("open")); render(); };
      };
      if (lsGet("legend", false)) div.classList.add("open");
      render();
      this._render = render;
      return div;
    },
  });
  const legend = new Legend().addTo(map);
  const view = lsGet("view", null);
  if (view) map.setView([view.lat, view.lon], view.z); else map.setView([38, -95], 4);
  map.on("moveend", () => { const c = map.getCenter(); lsSet("view", { lat: c.lat, lon: c.lng, z: map.getZoom() }); });
  function updateZoomClass() { map.getContainer().classList.toggle("z-low", map.getZoom() < 7); }
  map.on("zoomend", updateZoomClass); updateZoomClass();

  // ---- Map display options (persisted) ----
  function applyMapOptions(rebuild) {
    const c = map.getContainer();
    const labels = $("m-labels").value, size = $("m-size").value, mode = $("m-cluster").value;
    c.classList.remove("lbl-zoom", "lbl-always", "lbl-never", "size-normal", "size-large", "size-xlarge");
    c.classList.add("lbl-" + labels, "size-" + size);
    lsSet("map.labels", labels); lsSet("map.size", size); lsSet("map.cluster", mode); lsSet("map.owntrack", $("m-owntrack").checked);
    if (rebuild) rebuildCluster(mode);
    refreshOwnTrack();
  }
  function loadMapOptions() {
    $("m-labels").value = lsGet("map.labels", "zoom"); $("m-size").value = lsGet("map.size", "normal");
    $("m-cluster").value = lsGet("map.cluster", "overlap"); $("m-owntrack").checked = lsGet("map.owntrack", false);
    applyMapOptions(false);
  }
  $("m-cluster").addEventListener("change", () => applyMapOptions(true));
  ["m-labels", "m-size", "m-owntrack"].forEach((id) => $(id).addEventListener("change", () => applyMapOptions(false)));
  async function refreshOwnTrack() {
    ownTrackLayer.clearLayers();
    if (!$("m-owntrack").checked) return;
    try {
      const d = await api("/api/own?hours=24");
      const pts = (d.history || []).map((p) => [p.lat, p.lon]);
      if (pts.length > 1) L.polyline(pts, { color: "#d81b60", weight: 2.5, opacity: .8, dashArray: "6 4" }).addTo(ownTrackLayer);
    } catch (e) { /* ignore */ }
  }
  loadMapOptions();
  let didFit = !!view;

  function markerIcon(st) {
    const cls = ["stn", st.state, st.position_suspect ? "suspect" : "", st.is_emcomm ? "emcomm" : "",
      isAprs(st) ? "aprs" : "", st.is_object ? "object" : "",
      S.selected === st.callsign ? "sel" : "", S.hover === st.callsign ? "hl" : ""].filter(Boolean).join(" ");
    return L.divIcon({ className: cls, html: `<div class="dot"></div><div class="lbl">${esc(st.callsign)}${st.is_away ? " ⌛" : ""}</div>`, iconSize: [0, 0] });
  }
  function popupHtml(st) {
    return `<b style="font-family:monospace">${esc(st.callsign)}</b> ${esc(st.grid || "")}<br>` +
      `pos ${ageStr(st.position_time)} · heard ${ageStr(st.last_heard)}<br>` +
      `${esc(st.last_band || "")} SNR ${st.last_snr_db ?? "—"} ${dist(st) ? "· " + dist(st) + " " + st.bearing_deg + "°" : ""}`;
  }
  function syncMarker(st) {
    const visible = st.lat != null && passesMapFilter(st);
    let m = S.markers.get(st.callsign);
    if (!visible) { if (m) { cluster.removeLayer(m); S.markers.delete(st.callsign); } return; }
    if (!m) {
      m = L.marker([st.lat, st.lon], { icon: markerIcon(st), stateName: st.state, title: st.callsign });
      m.on("click", () => select(st.callsign));
      m.on("mouseover", () => hover(st.callsign, true)); m.on("mouseout", () => hover(st.callsign, false));
      m.bindTooltip(() => popupHtml(S.stations.get(st.callsign) || st), { direction: "top", offset: [0, -8], opacity: .95 });
      S.markers.set(st.callsign, m); cluster.addLayer(m);
    } else {
      const ll = m.getLatLng();
      if (Math.abs(ll.lat - st.lat) > 1e-9 || Math.abs(ll.lng - st.lon) > 1e-9) m.setLatLng([st.lat, st.lon]);
      if (m.options.stateName !== st.state) { m.options.stateName = st.state; if (cluster.refreshClusters) cluster.refreshClusters(m); }
      m.setIcon(markerIcon(st));
    }
  }
  function refreshAllMarkers() { S.stations.forEach(syncMarker); }
  // A refresh can replace the icon under the pointer, so the browser never sends mouseout: clear hover then.
  map.getContainer().addEventListener("mouseleave", () => { if (S.hover) hover(S.hover, false); });

  function showGridRect(st) {
    if (S.gridRect) { map.removeLayer(S.gridRect); S.gridRect = null; }
    if (!st || !st.grid_bounds || !$("f-grid").checked) return;
    const b = st.grid_bounds;
    S.gridRect = L.rectangle([[b[0], b[1]], [b[2], b[3]]], { color: "#1e88e5", weight: 1, fillOpacity: .08, dashArray: "4 3", interactive: false }).addTo(map);
  }
  function hover(cs, on) {
    S.hover = on ? cs : null;
    const st = S.stations.get(cs); if (st) { syncMarker(st); }
    document.querySelectorAll(".row-stn.hl").forEach((e) => e.classList.remove("hl"));
    if (on) { const r = document.querySelector(`.row-stn[data-cs="${CSS.escape(cs)}"]`); if (r) r.classList.add("hl"); showGridRect(st); }
    else if (S.selected) showGridRect(S.stations.get(S.selected)); else showGridRect(null);
  }

  // ---------------------------------------------------------------- filters
  const FILTER_DEFAULTS = { "f-age": "0", "f-heard": "604800", "f-band": "", "f-dist": "0", "f-snr": "", "f-src": "",
    "f-emcomm": false, "f-bbs": false, "f-email": false, "f-ai": false, "f-pota": false, "f-fav": false };
  const SRC_GROUPS = { beacon: ["beacon", "cq"], broadcast: ["broadcast_gps", "broadcast_grid"], vmail: ["gps_tag"], aprs: ["aprs"], exact: ["broadcast_gps", "gps_tag", "manual", "aprs"] };
  function filters() {
    return {
      q: $("search").value.trim().toUpperCase(), age: Number($("f-age").value), heard: Number($("f-heard").value),
      band: $("f-band").value, dist: Number($("f-dist").value), snr: $("f-snr").value === "" ? null : Number($("f-snr").value),
      src: $("f-src").value,
      emcomm: $("f-emcomm").checked, bbs: $("f-bbs").checked, email: $("f-email").checked, ai: $("f-ai").checked,
      pota: $("f-pota").checked, fav: $("f-fav").checked, unlocated: $("f-unlocated").checked, own: $("f-own").checked,
      aprs: $("f-aprs").checked,
    };
  }
  function passesCommon(st, f) {
    if (st.is_hidden) return false;
    if (!f.own && st.is_own) return false;
    if (!f.aprs && isAprs(st)) return false;
    if (f.q && !(st.callsign.includes(f.q) || (st.grid || "").includes(f.q) || (st.qth || "").toUpperCase().includes(f.q) || (st.op_name || "").toUpperCase().includes(f.q))) return false;
    if (f.band && st.last_band !== f.band && !(st.bands_recent || []).includes(f.band)) return false;
    if (f.heard && (ageSec(st.last_heard) ?? Infinity) > f.heard) return false;
    if (f.snr !== null && (st.last_snr_db == null || st.last_snr_db < f.snr)) return false;
    if (f.dist) { const km = st.distance_km; const lim = S.units === "MI" ? f.dist * 1.609344 : f.dist; if (km == null || km > lim) return false; }
    if (f.src && !(SRC_GROUPS[f.src] || []).includes(st.position_source)) return false;
    if (f.emcomm && !st.is_emcomm) return false; if (f.bbs && !st.is_bbs) return false;
    if (f.email && !st.is_email_gateway) return false; if (f.ai && !st.is_ai_gateway) return false;
    if (f.pota && st.last_cq_tag !== "POTA") return false; if (f.fav && !st.is_favorite) return false;
    return true;
  }
  let F = null;
  function passesMapFilter(st) {
    const f = F || filters();
    if (!passesCommon(st, f)) return false;
    if (f.age && (ageSec(st.position_time) ?? Infinity) > f.age) return false;
    return true;
  }
  function passesListFilter(st) {
    const f = F || filters();
    if (!passesCommon(st, f)) return false;
    if (st.lat == null && !f.unlocated) return false;
    if (f.age && st.lat != null && (ageSec(st.position_time) ?? Infinity) > f.age) return false;
    return true;
  }
  function filterCounts() {
    let n = 0, g = 0;
    for (const id in FILTER_DEFAULTS) {
      const el = $(id); const d = FILTER_DEFAULTS[id];
      const changed = el.type === "checkbox" ? el.checked !== d : el.value !== d;
      if (changed) { if (el.type === "checkbox") g++; else n++; }
    }
    if ($("search").value.trim()) n++;
    return [n, g];
  }
  function updateBadges() {
    const [n, g] = filterCounts();
    $("f-badge").textContent = n; $("f-badge").classList.toggle("hidden", n === 0);
    $("g-badge").textContent = g; $("g-badge").classList.toggle("hidden", g === 0);
  }
  function saveFilters() {
    const o = {}; for (const id in FILTER_DEFAULTS) { const el = $(id); o[id] = el.type === "checkbox" ? el.checked : el.value; }
    o["f-unlocated"] = $("f-unlocated").checked; o["f-own"] = $("f-own").checked; o["f-aprs"] = $("f-aprs").checked; o["f-grid"] = $("f-grid").checked; o.sort = $("sort").value;
    lsSet("filters", o);
  }
  function loadFilters() {
    const o = lsGet("filters", null); if (!o) return;
    // Migration: the old "position newer than" default (7 days) is now "never"; heard-within took over.
    if (o["f-age"] === "604800" && (o["f-heard"] === "0" || o["f-heard"] === undefined)) { o["f-age"] = "0"; o["f-heard"] = "604800"; }
    for (const id in o) { const el = $(id); if (!el) continue; if (el.type === "checkbox") el.checked = !!o[id]; else if ([...el.options].some((op) => op.value === String(o[id]))) el.value = o[id]; }
  }
  function applyFilters() { F = filters(); refreshAllMarkers(); renderList(); F = null; updateBadges(); saveFilters(); }
  ["search", "f-age", "f-heard", "f-band", "f-dist", "f-snr", "f-src", "f-emcomm", "f-bbs", "f-email", "f-ai", "f-pota", "f-fav", "f-unlocated", "f-own", "f-aprs", "sort"].forEach((id) => {
    $(id).addEventListener("input", applyFilters); $(id).addEventListener("change", applyFilters);
  });
  $("f-grid").addEventListener("change", () => { showGridRect(S.selected ? S.stations.get(S.selected) : null); saveFilters(); });
  $("btn-clear-filters").onclick = () => {
    for (const id in FILTER_DEFAULTS) { const el = $(id); if (el.type === "checkbox") el.checked = FILTER_DEFAULTS[id]; else el.value = FILTER_DEFAULTS[id]; }
    $("search").value = ""; applyFilters();
  };
  function updateDistanceLabels() {
    const unit = S.units === "MI" ? "mi" : "km";
    [...$("f-dist").options].forEach((op) => { if (op.value !== "0") op.textContent = `${op.value} ${unit}`; });
  }
  // Close any open dropdown when clicking anywhere else, like a real menu.
  document.addEventListener("click", (e) => { document.querySelectorAll(".dd[open]").forEach((dd) => { if (!dd.contains(e.target)) dd.removeAttribute("open"); }); });

  // ---------------------------------------------------------------- list
  function chips(st) {
    const out = [];
    if (isAprs(st)) out.push(`<span class="chip aprs" title="Heard on APRS via Graywolf${st.aprs_symbol ? " · symbol " + esc(st.aprs_symbol) : ""}">${st.is_object ? "APRS OBJ" : "APRS"}</span>`);
    if (st.is_emcomm) out.push('<span class="chip em">EMCOMM</span>');
    if (st.is_bbs) out.push('<span class="chip">BBS</span>');
    if (st.is_email_gateway) out.push('<span class="chip">EMAIL</span>');
    if (st.is_ai_gateway) out.push('<span class="chip">AI</span>');
    if (st.has_diploma) out.push('<span class="chip">DIPL</span>');
    if (st.last_cq_tag) out.push(`<span class="chip pota">${esc(st.last_cq_tag)}</span>`);
    if (st.is_away) out.push('<span class="chip away">⌛ AWAY</span>');
    if (st.is_favorite) out.push('<span class="chip fav">★</span>');
    if (st.aprs_consent === 1) out.push('<span class="chip consent-y" title="This station said APRS:Y - it may be relayed to APRS">APRS:Y</span>');
    else if (st.aprs_consent === 0) out.push('<span class="chip consent-n" title="This station said APRS:N - never relayed to APRS">APRS:N</span>');
    if (st.position_suspect) out.push('<span class="chip" title="Implausible jump from previous position">?</span>');
    return out.join(" ");
  }
  function sortFn() {
    const k = $("sort").value;
    if (k === "call") return (a, b) => a.callsign.localeCompare(b.callsign);
    if (k === "dist") return (a, b) => (a.distance_km ?? 1e9) - (b.distance_km ?? 1e9);
    if (k === "pos") return (a, b) => (parseT(b.position_time) || 0) - (parseT(a.position_time) || 0);
    return (a, b) => (parseT(b.last_heard) || 0) - (parseT(a.last_heard) || 0);
  }
  function renderList() {
    const f = F || filters();
    const rows = [...S.stations.values()].filter((st) => passesListFilter(st)).sort(sortFn());
    const shown = rows.filter((s) => s.lat != null && passesMapFilter(s)).length;
    $("count-shown").textContent = `${shown} on map · ${rows.length} listed`;
    const html = rows.slice(0, 600).map((st) => {
      const nopos = st.lat == null;
      const tip = `${st.callsign}: last heard ${ageStr(st.last_heard)} ago` + (nopos ? ". No grid locator received (standard beacons carry none)" : `, position ${ageStr(st.position_time)} old`) + ". Click for details";
      return `<div class="row-stn ${nopos ? "nopos" : ""} ${S.selected === st.callsign ? "sel" : ""}" data-cs="${esc(st.callsign)}" title="${esc(tip)}">
        <span class="call">${dotHtml(st)}${esc(st.callsign)}</span>
        <span class="grid">${nopos ? "— no position —" : esc(st.grid || (st.lat.toFixed(2) + "," + st.lon.toFixed(2)))}</span>
        <span class="sub"><span title="Last heard">${ageStr(st.last_heard)}</span>${dist(st) ? `<span>${dist(st)} ${st.bearing_deg}°</span>` : ""}<span title="${(st.bands_recent || []).length > 1 ? "Heard on " + st.bands_recent.join(", ") + " in the last 6 hours" : "Band of the last frame heard"}">${esc(bandsStr(st))}</span><span>SNR ${st.last_snr_db ?? "—"}</span>${chips(st)}</span>
      </div>`;
    }).join("");
    $("list").innerHTML = html || '<div class="muted" style="padding:12px">No stations match.</div>';
    S.listDirty = false;
  }
  $("list").addEventListener("click", (e) => { const r = e.target.closest(".row-stn"); if (r) select(r.dataset.cs); });
  $("list").addEventListener("mouseover", (e) => { const r = e.target.closest(".row-stn"); if (r && S.hover !== r.dataset.cs) hover(r.dataset.cs, true); });
  $("list").addEventListener("mouseleave", () => { if (S.hover) hover(S.hover, false); });

  function updateBands() {
    const sel = $("f-band"); const cur = sel.value;
    const bands = [...S.bands].sort((a, b) => parseFloat(b) - parseFloat(a));
    sel.innerHTML = '<option value="">All bands</option>' + bands.map((b) => `<option value="${esc(b)}">${esc(b)}</option>`).join("");
    sel.value = cur;
  }

  // ---------------------------------------------------------------- detail
  async function select(cs) {
    const prev = S.selected; S.selected = cs;
    if (prev && S.stations.get(prev)) syncMarker(S.stations.get(prev));
    const st = S.stations.get(cs); if (st) syncMarker(st);
    showGridRect(st);
    document.querySelectorAll(".row-stn.sel").forEach((e) => e.classList.remove("sel"));
    const r = document.querySelector(`.row-stn[data-cs="${CSS.escape(cs)}"]`); if (r) r.classList.add("sel");
    try { renderDetail(await api(`/api/station/${encodeURIComponent(cs)}`)); } catch (e) { console.warn(e); }
  }
  function closeDetail() {
    const prev = S.selected; S.selected = null; $("detail").classList.add("hidden"); showPanel(S.tab);
    if (prev && S.stations.get(prev)) syncMarker(S.stations.get(prev));
    showGridRect(null); clearTrack();
  }
  function renderDetail(d) {
    const unl = d.lat == null;
    const acc = d.accuracy_m ? (d.accuracy_m >= 1000 ? `±${(d.accuracy_m / 1000).toFixed(0)} km` : `±${d.accuracy_m.toFixed(0)} m`) : "";
    const srcName = { beacon: "VarAC advanced beacon (grid only)", cq: "VarAC CQ frame (grid only)", gps_tag: "VarAC VMail <GPS:> tag", broadcast_gps: "VarAC broadcast with <GPS:> (a Position TX)", broadcast_grid: "grid found in VarAC broadcast text", manual: "manual", aprs: "APRS via Graywolf" }[d.position_source] || d.position_source || "";
    const distinctGrids = new Set((d.positions || []).map((p) => p.grid).filter(Boolean)).size;
    const heard = (d.heard || []).map((h) => `<div class="heard-row"><span class="t">${ageStr(h.heard_at)}</span><span>${esc(h.frame_kind)}${h.had_position ? "" : " (no loc)"}</span><span>${esc(h.band || "")} ${h.snr_db ?? ""}</span><span class="txt">${esc(h.text || "")}</span></div>`).join("");
    $("detail").innerHTML = `
      <h2>${dotHtml(d)}${esc(d.callsign)} ${chips(d)}<button class="x" id="d-close">✕</button></h2>
      <table>
        <tr><td>Position</td><td>${unl ? "<i>no position — this station sends standard beacons, which carry no grid locator</i>" :
          `<b>${esc(d.grid || "")}</b> · ${d.lat.toFixed(d.accuracy_m && d.accuracy_m < 1000 ? 5 : 2)}, ${d.lon.toFixed(d.accuracy_m && d.accuracy_m < 1000 ? 5 : 2)} <span class="muted">(${d.grid ? "grid centre, " : ""}${acc}, ${esc(srcName)})</span>${d.position_suspect ? ' <span class="chip">? implausible jump</span>' : ""}`}</td></tr>
        <tr><td>Position age</td><td>${unl ? "—" : ageStr(d.position_time) + " ago"}</td></tr>
        <tr><td>Last heard</td><td><b>${ageStr(d.last_heard)} ago</b> <span class="muted">(${esc(d.last_frame_kind || "")})</span></td></tr>
        <tr><td>Distance</td><td>${dist(d) ? `${dist(d)} · bearing ${d.bearing_deg}°` : "—"}</td></tr>
        <tr><td>Radio</td><td>${esc(d.last_band || "")} ${d.last_frequency_hz ? fmtFreq(d.last_frequency_hz) + " Hz" : ""} ${d.last_bandwidth ? d.last_bandwidth + " Hz" : ""} · SNR ${d.last_snr_db ?? "—"} dB${(d.bands_recent || []).length > 1 ? `<br><span class="muted">heard on ${esc(d.bands_recent.join(", "))} in the last 6 h</span>` : ""}</td></tr>
        <tr><td>Operator</td><td>${esc(d.op_name || "")} ${d.qth ? "· " + esc(d.qth) : ""}</td></tr>
        <tr><td>Heard</td><td>${d.heard_count} times since ${esc((d.first_heard || "").slice(0, 10))}</td></tr>
        <tr><td>Positions</td><td>${d.position_count} recorded · ${distinctGrids} distinct grid${distinctGrids === 1 ? "" : "s"} (last 30 d)</td></tr>
        ${d.last_text ? `<tr><td>Last text</td><td>${esc(d.last_text)}</td></tr>` : ""}
      </table>
      <div class="actions">
        <button id="d-centre" ${unl ? "disabled" : ""} title="Centre the map on this station">Centre</button>
        <button id="d-track" ${unl ? "disabled" : ""} title="Draw this station's positions over the last 7 days as a line">${S.track === d.callsign ? "Hide track" : "Show track (7 d)"}</button>
        <button id="d-copy" ${d.grid ? "" : "disabled"} title="Copy the grid square to the clipboard">Copy grid</button>
        <button id="d-fav" title="Mark as a favourite so you can filter on it">${d.is_favorite ? "★ Unfavourite" : "☆ Favourite"}</button>
        <button id="d-hide" title="Hide this station from the map and list (it keeps being recorded)">${d.is_hidden ? "Unhide" : "Hide"}</button>
        ${isAprs(d) && !unl ? `<button id="d-relay" title="Broadcast this APRS station's position on VarAC once, as 'APRS ${esc(d.callsign)} <GPS:...> via ${esc((S.health && S.health.varac && S.health.varac.mycall) || "me")}'. Manual: needs 60 s since the previous VarAC transmission, counts against your hourly limit and every other Position TX interlock">Relay to VarAC</button>` : ""}
        ${!isAprs(d) && !unl && !d.is_own ? `<button id="d-relay-aprs" ${d.aprs_consent === 1 ? "" : "disabled"} title="${d.aprs_consent === 1 ? "Send this VarAC station to APRS now as an object under your callsign, through Graywolf (respects the APRS dry-run switch and the hourly cap)" : "Not allowed: this station has not said APRS:Y in a VarAC broadcast, so it may not be relayed to APRS"}">Relay to APRS</button>` : ""}
      </div>
      ${d.aprs_consent != null ? `<div class="muted" style="margin:4px 0">APRS relay consent: <b>${d.aprs_consent ? "given (APRS:Y)" : "refused (APRS:N)"}</b> · stated ${ageStr(d.aprs_consent_at)} ago</div>` : ""}
      <textarea id="d-notes" placeholder="Notes…" title="Your private notes about this station">${esc(d.notes || "")}</textarea>
      <div class="row"><button id="d-save-notes" title="Save the notes above">Save notes</button><span id="d-notes-msg" class="muted"></span></div>
      <h4 class="muted">Recent activity</h4>${heard}`;
    $("detail").classList.remove("hidden"); $("list").classList.add("hidden"); $("broadcasts").classList.add("hidden");
    $("d-close").onclick = closeDetail;
    $("d-centre").onclick = () => map.setView([d.lat, d.lon], Math.max(map.getZoom(), 9));
    $("d-track").onclick = () => (S.track === d.callsign ? clearTrack() : showTrack(d.callsign));
    $("d-copy").onclick = () => navigator.clipboard && navigator.clipboard.writeText(d.grid);
    $("d-fav").onclick = async () => { await api(`/api/station/${encodeURIComponent(d.callsign)}`, { is_favorite: d.is_favorite ? 0 : 1 }); refreshStations(); select(d.callsign); };
    $("d-hide").onclick = async () => { await api(`/api/station/${encodeURIComponent(d.callsign)}`, { is_hidden: d.is_hidden ? 0 : 1 }); closeDetail(); refreshStations(true); };
    $("d-save-notes").onclick = async () => { await api(`/api/station/${encodeURIComponent(d.callsign)}`, { notes: $("d-notes").value }); $("d-notes-msg").textContent = "saved"; };
    const relayAprs = $("d-relay-aprs");
    if (relayAprs) relayAprs.onclick = async () => {
      const gw = S.config && S.config.graywolf;
      if (!gw || !gw.enabled) { alert("Enable the APRS feed from Graywolf first (Settings > APRS)."); return; }
      if (!gw.dry_run && !confirm(`Send ${d.callsign} to APRS now as an object under your callsign?`)) return;
      const r = await api(`/api/station/${encodeURIComponent(d.callsign)}/relay_to_aprs`, {});
      alert(r.ok ? (r.dry_run ? "Dry run logged: " : "Sent to APRS: ") + r.message : "Not sent: " + r.error);
    };
    const relayBtn = $("d-relay");
    if (relayBtn) relayBtn.onclick = async () => {
      const dry = S.config && S.config.beacon && S.config.beacon.dry_run;
      if (!dry && !confirm(`Broadcast ${d.callsign}'s APRS position on VarAC now, under your callsign?`)) return;
      const r = await api(`/api/station/${encodeURIComponent(d.callsign)}/relay_to_varac`, {});
      alert(r.ok ? (r.dry_run ? "Dry run logged: " : "Sent on VarAC: ") + r.message : "Not sent: " + r.error);
    };
  }
  async function showTrack(cs) {
    clearTrack();
    const t = await api(`/api/station/${encodeURIComponent(cs)}/track?days=7`);
    if (!t.points.length) return;
    S.track = cs;
    const ll = t.points.map((p) => [p.lat, p.lon]);
    L.polyline(ll, { color: "#1e88e5", weight: 2.5, opacity: .8 }).addTo(trackLayer);
    t.points.forEach((p) => {
      L.marker([p.lat, p.lon], { icon: L.divIcon({ className: "track-pt", html: '<div class="p"></div>', iconSize: [0, 0] }), interactive: true })
        .bindTooltip(`${esc(p.grid || "")} ×${p.count}<br>${new Date(p.heard_at).toLocaleString()}${p.count > 1 ? " → " + new Date(p.until).toLocaleString() : ""}<br>${esc(p.source)}${p.suspect ? " (suspect)" : ""}`)
        .addTo(trackLayer);
    });
    if (ll.length > 1) map.fitBounds(L.latLngBounds(ll).pad(0.2));
    const b = $("d-track"); if (b) b.textContent = "Hide track";
  }
  function clearTrack() { trackLayer.clearLayers(); S.track = null; const b = $("d-track"); if (b) b.textContent = "Show track (7 d)"; }

  // ---------------------------------------------------------------- data refresh
  async function refreshStations(full) {
    const url = "/api/stations" + (S.since && !full ? "?since=" + encodeURIComponent(S.since) : "");
    let d; try { d = await api(url); } catch (e) { console.warn("stations", e); return; }
    S.offsetMs = Date.parse(d.now) - Date.now();
    if (full || !d.delta) { S.stations.clear(); S.markers.forEach((m) => cluster.removeLayer(m)); S.markers.clear(); }
    S.own = d.own;
    let changed = d.stations.length > 0;
    F = filters();
    if (S.hover && d.stations.some((st) => st.callsign === S.hover)) S.hover = null;   // do not carry a stale highlight through a refresh
    d.stations.forEach((st) => { S.stations.set(st.callsign, st); if (st.last_band) S.bands.add(st.last_band); syncMarker(st); });
    F = null;
    if (S.bands.size !== $("f-band").options.length - 1) { updateBands(); loadFilters(); }
    S.since = d.now;
    if (changed || full) renderList();
    if (!didFit && S.markers.size) { didFit = true; fitVisible(); }
  }
  function rerenderAges() {
    // Age classes drift with time even when no new data arrives.
    let dirty = false;
    S.stations.forEach((st) => { const s = stateOf(st.position_time); if (st.lat != null && s !== st.state) { st.state = s; dirty = true; } });
    if (dirty) { F = filters(); refreshAllMarkers(); F = null; }
    renderList();
  }
  function fitVisible() {
    const pts = [...S.markers.values()].map((m) => m.getLatLng());
    if (S.ownMarker) pts.push(S.ownMarker.getLatLng());
    if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.1), { maxZoom: 10 });
  }
  $("btn-fit").onclick = fitVisible;
  $("btn-me").onclick = () => { if (S.ownMarker) map.setView(S.ownMarker.getLatLng(), Math.max(map.getZoom(), 9)); };

  async function refreshHealth() {
    let h; try { h = await api("/api/health"); } catch (e) { $("pill-varac").textContent = "VarMap offline"; $("pill-varac").className = "pill err"; return; }
    S.health = h; if (S.units !== h.units) { S.units = h.units; updateDistanceLabels(); }
    const p = h.poller;
    const pill = $("pill-varac");
    if (p.connected) { pill.textContent = p.varac_running === false ? "VarAC DB OK · app not running" : "VarAC OK"; pill.className = "pill " + (p.varac_running === false ? "warn" : "ok"); }
    else { pill.textContent = p.status === "locked" ? "VarAC DB busy" : "VarAC DB unavailable"; pill.className = "pill " + (p.status === "locked" ? "warn" : "err"); pill.title = p.last_error || ""; }
    $("st-varac").textContent = `VarAC: ${p.connected ? "connected" : (p.last_error || "not connected")} · poll ${p.last_poll ? ageStr(p.last_poll) + " ago" : "—"} · ${(h.counts.frames || 0).toLocaleString()} frames · ${h.counts.stations_with_position}/${h.counts.stations} located` + (h.varac.last_frequency_hz ? ` · ${fmtFreq(h.varac.last_frequency_hz)} Hz` : "") + (p.db_version ? ` · db v${p.db_version}` : "");
    const bf = p.backfill;
    if (bf && bf.active) { $("st-backfill").classList.remove("hidden"); $("st-backfill").textContent = `Backfilling ${bf.table}: ${bf.done.toLocaleString()} / ${bf.total.toLocaleString()}`; }
    else $("st-backfill").classList.add("hidden");
    const own = h.own && h.own.fix;
    if (own) {
      $("st-own").textContent = `Own: ${own.grid || ""} ${own.lat.toFixed(4)}, ${own.lon.toFixed(4)} (${own.source}${own.speed_kmh != null ? `, ${(S.units === "MI" ? own.speed_kmh / 1.609344 : own.speed_kmh).toFixed(0)} ${S.units === "MI" ? "mph" : "km/h"}` : ""}${own.course_deg != null ? `, ${own.course_deg.toFixed(0)}°` : ""})`;
      if (!S.ownMarker) {
        S.ownMarker = L.marker([own.lat, own.lon], { icon: L.divIcon({ className: "own-marker", html: `<div class="ring"></div><div class="lbl">${esc(h.varac.mycall || "ME")}</div>`, iconSize: [0, 0] }), zIndexOffset: 1000 }).addTo(map);
      } else S.ownMarker.setLatLng([own.lat, own.lon]);
      S.ownMarker.bindTooltip(`<b>${esc(h.varac.mycall || "")}</b> ${esc(own.grid || "")}<br>${own.source} · ${ageStr(own.time)} ago`);
    } else { $("st-own").textContent = "Own: no position (" + ((h.own && h.own.last_error) || "no source") + ")"; }
    const gw = h.graywolf || {};
    if (gw.enabled) {
      $("st-aprs").classList.remove("hidden");
      $("st-aprs").textContent = `APRS: ${gw.connected ? "Graywolf connected" : "Graywolf " + (gw.status || "unavailable")}${gw.last_poll ? " · poll " + ageStr(gw.last_poll) + " ago" : ""}${gw.stations_total ? " · " + gw.stations_total.toLocaleString() + " updates" : ""}`;
      $("st-aprs").title = gw.last_error || ("Graywolf " + (gw.version || "") + " at " + (gw.base || ""));
    } else $("st-aprs").classList.add("hidden");
    const t = h.tiles;
    $("st-tiles").textContent = `Tiles: ${t.online_fetch ? "online + cache" : "OFFLINE cache only"}` + (t.download && t.download.active ? ` · downloading ${t.download.done}/${t.download.total}` : "") + (t.last_error && t.online_fetch ? " · fetch error" : "");
    const b = h.beacon; const bp = $("pill-beacon");
    if (!b.enabled) { bp.textContent = "POSITION TX OFF"; bp.className = "pill off"; }
    else if (b.blocked) { bp.textContent = "POSITION TX BLOCKED"; bp.className = "pill warn"; bp.title = b.blocked; }
    else { const nd = b.next_due_seconds != null ? ` · next ${Math.max(0, Math.round(b.next_due_seconds))} s` : ""; bp.textContent = (b.dry_run ? "POSITION TX DRY-RUN" : "POSITION TX LIVE") + nd; bp.className = "pill " + (b.dry_run ? "dry" : "on"); bp.title = b.decision || ""; }
    $("st-clock").textContent = new Date(nowServer()).toISOString().slice(11, 19) + "z";
    const up = h.updates || {};
    const pu = $("pill-update");
    if (up.available) { pu.textContent = `Update v${up.latest}`; pu.classList.remove("hidden"); pu.title = `VarMap ${up.latest} is available (you run ${up.current}). Click for details`; }
    else pu.classList.add("hidden");
    S.updates = up;
  }
  function renderUpdateStatus(up) {
    if (!up) return;
    const lines = [`Running VarMap ${up.current} (${up.mode} build)`,
      up.latest ? `Latest release: ${up.latest}${up.available ? " - UPDATE AVAILABLE" : (up.skipped === up.latest ? " (skipped)" : " - you are up to date")}` : "Latest release: not checked yet",
      up.checked_at ? `Last check: ${ageStr(up.checked_at)} ago` : "", up.error ? `Last check failed: ${up.error}` : "",
      up.applying ? `Update in progress: ${up.applying}` : ""].filter(Boolean);
    $("update-status").textContent = lines.join("\n");
    $("btn-update-apply").classList.toggle("hidden", !(up.available && up.can_self_update));
    $("btn-update-skip").classList.toggle("hidden", !up.available);
    const a = $("lnk-update"); a.href = up.url || "https://github.com/KK4ODA/VarMap/releases"; a.classList.toggle("hidden", !up.latest);
  }
  async function applyUpdate() {
    const up = S.updates || {};
    if (!up.available) return;
    if (!up.can_self_update) { window.open(up.url || "https://github.com/KK4ODA/VarMap/releases", "_blank"); return; }
    const notes = (up.notes || "").replace(/[#*`]/g, "").slice(0, 600);
    if (!confirm(`Install VarMap ${up.latest} now?\n\nVarMap will download the installer, verify it, close, install and reopen. Your settings and station database are kept.\n\n${notes}`)) return;
    const r = await api("/api/updates/apply", {});
    alert(r.ok ? r.message : "Update failed: " + r.error);
  }
  $("pill-update").onclick = applyUpdate;
  $("btn-update-apply").onclick = applyUpdate;
  $("btn-update-check").onclick = async () => { $("update-status").textContent = "Checking…"; const up = await api("/api/updates/check", {}); S.updates = up; renderUpdateStatus(up); refreshHealth(); };
  $("btn-update-skip").onclick = async () => { const r = await api("/api/updates/skip", { version: (S.updates || {}).latest }); S.updates = r.state; renderUpdateStatus(r.state); refreshHealth(); };

  async function refreshBroadcasts() {
    if (S.tab !== "broadcasts") return;
    try {
      const d = await api("/api/broadcasts?limit=80");
      $("broadcasts").innerHTML = d.broadcasts.map((b) => `<div class="bc-row"><div class="h"><b data-cs="${esc(b.callsign)}">${esc(b.callsign)}</b><span>${ageStr(b.heard_at)} ago</span><span>${esc(b.band || "")} ${b.snr_db ?? ""}</span></div><div>${esc(b.text || "")}</div></div>`).join("") || '<div class="muted" style="padding:12px">No broadcasts yet.</div>';
    } catch (e) { /* ignore */ }
  }
  $("broadcasts").addEventListener("click", (e) => { const b = e.target.closest("b[data-cs]"); if (b) select(b.dataset.cs); });

  function showPanel(tab) {
    S.tab = tab;
    document.querySelectorAll(".side-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    $("detail").classList.add("hidden");
    $("list").classList.toggle("hidden", tab !== "stations"); $("broadcasts").classList.toggle("hidden", tab !== "broadcasts");
    if (tab === "broadcasts") refreshBroadcasts();
  }
  document.querySelectorAll(".side-tab").forEach((b) => b.addEventListener("click", () => { S.selected = null; showPanel(b.dataset.tab); }));

  // ---------------------------------------------------------------- settings
  const modal = $("settings");
  function getPath(o, p) { return p.split(".").reduce((a, k) => (a == null ? undefined : a[k]), o); }
  function setPath(o, p, v) { const ks = p.split("."); let cur = o; ks.slice(0, -1).forEach((k) => { cur[k] = cur[k] || {}; cur = cur[k]; }); cur[ks[ks.length - 1]] = v; }
  function bindConfig(cfg) {
    document.querySelectorAll("[data-cfg]").forEach((el) => {
      const v = getPath(cfg, el.dataset.cfg);
      if (el.type === "checkbox") el.checked = !!v; else el.value = v == null ? "" : v;
    });
  }
  function collectConfig() {
    const patch = {};
    document.querySelectorAll("[data-cfg]").forEach((el) => {
      let v; if (el.type === "checkbox") v = el.checked; else if (el.type === "number") v = el.value === "" ? 0 : Number(el.value); else v = el.value;
      setPath(patch, el.dataset.cfg, v);
    });
    return patch;
  }
  async function openSettings(pane) {
    S.config = await api("/api/config"); bindConfig(S.config);
    modal.classList.remove("hidden"); if (pane) showPane(pane);
    refreshSettingsInfo(); refreshBeaconPane(); refreshTilesPane(); refreshAprsPane();
  }
  function showPane(p) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.pane === p));
    document.querySelectorAll(".pane").forEach((t) => t.classList.toggle("active", t.dataset.pane === p));
  }
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showPane(t.dataset.pane)));
  $("btn-settings").onclick = () => openSettings();
  $("pill-beacon").onclick = () => openSettings("beacon");
  $("btn-close-settings").onclick = () => modal.classList.add("hidden");
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if (!modal.classList.contains("hidden")) modal.classList.add("hidden"); else if (S.selected) closeDetail(); } });
  $("btn-save").onclick = async () => {
    const gwDry = document.querySelector('[data-cfg="graywolf.dry_run"]');
    const gwLive = gwDry && !gwDry.checked && (document.querySelector('[data-cfg="graywolf.mirror_enabled"]').checked || document.querySelector('[data-cfg="graywolf.gate_enabled"]').checked);
    if (gwLive && !confirm("APRS dry run is OFF and mirroring or object relaying is enabled: VarMap will ask Graywolf to transmit on APRS under your callsign. Continue?")) return;
    try { const r = await api("/api/config", collectConfig()); S.config = r.config; $("save-result").textContent = "Saved."; S.pollMs = Math.max(2000, (S.config.varac.poll_interval_seconds || 10) * 1000); if (legend._render) legend._render(); refreshHealth(); refreshStations(true); }
    catch (e) { $("save-result").textContent = "Save failed: " + e.message; }
  };
  $("btn-test-db").onclick = async () => {
    const path = document.querySelector('[data-cfg="varac.db_path"]').value;
    $("test-db-result").textContent = "testing…";
    try { const r = await api("/api/test_db", { path }); $("test-db-result").textContent = r.ok ? `OK: db_version ${r.db_version}, ${r.cqframe_rows.toLocaleString()} frames, journal ${r.journal_mode}` : "FAILED: " + r.error; }
    catch (e) { $("test-db-result").textContent = e.message; }
  };
  function refreshSettingsInfo() {
    if (!S.health) return;
    const fmt = (o) => JSON.stringify(o, null, 1).replace(/[{}",]/g, "").replace(/\n\s*\n/g, "\n").trim();
    $("varac-info").textContent = fmt(S.health.varac);
    $("own-info").textContent = fmt(S.health.own);
    $("graywolf-info").textContent = fmt(S.health.graywolf || {});
    renderUpdateStatus(S.health.updates);
  }
  async function refreshAprsPane() {
    if (modal.classList.contains("hidden")) return;
    let s; try { s = await api("/api/aprs/status"); } catch (e) { return; }
    const m = s.mirror || {}, g = s.gate || {};
    $("aprs-status").textContent = [
      `Mirror: ${m.enabled ? (m.dry_run ? "on (dry run)" : "on - LIVE") : "off"}${m.last ? " · last: " + (m.last.ok ? "ok " : "FAILED ") + (m.last.message || m.last.error || "") : ""}`,
      `Object relay: ${g.enabled ? (g.dry_run ? "on (dry run)" : "on - LIVE") : "off"} · last pass ${g.last_run ? ageStr(g.last_run) + " ago" : "never"} · ${g.candidates || 0} candidates · ${g.objects || 0} objects · ${g.sent_last_hour || 0} sent last hour${g.last_error ? " · error: " + g.last_error : ""}`,
    ].join("\n");
    $("aprs-consenting").innerHTML = (s.consenting || []).map((c) => `<div class="r"><span>${esc((c.aprs_consent_at || "").slice(0, 16).replace("T", " "))}</span><span>${c.aprs_consent ? "APRS:Y" : "APRS:N"}</span><span>${esc(c.grid || "")}</span><span>${esc(c.callsign)} · heard ${ageStr(c.last_heard)} ago</span></div>`).join("") || '<div class="muted" style="padding:6px">No station has stated APRS consent yet.</div>';
    $("aprs-log").innerHTML = (s.recent || []).map((r) => `<div class="r ${r.ok ? "" : "bad"} ${r.dry_run ? "dry" : ""}"><span>${esc((r.requested_at || "").slice(0, 19).replace("T", " "))}</span><span>${esc(r.trigger)}</span><span>${r.dry_run ? "dry-run" : r.ok ? "SENT" : "FAILED"}</span><span>${esc(r.message || "")} ${esc(r.error || "")}</span></div>`).join("") || '<div class="muted" style="padding:6px">Nothing sent to Graywolf yet.</div>';
  }
  $("btn-mirror-test").onclick = async () => {
    await api("/api/config", collectConfig());
    $("mirror-result").textContent = "sending…";
    const r = await api("/api/aprs/mirror/send_test", {});
    $("mirror-result").textContent = r.ok ? (r.dry_run ? "Dry run logged: " : "Sent: ") + r.message : "Not sent: " + (r.error || r.skipped);
    refreshAprsPane();
  };
  $("btn-gate-run").onclick = async () => {
    await api("/api/config", collectConfig());
    $("gate-result").textContent = "running…";
    const r = await api("/api/aprs/gate/run_now", {});
    $("gate-result").textContent = r.ok ? `${r.sent} object(s) ${r.state && r.state.dry_run ? "logged (dry run)" : "sent"}` : "Failed: " + r.error;
    refreshAprsPane();
  };
  $("btn-graywolf-test").onclick = async () => {
    $("graywolf-test-result").textContent = "testing…";
    const body = { url: document.querySelector('[data-cfg="graywolf.url"]').value, username: document.querySelector('[data-cfg="graywolf.username"]').value, password: document.querySelector('[data-cfg="graywolf.password"]').value };
    try { const r = await api("/api/graywolf/test", body); $("graywolf-test-result").textContent = r.ok ? `OK: Graywolf ${r.version || ""}, ${r.stations_24h} stations in 24 h, position ${r.position && r.position.valid ? r.position.source + " " + r.position.lat.toFixed(4) + ", " + r.position.lon.toFixed(4) : "none"}` : "FAILED: " + r.error; }
    catch (e) { $("graywolf-test-result").textContent = e.message; }
  };
  async function refreshBeaconPane() {
    if (modal.classList.contains("hidden")) return;
    let b; try { b = await api("/api/beacon"); } catch (e) { return; }
    const lines = [`Auto position TX: ${b.enabled ? (b.dry_run ? "RUNNING (dry run)" : "RUNNING - LIVE") : "stopped"}`,
      `Mode: ${b.mode} · method: ${b.method}`, `Decision: ${b.blocked ? "blocked - " + b.blocked : b.decision || "—"}${b.next_due_seconds != null ? ` · next possible in ${Math.round(b.next_due_seconds)} s` : ""}`,
      `VarAC frequency: ${b.current_frequency_hz ? fmtFreq(b.current_frequency_hz) + " Hz" : "unknown"}${b.on_calling_frequency_hz ? " - a CALLING FREQUENCY (smart timing blocked here)" : ""}`,
      `VarAC activity: ${b.varac_activity ? (b.varac_activity.busy === true ? "BUSY - " + b.varac_activity.reason : b.varac_activity.busy === false ? "free (not connected, Broadcast window closed)" : b.varac_activity.reason || "unknown") : "not checked yet"}`,
      `VarAC Ignore DCD box: ${b.ignore_dcd === true ? "TICKED - busy-channel protection OFF" : b.ignore_dcd === false ? "not ticked (VarAC waits for a clear channel)" : "unknown (VarAC not running or not checked yet)"}`,
      `Transmissions last hour: ${b.tx_last_hour} (limit ${b.effective ? b.effective.max_per_hour : "?"}/h, ${b.limits ? b.limits.max_per_day : "?"}/day)`,
      b.effective ? `Effective timing after built-in limits: fixed ${b.effective.fixed.interval_seconds} s${b.effective.fixed.only_if_moved ? " only if moved, keepalive " + b.effective.fixed.max_interval_seconds + " s" : ""} · smart min ${b.effective.smart.min_interval_seconds} s / keepalive ${b.effective.smart.max_interval_seconds} s` : "",
      b.last_tx ? `Last: ${b.last_tx.requested_at} ${b.last_tx.trigger} ${b.last_tx.ok ? "OK" : "FAILED " + (b.last_tx.error || "")}${b.last_tx.dry_run ? " (dry)" : ""}` : "Last: —"];
    $("beacon-status").textContent = lines.join("\n");
    $("beacon-preview").textContent = b.preview && b.preview.ok ? `${b.preview.message}  (${b.preview.length}/${b.preview.max})` : (b.preview && b.preview.error) || "";
    $("beacon-log").innerHTML = (b.recent || []).map((r) => `<div class="r ${r.ok ? "" : "bad"} ${r.dry_run ? "dry" : ""}"><span>${esc((r.requested_at || "").slice(0, 19).replace("T", " "))}</span><span>${esc(r.trigger)}</span><span>${r.dry_run ? "dry-run" : r.ok ? "SENT" : "FAILED"}${r.frequency_hz ? "<br>" + fmtFreq(r.frequency_hz) : ""}</span><span>${esc(r.message || "")} ${esc(r.error || "")}</span></div>`).join("") || '<div class="muted" style="padding:6px">No transmissions logged.</div>';
  }
  $("btn-beacon-on").onclick = async () => {
    const dry = document.querySelector('[data-cfg="beacon.dry_run"]').checked;
    if (!dry && !confirm("Start LIVE automatic position TX? VarMap will drive VarAC to broadcast your position on the air under your callsign, first in one interval and then per the selected timing, until you press Stop. You are responsible for these transmissions.")) return;
    await api("/api/config", collectConfig()); await api("/api/beacon/enable", { enabled: true, dry_run: dry }); refreshBeaconPane(); refreshHealth();
  };
  $("btn-beacon-off").onclick = async () => { await api("/api/beacon/enable", { enabled: false }); refreshBeaconPane(); refreshHealth(); };
  // Smart timing profiles.  HF mirrors VARA HF / HF APRS tracker practice; VHF is for VARA FM.
  const SMART_PROFILES = {
    hf: { min_interval_seconds: 600, max_interval_seconds: 3600, slow_speed_kmh: 5, slow_rate_seconds: 3600, fast_speed_kmh: 90,
          fast_rate_seconds: 600, min_turn_time_seconds: 600, turn_min_deg: 30, turn_slope: 255, min_move_m: 1000, grid_dwell_seconds: 120 },
    vhf: { min_interval_seconds: 300, max_interval_seconds: 1800, slow_speed_kmh: 5, slow_rate_seconds: 1800, fast_speed_kmh: 90,
           fast_rate_seconds: 300, min_turn_time_seconds: 300, turn_min_deg: 30, turn_slope: 255, min_move_m: 500, grid_dwell_seconds: 90 },
  };
  function applySmartProfile(name) {
    const p = SMART_PROFILES[name]; if (!p) return;
    for (const k in p) { const el = document.querySelector(`[data-cfg="beacon.smart.${k}"]`); if (el) el.value = p[k]; }
  }
  $("smart-profile").addEventListener("change", (e) => applySmartProfile(e.target.value));
  document.querySelectorAll('[data-cfg^="beacon.smart."]').forEach((el) => {
    if (el.id === "smart-profile" || el.type === "checkbox") return;
    el.addEventListener("input", () => { $("smart-profile").value = "custom"; });
  });
  document.querySelector('[data-cfg="beacon.fixed.only_if_moved"]').addEventListener("change", (e) => {
    if (!e.target.checked && !confirm("Switch off 'Only if moved'?\n\nA station that is not moving would then repeat the same position every interval on a shared calling frequency. VarMap will still enforce at least 5 minutes between transmissions and the hourly limit, but 'Only if moved' is the polite setting.\n\nOK = switch it off anyway, Cancel = keep it on.")) e.target.checked = true;
  });
  $("btn-rehearse").onclick = async () => {
    await api("/api/config", collectConfig());
    $("beacon-status").textContent = "Rehearsing: opening VarAC's broadcast dialog (it will steal focus for a moment)…";
    const r = await api("/api/beacon/rehearse", {});
    alert(r.ok ? `Dialog test OK, nothing sent.\nMessage: ${r.message}\nVarAC counted ${r.typed_bytes} bytes.` : "Dialog test FAILED: " + r.error);
    refreshBeaconPane();
  };
  $("btn-send-now").onclick = async () => {
    await api("/api/config", collectConfig());
    const r = await api("/api/beacon/send_now", {}); alert(r.ok ? (r.dry_run ? "Dry run logged: " : "Sent: ") + r.message : "Not sent: " + r.error); refreshBeaconPane();
  };
  document.querySelector('[data-cfg="beacon.message_template"]').addEventListener("input", async () => { await api("/api/config", { beacon: { message_template: document.querySelector('[data-cfg="beacon.message_template"]').value, comment: document.querySelector('[data-cfg="beacon.comment"]').value, coord_decimals: Number(document.querySelector('[data-cfg="beacon.coord_decimals"]').value) } }); refreshBeaconPane(); });

  // tiles pane
  $("btn-use-view").onclick = () => { const b = map.getBounds(); $("rg-south").value = b.getSouth().toFixed(4); $("rg-north").value = b.getNorth().toFixed(4); $("rg-west").value = b.getWest().toFixed(4); $("rg-east").value = b.getEast().toFixed(4); $("rg-zmin").value = Math.max(0, map.getZoom() - 2); $("rg-zmax").value = Math.min(16, map.getZoom() + 4); estimate(); };
  function bbox() { return { name: $("rg-name").value, south: Number($("rg-south").value), west: Number($("rg-west").value), north: Number($("rg-north").value), east: Number($("rg-east").value), zmin: Number($("rg-zmin").value), zmax: Number($("rg-zmax").value) }; }
  async function estimate() { try { const r = await api("/api/tiles/estimate", bbox()); $("rg-estimate").textContent = `${r.tiles.toLocaleString()} tiles, ~${r.approx_mb} MB ${r.ok ? "" : "— exceeds limit of " + r.limit.toLocaleString()}`; } catch (e) { $("rg-estimate").textContent = e.message; } }
  $("btn-estimate").onclick = estimate;
  ["rg-zmin", "rg-zmax"].forEach((id) => $(id).addEventListener("change", estimate));
  $("btn-download").onclick = async () => { await api("/api/config", collectConfig()); const r = await api("/api/tiles/download", bbox()); if (!r.ok) alert(r.error); refreshTilesPane(); };
  $("btn-cancel-dl").onclick = async () => { await api("/api/tiles/cancel", {}); };
  async function refreshTilesPane() {
    if (modal.classList.contains("hidden")) return;
    let s; try { s = await api("/api/tiles/stats"); } catch (e) { return; }
    const d = s.download || {};
    if (d.total) { $("dl-bar").style.width = Math.round(100 * d.done / d.total) + "%"; $("dl-status").textContent = `${d.active ? "Downloading" : d.cancelled ? "Cancelled" : "Finished"} "${d.name}": ${d.done.toLocaleString()} / ${d.total.toLocaleString()} (${d.skipped} cached, ${d.failed} failed, ${(d.bytes / 1048576).toFixed(1)} MB)${d.error ? " ERROR " + d.error : ""}`; }
    else { $("dl-bar").style.width = "0"; $("dl-status").textContent = ""; }
    $("tiles-stats").textContent = `${s.tiles.toLocaleString()} tiles · ${(s.file_bytes / 1048576).toFixed(1)} MB · ${s.path}\nby zoom: ${Object.entries(s.by_zoom).map(([z, n]) => `z${z}:${n}`).join(" ")}${s.fetch_errors ? "\nlast fetch error: " + s.fetch_errors : ""}`;
    $("regions").innerHTML = (s.regions || []).map((r) => `<div class="r"><span>${esc((r.created_at || "").slice(0, 16).replace("T", " "))}</span><span>z${r.zmin}-${r.zmax}</span><span>${(r.tiles || 0).toLocaleString()} tiles</span><span>${esc(r.name)} <button data-rid="${r.id}" class="rg-del">remove</button></span></div>`).join("") || '<div class="muted" style="padding:6px">No regions downloaded.</div>';
  }
  $("regions").addEventListener("click", async (e) => { const b = e.target.closest(".rg-del"); if (!b) return; const purge = confirm("Also delete the cached tiles of this region? (OK = delete tiles, Cancel = keep tiles, forget region)"); await api(`/api/tiles/region/${b.dataset.rid}?purge=${purge ? 1 : 0}`, null, "DELETE"); refreshTilesPane(); });

  // ---------------------------------------------------------------- boot
  async function boot() {
    loadFilters(); updateDistanceLabels(); updateBadges();
    try { S.config = await api("/api/config"); S.pollMs = Math.max(2000, (S.config.varac.poll_interval_seconds || 10) * 1000); tiles.options.maxZoom = S.config.tiles.max_zoom || 17; map.setMaxZoom(tiles.options.maxZoom); if (legend._render) legend._render(); } catch (e) { /* defaults */ }
    await refreshHealth();
    await refreshStations(true);
    setInterval(refreshHealth, 5000);
    setInterval(() => refreshStations(false), S.pollMs);
    setInterval(rerenderAges, 30000);
    setInterval(() => { refreshBeaconPane(); refreshTilesPane(); refreshSettingsInfo(); refreshAprsPane(); }, 3000);
    setInterval(refreshBroadcasts, 15000);
    setInterval(refreshOwnTrack, 60000);
    if (S.health && !S.health.poller.connected && !(S.config && S.config.varac.db_path) && S.health.poller.last_error) openSettings("varac");
  }
  boot();
})();
