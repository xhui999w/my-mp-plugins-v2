# -*- coding: utf-8 -*-
"""MovieArk 共享 UI 组件：侧边栏 / Provider 图标 / 统一影视卡片。

所有 Web 页面复用这里的侧边栏、顶部 Provider 图标与海报卡片结构，
保证后续修改一次即可同步到「榜单 / 汇影 / 搜索」等页面。
"""

SIDEBAR_NAV = (
    ("发现", (("search", "/search", "\u2315", "搜索"),
              ("rankings", "/rankings", "\U0001F3C6", "榜单"),
              ("explore", "/explore", "\u25C9", "汇影"))),
    ("订阅", (("subscriptions", "/subscriptions", "\u2637", "订阅列表"),
              ("tasks", "/tasks", "\u25A3", "订阅任务"),
              ("unlocks", "/unlocks", "\u25F4", "解锁记录"))),
    ("系统", (("authorizations", "/authorizations", "\U0001F511", "授权中心"),
              ("settings", "/settings", "\U0001F4E5", "转存配置"),
              ("telegram", "/telegram", "\u2708", "Telegram"),
              ("logs", "/logs", "\u25A4", "日志中心"))),
)


def sidebar_html(active: str) -> str:
    """生成左侧导航。active 传当前页面 key（search/rankings/explore/...）。"""
    parts = ['<div class="logo"><svg viewBox="0 0 512 512" width="22" height="22" style="vertical-align:-5px;margin-right:7px;border-radius:5px" aria-hidden="true"><defs><linearGradient id="maLogoGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#E11D48"/><stop offset="1" stop-color="#F59E0B"/></linearGradient></defs><rect width="512" height="512" rx="112" fill="url(#maLogoGrad)"/><circle cx="256" cy="236" r="118" fill="none" stroke="#fff" stroke-width="16" opacity=".95"/><path d="M236 190 L320 236 L236 282 Z" fill="#fff"/></svg>影舟 MovieArk</div>']
    for group, items in SIDEBAR_NAV:
        parts.append(f'<div class="group">{group}</div>')
        for key, href, icon, label in items:
            cls = "nav active" if key == active else "nav"
            parts.append(f'<a class="{cls}" href="{href}"><span class="nav-ico">{icon}</span>{label}</a>')
    parts.append('<div class="theme-switch" data-theme-switch></div>')
    return '<aside class="side">' + "".join(parts) + "</aside>"


PROVIDER_ICONS_JS = r"""
var PROVIDER_ICONS={
tmdb:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="14" height="11" rx="2"/><path d="M3 8.5h14"/><path d="m5.5 5.5 3.5 3"/><path d="m11 5.5 3.5 3"/></svg>',
douban:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="3.5" width="12" height="13" rx="1.5"/><path d="M10 3.5v13"/><path d="M4 8h12"/></svg>',
netflix:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="7"/><path d="m8.3 7.4 4.6 2.6-4.6 2.6z"/></svg>',
max:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.2" y="5" width="13.6" height="9.4" rx="1.6"/><path d="M8.2 18h3.6"/></svg>',
prime:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7l6-3 6 3v6l-6 3-6-3z"/><path d="M4 7l6 3 6-3"/><path d="M10 10v6"/></svg>',
disney:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2.8l1.9 5.3 5.3 1.9-5.3 1.9L10 17.2l-1.9-5.3-5.3-1.9 5.3-1.9z"/></svg>',
apple:'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.2" y="5" width="13.6" height="9.4" rx="1.6"/><path d="m9.4 7.6 3.4 2-3.4 2z"/><path d="M8.2 18h3.6"/></svg>'
};
function providerIcon(id){return PROVIDER_ICONS[id]||'<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="10" cy="10" r="6.5"/></svg>'}
"""

