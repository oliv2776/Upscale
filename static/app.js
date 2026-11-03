const $ = (id) => document.getElementById(id);
const state = { models: {}, filter: "", page: 1, limit: 25, total: 0, previews: [], currentPreview: null };
let picker = { input: null, mode: "dir", path: "" };
let toastTimer, scanPolling = null, gpuPolling = null;

function toast(message) {
  const el = $("toast"); el.textContent = message; el.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("show"), 3600);
}
async function api(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || ("Erreur HTTP " + response.status));
  return data;
}
const post = (url, body = {}) => api(url, { method: "POST", body: JSON.stringify(body) });
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function formatDuration(seconds) {
  if (seconds == null || !isFinite(seconds)) return "";
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60);
  if (h) return h + " h " + String(m).padStart(2, "0");
  if (m) return m + " min";
  return Math.max(1, Math.round(seconds)) + " s";
}
function parseTime(text) {
  const parts = String(text).trim().split(":").map(Number);
  if (parts.some(isNaN)) return 0;
  return parts.reduce((total, part) => total * 60 + part, 0);
}
const modelLabel = id => id === "auto" ? "Automatique (Ollama)" : (state.models[id]?.label || id);
const codecLabels = { h264: "H.264", hevc: "HEVC 10 bits", av1: "AV1 10 bits", h264_vaapi: "H.264 VAAPI", hevc_vaapi: "HEVC VAAPI" };

// ------------------------------------------------------------------ réglages
function fillModelSelects() {
  const options = Object.entries(state.models).map(([id, m]) =>
    '<option value="' + id + '">' + escapeHtml(m.label) + " · ×" + m.scales.join("/×") + "</option>").join("");
  $("model").innerHTML = options + '<option value="auto">Automatique · Ollama (vision) choisit</option>';
  $("previewModels").innerHTML = Object.entries(state.models).map(([id, m]) =>
    '<label class="check"><input type="checkbox" value="' + id + '"' + (["realesrgan-plus", "realesr-animevideov3"].includes(id) ? " checked" : "") + "> " + escapeHtml(m.label) + "</label>").join("");
}
function settingsPayload() {
  return {
    sources: readSources(), destination: $("destination").value.trim(),
    output_suffix: $("outputSuffix").value, include_subfolders: $("recursive").checked,
    replace_original: $("replaceOriginal").checked, trash_days: Number($("trashDays").value),
    max_input_height: Number($("maxInput").value), output_height: Number($("outputHeight").value),
    model: $("model").value, deinterlace: $("deinterlace").value, denoise: $("denoise").value,
    interpolate: Number($("interpolate").value), codec: $("codec").value,
    crf: Number($("crf").value), preset: $("preset").value, profiles: readProfiles()
  };
}
function automationPayload() {
  const payload = {
    watch_enabled: $("watchEnabled").checked, watch_interval: Number($("watchInterval").value),
    schedule_enabled: $("scheduleEnabled").checked, schedule_start: $("scheduleStart").value || "22:00",
    schedule_end: $("scheduleEnd").value || "07:00",
    notify_type: $("notifyType").value, notify_url: $("notifyUrl").value.trim(),
    notify_on_complete: $("notifyComplete").checked, notify_on_failure: $("notifyFailure").checked,
    media_type: $("mediaType").value, media_url: $("mediaUrl").value.trim()
  };
  if ($("notifyToken").value) payload.notify_token = $("notifyToken").value;
  if ($("mediaToken").value) payload.media_token = $("mediaToken").value;
  return payload;
}
function ollamaPayload() {
  return { ollama_enabled: $("ollamaEnabled").checked, ollama_url: $("ollamaUrl").value.trim(), ollama_model: $("ollamaModel").value };
}
function setSelect(id, value) { if (value != null) $(id).value = String(value); }
function applySettings(s) {
  renderSources(s.sources && s.sources.length ? s.sources : [s.source || ""]); $("destination").value = s.destination;
  $("outputSuffix").value = s.output_suffix || ""; $("recursive").checked = s.include_subfolders;
  $("replaceOriginal").checked = !!s.replace_original; setSelect("trashDays", s.trash_days ?? 30); updateReplaceUi();
  setSelect("maxInput", s.max_input_height); setSelect("outputHeight", s.output_height);
  setSelect("model", s.model); setSelect("deinterlace", s.deinterlace || "auto"); setSelect("denoise", s.denoise || "off");
  setSelect("interpolate", s.interpolate || 1); setSelect("codec", s.codec || "hevc");
  $("crf").value = s.crf; $("crfValue").textContent = s.crf; setSelect("preset", s.preset);
  renderProfiles(s.profiles || []);
  $("watchEnabled").checked = !!s.watch_enabled; setSelect("watchInterval", s.watch_interval || 30);
  $("scheduleEnabled").checked = !!s.schedule_enabled; $("scheduleStart").value = s.schedule_start || "22:00"; $("scheduleEnd").value = s.schedule_end || "07:00";
  setSelect("notifyType", s.notify_type || ""); $("notifyUrl").value = s.notify_url || "";
  $("notifyComplete").checked = s.notify_on_complete !== false; $("notifyFailure").checked = s.notify_on_failure !== false;
  $("notifyToken").value = ""; $("notifyTokenHint").textContent = s.notify_token_set ? "Jeton enregistré (laisser vide pour le conserver)." : "";
  setSelect("mediaType", s.media_type || ""); $("mediaUrl").value = s.media_url || "";
  $("mediaToken").value = ""; $("mediaTokenHint").textContent = s.media_token_set ? "Jeton enregistré (laisser vide pour le conserver)." : "";
  $("ollamaEnabled").checked = !!s.ollama_enabled; $("ollamaUrl").value = s.ollama_url || "http://host.docker.internal:11434";
  const select = $("ollamaModel");
  if (s.ollama_model && ![...select.options].some(o => o.value === s.ollama_model)) {
    select.insertAdjacentHTML("beforeend", '<option value="' + escapeHtml(s.ollama_model) + '">' + escapeHtml(s.ollama_model) + "</option>");
  }
  select.value = s.ollama_model || "";
}
async function saveSettings(quiet = false) {
  const result = await post("/api/config", settingsPayload());
  applySettings(result.settings);
  if (!quiet) toast("Réglages enregistrés");
  return result.settings;
}

