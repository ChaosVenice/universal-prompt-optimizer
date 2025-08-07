import os
import time
import json
import io
import zipfile
from urllib import request as urlreq
from urllib.error import URLError, HTTPError
import re
from textwrap import dedent
import random
from flask import Flask, request, jsonify, Response

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")

# ---------------------------
# Prompt Enhancement Engine
# ---------------------------

# Quality, style, mood terms for consistent categorization
STYLE_KEYWORDS = {
    "quality": ["masterpiece", "best quality", "ultra high detail", "8k", "4k", "highres", "ultra detailed", "extremely detailed", "intricate", "sharp focus", "professional"],
    "art_styles": ["photorealistic", "hyperrealistic", "cinematic", "digital art", "oil painting", "watercolor", "anime", "manga", "concept art", "impressionist", "baroque", "renaissance", "art nouveau", "cyberpunk", "steampunk", "minimalist"],
    "photography": ["bokeh", "depth of field", "macro", "wide angle", "telephoto", "portrait", "landscape", "street photography", "documentary", "fashion photography"],
    "lighting": ["soft lighting", "hard lighting", "natural lighting", "studio lighting", "golden hour", "blue hour", "backlighting", "rim lighting", "volumetric lighting", "chiaroscuro"],
    "composition": ["rule of thirds", "centered", "symmetrical", "leading lines", "framing", "negative space", "close-up", "medium shot", "wide shot", "bird's eye view", "worm's eye view"],
    "mood": ["moody", "dramatic", "serene", "melancholic", "uplifting", "mysterious", "romantic", "energetic", "peaceful", "tense", "nostalgic", "futuristic"],
    "color_grades": ["vibrant", "desaturated", "monochrome", "sepia", "teal and orange", "warm tones", "cool tones", "high contrast", "low contrast", "film grain"]
}

# Comprehensive negative defaults
NEGATIVE_DEFAULT = ["lowres", "bad anatomy", "bad hands", "text", "error", "missing fingers", "extra digit", "fewer digits", "cropped", "worst quality", "low quality", "normal quality", "jpeg artifacts", "signature", "watermark", "username", "blurry", "bad feet", "cropped", "poorly drawn hands", "poorly drawn face", "mutation", "deformed", "worst quality", "low quality", "normal quality", "jpeg artifacts", "signature", "watermark", "extra fingers", "fewer digits", "extra limbs", "extra arms", "extra legs", "malformed limbs", "fused fingers", "too many fingers", "long neck", "cross-eyed", "mutated hands", "polar lowres", "bad body", "bad proportions", "gross proportions", "text", "error", "missing fingers", "missing arms", "missing legs", "extra digit", "extra arms", "extra leg", "extra foot"]

# Aspect ratio mappings
AR_TO_RES = {"1:1": (1024, 1024), "16:9": (1344, 768), "9:16": (768, 1344), "2:3": (832, 1216), "3:2": (1216, 832)}

# Common stopwords to filter out
STOPWORDS = set("""
a an the of and to in on at by for with from into over under between
is are was were be been being do does did have has had can will would should
this that these those as if then than so such very really just it its it's
""".split())

# ---------------------------
# Core Processing Functions
# ---------------------------

def tokenize(text):
    """Extract alphanumeric tokens from text."""
    return re.findall(r"[A-Za-z0-9\-+/#']+", text.lower())

def dedup_preserve(seq):
    """Remove duplicates while preserving order."""
    seen = set()
    out = []
    for x in seq:
        if x not in seen and x:
            seen.add(x)
            out.append(x)
    return out

def clamp_length(s, max_chars=850):
    """Prevent overly long prompts that degrade quality."""
    return (s[:max_chars] + "…") if len(s) > max_chars else s

def extract_categories(user_text: str):
    """Extract style keywords and subject terms from user input."""
    words = tokenize(user_text)
    text = user_text.lower()

    found = {k: [] for k in STYLE_KEYWORDS}
    for k, arr in STYLE_KEYWORDS.items():
        for term in arr:
            if term in text:
                found[k].append(term)

    # Extract likely subject terms (non-style, non-stopword)
    all_style = set()
    for arr in STYLE_KEYWORDS.values():
        all_style.update(arr)
    subject_terms = [w for w in words if w not in STOPWORDS and w not in all_style and len(w) > 2]

    return found, subject_terms

def build_positive(subject_terms, found, extras):
    """Construct optimized positive prompt with strict order: quality → subject → style → lighting → composition → mood → color grade → extra tags."""
    # Strict order as specified
    quality = ", ".join(dedup_preserve(found["quality"][:3])) or "masterpiece, best quality, highly detailed"
    subject = ", ".join(subject_terms[:5]) if subject_terms else "primary subject"
    style = ", ".join(dedup_preserve(found["art_styles"][:2] + found["photography"][:2]))
    lighting = extras.get("lighting", "").strip()
    if not lighting:
        lighting = ", ".join(found["lighting"][:2])
    composition = ", ".join(found["composition"][:2])
    mood = ", ".join(found["mood"][:2])
    color_grade = extras.get("color_grade", "").strip()
    if not color_grade:
        color_grade = ", ".join(found["color_grades"][:2])
    extra_tags = extras.get("extra_tags", "").strip()

    # Combine in strict order
    parts = [quality, subject, style, lighting, composition, mood, color_grade, extra_tags]
    parts = [p for p in parts if p.strip()]
    result = ", ".join(parts)
    return clamp_length(result)

