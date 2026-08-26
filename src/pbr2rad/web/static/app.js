/* pbr2rad web frontend — vanilla JS */

const CHANNELS = [
  "albedo", "roughness", "metalness",
  "normal_gl", "normal_dx", "normal",
  "displacement", "ao", "(skip)"
];

let uploadedFiles = [];

// Conversions can legitimately take a couple of minutes on the small host;
// past this we assume the request is lost and give the button back.
const CONVERT_TIMEOUT_MS = 240000;

// Build a readable message from any error response. Cloudflare 429/5xx
// bodies are HTML, so never assume JSON; our own errors carry {detail}.
async function apiError(resp) {
  let detail = "";
  const ctype = resp.headers.get("Content-Type") || "";
  if (ctype.includes("application/json")) {
    try { detail = (await resp.json()).detail || ""; } catch (e) { /* not JSON after all */ }
  }
  if (detail) return detail;
  const retry = resp.headers.get("Retry-After");
  if (resp.status === 429) {
    return "Rate limited. Try again in " + (retry ? "~" + retry + "s" : "a few seconds") + ".";
  }
  if (resp.status === 503) return "Server busy. Try again in a few seconds.";
  return "Request failed (HTTP " + resp.status + ")";
}

// Normalize thrown errors (incl. AbortSignal.timeout) for display.
function errMessage(err) {
  if (err && err.name === "TimeoutError") {
    return "Timed out after " + Math.round(CONVERT_TIMEOUT_MS / 60000) +
      " minutes. The server may be overloaded. Please try again.";
  }
  return err && err.message ? err.message : String(err);
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

// One toggle bar drives both the visible panel (data-tab: browse / upload)
// and, for the two catalog buttons, which source feeds the Browse panel
// (data-source: polyhaven / ambientcg).
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => {
      b.classList.remove("active");
      b.setAttribute("aria-selected", "false");
    });
    document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    btn.setAttribute("aria-selected", "true");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.source && btn.dataset.source !== selectedSource) {
      selectSource(btn.dataset.source);
    }
  });
});

// On first load, populate the Browse grid with the first page of the
// catalog (empty-string search returns the cached first 30) so the user
// sees something instead of a blank panel.
window.addEventListener("DOMContentLoaded", () => {
  searchCatalog("");
});

// Conversion settings (Maps & Projection tab): a change applies to the next
// conversion and, when a result is showing, re-renders it. (The preview
// sphere always shows the source-style wrap — see the note under the
// previews — so projection edits show up in the export, not the sphere.)
document.getElementById("advanced-options").addEventListener("change", () => {
  if (currentJob) scheduleRerender();
});

// Planar axis only means something for planar projection
document.getElementById("opt-projection").addEventListener("change", e => {
  document.getElementById("opt-planar-axis").disabled = e.target.value !== "planar";
});

// The Bump slider (Override Properties) only applies when the normal map is used
document.getElementById("opt-normal").addEventListener("change", syncBumpEnabled);

// Tooltips: one fixed-position bubble for every [data-tip] (hover or focus),
// positioned in JS so the scrolling tab pane can't clip it.
const tipEl = document.createElement("div");
tipEl.id = "tooltip";
tipEl.setAttribute("role", "tooltip");
document.body.appendChild(tipEl);
function showTip(el) {
  tipEl.textContent = el.dataset.tip;
  tipEl.style.left = "0px";
  tipEl.style.top = "0px";
  tipEl.classList.add("show");
  const r = el.getBoundingClientRect();
  const tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
  const x = Math.max(8, Math.min(r.left + r.width / 2 - tw / 2, innerWidth - tw - 8));
  let y = r.top - th - 8;
  if (y < 8) y = r.bottom + 8;
  tipEl.style.left = x + "px";
  tipEl.style.top = y + "px";
}
function hideTip() { tipEl.classList.remove("show"); }
const tipTarget = e => (e.target && e.target.closest) ? e.target.closest("[data-tip]") : null;
document.addEventListener("mouseover", e => { const t = tipTarget(e); if (t) showTip(t); });
document.addEventListener("mouseout", e => { const t = tipTarget(e); if (t && !t.contains(e.relatedTarget)) hideTip(); });
document.addEventListener("focusin", e => { const t = tipTarget(e); if (t) showTip(t); });
document.addEventListener("focusout", hideTip);
document.addEventListener("scroll", hideTip, true);