MEDIA_CARD_CSS = r"""
.card{position:relative;min-width:0;background:var(--card-bg);border:1px solid var(--card-border);border-radius:11px;overflow:hidden;cursor:pointer;transition:transform .18s,border-color .18s,box-shadow .18s}.card:hover{transform:translateY(-3px);border-color:var(--hover-line);box-shadow:var(--card-shadow)}.thumb{position:relative;overflow:hidden}.poster{aspect-ratio:2/3;width:100%;object-fit:cover;display:block;background:var(--surface-secondary);transition:transform .22s ease}.card:hover .poster{transform:scale(1.025)}.shade{position:absolute;left:0;right:0;bottom:0;height:42%;background:linear-gradient(180deg,transparent,rgba(8,10,14,.42));pointer-events:none}.badge,.score,.rank{position:absolute;top:7px;padding:4px 7px;border-radius:10px;font-size:11.5px;font-weight:700;z-index:2}.badge{left:7px;background:var(--badge-bg);color:var(--badge-text)}.score{right:7px;background:var(--score-bg);color:var(--score-text)}.rank{left:7px;background:rgba(10,12,16,.5);color:var(--rank,#fff)}.cover-actions{position:absolute;left:8px;right:8px;bottom:8px;display:flex;justify-content:space-between;align-items:center;z-index:3}.act{flex:none;width:30px;height:30px;display:flex;align-items:center;justify-content:center;border:1px solid rgba(255,255,255,.42);background:rgba(12,14,18,.35);color:#fff;border-radius:9px;cursor:pointer;padding:0;transition:background .15s,transform .15s,color .15s;backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}.act svg{width:16px;height:16px}.act:hover{background:rgba(255,255,255,.24);transform:scale(1.07)}.act.subscribed{color:#ff6b92;background:rgba(255,107,146,.26);border-color:rgba(255,107,146,.55)}.act.subscribed svg{fill:currentColor}.meta{padding:8px 9px 10px}.title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12.5px;font-weight:600;color:var(--text-primary)}.sub{color:var(--text-muted);font-size:10.5px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}@media(hover:hover) and (pointer:fine){.cover-actions{opacity:0;transition:opacity .18s}.card:hover .cover-actions,.card:focus-within .cover-actions{opacity:1}}@media(max-width:850px){.cover-actions{opacity:1}.act{width:34px;height:34px}}
"""

