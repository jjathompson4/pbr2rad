/* pbr2rad web frontend — vanilla JS */

const CHANNELS = [
  "albedo", "roughness", "metalness",
  "normal_gl", "normal_dx", "normal",
  "displacement", "ao", "(skip)"
];

let uploadedFiles = [];
let selectedPHSlug = null;
let selectedPHRes = "1k";

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// On first load, populate the Poly Haven grid with the first page of the
// catalog (empty-string search returns the cached first 30) so the user
// sees something instead of a blank panel.
window.addEventListener("DOMContentLoaded", () => {
  searchPolyHaven("");
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
    setStatus("Discovery failed: " + err.message, "error");
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
    const resp = await fetch("/api/v1/convert/upload", { method: "POST", body: form });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Conversion failed");
    }
    const data = await resp.json();
    showResult(data);
  } catch (err) {
    setStatus("Error: " + err.message, "error");
  }

  document.getElementById("convert-btn").disabled = false;
});

// ---------------------------------------------------------------------------
// Poly Haven
// ---------------------------------------------------------------------------

let phSearchTimeout = null;
document.getElementById("ph-search").addEventListener("input", e => {
  clearTimeout(phSearchTimeout);
  phSearchTimeout = setTimeout(() => searchPolyHaven(e.target.value), 400);
});

async function searchPolyHaven(q) {
  const grid = document.getElementById("ph-grid");
  grid.innerHTML = "<p style='color:#999;font-size:0.85rem'>Searching...</p>";

  try {
    const resp = await fetch(`/api/v1/polyhaven/search?q=${encodeURIComponent(q)}`);
    const results = await resp.json();
    grid.innerHTML = "";

    results.forEach(item => {
      const div = document.createElement("div");
      div.className = "ph-item";
      div.innerHTML = `<img src="${item.preview}" alt="${item.name}" loading="lazy"><div class="ph-item-name">${item.name}</div>`;
      div.addEventListener("click", () => selectPHItem(item.slug, item.name));
      grid.appendChild(div);
    });

    if (!results.length) grid.innerHTML = "<p style='color:#999;font-size:0.85rem'>No results</p>";
  } catch (err) {
    grid.innerHTML = "<p style='color:#c62828;font-size:0.85rem'>Search failed</p>";
  }
}

async function selectPHItem(slug, name) {
  selectedPHSlug = slug;

  // Highlight
  document.querySelectorAll(".ph-item").forEach(el => el.classList.remove("selected"));
  event.currentTarget.closest(".ph-item").classList.add("selected");

  // Show detail
  const detail = document.getElementById("ph-detail");
  document.getElementById("ph-detail-name").textContent = name;
  detail.style.display = "block";

  // Reset per-map rotation state whenever a new asset is selected.
  phRotatePerMap = {};

  // Fetch resolutions + per-map thumbnails
  try {
    const resp = await fetch(`/api/v1/polyhaven/${slug}/info`);
    const info = await resp.json();
    const picker = document.getElementById("ph-res-picker");
    picker.innerHTML = "";
    (info.resolutions || ["1k", "2k", "4k"]).forEach(res => {
      const btn = document.createElement("button");
      btn.className = "res-btn" + (res === selectedPHRes ? " active" : "");
      btn.textContent = res;
      btn.addEventListener("click", () => {
        selectedPHRes = res;
        picker.querySelectorAll(".res-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
      });
      picker.appendChild(btn);
    });
    renderPHMapsGrid(info.maps || []);
  } catch (err) {
    // Fall back to default resolutions
  }
}

// ---------------------------------------------------------------------------
// Per-map rotation thumbnails (Poly Haven panel)
// ---------------------------------------------------------------------------
let phRotatePerMap = {};

function renderPHMapsGrid(maps) {
  const grid = document.getElementById("ph-maps-grid");
  grid.innerHTML = "";
  // Stable display order; polyhaven may return them in any order.
  const ORDER = ["albedo", "normal_gl", "normal_dx", "roughness", "metalness", "ao", "displacement"];
  const sorted = maps.slice().sort(
    (a, b) => ORDER.indexOf(a.channel) - ORDER.indexOf(b.channel)
  );
  for (const m of sorted) {
    const tile = document.createElement("div");
    tile.className = "map-thumb";
    tile.dataset.channel = m.channel;
    tile.dataset.rot = "0";
    tile.innerHTML = `
      <div class="map-thumb-img-wrap" title="Click to rotate 90° CCW">
        <img class="map-thumb-img" src="${m.thumbnail_url}" alt="${m.channel}" loading="lazy">
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
    else if (ch === "normal_gl" || ch === "normal_dx") used = useNormal;
    tile.dataset.used = used ? "true" : "false";
    const badge = tile.querySelector(".map-thumb-usage-badge");
    if (badge) badge.textContent = used ? "USED" : "IGNORED";
  });
}

// Recompute usage when relevant options toggle.
document.getElementById("opt-normal").addEventListener("change", updateMapUsage);

document.getElementById("ph-convert-btn").addEventListener("click", async () => {
  if (!selectedPHSlug) return;
  setStatus("Fetching from Poly Haven and converting...");
  document.getElementById("ph-convert-btn").disabled = true;

  try {
    const resp = await fetch("/api/v1/convert/polyhaven", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        slug: selectedPHSlug,
        resolution: selectedPHRes,
        fmt: "png",
        options: getOptions(),
      }),
    });

    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Conversion failed");
    }
    const data = await resp.json();
    showResult(data);
  } catch (err) {
    setStatus("Error: " + err.message, "error");
  }

  document.getElementById("ph-convert-btn").disabled = false;
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
  const dark = document.documentElement.dataset.theme === "dark";
  themeBtn.textContent = dark ? "☀️" : "\u{1F319}";
  themeBtn.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
}

themeBtn.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
  syncThemeButton();
});

syncThemeButton();
