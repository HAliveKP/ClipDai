"""Unit tests for clipdai.emphasis (rule-based keywords, no LLM, no network)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipdai.emphasis import plan, select_keywords_rule


def words(triples):
    return [[float(a), float(b), t] for a, b, t in triples]


def test_rule_based_keywords():
    ws = words([(0.0, 0.4, "WOW"), (0.5, 0.9, "the"), (1.0, 1.4, "money"),
                (2.0, 2.4, "secret"), (3.0, 3.4, "100"), (4.0, 4.4, "hello")])
    got = select_keywords_rule(ws, [])
    assert got == ["100", "money", "secret", "wow"], got


def test_rule_based_all_caps_and_custom():
    ws = words([(0.0, 0.4, "BREAKING"), (1.0, 1.4, "news"), (2.0, 2.4, "omg")])
    assert select_keywords_rule(ws, ["news"]) == ["breaking", "news", "omg"]


def test_plan_uses_explicit_keywords():
    ws = words([(0.0, 0.4, "unrelated")])
    style = {"highlight_keywords": True, "keywords": ["explicit"], "emoji": True}
    kw, em = plan(ws, style)
    assert kw == {"explicit"} and em == {}, (kw, em)


def test_plan_emoji_one_per_keyword_dict():
    ws = words([(0.0, 0.4, "wow"), (1.0, 1.4, "money"), (2.0, 2.4, "wow")])
    style = {"highlight_keywords": True, "keywords": [], "emoji": True}
    kw, em = plan(ws, style)
    assert "wow" in kw and "money" in kw
    assert em.get("wow") == "😮" and em.get("money") == "💰", em


def test_plan_disabled():
    ws = words([(0.0, 0.4, "wow")])
    assert plan(ws, {"highlight_keywords": False}) == ({}, {})


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all emphasis tests passed")
