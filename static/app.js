const $ = (id) => document.getElementById(id);
let pickerTarget = null;
let pickerPath = "/data";
let toastTimer;
let scanPolling = null;
let gpuPolling = null;

function toast(message) {
  const el = $("toast"); el.textContent = message; el.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}
async function api(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || ("Erreur HTTP " + response.status));
  return data;
}
const post = (url, body = {}) => api(url, { method: "POST", body: JSON.stringify(body) });

function settingsPayload() {
  return {
    source: $("source").value.trim(), destination: $("destination").value.trim(),
    max_input_height: Number($("maxInput").value), output_height: Number($("outputHeight").value),
    model: $("model").value, crf: Number($("crf").value), preset: $("preset").value,
    include_subfolders: $("recursive").checked, deinterlace: $("deinterlace").value
  };
}
function ollamaPayload() {
  return {
    ollama_enabled: $("ollamaEnabled").checked,
    ollama_url: $("ollamaUrl").value.trim(),
    ollama_model: $("ollamaModel").value
  };
}
function applySettings(s) {
  $("source").value=s.source; $("destination").value=s.destination;
  $("maxInput").value=String(s.max_input_height); $("outputHeight").value=String(s.output_height);
  $("model").value=s.model; $("crf").value=s.crf; $("crfValue").textContent=s.crf;
  $("preset").value=s.preset; $("recursive").checked=s.include_subfolders;
  $("deinterlace").value=s.deinterlace||"auto";
  $("ollamaEnabled").checked=!!s.ollama_enabled;
  $("ollamaUrl").value=s.ollama_url||"http://host.docker.internal:11434";
  const select=$("ollamaModel");
  if(s.ollama_model && ![...select.options].some(o=>o.value===s.ollama_model)){
    select.insertAdjacentHTML("beforeend",'<option value="'+escapeHtml(s.ollama_model)+'">'+escapeHtml(s.ollama_model)+'</option>');
  }
  select.value=s.ollama_model||"";
}

function showScanResult(text, isError = false) {
  const el=$("scanResult"); el.classList.remove("hidden"); el.classList.toggle("error", isError); el.textContent=text;
}
function describeScan(state) {
  if(state.error) return state.error;
  if(state.running) return "Analyse en cours… "+(state.total?state.checked+" / "+state.total+" fichier(s) vidéo examiné(s)":"recherche des fichiers");
  let text=state.found+" vidéo(s) éligible(s) sur "+state.checked+" fichier(s) vidéo.";
  if(state.already_known) text+=" "+state.already_known+" déjà en file ou déjà traitée(s).";
  if(state.enqueue){
    text+=" "+(state.added||0)+" ajoutée(s) à la file.";
    if(state.conflicts) text+=" "+state.conflicts+" ignorée(s) : même fichier de sortie qu'une tâche en cours.";
  }
  return text;
}
async function startScan(enqueue) {
  if(scanPolling) { toast("Une analyse est déjà en cours"); return; }
  try {
    await post(enqueue?"/api/queue":"/api/scan", settingsPayload());
    showScanResult("Analyse en cours…");
    pollScan();
  } catch(error) { toast(error.message); showScanResult(error.message, true); }
}
function pollScan() {
  if(scanPolling) return;
  scanPolling=setInterval(async()=>{
      try {
        const state=await api("/api/scan");
        showScanResult(describeScan(state), !!state.error);
        if(!state.running){
          clearInterval(scanPolling); scanPolling=null;
          if(state.error) toast(state.error);
          else if(state.enqueue){ toast((state.added||0)+" tâche(s) ajoutée(s)"); await refreshJobs(); $("queue").scrollIntoView({behavior:"smooth"}); }
          else toast(state.found+" vidéo(s) détectée(s)");
        }
      } catch(error) { clearInterval(scanPolling); scanPolling=null; showScanResult(error.message, true); }
  }, 1000);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function formatDuration(seconds) {
  if(seconds==null||!isFinite(seconds)) return "";
  const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60);
  if(h) return h+" h "+String(m).padStart(2,"0");
  if(m) return m+" min";
  return Math.max(1,Math.round(seconds))+" s";
}
function progressHtml(job) {
  if(job.status!=="running"||job.progress==null) return "";
  const pct=Math.max(0,Math.min(100,job.progress*100));
  const eta=job.eta_seconds!=null?" · reste ~"+formatDuration(job.eta_seconds):"";
  return '<div class="progress"><div class="progress-bar"><i style="width:'+pct.toFixed(1)+'%"></i></div><small>'+pct.toFixed(1)+' %'+eta+'</small></div>';
}
async function refreshJobs() {
  try {
    const data=await api("/api/jobs"), counts=data.counts||{};
    $("queuedCount").textContent=counts.queued||0; $("runningCount").textContent=counts.running||0;
    $("completedCount").textContent=counts.completed||0; $("failedCount").textContent=counts.failed||0;
    $("pauseBtn").classList.toggle("hidden",data.paused); $("resumeBtn").classList.toggle("hidden",!data.paused);
    const body=$("jobsBody");
    if(!data.jobs.length){body.innerHTML='<tr><td colspan="5" class="empty">Aucun traitement pour le moment.</td></tr>';return;}
    const labels={queued:"En attente",running:"En cours",completed:"Terminée",failed:"Erreur",cancelled:"Annulée"};
    body.innerHTML=data.jobs.map(job=>{
      const name=job.source.split("/").pop();
      const actions='<button class="text-btn" data-log="'+escapeHtml(job.id)+'">Logs</button>'+
        (["queued","running"].includes(job.status)?'<button class="text-btn" data-cancel="'+escapeHtml(job.id)+'">Arrêter</button>':"");
      return '<tr><td title="'+escapeHtml(job.source)+'">'+escapeHtml(name)+'</td><td>'+(job.width||"—")+' × '+(job.height||"—")+'</td>'+
        '<td><span class="status '+escapeHtml(job.status)+'">'+escapeHtml(labels[job.status]||job.status)+'</span></td>'+
        '<td>'+escapeHtml(job.stage||"—")+progressHtml(job)+(job.error?'<br><small title="'+escapeHtml(job.error)+'">'+escapeHtml(job.error.slice(0,120))+'</small>':"")+'</td>'+
        '<td><div class="row-actions">'+actions+'</div></td></tr>';
    }).join("");
  } catch(error) { console.warn(error); }
}

