"""Seed curated LONG-TAIL, low-competition keywords for SleepUpgradeHub and
re-prioritize the existing queue.

Why this exists
---------------
The auto-publish queue was ordered by a score that rewarded head terms
("best pillow", "oura ring review", "... wirecutter") which a brand-new,
low-authority domain cannot rank for. Two months of daily posting produced
~1 organic-search visitor per week. This script fixes the strategy at the
source:

  1. Inserts a hand-picked set of long-tail keywords (specific questions and
     problems) that a small site CAN realistically rank for, and that still
     lead to a product recommendation (affiliate) or a featured snippet / AI
     answer (AEO). Each gets a high score so it publishes BEFORE the old
     head-term backlog.
  2. Demotes the clearly-unwinnable backlog: brand-SERP terms (wirecutter /
     amazon / consumer reports / nyt / forbes / cnet) are rejected, and bare
     2-3 word head terms are pushed to the bottom (score 1) instead of the top.

Idempotent: safe to run more than once (INSERT OR IGNORE on keyword).

Usage:
  python scripts/seed_longtail.py               # apply
  python scripts/seed_longtail.py --dry-run     # show what would change
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from db import get_conn, site_id

SITE = "sleepupgradehub"

# Score bands (backlog max was 9, so anything >= 15 jumps ahead of it):
#   22 = strongest: high AEO + clear affiliate pull, easy to rank
#   20 = strong long-tail question/problem
#   18 = solid long-tail
HIGH, MED, LOW = 22, 20, 18

# (keyword, score). Category is re-derived from the keyword text by
# pipeline._category_for_keyword, so it is not stored here.
LONGTAIL: list[tuple[str, int]] = [
    # --- Falling asleep (onset) — informational, snippet-friendly -----------
    ("how to fall asleep when your mind is racing", 22),
    ("how to fall asleep faster naturally", 20),
    ("what to do when you cant sleep at night", 20),
    ("how to quiet your mind before bed", 20),
    ("how to fall asleep when you are not tired", 18),
    ("breathing techniques to fall asleep fast", 20),
    ("how to fall asleep in 5 minutes", 18),

    # --- Night waking — very high AEO, low competition ----------------------
    ("why do i wake up at 3am every night", 22),
    ("how to stop waking up at 3am", 22),
    ("why do i wake up at 4am and cant fall back asleep", 20),
    ("how to fall back asleep after waking up", 20),
    ("waking up at the same time every night meaning", 20),
    ("why do i keep waking up at night to pee", 18),

    # --- Waking up tired — problem searches ---------------------------------
    ("why do i wake up tired after 8 hours of sleep", 22),
    ("why am i still tired after sleeping all night", 20),
    ("how to wake up feeling refreshed", 20),
    ("why do i feel groggy in the morning", 18),
    ("how to stop hitting the snooze button", 18),

    # --- Magnesium & supplements — strong affiliate -------------------------
    ("magnesium glycinate vs citrate for sleep", 22),
    ("best time to take magnesium for sleep", 22),
    ("how much magnesium should i take for sleep", 20),
    ("is it safe to take magnesium for sleep every night", 20),
    ("magnesium vs melatonin for sleep", 20),
    ("does magnesium actually help you sleep", 18),
    ("best magnesium supplement for sleep and anxiety", 20),

    # --- Melatonin — affiliate + safety AEO ---------------------------------
    ("how much melatonin is safe for adults", 22),
    ("is it bad to take melatonin every night", 22),
    ("best time to take melatonin before bed", 20),
    ("melatonin side effects the next day", 20),
    ("why does melatonin give me weird dreams", 18),
    ("melatonin vs magnesium for staying asleep", 18),

    # --- Trackers & devices — affiliate -------------------------------------
    ("is the oura ring worth it for sleep tracking", 22),
    ("oura ring vs apple watch for sleep tracking", 20),
    ("how accurate are sleep trackers really", 20),
    ("best sleep tracker without a subscription", 22),
    ("can a smartwatch detect sleep apnea", 18),
    ("do fitness trackers measure deep sleep accurately", 18),

    # --- Environment & gadgets — affiliate ----------------------------------
    ("best room temperature for sleep", 22),
    ("does a weighted blanket really help you sleep", 20),
    ("do blackout curtains help you sleep better", 20),
    ("is mouth taping safe for sleep", 20),
    ("best color light for sleeping", 18),
    ("does a cooling mattress topper actually work", 18),

    # --- White / brown noise — affiliate + ties to the noise tool -----------
    ("is white noise or brown noise better for sleep", 22),
    ("does brown noise help you sleep", 20),
    ("is it bad to sleep with white noise every night", 18),
    ("best sound to fall asleep to", 18),

    # --- Caffeine — ties to the Caffeine Calculator tool --------------------
    ("how long before bed should i stop drinking coffee", 22),
    ("how long does caffeine keep you awake", 20),
    ("does caffeine affect deep sleep", 20),
    ("why can i fall asleep after drinking coffee", 18),

    # --- Habits & routines — ties to Sleep / Nap calculators ----------------
    ("how long before bed should i stop eating", 20),
    ("how many hours before bed should i stop drinking water", 18),
    ("is it bad to work out right before bed", 18),
    ("how to fix your sleep schedule after night shift", 20),
    ("how to nap without ruining your sleep", 20),
    ("does reading before bed actually help you sleep", 18),
    ("how to reset your sleep schedule in 3 days", 20),
]

# Backlog demotion --------------------------------------------------------
BRAND_SERP_TERMS = (
    "wirecutter", "amazon", "consumer reports", "nyt", "new york times",
    "forbes", "cnet", "reviews.com", "good housekeeping",
)


def apply(dry_run: bool = False) -> None:
    conn = get_conn()
    sid = site_id(conn, SITE)

    # 1) Insert curated long-tail keywords (idempotent) -------------------
    inserted = 0
    for kw, score in LONGTAIL:
        cur = conn.execute(
            "INSERT OR IGNORE INTO keywords "
            "(site_id, keyword, score, type, category, source, status) "
            "VALUES (?, ?, ?, 'longtail', 'Habits', 'curated_longtail', 'new')",
            (sid, kw.lower().strip(), score),
        )
        if cur.rowcount:
            inserted += 1
        else:
            # already present -> make sure it is prioritized and not stale
            conn.execute(
                "UPDATE keywords SET score = ?, status = 'new' "
                "WHERE site_id = ? AND keyword = ? AND status != 'written'",
                (score, sid, kw.lower().strip()),
            )

    # 2) Reject brand-SERP terms in the backlog ---------------------------
    rejected = 0
    rows = conn.execute(
        "SELECT id, keyword FROM keywords WHERE site_id = ? AND status = 'new'",
        (sid,),
    ).fetchall()
    for r in rows:
        kw = r["keyword"]
        if any(t in kw for t in BRAND_SERP_TERMS):
            if not dry_run:
                conn.execute(
                    "UPDATE keywords SET status = 'rejected' WHERE id = ?",
                    (r["id"],),
                )
            rejected += 1

    # 3) Push bare head terms to the bottom (score 1) ---------------------
    #    "best X" / "X review" with <= 3 words = head term a new site loses.
    demoted = 0
    rows = conn.execute(
        "SELECT id, keyword, score FROM keywords "
        "WHERE site_id = ? AND status = 'new' AND source != 'curated_longtail'",
        (sid,),
    ).fetchall()
    for r in rows:
        kw = r["keyword"]
        wc = len(kw.split())
        head = (kw.startswith("best ") or kw.endswith(" review")
                or kw.endswith(" reviews"))
        if head and wc <= 3 and r["score"] > 1:
            if not dry_run:
                conn.execute(
                    "UPDATE keywords SET score = 1 WHERE id = ?", (r["id"],)
                )
            demoted += 1

    if not dry_run:
        conn.commit()

    # Report --------------------------------------------------------------
    total_new = conn.execute(
        "SELECT COUNT(*) n FROM keywords WHERE site_id = ? AND status = 'new'",
        (sid,),
    ).fetchone()["n"]
    print(f"[seed_longtail] {'DRY RUN — ' if dry_run else ''}done")
    print(f"  curated long-tail inserted (new): {inserted} / {len(LONGTAIL)}")
    print(f"  brand-SERP terms rejected:        {rejected}")
    print(f"  bare head terms demoted to score 1: {demoted}")
    print(f"  queue size (status=new):          {total_new}")
    print("  next 15 to publish:")
    for r in conn.execute(
        "SELECT keyword, score FROM keywords WHERE site_id = ? AND status = 'new' "
        "ORDER BY score DESC, keyword LIMIT 15",
        (sid,),
    ):
        print(f"    [{r['score']}] {r['keyword']}")
    conn.close()


def write_markdown() -> Path:
    """Write the human-readable deliverable list."""
    out = Path(__file__).resolve().parent.parent / "data" / "keywords-longtail-sleep.md"
    lines = [
        "# SleepUpgradeHub — Long-tail keyword targets",
        "",
        "Hand-picked, low-competition long-tail keywords a new low-authority",
        "site can realistically rank for. Each leads to a product recommendation",
        "(affiliate) or a featured snippet / AI answer (AEO). Loaded into the",
        "publish queue with a high score by `scripts/seed_longtail.py`, so they",
        "publish before the old head-term backlog.",
        "",
        f"Total: {len(LONGTAIL)} keywords.",
        "",
        "| # | Keyword | Score |",
        "|---|---------|-------|",
    ]
    for i, (kw, score) in enumerate(LONGTAIL, 1):
        lines.append(f"| {i} | {kw} | {score} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    md = write_markdown()
    print(f"[seed_longtail] wrote {md}")
    apply(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