// Output tabs
document.querySelectorAll(".out-tab-btn").forEach(btn => {
  btn.addEventListener("click", () => selectOutTab(btn.dataset.outTab));
});
function selectOutTab(name) {
  document.querySelectorAll(".out-tab-btn").forEach(b => {
    const on = b.dataset.outTab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".out-pane").forEach(p => p.classList.toggle("active", p.id === "out-tab-" + name));
}

// ---------------------------------------------------------------------------
// File upload + drag-and-drop
// ---------------------------------------------------------------------------

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", e => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", e => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  handleFiles(e.dataTransfer.files);
});
fileInput.addEventListener("change", e => handleFiles(e.target.files));

async function handleFiles(fileList) {
  uploadedFiles = Array.from(fileList);
  if (!uploadedFiles.length) return;

  setStatus("Detecting channels...");

  // Auto-discover channels
  const form = new FormData();
  uploadedFiles.forEach(f => form.append("files", f));

  try {
    const resp = await fetch("/api/v1/discover", { method: "POST", body: form });
    if (!resp.ok) throw new Error(await apiError(resp));
    const data = await resp.json();

    // Populate channel table
    const tbody = document.querySelector("#channel-table tbody");
    tbody.innerHTML = "";
    data.channels.forEach(ch => {
      const tr = document.createElement("tr");
      const options = CHANNELS.map(c =>
        `<option value="${c}" ${c === ch.channel ? "selected" : ""}>${c}</option>`
      ).join("");
      tr.innerHTML = `<td>${ch.filename}</td><td><select>${options}</select></td>`;
      tbody.appendChild(tr);
    });
    document.getElementById("channel-table").style.display = "table";
    document.getElementById("convert-btn").disabled = false;

    if (!document.getElementById("material-name").value) {
      document.getElementById("material-name").value = data.name;
    }

    setStatus("Channels detected. Review and click Convert.", "success");
  } catch (err) {
    setStatus("Discovery failed: " + errMessage(err), "error");
  }
}

// ---------------------------------------------------------------------------
// Convert (upload)
// ---------------------------------------------------------------------------