async function renderOllamaResult(data) {
  const status=$("ollamaStatus"), badge=$("ollamaBadge"), select=$("ollamaModel");
  if(data.connected){
    if(data.url) $("ollamaUrl").value=data.url;
    status.textContent="Connecté · Ollama "+(data.version||"");
    status.className="ollama-status ok"; badge.textContent="CONNECTÉ";
    const previous=select.value||data.selected_model||"";
    select.innerHTML='<option value="">Sélectionner un modèle…</option>'+
      (data.models||[]).map(name=>'<option value="'+escapeHtml(name)+'">'+escapeHtml(name)+'</option>').join("");
    if(previous && (data.models||[]).includes(previous)) select.value=previous;
    else if(data.models&&data.models.length) select.value=data.models[0];
  } else {
    status.textContent=data.message||data.error||"Ollama non joignable";
    status.className="ollama-status error"; badge.textContent="NON CONNECTÉ";
  }
}
async function checkOllama() {
  try {
    const data=await api("/api/ollama/status");
    if(data.connected){
      await renderOllamaResult(data);
      if(!data.enabled){$("ollamaStatus").textContent="Ollama détecté. Activez l'intégration pour l'utiliser."; $("ollamaBadge").textContent="DÉTECTÉ";}
    } else if(!data.enabled){
      $("ollamaStatus").textContent="Aucune instance détectée pour le moment. L'intégration reste facultative.";
      $("ollamaStatus").className="ollama-status"; $("ollamaBadge").textContent="DÉSACTIVÉ";
    } else { await renderOllamaResult(data); }
  } catch(error) {
    $("ollamaStatus").textContent=error.message; $("ollamaStatus").className="ollama-status error";
  }
}
async function testOllama() {
  const url=$("ollamaUrl").value.trim();
  $("ollamaStatus").textContent="Test de connexion…"; $("ollamaStatus").className="ollama-status";
  try {
    const data=await post("/api/ollama/test",{url});
    await renderOllamaResult({connected:true,...data});
    toast("Connexion Ollama réussie");
  } catch(error) {
    $("ollamaStatus").textContent=error.message; $("ollamaStatus").className="ollama-status error";
    $("ollamaBadge").textContent="NON CONNECTÉ";
    toast("Ollama inaccessible");
  }
}
async function saveOllama() {
  try {
    await post("/api/ollama/config", ollamaPayload());
    toast("Configuration Ollama enregistrée");
    await checkOllama();
  } catch(error) { toast(error.message); }
}

function renderGpu(gpu) {
  const details=$("healthDetails");
  if(!gpu||gpu.status==="never"){ $("gpuState").textContent="Non vérifié"; return; }
  if(gpu.status==="running"){ $("gpuState").textContent="Test en cours…"; return; }
  $("gpuState").textContent=gpu.status==="ok"?"GPU Vulkan détecté":"Non détecté";
  const text=(gpu.devices||[]).join("\n")+(gpu.error?"\n"+gpu.error:"");
  details.textContent=text.trim(); details.classList.toggle("hidden",!text.trim());
}
async function checkHealth() {
  try {
    const data=await api("/api/health");
    $("dockerState").textContent=data.docker_ok?"Connecté":"Indisponible";
    $("imageState").textContent=data.image_present==null?"—":(data.image_present?"Présente":"À télécharger (automatique au 1er traitement)");
    $("engineName").textContent=data.image.split("/").pop().replace(":"," ");
    renderGpu(data.gpu);
    if(data.error){ $("healthDetails").textContent=data.error; $("healthDetails").classList.remove("hidden"); }
    $("connection").classList.toggle("online",data.docker_ok);
    $("connection").innerHTML="<i></i>"+(data.docker_ok?"Serveur connecté":"Docker indisponible");
    return data;
  } catch(error) {
    $("dockerState").textContent="Erreur"; $("gpuState").textContent="Inconnu";
    $("connection").classList.remove("online"); $("connection").innerHTML="<i></i> Hors ligne";
  }
}
async function testGpu() {
  try {
    await post("/api/health/gpu");
    $("gpuState").textContent="Test en cours…";
    toast("Test GPU lancé (le premier peut télécharger l'image Video2X)");
    if(gpuPolling) return;
    gpuPolling=setInterval(async()=>{
      const data=await checkHealth();
      if(!data||!data.gpu||data.gpu.status!=="running"){ clearInterval(gpuPolling); gpuPolling=null; }
    },3000);
  } catch(error) { toast(error.message); }
}

