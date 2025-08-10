# prompt_engine.py
# Deterministic, platform-aware prompt packaging for /api/optimize

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

# Terms stripped when safe_mode=True (in addition to app-level hard block)
SOFT_FILTER = ["nudity", "explicit", "nsfw", "porn", "sexual", "erotic", "fetish"]

# Sampler hints (clients may ignore)
SUGGESTED_SAMPLERS = ["DPM++ 2M Karras", "Euler a", "DDIM"]


# ---------- helpers ----------
def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

def _parse_aspect(aspect: str) -> Tuple[int, int, str]:
    """
    Normalize aspect to width/height pair and canonical 'W:H' string.
    Smaller side ~1024, dims rounded to multiples of 64.
    """
    a = (aspect or "16:9").strip().lower().replace(" ", "")
    # "1920x1080"
    if "x" in a and ":" not in a:
        try:
            w, h = [int(x) for x in a.split("x")]
