# prompt_engine.py
# Drop-in upgrade for build_prompt(pr)
# No external deps; deterministic, platform-aware prompt packaging.

from typing import Dict, Tuple, List

# ---- Style presets tuned for cross-model consistency ----
STYLE_PRESETS = {
    "cinematic": [
        "cinematic lighting", "rich contrast", "depth of field", "35mm film look",
        "color graded", "high dynamic range"
    ],
    "photorealistic": [
        "photorealistic", "physically based rendering", "global illumination",
        "volumetric light", "realistic textures", "natural skin tones"
    ],
    "anime": [
        "anime style", "clean line art", "cel shading", "studio-quality coloring",
        "expressive eyes", "dynamic composition"
    ],
    "illustration": [
        "illustration", "inked linework", "vibrant colors", "hand-drawn feel",
        "storybook composition"
    ],
    "3d render": [
        "ultra-detailed 3D render", "ray tracing", "subsurface scattering",
        "high poly", "octane-style lighting"
    ],
    "pixel art": [
        "pixel art", "limited palette", "crisp 1px outlines", "retro aesthetic",
        "NES/SNES-era styling"
    ],
    "watercolor": [
        "watercolor painting", "soft washes", "paper texture", "pigment bloom",
        "loose brushwork"
    ],
    "line art": [
        "monoline", "clean contour lines", "minimal shading", "vector style"
    ],
}

# ---- Baseline negative prompts by platform (safe defaults) ----
NEGATIVE_BASE = {
    "sdxl": [
        "low quality", "blurry", "jpeg artifacts", "overexposed", "underexposed",
        "extra limbs", "bad anatomy", "mutated hands", "deformed"
    ],
    "comfyui": [
        "lowres", "noise", "bad composition", "posterization", "oversharpened",
        "extra limbs", "bad anatomy", "deformed"
    ],
    "midjourney": [
        "blurry", "low detail", "mutated", "disfigured", "extra limbs", "bad anatomy",
        "watermark", "text"
    ],
    "pika": [
        "low quality", "motion smear", "jitter", "banding", "flicker", "bad anatomy"
    ],
    "runway": [
        "low quality", "blurry", "banding", "posterization", "artifacting", "bad anatomy"
    ],
}

# Terms we’ll strip when safe_mode=True (beyond your app-level hard block)
SOFT_FILTER = [
    "nudity", "explicit", "nsfw", "porn", "sexual", "erotic", "fetish",
]

# Samplers are hints for SDXL/ComfyUI packs (clients can ignore)
SUGGESTED_SAMPLERS = ["DPM++ 2M Karras", "Euler a", "DDIM"]

def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

def _parse_aspect(aspect: str) -> Tuple[int, int, str]:
    """
    Normalize aspect to a width/height pair and canonical string.
    Targets reasonable defaults compatible with most backends.
    We keep the smaller side <= 1024 by default to avoid OOM in common hosts.
    """
    a = (aspect or "16:9").strip().lower().replace(" ", "")
    if "x" in a and ":" not in a:
        # handle "1920x1080" style quickly
        try:
            w, h = [int(x) for x in a.split("x")]
            if w > 0 and h > 0:
                return w, h, f"{w}:{h}"
        except Exception:
            pass
    if ":" not in a:
        # fallback to common labels
        presets = {
            "square": (1024, 1024, "1:1"),
            "portrait": (896, 1152, "7:9"),
            "landscape": (1344, 768, "21:12"),
        }
        return presets.get(a, (1344, 768, "16:9"))

    try:
        num, den = a.split(":")
        num = float(num); den = float(den)
        if num <= 0 or den <= 0:
            raise ValueError
        ratio = num / den
        # pick the smaller side at 1024, scale the other
        base = 1024
        if ratio >= 1.0:
            h = base
            w = int(round(base * ratio))
        else:
            w = base
            h = int(round(base / ratio))
        # round to multiples of 64 (nice for many models)
        def round64(x): 
            return int(round(x / 64.0) * 64)
        w, h = round64(w), round64(h)
        return w, h, f"{int(num)}:{int(den)}"
    except Exception:
        return 1344, 768, "16:9"

