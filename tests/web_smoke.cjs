const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = [];
function element() {
  const e = {value:'', disabled:false, children:[], textContent:'', scrollTop:0, scrollHeight:100,
    appendChild(child) { this.children.push(child); }, focus() {}, remove() { this.removed=true; }};
  Object.defineProperty(e, 'innerText', {get(){return this.textContent;}, set(value){this.textContent=value;}});
  elements.push(e);
  return e;
}
const ids = Object.fromEntries(['sessionTag','chatBox','userInput','submitBtn'].map(id=>[id,element()]));
let sessionRequests = 0, confirms = 0;
const text = '第一行\n\n第二行：中文😀 "引号"';
const draft = {draft_id:'draft-1', details:{num_people:3,num_children:0,arrival_time:'2099-01-01 19:00:00',seat_preference:'靠窗'}};
const wire = [{type:'token',text}, {type:'reservation',draft}, {type:'done'}].map(e=>'data: '+JSON.stringify(e)+'\n\n').join('');
const bytes = new TextEncoder().encode(wire);
const storage = new Map();
const context = {
  document:{getElementById:id=>ids[id],createElement:element},
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  TextDecoder, Event:class Event { preventDefault(){} }, console,
  fetch:async(url,options={})=>{
    if(url==='/sessions') { sessionRequests++; return {ok:true,json:async()=>({session_id:'s1',session_token:'t1'})}; }
    assert.equal(options.headers.Authorization,'Bearer t1');
    if(url==='/reservations/pending') return {ok:true,json:async()=>({draft:null})};
    if(url==='/chat/stream') {
      let i=0;
      return {ok:true,body:{getReader:()=>({read:async()=> i<bytes.length?{done:false,value:bytes.slice(i,i+=3)}:{done:true}})}};
    }
    if(url==='/reservations/draft-1/confirm') {
      assert.deepEqual(JSON.parse(options.body),{confirm:true}); confirms++;
      return {ok:true,json:async()=>({order_id:77})};
    }
    throw new Error('Unexpected endpoint '+url);
  }
};
const html = fs.readFileSync(require('node:path').join(__dirname,'../web/index.html'),'utf8');
vm.createContext(context);
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
(async()=>{
  ids.userInput.value='我要预订';
  await context.handleSubmit({preventDefault(){}});
  assert.equal(sessionRequests,1,'并发初始化不能创建两个会话');
  assert(elements.some(e=>e.textContent===text),'分块接收必须完整保留换行、中文、Emoji 和引号');
  const button=elements.find(e=>e.textContent==='确认预订');
  assert(button,'必须展示明确确认按钮');
  assert.equal(confirms,0,'展示草稿不得自动提交');
  await button.onclick();
  assert.equal(confirms,1);
  assert(elements.some(e=>e.textContent==='预订成功，订单号 #77'));
  console.log('Web smoke: session initialization, byte-split SSE, draft display and explicit confirmation OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
