import re
import json
import os
from textwrap import dedent
from flask import Flask, request, jsonify, Response

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret-key-change-in-production")

# ---------------------------
# Heuristics & Dictionaries
# ---------------------------
STYLE_KEYWORDS = {
    "photography": [
        "photo", "photograph", "dslr", "cinematic", "portrait", "macro",
        "wide angle", "telephoto", "bokeh", "studio lighting", "natural light"
    ],
    "art_styles": [
        "oil painting", "watercolor", "ink", "charcoal", "pixel art",
        "low poly", "isometric", "cyberpunk", "steampunk", "fantasy",
        "sci-fi", "surreal", "minimalist", "brutalist", "vaporwave", "noir",
        "3d render", "octane render", "unreal engine", "concept art"
    ],
    "quality": [
        "masterpiece", "best quality", "highly detailed", "hyperrealistic",
        "photorealistic", "8k", "sharp focus", "soft focus",
        "dramatic lighting", "volumetric light", "global illumination"
    ],
    "mood": [
        "moody", "dramatic", "serene", "romantic", "mysterious",
        "ominous", "whimsical", "cozy", "epic", "elegant", "gritty",
        "hopeful", "melancholic", "triumphant"
    ],
    "composition": [
        "rule of thirds", "centered", "symmetry", "leading lines",
        "overhead", "dutch angle", "close-up", "full body", "medium shot"
    ],
}

NEGATIVE_DEFAULT = [
    # anatomy/structure
    "bad anatomy", "wrong anatomy", "extra fingers", "missing fingers",
    "extra limbs", "disconnected limbs", "floating limbs", "mutated hands",
    "two heads", "out of frame",
    # artifacts/quality
    "lowres", "blurry", "jpeg artifacts", "oversaturated", "banding",
    "posterization", "noisy", "grainy", "deformed", "poorly drawn",
    "duplicate",
    # branding/text
    "watermark", "signature", "logo", "text",
    # style issues
    "plastic skin", "doll-like", "uncanny valley", "overexposed",
    "underexposed", "harsh lighting"
]

STOPWORDS = set("""
a an the of and to in on at by for with from into over under between
is are was were be been being do does did have has had can will would should
this that these those as if then than so such very really just it its it's
""".split())

AR_TO_RES = {
    "1:1":  (1024, 1024),
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "2:3":  (832, 1216),
    "3:2":  (1216, 832),
}

def tokenize(text: str):
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

def clamp_length(s: str, max_chars=850):
    """Prevent overly long prompts that degrade quality."""
    return (s[:max_chars] + "…") if len(s) > max_chars else s

def extract_categories(user_text: str):
    """Extract style keywords and subject terms from user input."""
    words = tokenize(user_text)
    text = user_text.lower()

    found = {k: [] for k in STYLE_KEYWORDS}
    for k, arr in STYLE_KEYWORDS.items():
        for style in arr:
            if style in text:
                found[k].append(style)

    # subject terms: nouns-ish, remove style/quality/mood lists and stopwords
    flat = set([w for arr in STYLE_KEYWORDS.values() for w in arr])
    subject_terms = [w for w in words if w not in flat and w not in STOPWORDS and len(w) > 2]
    subject_terms = dedup_preserve(subject_terms)[:14]
    # prune pure numbers-only tokens (often useless)
    subject_terms = [t for t in subject_terms if not re.fullmatch(r"\d+(:\d+)?", t)]
    return found, subject_terms

def build_positive(subject_terms, found, extras):
    """Construct optimized positive prompt with strict order: quality → subject → style → lighting → composition → mood → color grade → extra tags."""
    # Strict order as specified
    quality = ", ".join(dedup_preserve(found["quality"][:3])) or "masterpiece, best quality, highly detailed"
    subject = ", ".join(subject_terms[:5]) if subject_terms else "primary subject"
    style = ", ".join(dedup_preserve(found["art_styles"][:2] + found["photography"][:2]))
    lighting = extras.get("lighting", "").strip()
    comp = ", ".join(dedup_preserve(found["composition"][:2]))
    mood = ", ".join(dedup_preserve(found["mood"][:2]))
    color_grade = extras.get("color_grade", "").strip()
    extra_tags = extras.get("extra_tags", "").strip()

    # Build in exact order, skip empty sections
    parts = []
    if quality: parts.append(quality)
    if subject: parts.append(subject)
    if style: parts.append(style)
    if lighting: parts.append(lighting)
    if comp: parts.append(comp)
    if mood: parts.append(mood)
    if color_grade: parts.append(color_grade)
    if extra_tags: parts.append(extra_tags)

    # Clean and clamp to 800-900 chars
    pos = ", ".join([p.strip(", ").replace("  ", " ") for p in parts if p])
    pos = re.sub(r"(, ){2,}", ", ", pos)
    return clamp_length(pos, 850)

def build_negative(user_negative: str):
    """Construct enhanced negative prompt covering anatomy/structure, artifacts/quality, branding/text, style pitfalls."""
    user_list = []
    if user_negative:
        user_list = [x.strip() for x in user_negative.split(",") if x.strip()]
    neg = dedup_preserve(NEGATIVE_DEFAULT + user_list)
    neg = ", ".join(neg)
    return clamp_length(neg, 850)

