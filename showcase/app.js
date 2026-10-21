import {evidenceLabel} from './view.mjs';
const root=document.getElementById('cases');
function el(tag,text){const node=document.createElement(tag);node.textContent=String(text??'');return node;}
try{
  const response=await fetch('./cases.json');if(!response.ok)throw Error('案例加载失败');
  const data=await response.json();
  document.getElementById('version').textContent=`版本 ${data.git_revision} · ${data.evidence_note}`;
  for(const item of data.cases){
    const card=el('article','');card.append(el('p',evidenceLabel(item.evidence_kind)),el('h2',item.title),el('p',item.summary));
    card.append(el('h3','执行时间线'));
    const list=el('ol','');for(const step of item.timeline)list.append(el('li',step));card.append(list);
    card.append(el('h3','修改片段'),el('pre',item.patch),el('h3','验证结果'),el('p',item.tests));root.append(card);
  }
}catch(e){root.textContent=e.message;}
