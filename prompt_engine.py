# prompt_engine.py
import re
from typing import Dict, Any

# --- Style stacks -------------------------------------------------------------
STYLE_DEFAULTS = {
    "cinematic": [
        "cinematic lighting",
        "subtle rim light",
        "film still realism",
        "fine texture detail",
        "shallow depth of field",
        "global illumination",
    ],
}

PLATFORM_PRESETS = {
    "sdxl": {
        "extras": ["8k detail", "photoreal", "realistic materials"],
        "negative": [
            "lowres", "blurry", "overexposed", "underexposed", "washed out",
            "disfigured", "extra fingers", "bad anatomy", "mutated", "grainy",
            "jpeg artifacts", "poorly drawn", "low quality", "overly saturated",
            "watermark", "text", "logo"
        ],
        "dimensions": "1024x1024",
    },
    "comfyui": {
        "extras": ["high fidelity", "photoreal"],
        "negative": ["bad anatomy", "artifacts", "mutated", "lowres", "text", "logo"],
        "dimensions": "1024x1024",
    },
    "midjourney": {
        "extras": ["--style raw", "--v 6", "--s 250"],
        "negative": [],
        "dimensions": "",
    },
    "pika": {
        "extras": ["smooth camera", "filmic motion"],
        "negative": ["flicker", "noise", "compression artifacts"],
        "dimensions": "1080x1080",
    },
    "runway": {
        "extras": ["cinematic camera", "high dynamic range"],
        "negative": ["banding", "posterization", "noise"],
        "dimensions": "1080x1080",
    },
}

# --- Light safety / moderation (configurable) ---------------------------------
EXPLICIT_TERMS = [r"\bsex\b", r"\bporn\b", r"\bnsfw\b", r"\bnude\b"]
EXTREME_VIOLENCE = [r"\bdrown\b", r"\bkill\b", r"\bmaim\b", r"\bgore\b"]
MINOR_TERMS = [r"\bchild\b", r"\bteen\b", r"\bminor\b"]

def _looks(patterns, s: str) -> bool:
    return any(re.search(p, s, flags=re.I) for p in patterns)

# --- Phrase-preserving cleaner ------------------------------------------------
def _clean_text(s: str) -> str:
    s = s.replace("\n", " ").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,", ", ", s)
    return s

# --- Heuristic camera/lighting inference -------------------------------------
def _infer_camera_craft(idea: str) -> Dict[str, str]:
    low = idea.lower()
    if any(k in low for k in ["night", "neon", "rain", "noir"]):
        lighting, lens, time, mood = "neon reflections, volumetric fog", "35mm, f/1.8", "night", "moody atmosphere"
    elif any(k in low for k in ["sunset", "golden hour", "dawn"]):
        lighting, lens, time, mood = "golden hour lighting", "50mm, f/2.0", "sunset", "warm ambience"
    else:
        lighting, lens, time, mood = "soft studio lighting", "50mm, f/2.8", "day", "natural atmosphere"
    return {"lighting": lighting, "lens": lens, "time": time, "mood": mood}

def _soften_if_needed(idea: str, safe_mode: str) -> str:
    """
    safe_mode: "strict" | "soften" | "off"
    - strict: rewrite explicit/extreme violence; block minors
    - soften: de-escalate violent/explicit terms
    - off: no changes
    """
    if safe_mode == "off":
        return idea
    txt = idea
    if _looks(MINOR_TERMS, txt):
        return "A cinematic scene suitable for all audiences."
    if _looks(EXTREME_VIOLENCE, txt):
        repl = "subdue" if safe_mode == "soften" else "overpower"
        txt = re.sub(r"\bdrown(ed|s|ing)?\b", repl, txt, flags=re.I)
        txt = re.sub(r"\bkill(ed|s|ing)?\b", "defeat", txt, flags=re.I)
    if _looks(EXPLICIT_TERMS, txt):
        if safe_mode in ("soften", "strict"):
            for p in EXPLICIT_TERMS:
                txt = re.sub(p, "", txt, flags=re.I).strip()
    return _clean_text(txt)

def build_prompt(
    idea: str,
    style: str = "cinematic",
    platform: str = "sdxl",
    safe_mode: str = "soften",
    extra_tags: str = "",
) -> Dict[str, Any]:
    idea = _clean_text(idea)
    idea = _soften_if_needed(idea, safe_mode=safe_mode)

    craft = _infer_camera_craft(idea)
    style_stack = STYLE_DEFAULTS.get(style, STYLE_DEFAULTS["cinematic"])
    preset = PLATFORM_PRESETS.get(platform, PLATFORM_PRESETS["sdxl"])

    core = f"{idea}, {craft['lighting']}, {craft['mood']}, {craft['time']}, {craft['lens']}"
    style_str = ", ".join(style_stack + preset["extras"])
    if extra_tags:
        style_str = f"{style_str}, {extra_tags}"
    primary = f"{core}, {style_str}"
    negative = ", ".join(preset["negative"])

    pack = {
        "primary": primary,
        "negative": negative,
        "meta": {"dimensions": preset["dimensions"], "platform": platform, "style": style, "safe_mode": safe_mode},
        "craft": craft,
        "platforms": {}
    }

    # Cross-platform variants
    for p, pr in PLATFORM_PRESETS.items():
        p_style = ", ".join(style_stack + pr["extras"])
        pack["platforms"][p] = {
            "primary": f"{core}, {p_style}",
            "negative": ", ".join(pr["negative"]),
            "dimensions": pr["dimensions"],
        }
    return pack
