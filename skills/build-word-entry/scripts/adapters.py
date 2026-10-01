"""Site-specific selectors. Returns small evidence, never a full page snapshot."""
from __future__ import annotations

import time
import re
from urllib.parse import quote, urljoin
from urllib.parse import urlparse

from utils.scripts.browser_checkpoint import inspect_page_for_user_action
from utils.scripts.timestamp import iso_timestamp


SITES = {
    "cambridge": "https://dictionary.cambridge.org/dictionary/english-chinese-simplified/{word}",
    "oxford": "https://www.oxfordlearnersdictionaries.com/definition/english/{word}",
    "longman": "https://www.ldoceonline.com/dictionary/{word}",
    "thesaurus": "https://www.thesaurus.com/browse/{word}",
}
FRAGMENT_LIMIT = 600
SENSE_LIMIT = 200
ENTRY_LIMIT = 100
EXTERNAL_LIMIT = 500

SELECTORS = {
    "cambridge": (".entry-body__el", ".def-block", ".def", ".trans", ".examp"),
    "oxford": (".entry", ".sense", ".def", ".examples", ".phon"),
    "longman": (".dictentry", ".Sense", ".DEF", ".EXAMPLE", ".PRON"),
    "thesaurus": ("main", "[data-testid='word-grid-container']", "a[href*='/browse/']"),
}

DERIVATIVE_SELECTORS = {
    "cambridge": (".word-family a", ".wordfamily a", ".related-words a"),
    "oxford": (".wordfamily a", ".word-family a"),
    "longman": (".wordfams a.crossRef",),
}


def regular_adjective_forms(word: str) -> dict[str, str]:
    """Return spellings to look for in source examples, never unsupported output."""
    from utils.scripts.english_inflections import regular_forms
    return regular_forms(word, "adjective")


