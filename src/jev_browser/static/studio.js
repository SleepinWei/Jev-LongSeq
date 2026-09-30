const routes = {
  '/': {id:'longseq', title:'LongSeq', module:'/app.js'},
  '/ultrafast': {id:'ultrafast', title:'Ultrafast', module:'/original.js'},
  '/research': {id:'research', title:'Autoresearch', module:'/research.js'},
};
const views = new Map(), loading = new Map();
const initial = document.getElementById('initial-view');
const status = document.getElementById('studio-status');
const layout = document.getElementById('studio-layout');
const sidebar = document.getElementById('record-sidebar');
const records = document.getElementById('record-list');
const recordSearch = document.getElementById('record-search');
const recordToggle = document.getElementById('records-toggle');
const backdrop = document.getElementById('records-backdrop');
const mobile = matchMedia('(max-width:900px)');
let active = '', request = 0;
let recordRequest = 0, recordSignature = '', recordRows = [], recordPath = '';
let desktopOpen = true;
try {desktopOpen = localStorage.getItem('studio-records-open') !== 'false';} catch {}

function sidebarOpen() {return !layout.classList.contains('is-collapsed');}
function setSidebar(open, persist=true) {
  layout.classList.toggle('is-collapsed', !open);
  recordToggle.setAttribute('aria-expanded', String(open));
  recordToggle.setAttribute('aria-label', open ? '收起运行记录' : '展开运行记录');
  sidebar.inert = !open;
  backdrop.hidden = !(mobile.matches && open);
  if (persist && !mobile.matches) {
    desktopOpen = open;
    try {localStorage.setItem('studio-records-open', String(open));} catch {}
  }
}
setSidebar(mobile.matches ? false : desktopOpen, false);
mobile.addEventListener('change', ()=>setSidebar(mobile.matches ? false : desktopOpen, false));
recordToggle.addEventListener('click', ()=>setSidebar(!sidebarOpen()));
document.getElementById('records-close').addEventListener('click', ()=>{setSidebar(false);recordToggle.focus();});
backdrop.addEventListener('click', ()=>setSidebar(false));
document.addEventListener('keydown', event=>{
  if(event.key==='Escape' && sidebarOpen() && mobile.matches){setSidebar(false);recordToggle.focus();}
});

