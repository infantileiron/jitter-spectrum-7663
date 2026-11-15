import {useEffect,useState} from 'react';import type {Workspace} from '../stores/workspace';import type {Batch,BatchResult} from '../types/domain';import {PageHeading} from '../components/common/PageHeading';import {Notice} from '../components/common/Notice';import {Empty} from '../components/common/Empty';import {create,detail,limits,retry} from '../api/significance';import type {BatchLimits} from '../api/significance';import {useAsync} from '../hooks/useAsync';import {CALIBRATION_PHASE,DETRENDS,SIGNIFICANCE_STATUS,WINDOWS} from '../constants/enums';import {number,date} from '../utils/format';

const TERMINAL=['available','failed'];

function BatchStatus({value}:{value:string}){return <span className={'status '+value}>{SIGNIFICANCE_STATUS[value]||value}</span>}
function Track({percent,inline=false}:{percent:number;inline?:boolean}){return <div className={'batch-progress-track'+(inline?' inline':'')}><i style={{width:Math.max(0,Math.min(100,percent||0))+'%'}}/></div>}

function Progress({batch}:{batch:Batch}){
 const p=batch.progress;
 return <div className="batch-progress">
  <Track percent={p?.percent||0}/>
  <div className="batch-progress-meta">
   <span>{CALIBRATION_PHASE[p?.phase||'queued']||p?.phase}</span>
   <span>窗口 {p?.items_done??0}/{p?.items_total??0} · 模拟 {p?.simulations_done??0}/{p?.simulations_total??batch.simulations}</span>
   <span>{number(p?.percent,1)}%</span>
  </div>
  {p?.current_window&&<small>当前窗口：{p.current_window}</small>}
 </div>
}

function Results({result,go}:{result:BatchResult;go:(s:string)=>void}){
 const b=result.batch_max;
 return <div className="batch-results">
  <div className="result-metrics">
   <div><small>模拟次数</small><b>{result.simulations}</b></div>
   <div><small>概率分辨率 1/(N+1)</small><b>{number(result.resolution,4)}</b></div>
   <div><small>随机种子</small><b>{result.seed}</b></div>
   <div><small>相关时长 / 分钟</small><b>{number(result.correlation_time_minutes,2)}</b></div>
  </div>
  <p className="caption">检验统计量：{result.statistic}。噪声模型：{result.correlation_model}。参考水平 α={number(result.reference_alpha,3)}，对应整批最大峰阈值 {b.threshold_at_reference_alpha==null?'不可达（模拟次数不足 1/α−1，不降口径）':number(b.threshold_at_reference_alpha,4)}（中位 {number(b.median,4)} / P90 {number(b.p90,4)} / P95 {number(b.p95,4)} / P99 {number(b.p99,4)} / 最大 {number(b.max,4)}）。</p>
  <div className="table-scroll"><table>
   <thead><tr><th>窗口 / 研究</th><th>观测最大峰</th><th>对应周期 / 分钟</th><th>整批最大峰 ≥ 观测值</th><th>误报概率</th><th>相对参考水平</th><th>原双谱</th></tr></thead>
   <tbody>{result.items.map(item=><tr key={item.study_id}>
    <td><b>{item.window_name}</b><small>{item.study_name} · {item.link_id} · {DETRENDS[item.detrend]} / {WINDOWS[item.fft_window]}</small></td>
    <td>{number(item.observed_max_power,4)}</td>
    <td>{number(item.peak_period_minutes,2)}<small>f={number(item.peak_frequency_per_minute,6)}/min</small></td>
    <td>{item.exceedances} / {item.simulations}</td>
    <td><b>{number(item.false_alarm_probability,4)}</b><small>每档 {number(item.resolution,4)}</small></td>
    <td><span className={'status '+(item.significant_at_reference_alpha?'completed':'failed')}>{b.threshold_at_reference_alpha==null?'α阈值不可达':item.significant_at_reference_alpha?'低于α，未超噪声期望':'未低于α'}</span><small>观测峰 {number(item.observed_max_power,3)} vs 阈值 {b.threshold_at_reference_alpha==null?'—':number(b.threshold_at_reference_alpha,3)}</small></td>
    <td><button className="text-button" onClick={()=>go('results?study='+encodeURIComponent(item.study_id))}>查看双谱　↗</button></td>
   </tr>)}</tbody>
  </table></div>
  <p className="caption">误报概率 = (1 + 整批最大峰 ≥ 本窗观测最大峰的模拟次数) / (模拟次数 + 1)。这是整批族校准，同批窗口的概率彼此相关，不能把单窗概率当作独立检验或因果结论。</p>
  {!!result.groups.length&&<p className="caption">共享观测：{result.groups.map(g=>`${g.link_id}（${g.item_count}窗共用${g.shared_observation_count}个采样时刻）`).join('；')}。重叠窗口在同一时刻使用同一条模拟噪声。</p>}
  <p className="caption">输入校验码 {result.input_checksum.slice(0,24)} · 结果校验码 {result.result_checksum.slice(0,24)}。同批同种子重跑或调换窗口顺序，结果逐字段一致。</p>
 </div>
}

