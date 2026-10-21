export function evidenceLabel(kind) {
  if(kind==='offline_demo')return '离线固定演示';
  if(kind==='historical_real')return '历史真实模型记录';
  throw Error('未知证据类型');
}
export function caseText(item) {return `${item.title}\n${evidenceLabel(item.evidence_kind)}\n${item.summary}`;}