async function browse(path) {
  try {
    const data=await api("/api/browse?path="+encodeURIComponent(path));
    pickerPath=data.path; $("currentPath").textContent=data.path;
    const rows=[];
    if(data.parent) rows.push('<div class="dir-item" data-path="'+escapeHtml(data.parent)+'">↰ <b>..</b><span>Remonter</span></div>');
    for(const dir of data.directories) rows.push('<div class="dir-item" data-path="'+escapeHtml(dir.path)+'">📁 <b>'+escapeHtml(dir.name)+'</b><span>Ouvrir →</span></div>');
    $("directoryList").innerHTML=rows.join("")||'<div class="empty">Aucun sous-dossier.</div>';
  } catch(error) { toast(error.message); }
}
function openPicker(target) {
  pickerTarget=target; $("picker").classList.remove("hidden");
  browse(target==="source"?$("source").value:$("destination").value);
}
async function loadLogs(id) {
  const data=await api("/api/logs/"+encodeURIComponent(id)); $("logTitle").textContent="Journal · "+id;
  $("logContent").textContent=data.log||"Aucun journal disponible."; $("logModal").classList.remove("hidden");
  $("logContent").scrollTop=$("logContent").scrollHeight;
}
function setupNav() {
  const links=[...document.querySelectorAll(".sidebar nav a")];
  const sections=links.map(a=>document.querySelector(a.getAttribute("href"))).filter(Boolean);
  const activate=id=>links.forEach(a=>a.classList.toggle("active",a.getAttribute("href")==="#"+id));
  if(!("IntersectionObserver" in window)) return;
  const observer=new IntersectionObserver(entries=>{
    const visible=entries.filter(e=>e.isIntersecting).sort((a,b)=>a.boundingClientRect.top-b.boundingClientRect.top)[0];
    if(visible) activate(visible.target.id);
  },{rootMargin:"-30% 0px -60% 0px"});
  sections.forEach(s=>observer.observe(s));
}

document.addEventListener("click",async event=>{
  const dir=event.target.closest("[data-path]"); if(dir) browse(dir.dataset.path);
  const picker=event.target.closest("[data-picker]"); if(picker) openPicker(picker.dataset.picker);
  const log=event.target.closest("[data-log]"); if(log) loadLogs(log.dataset.log).catch(e=>toast(e.message));
  const cancel=event.target.closest("[data-cancel]");
  if(cancel){try{await post("/api/cancel/"+encodeURIComponent(cancel.dataset.cancel));toast("Annulation demandée");refreshJobs();}catch(e){toast(e.message);}}
});
$("selectFolder").addEventListener("click",()=>{if(pickerTarget)$(pickerTarget).value=pickerPath;$("picker").classList.add("hidden");});
$("closePicker").addEventListener("click",()=>$("picker").classList.add("hidden"));
$("closeLogs").addEventListener("click",()=>$("logModal").classList.add("hidden"));
$("picker").addEventListener("click",e=>{if(e.target.id==="picker")$("picker").classList.add("hidden");});
$("logModal").addEventListener("click",e=>{if(e.target.id==="logModal")$("logModal").classList.add("hidden");});
$("scanBtn").addEventListener("click",()=>startScan(false)); $("scanHero").addEventListener("click",()=>startScan(false));
$("queueBtn").addEventListener("click",()=>startScan(true));
$("refreshBtn").addEventListener("click",()=>{refreshJobs();checkHealth();});
$("healthBtn").addEventListener("click",testGpu);
$("testOllama").addEventListener("click",testOllama); $("saveOllama").addEventListener("click",saveOllama);
$("pauseBtn").addEventListener("click",async()=>{try{await post("/api/pause");toast("La file s'arrêtera après la vidéo en cours");refreshJobs();}catch(e){toast(e.message);}});
$("resumeBtn").addEventListener("click",async()=>{try{await post("/api/resume");toast("Traitement repris");refreshJobs();}catch(e){toast(e.message);}});
$("crf").addEventListener("input",()=>$("crfValue").textContent=$("crf").value);
(async function init(){
  setupNav();
  try{const data=await api("/api/config");applySettings(data.settings);}catch(e){toast(e.message);}
  try{const state=await api("/api/scan"); if(state.running){ showScanResult(describeScan(state)); pollScan(); }}catch(e){/* rien */}
  refreshJobs();checkHealth();checkOllama();setInterval(refreshJobs,3000);setInterval(checkHealth,60000);setInterval(checkOllama,60000);
})();
