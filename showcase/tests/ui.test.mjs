import {test} from 'node:test';
import assert from 'node:assert/strict';
import {evidenceLabel, caseText} from '../view.mjs';
test('case evidence and untrusted HTML remain literal',()=>{
  assert.equal(evidenceLabel('offline_demo'),'离线固定演示');
  assert.throws(()=>evidenceLabel('real'));
  assert.match(caseText({title:'<script>alert(1)</script>',evidence_kind:'offline_demo',summary:'sample'}),/<script>/);
});
