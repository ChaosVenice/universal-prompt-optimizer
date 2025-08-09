# prompt_engine.py
# Clean, safe, phrase-preserving prompt builder with platform presets.
# Returns a dict with: primary, negative, meta, platforms{sdxl,comfyui,midjourney,pika,runway}

from __future__ import annotations
import re
from typing import Dict, Any, List


# ---------- basic cleaners ----------

def _clean_space(s: str) -> str:
    s = (s or "").replace("\n", " ").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,", ", ", s)
    return s

def _clamp_words(s: str, max_words: int = 160) -> str:
    parts = s.split()
    if len(parts) > max_words:
        parts = parts[:max_words]
    return " ".join(parts)


# ---------- safety (reject or soften) ----------

# Disallow sexual violence & minors. We will block these.
DISALLOW = [
    r"\b(non[- ]?consensual|against\s+their\s+will)\b",
    r"\b(rape|molest|incest|bestiality)\b",
    r"\b(minor|underage|child)\b.*\b(nude|sexual|explicit)\b",
    r"\b(sexual)\b.*\b(violence|assault)\b",
]

# We also block explicit violence combined with sex words
SEX_WORD = r"\b(sex|nsfw|porn|explicit|nude|nudity)\b"
VIOLENT_WORD = r"\b(drown|kill|murder|stab|shoot|maim|behead|strangle)\b"

def _is_blocked(idea: str) -> bool:
    t = idea.lower()
    if re.search(SEX_WORD, t) and re.search(VIOLENT_WORD, t):
        return True
    for pat in DISALLOW:
        if re.search(pat, t, re.I):
            return True
    return False

# Soft de-escalation for plain violence (no sexual component)
SOFTEN_MAP = {
    r"\bdrown(s|ed|ing)?\b": "overpower (off-screen, implied)",
    r"\bkill(s|ed|ing)?\b": "neutralize (off-screen, implied)",
    r"\bstab(s|bed|bing)?\b": "threaten (off-screen)",
    r"\bshoot(s|ing)?\b": "aim (off-screen)",
    r"\bblood(y)?\b": "splashing water",
}
def _soften(text: str) -> str:
    out = text
    for pat, repl in SOFTEN_MAP.items():
        out = re.sub(pat, repl, out, flags=re.I)
    return out


# ---------- light “reasoning” to extract cues ----------

QUALITY = [
    "highly detailed", "sharp focus", "global illumination",
    "professional grade", "8k detail"
]

STYLE_HINTS = {
    "cinematic": ["cinematic lighting", "film still realism", "shallow depth of field"],
    "portrait": ["85mm lens", "bokeh", "studio lighting"],
    "neon": ["neon reflections", "vibrant glow"],
    "analog": ["kodak portra 400", "35mm", "natural grain"],
    "moody": ["dramatic contrast", "desaturated palette"],
    "fantasy": ["epic fantasy", "volumetric light", "concept art"],
}

SUBJECT_VOCAB = {
    "woman","women","man","men","person","people","couple","model","athlete",
    "character","warrior","mage","robot","android","cat","dog","chef","artist",
}
ACTION_VOCAB = {
    "walking","running","arguing","confronting","dancing","posing","smiling",
    "holding","reading","cooking","flying","swimming","sitting","standing",
    "looking","staring","working","training","meditating",
}
SETTING_VOCAB = {
    "forest","desert","city","street","alley","rooftop","studio","beach",
    "mountain","cafe","kitchen","classroom","arena","hot","tub","rain","snow",
    "night","sunset","dawn","neon","cyberpunk","scifi",
}

def _extract_cues(idea: str) -> Dict[str, List[str]]:
    tokens = re.findall(r"[a-zA-Z\-']{2,}", idea.lower())
    subs, acts, sets_ = [], [], []
    for t in tokens:
        if t in SUBJECT_VOCAB and t not in subs: subs.append(t)
        if t in ACTION_VOCAB and t not in acts: acts.append(t)
        if t in SETTING_VOCAB and t not in sets_: sets_.append(t)
    # phrase repair: "hot tub"
    if "hot" in sets_ and "tub" in sets_:
        sets_ = [s for s in sets_ if s not in ("hot","tub")]
        sets_.insert(0, "hot tub")
    return {"subjects": subs[:4], "actions": acts[:4], "setting": sets_[:6]}

def _style_from_idea(idea: str) -> List[str]:
    t = idea.lower()
    out: List[str] = []
    for key, vals in STYLE_HINTS.items():
        if key in t:
            out.extend(vals)
    return out


# ---------- positive/negative builders ----------

BASE_NEG = [
    "blurry","soft focus","lowres","low quality","jpeg artifacts",
    "overexposed","underexposed","bad anatomy","extra limbs",
    "mutated hands","missing fingers","crooked eyes","poorly drawn hands",
    "deformed","duplicate","tiling","frame out of subject","watermark","text","logo"
]

