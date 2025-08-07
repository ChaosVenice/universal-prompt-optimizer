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

    # Subject terms are remaining meaningful words after removing style terms and stopwords
    used_style_words = set()
    for style_list in found.values():
        for term in style_list:
            used_style_words.update(tokenize(term))

    subject_terms = [w for w in words 
                    if w not in STOPWORDS 
                    and w not in used_style_words 
                    and len(w) > 2]
    
    return found, subject_terms

def build_positive(subject_terms, found_styles, extras):
    """Construct optimized positive prompt with strict order: quality → subject → style → lighting → composition → mood → color grade → extra tags."""
    parts = []
    
    # 1. Quality descriptors (always first)
    if found_styles["quality"]:
        parts.extend(found_styles["quality"][:2])  # Limit to avoid redundancy
    
    # 2. Core subject
    parts.extend(subject_terms[:8])  # Main nouns/descriptors from user input
    
    # 3. Art style and photography techniques
    parts.extend(found_styles["art_styles"][:2])
    parts.extend(found_styles["photography"][:2])
    
    # 4. Lighting (priority for user-specified or detected)
    lighting_terms = []
    if extras.get("lighting"):
        lighting_terms.extend(tokenize(extras["lighting"]))
    lighting_terms.extend(found_styles["lighting"][:2])
    parts.extend(lighting_terms[:2])
    
    # 5. Composition
    parts.extend(found_styles["composition"][:2])
    
    # 6. Mood
    parts.extend(found_styles["mood"][:2])
    
    # 7. Color grade (priority for user-specified)
    color_terms = []
    if extras.get("color_grade"):
        color_terms.extend(tokenize(extras["color_grade"]))
    color_terms.extend(found_styles["color_grades"][:2])
    parts.extend(color_terms[:2])
    
    # 8. Extra tags (user-specified)
    if extras.get("extra_tags"):
        parts.extend(tokenize(extras["extra_tags"])[:3])
    
    # Deduplicate and join
    final_parts = dedup_preserve(parts)
    result = ", ".join(final_parts)
    return clamp_length(result)

def build_negative(user_negative):
    """Construct enhanced negative prompt covering anatomy/structure, artifacts/quality, branding/text, style pitfalls."""
    parts = list(NEGATIVE_DEFAULT)  # Start with comprehensive defaults
    
    if user_negative:
        user_terms = [term.strip() for term in user_negative.split(",") if term.strip()]
        parts.extend(user_terms)
    
    final_parts = dedup_preserve(parts)
    result = ", ".join(final_parts)
    return clamp_length(result)

def sdxl_prompt(positive, negative, aspect_ratio):
    """Generate SDXL-optimized prompt configuration."""
    w, h = AR_TO_RES.get(aspect_ratio, (1024, 1024))
    return {
        "positive": positive,
        "negative": negative,
        "settings": {
            "width": w, "height": h, "steps": 30, "cfg_scale": 6.5, 
            "sampler": "DPM++ 2M Karras", "scheduler": "karras", "model": "SDXL"
        }
    }

def comfyui_recipe(positive, negative, aspect_ratio):
    """Generate ComfyUI workflow configuration with execution tips."""
    w, h = AR_TO_RES.get(aspect_ratio, (1024, 1024))
    return {
        "positive": positive,
        "negative": negative,
        "settings": {"width": w, "height": h, "steps": 30, "cfg_scale": 6.5, "sampler": "dpmpp_2m"},
        "execution_tips": [
            "Use SDXL base model for best results",
            "Enable 'Tiled VAE' if getting VRAM errors", 
            "Consider refiner model for final 20% of steps"
        ]
    }