document.getElementById("convert-btn").addEventListener("click", async () => {
  if (!uploadedFiles.length) return;
  setStatus("Converting...");
  document.getElementById("convert-btn").disabled = true;

  const form = new FormData();
  uploadedFiles.forEach(f => form.append("files", f));

  // Collect channel labels from table
  const rows = document.querySelectorAll("#channel-table tbody tr");
  const channels = [];
  rows.forEach(row => {
    const filename = row.cells[0].textContent;
    const channel = row.querySelector("select").value;
    if (channel !== "(skip)") {
      channels.push({ filename, channel });
    }
  });

  form.append("channels", JSON.stringify(channels));
  form.append("options", JSON.stringify(getOptions()));
  form.append("name", document.getElementById("material-name").value || "material");

  try {
    const resp = await fetch("/api/v1/convert/upload", {
      method: "POST", body: form,
      signal: AbortSignal.timeout(CONVERT_TIMEOUT_MS),
    });
    if (!resp.ok) throw new Error(await apiError(resp));
    const data = await resp.json();
    showResult(data, null);
  } catch (err) {
    setStatus("Error: " + errMessage(err), "error");
  } finally {
    document.getElementById("convert-btn").disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Browse — catalog search across texture sources (Poly Haven, ambientCG)
// ---------------------------------------------------------------------------

// Per-source UI facts. `fmt` is what we ask the server to fetch when the
// asset info doesn't say otherwise: ambientCG's JPG packs are about half
// the size of PNG and plenty for .hdr/.dat emission.
const SOURCES = {
  polyhaven: { label: "Poly Haven", fmt: "png", placeholder: "Search Poly Haven textures..." },
  ambientcg: { label: "ambientCG", fmt: "jpg", placeholder: "Search ambientCG materials..." },
};
// ambientCG is the landing source (first toggle button); keep in sync with
// the markup's active button.
let selectedSource = "ambientcg";
let selectedAssetId = null;
let selectedItem = null;       // the catalog result the hero is showing
let selectedRes = "1k";
let selectedFmt = SOURCES.ambientcg.fmt;

// Called by the Reference Material toggle when a catalog button is picked.
function selectSource(key) {
  selectedSource = key;
  selectedFmt = SOURCES[key].fmt;
  // An asset id means nothing across sources — drop the selection and any
  // per-map rotations before the new grid comes in.
  selectedAssetId = null;
  selectedItem = null;
  phRotatePerMap = {};
  document.getElementById("ph-detail").style.display = "none";
  document.getElementById("ph-maps-grid").innerHTML = "";
  document.getElementById("ph-res-picker").innerHTML = "";
  const search = document.getElementById("ph-search");
  search.placeholder = SOURCES[key].placeholder;
  searchCatalog(search.value);
}

// Debounced above typical inter-keystroke gaps so a deliberate typist fires
// one request per pause, not one per key. In-flight searches are aborted
// when superseded.
let phSearchTimeout = null;
let phSearchAbort = null;
document.getElementById("ph-search").addEventListener("input", e => {
  clearTimeout(phSearchTimeout);
  phSearchTimeout = setTimeout(() => searchCatalog(e.target.value), 700);
});

// Last grid contents, kept so a theme switch can re-pick thumbnails.
let lastResults = [];

// Sources that publish a dark-background thumbnail variant (ambientCG) get
// it in dark mode; everything else falls back to the default preview.
function thumbFor(item) {
  const dark = document.documentElement.dataset.theme === "dark";
  return (dark && item.preview_dark) || item.preview || "";
}

// Large variant for the selected-asset hero, same theme rule.
function heroFor(item) {
  const dark = document.documentElement.dataset.theme === "dark";
  return (dark && (item.preview_large_dark || item.preview_dark))
    || item.preview_large || item.preview || "";
}

function renderCatalogGrid(results) {
  const grid = document.getElementById("ph-grid");
  grid.innerHTML = "";
  results.forEach(item => {
    const div = document.createElement("div");
    div.className = "ph-item";
    if (item.id === selectedAssetId) div.classList.add("selected");
    div.innerHTML = `<img src="${thumbFor(item)}" alt="${item.name}" loading="lazy"><div class="ph-item-name">${item.name}</div>`;
    div.addEventListener("click", () => selectAsset(item, div));
    grid.appendChild(div);
  });
  if (!results.length) grid.innerHTML = "<p style='color:#999;font-size:0.85rem'>No results</p>";
}

async function searchCatalog(q) {
  const grid = document.getElementById("ph-grid");
  const source = selectedSource;
  grid.innerHTML = "<p style='color:#999;font-size:0.85rem'>Searching...</p>";

  if (phSearchAbort) phSearchAbort.abort();
  phSearchAbort = new AbortController();

  try {
    const resp = await fetch(`/api/v1/sources/${source}/search?q=${encodeURIComponent(q)}`,
      { signal: phSearchAbort.signal });
    if (!resp.ok) throw new Error(await apiError(resp));
    const results = await resp.json();
    if (source !== selectedSource) return; // user switched source mid-flight
    lastResults = results;
    renderCatalogGrid(results);
  } catch (err) {
    if (err && err.name === "AbortError") return; // superseded by a newer search
    grid.innerHTML = `<p style='color:#c62828;font-size:0.85rem'>Search failed: ${errMessage(err)}</p>`;
  }
}

function addResButton(picker, res) {
  const btn = document.createElement("button");
  btn.className = "res-btn" + (res === selectedRes ? " active" : "");
  btn.textContent = res;
  btn.addEventListener("click", () => {
    selectedRes = res;
    picker.querySelectorAll(".res-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
  });
  picker.appendChild(btn);
}

let phInfoAbort = null;

// Fill the hero block from a catalog result (instant) and, later, /info.
function renderHero(item, info) {
  const img = document.getElementById("ph-detail-preview");
  const src = heroFor(info ? {
    preview: info.preview_url, preview_large: info.preview_large_url,
    preview_large_dark: info.preview_large_dark_url,
    preview_dark: item && item.preview_dark,
  } : item);
  if (src && img.getAttribute("src") !== src) img.src = src;
  img.alt = item ? item.name : "";

  const sub = document.getElementById("ph-detail-sub");
  const tags = document.getElementById("ph-detail-tags");
  const link = document.getElementById("ph-detail-link");
  const label = SOURCES[selectedSource] ? SOURCES[selectedSource].label : selectedSource;
  if (!info) {
    sub.textContent = label;
    tags.textContent = "";
    link.style.display = "none";
    return;
  }
  const bits = [label];
  if (info.dimensions_cm && info.dimensions_cm.length === 2) {
    const [w, h] = info.dimensions_cm;
    bits.push(`${fmtCm(w)} × ${fmtCm(h)} cm`);
  }
  if (info.resolutions && info.resolutions.length) bits.push("up to " + info.resolutions[info.resolutions.length - 1]);
  sub.textContent = bits.join(" · ");
  tags.textContent = (info.categories || []).slice(0, 12).join(", ");
  if (info.asset_url) {
    link.href = info.asset_url;
    link.textContent = `View on ${label} ↗`;
    link.style.display = "inline";
  } else {
    link.style.display = "none";
  }
}

function fmtCm(v) {
  return Number.isInteger(v) ? String(v) : Number(v).toFixed(1).replace(/\.0$/, "");
}

async function selectAsset(item, el) {
  const id = item.id;
  selectedAssetId = id;
  selectedItem = item;
  const source = selectedSource;

  // Highlight
  document.querySelectorAll(".ph-item").forEach(x => x.classList.remove("selected"));
  el.classList.add("selected");

  // Show detail; clear stale state from the previously selected asset so a
  // failed info fetch can't leave the old asset's maps under the new name.
  const detail = document.getElementById("ph-detail");
  document.getElementById("ph-detail-name").textContent = item.name;
  detail.style.display = "block";
  renderHero(item, null);
  phRotatePerMap = {};
  const picker = document.getElementById("ph-res-picker");
  picker.innerHTML = "";
  const mapsGrid = document.getElementById("ph-maps-grid");
  mapsGrid.innerHTML = "<p class='maps-loading'>Loading maps…</p>";
  // Bring the selection into focus when the detail sits off-screen (narrow
  // layouts stack the panels, so the hero can land below the fold).
  requestAnimationFrame(() => {
    const r = detail.getBoundingClientRect();
    if (r.top < 0 || r.bottom > window.innerHeight) {
      detail.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });

  // Fetch resolutions + per-map thumbnails (aborting any in-flight fetch
  // from a rapid previous click). For ambientCG the first call for an asset
  // also downloads its 1K pack to build the map thumbnails (~1–3 s).
  if (phInfoAbort) phInfoAbort.abort();
  phInfoAbort = new AbortController();
  try {
    const resp = await fetch(`/api/v1/sources/${source}/${encodeURIComponent(id)}/info`,
      { signal: phInfoAbort.signal });
    if (!resp.ok) throw new Error(await apiError(resp));
    const info = await resp.json();
    if (source !== selectedSource || id !== selectedAssetId) return; // superseded
    let resolutions = info.resolutions || [];
    if (!resolutions.length) resolutions = ["1k", "2k"];
    resolutions.forEach(res => addResButton(picker, res));
    // Keep the selection valid if this asset lacks the previously chosen res.
    if (!resolutions.includes(selectedRes)) {
      selectedRes = resolutions[0];
      picker.querySelector(".res-btn").classList.add("active");
    }
    selectedFmt = info.default_fmt || SOURCES[source].fmt;
    selectedInfo = info;
    renderHero(item, info);
    renderPHMapsGrid(info.maps || []);
  } catch (err) {
    if (err && err.name === "AbortError") return; // superseded by a newer click
    mapsGrid.innerHTML = "";
    // Conversion still works without the details — offer the standard picks.
    ["1k", "2k"].forEach(res => addResButton(picker, res));
    setStatus("Couldn't load asset details: " + errMessage(err), "error");
  }
}
let selectedInfo = null;

// ---------------------------------------------------------------------------
// Per-map rotation thumbnails (Browse panel)
// ---------------------------------------------------------------------------
let phRotatePerMap = {};

// Short labels for sources that publish no per-map images.
const MAP_ABBREV = {
  albedo: "COL", normal_gl: "NRM", normal_dx: "NRM", normal: "NRM",
  roughness: "RGH", metalness: "MTL", ao: "AO", displacement: "DSP", arm: "ARM",
};

function renderPHMapsGrid(maps) {
  const grid = document.getElementById("ph-maps-grid");
  grid.innerHTML = "";
  // Stable display order; sources may return them in any order. Unknown
  // channels go last rather than first (indexOf would give them -1).
  const ORDER = ["albedo", "normal_gl", "normal_dx", "normal", "roughness", "metalness", "ao", "displacement", "arm"];
  const rank = ch => { const i = ORDER.indexOf(ch); return i < 0 ? ORDER.length : i; };
  const sorted = maps.slice().sort((a, b) => rank(a.channel) - rank(b.channel));
  for (const m of sorted) {
    const tile = document.createElement("div");
    tile.className = "map-thumb";
    tile.dataset.channel = m.channel;
    tile.dataset.rot = "0";
    const visual = m.thumbnail_url
      ? `<img class="map-thumb-img" src="${m.thumbnail_url}" alt="${m.channel}" loading="lazy">`
      : `<div class="map-thumb-img map-thumb-placeholder">${MAP_ABBREV[m.channel] || m.channel}</div>`;
    tile.innerHTML = `
      <div class="map-thumb-img-wrap" title="Click to rotate 90° CCW">
        ${visual}
        <span class="map-thumb-rot-badge">0°</span>
        <span class="map-thumb-usage-badge"></span>
      </div>
      <div class="map-thumb-label">${m.channel.replace("_", " ")}</div>
    `;
    tile.querySelector(".map-thumb-img-wrap").addEventListener("click", () => {
      const cur = parseInt(tile.dataset.rot, 10) || 0;
      const next = (cur + 90) % 360;
      tile.dataset.rot = String(next);
      tile.querySelector(".map-thumb-rot-badge").textContent = next + "°";
      tile.querySelector(".map-thumb-img").style.transform = `rotate(-${next}deg)`;
      if (next === 0) delete phRotatePerMap[m.channel];
      else phRotatePerMap[m.channel] = next;
    });
    grid.appendChild(tile);
  }
  updateMapUsage();
}

// Compute which channels the conversion will consume given the current
// option toggles, then mark each tile in the maps grid as used/ignored.
// Channels currently consumed by pbr2rad:
//   albedo     - always (required)
//   roughness  - whenever a roughness map is present (used as scalar mean
//                AND for varying-roughness if that option is on)
//   metalness  - whenever a metalness map is present
//   normal_gl/normal_dx - whenever the "Use normal map" option is on and
//                a normal map is present
//   ao, displacement - currently unused by the conversion
function updateMapUsage() {
  const useNormal = document.getElementById("opt-normal").checked;
  document.querySelectorAll(".map-thumb").forEach(tile => {
    const ch = tile.dataset.channel;
    let used = false;
    if (ch === "albedo") used = true;
    else if (ch === "roughness") used = true;
    else if (ch === "metalness") used = true;
    else if (ch === "normal_gl" || ch === "normal_dx" || ch === "normal") used = useNormal;
    tile.dataset.used = used ? "true" : "false";
    const badge = tile.querySelector(".map-thumb-usage-badge");
    if (badge) badge.textContent = used ? "USED" : "IGNORED";
  });
}

// Recompute usage when relevant options toggle.
document.getElementById("opt-normal").addEventListener("change", updateMapUsage);

document.getElementById("ph-convert-btn").addEventListener("click", async () => {
  if (!selectedAssetId) return;
  const source = selectedSource;
  setStatus(`Fetching from ${SOURCES[source].label} and converting...`);
  document.getElementById("ph-convert-btn").disabled = true;

  try {
    const resp = await fetch(`/api/v1/sources/${source}/convert`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: AbortSignal.timeout(CONVERT_TIMEOUT_MS),
      body: JSON.stringify({
        asset_id: selectedAssetId,
        resolution: selectedRes,
        fmt: selectedFmt,
        options: getOptions(),
      }),
    });

    if (!resp.ok) throw new Error(await apiError(resp));
    const data = await resp.json();
    showResult(data, selectedItem);
  } catch (err) {
    setStatus("Error: " + errMessage(err), "error");
  } finally {
    document.getElementById("ph-convert-btn").disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Options helper
// ---------------------------------------------------------------------------

function getOptions() {
  const opts = {
    projection: document.getElementById("opt-projection").value,
    planar_axis: document.getElementById("opt-planar-axis").value,
    u_scale: parseFloat(document.getElementById("opt-u-scale").value) || 1,
    v_scale: parseFloat(document.getElementById("opt-v-scale").value) || 1,
    u_offset: parseFloat(document.getElementById("opt-u-offset").value) || 0,
    v_offset: parseFloat(document.getElementById("opt-v-offset").value) || 0,
    normal: document.getElementById("opt-normal").checked,
    bump_scale: sliderVal("tune-bump"),   // Bump slider, Override Properties tab
    varying_roughness: document.getElementById("opt-varying-rough").checked,
    rotate_per_map: { ...phRotatePerMap },
    flip_h: document.getElementById("opt-flip-h").checked,
    flip_v: document.getElementById("opt-flip-v").checked,
  };

  // Specularity / roughness / metalness / diffuse are tuned after conversion
  // in the Output panel (see rerender()).
  return opts;
}

// ---------------------------------------------------------------------------
// Output display
// ---------------------------------------------------------------------------

function setStatus(msg, type = "") {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = "status " + type;
}

// The last conversion (for the tune sliders) and the catalog item it came
// from (for the side-by-side reference render).
let currentJob = null;      // { id, data, base }
let currentRefItem = null;

const PHOTOPIC = [0.265, 0.670, 0.065];
function visible(rgb) { return PHOTOPIC[0] * rgb[0] + PHOTOPIC[1] * rgb[1] + PHOTOPIC[2] * rgb[2]; }
function pct(v) { return (v * 100).toFixed(1).replace(/\.0$/, "") + "%"; }
function fmt3(v) { return Number(v).toFixed(3); }

function showResult(data, refItem, opts = {}) {
  setStatus(opts.statusText || "Conversion complete!", "success");
  const base = currentJob && currentJob.base;
  currentJob = { id: data.job_id, data, base };
  if (refItem !== undefined) currentRefItem = refItem;

  // Preview (+ reference render when the material came from the Browse panel)
  const previewImg = document.getElementById("preview-img");
  const cap = document.getElementById("preview-cap");
  const pair = document.getElementById("preview-pair");
  if (data.preview_url) {
    previewImg.src = data.preview_url;
    previewImg.classList.add("visible");
    cap.style.display = "block";
    pair.classList.add("has-preview");
  } else {
    previewImg.classList.remove("visible");
    cap.style.display = "none";
    pair.classList.remove("has-preview");
  }
  const refFig = document.getElementById("preview-ref-fig");
  const refImg = document.getElementById("preview-ref-img");
  if (currentRefItem && heroFor(currentRefItem) && data.preview_url) {
    refImg.src = heroFor(currentRefItem);
    const label = (SOURCES[data.source] && SOURCES[data.source].label) || "Source";
    document.getElementById("preview-ref-cap").textContent = label + " render";
    refFig.style.display = "flex";
  } else {
    refFig.style.display = "none";
  }

  // Summary tab — what the converted material IS (Radiance semantics). Name,
  // source and physical size already live on the reference card.
  const dl = document.getElementById("result-summary");
  const hex = data.avg_srgb_hex || "#888888";
  const refl = data.reflectance || null;
  const alpha = data.roughness_radiance != null ? data.roughness_radiance : Math.pow(data.roughness, 2);
  let rows = "";
  if (refl) {
    rows += `
    <dt class="section">Reflectance (visible, photopic)</dt>
    <dt>VLR total</dt><dd><strong>${pct(refl.total_vis)}</strong><span class="muted"> · diffuse ${pct(refl.diffuse_vis)} · specular ${pct(refl.specular_vis)}</span></dd>
    <dt>Diffuse RGB</dt><dd><span class="color-swatch" style="background:${hex}"></span>${refl.diffuse_rgb.map(fmt3).join(", ")}<span class="muted"> · ${hex}</span></dd>
    <dt>Specular RGB</dt><dd>${refl.specular_rgb.map(fmt3).join(", ")}</dd>
    `;
  }
  rows += `
    <dt class="section">Radiance primitive</dt>
    <dt>Type</dt><dd>${data.primitive}<span class="muted"> · metalness ${fmt3(data.metalness)}</span></dd>
    <dt>Roughness α</dt><dd>${fmt3(alpha)}<span class="muted"> (perceptual ${fmt3(data.roughness)})</span></dd>
  `;
  const used = (data.channels_used || []).slice();
  const est = data.channels_estimated || [];
  const mapsText = used.length
    ? used.join(" · ") + (est.length ? `<span class="muted"> · estimated: ${est.join(", ")}</span>` : "")
    : "—";
  rows += `
    <dt class="section">Texture</dt>
    <dt>Resolution</dt><dd>${data.resolution[0]} × ${data.resolution[1]} px</dd>
    <dt>Maps used</dt><dd>${mapsText}</dd>
  `;
  if (data.diffuse_scale && Math.abs(data.diffuse_scale - 1) > 1e-6) {
    rows += `<dt>Albedo ×</dt><dd>${fmt3(data.diffuse_scale)}</dd>`;
  }
  dl.innerHTML = rows;
  dl.style.display = "grid";
  document.getElementById("summary-empty").style.display = "none";

  // Override Properties tab — sliders reflect the effective values of this render
  const spec = data.specularity != null ? data.specularity : 0.05;
  if (!opts.keepSliders) {
    setSlider("tune-metal", data.metalness);
    setSlider("tune-spec", spec);
    setSlider("tune-rough", data.roughness);
    setSlider("tune-diff", data.diffuse_scale != null ? data.diffuse_scale : 1.0);
    specTouched = false;
    currentJob.base = { roughness: data.roughness, metalness: data.metalness };
  } else {
    // Server re-seeds specularity when the primitive flips; follow it unless
    // the user has taken over the slider.
    if (!specTouched) setSlider("tune-spec", spec);
    if (data.metalness != null) setSlider("tune-metal", data.metalness);
  }
  updateDerived(data);
  const tune = document.getElementById("tune-panel");
  tune.style.display = "block";
  tune.classList.remove("busy");
  document.getElementById("tune-empty").style.display = "none";
  document.getElementById("tune-note").style.display = "block";

  // Download (pinned at the bottom of the panel)
  const btn = document.getElementById("download-btn");
  btn.href = data.download_url;
  btn.style.display = "block";
  btn.textContent = "Download " + data.name + ".zip";
}

// ---------------------------------------------------------------------------
// Override Properties — re-render the current job with overrides
// ---------------------------------------------------------------------------

// Specularity follows the primitive default (plastic 0.05 / metal 1.0) until
// the user moves its slider; then it is an explicit override.
let specTouched = false;

function sliderDecimals(id) { return (id === "tune-diff" || id === "tune-metal" || id === "tune-bump") ? 2 : 3; }
function sliderVal(id) { return parseFloat(document.getElementById(id).value); }

function setSlider(id, value) {
  const input = document.getElementById(id);
  input.value = value;
  document.getElementById(id + "-val").textContent = Number(value).toFixed(sliderDecimals(id));
}

// What each slider produces in the Radiance material. Server values (from the
// last render) win; while dragging we show the local prediction.
function updateDerived(data) {
  const metal = sliderVal("tune-metal"), spec = sliderVal("tune-spec"), rough = sliderVal("tune-rough"), diff = sliderVal("tune-diff");
  const primitive = metal >= 0.5 ? "metal" : "plastic";
  document.getElementById("tune-metal-derived").textContent = `→ ${primitive} primitive`;
  document.getElementById("tune-spec-derived").textContent = `→ ${pct(spec)} of light`;
  const alpha = (data && data.roughness_radiance != null && Math.abs(data.roughness - rough) < 1e-6) ? data.roughness_radiance : rough * rough;
  document.getElementById("tune-rough-derived").textContent = `→ Radiance α ${fmt3(alpha)}`;
  let dvis = null;
  if (data && data.reflectance && Math.abs((data.diffuse_scale || 1) - diff) < 1e-6 && Math.abs((data.specularity != null ? data.specularity : 0.05) - spec) < 1e-6) {
    dvis = data.reflectance.diffuse_vis;
  } else if (data && data.avg_rgb) {
    const c = data.avg_rgb.map(v => Math.min(1, v * diff));
    dvis = visible(c.map(v => v * (1 - spec)));
  }
  document.getElementById("tune-diff-derived").textContent = dvis != null ? `→ diffuse ${pct(dvis)} VLR` : "";
  syncBumpEnabled();
}

// Bump is a conversion option (texdata scale): enabled only while the normal
// map is in use and the material actually has one.
function syncBumpEnabled() {
  const hasNormal = !currentJob || (currentJob.data.channels_used || []).includes("normal");
  const on = document.getElementById("opt-normal").checked && hasNormal;
  const input = document.getElementById("tune-bump");
  input.disabled = !on;
  const why = !hasNormal ? "no normal map" : "normal map off";
  document.getElementById("tune-bump-derived").textContent = on ? `→ texdata × ${sliderVal("tune-bump").toFixed(2)}` : `→ ${why}`;
}

["tune-metal", "tune-spec", "tune-rough", "tune-diff", "tune-bump"].forEach(id => {
  const input = document.getElementById(id);
  input.addEventListener("input", () => {
    document.getElementById(id + "-val").textContent = Number(input.value).toFixed(sliderDecimals(id));
    if (currentJob) updateDerived(currentJob.data);
  });
  input.addEventListener("change", () => {
    if (id === "tune-spec") specTouched = true;
    scheduleRerender();
  });
});

document.getElementById("tune-reset").addEventListener("click", () => {
  if (!currentJob) return;
  // Back to the map-derived roughness/metalness, the primitive's default
  // specularity, and no albedo scaling.
  const base = currentJob.base || { roughness: currentJob.data.roughness, metalness: currentJob.data.metalness };
  specTouched = false;
  setSlider("tune-metal", base.metalness);
  setSlider("tune-rough", base.roughness);
  setSlider("tune-diff", 1.0);
  setSlider("tune-bump", 1.0);
  updateDerived(currentJob.data);
  scheduleRerender();
});

let rerenderTimer = null;
function scheduleRerender() {
  clearTimeout(rerenderTimer);
  rerenderTimer = setTimeout(rerender, 300);
}

async function rerender() {
  if (!currentJob) return;
  const tune = document.getElementById("tune-panel");
  tune.classList.add("busy");
  setStatus("Re-rendering with your overrides…");
  const body = {
    options: getOptions(),     // projection / maps / rotations as currently set
    metalness: sliderVal("tune-metal"),
    roughness: sliderVal("tune-rough"),
    diffuse_scale: sliderVal("tune-diff"),
  };
  if (specTouched) body.specularity = sliderVal("tune-spec");
  else body.reset_specularity = true;     // follow the primitive default
  const post = () => fetch(`/api/v1/jobs/${currentJob.id}/rerender`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal: AbortSignal.timeout(CONVERT_TIMEOUT_MS),
    body: JSON.stringify(body),
  });
  try {
    let resp = await post();
    if (resp.status === 503) {
      // One conversion at a time on the server; a slider move that lands
      // while the previous re-render is still running just waits its turn.
      const retry = parseInt(resp.headers.get("Retry-After") || "5", 10);
      setStatus(`Server busy — retrying in ${retry}s…`);
      await new Promise(r => setTimeout(r, retry * 1000));
      resp = await post();
    }
    if (!resp.ok) throw new Error(await apiError(resp));
    const data = await resp.json();
    showResult(data, undefined, { keepSliders: true, statusText: "Re-rendered with overrides." });
  } catch (err) {
    tune.classList.remove("busy");
    setStatus("Re-render failed: " + errMessage(err), "error");
  }
}

// ---------------------------------------------------------------------------
// Light / dark theme toggle (initial theme is set pre-paint in index.html)
// ---------------------------------------------------------------------------

const themeBtn = document.getElementById("theme-btn");

function syncThemeButton() {
  // Icon swap is pure CSS ([data-theme] show/hide); only the label lives here.
  const dark = document.documentElement.dataset.theme === "dark";
  themeBtn.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
}

themeBtn.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
  syncThemeButton();
  // Re-pick catalog thumbnails + hero + the Output reference tile for the new
  // theme (ambientCG publishes light/dark variants).
  if (lastResults.length) renderCatalogGrid(lastResults);
  if (selectedItem) renderHero(selectedItem, selectedInfo);
  if (currentRefItem && heroFor(currentRefItem)) {
    document.getElementById("preview-ref-img").src = heroFor(currentRefItem);
  }
});

syncThemeButton();

// ---------------------------------------------------------------------------
// About dialog (header)
// ---------------------------------------------------------------------------
const aboutDlg = document.getElementById("about-dialog");
document.getElementById("about-btn").addEventListener("click", () => aboutDlg.showModal());
document.getElementById("about-close").addEventListener("click", () => aboutDlg.close());
aboutDlg.addEventListener("click", e => { if (e.target === aboutDlg) aboutDlg.close(); });  // backdrop click
