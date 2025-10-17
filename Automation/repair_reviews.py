# Automation/repair_reviews.py — safe repair with validation + retry + per-file targeting
from __future__ import annotations
import re, argparse, pathlib, yaml
from generate_content import (
    PROJECT, REVIEWS, call_llm, fill, REVIEW_PROMPT,
    derive_concise_titles, slug, DISCLOSURE_HTML
)

# ---- validation settings (match your REVIEW_PROMPT) ----
REVIEW_MIN_CHARS = 500
REQUIRED_MARKERS = ('class="full-width"', 'id="pros-cons"', 'id="faqs"')

def _read_fm_body(md_path: pathlib.Path):
    txt = md_path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)\Z", txt, flags=re.S)
    if not m:
        return {}, txt
    fm = yaml.safe_load(m.group(1)) or {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, m.group(2)

def _write_fm_body(md_path: pathlib.Path, fm: dict, body: str):
    front = "---\n" + yaml.safe_dump(fm, sort_keys=False).strip() + "\n---\n\n"
    md_path.write_text(front + body, encoding="utf-8")

def _is_ok(html: str) -> bool:
    if not html:
        return False
    clean = re.sub(r"\s+", "", html)
    if len(clean) < REVIEW_MIN_CHARS:
        return False
    return all(marker in html for marker in REQUIRED_MARKERS)

def _gen_once(fm: dict) -> str:
    brand = (fm.get("brand") or "").strip()
    category = (fm.get("category") or "").strip()
    niche = (fm.get("niche") or "").strip()
    raw_title = (fm.get("raw_product_title") or fm.get("display_title") or fm.get("title") or "").strip()
    affiliate = (fm.get("affiliate_link") or "").strip()

    h1, *_ = derive_concise_titles(brand, raw_title, niche)
    product_short = (h1.split(" — ", 1)[0]).strip()

    html = call_llm(fill(REVIEW_PROMPT, {
        "product_title": raw_title,
        "brand": brand,
        "category": category,
        "niche": niche,
        "affiliate_link_short": affiliate,
        "product_short": product_short,
    }))

    # harden links (same as generator)
    html = re.sub(
        r'<a ([^>]*?)rel="([^"]*nofollow sponsored[^"]*)"([^>]*)>',
        r'<a \1rel="\2 noopener" target="_blank"\3>',
        html or ""
    )

    # append standard footer (same as generator)
    roundup_url = f"/roundups/{slug(category)}/{slug(niche)}/" if category and niche else "/roundups/"
    cta_label = (fm.get("cta_label") or "View Here").strip()
    btn = f'\n<p><a class="btn" href="{affiliate}" target="_blank" rel="nofollow sponsored noopener">{cta_label}</a></p>' if affiliate else ""
    html = (html or "") + f'{btn}\n<p><a href="{roundup_url}">← Back to {niche or "roundups"}</a></p>\n' + DISCLOSURE_HTML + "\n"
    return html

def _repair_one(md: pathlib.Path, tries: int, write_stub_on_fail: bool) -> str:
    fm, old_body = _read_fm_body(md)
    if not fm or fm.get("stub") is True:
        return "skip-stub-or-bad-fm"

    html = ""
    for _ in range(max(1, tries)):
        html = _gen_once(fm)
        if _is_ok(html):
            break

    if not _is_ok(html):
        if write_stub_on_fail:
            fm["stub"] = True
            _write_fm_body(md, fm, f"<p>This review is being prepared.</p>\n{DISCLOSURE_HTML}\n")
            return "stubbed"
        return "skip-bad-body"

    _write_fm_body(md, fm, html)
    return "rewrote"

def needs_repair(html: str) -> bool:
    if not html:
        return True
    fullw = len(re.findall(r'class="full-width"', html))
    has_pros = ('id="pros-cons"' in html) or re.search(r'>\s*Pros\s*and\s*Cons\s*<', html, re.I)
    return (fullw < 2) or (not has_pros)

def _select_files(only: str | None) -> list[pathlib.Path]:
    files = sorted(REVIEWS.glob("*.md"))
    if not only:
        return files
    needle = only.lower().rstrip(".md")
    return [p for p in files if needle in p.name.lower()]

def main(dry_run: bool, limit: int | None, write_stub_on_fail: bool, only: str | None, tries: int):
    files = _select_files(only)
    flagged = rewritten = stubbed = skipped = 0
    for md in files:
        fm, body = _read_fm_body(md)
        if not fm or fm.get("stub") is True:
            continue
        if needs_repair(body):
            flagged += 1
            print(f"[needs-repair] {md.name}")
            if dry_run:
                continue
            result = _repair_one(md, tries, write_stub_on_fail)
            if result == "rewrote":
                rewritten += 1; print(f"[rewrote] {md.name}")
            elif result == "stubbed":
                stubbed += 1; print(f"[stubbed] {md.name}")
            else:
                skipped += 1; print(f"[skip-bad-body] {md.name}")
            if limit and (rewritten + stubbed + skipped) >= limit:
                break
    print(f"\nSummary: flagged={flagged}, rewritten={rewritten}, stubbed={stubbed}, skipped_bad={skipped}, total={len(files)}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Safely repair FAQ-only/empty reviews using current REVIEW_PROMPT.")
    ap.add_argument("--dry-run", action="store_true", help="List pages that would be repaired, but don’t write.")
    ap.add_argument("--limit", type=int, default=None, help="Max pages to process this run.")
    ap.add_argument("--write-stub-on-fail", action="store_true",
                    help="If model fails tries times, mark page as stub instead of leaving it broken.")
    ap.add_argument("--only", type=str, default=None,
                    help="Repair only files whose name contains this text (slug or partial filename).")
    ap.add_argument("--tries", type=int, default=2, help="Retries per file (default 2).")
    args = ap.parse_args()
    main(dry_run=args.dry_run, limit=args.limit, write_stub_on_fail=args.write_stub_on_fail,
         only=args.only, tries=args.tries)
