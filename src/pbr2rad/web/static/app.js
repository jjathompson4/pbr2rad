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
    return "Rate limited — try again in " + (retry ? "~" + retry + "s" : "a few seconds") + ".";
  }
  if (resp.status === 503) return "Server busy — try again in a few seconds.";
  return "Request failed (HTTP " + resp.status + ")";
}

// Normalize thrown errors (incl. AbortSignal.timeout) for display.
function errMessage(err) {
  if (err && err.name === "TimeoutError") {
    return "Timed out after " + Math.round(CONVERT_TIMEOUT_MS / 60000) +
      " minutes. The server may be overloaded — please try again.";
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

// Settings-mode toggle: Default keeps the advanced panel hidden (and the
// defaults apply); Advanced reveals it. The toggle is purely UI — values
// in the advanced panel stay at their defaults until the user changes them.
document.querySelectorAll(".mode-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".mode-btn").forEach(b => {
      b.classList.remove("active");
      b.setAttribute("aria-selected", "false");
    });
    btn.classList.add("active");
    btn.setAttribute("aria-selected", "true");
    const showAdvanced = btn.dataset.mode === "advanced";
    document.getElementById("advanced-options").hidden = !showAdvanced;
  });
});

// Show/hide planar axis based on projection
document.getElementById("opt-projection").addEventListener("change", e => {
  document.getElementById("opt-axis-group").style.display =
    e.target.value === "planar" ? "block" : "none";
});

// Show/hide bump scale based on normal checkbox
document.getElementById("opt-normal").addEventListener("change", e => {
  document.getElementById("opt-bump-group").style.display =
    e.target.checked ? "block" : "none";
});

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
    showResult(data);
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
    showResult(data);
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
    bump_scale: parseFloat(document.getElementById("opt-bump-scale").value) || 1,
    varying_roughness: document.getElementById("opt-varying-rough").checked,
    rotate_per_map: { ...phRotatePerMap },
    flip_h: document.getElementById("opt-flip-h").checked,
    flip_v: document.getElementById("opt-flip-v").checked,
  };

  const roughVal = document.getElementById("opt-roughness").value;
  if (roughVal !== "") opts.roughness_override = parseFloat(roughVal);

  const metalVal = document.getElementById("opt-metalness").value;
  if (metalVal !== "") opts.metalness_override = parseFloat(metalVal);

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

function showResult(data) {
  setStatus("Conversion complete!", "success");

  // Preview
  const previewImg = document.getElementById("preview-img");
  if (data.preview_url) {
    previewImg.src = data.preview_url;
    previewImg.classList.add("visible");
  } else {
    previewImg.classList.remove("visible");
  }

  // Summary
  const dl = document.getElementById("result-summary");
  const [r, g, b] = data.avg_rgb.map(v => Math.round(Math.pow(v, 1/2.2) * 255));
  dl.innerHTML = `
    <dt>Material</dt><dd>${data.name}</dd>
    <dt>Primitive</dt><dd>${data.primitive}</dd>
    <dt>Roughness</dt><dd>${data.roughness.toFixed(3)}</dd>
    <dt>Metalness</dt><dd>${data.metalness.toFixed(3)}</dd>
    <dt>Resolution</dt><dd>${data.resolution[0]} x ${data.resolution[1]}</dd>
    <dt>Avg Color</dt><dd><span class="color-swatch" style="background:rgb(${r},${g},${b})"></span> ${data.avg_rgb.map(v => v.toFixed(3)).join(", ")}</dd>
  `;
  if (data.source && data.source_url) {
    const label = (SOURCES[data.source] && SOURCES[data.source].label) || data.source;
    dl.innerHTML += `<dt>Source</dt><dd><a href="${data.source_url}" target="_blank" rel="noopener">${label} ↗</a></dd>`;
  }
  dl.style.display = "block";

  // Download button
  const btn = document.getElementById("download-btn");
  btn.href = data.download_url;
  btn.style.display = "block";
  btn.textContent = "Download " + data.name + ".zip";
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
  // Re-pick catalog thumbnails + hero for the new theme (ambientCG has dark variants).
  if (lastResults.length) renderCatalogGrid(lastResults);
  if (selectedItem) renderHero(selectedItem, selectedInfo);
});

syncThemeButton();