def midjourney_prompt(positive, aspect_ratio):
    """Generate Midjourney v6 prompt with proper flags."""
    ar_map = {"1:1": "1:1", "16:9": "16:9", "9:16": "9:16", "2:3": "2:3", "3:2": "3:2"}
    ar = ar_map.get(aspect_ratio, "1:1")
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
.progress{margin-top:10px;background:#0f141c;border:1px solid #27405e;border-radius:10px;overflow:hidden;height:14px}
.progress .bar{height:100%;width:0%}
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
        <button class="secondary" id="genComfyBtn" onclick="generateComfyAsync()">Generate (ComfyUI)</button>
        <button class="secondary" id="zipBtn" style="display:none" onclick="downloadZip(LAST_IMAGES)">Download ZIP</button>
      </div>
      <div id="sdxlBox" class="box section kv"></div>

      <div id="progressWrap" class="hidden">
        <div class="rowbtns">
          <button class="danger" id="cancelBtn" onclick="cancelGeneration()">Cancel</button>
          <span id="progressText" class="small"></span>
        </div>
        <div class="progress"><div id="progressBar" class="bar"></div></div>
      </div>

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
let POLL = null;
let CURRENT = {host:'', pid:'', expected:1, started:0};

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

// Presets/history helpers (unchanged from your current file) …
function loadAllPresets(){ try{ const raw=localStorage.getItem(LS_KEY); const map=raw?JSON.parse(raw):{}; const sel=document.getElementById('presetSelect'); sel.innerHTML=''; Object.keys(map).sort((a,b)=>a.localeCompare(b)).forEach(name=>{ const opt=document.createElement('option'); opt.value=name; opt.textContent=name; sel.appendChild(opt); }); }catch(e){} }
function savePreset(){ const name=(document.getElementById('presetName').value||'').trim(); if(!name){alert('Name your preset first.');return;} try{ const raw=localStorage.getItem(LS_KEY); const map=raw?JSON.parse(raw):{}; map[name]=getForm(); localStorage.setItem(LS_KEY, JSON.stringify(map)); loadAllPresets(); document.getElementById('presetSelect').value=name; }catch(e){ alert('Could not save preset.'); } }
function loadPreset(){ const sel=document.getElementById('presetSelect'); const name=sel.value; if(!name){alert('No preset selected.');return;} try{ const map=JSON.parse(localStorage.getItem(LS_KEY)||'{}'); setForm(map[name]); }catch(e){ alert('Could not load preset.'); } }
function deletePreset(){ const sel=document.getElementById('presetSelect'); const name=sel.value; if(!name){alert('No preset selected.');return;} if(!confirm(`Delete preset "${name}"?`)) return; try{ const map=JSON.parse(localStorage.getItem(LS_KEY)||'{}'); delete map[name]; localStorage.setItem(LS_KEY, JSON.stringify(map)); loadAllPresets(); }catch(e){ alert('Could not delete preset.'); } }
function exportPresets(){ try{ const raw=localStorage.getItem(LS_KEY)||'{}'; const blob=new Blob([raw],{type:'application/json'}); const url=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download='upo_presets.json'; a.click(); setTimeout(()=>URL.revokeObjectURL(url), 1000); }catch(e){ alert('Could not export presets.'); } }
function importPresets(){ const input=document.createElement('input'); input.type='file'; input.accept='application/json'; input.onchange=(e)=>{ const file=e.target.files[0]; if(!file) return; const reader=new FileReader(); reader.onload=()=>{ try{ const incoming=JSON.parse(reader.result); const current=JSON.parse(localStorage.getItem(LS_KEY)||'{}'); const merged=Object.assign(current,incoming); localStorage.setItem(LS_KEY, JSON.stringify(merged)); loadAllPresets(); alert('Presets imported.'); }catch(err){ alert('Invalid JSON.'); } }; reader.readAsText(file); }; input.click(); }

function loadHistory(){ try{ return JSON.parse(localStorage.getItem('upo_history_v1')||'[]'); }catch(e){ return []; } }
function saveHistory(arr){ localStorage.setItem('upo_history_v1', JSON.stringify(arr)); }
function addHistory(rec){ const arr=loadHistory(); arr.unshift(rec); if(arr.length>200) arr.length=200; saveHistory(arr); renderHistory(); }
function clearHistory(){ if(!confirm('Clear all history?')) return; saveHistory([]); renderHistory(); }
function exportHistory(){ const raw=localStorage.getItem('upo_history_v1')||'[]'; const blob=new Blob([raw],{type:'application/json'}); const url=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download='upo_history.json'; a.click(); setTimeout(()=>URL.revokeObjectURL(url), 1000); }
function renderHistory(){
  const grid=document.getElementById('historyGrid'); const arr=loadHistory(); grid.innerHTML='';
  arr.forEach((h)=>{
    const div=document.createElement('div'); div.className='thumb';
    const img=document.createElement('img'); img.src=(h.images&&h.images[0])||''; img.alt='generation'; img.loading='lazy'; img.style.maxWidth='100%';
    const meta=document.createElement('div'); meta.className='meta'; meta.textContent=`[${new Date(h.ts).toLocaleString()}] seed=${h.seed} steps=${h.steps} cfg=${h.cfg_scale} ${h.sampler} ${h.width}x${h.height}`;
    const btns=document.createElement('div'); btns.className='rowbtns';
    const rerun=document.createElement('button'); rerun.className='secondary'; rerun.textContent='Re-run'; rerun.onclick=()=>reRun(h);
    const dl=document.createElement('button'); dl.className='secondary'; dl.textContent='Download All'; dl.onclick=()=>downloadAll(h.images||[]);
    const zip=document.createElement('button'); zip.className='secondary'; zip.textContent='ZIP'; zip.onclick=()=>downloadZip(h.images||[]);
    btns.appendChild(rerun); btns.appendChild(dl); btns.appendChild(zip);
    div.appendChild(img); div.appendChild(meta); div.appendChild(btns); grid.appendChild(div);
  });
}
function downloadAll(urls){ urls.forEach((u,i)=>{ const a=document.createElement('a'); a.href=u; a.download=`image_${i+1}.png`; a.click(); }); }

function openSettings(){ const raw=localStorage.getItem(LS_SETTINGS)||'{}'; const s=JSON.parse(raw); document.getElementById('comfyHost').value=s.host||''; document.getElementById('workflowJson').value=s.workflow||''; document.getElementById('settingsModal').style.display='flex'; }
function closeSettings(){ document.getElementById('settingsModal').style.display='none'; }
function saveSettings(){ const s={ host:document.getElementById('comfyHost').value.trim(), workflow:document.getElementById('workflowJson').value.trim() }; localStorage.setItem(LS_SETTINGS, JSON.stringify(s)); closeSettings(); }

function copyText(id){ const el=document.getElementById(id); const txt=el?.innerText||el?.textContent||''; navigator.clipboard.writeText(txt); }
function downloadText(filename,id){ const el=document.getElementById(id); const txt=el?.innerText||el?.textContent||''; const blob=new Blob([txt],{type:'text/plain'}); const url=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download=filename; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); }
async function downloadZip(urls){ if(!urls||!urls.length){alert('No images to zip.');return;} const res=await fetch('/zip',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls})}); if(!res.ok){alert('ZIP failed.');return;} const blob=await res.blob(); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='upo_outputs.zip'; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),1000); }