function updateReplaceUi() {
  const on = $("replaceOriginal").checked;
  $("replaceBox").classList.toggle("active", on);
  $("destination").disabled = on; document.querySelector('[data-picker="destination"]').disabled = on;
  $("destination").closest(".field").classList.toggle("disabled", on);
  $("trashDays").disabled = !on;
}

// ------------------------------------------------------------------ profils
function profileRow(p = {}) {
  const opt = (values, selected, empty) => '<option value="">' + empty + "</option>" + values.map(([v, l]) =>
    '<option value="' + v + '"' + (String(selected ?? "") === String(v) ? " selected" : "") + ">" + escapeHtml(l) + "</option>").join("");
  const models = [...Object.entries(state.models).map(([id, m]) => [id, m.label]), ["auto", "Automatique (Ollama)"]];
  const heights = [[480, "480p"], [720, "720p"], [1080, "1080p"], [1440, "1440p"], [2160, "2160p"]];
  const denoise = [["off", "Aucun"], ["light", "Léger"], ["medium", "Moyen"], ["strong", "Fort"]];
  const codecs = Object.entries(codecLabels);
  return '<tr><td><div class="path-control"><input class="p-path" value="' + escapeHtml(p.path || "") + '" placeholder="/data/videos/Animation"><button class="browse" data-picker="profile">…</button></div></td>' +
    '<td><select class="p-model">' + opt(models, p.model, "Global") + "</select></td>" +
    '<td><select class="p-height">' + opt(heights, p.output_height, "Global") + "</select></td>" +
    '<td><select class="p-denoise">' + opt(denoise, p.denoise, "Global") + "</select></td>" +
    '<td><select class="p-codec">' + opt(codecs, p.codec, "Global") + "</select></td>" +
    '<td><button class="text-btn danger" data-remove-profile>Retirer</button></td></tr>';
}
function renderProfiles(profiles) {
  $("profilesBody").innerHTML = profiles.length ? profiles.map(profileRow).join("") :
    '<tr class="empty-row"><td colspan="6" class="empty">Aucun profil : les réglages ci-dessus s\'appliquent à tous les fichiers.</td></tr>';
}
function readProfiles() {
  return [...$("profilesBody").querySelectorAll("tr")].filter(r => r.querySelector(".p-path")).map(row => {
    const profile = { path: row.querySelector(".p-path").value.trim() };
    const model = row.querySelector(".p-model").value, height = row.querySelector(".p-height").value;
    const denoise = row.querySelector(".p-denoise").value, codec = row.querySelector(".p-codec").value;
    if (model) profile.model = model; if (height) profile.output_height = Number(height);
    if (denoise) profile.denoise = denoise; if (codec) profile.codec = codec;
    return profile;
  }).filter(p => p.path);
}

