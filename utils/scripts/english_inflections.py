"""Reusable spelling candidates for English inflections.

These are candidate spellings, not lexical evidence. Callers must record whether
a form was observed or only derived from an attested part of speech.
"""

from __future__ import annotations

import re

RULE_VERSION = "2"


def part_of_speech(label: str) -> str:
    match = re.match(r"\s*(adjective|adverb|noun|verb|adj|adv)\b", label, re.I)
    if not match:
        return ""
    return {"adj": "adjective", "adv": "adverb"}.get(match[1].lower(), match[1].lower())


def regular_forms(word: str, label: str) -> dict[str, str]:
    """Return plausible regular spellings; a caller must check applicability."""
    if not re.fullmatch(r"[a-z]+", word, re.I):
        return {}
    word = word.lower()
    pos = part_of_speech(label)
    consonant_y = word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou"
    if pos == "noun":
        if word.endswith("z") and not word.endswith("zz"):
            plural = word + "zes"
        elif re.search(r"(s|x|z|ch|sh)$", word):
            plural = word + "es"
        else:
            plural = word[:-1] + "ies" if consonant_y else word + "s"
        return {"plural": plural}
    if pos == "verb":
        third = regular_forms(word, "noun")["plural"]
        doubled = (len(word) >= 3 and word[-1] not in "aeiouwxy"
                   and word[-2] in "aeiou" and word[-3] not in "aeiou")
        stem = word + word[-1] if doubled else word
        past = word[:-1] + "ied" if consonant_y else word + "d" if word.endswith("e") else stem + "ed"
        progressive = word[:-2] + "ying" if word.endswith("ie") else word[:-1] + "ing" if word.endswith("e") and not word.endswith("ee") else stem + "ing"
        return {"third person singular": third, "past tense": past,
                "past participle": past, "present participle": progressive}
    if pos == "adjective":
        if consonant_y:
            stem = word[:-1] + "i"
        elif word.endswith("e"):
            stem = word
        elif (len(word) >= 3 and word[-1] not in "aeiouwxy"
              and word[-2] in "aeiou" and word[-3] not in "aeiou"):
            stem = word + word[-1]
        else:
            stem = word
        return {"comparative": stem + ("r" if word.endswith("e") else "er"),
                "superlative": stem + ("st" if word.endswith("e") else "est")}
    return {}


IRREGULAR_VERB_ROOTS = frozenset({
    "be", "begin", "bend", "bite", "blow", "break", "bring", "build", "buy", "catch",
    "choose", "come", "cut", "do", "draw", "drink", "drive", "eat", "fall", "feel",
    "find", "fly", "forget", "get", "give", "go", "grow", "have", "hear", "hold",
    "keep", "know", "leave", "lend", "let", "lie", "lose", "make", "mean", "meet",
    "pay", "put", "read", "ride", "ring", "rise", "run", "say", "see", "sell",
    "send", "set", "shake", "shine", "shoot", "show", "sing", "sit", "sleep",
    "speak", "spend", "stand", "steal", "swim", "take", "teach", "tear", "tell",
    "think", "throw", "understand", "wake", "wear", "win", "write",
})

IRREGULAR_NOUNS = frozenset({
    "aircraft", "appendix", "calf", "cactus", "child", "crisis", "datum", "deer",
    "die", "fish", "foot", "fungus", "goose", "half", "index", "knife", "leaf",
    "life", "loaf", "man", "medium", "mouse", "ox", "person", "phenomenon",
    "series", "sheep", "shelf", "species", "thesis", "thief", "tooth", "wife",
    "wolf", "woman",
})


def rule_derived_forms(word: str, label: str) -> dict[str, tuple[str, str]]:
    """Conservative source-POS-backed candidates, with stable rule IDs."""
    pos = part_of_speech(label)
    lower = word.lower()
    if pos == "noun":
        if not re.search(r"\[\s*C(?:\s+or\s+U)?\s*\]", label, re.I):
            return {}
        if (lower in IRREGULAR_NOUNS or re.search(r"(man|woman|child|foot|tooth|goose|mouse)$", lower)
                or re.search(r"(o|f|fe|is|us|um)$", lower)):
            return {}
    elif pos == "verb":
        irregular = lower in IRREGULAR_VERB_ROOTS or any(
            lower == prefix + root for prefix in ("over", "under", "re", "out", "mis", "un")
            for root in IRREGULAR_VERB_ROOTS
        )
    else:
        return {}
    result = {}
    for kind, form in regular_forms(word, label).items():
        if pos == "verb" and irregular and kind in ("past tense", "past participle"):
            continue
        if pos == "verb" and kind == "third person singular" and any(
            lower == prefix + root for prefix in ("", "over", "under", "re", "out", "mis", "un")
            for root in ("be", "have", "do", "go")
        ):
            continue
        if pos == "verb" and kind == "present participle" and lower == "be":
            continue
        # Final consonant doubling depends on stress. Infer it only for one-vowel-group verbs.
        if (pos == "verb" and kind in ("past tense", "past participle", "present participle")
                and re.search(r"[^aeiou][aeiou][^aeiouwxy]$", lower)
                and len(re.findall(r"[aeiou]+", lower)) > 1):
            continue
        if pos == "noun":
            rule = "noun_consonant_y_ies" if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou" else (
                "noun_sibilant_es" if re.search(r"(s|x|z|ch|sh)$", word) else "noun_regular_s")
        elif kind == "third person singular":
            rule = "verb_third_person_regular"
        elif kind == "present participle":
            rule = "verb_present_participle_regular"
        else:
            rule = "verb_past_regular"
        result[kind] = (form, rule)
    return result


def observed_regular_forms(source: dict, word: str) -> list[dict]:
    """Find rule candidates actually present in cited sense examples."""
    fragments = source.get("fragments", [])
    found: set[tuple[str, str, str]] = set()
    result: list[dict] = []
    senses = source.get("senseCandidates", [])
    for sense in senses:
        label = sense.get("partOfSpeech", "")
        pos = part_of_speech(label)
        index = sense.get("exampleFragment")
        if (not pos or index is None or index >= len(fragments)
                or pos == "noun" and ("[ U" in label or "[ plural" in label)):
            continue
        summary = fragments[index]["summary"]
        for kind, form in regular_forms(word, pos).items():
            key = (pos, kind, form)
            if key in found or not re.search(rf"(?<!\w){re.escape(form)}(?!\w)", summary, re.I):
                continue
            found.add(key)
            result.append({"partOfSpeech": label, "kind": kind, "form": form,
                           "fragment": index})
    labels = {part_of_speech(sense.get("partOfSpeech", "")): sense.get("partOfSpeech", "")
              for sense in senses if part_of_speech(sense.get("partOfSpeech", ""))}
    if len(labels) != 1:
        return result
    for index, fragment in enumerate(fragments):
        locator = fragment.get("locator", "").lower()
        if not any(marker in locator for marker in (".examp", ".x:nth", ".x ")):
            continue
        for pos, label in labels.items():
            if pos == "noun" and ("[ U" in label or "[ plural" in label):
                continue
            for kind, form in regular_forms(word, pos).items():
                key = (pos, kind, form)
                if key in found or not re.search(rf"(?<!\w){re.escape(form)}(?!\w)", fragment["summary"], re.I):
                    continue
                found.add(key)
                result.append({"partOfSpeech": label, "kind": kind, "form": form,
                               "fragment": index})
    return result
