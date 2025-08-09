# prompt_engine.py
# Lightweight prompt reasoning + expansion + platform presets
# No external dependencies.

from typing import Dict, Any, List
import re

# ---------------------------
# Content-safety guardrails
# ---------------------------

DISALLOWED_PATTERNS = [
    r"\b(sexual|rape|molest|incest|bestiality)\b",
    r"\b(non[- ]?consensual|against\s+their\s+will)\b",
    r"\b(minor|underage|child)\b.*\b(nude|sexual|explicit)\b",
]

def is_disallowed(text: str) -> bool:
    t = text.lower()
    for pat in DISALLOWED_PATTERNS:
        if re.search(pat, t):
            return True
    return False


# ---------------------------
# Heuristic “reasoning” helpers
# ---------------------------

QUALITY_TAGS = [
    "award-winning", "highly detailed", "intricate", "8k", "sharp focus",
    "global illumination", "photorealistic", "studio quality"
]

# Default negative prompt that’s generally safe across image/video models
BASE_NEGATIVE = [
    "blurry", "soft focus", "lowres", "low quality", "jpeg artifacts",
    "overexposed", "underexposed", "bad anatomy", "extra limbs",
    "mutated hands", "missing fingers", "crooked eyes", "poorly drawn hands",
    "deformed", "duplicate", "tiling", "frame out of subject"
]

# Style keywords we’ll collect (if user hints at them) to tighten the output
STYLE_HINTS = {
    "cinematic": ["cinematic lighting", "film still", "shallow depth of field"],
    "moody": ["moody lighting", "desaturated", "high contrast"],
    "neon": ["neon lighting", "specular highlights", "vibrant glow"],
    "analog": ["kodak portra 400", "35mm", "grain"],
    "portrait": ["bokeh", "85mm lens", "studio lighting"],
    "fantasy": ["epic fantasy", "volumetric light", "concept art"]
}


def extract_subjects_actions_setting(idea: str) -> Dict[str, List[str]]:
    """
    Very light NLP: pull out potential subjects, actions, and a setting cue.
    We avoid heavy libs to keep this deploy-friendly.
    """
    text = " " + idea.lower() + " "
    # crude tokenization
    tokens = re.findall(r"[a-zA-Z\-']{2,}", text)

    subjects = []
    actions = []
    setting = []

    # Naive vocab buckets
    subject_vocab = {
        "woman", "women", "man", "men", "person", "people",
        "girl", "boy", "couple", "lesbian", "lesbians",
        "robot", "android", "warrior", "mage", "creature", "dragon",
        "cat", "dog", "character", "model", "athlete", "chef"
    }

    action_vocab = {
        "walking", "running", "fighting", "arguing", "embracing", "dancing",
        "posing", "smiling", "holding", "reading", "cooking", "flying",
        "swimming", "sitting", "standing", "looking", "staring", "confronting",
        "driving", "working", "training", "meditating"
    }

    setting_vocab = {
        "forest", "desert", "city", "street", "alley", "rooftop", "studio",
        "beach", "mountain", "cafe", "kitchen", "classroom", "arena", "hot", "tub",
        "rain", "snow", "night", "sunset", "dawn", "neon", "cyberpunk", "scifi"
    }

    for t in tokens:
        if t in subject_vocab and t not in subjects:
            subjects.append(t)
        if t in action_vocab and t not in actions:
            actions.append(t)
        if t in setting_vocab and t not in setting:
            setting.append(t)

    return {"subjects": subjects[:4], "actions": actions[:4], "setting": setting[:6]}


def collect_style_hints(idea: str) -> List[str]:
    text = idea.lower()
    out = []
    for key, vals in STYLE_HINTS.items():
        if key in text:
            out.extend(vals)
    return out