// ------------------------------------------------------------------ analyse
function showScanResult(text, isError = false) {
  const el = $("scanResult"); el.classList.remove("hidden"); el.classList.toggle("error", isError); el.textContent = text;
}
function describeScan(s) {
  if (s.error) return s.error;
  if (s.running) return "Analyse en cours… " + (s.total ? s.checked + " / " + s.total + " fichier(s) vidéo examiné(s)" : "recherche des fichiers");
  let text = (s.origin === "watch" ? "Analyse automatique : " : "") + s.found + " vidéo(s) éligible(s) sur " + s.checked + " fichier(s) vidéo.";
  if (s.already_known) text += " " + s.already_known + " déjà en file ou traitée(s).";
  if (s.too_recent) text += " " + s.too_recent + " en cours de copie, ignorée(s) pour l'instant.";
  if (s.enqueue) {
    text += " " + (s.added || 0) + " ajoutée(s) à la file.";
    if (s.conflicts) text += " " + s.conflicts + " ignorée(s) : même fichier de sortie qu'une tâche en cours.";
  }
  return text;
}
async function startScan(enqueue) {
  if (scanPolling) { toast("Une analyse est déjà en cours"); return; }
  try {
    await post(enqueue ? "/api/queue" : "/api/scan", settingsPayload());
    showScanResult("Analyse en cours…"); pollScan();
  } catch (error) { toast(error.message); showScanResult(error.message, true); }
}
function pollScan() {
  if (scanPolling) return;
  scanPolling = setInterval(async () => {
    try {
      const s = await api("/api/scan");
      showScanResult(describeScan(s), !!s.error);
      if (!s.running) {
        clearInterval(scanPolling); scanPolling = null;
        if (s.error) toast(s.error);
        else if (s.enqueue) { toast((s.added || 0) + " tâche(s) ajoutée(s)"); await refreshJobs(); $("queue").scrollIntoView({ behavior: "smooth" }); }
        else toast(s.found + " vidéo(s) détectée(s)");
      }
    } catch (error) { clearInterval(scanPolling); scanPolling = null; showScanResult(error.message, true); }
  }, 1000);
}

// ------------------------------------------------------------------ file
function progressHtml(job) {
  if (job.status !== "running" || job.progress == null) {
    if (job.status === "queued" && job.progress) return '<div class="progress"><div class="progress-bar"><i style="width:' + (job.progress * 100).toFixed(1) + '%"></i></div><small>reprise à ' + (job.progress * 100).toFixed(0) + " %</small></div>";
    return "";
  }
  const pct = Math.max(0, Math.min(100, job.progress * 100));
  const eta = job.eta_seconds != null ? " · reste ~" + formatDuration(job.eta_seconds) : "";
  const seg = job.segments_total ? " · " + (job.segments_done || 0) + "/" + job.segments_total + " seg." : "";
  return '<div class="progress"><div class="progress-bar"><i style="width:' + pct.toFixed(1) + '%"></i></div><small>' + pct.toFixed(1) + " %" + seg + eta + "</small></div>";
}
function jobActions(job) {
  const b = (attr, label, cls = "") => '<button class="text-btn ' + cls + '" ' + attr + '="' + escapeHtml(job.id) + '">' + label + "</button>";
  const parts = [b("data-log", "Logs")];
  if (job.status === "queued") parts.push(b("data-move-top", "⤒", "icon"), b("data-move-up", "↑", "icon"), b("data-move-down", "↓", "icon"));
  if (["queued", "running"].includes(job.status)) parts.push(b("data-cancel", "Arrêter"));
  if (["failed", "cancelled"].includes(job.status)) parts.push(b("data-retry", "Relancer"));
  if (job.trash_path) parts.push(b("data-restore", "Restaurer l'original"));
  if (job.status !== "running") parts.push(b("data-delete", "Supprimer", "danger"));
  return parts.join("");
}
async function refreshJobs() {
  try {
    const query = "?limit=" + state.limit + "&page=" + state.page + (state.filter ? "&status=" + state.filter : "");
    const data = await api("/api/jobs" + query), counts = data.counts || {};
    $("queuedCount").textContent = counts.queued || 0; $("runningCount").textContent = counts.running || 0;
    $("completedCount").textContent = counts.completed || 0; $("failedCount").textContent = counts.failed || 0;
    $("queuedHint").textContent = data.deferred ? data.deferred : "Vidéos dans la file";
    $("queueInfo").textContent = data.deferred ? "⏸ " + data.deferred + " — les aperçus restent possibles." : "Suivez les tâches et consultez les journaux en cas de problème.";
    $("pauseBtn").classList.toggle("hidden", data.paused); $("resumeBtn").classList.toggle("hidden", !data.paused);
    state.total = data.total;
    const pages = Math.max(1, Math.ceil(data.total / state.limit));
    if (state.page > pages) { state.page = pages; return refreshJobs(); }
    $("pageInfo").textContent = "Page " + state.page + " / " + pages + " · " + data.total + " tâche(s)";
    $("prevPage").disabled = state.page <= 1; $("nextPage").disabled = state.page >= pages;
    const body = $("jobsBody");
    if (!data.jobs.length) { body.innerHTML = '<tr><td colspan="5" class="empty">Aucun traitement pour le moment.</td></tr>'; return; }
    const labels = { queued: "En attente", running: "En cours", completed: "Terminée", failed: "Erreur", cancelled: "Annulée" };
    body.innerHTML = data.jobs.map(job => {
      const name = job.source.split("/").pop();
      const conf = [modelLabel(job.model), job.output_height ? job.output_height + "p" : "", codecLabels[job.codec] || ""].filter(Boolean).join(" · ");
      const profile = job.profile ? '<br><small class="muted" title="' + escapeHtml(job.profile) + '">profil ' + escapeHtml(job.profile.split("/").pop()) + "</small>" : "";
      const trash = job.trash_path ? '<br><small class="muted" title="' + escapeHtml(job.trash_path) + '">original dans la corbeille</small>' : "";
      const warning = job.warning ? '<br><small class="warn" title="' + escapeHtml(job.warning) + '">⚠ ' + escapeHtml(job.warning.slice(0, 120)) + "</small>" : "";
      const err = job.error ? '<br><small title="' + escapeHtml(job.error) + '">' + escapeHtml(job.error.slice(0, 120)) + "</small>" : "";
      return '<tr><td title="' + escapeHtml(job.source) + '">' + escapeHtml(name) + '<br><small class="muted">' + (job.width || "—") + " × " + (job.height || "—") + "</small></td>" +
        "<td><small>" + escapeHtml(conf) + "</small>" + profile + "</td>" +
        '<td><span class="status ' + escapeHtml(job.status) + '">' + escapeHtml(labels[job.status] || job.status) + "</span></td>" +
        "<td>" + escapeHtml(job.stage || "—") + progressHtml(job) + trash + warning + err + "</td>" +
        '<td><div class="row-actions">' + jobActions(job) + "</div></td></tr>";
    }).join("");
  } catch (error) { console.warn(error); }
}
async function loadLogs(id) {
  const data = await api("/api/logs/" + encodeURIComponent(id)); $("logTitle").textContent = "Journal · " + id;
  $("logContent").textContent = data.log || "Aucun journal disponible."; $("logModal").classList.remove("hidden");
  $("logContent").scrollTop = $("logContent").scrollHeight;
}