def _build_positive(idea: str) -> str:
    cues = _extract_cues(idea)
    styles = _style_from_idea(idea)

    subjects = ", ".join(sorted(set(cues["subjects"]))) or "subject"
    actions = []
    for a in cues["actions"]:
        if a in ("arguing","confronting"):
            actions.append("tense confrontation")
        else:
            actions.append(a)
    action_phrase = ", ".join(sorted(set(actions)))
    setting_phrase = ", ".join(sorted(set(cues["setting"])))

    composition = ["rule of thirds","clean composition"]
    parts: List[str] = [subjects]
    if action_phrase: parts.append(action_phrase)
    if setting_phrase: parts.append(setting_phrase)
    parts += QUALITY + composition + styles

    # dedup while preserving order
    seen, final = set(), []
    for p in parts:
        p = p.strip(",;: ")
        if p and p not in seen:
            final.append(p); seen.add(p)
    return ", ".join(final)

def _build_negative(idea: str) -> str:
    neg = list(BASE_NEG)
    if re.search(r"\b(blood|gore|splatter|dismember)\b", idea.lower()):
        neg += ["blood","gore","splatter","wound","injury"]
    # dedup
    out, seen = [], set()
    for n in neg:
        if n not in seen:
            out.append(n); seen.add(n)
    return ", ".join(out)


# ---------- platform presets ----------

PRESETS = {
    "sdxl":  {"width": 1024, "height": 1024, "steps": 30, "cfg": 6.5, "sampler": "DPM++ 2M Karras"},
    "comfyui":{"width": 1024, "height": 1024, "steps": 30, "cfg": 6.5, "sampler": "dpmpp_2m_karras"},
    "midjourney": {"flags": "--v 6 --style raw --chaos 0 --ar 1:1"},
    "pika":  {"duration": 4, "motion": "subtle", "camera": "static"},
    "runway":{"duration": 4, "motion": "subtle", "camera": "static"},
}


# ---------- public API: build_prompt ----------

def build_prompt(
    idea: str,
    style: str = "cinematic",
    platform: str = "sdxl",
    safe_mode: str = "soften",
    extra_tags: str = "",
) -> Dict[str, Any]:
    """
    Returns:
    {
      "primary": str,
      "negative": str,
      "meta": {...},
      "platforms": { "sdxl": {...}, "comfyui": {...}, "midjourney": {...}, "pika": {...}, "runway": {...} }
    }
    """
    raw = _clean_space(idea)
    if not raw:
        return {"primary": "", "negative": "", "meta": {"error": "empty idea"}, "platforms": {}}

    # hard block sexual violence / minors
    if _is_blocked(raw):
        return {
            "primary": "",
            "negative": "",
            "meta": {
                "error": "Blocked: sexual violence or minors with explicit content.",
                "safe_mode": "strict"
            },
            "platforms": {}
        }

    # gentle de-escalation for plain violence if requested
    text = _soften(raw) if safe_mode in ("soften","strict") else raw
    text = _clean_space(_clamp_words(text))

    positive = _build_positive(text)
    if extra_tags:
        positive = f"{positive}, { _clean_space(extra_tags) }"
    negative = _build_negative(text)

    # cross-platform payloads
    platforms: Dict[str, Any] = {}

    # SDXL
    p = PRESETS["sdxl"]
    platforms["sdxl"] = {
        "prompt": positive,
        "negative": negative,
        "width": p["width"], "height": p["height"],
        "sampler": p["sampler"], "steps": p["steps"], "cfg": p["cfg"], "seed": "random"
    }

    # ComfyUI (compatible fields)
    c = PRESETS["comfyui"]
    platforms["comfyui"] = {
        "positive": positive,
        "negative": negative,
        "width": c["width"], "height": c["height"],
        "sampler": c["sampler"], "steps": c["steps"], "cfg": c["cfg"]
    }

    # Midjourney
    mj_flags = PRESETS["midjourney"]["flags"]
    platforms["midjourney"] = {
        "prompt": f"{positive} {mj_flags}",
        "negative": negative
    }

    # Pika
    pk = PRESETS["pika"]
    platforms["pika"] = {
        "prompt": positive, "avoid": negative,
        "duration": pk["duration"], "motion": pk["motion"], "camera": pk["camera"]
    }

    # Runway
    rw = PRESETS["runway"]
    platforms["runway"] = {
        "prompt": positive, "negative_prompt": negative,
        "duration": rw["duration"], "motion": rw["motion"], "camera": rw["camera"]
    }

    meta = {
        "style": style,
        "safe_mode": safe_mode,
        "note": "Phrase-preserving optimization with quality tags, composition, and platform presets."
    }

    return {
        "primary": positive,
        "negative": negative,
        "meta": meta,
        "platforms": platforms
    }
