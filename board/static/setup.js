"use strict";
const $ = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let config, constraints, watchlist, currentDraft, region = "CN";
const labels = {manual:"手动任务包",configured:"配置齐全",needs_login:"需要登录",missing_key:"缺少密钥",needs_model:"缺少模型 ID",connection_failed:"连接失败"};

function notice(text, error=false) { const box=$('notice'); box.hidden=false; box.textContent=text; box.classList.toggle('error',error); }
async function api(path, body, method='POST') {
  const options = {method, headers:{'X-CSRF-Token':csrf}};
  if (body instanceof FormData) options.body=body;
  else if (body !== undefined) {options.body=JSON.stringify(body); options.headers['Content-Type']='application/json';}
  const response=await fetch(path,options);
  const type=response.headers.get('content-type')||'';
  const result=type.includes('json')?await response.json():{};
  if (!response.ok) throw new Error(result.error||`操作失败（${response.status}），请刷新或检查设置。`);
  return result;
}
function action(id, handler) {$(id).addEventListener('click',async event=>{event.preventDefault();const button=event.currentTarget;button.disabled=true;try{await handler();}catch(error){notice(error.message,true);}finally{button.disabled=false;}});}
function tab() {const selected=['models','profile','sources','tasks'].includes(location.hash.slice(1))?location.hash.slice(1):'models';for(const id of ['models','profile','sources','tasks'])$(id).hidden=id!==selected;document.querySelectorAll('.setup-tabs a').forEach(link=>link.setAttribute('aria-current',link.hash==='#'+selected?'page':'false'));}
addEventListener('hashchange',tab);tab();

async function loadSettings() {
  const data=await api('/api/settings',undefined,'GET');config=data.config;
  $('agent-prompt').value=data.agent_prompt;
  $('model-status').textContent=labels[data.diagnosis.status]||data.diagnosis.status;
  $('diagnosis').textContent=data.diagnosis.message+'。'+data.diagnosis.next_step;
  const llm=config.llm;
  $('backend').value=llm.backend;$('model').value=llm.model;$('protocol').value=llm.protocol;$('base-url').value=llm.base_url;
  $('credential-kind').value=llm.credential.kind;$('credential-name').value=llm.credential.name;
  $('max-jobs').value=config.budget.max_jobs;$('browser-profile').value=config.browser.profile;
}
action('copy-prompt',async()=>{
  try {await navigator.clipboard.writeText($('agent-prompt').value);$('copy-state').textContent='已复制，交给你正在使用的 Agent 即可';}
  catch(_) {$('agent-prompt').closest('details').open=true;$('agent-prompt').focus();$('agent-prompt').select();$('copy-state').textContent='请按 Ctrl+C / Command+C 复制已选内容';}
});
$('provider').addEventListener('change',()=>{
  const presets={deepseek:['chat_completions','https://api.deepseek.com'],openai:['responses','https://api.openai.com/v1'],anthropic:['anthropic','https://api.anthropic.com']};
  const preset=presets[$('provider').value];if(preset){$('backend').value='api';$('protocol').value=preset[0];$('base-url').value=preset[1];}
});
$('credential-kind').addEventListener('change',()=>{
  if($('credential-kind').value==='keyring'&&!$('credential-name').value.includes(':'))$('credential-name').value='job-pipeline:api-key';
  if($('credential-kind').value==='env'&&$('credential-name').value.includes(':'))$('credential-name').value='JP_LLM_API_KEY';
});
async function saveModel(test) {
  const candidate=structuredClone(config),llm=candidate.llm;
  llm.backend=$('backend').value;llm.model=$('model').value.trim();llm.protocol=$('protocol').value;llm.base_url=$('base-url').value.trim();
  llm.credential={kind:$('credential-kind').value,name:$('credential-name').value.trim()};
  const body={config:candidate,test_model:test&&!['manual','claude'].includes(llm.backend)};
  if($('session-key').value)body.session_key=$('session-key').value;
  const saved=await api('/api/settings',body);$('session-key').value='';await loadSettings();
  if(saved.result.tested){$('model-status').textContent='最小测试通过';$('diagnosis').textContent='本次模型请求和 JSON 校验通过。实际岗位分析仍会逐包验证。';}
  notice(saved.result.tested?'配置已保存，最小连接测试通过。':test?'手动模式配置已保存，无需模型调用。':'配置已保存，尚未测试模型连接。');
}
$('model-form').addEventListener('submit',async event=>{event.preventDefault();try{await saveModel(true);}catch(error){notice(error.message,true);}});
action('save-model-only',()=>saveModel(false));

