"""Market News Research Lead's five analysts.

This desk is where a rule implementation gives up the most. A lexicon over
headlines catches direction but not nuance, and none of these bots can read an
article. Each says so in its findings rather than presenting a keyword count
as comprehension - and each is a prime candidate for an LLM `judge` body later,
where the difference would be measurable against these rules.

The Stock Movement Analyst is the exception: correlating filing dates against
next-session price gaps is arithmetic, and it is as good here as it would ever
be with a model.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from src.agents import lexicon
from src.agents.base import Analyst
from src.agents.schemas import Evidence, Verdict

DESK = "news"


def _items(ctx: Any) -> list[dict[str, Any]]:
    return list(ctx.get("news") or [])


def _announcement_frame(ctx: Any) -> pd.DataFrame | None:
    frame = ctx.get("announcements")
    return frame if isinstance(frame, pd.DataFrame) and not frame.empty else None


def _announcement_text(row: pd.Series) -> str:
    for column in ("desc", "subject", "attchmntText", "smIndustry"):
        value = row.get(column)
        if value and str(value).lower() not in ("nan", "none"):
            return str(value)
    return ""


def _announcement_date(row: pd.Series) -> datetime | None:
    for column in ("an_dt", "sort_date", "exchdisstime", "date"):
        value = row.get(column)
        if value and str(value).lower() not in ("nan", "none", "nat"):
            parsed = pd.to_datetime(str(value), errors="coerce", dayfirst=True)
            if pd.notna(parsed):
                return parsed.to_pydatetime()
    return None


class ResearchAnalyst(Analyst):
    """How much is being said, how recently, and what kind of thing it is."""

    bot_id = "research_analyst"
    desk = DESK
    name = "Research Analyst"
    requires = ("news", "announcements")

    def gather(self, ctx: Any) -> dict[str, Any]:
        items = _items(ctx)
        frame = _announcement_frame(ctx)

        events: dict[str, int] = {}
        material = 0
        routine = 0

        if frame is not None:
            for _, row in frame.iterrows():
                name, spec = lexicon.classify_event(_announcement_text(row))
                events[name] = events.get(name, 0) + 1
                if spec["materiality"] >= 0.6:
                    material += 1
                elif spec["materiality"] <= 0.2:
                    routine += 1

        recent = 0
        cutoff = datetime.now() - timedelta(days=7)
        for item in items:
            published = item.get("published")
            if published and published >= cutoff:
                recent += 1

        return {
            "headline_count": len(items),
            "announcement_count": 0 if frame is None else len(frame),
            "material_filings": material,
            "routine_filings": routine,
            "recent_headlines_7d": recent,
            "event_mix": events,
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        material = metrics["material_filings"]
        routine = metrics["routine_filings"]
        headlines = metrics["headline_count"]

        findings = [
            f"{metrics['announcement_count']} exchange filings in 90 days, "
            f"{material} of them material and {routine} routine compliance notices.",
            f"{headlines} headlines found, {metrics['recent_headlines_7d']} in the last week.",
        ]

        # Coverage is not a verdict on the company. A busy filing calendar
        # means there is something to read, not that it is good news - so the
        # score stays close to neutral and the other four bots do the judging.
        score = 0.0
        if material == 0 and headlines <= 2:
            score = -0.5
            findings.append(
                "Almost nothing published. Thin coverage makes every other news "
                "reading less reliable, rather than implying calm."
            )
        elif material >= 5:
            score = 0.5
            findings.append("An unusually busy period - worth reading the filings directly.")

        top = sorted(metrics["event_mix"].items(), key=lambda kv: -kv[1])[:4]
        if top:
            findings.append("Filing mix: " + ", ".join(f"{k} x{v}" for k, v in top if k != "unknown"))

        confidence = 0.5 if (headlines or metrics["announcement_count"]) else 0.1

        return self.verdict(
            score, confidence, findings,
            [
                Evidence("announcement_count", metrics["announcement_count"]),
                Evidence("material_filings", material),
                Evidence("headline_count", headlines),
            ],
        )


class SentimentAnalyst(Analyst):
    """Tone of the headlines, by weighted finance lexicon."""

    bot_id = "sentiment_analyst"
    desk = DESK
    name = "Sentiment Analyst"
    requires = ("news",)

    def gather(self, ctx: Any) -> dict[str, Any]:
        items = _items(ctx)
        if not items:
            return {"data_available": False, "note": "no headlines to score"}

        scored = []
        for item in items:
            score, terms = lexicon.sentiment_score(f"{item.get('title','')} {item.get('summary','')}")
            if terms:
                scored.append({
                    "title": item.get("title", "")[:90],
                    "score": score,
                    "terms": terms,
                    "source": item.get("source", ""),
                })

        if not scored:
            return {
                "data_available": False,
                "note": f"none of the {len(items)} headlines contained scoreable terms",
            }

        values = [s["score"] for s in scored]
        return {
            "scored_count": len(scored),
            "total_count": len(items),
            "mean": sum(values) / len(values),
            "positive": sum(1 for v in values if v > 0),
            "negative": sum(1 for v in values if v < 0),
            "most_positive": max(scored, key=lambda s: s["score"]),
            "most_negative": min(scored, key=lambda s: s["score"]),
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        mean = metrics["mean"]
        scored = metrics["scored_count"]

        findings = [
            f"{metrics['positive']} positive and {metrics['negative']} negative headlines "
            f"of {scored} scoreable, averaging {mean:+.2f}.",
            f"Most positive: \"{metrics['most_positive']['title']}\"",
            f"Most negative: \"{metrics['most_negative']['title']}\"",
            "Scored by keyword weighting, not comprehension - it reads direction "
            "reliably and nuance not at all, and cannot tell a beaten-down miss "
            "from an outright collapse.",
        ]

        # Lexicon means cluster near zero, so amplify - but cap well short of
        # the extremes, because no headline count justifies maximum conviction.
        score = max(-3.0, min(3.0, mean * 1.5))

        # Confidence grows with sample size and falls when the desk is split.
        agreement = abs(metrics["positive"] - metrics["negative"]) / max(scored, 1)
        confidence = min(0.6, 0.15 + scored * 0.04) * (0.5 + agreement * 0.5)

        return self.verdict(
            score, confidence, findings,
            [
                Evidence("mean_sentiment", round(mean, 2)),
                Evidence("scored_headlines", f"{scored} of {metrics['total_count']}"),
            ],
        )


class StockMovementAnalyst(Analyst):
    """What the market actually did on the days news landed.

    The only bot here that loses nothing to a rule implementation: it compares
    filing dates against next-session returns, which is arithmetic. It also
    answers a question the others cannot - whether the market agreed with the
    news, or ignored it.
    """

    bot_id = "stock_movement_analyst"
    desk = DESK
    name = "Stock Movement Analyst"
    requires = ("price_frame", "announcements")

    def gather(self, ctx: Any) -> dict[str, Any]:
        frame = ctx.price_frame
        announcements = _announcement_frame(ctx)

        if frame is None or frame.empty or announcements is None:
            return {"data_available": False, "note": "needs both prices and filings"}

        returns = frame["close"].pct_change() * 100.0
        reactions = []

        for _, row in announcements.iterrows():
            when = _announcement_date(row)
            if when is None:
                continue

            stamp = pd.Timestamp(when.date())
            future = returns.index[returns.index >= stamp]
            if future.empty:
                continue

            move = returns.loc[future[0]]
            if pd.isna(move):
                continue

            name, spec = lexicon.classify_event(_announcement_text(row))
            reactions.append({
                "date": stamp.date().isoformat(),
                "event": name,
                "materiality": spec["materiality"],
                "move_pct": float(move),
            })

        if not reactions:
            return {"data_available": False, "note": "no filings could be matched to a session"}

        material = [r for r in reactions if r["materiality"] >= 0.6]
        moves = [r["move_pct"] for r in material] or [r["move_pct"] for r in reactions]

        return {
            "reaction_count": len(reactions),
            "material_count": len(material),
            "mean_reaction_pct": sum(moves) / len(moves),
            "positive_reactions": sum(1 for m in moves if m > 0),
            "negative_reactions": sum(1 for m in moves if m < 0),
            "largest": max(reactions, key=lambda r: abs(r["move_pct"])),
            "recent": sorted(reactions, key=lambda r: r["date"], reverse=True)[:5],
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        mean = metrics["mean_reaction_pct"]
        largest = metrics["largest"]

        findings = [
            f"Across {metrics['reaction_count']} filings ({metrics['material_count']} material), "
            f"the next session averaged {mean:+.2f}%.",
            f"{metrics['positive_reactions']} were met with a rise and "
            f"{metrics['negative_reactions']} with a fall.",
            f"Largest reaction: {largest['move_pct']:+.1f}% on {largest['date']} "
            f"({largest['event'].replace('_', ' ')}).",
        ]

        if abs(mean) < 0.3:
            findings.append(
                "The market has largely shrugged at this company's filings, which "
                "cuts both ways: less headline risk, and less catalyst."
            )

        score = max(-3.5, min(3.5, mean * 1.2))
        confidence = min(0.7, 0.2 + metrics["material_count"] * 0.08)

        return self.verdict(
            score, confidence, findings,
            [
                Evidence("mean_next_session_pct", round(mean, 2)),
                Evidence("material_filings_matched", metrics["material_count"]),
            ],
        )


class NewsCredibilityAnalyst(Analyst):
    """Who is reporting this, and does anyone else corroborate it."""

    bot_id = "news_credibility_analyst"
    desk = DESK
    name = "News Credibility Analyst"
    requires = ("news",)

    def gather(self, ctx: Any) -> dict[str, Any]:
        items = _items(ctx)
        if not items:
            return {"data_available": False, "note": "no headlines to assess"}

        tiers: dict[int, int] = {}
        sources: set[str] = set()
        undated = 0

        for item in items:
            tier = lexicon.source_tier(item.get("source"))
            tiers[tier] = tiers.get(tier, 0) + 1
            if item.get("source"):
                sources.add(str(item["source"]).lower())
            if not item.get("published"):
                undated += 1

        # Corroboration: distinct sources carrying a materially similar story.
        # Crude shingling on the first few significant words - it catches
        # syndicated coverage of one event, not paraphrase.
        clusters: dict[str, set[str]] = {}
        for item in items:
            words = [w for w in str(item.get("title", "")).lower().split() if len(w) > 4]
            if len(words) < 2:
                continue
            key = " ".join(sorted(words[:3]))
            clusters.setdefault(key, set()).add(str(item.get("source", "")).lower())

        corroborated = sum(1 for sources_for in clusters.values() if len(sources_for) > 1)

        weighted = sum(lexicon.TIER_WEIGHT.get(t, 0.35) * n for t, n in tiers.items())
        quality = weighted / len(items)

        return {
            "item_count": len(items),
            "distinct_sources": len(sources),
            "tier_counts": tiers,
            "quality": quality,
            "corroborated_stories": corroborated,
            "undated": undated,
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        quality = metrics["quality"]
        tiers = metrics["tier_counts"]

        breakdown = ", ".join(
            f"{n} {lexicon.TIER_LABEL[t]}" for t, n in sorted(tiers.items())
        )
        findings = [
            f"{metrics['item_count']} items from {metrics['distinct_sources']} distinct sources: {breakdown}.",
            f"{metrics['corroborated_stories']} stories carried by more than one source.",
        ]

        if metrics["undated"]:
            findings.append(
                f"{metrics['undated']} items carry no timestamp, so their recency is unverified."
            )

        if tiers.get(1):
            findings.append(
                f"{tiers[1]} came from the exchange or a regulator - those are facts, "
                f"not reports of facts, and outrank everything else here."
            )

        # This bot rates the evidence, not the company. Its score stays small
        # and its real contribution is telling the desk how much to trust the
        # other news bots.
        score = (quality - 0.6) * 4.0
        confidence = min(0.5, 0.15 + metrics["item_count"] * 0.03)

        red_flags = []
        if quality < 0.45 and metrics["item_count"] >= 3:
            red_flags.append(
                "Coverage is dominated by unranked sources; treat the sentiment reading as weak."
            )

        return self.verdict(
            max(-2.0, min(2.0, score)), confidence, findings,
            [
                Evidence("source_quality", round(quality, 2), "1.0 is all exchange filings"),
                Evidence("distinct_sources", metrics["distinct_sources"]),
                Evidence("corroborated_stories", metrics["corroborated_stories"]),
            ],
            red_flags,
        )


class MarketImpactAnalyst(Analyst):
    """One-off noise, or something that changes the business."""

    bot_id = "market_impact_analyst"
    desk = DESK
    name = "Market Impact Analyst"
    requires = ("announcements", "news")

    def gather(self, ctx: Any) -> dict[str, Any]:
        frame = _announcement_frame(ctx)
        items = _items(ctx)

        texts: list[str] = []
        if frame is not None:
            texts.extend(_announcement_text(row) for _, row in frame.iterrows())
        texts.extend(str(i.get("title", "")) for i in items)
        texts = [t for t in texts if t]

        if not texts:
            return {"data_available": False, "note": "nothing to classify"}

        structural: list[dict[str, Any]] = []
        noise = 0
        impact = 0.0

        for text in texts:
            name, spec = lexicon.classify_event(text)
            materiality = float(spec["materiality"])
            direction = int(spec["direction"])

            if materiality >= 0.7 and name not in ("unknown", "routine"):
                structural.append({
                    "event": name,
                    "direction": direction,
                    "materiality": materiality,
                    "text": text[:110],
                    "note": spec.get("note"),
                })
                impact += direction * materiality
            elif materiality <= 0.2:
                noise += 1

        return {
            "classified": len(texts),
            "structural_events": structural,
            "routine_count": noise,
            "impact": impact,
            "unclassified": sum(
                1 for t in texts if lexicon.classify_event(t)[0] == "unknown"
            ),
        }

    def judge(self, metrics: dict[str, Any]) -> Verdict:
        structural = metrics["structural_events"]
        impact = metrics["impact"]

        findings = [
            f"{len(structural)} structurally material events among {metrics['classified']} items; "
            f"{metrics['routine_count']} were routine compliance notices.",
        ]

        for event in sorted(structural, key=lambda e: -e["materiality"])[:4]:
            arrow = "positive" if event["direction"] > 0 else ("negative" if event["direction"] < 0 else "neutral")
            line = f"{event['event'].replace('_', ' ')} ({arrow}): {event['text']}"
            if event.get("note"):
                line += f" - {event['note']}"
            findings.append(line)

        if metrics["unclassified"] > metrics["classified"] * 0.6:
            findings.append(
                f"{metrics['unclassified']} of {metrics['classified']} items matched no known "
                f"event type - much of this coverage is general market commentary rather "
                f"than company news."
            )

        red_flags = [
            f"{e['event'].replace('_', ' ')}: {e['text']}"
            for e in structural
            if e["direction"] < 0 and e["materiality"] >= 0.9
        ]

        score = max(-4.0, min(4.0, impact * 1.5))
        confidence = min(0.65, 0.2 + len(structural) * 0.12) if structural else 0.2

        return self.verdict(score, confidence, findings,
                            [Evidence("structural_events", len(structural)),
                             Evidence("net_impact", round(impact, 2))],
                            red_flags)


ANALYSTS = (
    ResearchAnalyst,
    SentimentAnalyst,
    StockMovementAnalyst,
    NewsCredibilityAnalyst,
    MarketImpactAnalyst,
)
