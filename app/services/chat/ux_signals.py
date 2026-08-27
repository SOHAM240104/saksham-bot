"""Small UX helpers for mid-flow clarifies (bank name, spam noise)."""

from __future__ import annotations

import re

_BANK_ONLY = frozenset(
    {
        "sbi",
        "hdfc",
        "icici",
        "axis",
        "kotak",
        "pnb",
        "boi",
        "canara",
        "union bank",
        "bank of baroda",
        "bob",
        "yes bank",
        "idbi",
        "indian bank",
    }
)

_NOISE_EXACT = frozenset(
    {
        "lol",
        "loll",
        "haha",
        "hahaha",
        "hehe",
        "ok?",
        "what",
        "whattt",
        "wtf",
        "idk",
        "hmm",
        "hm",
        "huh",
        "??",
        "???",
        "????",
        "?????",
        "...",
        "..",
        ".",
        "?",
        "!",
        "!!!",
        "asdf",
        "asdfgh",
        "test",
        "abc",
    }
)


def is_bank_name_only(text: str) -> bool:
    t = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t or len(t) > 40:
        return False
    if t in _BANK_ONLY:
        return True
    # "sbi bank" / "hdfc bank"
    parts = t.split()
    if len(parts) <= 3 and any(b in t for b in _BANK_ONLY):
        return True
    return False


def is_low_signal_noise(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    t = re.sub(r"\s+", " ", raw.lower()).rstrip(".!? ")
    if t in _NOISE_EXACT:
        return True
    # emoji / punctuation only
    letters = re.sub(r"[^a-z0-9]", "", t)
    if len(letters) <= 2 and len(raw) <= 24:
        return True
    if len(t) <= 12 and set(t) <= set("?!.…~*"):
        return True
    if re.fullmatch(r"(lol|what|haha|hehe|ok)+t*", t):
        return True
    # "lol whattt 😂" style — only slang + emoji/punct
    slang = re.sub(r"[^a-z\s]", " ", t)
    slang = re.sub(r"\s+", " ", slang).strip()
    if slang and len(slang) <= 24:
        words = slang.split()
        if words and all(
            w in {"lol", "loll", "haha", "hehe", "what", "whattt", "wtf", "ok", "hmm", "idk"}
            or re.fullmatch(r"what+t*", w)
            or re.fullmatch(r"lo+l+", w)
            for w in words
        ):
            return True
    return False


def count_trailing_noise_turns(history: list[dict], current_message: str) -> int:
    """How many consecutive low-signal user turns end the history (including current)."""
    count = 0
    if is_low_signal_noise(current_message):
        count = 1
    else:
        return 0
    for item in reversed(history or []):
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        if content == (current_message or "").strip() and count == 1:
            continue
        if is_low_signal_noise(content):
            count += 1
        else:
            break
    return count