function selection(path) {
  const url = views.get(path)?.url || new URL(location.href);
  return url.searchParams.get(path==='/research' ? 'study' : 'run') || '';
}
function recordItem({id, title, detail, href, child=false, verdict}, selected) {
  const link = document.createElement('a');
  link.href=href;
  if(child) link.className='record-child';
  if(verdict) link.dataset.verdict=verdict;
  if(selected) link.setAttribute('aria-current','page');
  const strong=document.createElement('strong'), small=document.createElement('small');
  strong.textContent=title; small.textContent=detail || id;
  link.append(strong,small);
  return link;
}
function recordSection(label) {
  const heading=document.createElement('div');
  heading.className='record-section'; heading.textContent=label;
  return heading;
}
function prettyDate(value) {
  const date=Date.parse(value || '');
  return Number.isFinite(date) ? new Date(date).toLocaleString() : '';
}
function benchmarkLabel(benchmark) {
  if(!benchmark)return null;
  if(benchmark.status==='invalid' || benchmark.data_valid===false)return ['评分无效','invalid'];
  if(benchmark.status==='pending')return ['等待判分','pending'];
  const value=Number.isFinite(benchmark.earned)&&Number.isFinite(benchmark.total)
    ? ` · ${benchmark.earned}/${benchmark.total}` : '';
  if(benchmark.strict_success===true)return [`官方通过${value}`,'passed'];
  if(benchmark.strict_success===false)return [`官方未通过${value}`,'failed'];
  return ['尚未判定','pending'];
}
function renderRecords() {
  const selected=selection(recordPath), query=recordSearch.value.trim().toLocaleLowerCase();
  const signature=JSON.stringify([recordPath,recordRows,selected,query]);
  if(signature===recordSignature)return;
  recordSignature=signature;
  const focusedHref=records.contains(document.activeElement) ? document.activeElement.closest('a')?.href : null;
  records.replaceChildren();
  const filtered=recordRows.filter(row=>`${row.id} ${row.title} ${row.detail}`.toLocaleLowerCase().includes(query));
  let section='';
  for(const row of filtered){
    if(row.section!==section){section=row.section;records.append(recordSection(section));}
    records.append(recordItem(row,row.href===location.pathname+location.search));
  }
  if(!filtered.length){const empty=document.createElement('p');empty.className='record-empty';empty.textContent=query?'没有匹配的记录。':'目前没有记录。';records.append(empty);}
  document.getElementById('record-count').textContent=`${filtered.length} 条记录`;
  const current=recordRows.find(row=>row.href===location.pathname+location.search);
  document.getElementById('studio-current').textContent=current?.title || '';
  if(focusedHref) [...records.querySelectorAll('a')].find(link=>link.href===focusedHref)?.focus({preventScroll:true});
}
async function refreshRecords() {
  if(!active)return;
  const path=active, revision=++recordRequest;
  if(path!==recordPath)recordSearch.value='';
  const settings=path==='/research'
    ? {url:'/api/studies', heading:'研究与任务'}
    : path==='/ultrafast'
    ? {url:'/api/ultrafast/runs', heading:'Ultrafast 运行'}
    : {url:'/api/runs', heading:'LongSeq 运行'};
  try {
    const response=await fetch(settings.url);
    if(!response.ok)throw Error('运行记录暂不可用');
    const data=await response.json();
    if(path!==active || revision!==recordRequest)return;
    const rows=[];
    if(path==='/research'){
      for(const item of data){
        if(!item.id)continue;
        rows.push({id:item.id,title:item.suite==='saas-bench'?'SaaS-Bench':item.suite==='public-web'?'公共网页':item.suite==='webarena'?'WebArena':'目录任务',detail:`${item.status || '未结束'} · ${item.trials ?? 0} 轮 · ${prettyDate(item.started_at) || item.id}`,href:`/research?${new URLSearchParams({study:item.id})}`,section:'研究记录'});
      }
      const study=selection(path);
      if(study && data.some(item=>item.id===study)){
        const detail=await fetch(`/api/study?${new URLSearchParams({id:study})}`);
        if(detail.ok){
          const projection=await detail.json();
          if(path!==active || revision!==recordRequest)return;
          for(const trial of projection.trials || []){
            for(const task of trial.tasks || []){
              if(!task.run_id)continue;
              const grade=benchmarkLabel(task.grade?.source?.includes('SaaS-Bench')?{...task.grade,strict_success:task.result?.strict_success}:null);
              rows.push({id:task.run_id,title:`${trial.id===0?'基线':`候选 ${trial.id}`} · ${task.id || '任务'}`,detail:`${grade?.[0] || task.result?.status || '运行中'} · ${task.run_id}`,verdict:grade?.[1],href:`/?${new URLSearchParams({run:task.run_id})}`,section:'本研究的任务轨迹',child:true});
            }
          }
        }
      }
    } else if(path==='/ultrafast'){
      for(const item of data){
        if(!item.id)continue;
        const task=item.id?.match(/(?:business|healthcare|software|teamwork|agriculture|media)_[0-9]+/)?.[0] || '';
        const grade=benchmarkLabel(item.benchmark);
        rows.push({id:item.id,title:task || String(item.prompt || item.id).split('\n')[0].slice(0,70),detail:`${grade?.[0] ? grade[0]+' · ' : ''}${prettyDate(item.started_at)} · ${item.id}`,verdict:grade?.[1],href:`/ultrafast?${new URLSearchParams({run:item.id})}`,section:'原版运行'});
      }
    } else {
      for(const item of data){
        if(!item.id)continue;
        const grade=benchmarkLabel(item.benchmark);
        rows.push({id:item.id,title:item.task_id && item.task_id!==item.id ? item.task_id : item.id,detail:`${grade?.[0] || item.status || '未结束'} · ${prettyDate(item.started_at) || item.id}`,verdict:grade?.[1],href:`/?${new URLSearchParams({run:item.id})}`,section:'执行轨迹'});
      }
    }
    recordPath=path;recordRows=rows;
    document.getElementById('record-heading').textContent=settings.heading;
    renderRecords();
  } catch(error) {
    if(path!==active || revision!==recordRequest)return;
    recordRows=[];recordPath=path;recordSignature='';renderRecords();
    const message=document.createElement('p');message.className='record-error';message.textContent=error.message;records.prepend(message);
  }
}
recordSearch.addEventListener('input',renderRecords);
document.getElementById('records-refresh').addEventListener('click',refreshRecords);
setInterval(()=>{if(active && sidebarOpen())refreshRecords();},4000);

