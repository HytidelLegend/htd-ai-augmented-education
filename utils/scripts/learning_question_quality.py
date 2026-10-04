"""Question history checks independent of answer-option order."""
import re
from difflib import SequenceMatcher


def normalized(text):
    return re.sub(r'[\W_]+', '', text.casefold())


def compare(question, history):
    findings = []
    prompt = normalized(question['prompt'])
    options = {normalized(x) for x in question.get('options', [])[:4]}
    for item in history:
        prior = normalized(item['prompt'])
        option_match = bool(options) and options == {normalized(x) for x in item.get('options', [])[:4]}
        ratio = SequenceMatcher(None, prompt, prior).ratio()
        if prompt == prior or option_match or ratio >= .78:
            findings.append({'question_id': question['question_id'], 'history_id': item['history_id'],
                             'kind': 'duplicate' if prompt == prior or option_match else 'similar', 'similarity': ratio})
    return findings


def history(model, navigation):
    result = [{**q, 'history_id': 'diagnostic-' + str(i)}
              for i, q in enumerate(navigation.get('planning_profile', {}).get('assessment', {}).get('answers', []), 1)]
    for lesson in model['lessons']:
        for q in lesson.get('content', {}).get('questions', []):
            result.append({**q, 'history_id': lesson['lesson_id'] + '/' + q['question_id']})
    return result


def arrange_options(question, index):
    if question['type'] != 'multiple_choice': return
    correct = question['correct_index']; target = index % 4
    options = question['options']; answer = options[correct]
    other = [x for i, x in enumerate(options) if i != correct]; other.insert(target, answer)
    question['options'] = other; question['correct_index'] = target
    # Preserve explanations while updating an explicit leading answer letter.
    reference = question.get('reference_answer', '').strip()
    old = chr(65 + correct)
    if re.match(r'^' + old + r'(?:$|[.、\s：:])', reference):
        question['reference_answer'] = chr(65 + target) + reference[1:]