def build_positive_prompt(idea: str) -> str:
    # Extract cues
    cues = extract_subjects_actions_setting(idea)
    style = collect_style_hints(idea)

    # Compose a clear subject line
    subjects = cues["subjects"] or ["subject"]
    actions = cues["actions"]
    setting = cues["setting"]

    # Clean up “hot tub” as two-word phrase if detected
    if "hot" in setting and "tub" in setting:
        setting = [s for s in setting if s not in ("hot", "tub")]
        setting.insert(0, "hot tub")

    # Always push toward neutral, non-violent depiction if fighting/arguing shows up
    # Replace with “tense confrontation” to avoid explicit violence
    action_phrase = ""
    if actions:
        safe_actions = []
        for a in actions:
            if a in ("fighting", "arguing", "confronting"):
                safe_actions.append("tense confrontation")
            else:
                safe_actions.append(a)
        action_phrase = ", ".join(sorted(set(safe_actions)))

    subject_phrase = ", ".join(sorted(set(subjects)))
    setting_phrase = ", ".join(sorted(set(setting))) if setting else ""

    # Quality & composition tags
    composition = [
        "rule of thirds", "subject centered", "clean composition"
    ]

    positive_parts = [
        subject_phrase,
        action_phrase if action_phrase else "",
        setting_phrase,
        *QUALITY_TAGS,
        *composition,
        *style
    ]

    # Collapse, remove empties, dedup while keeping order
    seen = set()
    final = []
    for p in positive_parts:
        p = p.strip(",;: ").strip()
        if not p:
            continue
        if p not in seen:
            final.append(p)
            seen.add(p)

    return ", ".join(final)


def build_negative_prompt(idea: str) -> str:
    # Base negatives
    negatives = list(BASE_NEGATIVE)

    # If user mentions “violent”/“gore”, hard-block gorey artifacts
    if re.search(r"\b(blood|gore|splatter|dismember)\b", idea.lower()):
        negatives.extend(["blood", "gore", "splatter", "wound", "injury"])

    # De-duplicate and join
    neg = []
    seen = set()
    for n in negatives:
        n = n.strip()
        if n and n not in seen:
            neg.append(n)
            seen.add(n)
    return ", ".join(neg)


# ---------------------------
# Platform presets
# ---------------------------

PLATFORM_PRESETS = {
    "sdxl":  {"width": 1024, "height": 1024, "steps": 30, "cfg": 6.5, "sampler": "DPM++ 2M Karras"},
    "comfy": {"width": 1024, "height": 1024, "steps": 30, "cfg": 6.5, "sampler": "dpmpp_2m"},
    "mjv6":  {"aspect": "1:1", "quality": "high"},
    "pika":  {"duration": 4, "motion": "subtle", "camera": "static"},
    "runway":{"duration": 4, "motion": "subtle", "camera": "static"}
}


# ---------------------------
# Public API
# ---------------------------

def optimize_prompt(
    idea: str,
    platform: str = "sdxl",
    extras: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    """
    Takes a raw idea and returns a structured result:
      {
        'platform': 'sdxl',
        'positive': '...',
        'negative': '...',
        'params': { ... platform defaults ... },
        'notes': '...'
      }
    Raises a friendly error in 'notes' if content is disallowed.
    """
    extras = extras or {}

    if is_disallowed(idea):
        return {
            "platform": platform,
            "positive": "",
            "negative": "",
            "params": {},
            "notes": (
                "The idea appears to include disallowed content (e.g., sexual violence). "
                "Please rewrite your prompt to avoid sexual or non-consensual scenarios."
            )
        }

    positive = build_positive_prompt(idea)
    negative = build_negative_prompt(idea)

    # Merge platform defaults
    preset = PLATFORM_PRESETS.get(platform.lower(), PLATFORM_PRESETS["sdxl"]).copy()
    preset.update(extras or {})

    notes = "Optimized with light reasoning, quality tags, style hints, and safety filtering."

    return {
        "platform": platform.lower(),
        "positive": positive,
        "negative": negative,
        "params": preset,
        "notes": notes
    }


# Quick local test (optional):
if __name__ == "__main__":
    tests = [
        "Two women in a hot tub arguing at night, cinematic, neon",
        "A warrior reading a book in a forest at dawn, fantasy, cinematic",
    ]
    for t in tests:
        out = optimize_prompt(t, platform="sdxl")
        print("\nIDEA:", t)
        print("POS:", out["positive"])
        print("NEG:", out["negative"])
        print("PAR:", out["params"])
        print("NOTES:", out["notes"])

        "dimensions": pr["dimensions"],
        }
    return pack
