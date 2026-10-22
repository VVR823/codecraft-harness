import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {createSelection, renderText, canResume, resumePayload} from '../state.mjs';

function deferred() {
  let resolve;
  return {promise:new Promise(r=>resolve=r),resolve:value=>resolve(value)};
}

async function app({waitForB, waitForResult}={}) {
  const nodes=new Map(), calls=[], downloads=[];
  function node(){return {value:'',children:[],textContent:'',dataset:{},disabled:false,
    replaceChildren(...items){this.children=items;if(items[0]?.value)this.value=items[0].value;},
    append(...items){this.children.push(...items);},setAttribute(){},click(){downloads.push(this.download);}};}
  const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);},createElement:node};
  document.getElementById('mode').value='offline_demo';
  const run=id=>({run_id:id,task_id:'t1_single_fix',mode:'offline_demo',status:'budget_paused',
    options:{token_budget:20},known_tokens:20,steps:2,unknown_calls:0,approvals:[],active:false});
  const fetch=async(path,options)=>{
    calls.push({path,method:options.method});
    let value;
    if(path==='/api/packs')value={packs:[{task_id:'t1_single_fix',desc:'demo',offline_demo:true}]};
    else if(path==='/api/runs')value={runs:[run('a'),run('b')]};
    else if(path.includes('/traces?'))value={traces:[],total:0};
    else if(path.endsWith('/result'))value=waitForResult?await waitForResult.promise:{run_id:'a'};
    else if(path==='/api/tasks/b'&&waitForB)value=await waitForB.promise;
    else value=run(path.includes('/tasks/a')?'a':'b');
    return {ok:true,status:200,json:async()=>value};
  };
  const source=(await readFile(new URL('../app.js',import.meta.url),'utf8')).replace(/^import .*?;\s*/, '');
  await vm.runInNewContext(`(async()=>{${source}})()`,{document,fetch,setTimeout:()=>0,
    createSelection,renderText,canResume,resumePayload,console,Blob,URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}}});
  return {document,calls,downloads,run,choose:index=>document.getElementById('runs').children[index].onclick()};
}

test('switching tasks cannot resume the new selection using stale task details',async()=>{
  const waitForB=deferred();
  const ui=await app({waitForB});
  await ui.choose(0);
  const selection=ui.choose(1);
  const resuming=ui.document.getElementById('resume').onclick();
  await Promise.resolve();
  const unsafePosts=ui.calls.filter(c=>c.method==='POST');
  waitForB.resolve(ui.run('b'));
  await Promise.allSettled([selection,resuming]);
  assert.deepEqual(unsafePosts,[]);
});

test('switching selection during download retains the requested run filename',async()=>{
  const waitForResult=deferred();
  const ui=await app({waitForResult});
  await ui.choose(0);
  const download=ui.document.getElementById('download').onclick();
  await ui.choose(1);
  waitForResult.resolve({run_id:'a'});
  await download;
  assert.deepEqual(ui.downloads,['codecraft-a.json']);
});
