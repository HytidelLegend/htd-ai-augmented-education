from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
from itertools import product
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT / "skills" / "build-word-entry" / "scripts"))
sys.path.insert(0, str(PROJECT))
import cli
from adapters import regular_adjective_forms
from entry import build_entry, item, parse_word_list, stable_word_id
from review import candidate_id
from content_review import apply as apply_content_review
from utils.scripts.dictionary_records import entries as entry_records
from utils.scripts.structured_io import read_json, write_json
from utils.scripts.dictionary_jsonl import read_all, render_entry_line, upsert
from utils.scripts.timestamp import iso_timestamp
from utils.scripts.english_inflections import observed_regular_forms, regular_forms, rule_derived_forms
from utils.scripts.word_similarity import iter_similar_pairs, meets_subsequence_threshold, subsequence_similarity

assert regular_forms("battery", "noun [ C ]")["plural"] == "batteries"
assert regular_forms("tip", "verb")["past tense"] == "tipped"
assert regular_forms("wrap", "verb")["present participle"] == "wrapping"
assert regular_forms("inactive", "adjective")["comparative"] == "inactiver"
assert regular_forms("handily", "adverb") == {}
assert rule_derived_forms("paycheck", "noun [ C ]")["plural"] == ("paychecks", "noun_regular_s")
assert rule_derived_forms("child", "noun [ C ]") == {}
assert rule_derived_forms("policeman", "noun [ C ]") == {}
assert rule_derived_forms("quiz", "noun [ C ]")["plural"][0] == "quizzes"
assert rule_derived_forms("sustainability", "noun [ U ]") == {}
assert "past tense" not in rule_derived_forms("overeat", "verb [ I ]")
assert rule_derived_forms("overeat", "verb [ I ]")["present participle"][0] == "overeating"
assert "third person singular" not in rule_derived_forms("have", "verb")
assert "present participle" not in rule_derived_forms("open", "verb")
assert rule_derived_forms("open", "verb")["third person singular"][0] == "opens"
assert observed_regular_forms({"fragments": [{"locator": ".EXAMPLE:nth(0)", "summary": "She recollected it."}],
                               "senseCandidates": [{"partOfSpeech": "verb", "exampleFragment": None}]},
                              "recollect")[0]["form"] == "recollected"


def evidence(word: str):
    return [{"site": "cambridge", "url": f"https://dictionary.cambridge.org/dictionary/english-chinese-simplified/{word}",
             "collectedAt": iso_timestamp(), "coverage": "extracted",
             "fragments": [{"locator": ".def:nth(0)", "summary": "a useful example"},
                            {"locator": ".uk .ipa:nth(0)", "summary": "bæŋk"},
                            {"locator": ".irreg-infls:nth(0)", "summary": "plural banks"}],
             "pronunciationCandidates": [{"partOfSpeech": "noun", "variety": "uk", "ipa": "bæŋk", "fragment": 1}],
             "inflectionCandidates": [{"partOfSpeech": "noun", "kind": "plural", "form": "banks", "fragment": 2}]}]


