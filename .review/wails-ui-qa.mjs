import {createServer} from 'node:http';
import {readFile,writeFile,mkdtemp} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {resolve} from 'node:path';
const root=resolve('wails-browser/web');
let session='qa-1'; const results=[]; const calls=[];
const status=()=>({session,root:'C:\\样本目录',state:'done',backend:'ntfs',message:'结构已就绪，展开目录即可浏览',notice:'',records:1365927,files:1281607,directories:84319,skipped:0,errors:0,memoryMB:148,budgetMB:2048,seconds:3.8,canBrowse:true,running:false});
const rootItems=[{id:2,name:'项目资料',directory:true,link:false},{id:3,name:'工作文档',directory:true,link:false},{id:4,name:'照片与视频',directory:true,link:false},{id:5,name:'归档',directory:true,link:false},{id:6,name:'README.md',directory:false,link:false},{id:7,name:'directory-notes.txt',directory:false,link:false},{id:8,name:'<img src=x onerror="window.PWNED=1">.txt',directory:false,link:false}];
const server=createServer(async(req,res)=>{try{const path=new URL(req.url,'http://localhost').pathname;
 if(path==='/app-config.json'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({service:'main.BrowserService'}));return}
 if(path==='/wails/runtime.js'){res.setHeader('Content-Type','application/javascript');res.end(`export const Call={ByName:async(name,...args)=>{const r=await fetch('/rpc',{method:'POST',body:JSON.stringify({name,args})});return r.json()}};`);return}
 if(path==='/qa-driver.js'){res.setHeader('Content-Type','application/javascript');res.end(driver);return}
 if(path==='/result'){let b='';for await(const d of req)b+=d;console.log(b);console.log('ranges',JSON.stringify(calls.slice(-12)));results.push(JSON.parse(b));res.end('ok');return}
 if(path==='/rpc'){let body='';for await(const p of req)body+=p;const {name,args}=JSON.parse(body);let value=null;
  if(name.endsWith('.Status'))value=status();
  if(name.endsWith('.SelectDirectory'))value='C:\\样本目录';
  if(name.endsWith('.Scan'))value=session='qa-2';
  if(name.endsWith('.DirectoryView')){calls.push(args.slice(1));const total=args[1]===1?1000003:1,offset=Math.min(args[2],total),count=args[3];const items=[];for(let i=offset;i<Math.min(total,offset+count);i++){items.push(args[1]!==1?{id:20,name:'会议记录.txt',directory:false,link:false}:i===0?rootItems[0]:i===1?rootItems[6]:{id:i+10,name:i===total-1?'最后一项.txt':'条目-'+i+'.txt',directory:false,link:false})}value={session,total,offset,items,breadcrumbs:[{id:1,name:'样本目录'},...(args[1]!==1?[{id:2,name:'项目资料'}]:[])]}}
  if(name.endsWith('.TreeView')){const opened=args[3]?!args[2].includes(1):args[1].includes(1);const total=opened?(args[3]?10002:10001):1,offset=Math.min(args[4],total),count=args[5];const items=[];for(let i=offset;i<Math.min(total,offset+count);i++)items.push({id:i===0?1:i+1,name:i===0?'样本目录':i===1?'项目资料':'目录-'+i,directory:true,link:false,depth:i===0?0:1,expanded:i===0&&opened});value={session,total,offset,items,maxDepth:1}}
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));return;
 }
 const file=path==='/'?'index.html':path.slice(1);if(!['index.html','main.js','virtual.js','style.css'].includes(file)){res.writeHead(404).end();return}
 res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html; charset=utf-8');let data=await readFile(resolve(root,file));if(file==='index.html')data=Buffer.from(data.toString().replace('</body>','<script type=\"module\" src=\"/qa-driver.js\"></script></body>'));res.end(data);
 }catch(e){res.writeHead(500).end('test error')}});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const driver=`import '/main.js';
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn,label){for(let i=0;i<150;i++){if(fn())return;await wait(30)}throw Error(label+' | rect='+JSON.stringify([...document.querySelectorAll('#list-space .file-row')].slice(-3).map(n=>[n.dataset.index,n.getBoundingClientRect().top]))+' scroll='+document.querySelector('#list-scroll').scrollTop+' | '+document.querySelector('#error').textContent+' | '+document.querySelector('#item-info').textContent)}
try{
await until(()=>document.querySelectorAll('#list-space .file-row').length>0,'initial rows');
if(window.PWNED||document.querySelectorAll('#list-space img').length)throw Error('unsafe name rendering');
if(!document.querySelector('#error').hidden)throw Error('UI error: '+document.querySelector('#error').textContent);
if(document.querySelector('#previous')||document.querySelector('#next')||document.querySelector('.more-tree'))throw Error('paging control remains');
if(!document.querySelector('#item-info').textContent.includes('1,000,003'))throw Error('incorrect total');
document.querySelector('#jump').value='1000003';document.querySelector('#jump-go').click();
await until(()=>document.querySelector('#list-space').textContent.includes('最后一项.txt'),'seek to millionth item');
if(document.querySelectorAll('#list-space .file-row').length>100)throw Error('unbounded file DOM');
const probe=document.querySelector('#list-space [data-index="999998"]');const before=probe.getBoundingClientRect().top;
document.querySelector('#list-scroll').dispatchEvent(new WheelEvent('wheel',{deltaY:-36,cancelable:true}));await until(()=>{const row=document.querySelector('#list-space [data-index="999998"]');return row&&Math.abs(row.getBoundingClientRect().top-before-36)<1},'wheel render');
const after=document.querySelector('#list-space [data-index="999998"]').getBoundingClientRect().top;
if(Math.abs(after-before-36)>1)throw Error('scaled wheel skipped logical rows '+(after-before));
document.querySelector('#list-scroll').dispatchEvent(new KeyboardEvent('keydown',{key:'Home',bubbles:true,cancelable:true}));
await until(()=>document.querySelector('#list-space .directory'),'return to first item');
document.querySelector('#list-space .directory').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));
await until(()=>document.querySelector('#list-space').textContent.includes('会议记录.txt'),'folder navigation');
document.querySelector('#breadcrumbs button').click();
await until(()=>document.querySelector('#list-space .directory'),'breadcrumbs');
document.querySelector('#tree').dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true,cancelable:true}));
await until(()=>document.querySelector('#tree-space [data-index="10000"]'),'last of 10000 folders');
if(document.querySelectorAll('#tree-space .tree-row').length>100)throw Error('unbounded tree DOM');
document.querySelector('#expand-all').click();await wait(100);document.querySelector('#collapse-all').click();
await until(()=>document.querySelectorAll('#tree-space .tree-row').length===1,'collapse all');
await fetch('/result',{method:'POST',body:JSON.stringify({passed:['no pages','millionth row','exact wheel','folder navigation','breadcrumbs','10000 folders','bounded DOM','collapse all','safe text'],width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,footerBottom:document.querySelector('footer').getBoundingClientRect().bottom,paneHeight:document.querySelector('.explorer').getBoundingClientRect().height})});
}catch(e){await fetch('/result',{method:'POST',body:JSON.stringify({error:e.message})})}
`;

try{
for(const [width,height,name] of [[1240,820,'ready'],[900,610,'small']]){
 const profile=await mkdtemp(resolve('.review/wails-ui-qa-'));
 const shot=resolve('.review/wails-browser-virtual-'+name+'.png');
 const chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--disable-gpu','--no-first-run','--disable-background-networking','--disable-component-update','--disable-sync','--force-device-scale-factor=1','--user-data-dir='+profile,'--window-size='+width+','+height,'--virtual-time-budget=30000','--screenshot='+shot,'http://127.0.0.1:'+server.address().port],{windowsHide:true,stdio:['ignore','ignore','pipe']});
 let errors='';chrome.stderr.on('data',d=>{errors+=d.toString()});
 await new Promise((r,j)=>{const timer=setTimeout(()=>{chrome.kill();j(Error('headless timeout'))},25000);chrome.on('exit',code=>{clearTimeout(timer);code===0?r():j(Error('headless exit '+code+' '+errors.slice(-1000)))})});
}
if(results.length!==2 || results.some(r=>r.error||r.scrollWidth>r.width||r.footerBottom>r.height+1))throw Error("UI assertions failed");
}finally{server.close()}
