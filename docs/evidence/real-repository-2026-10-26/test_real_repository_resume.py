"""Public 1d10f754 regression: a failed action must not revive an older read on resume."""
import json
from pathlib import Path
import pytest
from app.runtime.model_client import ModelReply
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.store import db

class Script:
    def __init__(self, steps, observe=None):
        self.steps=iter(steps)
        self.calls=0
        self.observe=observe
    def chat(self, messages, **kwargs):
        self.calls+=1
        if self.observe and self.calls==2:
            self.observe.append(messages[-1]['content'])
        step=next(self.steps, {'tool':None,'args':{},'done':True})
        return ModelReply(json.dumps({'thought':'frozen reproduction','done':False,**step}),1,0,1)
    def close(self): pass

READ={'tool':'read_file','args':{'path':'worker.py'}}
RUN={'tool':'run_tests','args':{}}

def setup(tmp_path, first, budget):
    source=tmp_path/'fixture'; source.mkdir()
    (source/'worker.py').write_text('def value():\n    return 1\n',encoding='utf-8')
    (source/'test_worker.py').write_text('from worker import value\ndef test_value():\n    assert value()==1\n',encoding='utf-8')
    service=RunService(Settings(data_dir=tmp_path/'data',base_url='http://127.0.0.1:9/v1'),model_factory=lambda *_:first)
    run=service.create('t1_single_fix','preserve valid implementation and allow recovery',RunOptions(mode='local',token_budget=budget),source=source)
    return service,run

@pytest.mark.parametrize('tool',['write_file','edit_file'])
def test_resume_reads_current_file_after_last_action_failed(tmp_path,tool):
    args=({'path':'worker.py','content':'def broken(:'} if tool=='write_file'
          else {'path':'worker.py','old':'missing original code','new':'replacement'})
    first=Script([READ,{'tool':tool,'args':args}])
    service,run=setup(tmp_path,first,2)
    paused=service.execute(run['run_id'])
    assert paused['status']=='budget_paused'
    with service.database():
        before=db.committed_actions(run['run_id'])
    assert len(before)==2 and before[0]['result']['ok'] and not before[-1]['result']['ok']
    seen=['capture']
    recovery=Script([READ,RUN],seen)
    resumed=RunService(service.settings,model_factory=lambda *_:recovery)
    result=resumed.resume(run['run_id'],new_budget=20)
    assert result['status']=='done' and result['verified']
    assert seen[-1].startswith('[工具结果 read_file]'), 'A failed action must allow a fresh read after resume'
    with resumed.database():
        after=db.committed_actions(run['run_id'])
    assert [a['tool'] for a in after]==['read_file',tool,'read_file','run_tests']
    assert Path(result['workspace_path'],'worker.py').read_text(encoding='utf-8')=='def value():\n    return 1\n'

def test_resume_preserves_duplicate_guard_after_a_successful_last_read(tmp_path):
    service,run=setup(tmp_path,Script([READ]),1)
    assert service.execute(run['run_id'])['status']=='budget_paused'
    seen=['capture']; recovery=Script([READ,RUN],seen)
    result=RunService(service.settings,model_factory=lambda *_:recovery).resume(run['run_id'],new_budget=20)
    assert result['status']=='done' and result['verified']
    assert '连续两次 read_file' in seen[-1]
