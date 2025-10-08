# -*- coding: utf-8 -*-
# Automation/generate_images.py — Single-image, add-only generator for Roundups
# Product-first, realistic, full-scene prompts with optional animal/people (auto inferred)
# If image API unavailable/blocked, falls back to SVG placeholders so pages don't break.

from __future__ import annotations
import base64, csv, os, pathlib, re, sys, hashlib, traceback
from typing import List, Tuple, Optional
try:
    import yaml  # pip install pyyaml
except Exception:
    yaml = None

ROOT = pathlib.Path(__file__).parent
PROJECT = ROOT.parent
DATA = PROJECT / "data"
STATIC = PROJECT / "static"
HERO_DIR = STATIC / "hero" / "roundups"

ROUNDUPS_CSV = DATA / "roundups.csv"
PREFS_YAML   = DATA / "image_prefs.yaml"

# ---------- Config ----------
ADD_ONLY = True
OPENAI_API_KEY = (os.getenv("OPENAI_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
# Supported sizes: 1024x1024, 1536x1024, 1024x1536, auto
IMAGE_SIZE = os.getenv("IMAGE_SIZE", "1536x1024")   # landscape default
FALLBACK_PLACEHOLDER = (OPENAI_API_KEY == "")

# ---------- Small utils ----------
def slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"(^-+|-+$|-{2,})", "-", s)

def clean_row_keys(row: dict) -> dict:
    return {(k or "").lstrip("\ufeff").strip().lower(): (v or "").strip() for k, v in row.items()}

def read_yaml(path: pathlib.Path) -> dict:
    if not path.exists() or yaml is None:
        return {}
    with path.open("r", encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
        return d if isinstance(d, dict) else {}

def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)

def write_bytes(path: pathlib.Path, data: bytes, *, overwrite: bool) -> str:
    ensure_dir(path.parent)
    if path.exists() and not overwrite:
        return "skip"
    path.write_bytes(data)
    return "wrote" if path.exists() else "created"

def write_placeholder_svg(path: pathlib.Path, title: str, subtitle: str, *, overwrite: bool) -> str:
    ensure_dir(path.parent)
    if path.exists() and not overwrite:
        return "skip"
    w, h = 1536, 1024
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="#0f172a"/><stop offset="100%" stop-color="#1e293b"/></linearGradient></defs>
  <rect width="100%" height="100%" fill="url(#g)"/>
  <g fill="#fff" opacity="0.08"><circle cx="25%" cy="30%" r="220"/><circle cx="78%" cy="70%" r="260"/></g>
  <text x="6%" y="20%" fill="#e5e7eb" font-family="Segoe UI, Roboto, Helvetica, Arial" font-size="64" font-weight="700">{title}</text>
  <text x="6%" y="30%" fill="#cbd5e1" font-family="Segoe UI, Roboto, Helvetica, Arial" font-size="34" font-weight="500">{subtitle}</text>
</svg>"""
    path.write_text(svg, encoding="utf-8")
    return "wrote"

# ---------- OpenAI images ----------
_client = None
def get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client

def _images_generate(prompt: str, size: str):
    client = get_client()
    return client.images.generate(model=OPENAI_IMAGE_MODEL, prompt=prompt, size=size)

def openai_image_b64_with_retry(prompt: str, size: str) -> str:
    """
    Generate and return base64 with graceful fallback sizes.
    Tries: requested -> 1536x1024 -> 1024x1024 -> auto.
    """
    order = [size, "1536x1024", "1024x1024", "auto"]
    seen = set(); last = None
    for s in order:
        if s in seen: continue
        seen.add(s)
        try:
            resp = _images_generate(prompt, s)
            d0 = resp.data[0]
            b64 = getattr(d0, "b64_json", None)
            if b64 is None and isinstance(d0, dict):
                b64 = d0.get("b64_json")
            if not b64:
                raise RuntimeError("Image API did not include base64 payload (b64_json).")
            return b64
        except Exception as e:
            last = e
            continue
    raise last

def preflight_images_available() -> tuple[bool, str]:
    if FALLBACK_PLACEHOLDER:
        return False, "no API key set"
    try:
        resp = _images_generate("brand-safe test image", "auto")
        _ = resp.data[0]
        return True, "ok"
    except Exception as e:
        m = str(e)
        if "403" in m or "must be verified" in m or "PermissionDeniedError" in m:
            return False, "image model not permitted for this org/key"
        return False, m

# ---------- Auto inference for animal/person ----------
CAT_WORDS = {"cat","kitten","feline","litter","scratcher","wand","teaser","catnip"}
DOG_WORDS = {"dog","canine","hound","kennel","leash","harness","bark","fetch","tug"}
BIRD_WORDS = {"bird","parrot","finch","aviary","seed","perch","cage","aviary"}
FISH_WORDS = {"fish","aquarium","filter","betta","reef","paludarium"}
REPTILE_WORDS = {"reptile","lizard","gecko","dragon","chameleon","python","boa","snake","iguana","tortoise","turtle","terrarium","uvb","basking"}
AMPHIB_WORDS = {"amphibian","frog","toad","salamander","newt"}
SMALLPET_WORDS = {"hamster","guinea","rabbit","bunny","ferret","rodent","gerbil","hedgehog"}

def infer_animal(category: str, niche: str) -> Optional[str]:
    s = f"{category} {niche}".lower()
    def has(words): return any(w in s for w in words)
    if has(CAT_WORDS): return "cat"
    if has(REPTILE_WORDS): return "reptile"
    if has(AMPHIB_WORDS): return "amphibian"
    if has(BIRD_WORDS): return "bird"
    if has(FISH_WORDS): return "fish"
    if has(SMALLPET_WORDS): return "small pet"
    if has(DOG_WORDS): return "dog"
    return None

def people_hint_for_use(niche: str) -> bool:
    niche_l = (niche or "").lower()
    # Allow a hand/arm when it's natural (wearables, grooming, feeding, training, cleaning)
    keywords = ["harness","leash","collar","groom","clipper","trimmer","nail","feeder","bowl","bottle",
                "litter","scoop","clean","brush","training","clicker","carrier","playpen","crate"]
    return any(k in niche_l for k in keywords)

# ---------- Prompt assembly (product-first, realistic, full scene) ----------
def build_prompt(category: str, niche: str, style: str,
                 include_animals_mode: str, row_include_animals: Optional[bool],
                 explicit_animal: str, people_mode: str) -> str:
    """
    include_animals_mode: 'auto'|'yes'|'no'
    people_mode: 'auto'|'yes'|'no'  (auto = hand/arm only when natural)
    """
    style_desc = {
        "lifestyle-soft": "clean lifestyle scene, soft daylight, shallow depth of field, realistic textures, layered foreground/mid/background",
        "layflat-min": "overhead flat-lay on neutral surface, neatly arranged on-theme props, high-key lighting, balanced spacing",
        "cinematic-warm": "golden-hour tones, shallow depth of field, gentle vignette, editorial framing",
        "studio-3d": "soft-studio render look, seamless backdrop, matte/metal textures, soft rim light",
        "macro-texture": "tight macro of on-theme materials and tools, crisp texture detail, soft side light",
        "isometric-illustration": "clean isometric illustration, restrained palette, modern editorial vibe"
    }.get(style or "lifestyle-soft")

    # Decide animal/person
    inferred = infer_animal(category, niche)
    animal: Optional[str] = None
    if row_include_animals is True:
        animal = explicit_animal or inferred
    elif include_animals_mode == "yes":
        animal = explicit_animal or inferred
    elif include_animals_mode == "auto":
        # include only if niche strongly implies an animal
        animal = inferred
    else:
        animal = None

    # NEVER force a dog unless niche implies dog or explicitly set dog
    if animal == "dog" and "dog" not in f"{category} {niche}".lower() and (explicit_animal or "").lower() != "dog":
        animal = None

    allow_hand = False
    if people_mode == "yes":
        allow_hand = True
    elif people_mode == "auto":
        allow_hand = people_hint_for_use(niche)
    else:
        allow_hand = False

    # Product/niche subject
    subject_hint = f"{niche}".strip() or f"{category}".strip()

    # Compose the prompt
    base = (
        "Create a brand-safe wide landscape hero image for a buyer's guide. "
        f"Category/Niche: {niche} ({category}). "
        f"Style: {style} — {style_desc}. "
        "Focus: the specific product type for this niche in **realistic use**, not just isolated on a blank surface. "
        "Composition: layered foreground/mid/background with context props so the scene feels full but uncluttered; leave some negative space for a headline. "
        "Quality: photorealistic materials, accurate anatomy if any animal appears, natural lighting, believable reflections and shadows. "
        "Rules: no logos, no text, no watermarks, no packaging, no UI, brand-neutral objects only. "
        "Limit living beings to **zero or one** individual; do not add extra animals."
    )
    subj = f" Primary subject: {subject_hint} shown in natural use."
    if animal:
        subj += f" Optionally include a **single {animal}** if it makes sense for this scene; pose should be natural and secondary to the product."
    else:
        subj += " Do not include animals unless they are naturally implied by the product scene."
    if allow_hand:
        subj += " Optionally include a human **hand or arm** interacting with the product (no full face), only if it feels natural."

    # Defensive guidance to reduce odd creatures and random dogs
    safety = (
        " Avoid toy-like or stylized creatures; use realistic proportions and textures. "
        " Do not include any dog unless the niche clearly relates to dogs. "
        " Avoid generic pet portraits; keep the **product-in-use** as the visual anchor."
    )
    return base + subj + safety

# ---------- CSV reading ----------
def read_roundups() -> List[dict]:
    rows: List[dict] = []
    if not ROUNDUPS_CSV.exists():
        return rows
    with ROUNDUPS_CSV.open(newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            r = clean_row_keys(raw)
            if r.get("publish","yes").lower() not in {"yes","true","1"}:
                continue
            rows.append(r)
    # de-dupe by (category, niche) slugs
    seen=set(); uniq=[]
    for r in rows:
        cat,niche = r.get("category",""), r.get("niche","")
        if not cat or not niche: continue
        key=(slug(cat), slug(niche))
        if key in seen: continue
        seen.add(key); uniq.append(r)
    return uniq

# ---------- Main ----------
def main(argv: List[str]) -> int:
    print("[images] START — single-image, add-only generator (roundups only)")
    key_src = "OPENAI_IMAGE_API_KEY" if os.getenv("OPENAI_IMAGE_API_KEY") else ("OPENAI_API_KEY" if os.getenv("OPENAI_API_KEY") else "NONE")
    print(f" - key source: {key_src}, model: {OPENAI_IMAGE_MODEL}, size: {IMAGE_SIZE}")

    api_ok, reason = preflight_images_available()
    if not api_ok:
        msg = f" - images API unavailable → using SVG placeholders ({reason})"
        print(msg)

    prefs = read_yaml(PREFS_YAML)
    include_animals_mode = str(prefs.get("include_animals","auto")).lower()  # auto|yes|no
    people_mode = str(prefs.get("people","auto")).lower()                    # auto|yes|no
    style_default = (prefs.get("style_default") or "lifestyle-soft").strip()
    print(f" - prefs: include_animals={include_animals_mode}, people={people_mode}, style_default='{style_default}'")

    rows = read_roundups()
    print(f" - roundups.csv rows (published, unique): {len(rows)}")
    if not rows:
        print(f"[warn] No rows found in {ROUNDUPS_CSV} (or none published).")
        return 0

    for r in rows:
        cat, niche = r.get("category",""), r.get("niche","")
        if not cat or not niche: continue
        cat_s, niche_s = slug(cat), slug(niche)
        out = HERO_DIR / cat_s / f"{niche_s}.webp"

        row_style = r.get("style","").strip() or style_default
        row_inc = r.get("include_animals","").lower()
        row_include_animals = None if row_inc=="" else (row_inc in {"yes","true","1"})
        row_animal = (r.get("animal") or "").strip()

        prompt = build_prompt(
            category=cat, niche=niche, style=row_style,
            include_animals_mode=include_animals_mode,
            row_include_animals=row_include_animals,
            explicit_animal=row_animal,
            people_mode=people_mode
        )

        if out.exists() and ADD_ONLY:
            print(f"[skip-exists] {out}")
            continue

        if not api_ok:
            svg_path = out.with_suffix(".svg")
            status = write_placeholder_svg(svg_path, title=niche, subtitle=cat, overwrite=not ADD_ONLY)
            print(f"[{status}] {svg_path}")
            continue

        try:
            b64 = openai_image_b64_with_retry(prompt, IMAGE_SIZE)
            img_bytes = base64.b64decode(b64)
            status = write_bytes(out, img_bytes, overwrite=not ADD_ONLY)
            print(f"[{status}] {out}")
        except Exception as e:
            print(f"[error] {out}: {e}")
            tb = traceback.format_exc(limit=2)
            print(tb)
            # last-resort placeholder so pages still get a hero
            svg_path = out.with_suffix(".svg")
            st2 = write_placeholder_svg(svg_path, title=niche, subtitle=cat, overwrite=not ADD_ONLY)
            print(f"[fallback-placeholder] {svg_path} ({st2})")

    print("[images] DONE")
    return 0

print("[images] DEBUG: file loaded")
if __name__ == "__main__":
    print("[images] DEBUG: calling main")
    sys.exit(main(sys.argv))