def collect_site(page, site: str, word: str) -> dict:
    url = SITES[site].format(word=quote(word, safe=""))
    time.sleep(1)
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            break
        except Exception:
            if attempt:
                raise
            time.sleep(2)
    expected_host = urlparse(url).hostname
    actual_host = urlparse(page.url).hostname
    if actual_host != expected_host:
        raise RuntimeError(f"站点跳转到非预期域名：{page.url}")
    inspect_page_for_user_action(page)
    selectors = SELECTORS[site]
    fragments = []
    sense_candidates = []
    pronunciation_candidates = []
    inflection_candidates = []
    derivative_candidates = []
    external_candidates = []
    relation_groups = []
    limits_exceeded = []
    preview_limits = []
    for selector in selectors[1:]:
        try:
            nodes = page.locator(selector).all()
            if len(nodes) > 12:
                preview_limits.append({"selector": selector, "matched": len(nodes), "retained": 12})
            for index, item in enumerate(nodes[:12]):
                value = " ".join(item.inner_text(timeout=3000).split())[:240]
                if value:
                    fragments.append({"locator": f"{selector}:nth({index})", "summary": value})
        except Exception:
            continue
    block_selector = {"cambridge": ".def-block", "oxford": ".sense", "longman": ".Sense"}.get(site)
    if block_selector:
        child_selectors = {"cambridge": (".def", ".trans", ".examp"),
                           "oxford": (".def", None, ".x"),
                           "longman": (".DEF", None, ".EXAMPLE")}[site]
        blocks = page.eval_on_selector_all(block_selector, """(nodes, selectors) => nodes.map((node, index) => {
            const exampleNode = node.querySelector(selectors[2]);
            return {
                index,
                fields: selectors.map(selector => selector ? (node.querySelector(selector)?.innerText || '') : ''),
                exampleTranslationZh: exampleNode?.querySelector('.trans,[lang^="zh"]')?.innerText || '',
                partOfSpeech: node.closest('.entry-body__el,.dictentry,.entry')?.querySelector('.posgram,.pos,.POS')?.innerText || ''
            };
        })""", child_selectors)
        if len(blocks) > SENSE_LIMIT:
            limits_exceeded.append(block_selector)
        for block in blocks[:SENSE_LIMIT]:
            index = block["index"]
            fields = [" ".join(value.split())[:240] for value in block["fields"]]
            if not fields[0]:
                continue
            full_example = fields[2]
            translation = " ".join(block.get("exampleTranslationZh", "").split())[:240]
            if site == "cambridge" and fields[2]:
                match = re.search(r"[\u3400-\u9fff]", fields[2])
                if match:
                    if not translation:
                        translation = fields[2][match.start():].strip()
                    fields[2] = fields[2][:match.start()].strip()
            part_of_speech = " ".join(block["partOfSpeech"].split())[:80]
            fragment_index = len(fragments)
            fragments.append({"locator": f"{block_selector}:nth({index})", "summary": " / ".join(value for value in fields if value)[:240]})
            example_fragment = None
            if full_example:
                example_fragment = len(fragments)
                fragments.append({"locator": f"{block_selector}:nth({index}) {child_selectors[2]}", "summary": full_example})
            sense_candidates.append({"partOfSpeech": part_of_speech, "definitionEn": fields[0],
                                     "definitionZh": fields[1], "example": fields[2],
                                     "exampleTranslationZh": translation, "exampleFragment": example_fragment,
                                     "fragment": fragment_index})
    if site == "cambridge":
        entries = page.eval_on_selector_all(".entry-body__el", """nodes => nodes.map((entry, index) => ({
            index,
            pos: entry.querySelector('.posgram')?.innerText || '',
            pronunciations: ['uk','us'].map(variety => ({
                variety,
                ipa: entry.querySelector('.' + variety + ' .ipa')?.innerText || '',
                audio: entry.querySelector('.' + variety + " source[type='audio/mpeg']")?.getAttribute('src') || ''
            })),
            inflections: Array.from(entry.querySelectorAll('.irreg-infls')).map((node, inflectionIndex) => ({
                inflectionIndex, text: node.innerText || ''
            }))
        }))""")
        if len(entries) > ENTRY_LIMIT:
            limits_exceeded.append(".entry-body__el")
        for entry in entries[:ENTRY_LIMIT]:
            entry_index = entry["index"]
            pos = " ".join(entry["pos"].split())[:80]
            for pron in entry["pronunciations"]:
                variety = pron["variety"]
                ipa = " ".join(pron["ipa"].split())
                if not ipa:
                    continue
                audio = pron["audio"]
                fragment_index = len(fragments)
                fragments.append({"locator": f".entry-body__el:nth({entry_index}) .{variety} .ipa", "summary": ipa})
                pronunciation_candidates.append({"partOfSpeech": pos, "variety": variety, "ipa": ipa,
                                                 "audioUrl": urljoin(page.url, audio) if audio else None,
                                                 "fragment": fragment_index})
            for inflection in entry["inflections"][:10]:
                inflection_index = inflection["inflectionIndex"]
                raw_forms = " ".join(inflection["text"].split())
                for piece in raw_forms.split("|"):
                    part = piece.strip()
                    match = re.match(r"^(present participle|past participle|past tense|third person singular|plural|comparative|superlative)\s+(.+)$", part, re.I)
                    if not match:
                        continue
                    kind, form = match.group(1).lower(), match.group(2).strip()
                    fragment_index = len(fragments)
                    fragments.append({"locator": f".entry-body__el:nth({entry_index}) .irreg-infls:nth({inflection_index})", "summary": part[:160]})
                    inflection_candidates.append({"partOfSpeech": pos, "kind": kind, "form": form,
                                                  "fragment": fragment_index})
    if site in ("cambridge", "oxford", "longman"):
        from utils.scripts.english_inflections import observed_regular_forms
        seen = {(value["kind"], value["form"].casefold()) for value in inflection_candidates}
        for candidate in observed_regular_forms({"senseCandidates": sense_candidates,
                                                  "fragments": fragments}, word):
            key = (candidate["kind"], candidate["form"].casefold())
            if key not in seen:
                inflection_candidates.append(candidate)
                seen.add(key)
    for selector in DERIVATIVE_SELECTORS.get(site, ()):
        try:
            links = page.eval_on_selector_all(selector, "nodes => nodes.map((node, index) => ({index, text: node.innerText || '', href: node.href || '', opposite: !!node.closest('.opp')}))")
        except Exception:
            continue
        if len(links) > SENSE_LIMIT:
            limits_exceeded.append(selector)
        for link in links[:SENSE_LIMIT]:
            related = " ".join(link["text"].split()).casefold()
            if link["opposite"] or not re.fullmatch(r"[a-z][a-z'-]*", related) or related == word:
                continue
            if urlparse(link["href"]).hostname != expected_host:
                continue
            fragment_index = len(fragments)
            fragments.append({"locator": f"{selector}:nth({link['index']})", "summary": f"word family: {related}"})
            derivative_candidates.append({"word": related, "relationHint": "word_family",
                                          "fragment": fragment_index})
    if site == "thesaurus":
        blocks = page.eval_on_selector_all(".definition-block", """nodes => nodes.map((node, index) => ({
            index, partOfSpeech: node.querySelector('.part-of-speech-label')?.innerText || '',
            gloss: node.querySelector('.definition-header .definition')?.innerText || '',
            panels: Array.from(node.querySelectorAll('.synonym-antonym-panel')).map((panel, panelIndex) => ({
                panelIndex, label: panel.querySelector('.synonym-antonym-panel-label')?.innerText || '',
                links: Array.from(panel.querySelectorAll('a.synonym-antonym-word-chip')).map((link, linkIndex) => ({
                    linkIndex, text: link.innerText || '', href: link.href || '', className: link.className || ''
                }))
            }))
        }))""")
        if len(blocks) > SENSE_LIMIT:
            limits_exceeded.append(".definition-block")
        for block in blocks[:SENSE_LIMIT]:
            group_index = block["index"]
            gloss = " ".join(block["gloss"].split())[:160]
            pos = " ".join(block["partOfSpeech"].split())[:80].casefold()
            if not gloss or not pos:
                continue
            group_fragment = len(fragments)
            fragments.append({"locator": f".definition-block:nth({group_index}) .definition-header",
                              "summary": f"{pos}: {gloss}"[:240]})
            relation_groups.append({"groupIndex": group_index, "partOfSpeech": pos,
                                    "gloss": gloss, "fragment": group_fragment})
            for panel in block["panels"]:
                label = panel["label"].strip().casefold()
                if label not in ("synonyms", "antonyms"):
                    continue
                hint = "synonym_or_near_synonym" if label == "synonyms" else "antonym"
                for link in panel["links"]:
                    if len(external_candidates) >= EXTERNAL_LIMIT:
                        limits_exceeded.append(".synonym-antonym-word-chip")
                        break
                    href = link["href"]
                    if urlparse(href).hostname != urlparse(page.url).hostname:
                        continue
                    candidate_word = link["text"].strip().casefold()
                    if not re.fullmatch(r"[a-z][a-z'-]*", candidate_word) or candidate_word == word:
                        continue
                    fragment_index = len(fragments)
                    locator = (f".definition-block:nth({group_index}) .synonym-antonym-panel:"
                               f"nth({panel['panelIndex']}) a.synonym-antonym-word-chip:nth({link['linkIndex']})")
                    fragments.append({"locator": locator, "summary": f"{label}: {candidate_word}; {gloss}"[:240]})
                    external_candidates.append({"word": candidate_word, "fragment": fragment_index,
                                                "groupIndex": group_index, "relationHint": hint,
                                                "partOfSpeech": pos, "senseGloss": gloss})
            # A one-word gloss head may be the only explicit relation on the page.
            group_words = {candidate["word"] for candidate in external_candidates
                           if candidate["groupIndex"] == group_index}
            for head in re.split(r"[,;]", gloss.casefold()):
                related = head.strip()
                if (len(external_candidates) >= EXTERNAL_LIMIT or
                    not re.fullmatch(r"[a-z][a-z'-]*", related) or
                    related == word or related in group_words):
                    continue
                external_candidates.append({"word": related, "fragment": group_fragment,
                                            "groupIndex": group_index,
                                            "relationHint": "synonym_or_near_synonym",
                                            "partOfSpeech": pos, "senseGloss": gloss})
                group_words.add(related)
    if len(fragments) > FRAGMENT_LIMIT:
        limits_exceeded.append("fragments")
    if site == "oxford" and word == "preinstall" and not (
            sense_candidates or pronunciation_candidates or inflection_candidates or derivative_candidates):
        # Oxford's learner entry uses the hyphenated spelling for this lemma.
        return collect_site(page, site, "pre-install")
    return {
        "site": site,
        "url": page.url,
        "collectedAt": iso_timestamp(),
        "attempts": attempt + 1,
        "coverage": "extracted" if (sense_candidates or pronunciation_candidates or inflection_candidates or derivative_candidates or external_candidates) else "unavailable",
        "fragments": fragments[:FRAGMENT_LIMIT],
        "senseCandidates": [candidate for candidate in sense_candidates if candidate["fragment"] < FRAGMENT_LIMIT],
        "pronunciationCandidates": [candidate for candidate in pronunciation_candidates if candidate["fragment"] < FRAGMENT_LIMIT],
        "inflectionCandidates": [candidate for candidate in inflection_candidates if candidate["fragment"] < FRAGMENT_LIMIT],
        "derivativeCandidates": [candidate for candidate in derivative_candidates if candidate["fragment"] < FRAGMENT_LIMIT],
        "externalCandidates": [candidate for candidate in external_candidates if candidate["fragment"] < FRAGMENT_LIMIT],
        "relationGroups": [group for group in relation_groups if group["fragment"] < FRAGMENT_LIMIT],
        "truncatedSelectors": sorted(set(limits_exceeded)),
        "previewLimits": preview_limits,
    }
