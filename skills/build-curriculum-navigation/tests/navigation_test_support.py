"""Explicitly complete the new mandatory diagnostic gate in regression fixtures."""
from pathlib import Path
from utils.scripts.structured_io import read_json, write_json

def diagnose(runner, run):
    receipt = runner.diagnostic_command(run, 'diagnostic-start', level='初学者', goal='理解综合应用')
    while receipt['next_target']:
        q = {'unit_id': receipt['next_target']['unit_id'], 'prompt':'根据资料，哪项描述正确？',
             'options':['正确描述','错误描述一','错误描述二','错误描述三','没听过 / 不清楚 / 没把握'],'correct_index':0}
        path = run/'test-question.json'; write_json(path,q)
        runner.diagnostic_command(run,'diagnostic-question',question=path)
        receipt=runner.diagnostic_command(run,'diagnostic-answer',choice='0')
    return read_json(run/'ordering-decisions.template.json')['learner_context']

def finish_ordering(runner, run, receipt):
    if receipt['status'] != 'ordering_decision_required': return receipt
    diagnose(runner,run)
    path=run/'ordering-decisions.template.json'
    return runner.resolve_ordering(run,path)
