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
    We keep the smaller side <= 1024 by default and round dims to /64.
    """
    a = (aspect or "16:9").strip().lower().replace(" ", "")
    if "x" in a and ":" not in a:
        try:
            w, h = [int(x) for x in a.split("x")]
            if w > 0 and h > 0:
                return w, h, f"{w}:{h}"
        except Exception:
            pass
    if ":" not in a:
        presets = {
            "square": (1024, 1024, "1:1"),
            "portrait": (896, 1152, "7:9"),
            "landscape": (1344, 768, "21:12"),
        }
        return presets.get(a, (1344, 768, "16:9"))

    try:
        num, den = a.split(":")
        num = float(num); den = fl