def build_negative(user_negative):
    """Construct enhanced negative prompt covering anatomy/structure, artifacts/quality, branding/text, style pitfalls."""
    user_list = []
    if user_negative:
        user_list = [x.strip() for x in user_negative.split(",") if x.strip()]
    neg = dedup_preserve(NEGATIVE_DEFAULT + user_list)
    neg = ", ".join(neg)
    return clamp_length(neg)

def sdxl_prompt(positive, negative, ar):
    """Generate SDXL-optimized prompt configuration."""
    w, h = AR_TO_RES.get(ar, (1024, 1024))
    return {
        "positive": positive,
        "negative": negative,
        "settings": {
            "width": w,
            "height": h,
            "steps": 25,
            "cfg_scale": 7.0,
            "seed": -1,
            "sampler": "DPM++ 2M Karras"
        }
    }

def comfyui_recipe(positive, negative, ar):
    """Generate ComfyUI workflow configuration with execution tips."""
    w, h = AR_TO_RES.get(ar, (1024, 1024))
    return {
        "positive": positive,
        "negative": negative,
        "nodes_hint": {
            "KSampler": {"steps": 25, "cfg": 7.0, "sampler_name": "dpmpp_2m", "scheduler": "karras"},
            "EmptyLatentImage": {"width": w, "height": h, "batch_size": 1},
            "CheckpointLoaderSimple": {"ckpt_name": "sd_xl_base_1.0.safetensors"}
        },
        "execution_tips": [
            "Use SDXL base model for best results",
            "Enable 'Tiled VAE' if getting VRAM errors",
            "Consider refiner model for final 20% of steps"
        ]
    }

def midjourney_prompt(positive, ar):
    """Generate Midjourney v6 prompt string."""
    ar_flag = f"--ar {ar}"
    # Replace "8k" with "ultra high detail" for MJ compatibility
    clean_positive = positive.replace("8k", "ultra high detail")
    return f"{clean_positive} --v 6 {ar_flag} --stylize 200 --chaos 5"

def pika_prompt(positive):
    """Generate Pika Labs video prompt configuration."""
    return {
        "prompt": f"{positive}, animated micro-details, smooth motion, temporal consistency",
        "motion": "subtle camera push-in, natural parallax, no warping",
        "duration_sec": 6,
        "guidance": 7.0
    }

def runway_prompt(positive):
    """Generate Runway ML video prompt configuration."""
    return {
        "text_prompt": f"{positive}",
        "camera_motion": "push_in",
        "motion_strength": "medium",
        "duration_sec": 5,
        "notes": "Keep subject centrally framed to reduce morphing."
    }

