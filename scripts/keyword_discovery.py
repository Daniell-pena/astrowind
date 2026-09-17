"""Discover keywords from Google Autocomplete + Reddit RSS, store in SQLite.

Usage:
  python scripts/keyword_discovery.py --site sleepupgradehub
  python scripts/keyword_discovery.py --site sleepupgradehub --limit 5
"""
from __future__ import annotations

import _env  # noqa: F401

import argparse
import json
import random
import re
import sys
import time
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import requests

from db import get_conn, site_id
from niches import NICHES

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def google_autocomplete(query: str) -> list[str]:
    url = ("https://suggestqueries.google.com/complete/search"
           f"?client=firefox&q={quote_plus(query)}")
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": UA})
        r.raise_for_status()
        data = r.json()
        return data[1] if isinstance(data, list) and len(data) > 1 else []
    except Exception as e:
        print(f"  autocomplete error for {query!r}: {e}")
        return []


def reddit_hot(subreddit: str, limit: int = 20) -> list[str]:
    url = f"https://www.reddit.com/r/{subreddit}/hot.rss?limit={limit}"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": UA})
        r.raise_for_status()
        root = ET.fromstring(r.text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        return [t.text for t in root.findall(".//a:entry/a:title", ns) if t.text]
    except Exception as e:
        print(f"  reddit r/{subreddit} error: {e}")
        return []


# Head/brand terms a brand-new, low-authority domain cannot rank for. These
# are pushed to the bottom so daily auto-publish stops wasting posts on them.
_BRAND_SERP_TERMS = (
    "wirecutter", "amazon", "consumer reports", "nyt", "new york times",
    "forbes", "cnet", "reviews.com", "good housekeeping",
)
_QUESTION_STARTS = ("how ", "why ", "what ", "when ", "is ", "are ", "does ",
                    "do ", "can ", "should ", "will ", "which ")


def _pick_category(kw: str, niche_cfg: dict) -> str:
    cats = niche_cfg["categories"]
    cat_map = niche_cfg.get("keyword_to_category", {})
    for hint, cat in cat_map.items():
        if hint in kw:
            return cat
    return cats[0]


def score_and_classify(keyword: str, niche_cfg: dict) -> dict:
    """Score a keyword for PUBLISH PRIORITY on a low-authority site.

    Strategy: reward specific long-tail questions/problems (rankable), penalize
    bare head terms and brand-owned SERPs (unwinnable). Higher score = published
    sooner by batch.py.
    """
    kw = keyword.lower().strip()
    words = kw.split()
    wc = len(words)
    score = 5

    # Brand-owned SERP -> effectively un-rankable for a new site: bottom it.
    if any(t in kw for t in _BRAND_SERP_TERMS):
        return {"score": 1, "type": "affiliate",
                "category": _pick_category(kw, niche_cfg)}

    # Long-tail specificity is the main ranking lever for a small site.
    if wc >= 7: score += 5
    elif wc >= 5: score += 3
    elif wc >= 4: score += 1

    # Question / problem intent (great for featured snippets + AI answers).
    if kw.startswith(_QUESTION_STARTS): score += 3
    # Specific qualifiers that narrow the SERP in our favor.
    if " for " in f" {kw} ": score += 1
    if any(t in kw for t in ("every night", "at night", "without", "vs ",
                             "at 3am", "in the middle of the night",
                             "after", "before bed")): score += 1

    # Mild commercial signals (affiliate value) — small, not dominant.
    if "buy" in kw or "price" in kw or "worth it" in kw: score += 1

    # Penalize bare head terms: "best pillow", "oura ring review" (<= 3 words).
    head = (kw.startswith("best ") or kw.endswith(" review")
            or kw.endswith(" reviews"))
    if head and wc <= 3:
        score = min(score, 2)

    score = max(1, min(score, 14))  # curated long-tail (>=15) still ranks above

    if head or " vs " in kw or "worth it" in kw:
        type_ = "affiliate"
    elif kw.startswith(_QUESTION_STARTS):
        type_ = "adsense"
    else:
        type_ = "informational"

    return {"score": score, "type": type_,
            "category": _pick_category(kw, niche_cfg)}


def discover(site_slug: str, limit: int = 10) -> int:
    cfg = next((c for c in NICHES.values() if c["site_slug"] == site_slug), None)
    if not cfg:
        print(f"unknown site: {site_slug}")
        return 0

    seeds = cfg["seed_keywords"]
    modifiers = cfg["modifiers"]
    subreddits = cfg.get("subreddits", [])

    pool: set[str] = set()

    sample_seeds = random.sample(seeds, min(limit, len(seeds)))
    for s in sample_seeds:
        for m in modifiers:
            q = f"{s} {m}".strip()
            for sug in google_autocomplete(q):
                pool.add(sug.lower().strip())
            time.sleep(0.4)

    for sr in subreddits[:2]:
        for title in reddit_hot(sr, limit=15):
            t = re.sub(r"[^a-z0-9\s]", " ", title.lower())[:80].strip()
            niche_terms = (cfg["seed_keywords"][0]).split()[0]
            if any(seed.split()[0] in t for seed in cfg["seed_keywords"][:5]):
                pool.add(t)
        time.sleep(0.5)

    pool = {k for k in pool if 8 <= len(k) <= 80}

    conn = get_conn()
    sid = site_id(conn, site_slug)

    existing = {
        r["keyword"].lower()
        for r in conn.execute(
            "SELECT keyword FROM keywords WHERE site_id = ?", (sid,)
        ).fetchall()
    }
    written_titles = {
        (r["title"] or "").lower()
        for r in conn.execute(
            "SELECT title FROM posts WHERE site_id = ?", (sid,)
        ).fetchall()
    }

    new_count = 0
    for kw in pool:
        if kw in existing:
            continue
        if any(kw in t or t.startswith(kw[:20]) for t in written_titles):
            continue
        meta = score_and_classify(kw, cfg)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO keywords "
                "(site_id, keyword, score, type, category, source, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'new')",
                (sid, kw, meta["score"], meta["type"],
                 meta["category"], "google_autocomplete"),
            )
            new_count += 1
        except Exception as e:
            print(f"  insert error for {kw!r}: {e}")
    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM keywords WHERE site_id = ? AND status = 'new'",
        (sid,),
    ).fetchone()["n"]
    print(f"[discover] +{new_count} new keywords for {site_slug} (queue: {total})")
    conn.close()
    return new_count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    sys.exit(0 if discover(args.site, args.limit) >= 0 else 1)


if __name__ == "__main__":
    main()
