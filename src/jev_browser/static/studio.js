const routes = {
  '/': {id:'longseq', title:'LongSeq', module:'/app.js'},
  '/ultrafast': {id:'ultrafast', title:'Ultrafast', module:'/original.js'},
  '/research': {id:'research', title:'Autoresearch', module:'/research.js'},
};
const views = new Map(), loading = new Map();
const initial = document.getElementById('initial-view');
const status = document.getElementById('studio-status');
let active = '', request = 0;

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
async function navigate(value, {push=true, remember=false}={}) {
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
    window.scrollTo({top:entry.scroll,behavior:'instant'});
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
  navigate(url, {remember:link.matches('.studio-nav a')});
});
window.addEventListener('popstate', ()=>navigate(location.href,{push:false}));
navigate(location.href, {push:false});
