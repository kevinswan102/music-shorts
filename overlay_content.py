#!/usr/bin/env python3
"""
overlay_content.py — local content pool for Shorts text overlays (2026-07-28).

Reddit's public JSON API refuses unauthenticated traffic and the RSS fallback
is throttled hard from CI IPs, so a render that reaches out live gets an empty
list most days and silently drops to the same handful of hardcoded facts. This
module inverts that: renders always read from a **local pool** on disk, and a
live fetch is only ever an opportunistic top-up of that pool. No OAuth, no
network dependency in the render path.

The pool ships seeded with two topics and serves a mix of both:
  * ``music``   — producer / artist / release tips, on-brand for the channel
  * ``general`` — LPT- and TIL-style lines that travel well on Shorts

Recently used lines are remembered in the cache so a 4x/day channel does not
repeat itself within a rotation.
"""

import hashlib
import json
import logging
import os
import random
import time
from datetime import datetime, timezone

logger = logging.getLogger("overlay_content")

CACHE_PATH = os.getenv(
    "OVERLAY_POOL_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "overlay_pool.json"),
)

# Keep the pool bounded — it is committed-adjacent state, not an archive.
MAX_POOL_ENTRIES = 400
# How many recently served lines to refuse. NUM_SHORTS is 2-4/day, so ~2 weeks.
RECENT_MEMORY = 40
# Share of picks that should be on-brand music content. The user asked for a
# mix of both rather than one or the other, weighted toward the channel's own
# subject matter.
MUSIC_SHARE = float(os.getenv("OVERLAY_MUSIC_SHARE", "0.5"))

# Music/artist subreddits worth topping the pool up from when a fetch succeeds.
MUSIC_SUBREDDITS = [
    ("WeAreTheMusicMakers", ""),
    ("edmproduction", ""),
    ("musicproduction", ""),
    ("musicmarketing", ""),
]


_SEED_MUSIC = [
    ("Mix at low volume. If it works quiet, it works loud.", "PRODUCER TIP"),
    ("Finish bad songs. Unfinished ideas teach you nothing.", "PRODUCER TIP"),
    ("Reference a released track in the same room, same volume.", "PRODUCER TIP"),
    ("High-pass everything that is not bass or kick.", "PRODUCER TIP"),
    ("If the arrangement is boring, no plugin will fix it.", "PRODUCER TIP"),
    ("Name your files. Future you is not a mind reader.", "PRODUCER TIP"),
    ("Sleep on a mix. Ears lie after two hours.", "PRODUCER TIP"),
    ("Sidechain the pad, not just the bass.", "PRODUCER TIP"),
    ("Mono-check your mix before you call it done.", "PRODUCER TIP"),
    ("One great sound beats ten stacked mediocre ones.", "PRODUCER TIP"),
    ("Automate volume before you reach for a compressor.", "PRODUCER TIP"),
    ("Leave headroom. Mastering needs somewhere to go.", "PRODUCER TIP"),
    ("Loops are drafts. Songs have a second half.", "PRODUCER TIP"),
    ("Release on a schedule, not on inspiration.", "ARTIST TIP"),
    ("Post the process, not just the finished track.", "ARTIST TIP"),
    ("Your first ten uploads are practice. Upload them anyway.", "ARTIST TIP"),
    ("Consistency beats virality over a year.", "ARTIST TIP"),
    ("Pitch playlists two weeks before release, not after.", "ARTIST TIP"),
    ("A hook in the first three seconds or they scroll.", "ARTIST TIP"),
    ("Beat tags cost you nothing and get you found.", "ARTIST TIP"),
    ("Register your splits before the song does numbers.", "ARTIST TIP"),
    ("Free downloads build lists. Lists build careers.", "ARTIST TIP"),
    ("Answer every comment for your first thousand fans.", "ARTIST TIP"),
    ("The algorithm rewards finishing, not perfecting.", "ARTIST TIP"),
]

