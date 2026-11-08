/* BackNest basic demo UI. Native file operations are delegated to the C++ engine. */
'use strict';
const paths={
 nest:'M4 10 12 4l8 6v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z M8 12l4 3 4-3 M8 16l4 3 4-3',
 grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
 folder:'M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3z',
 restore:'M3 10a9 9 0 1 1 1 8 M3 4v6h6 M12 8v5l3 2',
 clock:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v6l4 2',
 activity:'M3 12h4l3-7 4 14 3-7h4',
 settings:'M9.34 5.42 L10.02 2.71 L13.98 2.71 L14.66 5.42 L16.37 6.41 L19.06 5.64 L21.04 9.06 L19.03 11.01 L19.03 12.99 L21.04 14.94 L19.06 18.36 L16.37 17.59 L14.66 18.58 L13.98 21.29 L10.02 21.29 L9.34 18.58 L7.63 17.59 L4.94 18.36 L2.96 14.94 L4.97 12.99 L4.97 11.01 L2.96 9.06 L4.94 5.64 L7.63 6.41Z M15.25 12a3.25 3.25 0 1 0-6.5 0 3.25 3.25 0 0 0 6.5 0Z',
 shield:'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6',
 chevron:'m9 5 7 7-7 7',plus:'M12 5v14 M5 12h14',arrow:'M4 12h16 M14 6l6 6-6 6',
 drive:'M5 4h14l3 13v3H2v-3z M3 16h18 M16 18h2',
 check:'m5 12 4 4L19 6',close:'m6 6 12 12 M6 18 18 6',
 help:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5 M12 17h.01',
 bell:'M5 17h14l-2-3V9a5 5 0 0 0-10 0v5z M10 21h4 M12 2v2',
 more:'M5 12h.01 M12 12h.01 M19 12h.01',
 upload:'M12 16V3 M7 8l5-5 5 5 M4 14v6h16v-6',
 download:'M12 3v13 M7 11l5 5 5-5 M4 15v5h16v-5',
 lock:'M6 10h12v11H6z M8 10V6a4 4 0 0 1 8 0v4 M12 14v3',
 archive:'M3 3h18v5H3z M5 8v13h14V8 M9 12h6',
 leaf:'M20 3C7 3 3 8 5 15s14 6 15-12z M5 20 16 9',
 search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6',
 file:'M5 3h9l5 5v13H5z M14 3v6h5 M8 13h8 M8 17h6',
 play:'m8 5 11 7-11 7z',pause:'M7 4h3v16H7z M14 4h3v16h-3z',
 menu:'M4 6h16 M4 12h16 M4 18h16',info:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 11v6 M12 7h.01',
 calendar:'M3 5h18v16H3z M3 10h18 M7 3v4 M17 3v4 M7 14h3 M14 14h3',
 trash:'M4 6h16 M9 6V3h6v3 M6 6l1 15h10l1-15 M10 10v7 M14 10v7',
 code:'m8 7-5 5 5 5 M16 7l5 5-5 5 M14 3l-4 18',
 compress:'m4 4 5 5 M4 9h5V4 M20 20l-5-5 M15 20v-5h5',
 monitor:'M3 3h18v14H3z M8 21h8 M12 17v4',
};
const icon=(name,cls='')=>`<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.file}"/></svg>`;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const initial=createInitialState();
let state=workspaceStore.loadLocal();
workspaceStore.save(state);
let view='overview',filter='all',query='',selected=state.tasks[0]?.id,mobileOpen=false,wizard=null,modal=null,lastFocus=null,extWatchId='';
const navs=[['overview','grid','总览'],['backups','folder','我的备份'],['restore','restore','数据还原'],['extensions','archive','扩展能力'],['activity','activity','活动日志']];
const persist=()=>workspaceStore.save(state);
const getTask=id=>state.tasks.find(t=>t.id===id);
const now=()=>new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date());
const activeSchedules=()=>state.tasks.filter(t=>t.enabled&&t.schedule!=='手动备份');
const pill=t=>t.status==='running'?`<span class="badge running"><i class="dot"></i>${t.phase==='verifying'?'校验中':t.phase==='scanning'?'扫描中':'备份中'} ${t.progress||0}%</span>`:t.status==='done'?'<span class="badge"><i class="dot"></i>已完成</span>':t.status==='failed'?'<span class="badge failed">失败</span>':'<span class="badge pending">未备份</span>';
const folder=t=>`<span class="folder-icon ${esc(t.color)}">${icon('folder')}</span>`;
const textButton=(label,action)=>`<button class="text-btn" data-action="${action}">${label}${icon('chevron')}</button>`;
const button=(label,action,i='plus',cls='primary')=>`<button class="btn ${cls}" data-action="${action}">${icon(i)}${label}</button>`;
const heading=(title,desc,action='new',label='新建备份',i='plus')=>`<div class="page-heading"><div><h1>${title}</h1><p class="subtext">${desc}</p></div>${action?button(label,action,i):''}</div>`;
function render(){
 const title=navs.find(n=>n[0]===view)?.[2]||({settings:'偏好设置',plan:'项目说明',extensions:'扩展能力'}[view]);
 document.title=`${title} · 归档 BackNest`;
 document.getElementById('app').innerHTML=`
  <div class="mobile-overlay ${mobileOpen?'show':''}" data-action="menu"></div>
  <div class="shell">
   <aside class="sidebar ${mobileOpen?'open':''}">
    <a href="#overview" class="brand" aria-label="归档首页"><span class="brand-mark">${icon('archive')}</span><div><div class="brand-name">归档</div><small>BackNest</small></div></a>
    <div class="nav-label">工作区</div>
    <nav class="nav" aria-label="主导航">${navs.map(([id,i,name])=>`<button data-view="${id}" class="${view===id?'active':''}" ${view===id?'aria-current="page"':''}>${icon(i)}${name}${id==='backups'?`<span class="nav-count">${state.tasks.length}</span>`:''}</button>`).join('')}</nav>
    <div class="sidebar-bottom">
     <nav class="nav" aria-label="设置导航"><button data-view="settings" class="${view==='settings'?'active':''}">${icon('settings')}偏好设置</button><button data-view="plan" class="${view==='plan'?'active':''}">${icon('help')}项目说明</button></nav>
     <div class="workspace-info">${icon('monitor')}<div><strong>本地工作区</strong><small>数据保存在当前浏览器</small></div></div>
    </div>
   </aside>
   <header class="topbar"><div class="breadcrumb"><button class="icon-btn mobile-menu" aria-label="打开导航" data-action="menu">${icon('menu')}</button><span>本地工作区</span>${icon('chevron')}<span>${title}</span></div><div class="top-actions"><span class="prototype ${nativeSession?'connected':''}"><i></i>${nativeBusy?'正在执行':nativeSession?`本机引擎 · ${nativeSession.engine}`:'未连接引擎'}</span><button class="help-button" data-view="activity">${icon('activity')}<span>查看日志</span></button></div></header>
   <main class="content" id="main-content">${({overview,backups,restore:restoreView,schedules,activity,settings,plan,extensions}[view]||overview)()}<footer class="footer"><span>${nativeSession?`本机引擎 · ${nativeSession.engine}`:'运行 python3 server.py 后连接本机功能'}</span><span>归档 0.3</span></footer></main>
  </div>`;
}
function overview(){
 const versionCount=state.tasks.reduce((n,t)=>n+t.versions.length,0);
 const latest=state.logs[0];
 return `${heading('备份概览','查看备份任务、历史版本和最近的操作记录。')}
  <section class="summary-strip" aria-label="备份统计">
   ${[['备份任务',state.tasks.length,'个'],['历史版本',versionCount,'份'],['已备份文件',state.tasks.filter(t=>t.versions.length).reduce((n,t)=>n+(t.files||0),0),'个'],['最近操作',latest?.time||'暂无','']].map(([label,value,unit],i)=>`<div class="summary-item"><div class="summary-label">${label}</div><div class="summary-value ${i===3?'is-time':''}">${esc(value)}<small>${unit}</small></div></div>`).join('')}
  </section>
  <section class="panel task-panel"><div class="panel-head"><h2>备份任务 <span class="count-label">${state.tasks.length}</span></h2>${textButton('管理任务','goto-backups')}</div>${taskTable(state.tasks.slice(0,5))}<div class="table-footer"><span>${state.tasks.length?`显示 ${Math.min(5,state.tasks.length)} 个任务`:'暂无任务'}</span>${textButton('还原文件','goto-restore')}</div></section>
  <div class="overview-secondary">
   <section class="panel"><div class="panel-head"><h2>最近活动</h2>${textButton('全部记录','goto-activity')}</div><div class="recent-logs">${state.logs.slice(0,3).map(l=>`<div class="recent-log"><span class="log-mark">${icon(l.level==='success'?'check':l.level==='warning'?'info':'activity')}</span><div><strong>${esc(l.title)}</strong><small>${esc(l.detail)}</small></div><time>${esc(l.time)}</time></div>`).join('')||'<div class="empty">暂无操作记录</div>'}</div></section>
   <section class="panel destination-panel"><div class="panel-head"><h2>默认备份位置</h2>${textButton('更改','goto-settings')}</div><div class="destination-content"><div class="destination-item">${icon('drive')}<div><strong>${state.settings.target?esc(state.settings.target.split('/').filter(Boolean).at(-1)||'备份目录'):'未设置'}</strong>${state.settings.target?`<code>${esc(state.settings.target)}</code>`:'<span class="subtext">可在偏好设置中填写默认目录</span>'}</div></div><div class="destination-note">${icon('info')}${state.settings.target?'每次运行会在此位置创建独立的备份版本。':'尚未选择备份位置。'}</div><div class="destination-plan"><span>${icon('archive')}备份方式</span><span>完整备份 · 文件校验</span></div></div></section>
  </div>`;
}
function taskTable(tasks){
 if(!tasks.length)return state.tasks.length?`<div class="empty">没有匹配的任务<br><span class="subtext">请调整搜索内容或筛选条件。</span></div>`:`<div class="empty">${icon('folder')}还没有备份任务<br><span class="subtext">添加需要备份的目录，创建第一个任务。</span><br><button class="btn small" data-action="new">新建备份</button></div>`;
 return `<div class="table-wrap"><table><thead><tr><th>任务 / 源目录</th><th>备份方式</th><th>最近运行</th><th class="backup-size">大小</th><th>状态</th><th>操作</th></tr></thead><tbody>${tasks.map(t=>`<tr><td><div class="file-cell">${folder(t)}<div><strong>${esc(t.name)}</strong><small title="${esc(t.path)}">${esc(t.path)}</small></div></div></td><td><span class="type-label">${esc(t.type)}</span></td><td>${esc(t.time)}</td><td class="backup-size numeric">${esc(t.size)}</td><td>${pill(t)}</td><td><div class="row-actions"><button class="run-btn" data-action="run" data-id="${esc(t.id)}" aria-label="运行${esc(t.name)}备份" title="运行备份" ${nativeBusy?'disabled':''}>${icon('play')}运行</button><button class="icon-btn" data-action="detail" data-id="${esc(t.id)}" aria-label="查看${esc(t.name)}详情" title="任务详情">${icon('more')}</button></div></td></tr>`).join('')}</tbody></table></div>`;
}
function backups(){const tasks=state.tasks.filter(t=>(filter==='all'||(filter==='auto'?t.enabled:!t.enabled))&&`${t.name} ${t.path}`.toLowerCase().includes(query.toLowerCase()));return `${heading('我的备份','管理备份来源、目标位置和执行策略。')}<div class="toolbar"><div class="tabs" aria-label="任务筛选">${[['all','全部任务'],['auto','自动备份'],['manual','手动 / 已暂停']].map(([id,t])=>`<button class="${filter===id?'active':''}" data-filter="${id}">${t}</button>`).join('')}</div><label class="search">${icon('search')}<input id="search" placeholder="搜索任务或路径…" aria-label="搜索任务" value="${esc(query)}"></label></div><section class="panel"><div class="panel-head"><h2>备份任务 <span class="mini-pill">${tasks.length} 个</span></h2><span class="subtext">浏览器本地保存</span></div><div id="task-results">${taskTable(tasks)}</div></section>`}
function restoreView(){const restorable=state.tasks.filter(task=>task.versions.length>0);const t=restorable.find(task=>task.id===selected)||restorable[0];return `${heading('数据还原','选择历史版本，还原到新目录或空目录。',null)}<div class="restore-toolbar"><p class="subtext">所有版本来自实际备份；还原前会校验文件内容。</p><button class="btn small" data-native="import-open">${icon('archive')}导入已有备份</button></div>${t?`<div class="restore-layout"><section class="panel backup-picker" aria-label="选择备份任务">${restorable.map(x=>`<button class="${t.id===x.id?'selected':''}" data-action="select-restore" data-id="${esc(x.id)}">${folder(x)}<div><strong>${esc(x.name)}</strong><small>${x.versions.length} 个历史版本</small></div></button>`).join('')}</section><section class="panel"><div class="panel-head"><h2>${esc(t.name)} · 历史版本</h2><span class="subtext">${t.versions.length} 份可选快照</span></div><div class="version-list">${t.versions.map((v,i)=>`<div class="version"><span class="version-icon">${icon('archive')}</span><div><strong>${esc(v.label)} ${i===0?'<span class="mini-pill">最新</span>':''}</strong><small>${esc(v.size)} · ${Number(v.files).toLocaleString()} 个文件 · ${esc(t.type)}</small></div><button class="btn small" data-action="restore" data-id="${esc(t.id)}" data-version="${i}">${icon('restore')}还原此版本</button></div>`).join('')}</div><div class="table-footer">${icon('shield')}仅还原到新目录或空目录，不覆盖已有文件</div></section></div>`:'<div class="panel empty">暂无可还原的备份<br><span class="subtext">完成真实备份后才会生成可还原的版本，创建任务不会生成备份。</span></div>'}`}
function schedules(){const tasks=state.tasks.filter(t=>t.schedule!=='手动备份');return `${heading('计划任务','设置备份频率，启用或暂停自动备份。','new-schedule','新建计划')}<div class="notice">${icon('clock')}当前仅保存计划配置，不会自动执行备份。</div><div class="schedule-cards">${tasks.map(t=>`<section class="panel schedule-card"><div class="card-title"><div class="file-cell">${folder(t)}<div><strong>${esc(t.name)}</strong><small>${esc(t.schedule)}</small></div></div><button class="switch ${t.enabled?'on':''}" data-action="toggle-schedule" data-id="${esc(t.id)}" role="switch" aria-checked="${t.enabled}" aria-label="启用${esc(t.name)}计划"></button></div><div class="schedule-info"><div><span>备份方式</span><strong>${esc(t.type)}</strong></div><div><span>执行频率</span><strong>${esc(t.schedule)}</strong></div><div><span>数据保护</span><strong>${t.encryption?'已选择加密':'未选择加密'}</strong></div><div><span>计划状态</span><strong>${t.enabled?'已启用（演示）':'已暂停'}</strong></div></div><div class="schedule-card-footer"><span class="subtext">${icon('clock')} ${esc(t.time)}</span><button class="btn small" data-action="detail" data-id="${esc(t.id)}">查看配置</button></div></section>`).join('')||'<div class="panel empty">还没有计划，创建一个自动备份任务吧。</div>'}</div>`}
function activity(){return `${heading('活动日志','查看备份、还原与任务配置的操作记录。','export','导出日志','download')}<section class="panel"><div class="panel-head"><h2>活动记录</h2><span class="subtext">${state.logs.length} 条记录</span></div><div class="log-list">${state.logs.map(l=>`<div class="log"><div class="activity-icon">${icon(l.level==='success'?'check':l.level==='warning'?'info':'activity')}</div><div><strong>${esc(l.title)}</strong><small>${esc(l.detail)}</small></div><time>${esc(l.time)}</time></div>`).join('')||'<div class="empty">暂无活动记录</div>'}</div></section>`}
function settings(){const s=state.settings;return `${heading('偏好设置','设置新任务的默认选项。','save-settings','保存设置','check')}<div class="settings"><section class="panel settings-section"><h2>备份默认设置</h2><div class="settings-row"><div><strong>默认备份位置</strong><small>新建任务时默认填写的目标目录</small></div><input id="setting-target" value="${esc(s.target)}" placeholder="未设置" aria-label="默认备份位置"></div><div class="settings-row"><div><strong>文件校验</strong><small>备份与还原都会检查文件内容；校验通过后才报告完成</small></div><span class="subtext">始终开启</span></div><div class="settings-row"><div><strong>版本保留</strong><small>基础版保留所有备份版本，不自动删除磁盘上的备份</small></div><span class="subtext">全部保留</span><input id="setting-keep" type="hidden" value="${s.keep}"></div></section><section class="panel settings-section"><h2>本地配置</h2><div class="settings-row"><div><strong>清空界面记录</strong><small>清空任务与界面日志；已经生成的磁盘备份会保留，可重新导入</small></div><button class="btn small danger" data-action="reset">清空本地数据</button></div></section></div>`}
function settingSwitch(key,title,desc){return `<div class="settings-row"><div><strong>${title}</strong><small>${desc}</small></div><button class="switch ${state.settings[key]?'on':''}" data-setting="${key}" role="switch" aria-checked="${state.settings[key]}" aria-label="${title}"></button></div>`}
function plan(){return `${heading('项目说明','根据实验要求拆解：基础闭环已完成，扩展能力按 P4–P6 落地。',null)}<div class="notice">${icon('code')}当前：Web UI + 本地通信桥 + C++17 引擎（无编译器时使用兼容 Python 引擎）。实现说明见 README.md 与 docs/FEATURES_P4_P6.md。</div><div class="plan-grid">${[['01','界面与操作流程','已完成 · 本机 demo',['总览、备份与版本列表','三步创建与还原确认','扩展能力页：打包/压缩/加密/自动化','浏览器与后端配置持久化']],['02','本机备份与还原','已完成 · 基础要求',['目录递归遍历与复制','目录树重建与数据还原','路径校验与失败处理','备份清单与 CRC32 内容校验']],['03','扩展功能','P4–P6 已实现 · 其余待开发',['自实现 BNAR 容器与 LZSS 压缩','AES-256-GCM 认证加密与短期 secretRef','定时 cron、实时去抖监听','增量备份（硬链接）与独立版本']]].map(([n,t,d,items])=>`<section class="panel plan-card"><div class="plan-number">${n}</div><h2>${t}</h2><p>${d}</p><ul>${items.map(x=>`<li>${x}</li>`).join('')}</ul></section>`).join('')}</div>`}
function extensions(){
 const engine=nativeSession?nativeSession.engine:'未连接';
 setTimeout(refreshExtensionSchedules,0);
 return `${heading('扩展能力','P4 打包/压缩 · P5 加密 · P6 定时/实时/增量。所有结果都来自真实文件操作。',null)}
  <div class="notice">${icon('info')}当前引擎：${esc(engine)}。密码只在内存中换取短期 secretRef，不会写入任务记录、日志或浏览器存储。</div>
  <div class="plan-grid">
   <section class="panel plan-card"><div class="plan-number">P4 · 自实现容器与压缩</div><h2>打包与压缩</h2><p>BNAR 单文件容器与 LZSS 分块压缩均由项目代码实现，目标文件必须不存在。</p>
    <div class="field"><label for="ext-arc-source">源目录</label><div class="input-with-button"><input id="ext-arc-source" placeholder="绝对路径"><button class="btn" data-native="pick-input" data-id="ext-arc-source">浏览</button></div></div>
    <div class="field"><label for="ext-arc-file">归档文件</label><input id="ext-arc-file" placeholder="…/bundle.bkn"></div>
    <div class="inline-actions"><button class="btn primary" data-action="ext-pack">打包</button></div>
    <div class="field"><label for="ext-arc-out">解包到目录</label><div class="input-with-button"><input id="ext-arc-out" placeholder="新目录或空目录"><button class="btn" data-native="pick-input" data-id="ext-arc-out">浏览</button></div></div>
    <div class="inline-actions"><button class="btn" data-action="ext-unpack">解包</button></div>
    <div class="field"><label for="ext-cmp-source">压缩源文件</label><input id="ext-cmp-source" placeholder="任意普通文件"></div>
    <div class="field"><label for="ext-cmp-file">压缩/解压目标文件</label><input id="ext-cmp-file" placeholder="…/data.lz"></div>
    <div class="inline-actions"><button class="btn" data-action="ext-compress">压缩</button><button class="btn" data-action="ext-decompress">解压</button></div>
   </section>
   <section class="panel plan-card"><div class="plan-number">P5 · 加密</div><h2>认证加密</h2><p>AES-256-GCM + PBKDF2-HMAC-SHA256；正确密码可还原，错误密码或篡改会明确失败。</p>
    <div class="field"><label for="ext-enc-source">源文件</label><input id="ext-enc-source" placeholder="明文或密文文件"></div>
    <div class="field"><label for="ext-enc-file">目标文件</label><input id="ext-enc-file" placeholder="…/secret.bne"></div>
    <div class="field"><label for="ext-enc-password">密码</label><input id="ext-enc-password" type="password" autocomplete="off" placeholder="仅本次使用"></div>
    <div class="inline-actions"><button class="btn primary" data-action="ext-encrypt">加密</button><button class="btn" data-action="ext-decrypt">解密</button></div>
    <div class="notice">${icon('lock')}加密完成或失败后密码输入框会清空，secretRef 会立即撤销。</div>
   </section>
   <section class="panel plan-card"><div class="plan-number">P6 · 自动化与增量</div><h2>定时、实时与增量</h2><p>增量备份对未变文件使用硬链接，保留的每个版本都可独立还原；计划只在服务进程运行期间执行。</p>
    <div class="field"><label for="ext-source">源目录</label><div class="input-with-button"><input id="ext-source" placeholder="绝对路径"><button class="btn" data-native="pick-input" data-id="ext-source">浏览</button></div></div>
    <div class="field"><label for="ext-destination">备份位置</label><div class="input-with-button"><input id="ext-destination" placeholder="独立的新目录"><button class="btn" data-native="pick-input" data-id="ext-destination">浏览</button></div></div>
    <div class="two-fields"><div class="field"><label for="ext-cron">定时表达式</label><input id="ext-cron" value="*/5 * * * *"></div><div class="field"><label for="ext-tz">时区</label><input id="ext-tz" value="Asia/Shanghai"></div></div>
    <div class="inline-actions"><button class="btn" data-action="ext-preview">增量预览</button><button class="btn primary" data-action="ext-schedule">创建定时计划</button></div>
    <div class="inline-actions"><button class="btn" data-action="ext-watch">开始实时监听</button><button class="btn" data-action="ext-watch-stop">停止监听</button></div>
    <div id="ext-status" class="notice">${icon('clock')}填写源目录和备份位置后，可先预览增量，再创建计划或监听。</div>
   </section>
  </div>
  <section class="panel settings-section"><div class="panel-head"><h2>后端定时计划</h2><span class="subtext">仅在 server.py 运行期间生效；进程退出后不会在后台继续执行</span></div><div id="ext-schedules" class="recent-logs"><p class="subtext">正在读取…</p></div></section>`;
}
function extStatus(message){const node=document.getElementById('ext-status');if(node)node.innerHTML=`${icon('info')}${esc(message)}`}
function extValue(id){return document.getElementById(id)?.value.trim()||''}
async function refreshExtensionSchedules(){
 if(!nativeSession||view!=='extensions')return;
 try{
  const list=await nativeRequest('schedules.list');
  const node=document.getElementById('ext-schedules');if(!node)return;
  node.innerHTML=list.length?list.map(item=>`<div class="recent-log"><span class="log-mark">${icon('clock')}</span><div><strong>${esc(item.expression)} · ${esc(item.timezone)}</strong><small>${item.enabled?'已启用':'已暂停'} · 下次 ${esc(item.nextRunAt?localTime(item.nextRunAt):'—')} · ${esc(item.taskId)}</small></div><button class="btn small danger" data-action="ext-schedule-delete" data-id="${esc(item.id)}">删除</button></div>`).join(''):'<p class="subtext" style="padding:10px 0">暂无后端定时计划。</p>';
 }catch(error){const node=document.getElementById('ext-schedules');if(node)node.innerHTML=`<p class="subtext">${esc(error.message)}</p>`}
}
async function extensionJob(operation,body,label){
 if(!nativeSession){toast('请先运行 python3 server.py。');return null}
 if(nativeBusy){toast('已有文件操作正在运行，请等待完成。');return null}
 nativeBusy=true;extStatus(`${label}：正在执行…`);
 try{
  const accepted=await nativeRequest(operation,{body});
  const result=await pollNativeJob(accepted.id,job=>extStatus(`${label}：${job.phase||'running'} · ${job.files}/${job.totalFiles} 个文件`));
  log(`${label}完成`,`${result.files} 个文件 · ${formatSize(result.bytes)} · ${result.path}`,'success');
  toast(`${label}完成。`);extStatus(`${label}完成：${result.path}`);return result;
 }catch(error){log(`${label}失败`,error.message,'warning');toast(error.message);extStatus(`${label}失败：${error.message}`);return null}
 finally{nativeBusy=false}
}
async function extensionEncrypt(decrypt){
 const source=extValue('ext-enc-source'),destination=extValue('ext-enc-file'),password=document.getElementById('ext-enc-password')?.value||'';
 if(!source||!destination||!password){toast('请填写源文件、目标文件和密码。');return}
 if(nativeBusy){toast('已有文件操作正在运行，请等待完成。');return}
 nativeBusy=true;extStatus(`${decrypt?'解密':'加密'}：正在创建短期 secretRef…`);
 let reference='';
 try{
  const secret=await nativeRequest('secrets.create',{body:{value:password,purpose:'encryption'}});reference=secret.secretRef;
  const accepted=await nativeRequest(decrypt?'encryption.decrypt':'encryption.encrypt',{body:{source,destination,options:{algorithm:'aes-256-gcm'},secretRef:reference}});
  const result=await pollNativeJob(accepted.id,job=>extStatus(`${decrypt?'解密':'加密'}：${job.phase||'running'} · ${job.bytes}/${job.totalBytes} 字节`));
  log(`${decrypt?'解密':'加密'}完成`,`${result.path} · ${formatSize(result.bytes)}`,'success');toast(`${decrypt?'解密':'加密'}完成。`);extStatus(`${decrypt?'解密':'加密'}完成：${result.path}`);
 }catch(error){log(`${decrypt?'解密':'加密'}失败`,error.message,'warning');toast(error.message);extStatus(`${decrypt?'解密':'加密'}失败：${error.message}`)}
 finally{
  if(reference)await nativeRequest('secrets.revoke',{path:{secretId:reference}}).catch(()=>{});
  const input=document.getElementById('ext-enc-password');if(input)input.value='';
  nativeBusy=false;
 }
}
async function extensionPreview(){
 const source=extValue('ext-source'),destination=extValue('ext-destination');
 if(!source||!destination){toast('请填写源目录和备份位置。');return}
 try{
  const preview=await nativeRequest('incremental.preview',{body:{source,destination}});
  const text=`增量预览：新增 ${preview.added.length} · 变更 ${preview.changed.length} · 删除 ${preview.deleted.length}（基准 ${preview.baseSnapshotId||'无，首次为全量'}）`;
  extStatus(text);log('增量预览',text);toast(text);
 }catch(error){toast(error.message);extStatus(error.message)}
}
async function extensionTask(){
 const source=extValue('ext-source'),destination=extValue('ext-destination');
 if(!source||!destination)throw Error('请填写源目录和备份位置。');
 return await nativeRequest('tasks.create',{body:{name:`扩展任务 ${now()}`,source,destination,options:{mode:'incremental'}}});
}
async function extensionSchedule(){
 if(!nativeSession){toast('请先运行 python3 server.py。');return}
 try{
  const task=await extensionTask();
  const schedule=await nativeRequest('schedules.create',{body:{taskId:task.id,timezone:extValue('ext-tz')||'Asia/Shanghai',expression:extValue('ext-cron')||'*/5 * * * *',enabled:true,missedRunPolicy:'skip'}});
  extStatus(`定时计划已创建：${schedule.expression} · 下次 ${localTime(schedule.nextRunAt)}`);toast('定时计划已创建。');refreshExtensionSchedules();
 }catch(error){toast(error.message);extStatus(error.message)}
}
async function extensionWatch(){
 if(!nativeSession){toast('请先运行 python3 server.py。');return}
 try{
  const task=await extensionTask();
  const watch=await nativeRequest('watchers.start',{body:{taskId:task.id,source:extValue('ext-source'),debounceMs:500}});
  extWatchId=watch.id;extStatus(`实时监听已启动：${watch.id} · 去抖 500 ms`);toast('实时监听已启动。');
 }catch(error){toast(error.message);extStatus(error.message)}
}
async function extensionWatchStop(){
 if(!extWatchId){toast('当前没有已启动的监听。');return}
 try{await nativeRequest('watchers.stop',{path:{watchId:extWatchId}});extStatus(`实时监听已停止：${extWatchId}`);extWatchId='';toast('监听已停止。')}
 catch(error){toast(error.message);extStatus(error.message)}
}
function navigate(v){view=v;mobileOpen=false;query='';render();window.scrollTo({top:0,behavior:'instant'})}
function toast(msg){document.getElementById('toast-root').innerHTML=`<div class="toast" role="status">${icon('info')}${esc(msg)}</div>`;clearTimeout(toast.timer);toast.timer=setTimeout(()=>{document.getElementById('toast-root').innerHTML=''},4200)}
function log(title,detail,level='info'){state.logs.unshift({title,detail,level,time:`今天 ${now()}`});state.logs=state.logs.slice(0,100)}
function openModal(content){if(!modal)lastFocus=document.activeElement;modal=true;document.getElementById('overlay-root').innerHTML=`<div class="overlay" data-action="backdrop"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="dialog-title" tabindex="-1">${content}</section></div>`;(document.querySelector('.modal input:not([hidden]):not([type=hidden]),.modal select')||document.querySelector('.modal .btn,.modal .icon-btn'))?.focus({preventScroll:true})}
function closeModal(){document.getElementById('overlay-root').innerHTML='';modal=null;wizard=null;lastFocus?.isConnected&&lastFocus.focus()}
const modalHead=(title,desc='')=>`<div class="modal-head"><div><h2 id="dialog-title">${title}</h2>${desc?`<p>${desc}</p>`:''}</div><button class="icon-btn" data-action="close" aria-label="关闭对话框">${icon('close')}</button></div>`;
function startWizard(){wizard={step:1,name:'',path:'',target:state.settings.target,type:'完整备份',compression:false,encryption:false,schedule:'手动备份',files:[],error:''};renderWizard()}
function renderWizard(){const w=wizard;let body='';
 if(w.step===1)body=`<div class="field"><label for="task-name">任务名称</label><input id="task-name" placeholder="例如：工作文档" maxlength="40" value="${esc(w.name)}"></div><div class="field"><label for="task-path">源文件夹</label><div class="input-with-button"><input id="task-path" placeholder="输入本机绝对路径" value="${esc(w.path)}"><button class="btn" data-native="pick-source">浏览</button></div><small>备份此目录中的普通文件、子目录和空目录。基础版遇到链接或特殊文件会报错。</small></div><div class="field"><label for="task-target">备份位置</label><div class="input-with-button"><input id="task-target" placeholder="选择目标位置，或输入新目录的绝对路径" value="${esc(w.target)}"><button class="btn" data-native="pick-target">浏览</button></div><small>每次运行创建一个独立版本，不修改源文件。备份位置不能与源目录互相包含。</small></div>`;
 if(w.step===2)body=`<div class="two-fields"><div class="field"><label for="task-type">备份方式</label><select id="task-type"><option>完整备份</option></select><small>保存源目录中的全部普通文件和目录结构。</small></div><div class="field"><label for="task-schedule">执行方式</label><select id="task-schedule"><option>手动备份</option></select><small>创建后，在任务列表点击“运行”。</small></div></div><div class="option-row">${icon('shield')}<div><strong>内容校验</strong><small>复制后校验文件大小与 CRC32，检查备份内容是否完整。</small></div><span class="subtext">始终开启</span></div><div class="notice">${icon('info')}基础版保存原始文件，暂不提供压缩、加密、增量和自动备份。</div>`;
 if(w.step===3)body=`<div class="review">${[['任务名称',w.name],['源文件夹',w.path],['备份位置',w.target],['备份方式','完整备份'],['执行方式','手动运行'],['文件校验','大小 + CRC32']].map(([label,value])=>`<div class="review-row"><span>${label}</span><strong>${esc(value)}</strong></div>`).join('')}</div><p class="subtext">创建只保存任务配置。点击“运行”后才会读取源目录并写入备份。</p>`;
 openModal(`${modalHead('新建备份','选择源目录和目标位置。')}<div class="modal-body"><div class="steps">${['选择目录','备份选项','确认任务'].map((label,i)=>`<div class="step ${w.step===i+1?'current':''}"><b>${i+1}</b>${label}</div>`).join('')}</div>${w.error?`<div class="error" role="alert">${esc(w.error)}</div>`:''}${body}</div><div class="modal-footer"><button class="btn" data-action="${w.step===1?'close':'wizard-back'}">${w.step===1?'取消':'上一步'}</button><button class="btn primary" data-action="${w.step===3?'wizard-create':'wizard-next'}">${w.step===3?'创建任务':'下一步'}${icon('arrow')}</button></div>`)
}
function formatSize(n){return n>=1073741824?`${(n/1073741824).toFixed(1)} GB`:n>=1048576?`${(n/1048576).toFixed(1)} MB`:`${(n/1024).toFixed(1)} KB`}
function readWizard(){if(!wizard)return;if(wizard.step===1){wizard.name=document.getElementById('task-name').value.trim();wizard.path=document.getElementById('task-path').value.trim();wizard.target=document.getElementById('task-target').value.trim()}if(wizard.step===2){wizard.type=document.getElementById('task-type').value;wizard.schedule=document.getElementById('task-schedule').value}}
function createTask(){
 const w=wizard;
 const t={id:`task-${Date.now()}`,name:w.name,path:w.path,target:w.target,type:w.type,compression:w.compression,encryption:w.encryption,schedule:w.schedule,enabled:w.schedule!=='手动备份',size:w.files.length?formatSize(w.files.reduce((n,f)=>n+f.size,0)):'未读取',files:w.files.length||null,sourceScanned:w.files.length>0,time:'尚未运行',status:'ready',color:'',versions:[]};
 state.tasks.unshift(t);
 selected=t.id;
 log(`${t.name} · 已创建任务`,`源目录：${t.path} · 目标：${t.target}`);
 persist();
 closeModal();
 navigate('backups');
 toast('任务已创建，尚未执行备份。');
}
function runTask(id){performBackup(id)}
function detail(id){const t=getTask(id);if(!t)return;openModal(`${modalHead('备份任务详情')}<div class="modal-body"><div class="detail-header">${folder(t)}<div><h3>${esc(t.name)}</h3><small>${Number.isInteger(t.files)?`${t.files.toLocaleString()} 个文件 · ${esc(t.size)}`:'文件数量与大小尚未读取'}</small></div></div><div class="review">${[['源路径',t.path],['目标位置',t.target],['备份方式',t.type],['自动计划',`${t.schedule}${t.enabled?' · 已启用':' · 未启用'}`],['压缩 / 加密',`${t.compression?'选择压缩':'不压缩'} / ${t.encryption?'选择加密':'不加密'}`],['历史版本',`${t.versions.length} 份`]].map(([l,v])=>`<div class="review-row"><span>${l}</span><strong>${esc(v)}</strong></div>`).join('')}</div><p class="subtext">完整备份；已创建的磁盘版本不会随界面记录删除。</p></div><div class="modal-footer"><button class="btn danger" data-action="delete-confirm" data-id="${esc(t.id)}" ${nativeBusy?'disabled':''}>${icon('trash')}删除任务</button><div class="inline-actions"><button class="btn" data-action="detail-restore" data-id="${esc(t.id)}">历史版本</button><button class="btn primary" data-action="detail-run" data-id="${esc(t.id)}" ${nativeBusy?'disabled':''}>${icon('play')}运行备份</button></div></div>`)}
function restoreModal(id,index,destination){const t=getTask(id),v=t?.versions[index];if(!v)return;openModal(`${modalHead('还原历史版本','将文件恢复到新目录或空目录。')}<div class="modal-body"><div class="review"><div class="review-row"><span>备份任务</span><strong>${esc(t.name)}</strong></div><div class="review-row"><span>选择版本</span><strong>${esc(v.label)}</strong></div><div class="review-row"><span>文件数量</span><strong>${v.files.toLocaleString()} 个 · ${esc(v.size)}</strong></div><div class="review-row"><span>备份版本目录</span><strong>${esc(v.path)}</strong></div></div><div class="field"><label for="restore-target">还原到</label><div class="input-with-button"><input id="restore-target" value="${esc(destination===undefined?t.path+'-restored':destination)}"><button class="btn" data-native="pick-restore" data-id="${esc(id)}" data-index="${index}">浏览</button></div><small>可以输入尚不存在的新目录路径；已有目录必须为空。</small></div><div class="notice">${icon('shield')}点击“开始还原”后会实际写入文件。引擎会先校验备份，拒绝覆盖已有文件。</div><div id="restore-error" class="error" role="alert"></div></div><div class="modal-footer"><button class="btn" data-action="close">取消</button><button class="btn primary" data-action="confirm-restore" data-id="${esc(id)}" data-version="${index}">${icon('restore')}开始还原</button></div>`)}
document.addEventListener('click',e=>{
 const nav=e.target.closest('[data-view]');if(nav){navigate(nav.dataset.view);return}
 const f=e.target.closest('[data-filter]');if(f){filter=f.dataset.filter;render();return}
 const setting=e.target.closest('[data-setting]');if(setting){const key=setting.dataset.setting;state.settings[key]=!state.settings[key];persist();render();return}
 const option=e.target.closest('[data-wizard-option]');if(option&&wizard){readWizard();wizard[option.dataset.wizardOption]=!wizard[option.dataset.wizardOption];renderWizard();return}
 const el=e.target.closest('[data-action]');if(!el)return;const {action,id}=el.dataset;
 if(action==='backdrop'){if(e.target===el)closeModal();return}
 if(action==='ext-pack'){extensionJob('archive.pack',{source:extValue('ext-arc-source'),destination:extValue('ext-arc-file'),options:{algorithm:'bkn-archive-v1'}},'打包');return}
 if(action==='ext-unpack'){extensionJob('archive.unpack',{source:extValue('ext-arc-file'),destination:extValue('ext-arc-out'),options:{algorithm:'bkn-archive-v1'}},'解包');return}
 if(action==='ext-compress'){extensionJob('compression.compress',{source:extValue('ext-cmp-source'),destination:extValue('ext-cmp-file'),options:{algorithm:'lzss-v1','blockBytes':65536}},'压缩');return}
 if(action==='ext-decompress'){extensionJob('compression.decompress',{source:extValue('ext-cmp-source'),destination:extValue('ext-cmp-file'),options:{algorithm:'lzss-v1'}},'解压');return}
 if(action==='ext-encrypt'){extensionEncrypt(false);return}
 if(action==='ext-decrypt'){extensionEncrypt(true);return}
 if(action==='ext-preview'){extensionPreview();return}
 if(action==='ext-schedule'){extensionSchedule();return}
 if(action==='ext-watch'){extensionWatch();return}
 if(action==='ext-watch-stop'){extensionWatchStop();return}
 if(action==='ext-schedule-delete'){nativeRequest('schedules.delete',{path:{id}}).then(()=>{toast('计划已删除。');refreshExtensionSchedules()}).catch(error=>toast(error.message));return}
 if(action==='close'){closeModal();return}
 if(action==='menu'){mobileOpen=!mobileOpen;render();return}
 if(action.startsWith('goto-')){navigate(action.slice(5));return}
 if(action==='new'||action==='new-schedule'){startWizard(action==='new-schedule');return}
 if(action==='pick-folder'){readWizard();document.getElementById('folder-input').click();return}
 if(action==='wizard-next'){readWizard();wizard.error='';if(wizard.step===1&&(!wizard.name||!wizard.path||!wizard.target)){wizard.error='请填写任务名称、源文件夹和备份位置。';renderWizard();return}if(wizard.step===1&&wizard.path.replace(/\/$/,'')===wizard.target.replace(/\/$/,'')){wizard.error='源文件夹与备份位置不能相同。';renderWizard();return}wizard.step++;renderWizard();return}
 if(action==='wizard-back'){readWizard();wizard.step--;wizard.error='';renderWizard();return}
 if(action==='wizard-create'){createTask();return}
 if(action==='run'){runTask(id);return}
 if(action==='detail'){detail(id);return}
 if(action==='detail-run'){closeModal();runTask(id);return}
 if(action==='detail-restore'){closeModal();selected=id;navigate('restore');return}
 if(action==='select-restore'){selected=id;render();return}
 if(action==='restore'){restoreModal(id,Number(el.dataset.version));return}
 if(action==='confirm-restore'){performRestore(id,Number(el.dataset.version));return}
 if(action==='toggle-schedule'){const t=getTask(id);t.enabled=!t.enabled;log(`${t.name} · ${t.enabled?'启用':'暂停'}计划`,`${t.schedule} · 仅更新演示配置`);persist();render();toast(t.enabled?'已启用计划配置；当前不会自动执行。':'计划配置已暂停。');return}
 if(action==='export'){const blob=new Blob([JSON.stringify({app:'BackNest basic demo',exportedAt:new Date().toISOString(),logs:state.logs},null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='backnest-logs.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('活动日志已导出。');return}
 if(action==='delete-confirm'){const t=getTask(id);openModal(`${modalHead('删除备份任务')}<div class="modal-body"><p class="delete-confirm">删除「${esc(t.name)}」及其界面版本记录？<br>仅删除浏览器里的记录；磁盘上的备份文件会保留。</p></div><div class="modal-footer"><button class="btn" data-action="close">保留任务</button><button class="btn danger" data-action="delete" data-id="${esc(id)}">确认删除</button></div>`);return}
 if(action==='delete'){if(nativeBusy){toast('请等待文件操作完成。');return}const t=getTask(id);state.tasks=state.tasks.filter(t=>t.id!==id);log(`${t.name} · 已删除任务配置`,'仅移除浏览器内记录');persist();closeModal();render();toast('任务记录已删除，磁盘备份已保留。');return}
 if(action==='save-settings'){const target=document.getElementById('setting-target').value.trim(),keep=Number(document.getElementById('setting-keep').value);if(!Number.isInteger(keep)||keep<1||keep>100){toast('保留版本数量请输入 1 到 100 之间的整数。');document.getElementById('setting-keep').focus();return}state.settings.target=target;state.settings.keep=keep;persist();toast('偏好设置已保存。');return}
 if(action==='reset'){openModal(`${modalHead('清空本地数据')}<div class="modal-body"><p class="delete-confirm">清空当前浏览器中的任务配置、操作记录和偏好？<br>已创建的磁盘备份会保留，可通过“导入已有备份”重新打开。</p></div><div class="modal-footer"><button class="btn" data-action="close">取消</button><button class="btn danger" data-action="confirm-reset">清空本地数据</button></div>`);return}
 if(action==='confirm-reset'){if(nativeBusy){toast('请等待文件操作完成后再清空界面记录。');return}state=structuredClone(initial);selected=state.tasks[0]?.id;persist();closeModal();render();toast('本地数据已清空。');return}
});
document.addEventListener('input',e=>{if(e.target.id==='search'){query=e.target.value;const focus=e.target.selectionStart;render();const input=document.getElementById('search');input.focus();input.setSelectionRange(focus,focus)}});
document.addEventListener('change',e=>{
 if(e.target.id==='folder-input'&&wizard){readWizard();wizard.files=Array.from(e.target.files).map(f=>({path:f.webkitRelativePath||f.name,size:f.size}));if(wizard.files.length){const root=wizard.files[0].path.split('/')[0];wizard.path=root;wizard.name=wizard.name||root}renderWizard()}
 if(e.target.id==='setting-target'){const value=e.target.value.trim();state.settings.target=value;persist();toast('默认备份位置已保存。')}
 if(e.target.id==='setting-keep'){const n=Number(e.target.value);if(!Number.isInteger(n)||n<1||n>100){toast('请输入 1 到 100 之间的整数。');e.target.value=state.settings.keep;return}state.settings.keep=n;persist();toast('保留版本数量已保存。')}
});
document.addEventListener('keydown',e=>{if(!modal)return;if(e.key==='Escape'){closeModal();return}if(e.key==='Tab'){const items=[...document.querySelectorAll('.modal button:not(:disabled),.modal input:not([hidden]),.modal select,.modal a')].filter(x=>x.getClientRects().length);const first=items[0],last=items.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}}});
window.addEventListener('hashchange',()=>{if(location.hash==='#overview')navigate('overview')});
render();
connectNative();