def _expand_style(style: str) -> List[str]:
    s = (style or "").strip().lower()
    if not s:
        return []
    # allow comma-separated mixtures, map each token via STYLE_PRESETS if known
    tokens = [t.strip() for t in s.split(",") if t.strip()]
    out: List[str] = []
    for t in tokens:
        if t in STYLE_PRESETS:
            out.extend(STYLE_PRESETS[t])
        else:
            # unknown style: pass through as-is (keeps flexibility)
            out.append(t)
    return out

def _tag_list(extra_tags: str) -> List[str]:
    if not extra_tags:
        return []
    tags = [t.strip() for t in extra_tags.split(",")]
    return [t for t in tags if t]

def _apply_safe_mode(text: str, enabled: bool) -> str:
    if not enabled:
        return text
    lowered = text.lower()
    for term in SOFT_FILTER:
        if term in lowered:
            # crude remove: replace whole word variants
            text = _soft_remove(text, term)
    return text

def _soft_remove(text: str, term: str) -> str:
    # minimal non-regex removal to avoid adding deps
    repls = [
        term, term.title(), term.upper()
    ]
    for r in repls:
        text = text.replace(r, "")
    return " ".join(text.split())

def _complexity_boosters(level: int) -> List[str]:
    """
    Adds composition/lighting/texture cues as complexity rises (1..5).
    We keep these neutral so they don't hijack user intent.
    """
    level = _clamp(level, 1, 5)
    tiers = {
        1: ["clean composition"],
        2: ["balanced composition", "clear subject separation"],
        3: ["dynamic composition", "subtle rim lighting", "material detail"],
        4: ["advanced composition", "cinematic volumetrics", "micro-surface detail"],
        5: ["masterful composition", "complex lighting", "intricate texture fidelity"],
    }
    return tiers[level]

def _quality_params(score: int) -> Dict[str, int]:
    """
    Translate 0..100 quality to steps/cfg hints that most backends can map.
    """
    score = _clamp(score, 0, 100)
    # steps: 12..42, cfg: 3..9
    steps = int(round(12 + (score / 100.0) * (42 - 12)))
    cfg = int(round(3 + (score / 100.0) * (9 - 3)))
    return {"steps": steps, "cfg": cfg}

def _build_positive(idea: str,
                    style_tokens: List[str],
                    tags: List[str],
                    complexity: int,
                    safe_mode: bool) -> str:
    # Preserve user intent first
    base = idea.strip()
    # Apply safe mode soft filter
    base = _apply_safe_mode(base, safe_mode)
    # Expand with neutral complexity hints and style/tags
    pieces = [base]
    boosters = _complexity_boosters(complexity)
    if boosters:
        pieces.append(", ".join(boosters))
    if style_tokens:
        pieces.append(", ".join(style_tokens))
    if tags:
        pieces.append(", ".join(tags))
    # Join cleanly
    positive = ", ".join([p for p in pieces if p])
    # Compact stray commas/spaces
    return " ".join(positive.replace(" ,", ",").split())

def _merge_negative(platform: str, user_negative: str, safe_mode: bool) -> str:
    base = NEGATIVE_BASE.get(platform, [])
    user = (user_negative or "").strip()
    if user:
        base = base + [user]
    neg = ", ".join(base)
    return _apply_safe_mode(neg, safe_mode)