// ------------------------------------------------------------------ aperçus
async function startPreview() {
  const models = [...$("previewModels").querySelectorAll("input:checked")].map(i => i.value);
  try {
    await saveSettings(true);
    const result = await post("/api/preview", { source: $("previewSource").value.trim(), start: parseTime($("previewStart").value),
      duration: Number($("previewDuration").value), models });
    state.currentPreview = result.id; $("previewStatus").textContent = "Aperçu en file : il passe avant les autres tâches.";
    $("previewStatus").className = "ollama-status";
    toast("Aperçu lancé"); refreshPreviews();
  } catch (error) { toast(error.message); $("previewStatus").textContent = error.message; $("previewStatus").className = "ollama-status error"; }
}
async function refreshPreviews() {
  try {
    const data = await api("/api/previews"); state.previews = data.previews;
    const current = state.previews.find(p => p.id === state.currentPreview) || state.previews[0];
    if (!current) return;
    if (["queued", "running"].includes(current.status)) {
      $("previewStatus").textContent = (current.stage || "En attente") + (current.progress ? " · " + Math.round(current.progress * 100) + " %" : "");
      $("previewStatus").className = "ollama-status";
    } else if (current.status === "failed") {
      $("previewStatus").textContent = "Échec de l'aperçu : " + (current.error || ""); $("previewStatus").className = "ollama-status error";
    } else if (current.status === "completed") {
      $("previewStatus").textContent = "Aperçu prêt."; $("previewStatus").className = "ollama-status ok";
    }
    const ready = state.previews.filter(p => p.manifest && p.manifest.variants.length > 1);
    $("compareBox").classList.toggle("hidden", !ready.length);
    const pick = $("previewPick"), selected = pick.value;
    pick.innerHTML = ready.map(p => '<option value="' + p.id + '">' + escapeHtml(p.manifest.name) + " · " + formatClock(p.manifest.start) + (p.manifest.complete ? "" : " (en cours)") + "</option>").join("");
    if (state.currentPreview && ready.some(p => p.id === state.currentPreview) && selected !== state.currentPreview) { pick.value = state.currentPreview; loadComparison(); }
    else if (selected && ready.some(p => p.id === selected)) { pick.value = selected; updateVariantOptions(); }
    else if (ready.length) loadComparison();
  } catch (error) { console.warn(error); }
}
function formatClock(seconds) { const s = Math.round(seconds); return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); }
function currentManifest() { return state.previews.find(p => p.id === $("previewPick").value)?.manifest; }
function updateVariantOptions() {
  const m = currentManifest(); if (!m) return;
  for (const [id, fallback] of [["leftVariant", 0], ["rightVariant", 1]]) {
    const select = $(id), previous = select.value;
    select.innerHTML = m.variants.map(v => '<option value="' + v.id + '">' + escapeHtml(v.label) + "</option>").join("");
    select.value = m.variants.some(v => v.id === previous) ? previous : m.variants[Math.min(fallback, m.variants.length - 1)].id;
  }
}
function loadComparison() {
  updateVariantOptions(); setVariant("left"); setVariant("right");
}
function setVariant(side) {
  const m = currentManifest(); if (!m) return;
  const variant = m.variants.find(v => v.id === $(side + "Variant").value); if (!variant) return;
  const video = $(side === "left" ? "videoLeft" : "videoRight");
  const src = "/api/previews/" + m.id + "/" + variant.file;
  if (!video.src.endsWith(src)) { video.src = src; video.load(); }
  $(side + "Label").textContent = variant.label;
  $("compare").style.aspectRatio = m.width + " / " + m.height;
}
function setComparePosition(clientX) {
  const box = $("compare").getBoundingClientRect();
  const pct = Math.max(0, Math.min(100, (clientX - box.left) / box.width * 100));
  $("compare").style.setProperty("--pos", pct + "%");
}
function togglePlay() {
  const left = $("videoLeft"), right = $("videoRight");
  if (left.paused) { right.currentTime = left.currentTime; Promise.all([left.play(), right.play()]).catch(() => {}); $("playPause").textContent = "Ⅱ Pause"; }
  else { left.pause(); right.pause(); right.currentTime = left.currentTime; $("playPause").textContent = "▶ Lecture"; }
}

