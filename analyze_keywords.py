#!/usr/bin/env python3
"""Find interest_model.json coverage gaps: papers the user liked (up/star)
that scored 0 or matched nothing. Prints candidate titles/abstracts so a human
(or Claude) can eyeball them and propose new keyword/weight additions.

This script only ANALYZES — it never edits interest_model.json itself.

Standard library only; no packages to install.
Usage: python3 analyze_keywords.py
"""
import json
import sys
from pathlib import Path

# Windows consoles default to a legacy codepage (cp950 etc.) that can't print
# every unicode character in abstracts/titles; force UTF-8 so this never crashes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
MARKS_PATH = ROOT / "marks.json"
PAPERS_PATH = ROOT / "docs" / "papers.json"
SCORE_THRESHOLD = 5  # liked papers scoring at or below this are flagged as gaps


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    marks = load_json(MARKS_PATH, {})
    papers = load_json(PAPERS_PATH, {"papers": []})["papers"]
    by_doi = {p["doi"]: p for p in papers}

    liked = [doi for doi, m in marks.items() if m.get("up") or m.get("star")]
    gaps = []
    for doi in liked:
        p = by_doi.get(doi)
        if not p:
            continue  # aged out of the 90-day window; can't inspect anymore
        if p.get("score", 0) <= SCORE_THRESHOLD:
            gaps.append(p)

    print(f"{len(liked)} liked paper(s) total, {len(gaps)} scored <= {SCORE_THRESHOLD} (coverage gap candidates)\n")
    for p in sorted(gaps, key=lambda x: x.get("score", 0)):
        print(f"[score {p.get('score')}] {p['title']}")
        print(f"  journal: {p.get('journal')}  matched: {p.get('matched')}")
        abstract = (p.get("abstract") or "")[:300]
        if abstract:
            print(f"  abstract: {abstract}...")
        print()


if __name__ == "__main__":
    main()