def sdxl_prompt(positive, negative, ar):
    """Generate SDXL-optimized prompt configuration."""
    w, h = AR_TO_RES.get(ar, (1024, 1024))
    return {
        "positive": positive,
        "negative": negative,
        "settings": {
            "model": "SDXL 1.0",
            "steps": 30,
            "sampler": "DPM++ 2M Karras",
            "cfg_scale": 6.5,
            "width": w,
            "height": h,
            "hires_fix": True,
            "refiner": True
        }
    }

def comfyui_recipe(positive, negative, ar):
    """Generate ComfyUI workflow configuration with execution tips."""
    w, h = AR_TO_RES.get(ar, (1024, 1024))
    return {
        "positive": positive,
        "negative": negative,
        "nodes_hint": {
            "base_model": "sdxl_base_1.0",
            "refiner": "sdxl_refiner_1.0",
            "sampler": "DPM++ 2M Karras",
            "scheduler": "karras",
            "cfg": 6.5,
            "steps": 30,
            "width": w,
            "height": h
        },
        "tips": "If faces or hands break: lower CFG to 5.5–6.0, increase steps to 36–40."
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
textarea::placeholder,input::placeholder{color:#627a91}.row{display:flex;gap:12px;margin-top:12px;flex-wrap:wrap}.col{flex:1;min-width:260px}
button{margin-top:10px;padding:10px 14px;background:#2f6df6;border:none;border-radius:10px;color:#fff;font-weight:600;cursor:pointer}
button.secondary{background:#1e293b}button.danger{background:#9b1c1c}
button:hover{filter:brightness(1.05)}
.results{margin-top:22px}.hidden{display:none}
.box{background:#0f141c;border:1px solid #1f2a3a;padding:12px;border-radius:10px;white-space:pre-wrap}
.section{margin-top:14px}.rowbtns{display:flex;gap:8px;flex-wrap:wrap}
.kv{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px;color:#bcd0e3}
small,.small{font-size:12px;color:#7f93a7}.badge{display:inline-block;padding:4px 8px;border:1px solid #2d3b4e;border-radius:999px;margin-right:6px;color:#a9bdd4}
footer{margin-top:24px;text-align:center;color:#6f859c;font-size:12px}
h3{margin:8px 0}
select, input[type="text"] {height:40px}
textarea {min-height:110px}
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

    <button id="run">Optimize</button>
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
    <small>Presets store: idea, negative, aspect ratio, lighting, color grade, extra tags. Saved locally in your browser.</small>
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
      </div>
      <div id="sdxlBox" class="box section kv"></div>

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

  <footer>Seed tip: lock a seed for reproducibility; vary only seed to explore variants without wrecking the look.</footer>
</div>
<script>
const LS_KEY = 'upo_presets_v1';

function getForm(){
  return {
    idea: document.getElementById('idea').value.trim(),
    negative: document.getElementById('negative').value.trim(),
    aspect_ratio: document.getElementById('ar').value,
    lighting: document.getElementById('lighting').value.trim(),
    color_grade: document.getElementById('color_grade').value.trim(),
    extra_tags: document.getElementById('extra_tags').value.trim(),
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
}

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

async function optimize(){
  const payload = getForm();
  const res = await fetch('/optimize', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const out = await res.json();
  document.getElementById('results').classList.remove('hidden');

  // Render unified prompts
  document.getElementById('unifiedPos').textContent = out.unified?.positive || '';
  document.getElementById('unifiedNeg').textContent = out.unified?.negative || '';

  // Render model-specific
  document.getElementById('sdxlBox').textContent   = JSON.stringify(out.sdxl, null, 2);
  document.getElementById('comfyBox').textContent  = JSON.stringify(out.comfyui, null, 2);
  document.getElementById('mjBox').textContent     = out.midjourney || '';
  document.getElementById('pikaBox').textContent   = JSON.stringify(out.pika, null, 2);
  document.getElementById('runwayBox').textContent = JSON.stringify(out.runway, null, 2);

  // Hints
  document.getElementById('hintsBox').textContent  = JSON.stringify(out.hints, null, 2);
}

document.getElementById('run').addEventListener('click', optimize);
document.addEventListener('DOMContentLoaded', loadAllPresets);
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
            "prompt_order": "quality -> subject -> style -> lighting -> composition -> mood -> color grade -> extra tags",
            "debug": "If outputs look busy, reduce adjectives, add a concrete noun, remove conflicting styles.",
            "seed_strategy": "Lock a seed to compare variants; change only seed once composition is locked."
        }
    }
    return out, 200

# Web UI POST
@app.route("/optimize", methods=["POST"])
def optimize_ui():
    data = request.get_json(force=True)
    out, code = _optimize_core(data)
    return jsonify(out), code

# API alias to match your Agent
@app.route("/api/optimize", methods=["POST"])
def optimize_api():
    data = request.get_json(force=True)
    out, code = _optimize_core(data)
    return jsonify(out), code

if __name__ == "__main__":
    # Honor PORT if Replit/Agent sets it; fall back to 8080 or 5000
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