MEDIA_CARD_JS = r"""
const MAG_ICON='<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="9" cy="9" r="5.2"/><path d="m13 13 4 4"/></svg>',HEART_ICON='<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M10 16.6S3.4 12.7 3.4 8.3A3.5 3.5 0 0 1 10 6.2a3.5 3.5 0 0 1 6.6 2.1c0 4.4-6.6 8.3-6.6 8.3z"/></svg>';
function ph(){return "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='260' height='390'%3E%3Crect width='100%25' height='100%25' fill='%23211d15'/%3E%3Ctext x='50%25' y='50%25' text-anchor='middle' fill='%238f826d'%3E%E6%9A%82%E6%97%A0%E6%B5%B7%E6%8A%A5%3C/text%3E%3C/svg%3E"}
function cardHTML(x,o){o=o||{};const media=x.media_type==='tv'?'电视剧':'电影',sub=(x.countries||[]).length?' · '+esc((x.countries||[]).join(' ')):'';return `<article class="card" data-json="${encodeURIComponent(JSON.stringify(x))}" tabindex="0"><div class="thumb"><img class="poster" loading="lazy" src="${esc(x.poster||ph())}" onerror="this.onerror=null;this.src='${ph()}'">${o.rank?`<span class="rank">${String(o.rank).padStart(2,'0')}</span>`:`<span class="badge">${media}</span>`}<span class="score">★ ${x.rating||'-'}</span><div class="shade"></div><div class="cover-actions"><button class="act" data-action="search" title="搜索资源" aria-label="搜索资源">${MAG_ICON}</button><button class="act" data-action="subscribe" title="订阅" aria-label="订阅">${HEART_ICON}</button></div></div><div class="meta"><div class="title">${esc(x.title||'未命名')}</div><div class="sub">${esc(x.year||'年份未知')}${sub}</div></div></article>`}
function resourceQuery(x){return new URLSearchParams({media_type:x.media_type,tmdb_id:x.tmdb_id||0,douban_id:x.douban_id||'',title:x.title||'',year:x.year||'',poster:x.poster||'',backdrop:x.backdrop||'',rating:x.rating||'',overview:x.overview||'',countries:(x.countries||[]).join('、')})}
function openDetail(x){location.href='/resources?'+resourceQuery(x)}
function openResource(x){const q=resourceQuery(x);q.set('focus','resources');location.href='/resources?'+q}
var SUB_STATE=new Set();
async function loadSubState(){try{const r=await fetch('/api/web/subscriptions?tab=current&page_size=100');if(!r.ok)return;const d=await r.json();SUB_STATE=new Set((d.items||[]).map(s=>s.tmdb_id?('t'+s.tmdb_id):('d'+(s.douban_id||''))));document.querySelectorAll('.card').forEach(markSub)}catch(e){}}
function subKey(x){return x.tmdb_id?('t'+x.tmdb_id):('d'+(x.douban_id||''))}
function markSub(c){if(!c.dataset.json)return;const x=JSON.parse(decodeURIComponent(c.dataset.json)),b=c.querySelector('[data-action="subscribe"]');if(b&&SUB_STATE.has(subKey(x))){b.classList.add('subscribed');b.title='已订阅';b.setAttribute('aria-pressed','true')}}
async function subscribeToggle(x,b){if(!b)return;b.disabled=true;try{if(!x.tmdb_id&&(x.title||x.douban_id)){try{const rd=await fetch('/api/web/media/detail?'+new URLSearchParams({media_type:x.media_type,tmdb_id:0,title:x.title||'',year:x.year||''})).then(r=>r.json());if(rd&&rd.resolved_tmdb_id)x.tmdb_id=rd.resolved_tmdb_id;if(rd&&rd.season_number!==undefined)x.season=rd.season_number}catch(e){}}const r=await fetch('/api/web/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:x.title,media_type:x.media_type,tmdb_id:x.tmdb_id||0,douban_id:x.douban_id||'',season:x.season||'',year:x.year?+x.year:null,poster:x.poster||''})}),d=await r.json();if(!r.ok)throw Error(d.detail||'订阅失败');b.classList.add('subscribed');b.title='已订阅';b.setAttribute('aria-pressed','true')}catch(e){alert(e.message)}finally{b.disabled=false}}
function bindCards(){document.querySelectorAll('.poster').forEach(img=>img.onerror=()=>{img.onerror=null;img.src=ph()});document.querySelectorAll('.card').forEach(c=>{if(c.dataset.bound)return;c.dataset.bound='1';c.onclick=e=>{const x=JSON.parse(decodeURIComponent(c.dataset.json)),b=e.target.closest('[data-action]');if(b){e.stopPropagation();e.preventDefault();if(b.dataset.action==='search')openResource(x);else subscribeToggle(x,b);return}openDetail(x)};markSub(c)})}
"""


PROVIDER_NAV_CSS = r"""
.provider{display:inline-flex;align-items:center;gap:8px;border:1px solid transparent;background:transparent;color:var(--text-secondary);height:40px;padding:0 13px;border-radius:10px;font-weight:700;font-size:13px;white-space:nowrap;cursor:pointer;transition:color .15s,background .15s,border-color .15s}.provider:hover{color:var(--accent-primary);background:var(--hover-bg)}.provider.active{color:var(--accent-primary);background:var(--accent-soft);border-color:var(--accent-primary)}.provider.unconfigured{opacity:.5}.picon{flex:none;width:22px;height:22px;display:flex;align-items:center;justify-content:center;color:var(--accent-secondary)}.picon svg{width:20px;height:20px}
"""
