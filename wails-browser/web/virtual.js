// A viewport represents the complete logical collection. CSS scroll height is
// bounded because Chromium cannot represent hundreds of millions of pixels.
// Wheel/keyboard navigation retains an exact logical position at that scale.
export function geometry(total, height, rowHeight, top=0) {
 const visible=Math.max(1,Math.ceil(height/rowHeight));
 const maxTop=Math.max(0,total-height/rowHeight);
 const extent=Math.max(height,Math.min(8_000_000,total*rowHeight));
 const position=Math.max(0,Math.min(top,maxTop));
 return {visible,maxTop,extent,position,scroll:maxTop?position/maxTop*(extent-height):0};
}
export class VirtualView {
 constructor(element,space,rowHeight,fetchRange,renderRow,onChange,onError){
  Object.assign(this,{element,space,rowHeight,fetchRange,renderRow,onChange,onError});
  this.total=0;this.top=0;this.epoch=0;this.start=-1;this.rows=[];this.busy=false;this.expected=null;this.ready=false;this.enabled=false;
  element.addEventListener('scroll',()=>{const y=element.scrollTop;if(this.expected!==null&&Math.abs(y-this.expected)<1){this.expected=null;return}this.expected=null;const g=this.geom();this.top=g.extent>element.clientHeight?y/(g.extent-element.clientHeight)*g.maxTop:0;this.schedule()});
  element.addEventListener('wheel',e=>{e.preventDefault();const scale=e.deltaMode===1?rowHeight:e.deltaMode===2?element.clientHeight:1;this.go(this.top+e.deltaY*scale/rowHeight)},{passive:false});
  element.addEventListener('keydown',e=>{const g=this.geom(),moves={ArrowDown:1,ArrowUp:-1,PageDown:g.visible,PageUp:-g.visible};if(e.key in moves){e.preventDefault();this.go(this.top+moves[e.key])}else if(e.key==='Home'||e.key==='End'){e.preventDefault();this.go(e.key==='Home'?0:g.maxTop)}});
  new ResizeObserver(()=>{this.sync();this.schedule()}).observe(element);
 }
 geom(){return geometry(this.total,this.element.clientHeight,this.rowHeight,this.top)}
 sync(){const g=this.geom();this.top=g.position;this.space.style.height=g.extent+'px';this.element.scrollTop=g.scroll;this.expected=this.element.scrollTop}
 go(top){this.top=top;this.sync();this.schedule()}
 reset(preserve=false){this.enabled=true;this.epoch++;this.ready=false;this.start=-1;this.rows=[];this.space.replaceChildren();if(!preserve)this.top=0;this.sync();this.schedule()}
 clear(){this.enabled=false;this.epoch++;this.ready=false;this.total=0;this.top=0;this.start=-1;this.rows=[];this.space.replaceChildren();this.sync()}
 schedule(){if(!this.enabled||this.frame)return;this.frame=requestAnimationFrame(()=>{this.frame=0;this.draw()})}
 async draw(){
  if(!this.enabled)return;
  const g=this.geom();let start=Math.max(0,Math.floor(this.top)-8),end=Math.min(this.total,Math.ceil(this.top+g.visible)+8);
  if(!this.ready||start<this.start||end>this.start+this.rows.length){
   if(this.busy)return;
   const epoch=this.epoch;this.busy=true;
   try{const result=await this.fetchRange(start,Math.min(1024,g.visible+18));if(epoch!==this.epoch)return;
    this.total=result.total;this.start=result.offset;this.rows=result.items;this.ready=true;this.sync();this.onChange(result);
   }catch(e){if(epoch===this.epoch){this.ready=true;this.rows=[];this.start=0;this.total=0;this.sync();this.onError(e)}}
   finally{this.busy=false;this.schedule()}
   return;
  }
  this.space.replaceChildren();
  for(let i=start;i<end;i++){const item=this.rows[i-this.start];if(!item)continue;const row=this.renderRow(item,i);row.classList.add('virtual-row');row.style.top=(this.element.scrollTop+(i-this.top)*this.rowHeight)+'px';row.style.height=this.rowHeight+'px';this.space.append(row)}
 }
}
