"""Keyword highlights and auto-emoji for captions (feature 6).

Rule-based keyword selection (curated keywords, ALL CAPS, numbers, strong
words) plus an optional LLM-driven mode reusing the project's free LLM
integration. Auto-emoji maps chosen keywords to emoji from a curated
dictionary (at most one emoji per caption line). Rendering is handled in
layout.build_ass: keyword words are painted in the brand highlight color and
an emoji glyph is appended after them. Emoji render as text glyphs; drop
Twemoji/Noto Emoji PNGs into data/emoji/<codepoint>.png for image-based
rendering (documented in README).
"""
from __future__ import annotations

import re

from . import llm

KEYWORDS = {
    "wow", "omg", "no way", "oh my god", "insane", "crazy", "clutch",
    "unbelievable", "shocking", "secret", "the truth", "nobody", "never",
    "always", "best", "worst", "biggest", "first", "last", "only",
    "breaking", "exclusive", "revealed", "admit", "confirms", "finally",
    "money", "free", "win", "lost", "fail", "epic", "legend", "god",
    "king", "queen", "god tier", "mind blown",
}
STRONG = {
    "love", "hate", "kill", "die", "dead", "broke", "rich", "poor",
    "angry", "terrified", "thrilled", "disgusting", "amazing", "terrible",
}
EMOJI_MAP = {
    "wow": "😮", "omg": "😮", "no way": "😱", "shocking": "😱",
    "insane": "🤯", "crazy": "🤪", "clutch": "🔥", "fire": "🔥",
    "love": "❤️", "hate": "😠", "angry": "😡", "cry": "😭", "sad": "😢",
    "laugh": "😂", "funny": "😂", "epic": "😎", "best": "👑", "king": "👑",
    "queen": "👸", "money": "💰", "win": "🏆", "fail": "💀", "dead": "💀",
    "100": "💯", "yes": "👍", "no": "👎", "baby": "👶",
}


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def select_keywords_rule(words: list, custom=None) -> list[str]:
    custom = {norm(c) for c in (custom or []) if norm(c)}
    found: set[str] = set()
    for _s, _e, t in words:
        w = norm(t)
        if not w:
            continue
        if w in KEYWORDS or w in custom or w in STRONG:
            found.add(w)
        elif w.isupper() and len(w) > 1:
            found.add(w)
        elif re.fullmatch(r"\d[\d.,%]*", w):
            found.add(w)
    return sorted(found)


LLM_SYSTEM = ("You pick keywords worth highlighting in short-form video captions. "
              "Reply with JSON only.")
LLM_USER = ("Transcript (start - end text):\n{transcript}\n"
            "Custom keywords to always consider: {custom}\n"
            "Return up to 12 keywords worth highlighting (short, lowercased, "
            "no punctuation): {\"keywords\":[\"wow\",\"money\",\"secret\"]}")


def select_keywords_llm(s, words: list, custom=None) -> list[str]:
    transcript = "\n".join(f"[{float(s):.1f}-{float(e):.1f}] {t}" for s, e, t in words[:400])
    data = llm.ask_json(
        s, LLM_SYSTEM,
        LLM_USER.format(transcript=transcript, custom=", ".join(custom or [])))
    out = []
    for k in data.get("keywords", []):
        w = norm(k)
        if w and w not in out:
            out.append(w)
    return out[:40]


def plan(words: list, style: dict, llm_fn=None):
    """(keywords, emoji_map) for the captions style.

    style keys: highlight_keywords, keywords, keyword_mode ("rule"|"llm"),
    custom_keywords, emoji (bool).
    """
    if not style.get("highlight_keywords"):
        return {}, {}
    custom = [str(c) for c in (style.get("custom_keywords") or []) if str(c).strip()]
    mode = style.get("keyword_mode", "rule")
    keywords = list(style.get("keywords") or [])
    if not keywords:
        if mode == "llm" and llm_fn is not None:
            keywords = llm_fn(words, custom)
        else:
            keywords = select_keywords_rule(words, custom)
    kw = {norm(k) for k in keywords if norm(k)}
    emoji_map = {}
    if style.get("emoji"):
        for k in keywords:
            w = norm(k)
            if w in kw and w in EMOJI_MAP and w not in emoji_map:
                emoji_map[w] = EMOJI_MAP[w]
    return kw, emoji_map