# ---------------------------
# Routes
# ---------------------------
INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Universal Prompt Optimizer</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0b0f14;color:#e7edf5}
.container{max-width:980px;margin:36px auto;padding:0 16px}
.card{background:#111826;border:1px solid #1f2a3a;border-radius:14px;padding:16px;box-shadow:0 10px 30px rgba(0,0,0,.25);margin-bottom:16px}
h1{margin:0 0 6px;font-size:28px}.sub{margin:0 0 18px;color:#9fb2c7}
label{display:block;font-size:14px;color:#a9bdd4;margin-bottom:6px}
textarea,input,select{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #243447;background:#0f141c;color:#e7edf5}
textarea::placeholder,input::placeholder{color:#627a91}.row{display:flex;gap:12px;margin-top:12px;flex-wrap:wrap}.col{flex:1;min-width:240px}
button{margin-top:10px;padding:10px 14px;background:#2f6df6;border:none;border-radius:10px;color:#fff;font-weight:600;cursor:pointer}
button.secondary{background:#1e293b}button.danger{background:#9b1c1c}
button:hover{filter:brightness(1.05)}.results{margin-top:22px}.hidden{display:none}
.box{background:#0f141c;border:1px solid #1f2a3a;padding:12px;border-radius:10px;white-space:pre-wrap}
.section{margin-top:14px}.rowbtns{display:flex;gap:8px;flex-wrap:wrap}
.kv{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px;color:#bcd0e3}
small,.small{font-size:12px;color:#7f93a7}.badge{display:inline-block;padding:4px 8px;border:1px solid #2d3b4e;border-radius:999px;margin-right:6px;color:#a9bdd4}
footer{margin-top:24px;text-align:center;color:#6f859c;font-size:12px}
h3{margin:8px 0}select, input[type="text"] {height:40px}textarea {min-height:110px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:16px}
.modal .inner{background:#0f141c;border:1px solid #223045;border-radius:14px;max-width:720px;width:100%;padding:16px}
.modal .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:10px}
.imgwrap{margin-top:10px;display:flex;gap:12px;flex-wrap:wrap}
.imgwrap img{max-width:320px;border-radius:10px;border:1px solid #1f2a3a}
.thumb{display:flex;flex-direction:column;gap:6px}
.adv{background:#0e1420;border:1px dashed #27405e;border-radius:12px;padding:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.meta{font-family:ui-monospace,monospace;font-size:11px;color:#9fb2c7}
</style>
</head>
<body>
<div class="container">
  <h1>Universal Prompt Optimizer</h1>
  <p class="sub">Turn rough ideas into model-ready prompts for SDXL, ComfyUI, Midjourney, Pika, and Runway. Built for speed + consistency.</p>

  <!-- INPUTS -->
  <div class="card">
    <div class="row">
      <div class="col">
        <label>Your idea</label>
        <textarea id="idea" placeholder="e.g., A cozy coffee shop at golden hour, rain outside, cinematic lighting, moody vibe, shallow depth of field, candid couple"></textarea>
      </div>
    </div>

    <div class="row">
      <div class="col">
        <label>Negative prompt (optional)</label>
        <input id="negative" placeholder="e.g., lowres, watermark, bad anatomy">
      </div>
      <div class="col">
        <label>Aspect ratio</label>
        <select id="ar">
          <option selected>16:9</option>
          <option>1:1</option>
          <option>9:16</option>
          <option>2:3</option>
          <option>3:2</option>
        </select>
      </div>
    </div>

    <div class="row">
      <div class="col">
        <label>Lighting (optional)</label>
        <input id="lighting" placeholder="e.g., soft studio lighting, volumetric light">
      </div>
      <div class="col">
        <label>Color grade (optional)</label>
        <input id="color_grade" placeholder="e.g., teal and orange, Kodak Portra 400, moody grade">
      </div>
      <div class="col">
        <label>Extra tags (optional)</label>
        <input id="extra_tags" placeholder="e.g., film grain, depth of field, subsurface scattering">
      </div>
    </div>

    <div class="row">
      <div class="col small">
        <span class="badge">Tip</span> Keep nouns concrete. Add one clear mood + one lighting cue for best control.
      </div>
    </div>

    <!-- ADVANCED CONTROLS -->
    <div class="adv" style="margin-top:10px">
      <h3 style="margin-top:0">Advanced (Optional)</h3>
      <div class="row">
        <div class="col">
          <label>Steps</label>
          <input id="steps" type="text" placeholder="30">
        </div>
        <div class="col">
          <label>CFG Scale</label>
          <input id="cfg" type="text" placeholder="6.5">
        </div>
        <div class="col">
          <label>Sampler</label>
          <select id="sampler">
            <option selected>DPM++ 2M Karras</option>
            <option>DPM++ SDE Karras</option>
            <option>Euler a</option>
          </select>
        </div>
      </div>
      <div class="row">
        <div class="col">
          <label>Seed (leave blank or type "random")</label>
          <input id="seed" type="text" placeholder="random">
        </div>
        <div class="col">
          <label>Batch size (1–8)</label>
          <input id="batch" type="text" placeholder="1">
        </div>
      </div>
      <small>Advanced values override defaults during generation. Leave blank to use optimizer defaults.</small>
    </div>

    <div class="rowbtns" style="margin-top:10px">
      <button id="run">Optimize</button>
      <button class="secondary" onclick="openSettings()">Settings</button>
    </div>
  </div>

  <!-- PRESETS -->
  <div class="card">
    <h3>Presets</h3>
    <div class="row">
      <div class="col">
        <label>Preset name</label>
        <input id="presetName" placeholder="e.g., Neon Alley Cinematic">
      </div>
      <div class="col">
        <label>Load preset</label>
        <select id="presetSelect"></select>
      </div>
    </div>
    <div class="rowbtns" style="margin-top:10px">
      <button class="secondary" onclick="savePreset()">Save Preset</button>
      <button class="secondary" onclick="loadPreset()">Load</button>
      <button class="danger" onclick="deletePreset()">Delete</button>
      <button class="secondary" onclick="exportPresets()">Export JSON</button>
      <button class="secondary" onclick="importPresets()">Import JSON</button>
    </div>
    <small>Presets store: idea, negatives, AR, lighting, color grade, extra tags, and advanced settings (steps, cfg, sampler, seed, batch).</small>
  </div>

  <!-- RESULTS -->
  <div id="results" class="results hidden">
    <div class="card">
      <h3>Unified Prompt</h3>
      <div class="rowbtns">
        <button onclick="copyText('unifiedPos')">Copy Positive</button>
        <button onclick="copyText('unifiedNeg')">Copy Negative</button>
        <button onclick="downloadText('unified_positive.txt','unifiedPos')">Download .txt (Positive)</button>
        <button onclick="downloadText('unified_negative.txt','unifiedNeg')">Download .txt (Negative)</button>
      </div>
      <div id="unifiedPos" class="box section"></div>
      <div id="unifiedNeg" class="box section"></div>

      <h3>SDXL</h3>
      <div class="rowbtns">
        <button onclick="copyText('sdxlBox')">Copy JSON</button>
        <button onclick="downloadText('sdxl.json','sdxlBox')">Download JSON</button>
        <button class="secondary" id="genComfyBtn" onclick="generateComfy()">Generate (ComfyUI)</button>
        <button class="secondary" id="zipBtn" style="display:none" onclick="downloadZip(LAST_IMAGES)">Download ZIP</button>
      </div>
      <div id="sdxlBox" class="box section kv"></div>
      <div class="imgwrap" id="genImages"></div>

      <h3>ComfyUI</h3>
      <div class="rowbtns">
        <button onclick="copyText('comfyBox')">Copy JSON</button>
        <button onclick="downloadText('comfyui.json','comfyBox')">Download JSON</button>
      </div>
      <div id="comfyBox" class="box section kv"></div>

      <h3>Midjourney</h3>
      <div class="rowbtns">
        <button onclick="copyText('mjBox')">Copy Prompt</button>
        <button onclick="downloadText('midjourney.txt','mjBox')">Download .txt</button>
      </div>
      <div id="mjBox" class="box section kv"></div>

      <h3>Pika</h3>
      <div class="rowbtns">
        <button onclick="copyText('pikaBox')">Copy JSON</button>
        <button onclick="downloadText('pika.json','pikaBox')">Download JSON</button>
      </div>
      <div id="pikaBox" class="box section kv"></div>

      <h3>Runway</h3>
      <div class="rowbtns">
        <button onclick="copyText('runwayBox')">Copy JSON</button>
        <button onclick="downloadText('runway.json','runwayBox')">Download JSON</button>
      </div>
      <div id="runwayBox" class="box section kv"></div>

      <h3>Hints</h3>
      <div id="hintsBox" class="box section small"></div>
    </div>
  </div>

  <!-- HISTORY -->
  <div class="card">
    <h3>History</h3>
    <div class="rowbtns">
      <button class="secondary" onclick="exportHistory()">Export History JSON</button>
      <button class="danger" onclick="clearHistory()">Clear History</button>
    </div>
    <div id="historyGrid" class="grid" style="margin-top:10px"></div>
    <small class="small">History is saved locally in your browser (upo_history_v1). Click "Re-run" to regenerate with the same seed and settings.</small>
  </div>

  <footer>Seed tip: lock a seed for reproducibility; vary only seed to explore variants without wrecking the look.</footer>
</div>

<!-- SETTINGS MODAL -->
<div class="modal" id="settingsModal">
  <div class="inner">
    <h3>ComfyUI Settings</h3>
    <div class="row">
      <div class="col">
        <label>ComfyUI Host (e.g., http://127.0.0.1:8188)</label>
        <input id="comfyHost" placeholder="http://127.0.0.1:8188">
      </div>
    </div>
    <div class="row">
      <div class="col">
        <label>Custom Workflow JSON (optional)</label>
        <textarea id="workflowJson" placeholder='Paste a ComfyUI "workflow_api" JSON here to override defaults'></textarea>
      </div>
    </div>
    <div class="actions">
      <button class="secondary" onclick="closeSettings()">Close</button>
      <button onclick="saveSettings()">Save</button>
    </div>
  </div>
</div>

<script>
const LS_KEY = 'upo_presets_v1';
const LS_SETTINGS = 'upo_settings_v1';
const LS_HISTORY = 'upo_history_v1';
let LAST_IMAGES = [];

function getForm(){
  return {
    idea: document.getElementById('idea').value.trim(),
    negative: document.getElementById('negative').value.trim(),
    aspect_ratio: document.getElementById('ar').value,
    lighting: document.getElementById('lighting').value.trim(),
    color_grade: document.getElementById('color_grade').value.trim(),
    extra_tags: document.getElementById('extra_tags').value.trim(),
    steps: document.getElementById('steps').value.trim(),
    cfg_scale: document.getElementById('cfg').value.trim(),
    sampler: document.getElementById('sampler').value,
    seed: document.getElementById('seed').value.trim(),
    batch: document.getElementById('batch').value.trim()
  };
}
function setForm(v){
  if(!v) return;
  document.getElementById('idea').value = v.idea || '';
  document.getElementById('negative').value = v.negative || '';
  document.getElementById('ar').value = v.aspect_ratio || '16:9';
  document.getElementById('lighting').value = v.lighting || '';
  document.getElementById('color_grade').value = v.color_grade || '';
  document.getElementById('extra_tags').value = v.extra_tags || '';
  document.getElementById('steps').value = v.steps || '';
  document.getElementById('cfg').value = v.cfg_scale || '';
  document.getElementById('sampler').value = v.sampler || 'DPM++ 2M Karras';
  document.getElementById('seed').value = v.seed || '';
  document.getElementById('batch').value = v.batch || '';
}

// Presets (unchanged)
function loadAllPresets(){
  try{
    const raw = localStorage.getItem(LS_KEY);
    const map = raw ? JSON.parse(raw) : {};
    const sel = document.getElementById('presetSelect');
    sel.innerHTML = '';
    const keys = Object.keys(map).sort((a,b)=>a.localeCompare(b));
    keys.forEach(name=>{
      const opt = document.createElement('option');
      opt.value = name; opt.textContent = name;
      sel.appendChild(opt);
    });
  }catch(e){ console.warn('Failed to load presets', e); }
}
function savePreset(){
  const name = (document.getElementById('presetName').value || '').trim();
  if(!name){ alert('Name your preset first.'); return; }
  try{
    const raw = localStorage.getItem(LS_KEY);
    const map = raw ? JSON.parse(raw) : {};
    map[name] = getForm();
    localStorage.setItem(LS_KEY, JSON.stringify(map));
    loadAllPresets();
    document.getElementById('presetSelect').value = name;
  }catch(e){ alert('Could not save preset.'); }
}
function loadPreset(){
  const sel = document.getElementById('presetSelect');
  const name = sel.value;
  if(!name){ alert('No preset selected.'); return; }
  try{
    const map = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
    setForm(map[name]);
  }catch(e){ alert('Could not load preset.'); }
}
function deletePreset(){
  const sel = document.getElementById('presetSelect');
  const name = sel.value;
  if(!name){ alert('No preset selected.'); return; }
  if(!confirm(`Delete preset "${name}"?`)) return;
  try{
    const map = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
    delete map[name];
    localStorage.setItem(LS_KEY, JSON.stringify(map));
    loadAllPresets();
  }catch(e){ alert('Could not delete preset.'); }
}
function exportPresets(){
  try{
    const raw = localStorage.getItem(LS_KEY) || '{}';
    const blob = new Blob([raw], {type:'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'upo_presets.json'; a.click();
    setTimeout(()=>URL.revokeObjectURL(url), 1000);
  }catch(e){ alert('Could not export presets.'); }
}
function importPresets(){
  const input = document.createElement('input');
  input.type = 'file'; input.accept = 'application/json';
  input.onchange = (e)=>{
    const file = e.target.files[0];
    if(!file) return;
    const reader = new FileReader();
    reader.onload = ()=>{
      try{
        const incoming = JSON.parse(reader.result);
        const current = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
        const merged = Object.assign(current, incoming);
        localStorage.setItem(LS_KEY, JSON.stringify(merged));
        loadAllPresets();
        alert('Presets imported.');
      }catch(err){ alert('Invalid JSON.'); }
    };
    reader.readAsText(file);
  };
  input.click();
}

// Settings modal
function openSettings(){
  const raw = localStorage.getItem(LS_SETTINGS) || '{}';
  const s = JSON.parse(raw);
  document.getElementById('comfyHost').value = s.host || '';
  document.getElementById('workflowJson').value = s.workflow || '';
  document.getElementById('settingsModal').style.display = 'flex';
}
function closeSettings(){ document.getElementById('settingsModal').style.display = 'none'; }
function saveSettings(){
  const s = {
    host: document.getElementById('comfyHost').value.trim(),
    workflow: document.getElementById('workflowJson').value.trim()
  };
  localStorage.setItem(LS_SETTINGS, JSON.stringify(s));
  closeSettings();
}

// Helpers
function copyText(id){
  const el = document.getElementById(id);
  const txt = el?.innerText || el?.textContent || '';
  navigator.clipboard.writeText(txt);
}
function downloadText(filename, id){
  const el = document.getElementById(id);
  const txt = el?.innerText || el?.textContent || '';
  const blob = new Blob([txt], {type: 'text/plain'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  setTimeout(()=>URL.revokeObjectURL(url), 1000);
}
async function downloadZip(urls){
  if(!urls || !urls.length){ alert('No images to zip.'); return; }
  const res = await fetch('/zip', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ urls })
  });
  if(!res.ok){ alert('ZIP failed.'); return; }
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'upo_outputs.zip';
  a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
}

// History
function loadHistory(){
  try{
    return JSON.parse(localStorage.getItem(LS_HISTORY) || '[]');
  }catch(e){ return []; }
}
function saveHistory(arr){
  localStorage.setItem(LS_HISTORY, JSON.stringify(arr));
}
function addHistory(rec){
  const arr = loadHistory();
  arr.unshift(rec);
  // cap at 200 entries to avoid bloat
  if(arr.length > 200) arr.length = 200;
  saveHistory(arr);
  renderHistory();
}
function clearHistory(){
  if(!confirm('Clear all history?')) return;
  saveHistory([]);
  renderHistory();
}
function exportHistory(){
  const raw = localStorage.getItem(LS_HISTORY) || '[]';
  const blob = new Blob([raw], {type:'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = 'upo_history.json'; a.click();
  setTimeout(()=>URL.revokeObjectURL(url), 1000);
}
function renderHistory(){
  const grid = document.getElementById('historyGrid');
  const arr = loadHistory();
  grid.innerHTML = '';
  arr.forEach((h, idx)=>{
    const div = document.createElement('div');
    div.className = 'thumb';
    const img = document.createElement('img');
    img.src = (h.images && h.images[0]) || '';
    img.alt = 'generation';
    img.loading = 'lazy';
    img.style.maxWidth = '100%';
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = `[${new Date(h.ts).toLocaleString()}] seed=${h.seed} steps=${h.steps} cfg=${h.cfg_scale} ${h.sampler} ${h.width}x${h.height}`;
    const btns = document.createElement('div');
    btns.className = 'rowbtns';
    const rerun = document.createElement('button');
    rerun.className = 'secondary';
    rerun.textContent = 'Re-run';
    rerun.onclick = ()=>reRun(h);
    const dl = document.createElement('button');
    dl.className = 'secondary';
    dl.textContent = 'Download All';
    dl.onclick = ()=>downloadAll(h.images||[]);
    const zip = document.createElement('button');
    zip.className = 'secondary';
    zip.textContent = 'ZIP';
    zip.onclick = ()=>downloadZip(h.images||[]);
    btns.appendChild(rerun); btns.appendChild(dl); btns.appendChild(zip);
    div.appendChild(img); div.appendChild(meta); div.appendChild(btns);
    grid.appendChild(div);
  });
}
function downloadAll(urls){
  urls.forEach((u,i)=>{
    const a = document.createElement('a');
    a.href = u; a.download = `image_${i+1}.png`; a.click();
  });
}
function reRun(h){
  // Restore form + settings, then trigger generate
  setForm({
    idea: h.idea, negative: h.negative, aspect_ratio: h.aspect_ratio,
    lighting: h.lighting, color_grade: h.color_grade, extra_tags: h.extra_tags,
    steps: String(h.steps), cfg_scale: String(h.cfg_scale), sampler: h.sampler,
    seed: String(h.seed), batch: String(h.batch)
  });
  // show SDXL JSON again if needed: user may click Optimize to refresh
  generateComfy();
}

// Optimize
async function optimize(){
  const payload = getForm();
  const res = await fetch('/optimize', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const out = await res.json();
  document.getElementById('results').classList.remove('hidden');

  document.getElementById('unifiedPos').textContent = out.unified?.positive || '';
  document.getElementById('unifiedNeg').textContent = out.unified?.negative || '';
  document.getElementById('sdxlBox').textContent   = JSON.stringify(out.sdxl, null, 2);
  document.getElementById('comfyBox').textContent  = JSON.stringify(out.comfyui, null, 2);
  document.getElementById('mjBox').textContent     = out.midjourney || '';
  document.getElementById('pikaBox').textContent   = JSON.stringify(out.pika, null, 2);
  document.getElementById('runwayBox').textContent = JSON.stringify(out.runway, null, 2);
  document.getElementById('hintsBox').textContent  = JSON.stringify(out.hints, null, 2);

  const s = JSON.parse(localStorage.getItem(LS_SETTINGS) || '{}');
  document.getElementById('genComfyBtn').style.display = s.host ? 'inline-block' : 'none';
  document.getElementById('genImages').innerHTML = '';
  document.getElementById('zipBtn').style.display = 'none';
}

// Direct generation (ComfyUI)
async function generateComfy(){
  const s = JSON.parse(localStorage.getItem(LS_SETTINGS) || '{}');
  if(!s.host){ alert('Set your ComfyUI host in Settings.'); return; }

  let sdxlJson = {};
  try { sdxlJson = JSON.parse(document.getElementById('sdxlBox').textContent || '{}'); }
  catch(e){ alert('Invalid SDXL JSON—click Optimize again.'); return; }

  const f = getForm();
  const advanced = {
    steps: f.steps, cfg_scale: f.cfg_scale, sampler: f.sampler,
    seed: f.seed, batch: f.batch
  };

  const res = await fetch('/generate/comfy', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      host: s.host,
      workflow_override: s.workflow || '',
      sdxl: sdxlJson,
      advanced
    })
  });
  const out = await res.json();
  if(!res.ok){ alert(out.error || 'Generation failed.'); return; }

  // Render images
  const wrap = document.getElementById('genImages');
  wrap.innerHTML = '';
  (out.images || []).forEach(url=>{
    const img = document.createElement('img'); img.src = url; wrap.appendChild(img);
  });

  LAST_IMAGES = out.images || [];
  document.getElementById('zipBtn').style.display = LAST_IMAGES.length ? 'inline-block' : 'none';

  // Add to history
  try{
    const settings = sdxlJson.settings || {};
    addHistory({
      ts: Date.now(),
      images: out.images || [],
      seed: out.seed || (advanced.seed || 'random'),
      batch: Number(advanced.batch || 1),
      steps: Number(advanced.steps || settings.steps || 30),
      cfg_scale: Number(advanced.cfg_scale || settings.cfg_scale || 6.5),
      sampler: (advanced.sampler || settings.sampler || 'DPM++ 2M Karras'),
      width: Number(settings.width || 1024),
      height: Number(settings.height || 1024),
      // echo inputs for rerun
      idea: document.getElementById('idea').value.trim(),
      negative: document.getElementById('negative').value.trim(),
      aspect_ratio: document.getElementById('ar').value,
      lighting: document.getElementById('lighting').value.trim(),
      color_grade: document.getElementById('color_grade').value.trim(),
      extra_tags: document.getElementById('extra_tags').value.trim()
    });
  }catch(e){ console.warn('history add failed', e); }
}

function openSettings(){
  const raw = localStorage.getItem(LS_SETTINGS) || '{}';
  const s = JSON.parse(raw);
  document.getElementById('comfyHost').value = s.host || '';
  document.getElementById('workflowJson').value = s.workflow || '';
  document.getElementById('settingsModal').style.display = 'flex';
}
function closeSettings(){ document.getElementById('settingsModal').style.display = 'none'; }
function saveSettings(){
  const s = {
    host: document.getElementById('comfyHost').value.trim(),
    workflow: document.getElementById('workflowJson').value.trim()
  };
  localStorage.setItem(LS_SETTINGS, JSON.stringify(s));
  closeSettings();
}

document.getElementById('run').addEventListener('click', optimize);
document.addEventListener('DOMContentLoaded', ()=>{
  loadAllPresets();
  renderHistory();
});
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")

def _optimize_core(data):
    user_text = (data.get("idea") or "").strip()
    user_negative = (data.get("negative") or "").strip()
    aspect_ratio = (data.get("aspect_ratio") or "1:1").strip()
    extras = {
        "lighting": (data.get("lighting") or "").strip(),
        "color_grade": (data.get("color_grade") or "").strip(),
        "extra_tags": (data.get("extra_tags") or "").strip(),
    }
    if not user_text:
        return {"error": "Please describe your idea."}, 400

    found, subject_terms = extract_categories(user_text)
    positive = build_positive(subject_terms, found, extras)
    negative = build_negative(user_negative)

    out = {
        "unified": {"positive": positive, "negative": negative},
        "sdxl": sdxl_prompt(positive, negative, aspect_ratio),
        "comfyui": comfyui_recipe(positive, negative, aspect_ratio),
        "midjourney": midjourney_prompt(positive, aspect_ratio),
        "pika": pika_prompt(positive),
        "runway": runway_prompt(positive),
        "hints": {
            "faces": "For better faces: Add 'portrait, detailed face, sharp focus' and avoid 'bad anatomy' in negative",
            "motion": "For video: Use motion cues like 'gentle camera movement' but avoid 'warping, morphing'",
            "busy": "If output is too busy: Reduce adjectives and focus on 1-2 key elements"
        }
    }
    return out, 200

@app.route("/optimize", methods=["POST"])
def optimize():
    try:
        data = request.get_json() or {}
        result, status_code = _optimize_core(data)
        return jsonify(result), status_code
    except Exception as e:
        return jsonify({"error": f"Server error: {e}"}), 500

@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    return optimize()

def _build_default_sdxl_workflow(pos, neg, w, h, steps=30, cfg=6.5, sampler="dpmpp_2m", seed=123456789, batch_size=1):
    """
    Minimal SDXL text2img workflow_api graph. Adjust model/vae names to match your install.
    """
    return {
      "3": {"class_type": "KSampler", "inputs": {
        "seed": int(seed), "steps": int(steps), "cfg": float(cfg),
        "sampler_name": sampler, "scheduler": "karras", "denoise": 1.0,
        "model": ["21", 0], "positive": ["12", 0], "negative": ["13", 0], "latent_image": ["5", 0]
      }},
      "5": {"class_type": "EmptyLatentImage", "inputs": {"width": int(w), "height": int(h), "batch_size": int(batch_size)}},
      "7": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["22", 0]}},
      "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0]}},
      "12": {"class_type": "CLIPTextEncodeSDXL", "inputs": {"text_g": pos, "text_l": pos, "clip": ["20", 0]}},
      "13": {"class_type": "CLIPTextEncodeSDXL", "inputs": {"text_g": neg, "text_l": neg, "clip": ["20", 0]}},
      "20": {"class_type": "CLIPSetLastLayer", "inputs": {"stop_at_clip_layer": -1, "clip": ["19", 0]}},
      "21": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
      "22": {"class_type": "VAELoader", "inputs": {"vae_name": "sdxl_vae.safetensors"}},
      "19": {"class_type": "CLIPLoader", "inputs": {"clip_name": "sdxl_clip.safetensors"}}
    }

def _http_post_json(url, data):
    """Helper for POST requests with JSON."""
    req = urlreq.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    with urlreq.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode())

def _http_get_json(url):
    """Helper for GET requests returning JSON."""
    req = urlreq.Request(url)
    with urlreq.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode())

@app.route("/generate/comfy", methods=["POST"])
def generate_comfy():
    """
    Body: {
      "host": "http://127.0.0.1:8188",
      "workflow_override": "<optional workflow_api JSON string>",
      "sdxl": { "positive": "...", "negative": "...", "settings": { "width": 1344, "height": 768, "steps": 30, "cfg_scale": 6.5, "sampler": "DPM++ 2M Karras" } },
      "advanced": { "steps": "36", "cfg_scale": "6.0", "sampler": "DPM++ SDE Karras", "seed": "random", "batch": "2" }
    }
    """
    data = request.get_json(force=True)
    host = (data.get("host") or "").rstrip("/")
    if not host:
        return jsonify({"error":"Missing ComfyUI host"}), 400

    sdxl = data.get("sdxl") or {}
    positive = sdxl.get("positive") or ""
    negative = sdxl.get("negative") or ""
    settings = sdxl.get("settings") or {}
    w = int(settings.get("width", 1024))
    h = int(settings.get("height", 1024))

    # advanced overrides
    adv = data.get("advanced") or {}
    def _to_int(x, default):
        try:
            return int(str(x).strip())
        except:
            return default
    def _to_float(x, default):
        try:
            return float(str(x).strip())
        except:
            return default

    steps_default = int(settings.get("steps", 30))
    cfg_default = float(settings.get("cfg_scale", 6.5))
    sampler_default = settings.get("sampler", "DPM++ 2M Karras")
    batch_default = 1

    steps = _to_int(adv.get("steps", ""), steps_default)
    cfg = _to_float(adv.get("cfg_scale", ""), cfg_default)
    sampler_name = (adv.get("sampler") or sampler_default).strip()

    # sampler map to ComfyUI short names
    sampler_map = {
        "DPM++ 2M Karras": "dpmpp_2m", "DPM++ 2M": "dpmpp_2m",
        "DPM++ SDE Karras": "dpmpp_sde", "Euler a": "euler_ancestral"
    }
    sampler = sampler_map.get(sampler_name, "dpmpp_2m")

    # seed handling
    seed_raw = (adv.get("seed") or "").strip().lower()
    if seed_raw in ("", "random", "rnd"):
        seed = random.randint(1, 2**31 - 1)
    else:
        try: seed = int(seed_raw)
        except: seed = random.randint(1, 2**31 - 1)

    batch = max(1, min(8, _to_int(adv.get("batch", ""), batch_default)))

    # Build graph (or inject override)
    wf_override_raw = data.get("workflow_override") or ""
    if wf_override_raw.strip():
        try:
            g = json.loads(wf_override_raw)
            for k, node in g.items():
                ctype = (node.get("class_type") or "").lower()
                if ctype.startswith("cliptextencode"):
                    for key in ("text","text_g","text_l"):
                        if key in node.get("inputs", {}):
                            node["inputs"][key] = positive
                if ctype.startswith("emptylatentimage"):
                    node["inputs"]["width"]  = w
                    node["inputs"]["height"] = h
                    node["inputs"]["batch_size"] = int(batch)
                if ctype == "ksampler":
                    node["inputs"]["seed"] = int(seed)
                    node["inputs"]["steps"] = int(steps)
                    node["inputs"]["cfg"] = float(cfg)
                    node["inputs"]["sampler_name"] = sampler
                    node["inputs"]["scheduler"] = "karras"
            graph = g
        except Exception as e:
            return jsonify({"error": f"Invalid workflow_override JSON: {e}"}), 400
    else:
        graph = _build_default_sdxl_workflow(
            positive, negative, w, h,
            steps=steps, cfg=cfg, sampler=sampler,
            seed=seed, batch_size=batch
        )

    # queue & poll
    try:
        out = _http_post_json(f"{host}/prompt", {"prompt": graph})
        prompt_id = out.get("prompt_id")
        if not prompt_id:
            return jsonify({"error":"No prompt_id returned from ComfyUI"}), 502

        start = time.time()
        image_urls = []
        while time.time() - start < 180:
            hist = _http_get_json(f"{host}/history/{prompt_id}")
            for node_id, node_data in (hist.get(prompt_id) or {}).get("outputs", {}).items():
                if "images" in node_data:
                    for img in node_data["images"]:
                        fname = img.get("filename")
                        subf = img.get("subfolder","")
                        if fname:
                            url = f"{host}/view?filename={fname}&subfolder={subf}&type=output"
                            if url not in image_urls:
                                image_urls.append(url)
            if image_urls:
                break
            time.sleep(1.2)

        if not image_urls:
            return jsonify({"error":"Timed out waiting for images"}), 504

        return jsonify({"ok": True, "images": image_urls, "seed": seed, "batch": batch}), 200

    except (URLError, HTTPError) as e:
        return jsonify({"error": f"Network error contacting ComfyUI: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500

@app.post("/zip")
def zip_images():
    """
    Body: { "urls": ["http://host/view?filename=...","..."] }
    Returns: application/zip of all images (skips any that fail)
    """
    data = request.get_json(force=True)
    urls = data.get("urls") or []
    if not isinstance(urls, list) or not urls:
        return jsonify({"error": "No urls provided"}), 400

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, u in enumerate(urls):
            try:
                with urlreq.urlopen(u, timeout=20) as resp:
                    content = resp.read()
                # naive extension guess; ComfyUI serves PNG by default
                z.writestr(f"image_{i+1}.png", content)
            except Exception:
                # skip errors silently to still return a ZIP for the rest
                continue
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=upo_outputs.zip"}
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)