export function Significance({data,canWrite,refresh,go}:{data:Workspace;canWrite:boolean;refresh:()=>Promise<void>;go:(s:string)=>void}){
 const [selected,setSelected]=useState(''),[name,setName]=useState(''),[studyIds,setStudyIds]=useState<string[]>([]),[simulations,setSimulations]=useState(300),[seed,setSeed]=useState(2307),[tau,setTau]=useState(5);
 const [loaded,setLoaded]=useState<Batch|null>(null),[loadError,setLoadError]=useState('');
 const [bounds,setBounds]=useState<BatchLimits|null>(null);
 const task=useAsync();
 const active=data.batches.find(b=>b.id===selected)||data.batches[0];
 const activeId=active?.id||'',activeStatus=active?.status||'';
 useEffect(()=>{let alive=true;limits().then(l=>{if(alive)setBounds(l)}).catch(()=>{});return()=>{alive=false}},[]);
 useEffect(()=>{
  if(!activeId){setLoaded(null);return}
  let cancelled=false, timer:ReturnType<typeof setInterval>|undefined;
  const pull=async()=>{try{const next=await detail(activeId);if(!cancelled){setLoaded(next);setLoadError('')}}catch(e){if(!cancelled)setLoadError((e as Error).message)}};
  pull();
  if(!TERMINAL.includes(activeStatus))timer=setInterval(pull,1500);
  return()=>{cancelled=true;if(timer)clearInterval(timer)}
 },[activeId,activeStatus]);
 const estimate=studyIds.reduce((sum,id)=>{const study=data.studies.find(s=>s.id===id),window=data.windows.find(w=>w.id===study?.window_id);return sum+(study&&window?study.frequency_points*window.point_count:0)},0)*simulations;
 const batch=loaded&&loaded.id===activeId?loaded:active;
 const result=batch?.status==='available'?(batch.result as BatchResult):null;

 return <>
  <PageHeading label="07 / BATCH SIGNIFICANCE" title="逐窗挑峰，噪声高峰也会误报。">冻结所选窗口的观测与搜索范围，按原采样时刻模拟无周期噪声，<br/>每次重做去趋势与全频搜索，以整批最大峰校准各窗误报概率。</PageHeading>
  {canWrite&&<section className="paper">
   <h2>发起批量显著性校准</h2>
   <form className="grid-form" onSubmit={e=>{e.preventDefault();task.execute(async()=>{const next=await create({name,study_ids:studyIds,simulations,seed,correlation_time_minutes:tau});setName('');await refresh();setSelected(next.id)})}}>
    <label>批次名称<input aria-label="批次名称" required minLength={2} value={name} onChange={e=>setName(e.target.value)}/></label>
    <div className="coverage-note">已选 {studyIds.length} 窗 · 预估评估量 {estimate.toLocaleString('zh-CN')}<br/>上限 {bounds?bounds.max_evaluations.toLocaleString('zh-CN'):'—'}（模拟次数 × Σ观测点数 × LS网格点数）；最多{bounds?.max_items??12}窗、{(bounds?.max_simulations??2000).toLocaleString('zh-CN')}次模拟</div>
    <label className="wide">所选窗口（可多选，共{data.studies.length}项）{data.studies.length?<select aria-label="选择频谱研究窗口" multiple size={Math.min(6,Math.max(2,data.studies.length))} value={studyIds} onChange={e=>setStudyIds([...e.target.selectedOptions].map(o=>o.value))}>{data.studies.map(s=><option key={s.id} value={s.id}>{s.name} · {data.windows.find(w=>w.id===s.window_id)?.name||'未知窗口'}</option>)}</select>:<span className="coverage-note">请先在“频谱设置”保存研究。</span>}</label>
    <label>模拟次数<input aria-label="模拟次数" type="number" min={1} max={bounds?.max_simulations??2000} value={simulations} onChange={e=>setSimulations(+e.target.value)}/></label>
    <label>随机种子<input aria-label="随机种子" type="number" min={0} max={2147483647} value={seed} onChange={e=>setSeed(+e.target.value)}/></label>
    <label>相关时长 / 分钟<input aria-label="相关时长" type="number" min={0.000001} step="any" value={tau} onChange={e=>setTau(+e.target.value)}/></label>
    {task.error&&<Notice error>{task.error}</Notice>}
    <button disabled={task.busy||!studyIds.length}>发起批次校准</button>
   </form>
   <p className="caption">创建时冻结所选窗口的实际采样时刻、观测值、去趋势方式、频率范围与网格点数；之后的观测集或研究修改不影响已提交批次。种子按“批次种子 + 观测集 + 链路 + 模拟序号”派生，与窗口提交顺序无关。容量超限会明确拒绝，不会减少模拟次数冒充完成。</p>
  </section>}
  <section className="paper">
   <div className="section-heading"><h2>校准批次</h2><span>全部窗口校准完成后才一次性切换为可用</span></div>
   {!data.batches.length?<Empty>还没有显著性批次。选择窗口并声明模拟次数、种子与相关时长后发起。</Empty>:
    <div className="table-scroll"><table>
     <thead><tr><th>批次</th><th>状态</th><th>模拟 / 种子 / 相关时长</th><th>进度</th><th>提交时间</th></tr></thead>
     <tbody>{data.batches.map(b=><tr key={b.id}>
      <td><b>{b.name}</b><small>{b.item_count??0}窗 · {b.simulations}次模拟 · {b.input_checksum.slice(0,12)}</small></td>
      <td><BatchStatus value={b.status}/>{b.status==='running'&&<small>{CALIBRATION_PHASE[b.progress?.phase||'']||''}</small>}</td>
      <td>{b.simulations} / {b.seed} / {number(b.correlation_time_minutes,2)}min</td>
      <td><Track percent={b.progress?.percent||0} inline/><small>{number(b.progress?.percent,1)}%</small></td>
      <td><button className="text-button" onClick={()=>setSelected(b.id)}>查看</button><small>{date(b.created_at)}</small></td>
     </tr>)}</tbody>
    </table></div>}
  </section>
  {loadError&&<Notice error>{loadError}</Notice>}
  {batch&&<section className="paper">
   <div className="section-heading"><h2>{batch.name}</h2><span>创建者 {batch.created_by} · {date(batch.created_at)}</span></div>
   <div className="batch-headline"><BatchStatus value={batch.status}/><small>输入校验码 {batch.input_checksum.slice(0,24)} · 预估评估量 {batch.total_evaluations?.toLocaleString('zh-CN')}</small></div>
   <Progress batch={batch}/>
   {batch.status!=='available'&&<Notice error={batch.status==='failed'}>{batch.status==='failed'?<><b>整批不给出显著结论。</b>{batch.error||'存在失败窗口。'}</>:'校准进行中：全部窗口完成后误报概率才同时可用，期间不提供部分结果。'}</Notice>}
   {batch.status==='failed'&&canWrite&&<button className="secondary" disabled={task.busy} onClick={()=>task.execute(async()=>{await retry(batch.id);await refresh()})}>按原冻结输入重跑</button>}
   {!!batch.frozen_inputs?.length&&<details className="audit-details" open><summary>冻结的观测与搜索范围</summary><div className="table-scroll"><table>
    <thead><tr><th>研究 / 窗口</th><th>链路</th><th>去趋势 / 窗</th><th>频率范围 / 每分钟</th><th>网格点数</th><th>观测点 / 跨度</th></tr></thead>
    <tbody>{batch.frozen_inputs.map(f=><tr key={f.study_id}>
     <td><b>{f.study_name}</b><small>{f.window_name}</small></td><td>{f.link_id}</td>
     <td>{DETRENDS[f.detrend]||f.detrend} / {WINDOWS[f.fft_window]||f.fft_window}</td>
     <td>{number(f.min_frequency,5)}—{number(f.max_frequency,5)}</td><td>{f.frequency_points}</td>
     <td>{f.observation_count}点 / {number(f.span_minutes,2)}min</td>
    </tr>)}</tbody>
   </table></div></details>}
   {result&&<Results result={result} go={go}/>}
   {result&&<aside className="assumptions"><h2>解释边界</h2>{result.assumptions.map((a,i)=><p key={a}><b>0{i+1}</b>{a}</p>)}<small>结果校验码 {result.result_checksum}</small></aside>}
  </section>}
 </>
}