function address(entry, value) {
  const url = new URL(value, entry.url);
  // A background refresh only updates that view's remembered selection.
  entry.url = url;
  if (active === entry.path) history.replaceState(null, '', url.pathname + url.search);
}
async function createView(path, url) {
  if (views.has(path)) return views.get(path);
  if (loading.has(path)) return loading.get(path);
  const pending = (async () => {
    const route = routes[path];
    let fragment;
    if (initial.dataset.path === path && initial.content.querySelector('main')) {
      fragment = initial.content;
    } else {
      const response = await fetch(`/views/${route.id}`);
      if (!response.ok) throw Error('无法加载视图，请重试。');
      fragment = new DOMParser().parseFromString(await response.text(), 'text/html');
    }
    const host = document.createElement('section');
    host.className = 'studio-view'; host.dataset.view = route.id; host.hidden = true;
    host.setAttribute('aria-label', `${route.title} 工作区`);
    const root = host.attachShadow({mode:'open'});
    const styles = [...fragment.querySelectorAll('link[rel=stylesheet]')].map(link => link.cloneNode(true));
    const common = document.createElement('link'); common.rel='stylesheet'; common.href='/studio-view.css';
    styles.push(common);
    const styled = styles.map(link => new Promise(resolve => {link.onload=resolve;link.onerror=resolve;}));
    root.append(...styles, fragment.querySelector('main').cloneNode(true));
    document.getElementById('studio-views').append(host);
    const entry = {path, host, root, url, scroll:0, api:null};
    try {
      const module = await import(route.module);
      await Promise.all(styled);
      entry.api = module.mount(root, {
        location: {get search() {return entry.url.search;}},
        history: {replaceState: (_state, _unused, value) => address(entry, value)},
        isActive: () => active === path,
      });
      views.set(path, entry);
      return entry;
    } catch(error) {host.remove();throw error;}
  })();
  loading.set(path, pending);
  try {return await pending;} finally {loading.delete(path);}
}
async function navigate(value, {push=true, remember=false, focusTrace=false}={}) {
  const url = new URL(value, location.href), path = url.pathname;
  if (!routes[path]) return;
  const revision = ++request;
  const previous = views.get(active);
  if (previous) previous.scroll=window.scrollY;
  const target = remember && views.has(path) ? views.get(path).url : url;
  status.textContent='正在加载工作区…';status.hidden=false;
  try {
    const entry = await createView(path, target);
    if (revision !== request) return;
    if(previous && previous!==entry) previous.api.deactivate?.();
    if (push) history.pushState(null, '', target.pathname + target.search);
    active=path;
    for (const view of views.values()) view.host.hidden=view!==entry;
    document.querySelectorAll('.studio-nav a').forEach(link=>{
      if(link.dataset.view===routes[path].id)link.setAttribute('aria-current','page');
      else link.removeAttribute('aria-current');
    });
    document.title=`${routes[path].title} · Trace Studio`;
    status.hidden=true;
    // Restore local state on tab switches; explicit record links update that view.
    if (!remember) {entry.url=target;await entry.api.navigate(target);}
    if (revision !== request) return;
    address(entry, entry.url);
    entry.api.activate();
    refreshRecords();
    if(focusTrace){
      const targetArea=entry.root.querySelector(path==='/research' ? '#study' : '.workspace');
      window.scrollTo({top:targetArea ? targetArea.getBoundingClientRect().top+window.scrollY-82 : entry.scroll,behavior:'instant'});
    } else window.scrollTo({top:entry.scroll,behavior:'instant'});
  } catch(error) {
    if(revision!==request)return;
    status.textContent=error.message || '工作区加载失败，请再次选择顶部视图。';status.hidden=false;
  }
}
document.addEventListener('click', event => {
  const link = event.composedPath().find(node=>node instanceof HTMLAnchorElement);
  if (!link || event.defaultPrevented || event.button!==0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.hasAttribute('download') || (link.target && link.target!=='_self')) return;
  const url = new URL(link.href);
  if (url.origin!==location.origin || !routes[url.pathname] || url.hash) return;
  event.preventDefault();
  if(mobile.matches && link.closest('#record-list'))setSidebar(false);
  navigate(url, {remember:link.matches('.studio-nav a'),focusTrace:!!link.closest('#record-list')});
});
window.addEventListener('popstate', ()=>navigate(location.href,{push:false}));
navigate(location.href, {push:false});