// ------------------------------------------------------------------ Ollama
async function renderOllamaResult(data) {
  const status = $("ollamaStatus"), badge = $("ollamaBadge"), select = $("ollamaModel");
  if (data.connected) {
    if (data.url) $("ollamaUrl").value = data.url;
    status.textContent = "Connecté · Ollama " + (data.version || ""); status.className = "ollama-status ok"; badge.textContent = "CONNECTÉ";
    const previous = select.value || data.selected_model || "";
    select.innerHTML = '<option value="">Sélectionner un modèle…</option>' + (data.models || []).map(n => '<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + "</option>").join("");
    if (previous && (data.models || []).includes(previous)) select.value = previous;
    else if (data.models && data.models.length) select.value = data.models[0];
  } else {
    status.textContent = data.message || data.error || "Ollama non joignable"; status.className = "ollama-status error"; badge.textContent = "NON CONNECTÉ";
  }
}
async function checkOllama() {
  try {
    const data = await api("/api/ollama/status");
    if (data.connected) {
      await renderOllamaResult(data);
      if (!data.enabled) { $("ollamaStatus").textContent = "Ollama détecté. Activez l'intégration pour l'utiliser."; $("ollamaBadge").textContent = "DÉTECTÉ"; }
    } else if (!data.enabled) {
      $("ollamaStatus").textContent = "Aucune instance détectée pour le moment. L'intégration reste facultative.";
      $("ollamaStatus").className = "ollama-status"; $("ollamaBadge").textContent = "DÉSACTIVÉ";
    } else { await renderOllamaResult(data); }
  } catch (error) { $("ollamaStatus").textContent = error.message; $("ollamaStatus").className = "ollama-status error"; }
}
async function testOllama() {
  $("ollamaStatus").textContent = "Test de connexion…"; $("ollamaStatus").className = "ollama-status";
  try { const data = await post("/api/ollama/test", { url: $("ollamaUrl").value.trim() }); await renderOllamaResult({ connected: true, ...data }); toast("Connexion Ollama réussie"); }
  catch (error) { $("ollamaStatus").textContent = error.message; $("ollamaStatus").className = "ollama-status error"; $("ollamaBadge").textContent = "NON CONNECTÉ"; toast("Ollama inaccessible"); }
}
async function saveOllama() {
  try { await post("/api/ollama/config", ollamaPayload()); toast("Configuration Ollama enregistrée"); await checkOllama(); }
  catch (error) { toast(error.message); }
}

// ------------------------------------------------------------------ automatisation
async function saveAutomation(quiet = false) {
  const result = await post("/api/config", automationPayload());
  applySettings(result.settings); if (!quiet) toast("Automatisation enregistrée");
}
async function testIntegration(url, label) {
  const status = $("automationStatus");
  try {
    await saveAutomation(true); status.textContent = label + "…"; status.className = "ollama-status";
    const data = await post(url); status.textContent = data.message || "Envoyé avec succès"; status.className = "ollama-status ok";
  } catch (error) { status.textContent = error.message; status.className = "ollama-status error"; }
}

// ------------------------------------------------------------------ système
function renderGpu(gpu) {
  const details = $("healthDetails");
  if (!gpu || gpu.status === "never") { $("gpuState").textContent = "Non vérifié"; return; }
  if (gpu.status === "running") { $("gpuState").textContent = "Test en cours…"; return; }
  $("gpuState").textContent = gpu.status === "ok" ? "GPU Vulkan détecté" : "Non détecté";
  const text = (gpu.devices || []).join("\n") + (gpu.error ? "\n" + gpu.error : "");
  details.textContent = text.trim(); details.classList.toggle("hidden", !text.trim());
}
async function checkHealth() {
  try {
    const data = await api("/api/health");
    const local = data.runner === "local";
    $("dockerState").textContent = local ? "Mode local (sans socket)" : (data.docker_ok ? "Connecté" : "Indisponible");
    $("imageState").textContent = data.image_present == null ? "—" : (data.image_present ? (local ? "Installé" : "Image présente") : (local ? "Binaire introuvable" : "À télécharger (automatique)"));
    $("engineName").textContent = String(data.image).split("/").pop().replace(":", " ");
    $("footerVersion").textContent = "v" + data.version; $("sideVersion").textContent = "v" + data.version + " · Video2X";
    renderGpu(data.gpu);
    if (data.error) { $("healthDetails").textContent = data.error; $("healthDetails").classList.remove("hidden"); }
    $("connection").classList.toggle("online", data.docker_ok);
    $("connection").innerHTML = "<i></i>" + (data.docker_ok ? "Serveur connecté" : "Docker indisponible");
    return data;
  } catch (error) {
    $("dockerState").textContent = "Erreur"; $("gpuState").textContent = "Inconnu";
    $("connection").classList.remove("online"); $("connection").innerHTML = "<i></i> Hors ligne";
  }
}
async function refreshGpuStats() {
  try {
    const { cards } = await api("/api/gpu/stats");
    $("gpuLive").innerHTML = cards.map(c => {
      const items = [];
      if (c.busy_percent != null) items.push("<b>" + c.busy_percent + " %</b> charge");
      if (c.temperature_c != null) items.push("<b>" + Math.round(c.temperature_c) + " °C</b>");
      if (c.vram_total) items.push("<b>" + (c.vram_used / 1073741824).toFixed(1) + " / " + (c.vram_total / 1073741824).toFixed(1) + " Go</b> VRAM");
      if (c.power_w != null) items.push("<b>" + Math.round(c.power_w) + " W</b>");
      if (c.fan_rpm != null) items.push("<b>" + c.fan_rpm + "</b> tr/min");
      return '<div class="gpu-card"><span>' + escapeHtml(c.card) + "</span>" + items.join("<i>·</i>") +
        (c.busy_percent != null ? '<div class="progress-bar"><i style="width:' + c.busy_percent + '%"></i></div>' : "") + "</div>";
    }).join("");
  } catch (error) { console.warn(error); }
}
async function testGpu() {
  try {
    await post("/api/health/gpu"); $("gpuState").textContent = "Test en cours…";
    toast("Test GPU lancé (le premier peut télécharger l'image Video2X)");
    if (gpuPolling) return;
    gpuPolling = setInterval(async () => {
      const data = await checkHealth();
      if (!data || !data.gpu || data.gpu.status !== "running") { clearInterval(gpuPolling); gpuPolling = null; }
    }, 3000);
  } catch (error) { toast(error.message); }
}

// ------------------------------------------------------------------ dossiers source et disques
function sourceRow(path = "") {
  return '<div class="path-control source-row"><input class="s-path" value="' + escapeHtml(path) + '" placeholder="/data/videos ou /disques/…"><button class="browse" data-picker="sourceRow">Parcourir</button><button class="icon-btn remove-source" data-remove-source title="Retirer ce dossier">×</button></div>';
}
function renderSources(paths) { $("sourcesList").innerHTML = (paths.length ? paths : [""]).map(sourceRow).join(""); }
function readSources() { return [...document.querySelectorAll("#sourcesList .s-path")].map(i => i.value.trim()).filter(Boolean); }
function firstSource() { return readSources()[0] || ""; }
function diskSpace(disk) {
  if (!disk.total) return "";
  return (disk.free / 1073741824).toFixed(0) + " Go libres sur " + (disk.total / 1073741824).toFixed(0) + " Go";
}
function renderDisks(disks) {
  $("disksHint").innerHTML = "<b>Disques accessibles :</b> " + disks.map(d => '<span class="disk-chip" title="' + escapeHtml(d.path) + '">🖴 ' + escapeHtml(d.name) + (d.total ? " · " + escapeHtml(diskSpace(d)) : "") + "</span>").join("") +
    '<small>Un disque manque ? Sur le NAS : <code>sudo bash bibliotheques.sh ajouter /chemin/du/disque</code></small>';
}

// ------------------------------------------------------------------ explorateur
async function browse(path) {
  try {
    const data = await api("/api/browse?path=" + encodeURIComponent(path) + (picker.mode === "file" ? "&files=1" : ""));
    const rows = [];
    picker.path = data.path; $("currentPath").textContent = data.path || "Disques accessibles";
    $("selectFolder").disabled = !data.path;
    if (data.parent != null) rows.push('<div class="dir-item" data-path="' + escapeHtml(data.parent) + '">↰ <b>..</b><span>' + (data.parent ? "Remonter" : "Tous les disques") + "</span></div>");
    for (const disk of data.disks || []) rows.push('<div class="dir-item" data-path="' + escapeHtml(disk.path) + '">🖴 <b>' + escapeHtml(disk.name) + "</b><span>" + escapeHtml(diskSpace(disk)) + " · Ouvrir →</span></div>");
    for (const dir of data.directories) rows.push('<div class="dir-item" data-path="' + escapeHtml(dir.path) + '">📁 <b>' + escapeHtml(dir.name) + "</b><span>Ouvrir →</span></div>");
    for (const file of data.files || []) rows.push('<div class="dir-item" data-file="' + escapeHtml(file.path) + '">🎞 <b>' + escapeHtml(file.name) + "</b><span>" + (file.size / 1048576).toFixed(0) + " Mo · Choisir</span></div>");
    $("directoryList").innerHTML = rows.join("") || '<div class="empty">Aucun élément.</div>';
  } catch (error) { toast(error.message); }
}
function openPicker(input, mode) {
  picker = { input, mode, path: "" };
  $("pickerTitle").textContent = mode === "file" ? "Choisir une vidéo" : "Choisir un dossier";
  $("selectFolder").classList.toggle("hidden", mode === "file");
  $("picker").classList.remove("hidden");
  browse(input.value || (mode === "file" ? firstSource() : ""));
}
function setupNav() {
  const links = [...document.querySelectorAll(".sidebar nav a")];
  const sections = links.map(a => document.querySelector(a.getAttribute("href"))).filter(Boolean);
  const activate = id => links.forEach(a => a.classList.toggle("active", a.getAttribute("href") === "#" + id));
  links.forEach(a => a.addEventListener("click", () => activate(a.getAttribute("href").slice(1))));
  if (!("IntersectionObserver" in window)) return;
  const observer = new IntersectionObserver(entries => {
    const visible = entries.filter(e => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (visible) activate(visible.target.id);
  }, { rootMargin: "-20% 0px -55% 0px" });
  sections.forEach(s => observer.observe(s));
}

// ------------------------------------------------------------------ événements
const jobAction = async (fn, message) => { try { await fn(); if (message) toast(message); refreshJobs(); } catch (e) { toast(e.message); } };
document.addEventListener("click", async event => {
  const t = event.target;
  const dir = t.closest("[data-path]"); if (dir) return browse(dir.dataset.path);
  const file = t.closest("[data-file]"); if (file) { picker.input.value = file.dataset.file; $("picker").classList.add("hidden"); return; }
  const pick = t.closest("[data-picker]");
  if (pick) {
    const kind = pick.dataset.picker;
    const input = kind === "profile" ? pick.closest("td").querySelector("input") : kind === "sourceRow" ? pick.closest(".source-row").querySelector("input") : $(kind);
    return openPicker(input, pick.dataset.mode || "dir");
  }
  const id = attr => t.closest("[" + attr + "]")?.getAttribute(attr);
  if (id("data-log")) return loadLogs(id("data-log")).catch(e => toast(e.message));
  if (id("data-cancel")) return jobAction(() => post("/api/jobs/" + id("data-cancel") + "/cancel"), "Annulation demandée");
  if (id("data-restore")) { if (!confirm("Supprimer la version Upscale et remettre le fichier d'origine à sa place ?")) return; return jobAction(() => post("/api/jobs/" + id("data-restore") + "/restore"), "Original restauré"); }
  if (id("data-retry")) return jobAction(() => post("/api/jobs/" + id("data-retry") + "/retry"), "Tâche remise en file (reprise au dernier segment)");
  if (id("data-delete")) { if (!confirm("Supprimer cette tâche, son journal et ses fichiers temporaires ?")) return; return jobAction(() => post("/api/jobs/" + id("data-delete") + "/delete"), "Tâche supprimée"); }
  for (const where of ["top", "up", "down"]) if (id("data-move-" + where)) return jobAction(() => post("/api/jobs/" + id("data-move-" + where) + "/move", { to: where }));
  if (t.closest("[data-remove-source]")) { t.closest(".source-row").remove(); if (!document.querySelector("#sourcesList .source-row")) renderSources([]); return; }
  if (t.closest("[data-remove-profile]")) { t.closest("tr").remove(); if (!readProfiles().length && !$("profilesBody").querySelector(".p-path")) renderProfiles([]); }
  const filter = t.closest("[data-filter]");
  if (filter) { state.filter = filter.dataset.filter; state.page = 1; document.querySelectorAll("#filters button").forEach(b => b.classList.toggle("active", b === filter)); refreshJobs(); }
});
$("addSource").addEventListener("click", () => { $("sourcesList").insertAdjacentHTML("beforeend", sourceRow("")); openPicker([...document.querySelectorAll("#sourcesList .s-path")].pop(), "dir"); });
$("addProfile").addEventListener("click", () => { $("profilesBody").querySelector(".empty-row")?.remove(); $("profilesBody").insertAdjacentHTML("beforeend", profileRow({ path: firstSource() })); });
$("selectFolder").addEventListener("click", () => { if (picker.input) picker.input.value = picker.path; $("picker").classList.add("hidden"); });
$("closePicker").addEventListener("click", () => $("picker").classList.add("hidden"));
$("closeLogs").addEventListener("click", () => $("logModal").classList.add("hidden"));
$("picker").addEventListener("click", e => { if (e.target.id === "picker") $("picker").classList.add("hidden"); });
$("logModal").addEventListener("click", e => { if (e.target.id === "logModal") $("logModal").classList.add("hidden"); });
$("saveSettings").addEventListener("click", () => saveSettings().catch(e => toast(e.message)));
$("scanBtn").addEventListener("click", () => startScan(false)); $("scanHero").addEventListener("click", () => startScan(false));
$("queueBtn").addEventListener("click", () => startScan(true));
$("refreshBtn").addEventListener("click", () => { refreshJobs(); checkHealth(); refreshPreviews(); });
$("healthBtn").addEventListener("click", testGpu);
$("testOllama").addEventListener("click", testOllama); $("saveOllama").addEventListener("click", saveOllama);
$("saveAutomation").addEventListener("click", () => saveAutomation().catch(e => toast(e.message)));
$("testNotify").addEventListener("click", () => testIntegration("/api/notify/test", "Envoi de la notification"));
$("testMedia").addEventListener("click", () => testIntegration("/api/media/test", "Contact du serveur"));
$("pauseBtn").addEventListener("click", () => jobAction(() => post("/api/pause"), "La vidéo en cours s'arrêtera à la fin de son segment"));
$("resumeBtn").addEventListener("click", () => jobAction(() => post("/api/resume"), "Traitement repris"));
$("clearBtn").addEventListener("click", () => { if (confirm("Supprimer l'historique des tâches terminées ? Les vidéos produites sont conservées.")) jobAction(() => post("/api/jobs/clear", { statuses: ["completed"] }), "Historique vidé"); });
$("prevPage").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); refreshJobs(); });
$("nextPage").addEventListener("click", () => { state.page += 1; refreshJobs(); });
$("crf").addEventListener("input", () => $("crfValue").textContent = $("crf").value);
$("replaceOriginal").addEventListener("change", () => {
  if ($("replaceOriginal").checked && !confirm("Les fichiers d'origine seront remplacés par la version Upscale (l'original part dans la corbeille selon le réglage choisi). Activer ?")) $("replaceOriginal").checked = false;
  updateReplaceUi();
});
$("trashDays").addEventListener("change", () => {
  if ($("trashDays").value === "0" && !confirm("Les originaux seront supprimés définitivement, sans possibilité de restauration. Confirmer ?")) setSelect("trashDays", 30);
});
$("previewBtn").addEventListener("click", startPreview);
$("previewPick").addEventListener("change", () => { state.currentPreview = $("previewPick").value; loadComparison(); });
$("leftVariant").addEventListener("change", () => setVariant("left"));
$("rightVariant").addEventListener("change", () => setVariant("right"));
$("playPause").addEventListener("click", togglePlay);
$("compare").addEventListener("pointerdown", e => { $("compare").setPointerCapture(e.pointerId); setComparePosition(e.clientX); });
$("compare").addEventListener("pointermove", e => { if (e.buttons) setComparePosition(e.clientX); });
$("videoLeft").addEventListener("timeupdate", () => { const l = $("videoLeft"), r = $("videoRight"); if (!l.paused && Math.abs(l.currentTime - r.currentTime) > 0.08) r.currentTime = l.currentTime; });
$("videoLeft").addEventListener("seeked", () => { $("videoRight").currentTime = $("videoLeft").currentTime; });

(async function init() {
  setupNav();
  try {
    const data = await api("/api/config"); state.models = data.models; fillModelSelects(); applySettings(data.settings); renderDisks(data.disks || []);
  } catch (e) { toast(e.message); }
  try { const s = await api("/api/scan"); if (s.running) { showScanResult(describeScan(s)); pollScan(); } } catch (e) { /* rien */ }
  refreshJobs(); checkHealth(); checkOllama(); refreshPreviews(); refreshGpuStats();
  setInterval(refreshJobs, 3000); setInterval(refreshPreviews, 4000); setInterval(refreshGpuStats, 5000);
  setInterval(checkHealth, 60000); setInterval(checkOllama, 60000);
})();
