const $ = (id) => document.getElementById(id);
let pickerTarget = null;
let pickerPath = "/data";
let toastTimer;

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
function settingsPayload() {
  return {
    source: $("source").value.trim(), destination: $("destination").value.trim(),
    max_input_height: Number($("maxInput").value), output_height: Number($("outputHeight").value),
    model: $("model").value, crf: Number($("crf").value), preset: $("preset").value,
    include_subfolders: $("recursive").checked,
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
  $("ollamaEnabled").checked=!!s.ollama_enabled;
  $("ollamaUrl").value=s.ollama_url||"http://host.docker.internal:11434";
  $("ollamaModel").value=s.ollama_model||"";
}
async function saveSettings() {
  const result=await api("/api/config",{method:"POST",body:JSON.stringify(settingsPayload())});
  applySettings(result.settings); return result.settings;
}
async function scan() {
  try {
    await saveSettings(); $("scanResult").classList.remove("hidden"); $("scanResult").textContent="Analyse en cours…";
    const result=await api("/api/scan",{method:"POST",body:JSON.stringify(settingsPayload())});
    $("scanResult").textContent=result.count+" vidéo(s) éligible(s) trouvée(s). Les fichiers déjà présents à destination sont ignorés.";
    toast(result.count+" vidéo(s) détectée(s)");
  } catch(error) { toast(error.message); $("scanResult").textContent=error.message; }
}
async function queue() {
  try {
    await saveSettings();
    const result=await api("/api/queue",{method:"POST",body:JSON.stringify(settingsPayload())});
    $("scanResult").classList.remove("hidden");
    $("scanResult").textContent=result.added+" nouvelle(s) vidéo(s) ajoutée(s) à la file ("+result.found+" éligible(s)).";
    toast(result.added+" tâche(s) ajoutée(s)"); await refreshJobs();
    $("queue").scrollIntoView({behavior:"smooth"});
  } catch(error) { toast(error.message); }
}
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
async function refreshJobs() {
  try {
    const data=await api("/api/jobs"), counts=data.counts||{};
    $("queuedCount").textContent=counts.queued||0; $("runningCount").textContent=counts.running||0;
    $("completedCount").textContent=counts.completed||0; $("failedCount").textContent=counts.failed||0;
    $("pauseBtn").classList.toggle("hidden",data.paused); $("resumeBtn").classList.toggle("hidden",!data.paused);
    const body=$("jobsBody");
    if(!data.jobs.length){body.innerHTML='<tr><td colspan="5" class="empty">Aucun traitement pour le moment.</td></tr>';return;}
    body.innerHTML=data.jobs.map(job=>{
      const name=job.source.split("/").pop();
      const labels={queued:"En attente",running:"En cours",completed:"Terminée",failed:"Erreur",cancelled:"Annulée"};
      const actions='<button class="text-btn" data-log="'+job.id+'">Logs</button>'+
        (["queued","running"].includes(job.status)?'<button class="text-btn" data-cancel="'+job.id+'">Arrêter</button>':"");
      return '<tr><td title="'+escapeHtml(job.source)+'">'+escapeHtml(name)+'</td><td>'+(job.width||"—")+' × '+(job.height||"—")+'</td>'+
        '<td><span class="status '+job.status+'">'+(labels[job.status]||job.status)+'</span></td>'+
        '<td>'+escapeHtml(job.stage||"—")+(job.error?'<br><small>'+escapeHtml(job.error.slice(0,80))+'</small>':"")+'</td>'+
        '<td><div class="row-actions">'+actions+'</div></td></tr>';
    }).join("");
  } catch(error) { console.warn(error); }
}

async function renderOllamaResult(data) {
  const status=$("ollamaStatus"), badge=$("ollamaBadge"), select=$("ollamaModel");
  if(data.connected){
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
    const data=await api("/api/ollama/test",{method:"POST",body:JSON.stringify({url})});
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
    await saveSettings();
    toast("Configuration Ollama enregistrée");
    await checkOllama();
  } catch(error) { toast(error.message); }
}
async function checkHealth() {
  $("dockerState").textContent="Vérification…"; $("gpuState").textContent="Vérification…";
  $("healthDetails").classList.add("hidden");
  try {
    const data=await api("/api/health");
    $("dockerState").textContent=data.docker_ok?"Connecté":"Indisponible";
    $("gpuState").textContent=data.gpu_detected?"GPU Vulkan détecté":"Non détecté";
    if(data.error||(data.gpu_output&&data.gpu_output.length)){
      $("healthDetails").textContent=(data.gpu_output||[]).join("\n")+(data.error?"\n"+data.error:"");
      $("healthDetails").classList.remove("hidden");
    }
    $("connection").classList.toggle("online",data.docker_ok);
    $("connection").innerHTML="<i></i>"+(data.docker_ok?"Serveur connecté":"Docker indisponible");
  } catch(error) {
    $("dockerState").textContent="Erreur"; $("gpuState").textContent="Inconnu";
    $("connection").classList.remove("online"); $("connection").innerHTML="<i></i> Hors ligne"; toast(error.message);
  }
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
  const data=await api("/api/logs/"+id); $("logTitle").textContent="Journal · "+id;
  $("logContent").textContent=data.log||"Aucun journal disponible."; $("logModal").classList.remove("hidden");
}
document.addEventListener("click",async event=>{
  const dir=event.target.closest("[data-path]"); if(dir) browse(dir.dataset.path);
  const picker=event.target.closest("[data-picker]"); if(picker) openPicker(picker.dataset.picker);
  const log=event.target.closest("[data-log]"); if(log) loadLogs(log.dataset.log).catch(e=>toast(e.message));
  const cancel=event.target.closest("[data-cancel]");
  if(cancel){try{await api("/api/cancel/"+cancel.dataset.cancel,{method:"POST",body:"{}"});toast("Annulation demandée");refreshJobs();}catch(e){toast(e.message);}}
});
$("selectFolder").addEventListener("click",()=>{if(pickerTarget)$(pickerTarget).value=pickerPath;$("picker").classList.add("hidden");});
$("closePicker").addEventListener("click",()=>$("picker").classList.add("hidden"));
$("closeLogs").addEventListener("click",()=>$("logModal").classList.add("hidden"));
$("picker").addEventListener("click",e=>{if(e.target.id==="picker")$("picker").classList.add("hidden");});
$("logModal").addEventListener("click",e=>{if(e.target.id==="logModal")$("logModal").classList.add("hidden");});
$("scanBtn").addEventListener("click",scan); $("scanHero").addEventListener("click",scan);
$("queueBtn").addEventListener("click",queue);
$("refreshBtn").addEventListener("click",()=>{refreshJobs();checkHealth();});
$("healthBtn").addEventListener("click",checkHealth);
$("testOllama").addEventListener("click",testOllama); $("saveOllama").addEventListener("click",saveOllama);
$("pauseBtn").addEventListener("click",async()=>{await api("/api/pause",{method:"POST",body:"{}"});toast("File mise en pause");refreshJobs();});
$("resumeBtn").addEventListener("click",async()=>{await api("/api/resume",{method:"POST",body:"{}"});toast("Traitement repris");refreshJobs();});
$("crf").addEventListener("input",()=>$("crfValue").textContent=$("crf").value);
(async function init(){
  try{const data=await api("/api/config");applySettings(data.settings);}catch(e){toast(e.message);}
  refreshJobs();checkHealth();checkOllama();setInterval(refreshJobs,3000);setInterval(checkHealth,60000);setInterval(checkOllama,60000);
})();