def _platform_pack(platform: str,
                   positive: str,
                   negative: str,
                   width: int,
                   height: int,
                   quality: int,
                   complexity: int) -> Dict:
    qp = _quality_params(quality)
    steps = qp["steps"]
    cfg = qp["cfg"]

    if platform == "midjourney":
        # Map to common MJ flags (avoid version pin)
        # Stylize ~ quality; Chaos ~ complexity
        stylize = int(round(50 + (quality / 100.0) * 750))   # ~50..800
        chaos = int(round((complexity - 1) * 10))            # 0..40
        ar = _mj_aspect_flag(width, height)
        mj_prompt = f"{positive} --ar {ar} --stylize {stylize} --chaos {chaos}"
        return {
            "platform": "midjourney",
            "prompt": mj_prompt,
            "negative_hint": negative,   # MJ lacks true negative; kept as hint
            "params": {"stylize": stylize, "chaos": chaos, "aspect": ar}
        }

    if platform == "comfyui":
        # Provide a ready set of node input hints; clients can wire in their graph
        return {
            "platform": "comfyui",
            "inputs": {
                "positive": positive,
                "negative": negative,
                "width": width,
                "height": height,
                "cfg": cfg,
                "steps": steps,
                "sampler_name": SUGGESTED_SAMPLERS[0],
            }
        }

    if platform == "pika":
        # Text-to-video style hints (Pika interprets text + internal params)
        duration_hint = 4 + (complexity - 1)  # 4..8s suggestion
        return {
            "platform": "pika",
            "text_prompt": positive,
            "negative_prompt": negative,
            "params": {
                "width": width,
                "height": height,
                "guidance": cfg,
                "steps": steps,
                "duration_seconds_hint": duration_hint
            }
        }

    if platform == "runway":
        # Runway Gen features map loosely: we pass sane hints
        return {
            "platform": "runway",
            "text_prompt": positive,
            "negative_prompt": negative,
            "params": {
                "width": width,
                "height": height,
                "guidance": cfg,
                "steps": steps
            }
        }

    # Default SDXL pack
    return {
        "platform": "sdxl",
        "positive": positive,
        "negative": negative,
        "params": {
            "width": width,
            "height": height,
            "cfg": cfg,
            "steps": steps,
            "sampler": SUGGESTED_SAMPLERS[0],
        }
    }

def _mj_aspect_flag(w: int, h: int) -> str:
    # Reduce ratio to simplest common string "W:H"
    from math import gcd
    g = gcd(w, h)
    return f"{w//g}:{h//g}"

# ---- Public API ----
def build_prompt(pr: Dict) -> Dict:
    """
    Input (from app.py):
      {
        "idea": str,
        "platform": "sdxl"|"comfyui"|"midjourney"|"pika"|"runway",
        "style": str,
        "aspect": str,             # e.g. "16:9", "1:1", "portrait", "1920x1080"
        "extra_tags": str,         # comma-separated
        "negative": str,
        "safe_mode": bool,
        "complexity": int,         # 1..5
        "quality": int,            # 0..100
      }
    Returns a stable pack with minimal required top-level keys preserved
    to avoid breaking clients: 'platform', 'prompt' (or 'positive'), 'negative'.
    Extra platform-specific fields are namespaced.
    """
    platform = (pr.get("platform") or "sdxl").lower()
    if platform not in {"sdxl", "comfyui", "midjourney", "pika", "runway"}:
        platform = "sdxl"

    idea = (pr.get("idea") or "").strip()
    style = (pr.get("style") or "").strip()
    extra_tags = (pr.get("extra_tags") or "").strip()
    negative_in = (pr.get("negative") or "").strip()
    safe_mode = bool(pr.get("safe_mode", True))
    complexity = int(pr.get("complexity", 3))
    quality = int(pr.get("quality", 70))

    # Aspect -> numeric dims + canonical string
    width, height, canonical_ar = _parse_aspect(pr.get("aspect") or "16:9")

    # Build positive
    style_tokens = _expand_style(style)
    tags = _tag_list(extra_tags)
    positive = _build_positive(idea, style_tokens, tags, complexity, safe_mode)

    # Merge negative
    negative = _merge_negative(platform, negative_in, safe_mode)

    # Platform-specific packaging
    pack_specific = _platform_pack(platform, positive, negative, width, height, quality, complexity)

    # Top-level, conservative keys to avoid breaking existing simple clients
    # - If platform is MJ we expose 'prompt'; SDXL/ComfyUI expose 'positive'.
    out: Dict = {
        "platform": platform,
        "idea": idea,
        "aspect": canonical_ar,
        "width": width,
        "height": height,
        "quality": _clamp(quality, 0, 100),
        "complexity": _clamp(complexity, 1, 5),
        "safe_mode": safe_mode,
        "style": style,
        "tags": tags,
    }

    # Flatten a couple of common fields for easy consumption
    if platform == "midjourney":
        out["prompt"] = pack_specific["prompt"]
        out["negative"] = pack_specific.get("negative_hint", "")
    elif platform in {"pika", "runway"}:
        out["prompt"] = pack_specific["text_prompt"]
        out["negative"] = pack_specific.get("negative_prompt", "")
    else:
        # sdxl / comfyui
        out["positive"] = positive
        out["prompt"] = positive  # alias for compatibility
        out["negative"] = negative

    # Namespaced detailed pack for power users
    out["platform_pack"] = pack_specific

    return out
