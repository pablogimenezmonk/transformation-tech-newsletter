#!/usr/bin/env python3
"""Convert last30days JSON exports into Transformation Tech Newsletter inputs.

Three output formats:

  --format email-html   (default) An HTML "newsletter email" that the existing n8n
                        pipeline can feed to the Gemini curation prompt unchanged:
                        one <a href> per candidate with title, description and
                        engagement context, exactly like a forwarded newsletter.
  --format candidates   Flat JSON array of candidate links (title, link, description,
                        source, engagement, relevance) for any other consumer.
  --format issue        A ready-to-render data/newsletters/*.json issue, bucketed by
                        a popularity heuristic. PoC only; the real editorial call
                        belongs to the Gemini step.

Inputs are any mix of research exports (--emit=json) and discovery exports
(--discover ... --emit=json) from last30days.py. No third-party dependencies.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

SOURCE_LABEL = {
    "reddit": "Reddit",
    "hackernews": "Hacker News",
    "github": "GitHub",
    "grounding": "Web",
    "x": "X",
    "youtube": "YouTube",
    "polymarket": "Polymarket",
    "arxiv": "arXiv",
    "techmeme": "Techmeme",
    "jobs": "Jobs",
    "devto": "DEV Community",
    "digg": "Digg",
}

# Which native engagement counter to show per source, in priority order.
ENGAGEMENT_KEYS = {
    "reddit": [("score", "upvotes"), ("num_comments", "comments")],
    "hackernews": [("points", "points"), ("num_comments", "comments")],
    "github": [("stars", "stars"), ("stargazers_count", "stars"), ("comments", "comments")],
    "x": [("likes", "likes"), ("reposts", "reposts")],
    "youtube": [("views", "views"), ("likes", "likes")],
    "devto": [("reactions", "reactions"), ("comments", "comments")],
}

# Sources that never carry a link worth curating on their own.
SKIP_SOURCES = {"jobs", "polymarket"}

# Ad and tracking redirectors that the keyless web backends sometimes leak into evidence URLs.
JUNK_URL = re.compile(r"^https?://(duckduckgo\.com/y\.js|www\.bing\.com/aclick|www\.google\.com/aclk|.*\.doubleclick\.net/)", re.I)

# Minimum engine relevance for a result to be offered to the curator at all.
CANDIDATE_FLOOR = 0.25
MAX_CANDIDATES = 80

# --- issue-format heuristics (PoC only) -------------------------------------
USEFUL_FLOOR = 0.40
MAYBE_FLOOR = 0.30
USEFUL_ENGAGEMENT = 50
USEFUL_ENGAGEMENT_BY_SOURCE: dict[str, list[tuple[str, int]]] = {
    "youtube": [("likes", 100), ("views", 10_000)],
    "devto": [("reactions", 20), ("comments", 10)],
    "github": [("stars", 50), ("comments", 50)],
}


def slugify(text: str) -> str:
    return re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", text.lower()))


def humanize_url(url: str) -> str:
    """'https://fortune.com/2026/10/30/goldman-sachs-ai-layoffs' -> 'fortune.com: goldman sachs ai layoffs'."""
    m = re.match(r"https?://(?:www\.)?([^/]+)/?(.*)", url)
    if not m:
        return url
    host, path = m.group(1), m.group(2)
    segments = [
        s for s in re.split(r"[/?#]", path)
        if s and "=" not in s and not re.fullmatch(r"[\d_-]+|[0-9a-f]{8,}|p|blog|posts?|articles?|items?|comments", s)
    ]
    tokens = re.split(r"[-_+%.]+", segments[-1]) if segments else []
    # drop ids: pure digits, hex chunks, trailing short hashes like dev.to's "571b"
    tokens = [t for t in tokens if t and not re.fullmatch(r"\d+|[0-9a-f]*\d[0-9a-f]*", t.lower())]
    if len(tokens) < 2:
        return host
    return f"{host}: {' '.join(tokens)}"


def engagement_phrase(source: str, engagement: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, label in ENGAGEMENT_KEYS.get(source, []):
        value = engagement.get(key)
        if isinstance(value, (int, float)) and value > 0:
            parts.append(f"{int(value):,} {label}")
    if not parts:
        numeric = {k: v for k, v in engagement.items() if isinstance(v, (int, float)) and v > 0}
        if numeric:
            k, v = max(numeric.items(), key=lambda kv: kv[1])
            parts.append(f"{int(v):,} {k}")
    return ", ".join(parts)


def max_engagement(engagement: dict[str, Any]) -> int:
    numeric = [v for v in engagement.values() if isinstance(v, (int, float))]
    return int(max(numeric)) if numeric else 0


def strong_engagement(source: str, engagement: dict[str, Any]) -> bool:
    floors = USEFUL_ENGAGEMENT_BY_SOURCE.get(source)
    if floors:
        return any(isinstance(engagement.get(k), (int, float)) and engagement[k] >= floor for k, floor in floors)
    return max_engagement(engagement) >= USEFUL_ENGAGEMENT


def bucket_for(score: float, engagement: dict[str, Any], source: str = "") -> str:
    if score >= USEFUL_FLOOR and strong_engagement(source, engagement):
        return "useful"
    if score >= MAYBE_FLOOR:
        return "maybeUseful"
    return "discarded"


# --- candidate extraction ----------------------------------------------------

def candidate_from_result(result: dict[str, Any], topic: str, clusters: list[dict[str, Any]]) -> dict[str, Any] | None:
    url = (result.get("url") or "").strip()
    title = (result.get("title") or "").strip()
    source = result.get("source", "web")
    if not url or not title or source in SKIP_SOURCES or JUNK_URL.match(url):
        return None
    score = float(result.get("relevance_score", 0.0))
    engagement = result.get("engagement") or {}
    cluster_title = ""
    idx = result.get("cluster")
    if isinstance(idx, int) and 0 <= idx < len(clusters):
        cluster_title = clusters[idx].get("title", "")
    return {
        "title": title,
        "link": url,
        "description": (result.get("summary") or "").strip()[:600],
        "source": SOURCE_LABEL.get(source, source.title()),
        "source_key": source,
        "topic": topic,
        "engagement": engagement_phrase(source, engagement),
        "engagement_raw": engagement,
        "relevance": round(score, 3),
        "cluster": cluster_title if cluster_title != title else "",
        "published_at": str(result.get("published_at") or "")[:10],
        "kind": "research",
    }


def candidates_from_research(doc: dict[str, Any]) -> list[dict[str, Any]]:
    topic = doc.get("query", "ai")
    clusters = doc.get("clusters", [])
    out = []
    for result in doc.get("results", []):
        cand = candidate_from_result(result, topic, clusters)
        if cand and cand["relevance"] >= CANDIDATE_FLOOR:
            out.append(cand)
    return out


def candidates_from_discovery(doc: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in doc.get("results", []):
        urls = [u for u in item.get("evidence_urls", []) if u and not JUNK_URL.match(u)]
        if not urls:
            continue
        topic = item.get("topic", "Untitled topic")
        eng = item.get("engagement") or {}
        per_source = []
        if isinstance(eng, dict):
            for s, v in eng.items():
                phrase = engagement_phrase(s, v) if isinstance(v, dict) else str(v)
                if phrase:
                    per_source.append(f"{SOURCE_LABEL.get(s, s)} {phrase}")
        description = (item.get("why_spiking") or "").strip()
        top = item.get("top_comment")
        if top:
            description += f' Top comment: "{str(top)[:240]}"'
        out.append(
            {
                "title": topic,
                "link": urls[0],
                "description": description,
                "source": "Trending (" + ", ".join(SOURCE_LABEL.get(s, s) for s in item.get("sources", [])) + ")",
                "source_key": "discovery",
                "topic": f"trending:{doc.get('domain') or 'global'}",
                "engagement": "; ".join(per_source),
                "engagement_raw": eng if isinstance(eng, dict) else {},
                "relevance": 1.0,
                "cluster": "",
                "published_at": "",
                "kind": "discovery",
                "extra_links": urls[1:4],
            }
        )
    return out


def collect(docs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    candidates: list[dict[str, Any]] = []
    source_status: dict[str, str] = {}
    topics: list[str] = []
    for doc in docs:
        source_status.update(doc.get("source_status", {}))
        if doc.get("kind") == "discovery":
            candidates.extend(candidates_from_discovery(doc))
            topics.append(f"trending:{doc.get('domain') or 'global'}")
        else:
            candidates.extend(candidates_from_research(doc))
            topics.append(doc.get("query", "?"))

    # Dedupe by URL, keeping the highest relevance; then rank by relevance, engagement.
    best: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        key = cand["link"].rstrip("/").lower()
        if key not in best or cand["relevance"] > best[key]["relevance"]:
            best[key] = cand
    ranked = sorted(best.values(), key=lambda c: (-c["relevance"], -max_engagement(c["engagement_raw"])))
    return ranked[:MAX_CANDIDATES], source_status, topics


# --- renderers ---------------------------------------------------------------

def render_email_html(cands: list[dict[str, Any]], source_status: dict[str, str], topics: list[str], issue_date: str) -> str:
    ok_sources = sorted(SOURCE_LABEL.get(s, s) for s, st in source_status.items() if st == "ok")
    failed = sorted(f"{SOURCE_LABEL.get(s, s)} ({st})" for s, st in source_status.items() if st not in ("ok", "no-results", "skipped-unconfigured"))
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Community Signal - {e(issue_date)}</title></head><body>",
        f"<h1>Community Signal - week of {e(issue_date)}</h1>",
        "<p>Links engineers actually discussed in the last 30 days, gathered by "
        "<a href='https://github.com/mvanhorn/last30days-skill'>last30days</a> from "
        f"{e(', '.join(ok_sources) or 'no live sources')}. Each item shows the native engagement "
        "(points, upvotes, views, reactions) as context. Topics searched: "
        f"{e(', '.join(topics))}.</p>",
    ]
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for c in cands:
        by_topic.setdefault(c["topic"], []).append(c)
    for topic, items in by_topic.items():
        heading = "Trending in AI this month" if topic.startswith("trending:") else f"Topic: {topic}"
        parts.append(f"<h2>{e(heading)}</h2>")
        for c in items:
            meta = [c["source"]]
            if c["engagement"]:
                meta.append(c["engagement"])
            if c["published_at"]:
                meta.append(c["published_at"])
            if c["cluster"]:
                meta.append(f"story: {c['cluster']}")
            parts.append("<div style='margin:0 0 18px 0'>")
            parts.append(f"<p><strong><a href='{e(c['link'])}'>{e(c['title'])}</a></strong></p>")
            if c["description"]:
                parts.append(f"<p>{e(c['description'])}</p>")
            parts.append(f"<p><em>{e(' · '.join(meta))}</em></p>")
            for extra in c.get("extra_links", []):
                parts.append(f"<p>Related coverage: <a href='{e(extra)}'>{e(humanize_url(extra))}</a></p>")
            parts.append("</div>")
    if failed:
        parts.append(f"<p><small>Sources unavailable this run: {e(', '.join(failed))}.</small></p>")
    parts.append("<p><small>Generated automatically. <a href='https://example.invalid/unsubscribe'>Unsubscribe</a></small></p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def render_candidates(cands: list[dict[str, Any]], source_status: dict[str, str], topics: list[str], issue_date: str) -> str:
    payload = {
        "generatedFor": issue_date,
        "generator": "last30days",
        "topics": topics,
        "sourceStatus": source_status,
        "candidates": [
            {k: v for k, v in c.items() if k not in ("engagement_raw", "source_key")} for c in cands
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def render_issue(cands: list[dict[str, Any]], source_status: dict[str, str], topics: list[str], issue_date: str) -> str:
    sections: dict[str, list[dict[str, Any]]] = {"useful": [], "maybeUseful": [], "discarded": []}
    summary_lines: list[str] = []
    for c in cands:
        reason_bits = []
        if c["kind"] == "discovery":
            bucket = "useful"
            reason_bits.append(f"Trending topic in the AI domain. Engagement: {c['engagement']}." if c["engagement"] else "Trending topic in the AI domain.")
            summary_lines.append(f"**{c['title']}** - {c['description'].split(' Top comment:')[0]}")
        else:
            bucket = bucket_for(c["relevance"], c["engagement_raw"], c["source_key"])
            reason_bits.append(f"Relevance {c['relevance']:.2f} for '{c['topic']}' on {c['source']}.")
            if c["engagement"]:
                reason_bits.append(f"Community engagement: {c['engagement']}.")
            if c["cluster"]:
                reason_bits.append(f"Part of the '{c['cluster']}' story cluster.")
            if c["published_at"]:
                reason_bits.append(f"Published {c['published_at']}.")
        sections[bucket].append(
            {
                "id": slugify(c["title"])[:120],
                "title": c["title"],
                "description": c["description"] or "No description provided.",
                "reason": " ".join(reason_bits),
                "tags": ["ai", slugify(c["topic"]), c["source_key"]],
                "url": c["link"],
                "source": f"last30days / {c['source']}",
            }
        )
    ok_sources = sorted(SOURCE_LABEL.get(s, s) for s, st in source_status.items() if st == "ok")
    failed = sorted(f"{s} ({st})" for s, st in source_status.items() if st not in ("ok", "no-results"))
    summary = (
        f"Community-sourced edition generated by last30days from {', '.join(ok_sources) or 'no live sources'} "
        f"over the last 30 days, for the topics: {', '.join(topics)}. "
        "Links are ranked by what people actually engaged with (upvotes, points, stars), not by editorial pick."
    )
    if summary_lines:
        summary += "\n\n" + "\n\n".join(summary_lines)
    if failed:
        summary += f"\n\n_Sources unavailable this run: {', '.join(failed)}._"
    issue = {
        "slug": f"{issue_date}-last30days",
        "title": "Transformation Tech Weekly - Community Signal (last30days PoC)",
        "date": issue_date,
        "emailsProcessed": 0,
        "generator": {"name": "last30days", "topics": topics, "sourceStatus": source_status},
        "summary": summary,
        "sections": sections,
    }
    return json.dumps(issue, indent=2, ensure_ascii=False) + "\n"


RENDERERS = {"email-html": render_email_html, "candidates": render_candidates, "issue": render_issue}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", type=Path, help="last30days --emit=json files")
    parser.add_argument("--format", choices=sorted(RENDERERS), default="email-html")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    docs = []
    for path in args.inputs:
        with path.open() as fh:
            docs.append(json.load(fh))

    cands, source_status, topics = collect(docs)
    rendered = RENDERERS[args.format](cands, source_status, topics, args.date)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.out} ({args.format}): {len(cands)} candidates from {len(docs)} inputs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
