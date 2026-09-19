import {Call} from '/wails/runtime.js';
import {VirtualView} from './virtual.js';
const config=await fetch('/app-config.json').then(r=>r.json());
const api=(name,...args)=>Call.ByName(`${config.service}.${name}`,...args);
const $=id=>document.getElementById(id);
const number=n=>new Intl.NumberFormat('zh-CN').format(n||0);
const safe=s=>String(s).replace(/[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
let status={},session='',directory=1,polling=false,rootBuilt=false,allExpanded=false;
const expanded=new Set([1]),collapsed=new Set();
function error(e){$('error').textContent=safe(e?.message||e);$('error').hidden=false}
function clearError(){$('error').hidden=true}
function updateActions(){for(const id of ['refresh','expand-all','collapse-all','jump-go'])$(id).disabled=!status.canBrowse}
function empty(title,text=''){$('empty').hidden=false;$('empty').querySelector('h2').textContent=title;$('empty').querySelector('p').textContent=text}
const fileView=new VirtualView($('list-scroll'),$('list-space'),36,
 async(offset,count)=>{if(!status.canBrowse)throw Error('请先完成目录读取');return api('DirectoryView',session,directory,offset,count)},
 fileRow,result=>{$('empty').hidden=result.total>0;if(!result.total)empty('暂无已读取的项目',status.running?'扫描仍在进行，完成后将刷新。':'目录为空、被过滤或未能完整读取。');$('item-info').textContent=`共 ${number(result.total)} 项 · 连续滚动浏览${status.running?' · 扫描中':''}`;$('jump').max=Math.max(1,result.total);renderCrumbs(result.breadcrumbs)},error);
const treeView=new VirtualView($('tree'),$('tree-space'),32,
 async(offset,count)=>{if(!status.canBrowse)throw Error('请先完成目录读取');return api('TreeView',session,[...expanded],[...collapsed],allExpanded,offset,count)},
 treeRow,result=>{treeView.space.style.minWidth=Math.max($('tree').clientWidth,result.maxDepth*16+260)+'px'},error);
function resetView(){directory=1;rootBuilt=false;allExpanded=false;expanded.clear();expanded.add(1);collapsed.clear();fileView.clear();treeView.clear();$('breadcrumbs').textContent='正在读取目录…';empty('正在读取目录结构','索引就绪后即可连续浏览。');$('item-info').textContent='连续滚动浏览完整列表';updateActions()}
async function scan(){clearError();try{const id=await api('Scan',$('root-path').value.trim(),$('mode').value);session=id;resetView();await poll()}catch(e){error(e)}}
$('choose').onclick=async()=>{clearError();try{const path=await api('SelectDirectory');if(path){$('root-path').value=path;await scan()}}catch(e){error(e)}};
$('scan').onclick=scan;$('root-path').onkeydown=e=>{if(e.key==='Enter')scan()};
$('cancel').onclick=async()=>{try{await api('Cancel');await poll()}catch(e){error(e)}};
$('refresh').onclick=()=>{clearError();fileView.reset(true);treeView.reset(true)};
$('expand-all').onclick=()=>{allExpanded=true;collapsed.clear();treeView.reset(true)};
$('collapse-all').onclick=()=>{allExpanded=false;expanded.clear();collapsed.clear();treeView.reset()};
$('jump-go').onclick=()=>fileView.go(Math.max(0,Number($('jump').value)-1));
$('jump').onkeydown=e=>{if(e.key==='Enter')$('jump-go').click()};
function loadDirectory(id){clearError();empty("正在读取目录…");directory=id;fileView.reset();treeView.schedule()}
function fileRow(item,index){const row=document.createElement('div');row.className='file-row'+(item.directory&&!item.link?' directory':'');row.dataset.index=index;row.setAttribute('role','row');
 const name=document.createElement('div'),icon=document.createElement('span'),label=document.createElement('span'),type=document.createElement('div');name.className='file-name';icon.className='file-icon';icon.textContent=item.link?'↗':item.directory?'▱':'▤';label.textContent=safe(item.name);name.title=safe(item.name);name.append(icon,label);type.className='file-type';type.textContent=item.link?'链接 · 不展开':item.directory?'文件夹':'文件';row.append(name,type);if(item.directory&&!item.link)row.ondblclick=()=>loadDirectory(item.id);return row}
function treeRow(item,index){const row=document.createElement('div');row.className='tree-row'+(item.id===directory?' active':'');row.dataset.id=item.id;row.dataset.index=index;row.setAttribute('role','treeitem');row.setAttribute('aria-level',String(item.depth+1));row.setAttribute('aria-expanded',String(item.expanded));row.style.paddingLeft=item.depth*16+'px';
 const toggle=document.createElement('button'),name=document.createElement('span'),icon=document.createElement('span');toggle.className='twisty';toggle.textContent=item.link?'·':item.expanded?'▾':'▸';toggle.disabled=item.link;toggle.setAttribute('aria-label',(item.expanded?'折叠 ':'展开 ')+safe(item.name));name.className='tree-name';name.title=safe(item.name);icon.className='folder-icon';icon.textContent='▱';name.append(icon,document.createTextNode(safe(item.id===1?status.root:item.name)));
 toggle.onclick=()=>{if(allExpanded){item.expanded?collapsed.add(item.id):collapsed.delete(item.id)}else{item.expanded?expanded.delete(item.id):expanded.add(item.id)}treeView.reset(true)};name.onclick=()=>{if(!item.link)loadDirectory(item.id)};row.append(toggle,name);return row}
function renderCrumbs(crumbs){$('breadcrumbs').replaceChildren();for(const[i,c]of crumbs.entries()){if(i){const sep=document.createElement('span');sep.className='separator';sep.textContent='›';$('breadcrumbs').append(sep)}const b=document.createElement('button');b.textContent=i===0?(status.root||safe(c.name)):safe(c.name);b.title=b.textContent;b.onclick=()=>loadDirectory(c.id);$('breadcrumbs').append(b)}}
async function poll(){if(polling)return;polling=true;const askedSession=session;try{const s=await api('Status');if(askedSession!==session&&s.session!==session)return;const wasRunning=status.running;status=s;document.body.classList.toggle("has-scan",Boolean(s.session));if(s.session&&session!==s.session){session=s.session;$('root-path').value=s.root;resetView()}
 $('records').textContent=s.session?number(s.records):'—';$('files').textContent=s.session?number(s.files):'—';$('folders').textContent=s.session?number(s.directories):'—';$('records-label').textContent=s.backend==='ntfs'?'已读卷级记录':'已读取条目';$('state').textContent=({idle:'尚未开始',scanning:'扫描中',cancelling:'正在停止',done:'扫描完成',partial:'部分完成',cancelled:'已停止',failed:'扫描失败'})[s.state]||'准备中';$('elapsed').textContent=s.session?`${s.seconds.toFixed(1)} 秒`:'准备就绪';$('message').textContent=safe(s.message);$('backend').textContent=({ntfs:'NTFS 主名称索引',walk:'普通目录遍历'})[s.backend]||'等待选择';$('pulse').classList.toggle('running',s.running);$('cancel').hidden=!s.running;$('scan').hidden=s.running;$('choose').disabled=s.running;$('mode').disabled=s.running;$('root-path').disabled=s.running;
 $('notice').hidden=!s.notice;$('notice').textContent=safe(s.notice||'');$('memory').textContent=s.session?`索引约 ${number(s.memoryMB)} MB / 预算 ${number(s.budgetMB)} MB`:'Wails 3 · Go';$('scope-note').textContent=s.backend==='ntfs'?'NTFS 计数为卷级记录；列表限定于所选目录，可能缺少硬链接别名。':`只读扫描 · 跳过 ${number(s.skipped)} · 访问失败 ${number(s.errors)}`;
 if(s.canBrowse&&!rootBuilt){rootBuilt=true;treeView.reset();loadDirectory(1)}
 else if(s.canBrowse&&wasRunning&&!s.running){treeView.reset(true);fileView.reset(true)}
 if(s.state==='failed')error(s.message);updateActions();
 }catch(e){error(e)}finally{polling=false}}
await poll();setInterval(poll,450);
