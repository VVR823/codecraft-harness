import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {createSelection, renderText, canResume, resumePayload} from '../state.mjs';

test('untrusted strings render as text',()=>{
  const node={set innerHTML(_){throw Error('HTML execution forbidden');}};
  renderText(node,'<img src=x onerror=alert(1)>');
  assert.equal(node.textContent,'<img src=x onerror=alert(1)>');
});

test('actual app reconnects packs after initial authentication failure',async()=>{
  const nodes=new Map();
  function node(){return {value:'',children:[],textContent:'',replaceChildren(...items){this.children=items;if(items[0]?.value)this.value=items[0].value;},append(...items){this.children.push(...items);},setAttribute(){}};}
  const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);},createElement:node};
  document.getElementById('mode').value='offline_demo';
  const calls=[];
  const fetch=async(path,options)=>{
    calls.push(path);
    if(!options.headers.Authorization)return {ok:false,status:401,json:async()=>({detail:'auth required'})};
    return {ok:true,json:async()=>path==='/api/packs'?{packs:[{task_id:'t1_single_fix',desc:'demo',offline_demo:true}]}:{runs:[]}};
  };
  const source=(await readFile(new URL('../app.js',import.meta.url),'utf8')).replace(/^import .*?;\s*/, '');
  await vm.runInNewContext(`(async()=>{${source}})()`,{document,fetch,setTimeout:()=>0,createSelection,renderText,canResume,resumePayload,console});
  document.getElementById('token').value='test-only-token';
  await document.getElementById('refresh').onclick();
  assert.equal(document.getElementById('pack').children.length,1);
  assert.equal(calls.filter(path=>path==='/api/packs').length,2);
});
test('late previous run response never replaces current run',async()=>{
  let resolveA;const seen=[];
  const select=createSelection(id=>id==='a'?new Promise(r=>resolveA=r):Promise.resolve({run_id:id}),r=>seen.push(r.run_id));
  const a=select.load('a');await select.load('b');resolveA({run_id:'a'});await a;
  assert.deepEqual(seen,['b']);
});
test('resume semantics and finite budget',()=>{
  for(const status of ['done','failed','interrupted','pending']) assert.equal(canResume({status}),false);
  assert.equal(canResume({status:'paused',legacy:true}),false);
  assert.equal(canResume({status:'paused',unknown_calls:1}),false);
  assert.equal(canResume({status:'paused'}),true);
  assert.deepEqual(resumePayload({status:'budget_paused',known_tokens:10},'20'),{new_budget:20});
  assert.throws(()=>resumePayload({status:'budget_paused',known_tokens:10},'10'));
  assert.throws(()=>resumePayload({status:'budget_paused',known_tokens:10},'Infinity'));
});

test('stale running record offers explicit recovery but active owner does not',()=>{
  assert.equal(canResume({status:'running',active:false,unknown_calls:0}),true);
  assert.equal(canResume({status:'running',active:true,unknown_calls:0}),false);
});