async function optimize(){
  const payload=getForm();
  const res=await fetch('/optimize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const out=await res.json();
  document.getElementById('results').classList.remove('hidden');
  document.getElementById('unifiedPos').textContent=out.unified?.positive||'';
  document.getElementById('unifiedNeg').textContent=out.unified?.negative||'';
  document.getElementById('sdxlBox').textContent=JSON.stringify(out.sdxl,null,2);
  document.getElementById('comfyBox').textContent=JSON.stringify(out.comfyui,null,2);
  document.getElementById('mjBox').textContent=out.midjourney||'';
  document.getElementById('pikaBox').textContent=JSON.stringify(out.pika,null,2);
  document.getElementById('runwayBox').textContent=JSON.stringify(out.runway,null,2);
  document.getElementById('hintsBox').textContent=JSON.stringify(out.hints,null,2);
  const s=JSON.parse(localStorage.getItem(LS_SETTINGS)||'{}');
  document.getElementById('genComfyBtn').style.display=s.host?'inline-block':'none';
  document.getElementById('genImages').innerHTML=''; document.getElementById('zipBtn').style.display='none';
  hideProgress();
}
document.getElementById('run').addEventListener('click', optimize);

function showProgress(){ document.getElementById('progressWrap').classList.remove('hidden'); setProgress(0,'Queued...'); }
function hideProgress(){ document.getElementById('progressWrap').classList.add('hidden'); setProgress(0,''); }
function setProgress(pct, text){ const bar=document.getElementById('progressBar'); bar.style.width=(pct||0)+'%'; bar.style.background = 'linear-gradient(90deg,#2f6df6,#36c3ff)'; document.getElementById('progressText').textContent=text||''; }

async function generateComfyAsync(){
  const s=JSON.parse(localStorage.getItem(LS_SETTINGS)||'{}');
  if(!s.host){ alert('Set your ComfyUI host in Settings.'); return; }
  let sdxlJson={}; try{ sdxlJson=JSON.parse(document.getElementById('sdxlBox').textContent||'{}'); }catch(e){ alert('Invalid SDXL JSON. Run "Optimize" first.'); return; }
  const payload={host:s.host,workflow_override:s.workflow||'',sdxl:sdxlJson,advanced:{steps:document.getElementById('steps').value.trim(),cfg_scale:document.getElementById('cfg').value.trim(),sampler:document.getElementById('sampler').value,seed:document.getElementById('seed').value.trim(),batch:document.getElementById('batch').value.trim()}};
  try{
    showProgress(); LAST_IMAGES=[];
    const res=await fetch('/generate/comfy_async',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const out=await res.json();
    if(!res.ok||out.error){ hideProgress(); alert(`Queue failed: ${out.error||'Unknown error'}`); return; }
    CURRENT={host:s.host,pid:out.prompt_id,expected:out.batch||1,started:Date.now()};
    setProgress(5,`Queued (${out.prompt_id})`);
    POLL=setInterval(pollStatus,1500);
  }catch(err){ hideProgress(); alert(`Network error: ${err.message}`); }
}

async function pollStatus(){
  if(!CURRENT.pid) return;
  try{
    const url=`/generate/comfy_status?host=${encodeURIComponent(CURRENT.host)}&pid=${encodeURIComponent(CURRENT.pid)}`;
    const res=await fetch(url);
    const out=await res.json();
    if(!res.ok||out.error){ clearInterval(POLL); hideProgress(); alert(`Status error: ${out.error||'Unknown'}`); return; }
    const found=out.images||[]; const elapsed=((Date.now()-CURRENT.started)/1000).toFixed(0);
    if(found.length>=CURRENT.expected){
      clearInterval(POLL); POLL=null; hideProgress();
      LAST_IMAGES=found; const wrap=document.getElementById('genImages'); wrap.innerHTML='';
      found.forEach(url=>{const img=document.createElement('img'); img.src=url; img.alt='Generated image'; img.loading='lazy'; wrap.appendChild(img);});
      document.getElementById('zipBtn').style.display='inline-block';
      const form=getForm(); const historyRecord={ts:Date.now(),idea:form.idea,negative:form.negative,aspect_ratio:form.aspect_ratio,lighting:form.lighting,color_grade:form.color_grade,extra_tags:form.extra_tags,steps:form.steps||'30',cfg_scale:form.cfg_scale||'6.5',sampler:form.sampler||'DPM++ 2M Karras',seed:form.seed||'random',batch:form.batch||'1',width:1024,height:1024,images:LAST_IMAGES}; addHistory(historyRecord);
    }else{
      const pct=Math.min(95,10+((found.length/CURRENT.expected)*85));
      setProgress(pct,`Generating ${found.length}/${CURRENT.expected} (${elapsed}s)`);
    }
  }catch(err){ clearInterval(POLL); hideProgress(); alert(`Poll error: ${err.message}`); }
}

function cancelGeneration(){ if(POLL){ clearInterval(POLL); POLL=null; } hideProgress(); fetch('/generate/comfy_cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt_id:CURRENT.pid})}); CURRENT={host:'',pid:'',expected:1,started:0}; }

function reRun(h){ if(!h) return; setForm({idea:h.idea||'',negative:h.negative||'',aspect_ratio:h.aspect_ratio||'16:9',lighting:h.lighting||'',color_grade:h.color_grade||'',extra_tags:h.extra_tags||'',steps:h.steps||'',cfg_scale:h.cfg_scale||'',sampler:h.sampler||'DPM++ 2M Karras',seed:h.seed||'',batch:h.batch||''}); optimize(); }

document.addEventListener('DOMContentLoaded',()=>{ loadAllPresets(); renderHistory(); });
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

# Map pretty sampler names to ComfyUI short names
_SAMPLER_MAP = {
    "DPM++ 2M Karras": "dpmpp_2m", "DPM++ 2M": "dpmpp_2m",
    "DPM++ SDE Karras": "dpmpp_sde", "Euler a": "euler_ancestral"
}

@app.route("/generate/comfy_async", methods=["POST"])
def generate_comfy_async():
    """
    Queues a ComfyUI job and returns prompt_id immediately for polling.
    Body: {
      "host": "http://127.0.0.1:8188",
      "workflow_override": "<optional JSON string>",
      "sdxl": {positive, negative, settings:{width,height,steps,cfg_scale,sampler}},
      "advanced": {steps,cfg_scale,sampler,seed,batch}
    }
    """
    data = request.get_json(force=True)
    host = (data.get("host") or "").rstrip("/")
    if not host:
        return jsonify({"error":"Missing ComfyUI host"}), 400

    sdxl = data.get("sdxl") or {}
    pos = sdxl.get("positive") or ""
    neg = sdxl.get("negative") or ""
    st  = sdxl.get("settings") or {}
    w = int(st.get("width", 1024)); h = int(st.get("height", 1024))
    steps_default = int(st.get("steps", 30))
    cfg_default   = float(st.get("cfg_scale", 6.5))
    sampler_name  = (st.get("sampler") or "DPM++ 2M Karras").strip()

    adv = data.get("advanced") or {}
    def _i(x, d): 
        try: return int(str(x).strip())
        except: return d
    def _f(x, d): 
        try: return float(str(x).strip())
        except: return d

    steps = _i(adv.get("steps",""), steps_default)
    cfg   = _f(adv.get("cfg_scale",""), cfg_default)
    sampler = _SAMPLER_MAP.get(adv.get("sampler", sampler_name), "dpmpp_2m")
    seed_raw = str(adv.get("seed","")).lower().strip()
    seed = random.randint(1, 2**31 - 1) if seed_raw in ("", "random", "rnd") else _i(seed_raw, random.randint(1, 2**31 - 1))
    batch = max(1, min(8, _i(adv.get("batch",""), 1)))

    wf_override_raw = data.get("workflow_override") or ""
    if wf_override_raw.strip():
        try:
            g = json.loads(wf_override_raw)
            for _, node in g.items():
                c = (node.get("class_type") or "").lower()
                if c.startswith("cliptextencode"):
                    for key in ("text","text_g","text_l"):
                        if key in node.get("inputs", {}):
                            node["inputs"][key] = pos
                if c.startswith("emptylatentimage"):
                    node["inputs"]["width"] = w
                    node["inputs"]["height"] = h
                    node["inputs"]["batch_size"] = int(batch)
                if c == "ksampler":
                    node["inputs"]["seed"] = int(seed)
                    node["inputs"]["steps"] = int(steps)
                    node["inputs"]["cfg"] = float(cfg)
                    node["inputs"]["sampler_name"] = sampler
                    node["inputs"]["scheduler"] = "karras"
            graph = g
        except Exception as e:
            return jsonify({"error": f"Invalid workflow_override JSON: {e}"}), 400
    else:
        graph = _build_default_sdxl_workflow(pos, neg, w, h, steps=steps, cfg=cfg, sampler=sampler, seed=seed, batch_size=batch)

    try:
        out = _http_post_json(f"{host}/prompt", {"prompt": graph})
        pid = out.get("prompt_id")
        if not pid:
            return jsonify({"error":"No prompt_id from ComfyUI"}), 502
        return jsonify({"ok": True, "prompt_id": pid, "seed": seed, "batch": batch, "width": w, "height": h}), 200
    except Exception as e:
        return jsonify({"error": f"Queue error: {e}"}), 502

@app.route("/generate/comfy_status", methods=["GET"])
def generate_comfy_status():
    """
    Query: host=http://127.0.0.1:8188&pid=<prompt_id>
    Returns images found so far (if any).
    """
    host = (request.args.get("host") or "").rstrip("/")
    pid  = request.args.get("pid") or ""
    if not host or not pid:
        return jsonify({"error":"Missing host or pid"}), 400
    try:
        hist = _http_get_json(f"{host}/history/{pid}")
        images = []
        entry = hist.get(pid) or {}
        for _, node_out in (entry.get("outputs") or {}).items():
            if "images" in node_out:
                for img in node_out["images"]:
                    fname = img.get("filename")
                    subf  = img.get("subfolder","")
                    if fname:
                        images.append(f"{host}/view?filename={fname}&subfolder={subf}&type=output")
        return jsonify({"images": images, "done": bool(images)}), 200
    except Exception as e:
        return jsonify({"error": f"Status error: {e}"}), 502

@app.route("/generate/comfy_cancel", methods=["POST"])
def generate_comfy_cancel():
    """
    Soft-cancel: client stops polling. We return OK immediately.
    (Some ComfyUI builds support queue management via API, but it's not guaranteed.)
    Body: { "prompt_id": "..." }
    """
    return jsonify({"ok": True}), 200

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
    pos = sdxl.get("positive") or ""
    neg = sdxl.get("negative") or ""
    st  = sdxl.get("settings") or {}
    w = int(st.get("width", 1024))
    h = int(st.get("height", 1024))
    steps_default = int(st.get("steps", 30))
    cfg_default   = float(st.get("cfg_scale", 6.5))
    sampler_name  = (st.get("sampler") or "DPM++ 2M Karras").strip()

    # Advanced overrides
    adv = data.get("advanced") or {}
    def _i(x, d): 
        try: return int(str(x).strip())
        except: return d
    def _f(x, d): 
        try: return float(str(x).strip())
        except: return d

    steps = _i(adv.get("steps",""), steps_default)
    cfg   = _f(adv.get("cfg_scale",""), cfg_default)
    sampler = _SAMPLER_MAP.get(adv.get("sampler", sampler_name), "dpmpp_2m")
    
    seed_raw = str(adv.get("seed","")).lower().strip()
    seed = random.randint(1, 2**31 - 1) if seed_raw in ("", "random", "rnd") else _i(seed_raw, random.randint(1, 2**31 - 1))
    
    batch = max(1, min(8, _i(adv.get("batch",""), 1)))

    # Custom workflow check
    wf_override_raw = data.get("workflow_override") or ""
    if wf_override_raw.strip():
        try:
            g = json.loads(wf_override_raw)
            # Basic text replacements
            for _, node in g.items():
                c = (node.get("class_type") or "").lower()
                if c.startswith("cliptextencode"):
                    for key in ("text","text_g","text_l"):
                        if key in node.get("inputs", {}):
                            node["inputs"][key] = pos
                if c.startswith("emptylatentimage"):
                    node["inputs"]["width"] = w
                    node["inputs"]["height"] = h
                    node["inputs"]["batch_size"] = int(batch)
                if c == "ksampler":
                    node["inputs"]["seed"] = int(seed)
                    node["inputs"]["steps"] = int(steps)
                    node["inputs"]["cfg"] = float(cfg)
                    node["inputs"]["sampler_name"] = sampler
                    node["inputs"]["scheduler"] = "karras"
            graph = g
        except Exception as e:
            return jsonify({"error": f"Invalid workflow_override JSON: {e}"}), 400
    else:
        graph = _build_default_sdxl_workflow(pos, neg, w, h, steps=steps, cfg=cfg, sampler=sampler, seed=seed, batch_size=batch)

    try:
        out = _http_post_json(f"{host}/prompt", {"prompt": graph})
        pid = out.get("prompt_id")
        if not pid:
            return jsonify({"error":"No prompt_id from ComfyUI"}), 502

        # Poll for results (blocking)
        start_time = time.time()
        max_wait = 120  # 2 minutes timeout
        images = []
        
        while time.time() - start_time < max_wait:
            try:
                hist = _http_get_json(f"{host}/history/{pid}")
                entry = hist.get(pid) or {}
                for _, node_out in (entry.get("outputs") or {}).items():
                    if "images" in node_out:
                        for img in node_out["images"]:
                            fname = img.get("filename")
                            subf  = img.get("subfolder","")
                            if fname:
                                images.append(f"{host}/view?filename={fname}&subfolder={subf}&type=output")
                
                if images:
                    return jsonify({
                        "images": images,
                        "seed": seed,
                        "steps": steps,
                        "cfg_scale": cfg,
                        "sampler": sampler,
                        "batch": batch,
                        "width": w,
                        "height": h
                    }), 200
                
                time.sleep(1)
            except Exception:
                time.sleep(1)
                continue
        
        return jsonify({"error": "Generation timed out. Check ComfyUI console."}), 408
    
    except Exception as e:
        return jsonify({"error": f"ComfyUI error: {e}"}), 502

@app.route("/zip", methods=["POST"])
def create_zip():
    """Create ZIP archive from image URLs"""
    try:
        data = request.get_json() or {}
        urls = data.get("urls", [])
        
        if not urls:
            return jsonify({"error": "No URLs provided"}), 400
        
        # Create in-memory ZIP
        zip_buffer = io.BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for i, url in enumerate(urls):
                try:
                    # Fetch image
                    req = urlreq.Request(url)
                    with urlreq.urlopen(req, timeout=10) as response:
                        image_data = response.read()
                    
                    # Add to ZIP with filename
                    filename = f"image_{i+1:02d}.png"
                    zip_file.writestr(filename, image_data)
                    
                except Exception as e:
                    # Skip failed images but continue with others
                    print(f"Failed to fetch {url}: {e}")
                    continue
        
        zip_buffer.seek(0)
        
        return Response(
            zip_buffer.getvalue(),
            mimetype='application/zip',
            headers={
                'Content-Disposition': 'attachment; filename=upo_outputs.zip',
                'Content-Length': str(len(zip_buffer.getvalue()))
            }
        )
        
    except Exception as e:
        return jsonify({"error": f"ZIP creation failed: {e}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)