function element(tag,text,className) {const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
async function loadProfile() {
  const data=await api('/api/profile',undefined,'GET');currentDraft=data.draft;
  $('fact-count').textContent=`已确认 ${data.facts.length} 条经历`;
  $('confirmed-facts').textContent=data.facts.map(item=>`${item.title}：${item.text}`).join('\n')||'尚无已确认经历。';
  if(currentDraft)$('resume-text').value=currentDraft.text;
  const container=$('draft-items');container.replaceChildren();
  for(const item of currentDraft?.items||[]) {
    const card=element('div',undefined,'fact-item');card.dataset.id=item.id;
    const label=element('label',undefined,'check-line'),checkbox=element('input');checkbox.type='checkbox';checkbox.checked=!item.inferred;checkbox.disabled=item.inferred;
    label.append(checkbox,element('span',item.inferred?'推断条目（不能用于匹配）':'确认这条经历'));card.append(label);
    const title=element('input');title.value=item.title;title.className='fact-title';title.setAttribute('aria-label','经历标题');
    const text=element('textarea');text.value=item.text;text.className='fact-text';text.rows=3;text.setAttribute('aria-label','经历内容');
    card.append(element('label','标题'),title,element('label','内容'),text,element('blockquote','原文依据：'+item.source_quote));container.append(card);
  }
  $('confirm-facts').hidden=!(currentDraft?.items?.length);
}
$('upload-form').addEventListener('submit',async event=>{event.preventDefault();try{const file=$('resume-file').files[0];if(!file)throw new Error('请先选择文件。');const body=new FormData();body.append('file',file);await api('/api/profile',body);$('resume-file').value='';await loadProfile();notice('已在本地提取和遮盖常见敏感字段，请检查预览。');}catch(error){notice(error.message,true);}});
action('save-preview',async()=>{await api('/api/profile',{text:$('resume-text').value});await loadProfile();notice('已保存本地脱敏预览，未调用模型。');});
action('generate-draft',async()=>{await api('/api/profile',{text:$('resume-text').value});await loadProfile();await api('/api/tasks',{kind:'resume_generate'});notice('经历草稿任务已启动，请在运行页面查看；完成后返回这里检查条目。');location.hash='tasks';await loadTasks();});
action('collect-draft',async()=>{await api('/api/profile/collect',{});await loadProfile();notice('手动任务包已校验，确认后用于匹配。');});
action('confirm-facts',async()=>{const items=[...document.querySelectorAll('.fact-item')].map(card=>({id:card.dataset.id,confirmed:card.querySelector('input[type=checkbox]').checked,title:card.querySelector('.fact-title').value,text:card.querySelector('.fact-text').value}));const result=await api('/api/profile/confirm',{id:currentDraft.id,items});await loadProfile();notice(`已确认 ${result.accepted} 条经历。下一步设置岗位来源。`);});

function sourceFields() {
  const enabled=config.sources[region],allowed=region==='CN'?['boss','nowcoder','watchlist']:['linkedin','jobsdb','watchlist'];
  document.querySelectorAll('.source-options input').forEach(input=>{input.disabled=!allowed.includes(input.value);input.checked=enabled.includes(input.value);});
  const section=config.fetch[region];let example=Object.values(section).find(value=>value?.keywords||Array.isArray(value));if(Array.isArray(example))example=example[0];
  $('search-city').value=example?.city||'';$('search-keywords').value=(example?.keywords||[]).join(', ');
  $('fetch-json').value=JSON.stringify(section,null,2);$('advanced-fetch').checked=false;
  const condition=constraints[region];$('allowed-cities').value=condition.unrestricted_locations?'':condition.locations.join(', ');
  $('max-days').value=condition.max_days_per_week;$('max-months').value=condition.max_min_months;$('negotiable').checked=!!condition.days_negotiable;
  $('class-year').value=(condition.campus?.class_years||[])[0]||'';
}
async function loadSources(){const data=await api('/api/sources',undefined,'GET');constraints=data.constraints;watchlist=data.watchlist;$('watchlist-json').value=JSON.stringify(watchlist,null,2);sourceFields();}
$('search-region').addEventListener('change',()=>{region=$('search-region').value;sourceFields();});
function comma(value){return value.split(/[,，]/).map(x=>x.trim()).filter(Boolean);}
$('sources-form').addEventListener('submit',async event=>{
  event.preventDefault();try{
    const candidate=structuredClone(config),nextConstraints=structuredClone(constraints);
    candidate.sources[region]=[...document.querySelectorAll('.source-options input:checked')].map(input=>input.value);
    candidate.browser.profile=$('browser-profile').value.trim();
    if($('advanced-fetch').checked)candidate.fetch[region]=JSON.parse($('fetch-json').value);
    else for(const source of candidate.sources[region].filter(source=>source!=='watchlist'))candidate.fetch[region][source]={keywords:comma($('search-keywords').value),city:$('search-city').value.trim(),max_pages:2,page_size:15,detail_limit:30,extra:{}};
    const condition=nextConstraints[region];condition.locations=comma($('allowed-cities').value);condition.unrestricted_locations=condition.locations.length===0;
    condition.max_days_per_week=Number($('max-days').value);condition.max_min_months=Number($('max-months').value);condition.days_negotiable=$('negotiable').checked;
    condition.campus={...(condition.campus||{}),class_years:$('class-year').value?[Number($('class-year').value)]:[]};
    await api('/api/sources',{sources:candidate.sources,fetch:candidate.fetch,browser:candidate.browser,budget:candidate.budget,constraints:nextConstraints,watchlist:JSON.parse($('watchlist-json').value)});
    await loadSettings();await loadSources();notice('来源和求职条件已保存，未启动采集。');
  }catch(error){notice(error.message,true);}
});
action('check-sources',async()=>{const data=await api('/api/sources/check',{});$('source-status').hidden=false;$('source-status').textContent=data.checks.filter(row=>row.status!=='disabled').map(row=>`${row.region} / ${row.source}：${row.status} · ${row.message}`).join('\n')||'尚未启用任何来源。';});

async function loadTasks(){
  const data=await api('/api/tasks',undefined,'GET');const table=$('task-rows');table.replaceChildren();$('no-tasks').hidden=data.tasks.length>0;
  const names={done:'完成',running:'运行中',failed:'失败',stopped:'已停止',interrupted:'已中断',waiting_manual:'待手动填包'};
  for(const task of data.tasks){const row=element('tr');row.append(element('td',task.kind),element('td',names[task.status]||task.status),element('td',task.stage+' '+JSON.stringify(task.result)));const cell=element('td',undefined,'task-actions');
    if(task.status==='running'){const button=element('button','停止');button.onclick=async()=>{try{const result=await api(`/api/tasks/${task.id}/stop`,{});notice(result.message);await loadTasks();}catch(error){notice(error.message,true);}};cell.append(button);}
    if(['interrupted','stopped','failed','waiting_manual'].includes(task.status)){const button=element('button','恢复');button.onclick=async()=>{try{await api(`/api/tasks/${task.id}/resume`,{});await loadTasks();}catch(error){notice(error.message,true);}};cell.append(button);}
    row.append(cell);table.append(row);
  }
}
document.querySelectorAll('[data-run]').forEach(button=>button.addEventListener('click',async()=>{try{await api('/api/tasks',{kind:button.dataset.run,options:{region:$('run-region').value,max_jobs:Number($('max-jobs').value)}});notice('任务已启动。');await loadTasks();}catch(error){notice(error.message,true);}}));
action('demo',async()=>{const data=await api('/api/demo',{});notice(`已导入 ${data.new} 个虚构演示岗位，没有调用模型。点击“查看岗位看板”体验。`);});
async function init(){try{await loadSettings();await loadProfile();await loadSources();await loadTasks();}catch(error){notice(error.message,true);}}
init();
setInterval(async()=>{if(location.hash==='#tasks'){try{await loadTasks();}catch(error){notice(error.message,true);}}},2500);
document.querySelector('.setup-tabs a[href="#profile"]').addEventListener('click',()=>loadProfile().catch(error=>notice(error.message,true)));