_SEED_GENERAL = [
    ("If a task takes under two minutes, do it now.", "PRO TIP"),
    ("Put your phone across the room for one focused hour.", "PRO TIP"),
    ("Write the first bad version fast, edit it slowly.", "PRO TIP"),
    ("Use one playlist for focus so your brain learns the cue.", "PRO TIP"),
    ("Wait one day before buying. Most wants expire.", "PRO TIP"),
    ("Shrink a stuck task until one move is obvious.", "PRO TIP"),
    ("Schedule the hard thing first. Willpower is a morning resource.", "PRO TIP"),
    ("Say the number out loud when you park. You will remember it.", "PRO TIP"),
    ("Two-minute walk after eating beats an hour at the gym later.", "PRO TIP"),
    ("Ask for the discount. The worst answer is the current price.", "PRO TIP"),
    ("Honey basically never expires.", "DID YOU KNOW?"),
    ("A day on Venus is longer than its year.", "DID YOU KNOW?"),
    ("Sharks are older than trees.", "DID YOU KNOW?"),
    ("Oxford University is older than the Aztec Empire.", "DID YOU KNOW?"),
    ("The average cloud weighs over a million pounds.", "DID YOU KNOW?"),
    ("Bananas are berries. Strawberries are not.", "DID YOU KNOW?"),
    ("Nintendo started as a playing card company.", "DID YOU KNOW?"),
    ("The moon drifts about 3.8cm further away each year.", "DID YOU KNOW?"),
    ("Octopuses have three hearts and blue blood.", "DID YOU KNOW?"),
    ("Wombat droppings are cube shaped.", "DID YOU KNOW?"),
    ("Vinyl outsold CDs again for the first time since 1987.", "DID YOU KNOW?"),
    ("The loudest sound ever recorded was heard 3,000 miles away.", "DID YOU KNOW?"),
    ("A group of flamingos is called a flamboyance.", "DID YOU KNOW?"),
    ("Bubble wrap was invented as wallpaper.", "DID YOU KNOW?"),
]


def _key(text: str) -> str:
    """Stable id for dedup and recent-memory, insensitive to case/spacing."""
    return hashlib.sha1(" ".join(str(text).lower().split()).encode()).hexdigest()[:16]


def _seed_entries() -> list:
    out = []
    for text, label in _SEED_MUSIC:
        out.append({"text": text, "label": label, "topic": "music", "seed": True})
    for text, label in _SEED_GENERAL:
        out.append({"text": text, "label": label, "topic": "general", "seed": True})
    for entry in out:
        entry["key"] = _key(entry["text"])
    return out


def load_pool() -> dict:
    """Read the pool, always merging the seeds so the file can never starve."""
    pool = {"entries": [], "recent": []}
    try:
        with open(CACHE_PATH) as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            pool["entries"] = [e for e in raw.get("entries", []) if isinstance(e, dict)]
            pool["recent"] = [str(k) for k in raw.get("recent", [])]
    except FileNotFoundError:
        pass
    except Exception as exc:
        # A corrupt cache must degrade to seeds, never break a render.
        logger.warning("Overlay pool unreadable (%s) — using seeds only", exc)

    have = {e.get("key") or _key(e.get("text", "")) for e in pool["entries"]}
    for entry in _seed_entries():
        if entry["key"] not in have:
            pool["entries"].append(entry)
    return pool


def save_pool(pool: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        # Seeds live in code; persisting them only bloats the file.
        entries = [e for e in pool.get("entries", []) if not e.get("seed")]
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "entries": entries[-MAX_POOL_ENTRIES:],
            "recent": [str(k) for k in pool.get("recent", [])][-RECENT_MEMORY:],
        }
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, CACHE_PATH)
    except Exception as exc:
        logger.warning("Could not save overlay pool: %s", exc)


def add_lines(texts, topic: str = "general", label: str = "") -> int:
    """Fold freshly fetched lines into the pool. Returns how many were new."""
    pool = load_pool()
    have = {e.get("key") or _key(e.get("text", "")) for e in pool["entries"]}
    added = 0
    for text in texts or []:
        text = " ".join(str(text).split())
        if len(text) < 20 or len(text) > 115:
            continue
        k = _key(text)
        if k in have:
            continue
        have.add(k)
        pool["entries"].append({
            "key": k, "text": text, "label": label,
            "topic": topic, "added": time.time(),
        })
        added += 1
    if added:
        save_pool(pool)
        logger.info("Overlay pool: +%d new %s line(s)", added, topic)
    return added


def pick(topic: str = "", rng=None) -> dict:
    """
    Return one ``{text, label, topic}`` line from the local pool.

    Never touches the network. Lines served recently are skipped until the
    remaining choices run out, at which point the memory is ignored rather
    than returning nothing.
    """
    rng = rng or random
    pool = load_pool()
    recent = set(pool.get("recent", []))

    if not topic:
        topic = "music" if rng.random() < MUSIC_SHARE else "general"

    def _by_topic(want):
        return [e for e in pool["entries"] if e.get("topic") == want and e.get("text")]

    candidates = _by_topic(topic) or _by_topic(
        "general" if topic == "music" else "music") or list(pool["entries"])
    if not candidates:
        return {}

    fresh = [e for e in candidates
             if (e.get("key") or _key(e["text"])) not in recent]
    entry = rng.choice(fresh or candidates)

    key = entry.get("key") or _key(entry["text"])
    pool["recent"] = [k for k in pool.get("recent", []) if k != key] + [key]
    save_pool(pool)
    return {
        "text": entry["text"],
        "label": entry.get("label", ""),
        "topic": entry.get("topic", topic),
    }
