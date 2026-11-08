/* Same-origin bridge to the local C++ process; no simulated progress or results. */
'use strict';
let nativeSession=null, nativeBusy=false, pickerContext=null, backendApi=null;

function engineMessage(message){
 const translations=[
  ['Source directory does not exist','源目录不存在，请检查路径。'],
  ['must not contain each other','源目录和备份位置不能相同或互相包含。'],
  ['new or empty directory','还原目标必须是新目录或空目录，不会覆盖已有文件。'],
  ['checksum mismatch','备份内容校验失败，文件可能已损坏。'],
  ['Symbolic links','基础版暂不支持符号链接，请选择普通文件和目录。'],
  ['absolute directory path','请填写本机绝对路径，或点击“浏览”选择目录。'],
  ['changed during','文件在操作过程中发生变化，本次操作未完成，请重试。'],
  ['Permission denied','没有访问目录或文件的权限。'],
  ['Cannot read file','无法读取文件，请检查访问权限和文件是否仍存在。'],
  ['Cannot write file','无法写入文件，请检查目标目录的权限和可用空间。'],
  ['Write failed','写入失败，请检查目标磁盘是否已满或已断开。'],
  ['manifest','备份清单无效、损坏或不受支持。'],
  ['Wrong password','密码错误，或文件已被篡改。'],
  ['tampered','密码错误，或文件已被篡改。'],
  ['secretRef','短期密码引用无效或已过期，请重新输入密码。'],
  ['already exists','目标已存在，请换一个新路径。'],
  ['new or empty directory','目标必须是新目录或空目录，不会覆盖已有文件。'],
  ['maximum','内容超过允许的输出上限。'],
  ['cron','定时表达式无效，请使用 5 个字段：分 时 日 月 周。'],
 ];
 return translations.find(([key])=>message.includes(key))?.[1]||message;
}
async function nativeRequest(operationId,options={}){
 if(!backendApi)throw Error('本机引擎未连接，请先运行 python3 server.py，再刷新页面。');
 try{return await backendApi.invoke(operationId,options)}catch(error){error.message=engineMessage(error.message);throw error}
}
async function connectNative(){
 try{
  const response=await fetch('/api/session',{cache:'no-store'});
  if(!response.ok)return;
  const session=await response.json();
  if(!['C++17','Python'].includes(session.engine))return;
  const port=new BackNestBackend({token:session.token});
  const capabilities=await port.invoke('system.capabilities');
  backendApi=port;nativeSession={...session,capabilities};
  render();
  const pending=state.tasks.find(task=>task.activeJob);
  if(pending){
   nativeBusy=true;
   try{await completeBackupJob(pending,pending.activeJob)}finally{nativeBusy=false;render()}
  }
 }catch{/* Static UI can still open; native actions explain how to start the engine. */}
}
function localTime(iso){return new Date(iso||Date.now()).toLocaleString('zh-CN',{hour12:false})}
async function pollNativeJob(id,onProgress){
 for(;;){
  const job=await nativeRequest('jobs.get',{path:{jobId:id}});
  if(job.state==='failed')throw Error(engineMessage(job.error||'文件操作失败。'));
  if(job.state==='completed')return job.result;
  onProgress?.(job);
  await new Promise(resolve=>setTimeout(resolve,250));
 }
}
async function completeBackupJob(task,jobId){
 try{
  const result=await pollNativeJob(jobId,job=>{
   task.status='running';
   task.phase=job.phase;
   task.progress=job.totalBytes?Math.min(99,Math.floor(job.bytes/job.totalBytes*100)):0;
   task.processedFiles=job.files;
   render();
  });
  const version={id:result.path,path:result.path,label:localTime(result.completedAt),size:formatSize(result.bytes),bytes:result.bytes,files:result.files,directories:result.directories,verified:true};
  if(!task.versions.some(item=>item.path===version.path))task.versions.unshift(version);
  task.status='done';task.progress=100;task.time=version.label;task.files=result.files;task.size=version.size;task.sourceScanned=true;
  log(`${task.name} · 备份完成`,`${result.files} 个文件 · ${result.directories} 个目录 · ${version.size} · 校验通过`,'success');
  toast('备份完成，文件校验通过。');
 }catch(error){
  task.status='failed';task.lastError=error.message;
  log(`${task.name} · 备份失败`,error.message,'warning');toast(error.message);
 }finally{
  delete task.activeJob;persist();render();
 }
}
async function performBackup(id){
 const task=getTask(id);if(!task)return;
 if(nativeBusy){toast('已有文件操作正在运行，请等待完成。');return}
 if(task.type!=='完整备份'||task.compression||task.encryption){toast('基础版仅支持未压缩、未加密的完整备份，请新建基础备份任务。');return}
 nativeBusy=true;render();
 try{
  const job=await nativeRequest('backup.start',{body:{source:task.path,destination:task.target}});
  task.activeJob=job.id;task.status='running';delete task.lastError;persist();
  await completeBackupJob(task,job.id);
 }catch(error){toast(error.message)}finally{nativeBusy=false;render()}
}
async function performRestore(id,index){
 if(nativeBusy){toast('已有文件操作正在运行，请等待完成。');return}
 const task=getTask(id),version=task?.versions[index],destination=document.getElementById('restore-target').value.trim();
 if(!version?.path)return;
 if(!destination){document.getElementById('restore-error').textContent='请填写还原目录。';return}
 nativeBusy=true;
 try{
  const job=await nativeRequest('restore.start',{body:{snapshot:version.path,destination}});
  openModal(`${modalHead('正在还原')}<div class="modal-body"><p class="subtext">先校验备份内容，再写入目标目录。请等待操作结果。</p><div class="progress-track"><span id="restore-progress" style="width:0%"></span></div><p class="subtext" id="restore-progress-label">正在校验…</p></div>`);
  const result=await pollNativeJob(job.id,progress=>{
   const bar=document.getElementById('restore-progress'),label=document.getElementById('restore-progress-label');
   if(bar)bar.style.width=`${progress.totalBytes?Math.min(99,Math.round(progress.bytes/progress.totalBytes*100)):0}%`;
   if(label)label.textContent=`${progress.phase==='verifying'?'校验':'复制'}：${progress.files} / ${progress.totalFiles} 个文件`;
  });
  log(`${task.name} · 还原完成`,`${result.files} 个文件 · 校验通过 · ${result.path}`,'success');persist();
  openModal(`${modalHead('还原完成')}<div class="modal-body"><div class="notice">${icon('check')}已还原 ${result.files} 个文件、${result.directories} 个目录，内容校验通过。</div><div class="review"><div class="review-row"><span>还原位置</span><strong>${esc(result.path)}</strong></div></div></div><div class="modal-footer"><span></span><button class="btn primary" data-action="close">完成</button></div>`);
 }catch(error){
  log(`${task?.name||'文件'} · 还原失败`,error.message,'warning');persist();
  openModal(`${modalHead('还原失败')}<div class="modal-body"><p class="error">${esc(error.message)}</p><p class="subtext">已有文件不会被覆盖。</p></div><div class="modal-footer"><span></span><button class="btn" data-action="close">关闭</button></div>`);
 }finally{nativeBusy=false;render()}
}
async function importBackup(){
 if(nativeBusy)return;
 const input=document.getElementById('import-path');
 const snapshot=input.value.trim();if(!snapshot){toast('请输入已有备份版本目录。');return}
 nativeBusy=true;
 try{
  const job=await nativeRequest('snapshots.verify',{body:{snapshot}});
  toast('正在检查备份内容…');
  const result=await pollNativeJob(job.id);
  let task=state.tasks.find(t=>t.versions.some(v=>v.path===result.path));
  if(!task){task={id:`import-${Date.now()}`,name:result.source.split(/[\\/]/).filter(Boolean).at(-1)||'已导入备份',path:result.source,target:result.path.replace(/[\\/][^\\/]+$/,''),type:'完整备份',compression:false,encryption:false,schedule:'手动备份',enabled:false,versions:[],status:'done',color:''};state.tasks.unshift(task)}
  if(!task.versions.some(v=>v.path===result.path))task.versions.push({id:result.path,path:result.path,label:`导入于 ${localTime(result.completedAt)}`,files:result.files,directories:result.directories,bytes:result.bytes,size:formatSize(result.bytes),verified:true});
  task.files=result.files;task.size=formatSize(result.bytes);task.time=localTime(result.completedAt);task.status='done';selected=task.id;
  log(`${task.name} · 已导入备份`,`${result.path} · 内容校验通过`,'success');persist();closeModal();navigate('restore');toast('备份已校验并导入。');
 }catch(error){toast(error.message)}finally{nativeBusy=false;render()}
}
async function openDirectoryPicker(kind,id,index){
 if(!nativeSession){toast('请先运行 python3 server.py 连接本机目录。');return}
 if(wizard)readWizard();
 const current=kind==='source'?wizard?.path:kind==='target'?wizard?.target:kind==='restore'?document.getElementById('restore-target')?.value:kind==='input'?document.getElementById(id)?.value:document.getElementById('import-path')?.value;
 pickerContext={kind,id,index,previous:current};
 await showDirectories(current||nativeSession.workspace);
}
async function showDirectories(path,fallback=true){
 try{
  const result=await nativeRequest('filesystem.directories',{query:{path}});
  pickerContext.current=result.path;
  openModal(`${modalHead('选择本机目录')}<div class="modal-body"><div class="directory-path"><button class="btn small" data-native="directory" data-path="${esc(result.parent)}">上一级</button><code>${esc(result.path)}</code></div><div class="directory-list">${result.directories.map(item=>`<button data-native="directory" data-path="${esc(item.path)}">${icon('folder')}<span>${esc(item.name)}</span>${icon('chevron')}</button>`).join('')||'<p class="subtext">此目录中没有子目录。</p>'}</div></div><div class="modal-footer"><button class="btn" data-native="picker-cancel">取消</button><button class="btn primary" data-native="picker-select">选择此目录</button></div>`);
 }catch(error){if(fallback)await showDirectories(nativeSession.workspace,false);else{toast(error.message);returnFromPicker(false)}}
}
function returnFromPicker(chosen){
 const context=pickerContext;pickerContext=null;
 if(!context)return;
 const path=chosen?context.current:context.previous;
 if(context.kind==='source'||context.kind==='target'){
  if(chosen){wizard[context.kind==='source'?'path':'target']=path;if(context.kind==='source'&&!wizard.name)wizard.name=path.split(/[\\/]/).filter(Boolean).at(-1)||'文件备份'}
  renderWizard();
 }else if(context.kind==='input'){
  if(chosen){const input=document.getElementById(context.id);if(input)input.value=path}
 }else if(context.kind==='restore')restoreModal(context.id,context.index,path);
 else openImportModal(path);
}
function openImportModal(path=''){
 openModal(`${modalHead('导入已有备份','选择包含 manifest.bkn 与 data 目录的完整备份版本。')}<div class="modal-body"><div class="field"><label for="import-path">备份版本目录</label><div class="input-with-button"><input id="import-path" value="${esc(path)}" placeholder="备份位置 / snapshot-…"><button class="btn" data-native="pick-import">浏览</button></div></div><p class="subtext">导入前会校验文件内容。不会修改备份，也不会自动还原。</p></div><div class="modal-footer"><button class="btn" data-action="close">取消</button><button class="btn primary" data-native="import">校验并导入</button></div>`);
}
document.addEventListener('click',event=>{
 const control=event.target.closest('[data-native]');if(!control)return;
 event.stopImmediatePropagation();
 const {native:action,id,index,path}=control.dataset;
 if(action==='pick-source')openDirectoryPicker('source');
 if(action==='pick-target')openDirectoryPicker('target');
 if(action==='pick-restore')openDirectoryPicker('restore',id,Number(index));
 if(action==='pick-import')openDirectoryPicker('import');
 if(action==='pick-input')openDirectoryPicker('input',id);
 if(action==='directory')showDirectories(path);
 if(action==='picker-cancel')returnFromPicker(false);
 if(action==='picker-select')returnFromPicker(true);
 if(action==='import-open')openImportModal();
 if(action==='import')importBackup();
});
