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
 res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html; charset=utf-8');let data=await readFile(resolve(root,file));res.end(data);
 }catch(e){res.writeHead(500).end('test error')}});

const driver="\nconst wait=ms=>new Promise(r=>setTimeout(r,ms));\nasync function until(fn,label){for(let i=0;i<150;i++){if(fn())return;await wait(30)}throw Error(label+' | rect='+JSON.stringify([...document.querySelectorAll('#list-space .file-row')].slice(-3).map(n=>[n.dataset.index,n.getBoundingClientRect().top]))+' scroll='+document.querySelector('#list-scroll').scrollTop+' | '+document.querySelector('#error').textContent+' | '+document.querySelector('#item-info').textContent)}\ntry{\nawait until(()=>document.querySelectorAll('#list-space .file-row').length>0,'initial rows');\nif(window.PWNED||document.querySelectorAll('#list-space img').length)throw Error('unsafe name rendering');\nif(!document.querySelector('#error').hidden)throw Error('UI error: '+document.querySelector('#error').textContent);\nif(document.querySelector('#previous')||document.querySelector('#next')||document.querySelector('.more-tree'))throw Error('paging control remains');\nif(!document.querySelector('#item-info').textContent.includes('1,000,003'))throw Error('incorrect total');\ndocument.querySelector('#jump').value='1000003';document.querySelector('#jump-go').click();\nawait until(()=>document.querySelector('#list-space').textContent.includes('最后一项.txt'),'seek to millionth item');\nif(document.querySelectorAll('#list-space .file-row').length>100)throw Error('unbounded file DOM');\nconst probe=document.querySelector('#list-space [data-index=\"999998\"]');const before=probe.getBoundingClientRect().top;\ndocument.querySelector('#list-scroll').dispatchEvent(new WheelEvent('wheel',{deltaY:-36,cancelable:true}));await until(()=>{const row=document.querySelector('#list-space [data-index=\"999998\"]');return row&&Math.abs(row.getBoundingClientRect().top-before-36)<1},'wheel render');\nconst after=document.querySelector('#list-space [data-index=\"999998\"]').getBoundingClientRect().top;\nif(Math.abs(after-before-36)>1)throw Error('scaled wheel skipped logical rows '+(after-before));\ndocument.querySelector('#list-scroll').dispatchEvent(new KeyboardEvent('keydown',{key:'Home',bubbles:true,cancelable:true}));\nawait until(()=>document.querySelector('#list-space .directory'),'return to first item');\ndocument.querySelector('#list-space .directory').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));\nawait until(()=>document.querySelector('#list-space').textContent.includes('会议记录.txt'),'folder navigation');\ndocument.querySelector('#breadcrumbs button').click();\nawait until(()=>document.querySelector('#list-space .directory'),'breadcrumbs');\ndocument.querySelector('#tree').dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true,cancelable:true}));\nawait until(()=>document.querySelector('#tree-space [data-index=\"10000\"]'),'last of 10000 folders');\nif(document.querySelectorAll('#tree-space .tree-row').length>100)throw Error('unbounded tree DOM');\ndocument.querySelector('#expand-all').click();await wait(100);document.querySelector('#collapse-all').click();\nawait until(()=>document.querySelectorAll('#tree-space .tree-row').length===1,'collapse all');\nawait fetch('/result',{method:'POST',body:JSON.stringify({passed:['no pages','millionth row','exact wheel','folder navigation','breadcrumbs','10000 folders','bounded DOM','collapse all','safe text'],width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,footerBottom:document.querySelector('footer').getBoundingClientRect().bottom,paneHeight:document.querySelector('.explorer').getBoundingClientRect().height})});\n}catch(e){await fetch('/result',{method:'POST',body:JSON.stringify({error:e.message})})}\n";

await new Promise(r=>server.listen(0,'127.0.0.1',r));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const profile=await mkdtemp(resolve('.review/wails-virtual-cdp-'));
const child=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--disable-gpu','--no-first-run','--disable-background-networking','--disable-component-update','--disable-sync','--disable-renderer-backgrounding','--disable-background-timer-throttling','--enable-automation','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
let ws;
try{
 let tabs;for(let i=0;i<100;i++){try{const port=(await readFile(resolve(profile,'DevToolsActivePort'),'utf8')).split('\n')[0].trim();tabs=await fetch('http://127.0.0.1:'+port+'/json/list',{signal:AbortSignal.timeout(1500)}).then(r=>r.json());break}catch{await sleep(100)}}
 if(!tabs)throw Error('browser not available');
 ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);await new Promise((r,j)=>{ws.onopen=r;ws.onerror=j});
 let seq=0;const pending=new Map();
 ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);if(m.error)p?.reject(m.error);else p?.resolve(m.result)}};
 const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;const timer=setTimeout(()=>reject(Error('timeout '+method)),20000);pending.set(id,{resolve:r=>{clearTimeout(timer);resolve(r)},reject:e=>{clearTimeout(timer);reject(e)}});ws.send(JSON.stringify({id,method,params}))});
 await send('Page.enable');await send('Runtime.enable');
 for(const [width,height,name]of [[1240,820,'ready'],[900,610,'small']]){
  await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
  await send('Page.navigate',{url:'http://127.0.0.1:'+server.address().port});await sleep(1500);
  console.log(await send('Runtime.evaluate',{expression:'(async()=>{'+driver+'})()',awaitPromise:true,returnByValue:true}));
  const shot=await send('Page.captureScreenshot',{format:'png'});await writeFile('.review/wails-browser-virtual-'+name+'.png',Buffer.from(shot.data,'base64'));
  
 }
 if(results.length!==2||results.some(r=>r.error||r.scrollWidth>r.width||r.footerBottom>r.height+1))throw Error('virtual UI validation failed');
 ws.send(JSON.stringify({id:++seq,method:'Browser.close'}));await sleep(200);
}finally{ws?.close();child.kill();server.close()}



