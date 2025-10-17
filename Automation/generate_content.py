# generate_content.py — Roundups = Buyer's Guide only; split-hero; hardened scaffolds; higher token limits
from __future__ import annotations
import csv, pathlib, re, tomllib, os, yaml, hashlib
from openai import OpenAI
from openai import BadRequestError

# ----- roots -----
ROOT = pathlib.Path(__file__).parent
PROJECT = ROOT.parent
CONTENT = PROJECT / "content"
ROUNDUPS = CONTENT / "roundups"
REVIEWS  = CONTENT / "reviews"
STATIC   = PROJECT / "static"
HERO_DIR = STATIC / "hero" / "roundups"

# ----- global write policy -----
ADD_ONLY = True                 # never overwrite authored pages (stubs can be replaced)
OVERWRITE_HOMEPAGE = False      # set True if you want homepage to change each run
OVERWRITE_ABOUT = False         # set True if you want about page to change each run
CLEAN_ROUNDUP_HERO_TITLES = False  # one-time fixer; keep False to avoid touching existing pages

# Review structure toggles that match the new outline
JUMP_LINKS_ENABLED = False      # suppress auto "Jump to:" injection (we're not using it now)
BAN_LONG_DASHES    = True       # normalize — / – (and entities) to " - "
REVIEW_PASSTHROUGH = True       # REVIEW BODY = exactly what the prompt returns

# ===== small utils =====
def slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"(^-+|-+$|-{2,})", "-", s)

def fm_yaml(d: dict) -> str:
    return "---\n" + yaml.safe_dump(d, sort_keys=False).strip() + "\n---\n\n"

def _read_frontmatter(path: pathlib.Path) -> dict:
    try:
        txt = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", txt, flags=re.S)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _is_nonstub_existing(path: pathlib.Path) -> bool:
    if not path.exists():
        return False
    fm = _read_frontmatter(path)
    return not bool(fm.get("stub") is True)