def decision():
    return {"senses": [{"partOfSpeech": "noun", "definitionZh": "测试含义", "definitionEn": "a useful example",
                        "zhConfidence": 1.00, "enSourceSupported": True,
                        "evidence": [{"source": 0, "fragment": 0}],
                        "examples": [{"text": "This is a useful example.", "confidence": 0.85}]}]}


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    cli.ENTRY_SCHEMA_VERSION = "1.2"  # Exercise the persisted legacy workflow before the 1.3 contract.
    schema_dir = root / "utils" / "references"
    schema_dir.mkdir(parents=True)
    shutil.copy2(PROJECT / "utils" / "references" / "workflow-state-v1.schema.json", schema_dir)

    word_run = cli.new_run(root, {"kind": "word", "word": "bank"})
    log, out = cli.paths(root, word_run)
    write_json(log / "decision-template.json", cli.decision_template(evidence("bank")))
    patch = {"edits": [
        {"senseIndex": 0, "path": "partOfSpeech", "value": "noun"},
        {"senseIndex": 0, "path": "definitionZh", "value": "测试含义"},
        {"senseIndex": 0, "path": "definitionEn", "value": "a useful example"},
        {"senseIndex": 0, "path": "examples/0/text", "value": "This is a useful example."},
        {"senseIndex": 0, "path": "zhConfidence", "value": 1.0},
        {"senseIndex": 0, "path": "enConfidence", "value": 0.85},
        {"senseIndex": 0, "path": "examples/0/confidence", "value": 0.85},
    ]}
    assert cli.expand_decision(log, patch)["senses"][0]["definitionZh"] == "测试含义"
    assert cli.expand_decision(log, {**patch, "relationDecisions": [
        {"candidateId": "rc_" + "a" * 20, "action": "accept", "type": "synonym"}],
        "derivativeDecisions": [{"candidateId": "dc_" + "b" * 20, "action": "same_family"}]})["relationDecisions"][0]["type"] == "synonym"
    aligned_source = {"site": "oxford", "url": "https://www.oxfordlearnersdictionaries.com/definition/english/bank",
                      "collectedAt": iso_timestamp(), "coverage": "extracted",
                      "fragments": [{"locator": ".sense:nth(0)", "summary": "a useful example"}],
                      "senseCandidates": [{"partOfSpeech": "noun", "definitionEn": "a useful example",
                                           "definitionZh": "", "example": "", "fragment": 0}]}
    aligned_decision = decision()
    aligned_decision["senses"][0]["alignmentEvidence"] = [{"source": 1, "fragment": 0}]
    aligned_entry = build_entry("bank", aligned_decision, [*evidence("bank"), aligned_source])
    assert {value["site"] for value in aligned_entry["senses"][0]["sourceOrders"]} == {"oxford"}
    assert {value["site"] for value in aligned_entry["senses"][0]["definitionEn"]["sourceRefs"]} == {"cambridge"}
    write_json(log / "decision-template.json", decision())
    reviewed = cli.expand_decision(log, {"edits": [], "alignmentEvidenceAdditions": [
        {"senseIndex": 0, "source": 1, "fragment": 0}]})
    reviewed_entry = build_entry("bank", reviewed, [*evidence("bank"), aligned_source])
    assert {value["site"] for value in reviewed_entry["senses"][0]["definitionEn"]["sourceRefs"]} == {"cambridge", "oxford"}
    write_json(log / "evidence.json", evidence("bank"))
    assert cli.advance_word(root, word_run) == 3
    assert cli.read_run(root, word_run)[-1]["status"] == "paused_agent_decision"
    response_file = root / "response.json"
    write_json(response_file, decision())
    sys.argv = ["cli.py", "resume", "--root", str(root), "--run-id", word_run, "--input", str(response_file)]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    assert any((log / "events").glob("*.jsonl"))
    entry = read_json(out / "entry.json")
    legacy_entry = json.loads(json.dumps(entry))
    legacy_entry["schemaVersion"] = "1.1"
    assert cli.validate_entry(legacy_entry, cli.HERE / "references" / "entry.schema.json")
    assert '"confidence": 1.00' in (out / "entry.json").read_text(encoding="utf-8")
    assert "（AI 生成，置信度 0.85）" in (out / "entry.md").read_text(encoding="utf-8")
    assert entry["wordId"] == stable_word_id("bank")
    assert len(entry["pronunciations"]) == 1
    assert entry["inflections"][0]["form"]["text"] == "banks"
    assert regular_adjective_forms("happy") == {"comparative": "happier", "superlative": "happiest"}
    gaps = read_json(out / "gaps.json")
    assert gaps["fieldGaps"] == [{"field": "derivatives", "status": "pending_review",
                                   "reason": "field_not_confirmed", "sourceSites": []}]
    assert "待核验" in (out / "entry.md").read_text(encoding="utf-8")
    adjective = decision()
    adjective["senses"][0]["partOfSpeech"] = "adjective"
    adjective_evidence = evidence("happy")
    adjective_evidence[0]["inflectionCandidates"] = []
    adjective_entry = build_entry("happy", adjective, adjective_evidence)
    assert {gap["field"]: gap["status"] for gap in cli.assess_field_gaps(adjective_entry, adjective_evidence)} == {
        "inflections": "pending_review", "derivatives": "pending_review"}
    assert len(parse_word_list(b"word,pos\nbank,noun\nbank,verb\n", "csv", "word", "pos")) == 1
    assert parse_word_list(b"bank\nbanker\nbank\n", "text")[0]["sourceLines"] == [1, 3]
    assert parse_word_list(b'{"word":"bank","core_meanings":[{"pos":"n","definition_zh":"shore"}]}\n', "jsonl")[0]["meaningHint"] == "shore"
    assert parse_word_list(b'[{"word":"bank"}]', "json")[0]["word"] == "bank"
    cited_ai = item("AI paraphrase", refs=[{"site": "cambridge", "url": "https://dictionary.cambridge.org/example",
                                             "locator": ".def:nth(0)", "collectedAt": iso_timestamp(), "summary": "evidence"}],
                    confidence=0.85, source_supported=False)
    assert cited_ai["generationMethod"] == "ai_generated" and cited_ai["sourceRefs"]
    irregular_source = evidence("go")
    irregular_source[0]["fragments"].append({"locator": ".irreg-infls:nth(1)", "summary": "past tense went"})
    irregular_source[0]["inflectionCandidates"].append({"partOfSpeech": "verb", "kind": "past tense", "form": "went", "fragment": 3})
    go_decision = decision()
    go_decision["senses"][0]["partOfSpeech"] = "verb"
    assert next(form for form in build_entry("go", go_decision, irregular_source)["inflections"] if form["form"]["text"] == "went")["irregular"]
    fallback = [{**evidence("bank")[0], "site": "oxford", "senseCandidates": [{"partOfSpeech": "noun", "definitionEn": "edge of a river", "definitionZh": "", "example": "", "fragment": 0}]}]
    assert cli.decision_template(fallback)["senses"][0]["definitionEn"] == "edge of a river"
    shared = evidence("bank")
    shared[0]["senseCandidates"] = [{"partOfSpeech": "noun", "definitionEn": "a useful example", "definitionZh": "测试含义", "example": "", "fragment": 0}]
    shared.append({**shared[0], "site": "oxford", "senseCandidates": [{"partOfSpeech": "noun", "definitionEn": "a useful example", "definitionZh": "", "example": "", "fragment": 0}]})
    aligned = cli.decision_template(shared)["senses"][0]
    assert aligned["alignmentEvidence"] == [{"source": 1, "fragment": 0}]
    assert aligned["enEvidence"] == [{"source": 0, "fragment": 0}, {"source": 1, "fragment": 0}]
    for command in ("status", "verify", "deliver"):
        sys.argv = ["cli.py", command, "--root", str(root), "--run-id", word_run]
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli.main() == 0
    canonical_path = entry_records(root) / f"{stable_word_id('bank')}.json"
    intact = canonical_path.read_text(encoding="utf-8")
    altered = read_json(canonical_path)
    altered["senses"][0]["definitionZh"]["text"] = "被篡改"
    canonical_path.write_text(cli.entry_json_text(altered), encoding="utf-8")
    sys.argv = ["cli.py", "verify", "--root", str(root), "--run-id", word_run]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert cli.main() == 4
    canonical_path.write_text(intact, encoding="utf-8")

    lexical_evidence = evidence("bright")
    lexical_evidence[0]["fragments"].append({"locator": ".wordfamily a:nth(0)", "summary": "word family: brightness"})
    lexical_evidence[0]["derivativeCandidates"] = [{"word": "brightness", "relationHint": "word_family", "fragment": 3}]
    lexical_evidence.append({"site": "thesaurus", "url": "https://www.thesaurus.com/browse/bright",
                             "collectedAt": iso_timestamp(), "coverage": "extracted",
                             "fragments": [{"locator": ".definition-block:nth(0)", "summary": "adjective: intelligent"},
                                           {"locator": ".synonyms a:nth(0)", "summary": "synonyms: clever"},
                                           {"locator": ".synonyms a:nth(1)", "summary": "synonyms: sharp"}],
                             "relationGroups": [{"groupIndex": 0, "partOfSpeech": "adjective", "gloss": "intelligent", "fragment": 0}],
                             "externalCandidates": [{"word": target, "fragment": index, "groupIndex": 0,
                                                     "relationHint": "synonym_or_near_synonym", "partOfSpeech": "adjective",
                                                     "senseGloss": "intelligent"}
                                                    for index, target in enumerate(("clever", "sharp"), 1)]})
    lexical_decision = decision()
    lexical_decision["senses"][0]["partOfSpeech"] = "adjective"
    lexical_decision["relationSenseMappings"] = [{"source": 1, "groupIndex": 0, "senseIndex": 0}]
    lexical_decision["relationDecisions"] = [
        {"candidateId": candidate_id("rc", "bright", 1, 0, "clever", "synonym_or_near_synonym"),
         "action": "accept", "type": "near_synonym"},
        {"candidateId": candidate_id("rc", "bright", 1, 0, "sharp", "synonym_or_near_synonym"),
         "action": "reject"}]
    lexical_decision["derivativeDecisions"] = [
        {"candidateId": candidate_id("dc", "bright", 0, 0, "brightness"), "action": "direct_derivative"}]
    lexical_run = cli.new_run(root, {"kind": "word", "word": "bright"})
    lexical_log, lexical_out = cli.paths(root, lexical_run)
    write_json(lexical_log / "evidence.json", lexical_evidence)
    write_json(lexical_log / "agent-response.json", lexical_decision)
    assert cli.advance_word(root, lexical_run) == 0
    assert len(list((root / "outputs" / "词汇星图" / "dicts").glob("?.jsonl"))) == 26
    assert stable_word_id("bright") in read_all(root / "outputs" / "词汇星图" / "dicts")
    lexical_entry = read_json(lexical_out / "entry.json")
    assert [(value["targetLemma"], value["type"], value["linkStatus"])
            for value in lexical_entry["relationships"]] == [("clever", "near_synonym", "lemma_only")]
    assert not lexical_entry["pendingRelations"]
    assert lexical_entry["derivatives"][0]["word"] == "brightness"
    malformed_relation = json.loads(json.dumps(lexical_entry))
    del malformed_relation["relationships"][0]["linkStatus"]
    try:
        cli.validate_entry(malformed_relation, cli.HERE / "references" / "entry.schema.json")
        raise AssertionError("1.2 关系必须有 linkStatus")
    except ValueError:
        pass
    assert not any(gap["field"] == "derivatives" for gap in read_json(lexical_out / "gaps.json")["fieldGaps"])
    for command in ("status", "verify", "deliver"):
        sys.argv = ["cli.py", command, "--root", str(root), "--run-id", lexical_run]
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli.main() == 0

    original = cli.advance_word
    def fixture_word(root: Path, run_id: str, answer=None):
        word_log, _ = cli.paths(root, run_id)
        request = read_json(word_log / "request.json")
        if not (word_log / "evidence.json").exists():
            sites = lexical_evidence if request["word"] == "bright" else evidence(request["word"])
            if request["word"] == "banker":
                sites.append({"site": "thesaurus", "url": "https://www.thesaurus.com/browse/banker",
                              "collectedAt": iso_timestamp(), "coverage": "extracted",
                              "fragments": [{"locator": ".definition-block:nth(0)", "summary": "noun: a person in finance"},
                                            {"locator": ".synonym-antonym-word-chip:nth(0)", "summary": "synonyms: financier"}],
                              "relationGroups": [{"groupIndex": 0, "partOfSpeech": "noun", "gloss": "a person in finance", "fragment": 0}],
                              "externalCandidates": [{"word": "financier", "fragment": 1, "groupIndex": 0,
                                                      "relationHint": "synonym_or_near_synonym", "partOfSpeech": "noun",
                                                      "senseGloss": "a person in finance"}]})
            write_json(word_log / "evidence.json", sites)
        if not (word_log / "agent-response.json").exists():
            response = lexical_decision if request["word"] == "bright" else decision()
            if request["word"] in ("bright", "clever"):
                response["senses"][0]["partOfSpeech"] = "adjective"
            if request["word"] == "banker":
                response["relationSenseMappings"] = [{"source": 1, "groupIndex": 0, "senseIndex": 0}]
                response["relationDecisions"] = [{"candidateId": candidate_id(
                    "rc", "banker", 1, 0, "financier", "synonym_or_near_synonym"), "action": "uncertain"}]
            write_json(word_log / "agent-response.json", response)
        return original(root, run_id, answer)
    cli.advance_word = fixture_word
    clever_run = cli.new_run(root, {"kind": "word", "word": "clever"})
    assert cli.advance_word(root, clever_run) == 3
    assert cli.read_run(root, clever_run)[-1]["status"] == "paused_relation_review"
    incoming = read_json(cli.paths(root, clever_run)[0] / "incoming-link-review.json")
    assert [(value["sourceWord"], value["targetWord"]) for value in incoming] == [("bright", "clever")]
    bright_path = entry_records(root) / f"{stable_word_id('bright')}.json"
    clever_path = entry_records(root) / f"{stable_word_id('clever')}.json"
    bright_before_link = read_json(bright_path)
    original_relation_id = bright_before_link["relationships"][0]["relationshipId"]
    assert cli.advance_word(root, clever_run, {"links": []}) == 3
    assert cli.read_run(root, clever_run)[-1]["status"] == "paused_quality_review"
    link_response = {"links": [{
        "sourceWord": "bright", "relationshipId": original_relation_id, "action": "link",
        "targetSenseId": read_json(clever_path)["senses"][0]["senseId"]}]}
    link_response_file = root / "link-response.json"
    write_json(link_response_file, link_response)
    sys.argv = ["cli.py", "resume", "--root", str(root), "--run-id", clever_run,
                "--input", str(link_response_file)]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    bright_linked = read_json(bright_path)
    assert bright_linked["relationships"][0]["relationshipId"] == original_relation_id
    assert bright_linked["relationships"][0]["linkStatus"] == "linked"
    assert bright_linked["relationships"][0]["targetWordId"] == stable_word_id("clever")
    assert any(value["targetWordId"] == stable_word_id("bright")
               for value in read_json(clever_path)["relationships"])
    sys.argv = ["cli.py", "verify", "--root", str(root), "--run-id", lexical_run]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    bright_again = cli.new_run(root, {"kind": "word", "word": "bright"})
    bright_again_log, bright_again_out = cli.paths(root, bright_again)
    write_json(bright_again_log / "evidence.json", lexical_evidence)
    write_json(bright_again_log / "agent-response.json", lexical_decision)
    assert cli.advance_word(root, bright_again) == 0
    assert len(read_json(bright_again_out / "entry.json")["relationships"]) == 1
    lexical_batch = cli.new_run(root, {"kind": "batch", "inlineText": "bright\nclever"})
    assert cli.advance_batch(root, lexical_batch) == 3
    lexical_pair = next(value for value in read_json(cli.paths(root, lexical_batch)[0] / "relation-candidates.json")
                        if {value["left"], value["right"]} == {"bright", "clever"})
    assert lexical_pair["left"] == "bright"
    assert cli.advance_batch(root, lexical_batch, {"accepted": [{
        "left": "bright", "right": "clever", "type": "near_synonym",
        "sourceSenseId": read_json(bright_path)["senses"][0]["senseId"],
        "targetSenseId": read_json(clever_path)["senses"][0]["senseId"],
        "evidence": [{"word": "bright", "source": 1, "fragment": 1}]}]}) == 0
    assert len(read_json(bright_path)["relationships"]) == 1
    sys.argv = ["cli.py", "verify", "--root", str(root), "--run-id", clever_run]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    sys.argv = ["cli.py", "start-word", "--root", str(root), "--word", "river"]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    sys.argv = ["cli.py", "start-list", "--root", str(root), "--inline-text", "stone"]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    batch_run = cli.new_run(root, {"kind": "batch", "inlineText": "bank\nbanker\nbank"})
    assert cli.advance_batch(root, batch_run) == 3
    assert cli.read_run(root, batch_run)[-1]["status"] == "paused_relation_review"
    candidate = read_json(cli.paths(root, batch_run)[0] / "relation-candidates.json")[0]
    assert cli.advance_batch(root, batch_run, {"accepted": [{"left": candidate["left"], "right": candidate["right"], "type": "family"}]}) == 0
    result = read_json(cli.paths(root, batch_run)[1] / "batch-result.json")
    assert result["coverage"]["completed"] == result["coverage"]["total"] == 2
    assert result["coverage"]["sites"]["cambridge"] == 2
    bank = read_json(entry_records(root) / f"{stable_word_id('bank')}.json")
    banker = read_json(entry_records(root) / f"{stable_word_id('banker')}.json")
    assert any(r["intraList"] for r in bank["relationships"])
    assert len(bank["senses"]) == 1
    assert bank["familyId"] == banker["familyId"]
    assert not bank["derivatives"] and not banker["derivatives"]
    assert banker["schemaVersion"] == "1.2"
    assert [(candidate["targetLemma"], candidate["proposedType"], candidate["sourceSenseId"])
            for candidate in banker["pendingRelations"]] == [
                ("financier", "synonym_or_near_synonym", banker["senses"][0]["senseId"])]
    banker_response = decision()
    banker_response["relationSenseMappings"] = [{"source": 1, "groupIndex": 0, "senseIndex": 0}]
    banker_evidence = read_json(cli.paths(root, result["words"][1]["runId"])[0] / "evidence.json")
    repeated_banker = build_entry("banker", banker_response, banker_evidence, banker)
    assert repeated_banker["pendingRelations"] == banker["pendingRelations"]
    assert result["words"][0]["sourceLines"] == [1, 3]
    assert result["pendingExternalCandidates"][0]["word"] == "financier"
    assert '"confidence": 1.00' in (entry_records(root) / f"{stable_word_id('bank')}.json").read_text(encoding="utf-8")
    for command in ("status", "verify", "deliver"):
        sys.argv = ["cli.py", command, "--root", str(root), "--run-id", batch_run]
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli.main() == 0
    relation_run = cli.new_run(root, {"kind": "batch", "inlineText": "banker\nfinancier"})
    assert cli.advance_batch(root, relation_run) == 3
    assert cli.read_run(root, relation_run)[-1]["status"] == "paused_relation_review"
    banker_path = entry_records(root) / f"{stable_word_id('banker')}.json"
    financier_path = entry_records(root) / f"{stable_word_id('financier')}.json"
    assert read_json(banker_path)["pendingRelations"][0]["status"] == "pending_relation_review"
    review_packet = read_json(cli.paths(root, relation_run)[0] / "relation-review-packet.json")
    pair = next(value for value in review_packet if value["left"] == "banker" and value["right"] == "financier")
    assert all(pair["senses"][word][0]["dictionaryRefs"] for word in ("banker", "financier"))
    banker_sense = read_json(banker_path)["senses"][0]["senseId"]
    financier_sense = read_json(financier_path)["senses"][0]["senseId"]
    assert cli.advance_batch(root, relation_run, {"accepted": [{
        "left": "banker", "right": "financier", "type": "synonym",
        "sourceSenseId": banker_sense, "targetSenseId": financier_sense,
        "evidence": [{"word": "banker", "source": 1, "fragment": 1}]}]}) == 0
    banker = read_json(banker_path)
    financier = read_json(financier_path)
    assert not banker["pendingRelations"]
    assert any(r["targetWordId"] == financier["wordId"] and r["type"] == "synonym" for r in banker["relationships"])
    assert any(r["targetWordId"] == banker["wordId"] and r["type"] == "synonym" for r in financier["relationships"])
    banker_run = next(row["runId"] for row in result["words"] if row["word"] == "banker")
    sys.argv = ["cli.py", "verify", "--root", str(root), "--run-id", banker_run]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    assert not build_entry("banker", banker_response, banker_evidence, banker)["pendingRelations"]
    bank_path = entry_records(root) / f"{stable_word_id('bank')}.json"
    bank_before_derivation = read_json(bank_path)
    bank_before_derivation["derivatives"].append({"derivativeId": "d_" + "a" * 20,
        "word": "banker", "status": "candidate", "verificationStatus": "agent_reviewed",
        "sourceRefs": bank_before_derivation["senses"][0]["definitionEn"]["sourceRefs"]})
    write_json(bank_path, bank_before_derivation)
    derivation_run = cli.new_run(root, {"kind": "batch", "inlineText": "bank\nbanker"})
    assert cli.advance_batch(root, derivation_run) == 3
    assert cli.advance_batch(root, derivation_run, {"accepted": [{"left": "bank", "right": "banker",
        "type": "family", "derivationConfirmed": True,
        "evidence": [{"word": "bank", "source": 0, "fragment": 0}]}]}) == 0
    bank = read_json(entry_records(root) / f"{stable_word_id('bank')}.json")
    banker = read_json(entry_records(root) / f"{stable_word_id('banker')}.json")
    assert bank["derivatives"][0]["targetWordId"] == banker["wordId"]
    assert bank["derivatives"][0]["derivativeId"] == "d_" + "a" * 20
    assert banker["derivatives"][0]["targetWordId"] == bank["wordId"]
    input_file = root / "private-list.csv"
    input_file.write_text("word,pos,hint\nriver,noun,water\n", encoding="utf-8")
    sys.argv = ["cli.py", "start-list", "--root", str(root), "--input", str(input_file),
                "--format", "csv", "--word-column", "word", "--pos-column", "pos", "--meaning-column", "hint"]
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        assert cli.main() == 0
    recent = json.loads(capture.getvalue())["run_id"]
    report = (cli.paths(root, recent)[1] / "batch-result.json").read_text(encoding="utf-8")
    assert str(root) not in report and "private-list.csv" in report

    cli.advance_word = original
    cli.ENTRY_SCHEMA_VERSION = "1.3"
    modern_evidence = [{"site": "cambridge", "url": "https://dictionary.cambridge.org/dictionary/english-chinese-simplified/happy",
        "collectedAt": iso_timestamp(), "coverage": "extracted", "fragments": [
            {"locator": ".def-block:nth(0)", "summary": "feeling pleasure / 快乐的"},
            {"locator": ".def-block:nth(0) .examp", "summary": "She looks happy. 她看起来很快乐。"},
            {"locator": ".uk .ipa", "summary": "ˈhæp.i"},
            {"locator": ".examp:nth(1)", "summary": "She looks happier today."}],
        "senseCandidates": [{"partOfSpeech": "adjective", "definitionEn": "feeling pleasure",
            "definitionZh": "快乐的", "example": "She looks happy.",
            "exampleTranslationZh": "她看起来很快乐。", "fragment": 0, "exampleFragment": 1}],
        "pronunciationCandidates": [{"partOfSpeech": "adjective", "variety": "uk",
            "ipa": "ˈhæp.i", "fragment": 2}],
        "inflectionCandidates": [{"partOfSpeech": "adjective", "kind": "comparative",
            "form": "happier", "fragment": 3}]}]
    modern_run = cli.new_run(root, {"kind": "word", "word": "happy"})
    modern_log, modern_out = cli.paths(root, modern_run)
    write_json(modern_log / "evidence.json", modern_evidence)
    assert cli.advance_word(root, modern_run) == 3
    assert cli.read_run(root, modern_run)[-1]["status"] == "paused_agent_decision", cli.read_run(root, modern_run)[-1]
    modern_packet = read_json(modern_log / "generation_packet.json")
    modern_patch = cli.expand_decision(modern_log, {"edits": [],
        "evidenceDigest": modern_packet["evidenceDigest"],
        "usageUpdates": [{"senseIndex": 0, "kind": "examples", "itemIndex": 0,
                          "zhSpans": [{"start": 5, "end": 7}]}]})
    assert modern_patch["senses"][0]["examples"][0]["emphasis"]["zh"] == [{"start": 5, "end": 7}]
    modern_response = read_json(modern_log / "decision-template.json")
    modern_response["evidenceDigest"] = modern_packet["evidenceDigest"]
    modern_response["senses"][0]["examples"][0]["emphasis"]["zh"] = [{"start": 5, "end": 7}]
    modern_response["senses"][0]["collocations"] = [{"text": "a happy day", "confidence": 0.90,
        "translationZh": {"text": "快乐的一天", "confidence": 0.90}}]
    modern_response["senses"][0]["phrases"] = [{"text": "happy medium", "confidence": 0.90,
        "translationZh": {"text": "折中方案", "confidence": 0.90}}]
    modern_input = root / "modern-response.json"
    write_json(modern_input, modern_response)
    sys.argv = ["cli.py", "resume", "--root", str(root), "--run-id", modern_run,
                "--input", str(modern_input)]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    modern_entry = read_json(modern_out / "entry.json")
    assert modern_entry["schemaVersion"] == "1.3"
    assert modern_entry["pronunciations"][0]["ipa"]["verificationStatus"] == "automatic_passed"
    assert modern_entry["inflections"][0]["form"]["verificationStatus"] == "automatic_passed"
    from rule_inflections import _updated
    derived_entry = json.loads(json.dumps(modern_entry))
    basis = derived_entry["senses"][0]
    derived_entry = _updated(derived_entry, [{"kind": "superlative", "text": "happiest",
        "partOfSpeech": basis["partOfSpeech"], "derivation": {"ruleId": "test_rule", "ruleVersion": "1",
        "basisSenseId": basis["senseId"], "basisSourceRefs": basis["definitionEn"]["sourceRefs"]}}])
    cli.validate_entry(derived_entry, cli.HERE / "references" / "entry.schema.json")
    assert derived_entry["inflections"][-1]["form"]["generationMethod"] == "rule_derived"
    unsupported_rule = json.loads(json.dumps(derived_entry))
    unsupported_rule["inflections"][-1]["form"]["sourceRefs"] = basis["definitionEn"]["sourceRefs"]
    try:
        cli.validate_entry(unsupported_rule, cli.HERE / "references" / "entry.schema.json")
        raise AssertionError("规则推导不能伪称直接来源")
    except ValueError:
        pass
    from family_review import _add_candidate
    source = json.loads(json.dumps(modern_evidence[0]))
    source["fragments"].append({"locator": ".examp:nth(2)", "summary": "She looks happiest today."})
    source["inflectionCandidates"].append({"partOfSpeech": "adjective", "kind": "superlative",
                                           "form": "happiest", "fragment": 4})
    previous_form_id = derived_entry["inflections"][-1]["formId"]
    _add_candidate(derived_entry, "inflections", {"value": "happiest", "kind": "superlative",
        "partOfSpeech": "adjective", "sourceRef": cli.source_ref(source["fragments"][4], source)}, [source])
    assert derived_entry["inflections"][-1]["formId"] == previous_form_id
    assert derived_entry["inflections"][-1]["form"]["generationMethod"] == "source_supported"
    assert "derivation" not in derived_entry["inflections"][-1]
    assert read_json(modern_out / "gaps.json")["verificationGaps"] == []
    pending_entry = json.loads(json.dumps(modern_entry))
    pending_evidence = json.loads(json.dumps(modern_evidence))
    pending_evidence[0]["pronunciationCandidates"] = []
    pending_gaps = cli.verify_observed_pronunciations_and_forms(pending_entry, pending_evidence)
    assert pending_entry["pronunciations"][0]["ipa"]["verificationStatus"] == "pending"
    assert any(gap["field"] == "pronunciations" and gap["reason"] == "source_candidate_mismatch"
               for gap in pending_gaps)
    assert "source_candidate_mismatch" in cli.render_entry_markdown(pending_entry, {
        "sourceCoverage": [], "fieldGaps": [], "verificationGaps": pending_gaps})
    markdown = (modern_out / "entry.md").read_text(encoding="utf-8")
    assert "She looks **happy**." in markdown and "她看起来很**快乐**。" in markdown
    assert "快乐的一天" in markdown and "折中方案" in markdown
    missing_translation = json.loads(json.dumps(modern_entry))
    del missing_translation["senses"][0]["collocations"][0]["translationZh"]
    try:
        cli.validate_entry(missing_translation, cli.HERE / "references" / "entry.schema.json")
        raise AssertionError("1.3 搭配必须有中文翻译")
    except ValueError:
        pass
    for command in ("status", "verify", "deliver"):
        sys.argv = ["cli.py", command, "--root", str(root), "--run-id", modern_run]
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli.main() == 0
    (modern_out / "entry.md").write_text(markdown + "tampered\n", encoding="utf-8")
    sys.argv = ["cli.py", "verify", "--root", str(root), "--run-id", modern_run]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert cli.main() == 4
    (modern_out / "entry.md").write_text(markdown, encoding="utf-8")
    prior_entry = json.loads(json.dumps(modern_entry))
    prior_entry["schemaVersion"] = "1.2"
    for sense in prior_entry["senses"]:
        for group in ("examples", "collocations", "phrases"):
            for usage in sense[group]:
                usage.pop("translationZh", None)
                usage.pop("emphasis", None)
    write_json(entry_records(root) / f"{stable_word_id('happy')}.json", prior_entry)
    upsert(root / "outputs" / "词汇星图" / "dicts", prior_entry, render_entry_line)
    upgrade_run = cli.new_run(root, {"kind": "word", "word": "happy"})
    upgrade_log, _ = cli.paths(root, upgrade_run)
    write_json(upgrade_log / "evidence.json", modern_evidence)
    assert cli.advance_word(root, upgrade_run) == 3
    upgrade_packet = read_json(upgrade_log / "generation_packet.json")
    assert {item["kind"] for item in upgrade_packet["legacyUsage"]} == {
        "examples", "collocations", "phrases"}
    legacy_updates = [{"itemId": item["itemId"],
                       "translationZh": "快乐的一天" if item["kind"] == "collocations" else "折中方案",
                       "confidence": 0.90}
                      for item in upgrade_packet["legacyUsage"] if item["kind"] != "examples"]
    upgrade_response = {"edits": [], "evidenceDigest": upgrade_packet["evidenceDigest"],
                        "usageUpdates": [{"senseIndex": 0, "kind": "examples", "itemIndex": 0,
                                          "zhSpans": [{"start": 5, "end": 7}]}],
                        "legacyUsageUpdates": legacy_updates}
    upgrade_input = root / "upgrade-response.json"
    write_json(upgrade_input, upgrade_response)
    sys.argv = ["cli.py", "resume", "--root", str(root), "--run-id", upgrade_run,
                "--input", str(upgrade_input)]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    upgraded = read_json(cli.paths(root, upgrade_run)[1] / "entry.json")
    assert upgraded["revision"] == modern_entry["revision"] + 1
    assert upgraded["senses"][0]["examples"][0]["translationZh"]["generationMethod"] == "source_supported"
    assert upgraded["senses"][0]["collocations"][0]["translationZh"]["text"] == "快乐的一天"
    modern_batch = cli.new_run(root, {"kind": "batch", "inlineText": "joyful"})
    saved_advance_word = cli.advance_word
    cli.advance_word = lambda *_args, **_kwargs: 3
    try:
        assert cli.advance_batch(root, modern_batch) == 3
    finally:
        cli.advance_word = saved_advance_word
    modern_batch_log, _ = cli.paths(root, modern_batch)
    child_run = read_json(modern_batch_log / "words.json")[0]["runId"]
    assert cli.read_run(root, child_run)[2]["entrySchemaVersion"] == "1.3"

    # Current workflow: content review is a separate, resumable and auditable stage.
    root14 = root / "v14"
    schema14 = root14 / "utils" / "references"
    schema14.mkdir(parents=True)
    shutil.copy2(PROJECT / "utils" / "references" / "workflow-state-v1.schema.json", schema14)
    cli.ENTRY_SCHEMA_VERSION = "1.4"
    review_run = cli.new_run(root14, {"kind": "word", "word": "happy"})
    review_log, review_out = cli.paths(root14, review_run)
    write_json(review_log / "evidence.json", modern_evidence)
    assert cli.advance_word(root14, review_run) == 3
    review_packet = read_json(review_log / "generation_packet.json")
    review_response = read_json(review_log / "decision-template.json")
    review_response["evidenceDigest"] = review_packet["evidenceDigest"]
    review_response["senses"][0]["examples"][0]["emphasis"]["zh"] = [{"start": 5, "end": 7}]
    review_response["senses"][0]["collocations"] = [{"text": "a happy day", "confidence": 0.90,
        "translationZh": {"text": "快乐的一天", "confidence": 0.90}}]
    assert cli.advance_word(root14, review_run, review_response) == 3
    assert cli.read_run(root14, review_run)[-1]["status"] == "paused_agent_decision"
    content_packet = read_json(review_log / "content-review-template.json")
    assert len(content_packet["senses"]) == 1
    assert len(content_packet["senses"][0]["items"]) == 6
    content_answer = {"evidenceDigest": content_packet["evidenceDigest"],
        "candidateDigest": content_packet["candidateDigest"],
        "approvedSenseIds": [content_packet["senses"][0]["senseId"]], "deferredItemIds": []}
    deferred_entry = read_json(review_log / "content-review-candidate.json")
    deferred_id = deferred_entry["senses"][0]["examples"][0]["translationZh"]["itemId"]
    deferred_gaps = apply_content_review(deferred_entry, content_packet,
        {**content_answer, "deferredItemIds": [deferred_id]}, review_run)
    assert deferred_entry["senses"][0]["examples"][0]["translationZh"]["verificationStatus"] == "pending"
    assert [gap["itemId"] for gap in deferred_gaps] == [deferred_id]
    content_input = root14 / "content-response.json"
    write_json(content_input, content_answer)
    sys.argv = ["cli.py", "resume", "--root", str(root14), "--run-id", review_run,
                "--input", str(content_input)]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    reviewed_entry = read_json(review_out / "entry.json")
    assert reviewed_entry["schemaVersion"] == "1.4"
    assert all(row["verificationStatus"] == "agent_passed" for row in
        [reviewed_entry["senses"][0]["definitionZh"], reviewed_entry["senses"][0]["definitionEn"],
         reviewed_entry["senses"][0]["examples"][0],
         reviewed_entry["senses"][0]["examples"][0]["translationZh"],
         reviewed_entry["senses"][0]["collocations"][0],
         reviewed_entry["senses"][0]["collocations"][0]["translationZh"]])
    assert reviewed_entry["senses"][0]["collocations"][0]["generationMethod"] == "ai_generated"
    assert "Agent 已核验" in (review_out / "entry.md").read_text(encoding="utf-8")
    invalid_legacy = json.loads(json.dumps(reviewed_entry))
    invalid_legacy["schemaVersion"] = "1.3"
    try:
        cli.validate_entry(invalid_legacy, cli.HERE / "references" / "entry.schema.json")
        raise AssertionError("1.3 词条不得冒用 Agent 内容核验状态")
    except ValueError:
        pass
    invalid_ipa = json.loads(json.dumps(reviewed_entry))
    invalid_ipa["pronunciations"][0]["ipa"]["verificationStatus"] = "agent_passed"
    invalid_ipa["pronunciations"][0]["ipa"]["verificationRef"] = reviewed_entry["senses"][0]["definitionZh"]["verificationRef"]
    try:
        cli.validate_entry(invalid_ipa, cli.HERE / "references" / "entry.schema.json")
        raise AssertionError("读音不得冒用 Agent 内容核验状态")
    except ValueError:
        pass
    for command in ("status", "verify", "deliver"):
        sys.argv = ["cli.py", command, "--root", str(root14), "--run-id", review_run]
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli.main() == 0
    countable_evidence = json.loads(json.dumps(modern_evidence))
    countable_source = countable_evidence[0]
    countable_source["url"] = "https://dictionary.cambridge.org/dictionary/english-chinese-simplified/paycheck"
    countable_source["fragments"] = [
        {"locator": ".def-block:nth(0)", "summary": "money earned for work / 薪水"},
        {"locator": ".examp:nth(0)", "summary": "She received a paycheck. 她收到一笔薪水。"},
        {"locator": ".uk .ipa", "summary": "ˈpeɪ.tʃek"}]
    countable_source["senseCandidates"] = [{"partOfSpeech": "noun [ C ]",
        "definitionEn": "money earned for work", "definitionZh": "薪水",
        "example": "She received a paycheck.", "exampleTranslationZh": "她收到一笔薪水。",
        "fragment": 0, "exampleFragment": 1}]
    countable_source["pronunciationCandidates"] = [{"partOfSpeech": "noun [ C ]",
        "variety": "uk", "ipa": "ˈpeɪ.tʃek", "fragment": 2}]
    countable_source["inflectionCandidates"] = []
    countable_run = cli.new_run(root14, {"kind": "word", "word": "paycheck"})
    countable_log, countable_out = cli.paths(root14, countable_run)
    write_json(countable_log / "evidence.json", countable_evidence)
    assert cli.advance_word(root14, countable_run) == 3
    countable_response = read_json(countable_log / "decision-template.json")
    countable_response["evidenceDigest"] = read_json(countable_log / "generation_packet.json")["evidenceDigest"]
    countable_response["senses"][0]["examples"][0]["emphasis"]["zh"] = [{"start": 5, "end": 7}]
    assert cli.advance_word(root14, countable_run, countable_response) == 3
    countable_packet = read_json(countable_log / "content-review-template.json")
    assert cli.advance_word(root14, countable_run, {"evidenceDigest": countable_packet["evidenceDigest"],
        "candidateDigest": countable_packet["candidateDigest"],
        "approvedSenseIds": [countable_packet["senses"][0]["senseId"]], "deferredItemIds": []}) == 0
    countable_entry = read_json(countable_out / "entry.json")
    assert countable_entry["schemaVersion"] == "1.5"
    pending_plural = next(form for form in countable_entry["inflections"] if form["form"]["text"] == "paychecks")
    assert pending_plural["form"]["generationMethod"] == "rule_derived"
    assert pending_plural["derivation"]["ruleVersion"] == "2"
    assert "规则推导 · 待核验" in (countable_out / "entry.md").read_text(encoding="utf-8")
    sys.argv = ["cli.py", "verify", "--root", str(root14), "--run-id", countable_run]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    corrupted = {**content_answer, "candidateDigest": "0" * 64}
    write_json(review_log / "content-review-decision.json", corrupted)
    sys.argv = ["cli.py", "verify", "--root", str(root14), "--run-id", review_run]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert cli.main() == 4
    write_json(review_log / "content-review-decision.json", content_answer)
    receipt = read_json(review_log / "content-review-receipt.json")
    write_json(review_log / "content-review-receipt.json", {**receipt, "reviewedAt": "2000-01-01T00:00:00"})
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert cli.main() == 4
    write_json(review_log / "content-review-receipt.json", receipt)
    batch14 = cli.new_run(root14, {"kind": "batch", "inlineText": "happy"})
    saved_advance_word = cli.advance_word
    cli.advance_word = lambda *_args, **_kwargs: 3
    try:
        assert cli.advance_batch(root14, batch14) == 3
    finally:
        cli.advance_word = saved_advance_word
    batch14_log, _ = cli.paths(root14, batch14)
    batch14_child = read_json(batch14_log / "words.json")[0]["runId"]
    assert cli.read_run(root14, batch14_child)[2]["entrySchemaVersion"] == "1.4"
    batch_child_log, _ = cli.paths(root14, batch14_child)
    write_json(batch_child_log / "evidence.json", modern_evidence)
    assert cli.advance_batch(root14, batch14) == 3
    child_generation = read_json(batch_child_log / "generation_packet.json")
    child_response = read_json(batch_child_log / "decision-template.json")
    child_response["evidenceDigest"] = child_generation["evidenceDigest"]
    child_response["senses"][0]["examples"][0]["emphasis"]["zh"] = [{"start": 5, "end": 7}]
    assert cli.advance_batch(root14, batch14, child_response) == 3
    child_content = read_json(batch_child_log / "content-review-template.json")
    child_decision = {"evidenceDigest": child_content["evidenceDigest"],
        "candidateDigest": child_content["candidateDigest"],
        "approvedSenseIds": [child_content["senses"][0]["senseId"]],
        "deferredItemIds": [next(item["itemId"] for item in child_content["senses"][0]["items"]
                                  if item["field"] == "examples.translationZh")]}
    assert cli.advance_batch(root14, batch14, child_decision) == 0
    child_out = cli.paths(root14, batch14_child)[1]
    assert read_json(child_out / "gaps.json")["contentGaps"][0]["itemId"] == child_decision["deferredItemIds"][0]
    assert read_json(child_out / "entry.json")["senses"][0]["examples"][0]["translationZh"]["verificationStatus"] == "pending"
    sys.argv = ["cli.py", "verify", "--root", str(root14), "--run-id", batch14]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    sys.argv = ["cli.py", "verify", "--root", str(root14), "--run-id", review_run]
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main() == 0
    assert subsequence_similarity("quiet", "quite") == 0.8
    assert subsequence_similarity("form", "from") == 0.75
    assert ("abcdefgh", "abxcdxefxgh") in set(iter_similar_pairs(["abcdefgh", "abxcdxefxgh"]))
    assert ("quiet", "quite") in set(iter_similar_pairs(["quiet", "quite", "stone"]))
    assert ("form", "word") not in set(iter_similar_pairs(["form", "word"]))
    small_words = ["".join(chars) for length in range(1, 5) for chars in product("ab", repeat=length)]
    expected_pairs = {tuple(sorted((left, right))) for index, left in enumerate(small_words)
                      for right in small_words[index + 1:] if meets_subsequence_threshold(left, right)}
    assert set(iter_similar_pairs(small_words)) == expected_pairs
    spelling_root = root / "spelling"
    spelling_schema = spelling_root / "utils" / "references"
    spelling_schema.mkdir(parents=True)
    shutil.copy2(PROJECT / "utils" / "references" / "workflow-state-v1.schema.json", spelling_schema)
    for word in ("quiet", "quite"):
        word_entry = build_entry(word, decision(), evidence(word))
        # These two spelling fixtures represent independent words, not the happy fixture
        # whose aliases and inflected forms the generic decision helper supplies.
        word_entry.update(aliases=[], inflections=[], derivatives=[])
        entry_path = entry_records(spelling_root) / f"{stable_word_id(word)}.json"
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text_atomic(entry_path, cli.entry_json_text(word_entry))
    spelling_run = cli.new_run(spelling_root, {"kind": "batch", "inlineText": "quiet\nquite", "entrySchemaVersion": "1.2"})
    saved_advance_word = cli.advance_word
    cli.advance_word = lambda *_args, **_kwargs: 0
    try:
        assert cli.advance_batch(spelling_root, spelling_run) == 0
    finally:
        cli.advance_word = saved_advance_word
    spelling_result = read_json(cli.paths(spelling_root, spelling_run)[1] / "batch-result.json")
    assert len(spelling_result["intraListRelations"]) == 1
    assert spelling_result["intraListRelations"][0]["verificationStatus"] == "automatic_passed"
    invalid_result = json.loads(json.dumps(spelling_result))
    invalid_result["intraListRelations"][0]["type"] = "synonym"
    try:
        cli.validate_json_schema(invalid_result, cli.HERE / "references" / "batch-result.schema.json")
        raise AssertionError("语义关系不得标记为自动通过")
    except ValueError:
        pass
    for source, target in (("quiet", "quite"), ("quite", "quiet")):
        source_entry = read_json(entry_records(spelling_root) / f"{stable_word_id(source)}.json")
        assert any(relation["type"] == "spelling_similar" and relation["targetWordId"] == stable_word_id(target)
                   and relation["verificationStatus"] == "automatic_passed"
                   and "sourceSenseId" not in relation for relation in source_entry["relationships"])
        rendered = cli.render_entry_markdown(source_entry, {"sourceCoverage": []})
        assert "词条级关系 · 拼写相似（可能易混）" in rendered
        assert "判据：最长公共子列相似度 ≥ 0.75" in rendered
    quiet_path = entry_records(spelling_root) / f"{stable_word_id('quiet')}.json"
    quiet_entry = read_json(quiet_path)
    invalid_spelling = json.loads(json.dumps(quiet_entry))
    invalid_spelling["relationships"][0]["targetLemma"] = "stone"
    try:
        cli.validate_entry(invalid_spelling, cli.HERE / "references" / "entry.schema.json")
        raise AssertionError("自动拼写相似关系低于阈值时不得通过词条校验")
    except ValueError:
        pass
    quiet_entry["aliases"].append(item("quite", confidence=1.00))
    try:
        cli.validate_entry(quiet_entry, cli.HERE / "references" / "entry.schema.json")
        raise AssertionError("别名与自动拼写相似关系不得并存")
    except ValueError:
        pass
    cli.write_text_atomic(quiet_path, cli.entry_json_text(quiet_entry))
    excluded_pair = cli._relation_candidates(spelling_root, [{"word": "quiet"}, {"word": "quite"}])
    assert not any("spelling_similarity" in pair["reasons"] for pair in excluded_pair)
    quiet_entry["aliases"].clear()
    quiet_entry["inflections"] = build_entry("quiet", decision(), evidence("quiet"))["inflections"]
    quiet_entry["inflections"][0]["form"]["text"] = "quite"
    cli.write_text_atomic(quiet_path, cli.entry_json_text(quiet_entry))
    excluded_pair = cli._relation_candidates(spelling_root, [{"word": "quiet"}, {"word": "quite"}])
    assert not any("spelling_similarity" in pair["reasons"] for pair in excluded_pair)
    print(json.dumps({"word": word_run, "batch": batch_run, "status": "ok"}))