def _write_markdown(path: pathlib.Path, frontmatter: dict, body_html: str, *, overwrite: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = fm_yaml(frontmatter) + body_html
    if not overwrite and ADD_ONLY:
        try:
            with open(path, "x", encoding="utf-8") as f:
                f.write(content)
            print(f"[created] {path}")
            return "created"
        except FileExistsError:
            print(f"[skip-exists] {path}")
            return "skipped"
    else:
        path.write_text(content, encoding="utf-8")
        print(f"[wrote] {path}")
        return "wrote"

def write_markdown(path: pathlib.Path, frontmatter: dict, body_html: str, *, overwrite: bool = False) -> str:
    return _write_markdown(path, frontmatter, body_html, overwrite=overwrite)

def _clean_row_keys(row: dict) -> dict:
    return {(k or "").lstrip("\ufeff").strip().lower(): (v or "").strip() for k, v in row.items()}

def _first_link(row: dict) -> str:
    for key in ("affiliate_link_short","affiliate_link","amzn_short","amzn_link","short_link","shortlink","url","link"):
        v = (row.get(key) or "").strip()
        if v:
            return v
    return ""

def _asin_from_row(row: dict) -> str:
    for k, v in row.items():
        if "asin" in k and v:
            return v.strip()
    return ""

def _sanitize_entities(html: str) -> str:
    # Normalize long dashes first if enabled, then entity-fix arrows
    if BAN_LONG_DASHES:
        html = (html.replace("—", " - ")
                    .replace("&mdash;", " - ")
                    .replace("–", " - ")
                    .replace("&ndash;", " - "))
    return html.replace("→","&rarr;")

def _combine_brand_title(brand: str, title: str) -> str:
    b = (brand or "").strip()
    t = (title or "").strip()
    if not b: return t
    tl, bl = t.lower(), b.lower()
    if tl.startswith(bl + " "): return t
    if tl.startswith(bl + " " + bl + " "): return t
    return f"{b} {t}"

def _digestible_display_name(row: dict) -> str:
    brand = (row.get("brand") or "").strip()
    raw   = (row.get("display_title") or row.get("product_title") or _asin_from_row(row) or "Product").strip()
    raw = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", raw)
    if " - " in raw: raw = raw.split(" - ", 1)[0]
    raw = re.sub(r"\s{2,}", " ", raw).strip()
    nice = _combine_brand_title(brand, raw)
    if len(nice) > 70:
        cut = nice[:70]; cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
        nice = cut + "…"
    return nice

def predict_product_slug(row: dict) -> str:
    SLUG_MAX = 80
    explicit = (row.get("product_slug") or "").strip()
    if explicit:
        base = slug(explicit)
    else:
        asin = _asin_from_row(row)
        if asin:
            base = slug(asin)
        else:
            title = (row.get("display_title") or row.get("product_title") or "").strip()
            brand = (row.get("brand") or "").strip()
            base = slug(_combine_brand_title(brand, title)) if title else slug(f"{row.get('category','')} {row.get('niche','')} {brand}")
    if not base:
        h = hashlib.md5("|".join([
            row.get("category",""), row.get("niche",""),
            (row.get("brand") or ""), (row.get("display_title") or row.get("product_title") or ""), _asin_from_row(row)
        ]).encode("utf-8")).hexdigest()[:8]
        return f"item-{h}"
    if len(base) > SLUG_MAX:
        h = hashlib.md5(base.encode("utf-8")).hexdigest()[:8]
        base = f"{base[:SLUG_MAX-9].rstrip('-')}-{h}"
    return base

def _derive_best_for(row: dict) -> str:
    v = (row.get("best_for") or "").strip()
    if v: return v
    for k in ("standout_reason","trait","type_or_format","type"):
        vv = (row.get(k) or "").strip()
        if vv: return vv
    return ""

def _derive_key_feature(row: dict) -> str:
    v = (row.get("key_feature") or "").strip()
    if v: return v
    for k in ("standout_reason","trait","type_or_format","type"):
        vv = (row.get(k) or "").strip()
        if vv: return vv
    return ""

# ===== concise title helpers =====
_ABBR = {
    "Active Noise Cancellation": "ANC",
    "Noise Cancelling": "ANC",
    "Bluetooth": "BT",
    "Hours": "hr", "Hour": "hr",
    "True Wireless": "", "Built in": "", "Built-in": "",
}
_COLOR_WORDS = {"black","blue","white","silver","gray","grey","green","red","pink","gold","beige"}

def _strip_tail_noise(s: str) -> str:
    s = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", s)
    s = s.split(" - ", 1)[0]
    s = re.sub(r"\b[A-Z0-9\-]{6,}\b", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,.-")
    return s

def _extract_feats(src: str) -> list[str]:
    feats = []
    m = re.search(r"\bIPX(\d)\b", src or "", re.I)
    if m: feats.append(f"IPX{m.group(1)}")
    m = re.search(r"\b(\d+)\s*hours?\b", src or "", re.I)
    if m: feats.append(f"{m.group(1)}-hr Battery")
    if re.search(r"noise cancell", src or "", re.I): feats.append("ANC")
    m = re.search(r"\bbluetooth\s*([0-9.]+)\b", src or "", re.I)
    if m: feats.append(f"BT {m.group(1)}")
    return feats

def derive_concise_titles(brand: str, product_title: str, niche: str) -> tuple[str,str,str,str]:
    raw = _strip_tail_noise((product_title or "").strip())
    raw = " ".join([w for w in raw.split() if w.lower() not in _COLOR_WORDS])
    for k, v in _ABBR.items():
        raw = re.sub(k, v, raw, flags=re.I)
    nice = _combine_brand_title(brand, raw)
    niche_core = (niche or "").strip()
    feats = _extract_feats(product_title or "")
    base = f"{brand} {niche_core}".strip() if niche_core else nice
    tail = ", ".join(dict.fromkeys([f for f in feats if f]))
    h1 = f"{base} — {tail}" if tail else base
    h1 = re.sub(r"\s{2,}", " ", h1).strip(" ,-")
    def cap(s, n): return (s[:n].rsplit(" ",1)[0] + "…") if len(s) > n and " " in s[:n] else (s if len(s)<=n else s[:n-1] + "…")
    h1 = cap(h1, 65)
    seo = cap(f"{brand} {raw}", 60) if brand else cap(raw, 60)
    crumb_base = f"{brand} {niche_core}".strip() if niche_core else (brand or raw)
    breadcrumb = cap(crumb_base, 38)
    return (h1, seo, breadcrumb, (product_title or "").strip())

# -----------------------------------------------------------------------------
# OpenAI wiring (robust: auto-select token param + Responses fallback)
# -----------------------------------------------------------------------------
_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

def _model_needs_default_temperature(model_name: str) -> bool:
    """Some families (e.g., gpt-5) behave better with default temperature; omit explicit temp."""
    return (model_name or "").lower().startswith("gpt-5")

def call_llm(prompt: str) -> str:
    """
    Robust LLM call:
    - Tries Chat Completions with the correct token param for the model
      (some models use `max_tokens`, others use `max_completion_tokens`).
    - Bumps the output budget if the first call returns too short/empty.
    - Falls back to the Responses API (with `max_output_tokens`) if needed.
    """
    model_lower = (MODEL or "").lower()
    base_sys  = {"role": "system", "content": "Return only a clean HTML fragment. No scripts."}
    base_user = {"role": "user", "content": prompt}
    msgs = [base_sys, base_user]

    def _chat_with(param_name: str, max_out: int):
        kwargs = {"model": MODEL, "messages": msgs}
        if param_name == "max_completion_tokens":
            kwargs["max_completion_tokens"] = max_out
        else:
            kwargs["max_tokens"] = max_out
        if not _model_needs_default_temperature(MODEL):
            kwargs["temperature"] = 0.4
        return _client.chat.completions.create(**kwargs)

    def _chat_best(max_out: int) -> str:
        # Heuristic: newer families (gpt-5, o4/o3, 4.1) usually take max_completion_tokens.
        prefer_completion = any(tag in model_lower for tag in ("gpt-5", "o4", "o3", "4.1"))
        order = ("max_completion_tokens", "max_tokens") if prefer_completion else ("max_tokens", "max_completion_tokens")
        last_err = None
        for pname in order:
            try:
                resp = _chat_with(pname, max_out)
                out = (resp.choices[0].message.content or "").strip()
                if out:
                    return out
            except BadRequestError as e:
                msg = (getattr(e, "message", "") or str(e)).lower()
                last_err = e
                # If the server tells us the param is wrong, try the other one.
                if ("unsupported parameter" in msg and "max_tokens" in msg) or ("unsupported parameter" in msg and "max_completion_tokens" in msg):
                    continue
                # Any other chat error: rethrow.
                raise
        # If both param names failed or returned empty, raise to trigger Responses fallback.
        if last_err:
            raise last_err
        return ""

    def _responses(max_out: int) -> str:
        # Responses API needs plain text and `max_output_tokens`
        user_text = "".join(m.get("content", "") for m in msgs if m.get("role") == "user")
        r = _client.responses.create(model=MODEL, input=user_text, max_output_tokens=max_out)
        return (getattr(r, "output_text", "") or "").strip()

    # Try chat with normal budget, then larger; if that fails, try Responses.
    try:
        out = _chat_best(2000)
        if out:
            return out
        out = _chat_best(3000)
        if out:
            return out
    except BadRequestError:
        # Fall through to Responses
        pass

    out = _responses(2000)
    if out:
        return out
    return _responses(3000)

# Simple Jinja2 fill (used by generator and repair script)
def fill(template: str, vars: dict) -> str:
    from jinja2 import Template
    return Template(template).render(**vars)

# ----- constants -----
DISCLOSURE_HTML = (
    '<aside class="disclosure">This page may include sponsored links. '
    'As an Amazon Associate, this site may earn from qualifying purchases.</aside>'
)
FEATURED_ANCHOR = "<!-- GENERATOR_INSERT_FEATURED -->"
STUB_SENTENCE = "This review is being prepared. In the meantime, you can check availability below."

# ----- hero image helpers -----
def _find_roundup_hero_src(category: str, niche: str) -> str | None:
    cat_s, niche_s = slug(category), slug(niche)
    base = HERO_DIR / cat_s / niche_s
    for ext in (".webp", ".svg", ".png", ".jpg", ".jpeg"):
        if (p := base.with_suffix(ext)).exists():
            return f"/hero/roundups/{cat_s}/{niche_s}{ext}"
    return None

def _render_roundup_hero_split(src: str, intro_html: str) -> str:
    """Side-by-side hero: image left, intro right; full width; clear border (NO inner title)."""
    return (
        '<section class="hero-split" '
        'style="width:100%;box-sizing:border-box;border:1px solid #e5e7eb;border-radius:12px;'
        'padding:16px;display:grid;grid-template-columns:minmax(260px,40%) 1fr;gap:20px;align-items:center;">'
        f'<figure style="margin:0;"><img src="{src}" alt="" '
        'style="width:100%;height:auto;display:block;border-radius:10px;"/></figure>'
        f'<div class="hero-copy" style="min-width:0;">{intro_html}</div>'
        '</section>\n'
    )

# ===== Homepage prompt =====
HOME_PROMPT = r"""You are writing the HOMEPAGE BODY for {{site_name}}. This page lives at the domain root (the main homepage).

OUTPUT RULES
- Return a CLEAN HTML FRAGMENT only (no <html>, <head>, scripts).
- Tone: professional, engaging, benefit-focused; short paragraphs (1–3 lines); use <strong>…</strong> to emphasize benefits (not hype).
- Avoid banned words: us, our, guarantee/guarantees, 100%, money, lowest, cheapest.
- No prices, ratings, model years, or time-sensitive claims.
- Internal links only to /roundups/ and on-page anchors (#quickstart, #why-us, #how-we-review, #faqs).
- You MAY use a single inline style of overflow-x:auto on a wrapper <div> around the Quick Start <table> to enable horizontal scrolling on mobile.

STRUCTURE (exact order)

<h2>Find the Right Gear, Faster</h2>
<p><strong>{{site_name}} turns hours of research into clear, comparison-ready insights</strong> so you can buy once and love what you pick. We focus on outcomes that matter—comfort, compatibility, and ease of setup.</p>
<p><a href="/roundups/">Explore Products</a></p>

<p><strong>Jump to:</strong>
  <a href="#quickstart">Quick Start</a> ·
  <a href="#why-us">Why Choose {{site_name}}</a> ·
  <a href="#how-we-review">How We Review</a> ·
  <a href="#faqs">FAQs</a>
</p>

<h2>Featured Category</h2>
<!-- GENERATOR_INSERT_FEATURED -->

<h2 id="quickstart">Quick Start: Match a Goal to a Category</h2>
<p>Start with your goal, then compare options in the Roundups hub.</p>
<div class="table-scroll" style="overflow-x:auto">
  <table>
    <thead><tr><th>Your Goal</th><th>Start Here</th><th>Why This Works</th><th>Action</th></tr></thead>
    <tbody>
      <tr><td><strong>Travel with pets</strong></td><td>Airline-Approved Carriers</td><td>Focus on <strong>fit</strong>, <strong>ventilation</strong>, and <strong>ease of carry</strong>.</td><td><a href="/roundups/">Compare options &rarr;</a></td></tr>
      <tr><td><strong>Cleaner terrariums</strong></td><td>Terrarium Cleaners</td><td><strong>Residue control</strong> and <strong>material safety</strong> matter most.</td><td><a href="/roundups/">See picks &rarr;</a></td></tr>
      <tr><td><strong>Comfortable walks</strong></td><td>Harnesses & Leads</td><td><strong>Fit</strong> + <strong>control</strong> reduce pulling and chafing.</td><td><a href="/roundups/">View systems &rarr;</a></td></tr>
    </tbody>
  </table>
</div>
<p><a href="/roundups/">Open Roundups Hub</a></p>

<h2 id="why-us">Why Choose {{site_name}}</h2>
<ul>
  <li><strong>Outcome-first picks:</strong> what you’ll feel and notice—comfort, clarity, reliability.</li>
  <li><strong>Comparison-ready layouts:</strong> tables and summaries that make differences obvious.</li>
  <li><strong>Up-to-date thinking:</strong> practical guidance around common pet-gear trade-offs.</li>
</ul>

<h2 id="how-we-review">How We Review</h2>
<ul>
  <li><strong>Define the job:</strong> the result you want (e.g., safer travel, easier cleanup).</li>
  <li><strong>Compare what matters:</strong> avoid spec noise; consider setup friction and daily usability.</li>
  <li><strong>Show trade-offs:</strong> short pros/cons so you know where a product shines—and where it doesn’t.</li>
</ul>
<p><a href="/roundups/">Browse Roundups</a></p>

<h2 id="faqs">FAQs</h2>
<h3>What does {{site_name}} do?</h3>
<p>We synthesize buyer-relevant details into clear comparisons so you can pick faster with fewer doubts.</p>
<h3>Where should I start?</h3>
<p>Use the <strong>Quick Start</strong> table to match a goal to a category, then open the Roundups hub.</p>
<h3>Do you list prices or ratings?</h3>
<p>No. To keep pages evergreen and objective, we avoid pricing and star ratings while emphasizing practical outcomes.</p>

<p style="font-size:0.9em;opacity:0.85;">This page may include sponsored links. As an Amazon Associate, this site may earn from qualifying purchases.</p>
"""

def load_featured_pairs(limit: int = 1) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    featured_csv = PROJECT / "data" / "featured.csv"
    src = featured_csv if featured_csv.exists() else (PROJECT / "data" / "roundups.csv")
    if not src.exists(): return pairs
    seen = set()
    with src.open(newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            row = _clean_row_keys(raw)
            cat, niche = row.get("category",""), row.get("niche","")
            if not cat or not niche: continue
            key = (slug(cat), slug(niche))
            if key in seen: continue
            seen.add(key); pairs.append((cat, niche))
            if len(pairs) >= limit: break
    return pairs

def render_featured_grid(pairs: list[tuple[str, str]]) -> str:
    cards = []
    for cat, niche in pairs[:1]:
        url = f"/roundups/{slug(cat)}/{slug(niche)}/"
        cards.append(f'<a class="roundups-card" href="{url}"><h3>{niche}</h3><p>{cat}</p></a>')
    return '<div class="roundups-grid">\n' + "\n".join(cards) + "\n</div>" if cards else "<p>Category coming soon.</p>"

# ===== Roundup prompt =====
ROUNDUP_PROMPT = r"""
You are writing a ROUNDUP PAGE for the {{niche}} niche ({{category}}).

OUTPUT RULES
- Clean HTML fragment only (no <html>, <head>, scripts).
- Tone: professional, engaging, SEO-aware, benefit-focused. Use clear, skimmable phrasing with some <strong>…</strong> emphasis.
- Avoid banned words: us, our, guarantee/guarantees, 100%, money, lowest, cheapest.
- No prices, no ratings, no model years, no time-sensitive claims.
- All external buy links must use rel="nofollow sponsored".

STRUCTURE (exact)
1) Intro (one paragraph, 5–7 sentences)
- Start with a useful framing line or a bold evergreen trend/stat related to {{niche}}.
- Explain what matters when comparing options (benefit-first for {{category}}).
- End with a light CTA to scan the Buyer’s Guide below.
- Include exactly one bold sentence in the intro with <strong>…</strong>.

2) <h2>Buyer’s Guide</h2>
- STOP: Do not list products here. The generator inserts one dense product paragraph per item under this heading (each with an external affiliate link and an internal review link).
"""

# ===== Review prompt (UPDATED to match new outline) =====
REVIEW_PROMPT = r"""
You are writing a REVIEW (individual product page) for {{brand}} {{product_title}} in the {{niche}} niche.

OUTPUT RULES
- Return a CLEAN HTML fragment only (no <html>, <head>, scripts).
- Short paragraphs (1–3 sentences). Objective, conversational, human.
- DO NOT mention prices, discounts, ratings/reviews, warranties/returns, or financing.
- All external buy links must use rel="nofollow sponsored".
- Use {{product_short}} whenever you name the product. Never paste a full marketplace listing title; keep names concise (~5–8 words).
- Do NOT use em-dashes or long chained dashes. Prefer commas or short sentences. Hyphens (-) for compound modifiers are fine.
- Avoid formulaic openers like “In {{niche}}, shoppers prioritize…”. Write naturally (e.g., “Background sound can steady anxious pets…”).
- The intro and the product info sections must each be wrapped in <div class="full-width">…</div> so they span the page width.
- Exactly ONE external buy link appears in the intro, and exactly ONE in the product info paragraph. Anchor text must be <strong>Buy on Amazon</strong>.
- Keep language evergreen; no time-sensitive claims.

STRUCTURE (exact order)

2) Intro (80–130 words) — natural opener + single buy link at end
<div id="intro" class="full-width">
  <p><strong>[Write a natural, human opener about the use-case for {{niche}} without using the literal phrase “In {{niche}}”.]</strong> Introduce {{brand}} {{product_short}} early and explain practical benefits (setup friction, portability/fit, maintenance, day-to-day reliability). Keep sentences tight; no em-dashes. <a href="{{affiliate_link_short}}" rel="nofollow sponsored"><strong>Buy on Amazon</strong></a></p>
</div>

3) Pros & Cons (T-chart; balanced; no links inside bullets)
- Start with an <hr> line above the title.
- Title: (h3) “Pros and Cons”.
- Pros: 4–6 bullets. Cons: 2–4 bullets.
- Each bullet begins with a <strong>bold lead word</strong> (e.g., Comfort, Setup, Battery).

<hr />
<h3 id="pros-cons">Pros and Cons</h3>
<div class="pc-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
  <ul>
    <li><strong>[Lead word]:</strong> [Short, concrete benefit.]</li>
    <li><strong>[Lead word]:</strong> [Short, concrete benefit.]</li>
    <li><strong>[Lead word]:</strong> [Short, concrete benefit.]</li>
    <li><strong>[Lead word]:</strong> [Short, concrete benefit.]</li>
  </ul>
  <ul>
    <li><strong>[Lead word]:</strong> [Short, honest limitation.]</li>
    <li><strong>[Lead word]:</strong> [Short, honest limitation.]</li>
  </ul>
</div>
<hr />

4) Product Info Paragraph (180–240 words; exactly one buy link)
- Mention {{brand}} {{product_short}} in the first two lines.
- Describe what’s noteworthy or unique; highlight at least two concrete benefits or outcomes users can expect.
- Keep sentences tight; no em-dashes. Use {{product_short}} (never the raw long listing title).
- Include ONE external buy link with anchor text <strong>Buy on Amazon</strong> exactly once in this paragraph.

<div class="full-width">
  <p>[Write 180–240 words explaining who {{brand}} {{product_short}} is for, key traits, fit/compatibility notes, and simple upkeep. Focus on outcomes such as steadier behavior, easier setup, or cleaner results. Keep language evergreen and readable; no em-dashes or long chained phrases.] <a href="{{affiliate_link_short}}" rel="nofollow sponsored"><strong>Buy on Amazon</strong></a></p>
</div>

5) FAQs — 5 Q&A pairs
- Place an <hr> directly above the FAQ title.
- Title must be (h3) “Frequently Asked Questions”.
- Formatting: each question on its own line in <strong>…</strong>, followed by its answer on the next line. Insert a single blank line between each Q&A pair.
- Topics allowed: compatibility/fit, setup, maintenance/cleaning, safety/best practices, use cases, lifespan/materials, portability. No prices/ratings/returns.
- Keep answers 2–3 sentences each (~45–70 words).

<hr />
<h3 id="faqs">Frequently Asked Questions</h3>

<p><strong>[Question 1 tailored to {{product_short}} and {{niche}}]</strong></p>
<p>[Answer 1 — 2–3 sentences within allowed topics; no em-dashes.]</p>

<p><strong>[Question 2]</strong></p>
<p>[Answer 2 — 2–3 sentences.]</p>

<p><strong>[Question 3]</strong></p>
<p>[Answer 3 — 2–3 sentences.]</p>

<p><strong>[Question 4]</strong></p>
<p>[Answer 4 — 2–3 sentences.]</p>

<p><strong>[Question 5]</strong></p>
<p>[Answer 5 — 2–3 sentences.]</p>
"""

ABOUT_PROMPT = r"""
You are writing the ABOUT page for {{site_name}}.

RULES
- Clean HTML fragment only. No <script>, no external links.
- Tone: professional, plain-English, confident; short paragraphs (1–3 lines).

STRUCTURE
<h1>About {{site_name}}</h1>
<p>{{site_name}} helps pet owners choose gear with confidence. We turn specs into practical takeaways—ease of use, reliability, fit, and cleanup—so you spend less time researching and more time with your pets.</p>

<h2>What We Do</h2>
<ul>
  <li><strong>Roundups:</strong> category pages that explain what matters and where the trade-offs live.</li>
  <li><strong>Reviews:</strong> focused walkthroughs that answer “Will this fit how I actually use it?”</li>
  <li><strong>Buyer’s Guides:</strong> plain-English notes on setup, materials, maintenance—without hype.</li>
</ul>

<h2>How We Evaluate</h2>
<ul>
  <li><strong>Outcome-first:</strong> consistent performance, ergonomics, setup friction, and durability.</li>
  <li><strong>Everyday reality:</strong> space, compatibility, useful accessories, and parts availability.</li>
  <li><strong>Clarity over noise:</strong> specs explained in terms of real-world impact.</li>
</ul>

<h2>Affiliate Disclosure</h2>
<p>Some pages include links to retailers. If a purchase is made through those links, {{site_name}} may earn a commission at no additional cost to you. Recommendations remain independent and based on practical criteria.</p>
"""

# ===== Homepage / About builders =====
def build_about():
    site_name = tomllib.loads((PROJECT / "hugo.toml").read_text()).get("title", "Site")
    html = call_llm(fill(ABOUT_PROMPT, {"site_name": site_name}))
    html = _sanitize_entities(html)
    out_path = CONTENT / "about" / "_index.md"
    write_markdown(out_path, {"title": "About"}, html, overwrite=OVERWRITE_ABOUT)

def build_homepage():
    site_name = tomllib.loads((PROJECT / "hugo.toml").read_text()).get("title", "Site")
    html = call_llm(fill(HOME_PROMPT, {"site_name": site_name})).strip()
    # Fallback if model ever returns too little
    if len(re.sub(r"\s+", "", html)) < 200:
        html = fill("""
<h2>Find the Right Gear, Faster</h2>
<p><strong>{{site_name}} turns hours of research into clear, comparison-ready insights</strong> so you can buy once and love what you pick. We focus on outcomes that matter—comfort, compatibility, and ease of setup.</p>
<p><a href="/roundups/">Explore Products</a></p>
""", {"site_name": site_name}).strip()
    html = _sanitize_entities(html)
    featured_pairs = load_featured_pairs(limit=1)
    grid_html = render_featured_grid(featured_pairs)
    html = html.replace(FEATURED_ANCHOR, grid_html)
    html = html.replace("<!-- GENERATOR_INSERT_ROUNDUPS_GRID -->", grid_html)
    out_path = CONTENT / "_index.md"
    write_markdown(out_path, {"title": ""}, html, overwrite=OVERWRITE_HOMEPAGE)

# ===== Roundups: scaffold enforcer =====
def _extract_intro_for_roundup(body_html: str) -> tuple[str, str]:
    marker = "<h2>Buyer’s Guide</h2>"
    if marker not in body_html: return "", body_html
    head, tail = body_html.split(marker, 1)
    m = re.search(r"<p>.*?</p>", head, flags=re.S)
    intro_html = m.group(0) if m else ""
    if m: head = head.replace(m.group(0), "", 1)
    remainder_html = head + marker + tail
    return intro_html, remainder_html

def _ensure_roundup_scaffold(body_html: str, category: str, niche: str) -> str:
    """Guarantee an intro paragraph + <h2>Buyer’s Guide</h2> even if the model returns little/empty."""
    safe_intro = (
        f"<p><strong>{niche}</strong> choices are easiest to compare when you focus on real outcomes—"
        f"fit, materials, setup friction, and day-to-day reliability—rather than spec noise. "
        f"Use the Buyer’s Guide below to spot trade-offs quickly.</p>"
    )
    content = (body_html or "").strip()
    if len(re.sub(r"\s+", "", content)) < 200:
        return safe_intro + "\n<h2>Buyer’s Guide</h2>"
    if "<h2>Buyer’s Guide</h2>" not in content:
        m = re.search(r"</p>|</ul>|</ol>|</div>", content, flags=re.I)
        if m:
            return content[:m.end()] + "\n<h2>Buyer’s Guide</h2>" + content[m.end():]
        return content + "\n<h2>Buyer’s Guide</h2>"
    return content

def build_roundup_row(row: dict, products: list[dict]):
    category = row["category"].strip()
    niche    = row["niche"].strip()
    cat_slug, niche_slug = slug(category), slug(niche)
    out_path = ROUNDUPS / cat_slug / f"{niche_slug}.md"

    if ADD_ONLY and out_path.exists():
        print(f"[roundup-skip-existing] {out_path}")
        return

    body = call_llm(fill(ROUNDUP_PROMPT, {"category": category, "niche": niche}))
    body = _sanitize_entities(body)
    body = _ensure_roundup_scaffold(body, category, niche)

    hero_src = _find_roundup_hero_src(category, niche)
    if hero_src:
        intro_html, body = _extract_intro_for_roundup(body)
        if intro_html:
            body = _render_roundup_hero_split(hero_src, intro_html) + body

    guide_html = render_buyers_guide_sections(products, category, niche)

    if "<h2>Buyer’s Guide</h2>" in body:
        body = body.replace("<h2>Buyer’s Guide</h2>", f"<h2>Buyer’s Guide</h2>\n{guide_html}", 1)
    else:
        body = body + "\n" + guide_html

    # (Optional) create stubs to avoid 404s on internal review links
    # for p in products:
    #     link = _first_link(p)
    #     if not link: continue
    #     pslug = predict_product_slug(p)
    #     ensure_review_stub_if_missing(p, pslug)

    body = _sanitize_entities(body)
    body = re.sub(r'<a ([^>]*?)rel="([^"]*nofollow sponsored[^"]*)"([^>]*)>', r'<a \1rel="\2 noopener" target="_blank"\3>', body)
    body += "\n" + DISCLOSURE_HTML + "\n"

    fm = {"title": niche, "display_title": niche, "type": "roundup",
          "category": category, "niche": niche, "publish": True}
    write_markdown(out_path, fm, body, overwrite=(not ADD_ONLY))

# ===== Roundup product blurbs =====
CTA_TEXTS = ["See on Amazon", "Check availability &rarr;", "View current options &rarr;", "See details &rarr;"]

def render_buyers_guide_sections(products: list[dict], category: str, niche: str) -> str:
    parts = []
    for i, p in enumerate(products):
        link  = _first_link(p)
        if not link: continue
        title = _digestible_display_name(p)
        bestf = _derive_best_for(p)
        keyf  = _derive_key_feature(p)
        typef = (p.get("type_or_format") or p.get("type") or "").strip()
        trait = (p.get("standout_reason") or p.get("trait") or "").strip()
        pslug = predict_product_slug(p)
        anchor = CTA_TEXTS[i % len(CTA_TEXTS)]

        frags = []
        if trait: frags.append(f"**{trait}**")
        if keyf:  frags.append(f"**{keyf}**")
        if typef: frags.append(f"{typef}")
        if bestf: frags.append(f"best for {bestf.lower()}")
        summary_bits = ", ".join(frags) if frags else "balanced day-to-day performance"

        extra = " In day-to-day use, setup remains predictable and the learning curve is minimal—prioritizing comfort, clarity, and compatibility over spec-sheet noise."

        paragraph = (
            f"<h3>{title}</h3>\n"
            f"<p>{title} in {niche} aims to deliver {summary_bits}. "
            f"It’s designed to keep setup friction low and real-world use predictable—"
            f"focus on comfort, clarity, and compatibility rather than spec-sheet noise.{extra} "
            f'<a href="{link}" target="_blank" rel="nofollow sponsored noopener"><strong>{anchor}</strong></a> · '
            f'<a href="/reviews/{pslug}/"><strong>Read full review &rarr;</strong></a></p>'
        )
        parts.append(_sanitize_entities(paragraph))
    return "\n".join(parts) if parts else "<p>Products will appear here as they’re added.</p>"

def ensure_review_stub_if_missing(row: dict, product_slug: str):
    path = REVIEWS / f"{product_slug}.md"
    if path.exists(): return
    title = _digestible_display_name(row)
    affiliate = _first_link(row)
    body = f'<h2>{title}</h2>\n<p>{STUB_SENTENCE}</p>\n'
    if affiliate:
        body += f'<p><a class="btn" href="{affiliate}" target="_blank" rel="nofollow sponsored noopener">See on Amazon</a></p>\n'
    body += DISCLOSURE_HTML + "\n"
    fm = {
        "title": title, "display_title": title, "type": "review",
        "product_slug": product_slug, "brand": (row.get("brand") or "").strip(),
        "category": (row.get("category") or "").strip(), "niche": (row.get("niche") or "").strip(),
        "publish": True, "stub": True,
    }
    write_markdown(path, fm, _sanitize_entities(body))

# ===== Reviews: helpers kept (not used when pass-through) =====
def _sanitize_top_to_jump_links(body_html: str) -> str:
    if not JUMP_LINKS_ENABLED:
        return body_html
    m = re.search(r'<p><strong>Jump to:</strong>', body_html)
    if not m: return body_html
    return body_html[m.start():]

def render_quick_take_optional(row: dict) -> str:
    best = _derive_best_for(row); feat = _derive_key_feature(row)
    if not best and not feat: return ""
    best = best or "&mdash;"; feat = feat or "&mdash;"
    return (
        '<h3 id="quick-take" style="text-align:center;">Quick Take</h3>\n'
        '<div class="full-width">\n'
        '  <table style="width:100%;">\n'
        '    <thead><tr><th>Best for</th><th>Key feature</th></tr></thead>\n'
        f'    <tbody><tr><td>{best}</td><td>{feat}</td></tr></tbody>\n'
        '  </table>\n'
        '</div>\n'
    )

def _inject_review_lead_paragraph(body_html: str, niche: str, affiliate_link_short: str) -> str:
    return body_html

def _ensure_jump_links(html: str) -> str:
    return html

def _ensure_pros_cons(html: str, product_short: str) -> str:
    if 'id="pros-cons"' in html:
        return html
    block = (
        '<hr />\n'
        f'<h3 id="pros-cons" style="text-align:center;">Pros &amp; Cons of {product_short}</h3>\n'
        '<div class="pc-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">\n'
        '  <ul>\n'
        '    <li><strong>Comfort:</strong> lightweight build and good grip reduce fatigue.</li>\n'
        '    <li><strong>Clarity:</strong> clear labeling or cues speed up setup.</li>\n'
        '    <li><strong>Durability:</strong> reinforced parts extend lifespan.</li>\n'
        '    <li><strong>Versatility:</strong> works across common scenarios at home or travel.</li>\n'
        '  </ul>\n'
        '  <ul>\n'
        '    <li><strong>Learning curve:</strong> some modes/settings may need a quick read-through.</li>\n'
        '    <li><strong>Accessories:</strong> some add-ons may be sold separately.</li>\n'
        '  </ul>\n'
        '</div>\n'
        '<hr />\n'
    )
    return block + html

def _ensure_compare_verdict_faqs(html: str, niche: str, brand: str, product_title: str, affiliate: str) -> str:
    if 'id="faqs"' not in html:
        html += (
            '\n<hr />\n'
            '<h3 id="faqs" style="text-align:center;">Frequently Asked Questions</h3>\n'
            '<p><strong>How do I confirm fit or compatibility?</strong></p>\n'
            '<p>Check dimensions and connector notes against your setup. Measure when in doubt and compare to the product specifications.</p>\n'
            '<p><strong>What basic maintenance keeps performance consistent?</strong></p>\n'
            '<p>Wipe the exterior with a slightly damp cloth and dry it. Keep ports clear and avoid liquids near buttons or vents.</p>\n'
            '<p><strong>How should I set placement?</strong></p>\n'
            '<p>Place it a few feet from your pet on a stable surface, pointed toward the sound or area you want to influence.</p>\n'
            '<p><strong>Can it run while charging?</strong></p>\n'
            '<p>Many units can, but charging cables add clutter. For travel or crate use, charge beforehand and keep cables out of reach.</p>\n'
            '<p><strong>What volume level is sensible?</strong></p>\n'
            '<p>Use the lowest level that masks the distraction while staying comfortable for conversation in the room.</p>\n'
        )
    return html

def _short_name_from_h1(h1: str) -> str:
    return (h1.split(" — ", 1)[0]).strip()

def build_review_row(row: dict, idx: int):
    row = {k: (v or "").strip() for k, v in row.items()}
    category, niche = row.get("category",""), row.get("niche","")
    brand = row.get("brand","")
    product_slug = predict_product_slug(row)
    affiliate_link = _first_link(row)
    cta_label = (row.get("cta_label") or "View Here").strip()
    out_path = REVIEWS / f"{product_slug}.md"

    if ADD_ONLY and _is_nonstub_existing(out_path):
        print(f"[review-skip-existing] {out_path}")
        return

    raw_title = (row.get("display_title") or row.get("product_title") or "").strip()
    if not raw_title:
        human = product_slug.replace("-", " ").title()
        raw_title = (brand + " " + human).strip() if brand else human
    h1, seo_title, crumb, raw_vendor = derive_concise_titles(brand, raw_title, niche)
    product_short = _short_name_from_h1(h1)

    body = call_llm(fill(REVIEW_PROMPT, {
        "product_title": raw_title,
        "brand": brand,
        "category": category,
        "niche": niche,
        "affiliate_link_short": affiliate_link or "",
        "product_short": product_short,
    }))

    # PASS-THROUGH for reviews: do not inject/alter structure; only harden links
    body = re.sub(
        r'<a ([^>]*?)rel="([^"]*nofollow sponsored[^"]*)"([^>]*)>',
        r'<a \1rel="\2 noopener" target="_blank"\3>',
        body
    )

    roundup_url = f"/roundups/{slug(category)}/{slug(niche)}/" if category and niche else "/roundups/"
    btn = f'<p><a class="btn" href="{affiliate_link}" target="_blank" rel="nofollow sponsored noopener">{cta_label}</a></p>' if affiliate_link else ""
    body += f'\n{btn}\n<p><a href="{roundup_url}">← Back to {niche or "roundups"}</a></p>\n' + DISCLOSURE_HTML + "\n"

    fm = {
        "title": h1, "h1": h1, "seo_title": seo_title, "breadcrumb_title": crumb,
        "raw_product_title": raw_vendor, "display_title": h1,
        "type": "review", "product_slug": product_slug, "brand": brand,
        "affiliate_link": affiliate_link, "cta_label": cta_label,
        "category": category, "niche": niche, "publish": True, "stub": False,
    }
    overwrite = (not ADD_ONLY) or (not out_path.exists()) or (not _is_nonstub_existing(out_path))
    write_markdown(out_path, fm, body, overwrite=overwrite)

# ===== CSV loaders / orchestrators =====
def load_reviews_map(reviews_csv: pathlib.Path) -> dict[tuple[str, str], list[dict]]:
    by_niche: dict[tuple[str, str], list[dict]] = {}
    if not reviews_csv or not reviews_csv.exists():
        return by_niche
    with reviews_csv.open(newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            r = _clean_row_keys(raw)
            if r.get("publish","yes").lower() not in {"yes","true","1"}: continue
            cat, niche = r.get("category",""), r.get("niche","")
            if not cat or not niche: continue
            key = (slug(cat), slug(niche))
            by_niche.setdefault(key, []).append(r)
    return by_niche

def build_roundups_from_sources(reviews_csv: pathlib.Path, roundups_csv: pathlib.Path):
    print("[roundups] start")
    reviews_map = load_reviews_map(reviews_csv)
    desired_keys: list[tuple[str, str, str, str]] = []
    if roundups_csv.exists():
        with roundups_csv.open(newline="", encoding="utf-8") as f:
            for raw in csv.DictReader(f):
                row = _clean_row_keys(raw)
                pub = row.get("publish","yes").lower() in {"yes","true","1"}
                cat, niche = row.get("category",""), row.get("niche","")
                if pub and cat and niche:
                    desired_keys.append((cat, niche, slug(cat), slug(niche)))
    if not desired_keys:
        for (cat_s, niche_s), items in reviews_map.items():
            sample = items[0]
            desired_keys.append((sample.get("category",""), sample.get("niche",""), cat_s, niche_s))
    seen=set(); dedup=[]
    for tpl in desired_keys:
        key=(tpl[2],tpl[3])
        if key in seen: continue
        seen.add(key); dedup.append(tpl)
    desired_keys = dedup
    print(f"[roundups] unique_keys={len(desired_keys)}")
    for cat_lbl, niche_lbl, cat_s, niche_s in desired_keys:
        products = reviews_map.get((cat_s, niche_s), [])
        build_roundup_row({"category": cat_lbl, "niche": niche_lbl}, products)

def build_reviews_from_csv(reviews_csv: pathlib.Path):
    print("[reviews] start")
    if not reviews_csv.exists():
        print("[reviews] no CSV found")
        return
    with reviews_csv.open(newline="", encoding="utf-8") as f:
        for idx, raw in enumerate(csv.DictReader(f), start=1):
            row = _clean_row_keys(raw)
            if row.get("publish","yes").lower() not in {"yes","true","1"}:
                continue
            build_review_row(row, idx)

# ===== One-time cleaner: remove old inner hero titles =====
def clean_existing_roundup_hero_titles():
    if not ROUNDUPS.exists(): return
    if ADD_ONLY and not CLEAN_ROUNDUP_HERO_TITLES:
        print("[cleaner-skip] ADD_ONLY=True and CLEAN_ROUNDUP_HERO_TITLES=False; not touching existing roundups.")
        return
    for md in ROUNDUPS.rglob("*.md"):
        txt = md.read_text(encoding="utf-8")
        changed = re.sub(
            r'(<section class="hero-split".*?>.*?)(<h1 class="page-title".*?</h1>)(.*?)</section>',
            lambda m: m.group(1) + m.group(3) + "</section>",
            txt, flags=re.S
        )
        if changed != txt:
            md.write_text(changed, encoding="utf-8")
            print(f"[cleaned-hero-title] {md}")

# ===== entry =====
if __name__ == "__main__":
    build_homepage()
    build_about()
    build_roundups_from_sources(PROJECT / "data" / "reviews.csv", PROJECT / "data" / "roundups.csv")
    build_reviews_from_csv(PROJECT / "data" / "reviews.csv")
    clean_existing_roundup_hero_titles()
