# -*- coding: utf-8 -*-
"""MovieArk 三主题系统：Design Token + Theme Provider。

- 三个主题：fresh(清新护眼) / business(商务专业) / cute(可爱风格)
- 通过 <html data-theme="..."> 切换，仅改变视觉，不触发业务数据请求
- 用户选择保存在 localStorage(movieark_theme)，未选择时跟随系统 prefers-color-scheme
"""

TOKEN_NAMES = """--bg-primary --bg-secondary --bg-tertiary --surface-primary --surface-secondary --surface-hover
--sidebar-bg --sidebar-border --text-primary --text-secondary --text-muted --text-disabled
--border-primary --border-secondary --accent-primary --accent-secondary --accent-hover --accent-soft
--success --success-bg --warning --warning-bg --danger --danger-bg --info --info-bg
--button-primary-bg --button-primary-text --button-secondary-bg --button-secondary-text
--input-bg --input-border --input-focus --card-bg --card-border --card-shadow
--modal-bg --overlay-bg --scrollbar-track --scrollbar-thumb
--bg-deep --panel-deep --hover-bg --hover-line --chip-bg --chip-hover --chip-text
--state-bg --state-line --skeleton-a --skeleton-b --score-bg --score-text --badge-bg --badge-text
--ok --ok-bg --ok-line --bad --bad-bg --bad-line --warn --placeholder --hero-shade
--bg --panel --panel2 --card --line --gold --cyan --text --muted --danger --green --red"""

# 每套主题的 token 值（canonical + legacy 别名）
_THEMES = {
    "fresh": {
        "bg-primary": "#F6F8F5", "bg-secondary": "#F8FAF8", "bg-tertiary": "#EEF3ED",
        "surface-primary": "#FFFFFF", "surface-secondary": "#FAFCFA", "surface-hover": "#F0F4EF",
        "sidebar-bg": "#EDF2EB", "sidebar-border": "#DCE5D8",
        "text-primary": "#1F2933", "text-secondary": "#48555F", "text-muted": "#68737D", "text-disabled": "#9AA5AD",
        "border-primary": "#DCE5D8", "border-secondary": "#E8EEE6",
        "accent-primary": "#4C9A68", "accent-secondary": "#2FA3A6", "accent-hover": "#3E8457", "accent-soft": "#E4F1E7",
        "success": "#3D9A5F", "success-bg": "#E6F4EA", "warning": "#D98E4A", "warning-bg": "#FBF0E4",
        "danger": "#D45D5D", "danger-bg": "#FBE8E8", "info": "#3A8FB7", "info-bg": "#E7F1F7",
        "button-primary-bg": "#4C9A68", "button-primary-text": "#FFFFFF",
        "button-secondary-bg": "#FFFFFF", "button-secondary-text": "#1F2933",
        "input-bg": "#FFFFFF", "input-border": "#D5DFD2", "input-focus": "#4C9A68",
        "card-bg": "#FFFFFF", "card-border": "#E0E7DD", "card-shadow": "0 6px 18px rgba(31,41,51,.08)",
        "modal-bg": "#FFFFFF", "overlay-bg": "rgba(23,33,41,.45)",
        "scrollbar-track": "#EEF3ED", "scrollbar-thumb": "#C6D2C1",
        "bg-deep": "#EEF3ED", "panel-deep": "#F2F6F1", "hover-bg": "#E4F1E7", "hover-line": "#4C9A68",
        "chip-bg": "#EDF3EB", "chip-hover": "#4C9A68", "chip-text": "#55645A",
        "state-bg": "#F2F6F1", "state-line": "#DCE5D8",
        "skeleton-a": "#ECF1EA", "skeleton-b": "#DFE8DC",
        "score-bg": "rgba(255,255,255,.92)", "score-text": "#B8860B",
        "badge-bg": "#E4F1E7", "badge-text": "#3D7A55",
        "ok": "#3D9A5F", "ok-bg": "#E6F4EA", "ok-line": "#2E7D43",
        "bad": "#D45D5D", "bad-bg": "#FBE8E8", "bad-line": "#C94F4F",
        "warn": "#D98E4A", "placeholder": "#9AA5AD",
        "hero-shade": "linear-gradient(90deg,rgba(246,248,245,.92) 5%,rgba(246,248,245,.7) 42%,transparent 75%),linear-gradient(0deg,rgba(246,248,245,.9) 0,transparent 50%)",
        # legacy 别名（供各页面 var(--x) 使用）
        "bg": "#F6F8F5", "panel": "#FFFFFF", "panel2": "#F2F6F1", "card": "#FFFFFF",
        "line": "#DCE5D8", "gold": "#C88A2D", "cyan": "#2FA3A6", "text": "#1F2933",
        "muted": "#68737D", "danger": "#D45D5D", "green": "#3D9A5F", "red": "#D45D5D",
    },
    "business": {
        "bg-primary": "#0F1722", "bg-secondary": "#131C2B", "bg-tertiary": "#182333",
        "surface-primary": "#17212F", "surface-secondary": "#1C2838", "surface-hover": "#223146",
        "sidebar-bg": "#0B1220", "sidebar-border": "#223049",
        "text-primary": "#E6EBF2", "text-secondary": "#B7C2D4", "text-muted": "#8A97AB", "text-disabled": "#5C6A80",
        "border-primary": "#24344C", "border-secondary": "#1E2C42",
        "accent-primary": "#3D8BFF", "accent-secondary": "#22C4DC", "accent-hover": "#5C9EFF", "accent-soft": "#122A4A",
        "success": "#3FBF7F", "success-bg": "#12311F", "warning": "#E8A23C", "warning-bg": "#3A2C12",
        "danger": "#E05B5B", "danger-bg": "#3A1A1C", "info": "#35A8D9", "info-bg": "#12293A",
        "button-primary-bg": "#3D8BFF", "button-primary-text": "#FFFFFF",
        "button-secondary-bg": "#1C2838", "button-secondary-text": "#E6EBF2",
        "input-bg": "#101A29", "input-border": "#2A3B57", "input-focus": "#3D8BFF",
        "card-bg": "#17212F", "card-border": "#22334D", "card-shadow": "0 8px 24px rgba(0,0,0,.35)",
        "modal-bg": "#182333", "overlay-bg": "rgba(3,7,14,.62)",
        "scrollbar-track": "#101A29", "scrollbar-thumb": "#2E405E",
        "bg-deep": "#0B1220", "panel-deep": "#101A29", "hover-bg": "#122A4A", "hover-line": "#5C9EFF",
        "chip-bg": "#182338", "chip-hover": "#3D8BFF", "chip-text": "#A8B6C9",
        "state-bg": "#101A29", "state-line": "#24344C",
        "skeleton-a": "#16202F", "skeleton-b": "#22314A",
        "score-bg": "rgba(10,15,25,.85)", "score-text": "#FFD456",
        "badge-bg": "#075863", "badge-text": "#D6FBFF",
        "ok": "#3FBF7F", "ok-bg": "#12311F", "ok-line": "#2E8B57",
        "bad": "#E05B5B", "bad-bg": "#3A1A1C", "bad-line": "#E05B5B",
        "warn": "#E8A23C", "placeholder": "#5C6A80",
        "hero-shade": "linear-gradient(90deg,#090907 5%,#090907b5 42%,transparent 75%),linear-gradient(0deg,#090907 0,transparent 50%)",
        "bg": "#0F1722", "panel": "#17212F", "panel2": "#1C2838", "card": "#17212F",
        "line": "#22334D", "gold": "#C9A35C", "cyan": "#22C4DC", "text": "#E6EBF2",
        "muted": "#8A97AB", "danger": "#E05B5B", "green": "#3FBF7F", "red": "#E05B5B",
    },
    "cute": {
        "bg-primary": "#FFF9FB", "bg-secondary": "#FAF7FF", "bg-tertiary": "#F5F0FB",
        "surface-primary": "#FFFFFF", "surface-secondary": "#FDF9FD", "surface-hover": "#F9EFF7",
        "sidebar-bg": "#F6ECF6", "sidebar-border": "#EEDDF0",
        "text-primary": "#4A3F5C", "text-secondary": "#6D5F85", "text-muted": "#9688AC", "text-disabled": "#BCAFCD",
        "border-primary": "#E9DCEF", "border-secondary": "#F1E8F5",
        "accent-primary": "#F48FB1", "accent-secondary": "#B39DDB", "accent-hover": "#E9739C", "accent-soft": "#FDEAF2",
        "success": "#6FBF8F", "success-bg": "#E6F5EC", "warning": "#F0A45D", "warning-bg": "#FCF0E3",
        "danger": "#EF7E8C", "danger-bg": "#FDEAEC", "info": "#7FB3E8", "info-bg": "#EBF2FC",
        "button-primary-bg": "#F48FB1", "button-primary-text": "#FFFFFF",
        "button-secondary-bg": "#FFFFFF", "button-secondary-text": "#4A3F5C",
        "input-bg": "#FFFFFF", "input-border": "#E8DAEF", "input-focus": "#F48FB1",
        "card-bg": "#FFFFFF", "card-border": "#EDE2F1", "card-shadow": "0 8px 22px rgba(180,130,200,.18)",
        "modal-bg": "#FFFFFF", "overlay-bg": "rgba(90,60,110,.42)",
        "scrollbar-track": "#F6F0F8", "scrollbar-thumb": "#DCC8E6",
        "bg-deep": "#F4EAF6", "panel-deep": "#F8F0FA", "hover-bg": "#FDEAF2", "hover-line": "#F48FB1",
        "chip-bg": "#F8EFF7", "chip-hover": "#F48FB1", "chip-text": "#7A6B92",
        "state-bg": "#F8F0FA", "state-line": "#E9DCEF",
        "skeleton-a": "#F4EAF6", "skeleton-b": "#EBDCF0",
        "score-bg": "rgba(255,255,255,.92)", "score-text": "#D982A5",
        "badge-bg": "#F3E8FA", "badge-text": "#A074C6",
        "ok": "#6FBF8F", "ok-bg": "#E6F5EC", "ok-line": "#5CA87B",
        "bad": "#EF7E8C", "bad-bg": "#FDEAEC", "bad-line": "#E56B7D",
        "warn": "#F0A45D", "placeholder": "#BCAFCD",
        "hero-shade": "linear-gradient(90deg,rgba(255,249,251,.92) 5%,rgba(255,249,251,.72) 42%,transparent 75%),linear-gradient(0deg,rgba(255,249,251,.9) 0,transparent 50%)",
        "bg": "#FFF9FB", "panel": "#FFFFFF", "panel2": "#F8F2FA", "card": "#FFFFFF",
        "line": "#E9DCEF", "gold": "#E8A0B8", "cyan": "#9BB8F0", "text": "#4A3F5C",
        "muted": "#9688AC", "danger": "#EF7E8C", "green": "#6FBF8F", "red": "#EF7E8C",
    },
}


def _css_block() -> str:
    base = "".join(f"--{k}:{v};" for k, v in _THEMES["business"].items())
    fresh = "".join(f"--{k}:{v};" for k, v in _THEMES["fresh"].items())
    cute = "".join(f"--{k}:{v};" for k, v in _THEMES["cute"].items())
    return (
        ":root{" + base + "}"
        ":root[data-theme='fresh']{" + fresh + "}"
        ":root[data-theme='cute']{" + cute + "}"
    )


def _js_block() -> str:
    return r"""
(function(){
  var THEMES={fresh:'🌿 \u6e05\u65b0',business:'💼 \u5546\u52a1',cute:'🐰 \u53ef\u7231'};
  var KEY='movieark_theme',saved=null;
  try{saved=localStorage.getItem(KEY)}catch(e){}
  if(!saved||!THEMES[saved]){
    saved=(window.matchMedia&&matchMedia('(prefers-color-scheme: light)').matches)?'fresh':'business';
  }
  document.documentElement.setAttribute('data-theme',saved);
  function apply(t){
    document.documentElement.setAttribute('data-theme',t);
    try{localStorage.setItem(KEY,t)}catch(e){}
  }
  function mount(){
    document.querySelectorAll('[data-theme-switch]').forEach(function(box){
      if(box.dataset.built)return;box.dataset.built='1';
      var html='';
      Object.keys(THEMES).forEach(function(k){
        html+='<button class="tsw" data-t="'+k+'" title="'+THEMES[k]+'"'+(k===saved?' aria-pressed="true"':'')+'>'+THEMES[k]+'</button>';
      });
      box.innerHTML=html;
      box.querySelectorAll('.tsw').forEach(function(btn){
        btn.onclick=function(){
          saved=btn.dataset.t;apply(saved);
          document.querySelectorAll('[data-theme-switch] .tsw').forEach(function(b){
            b.setAttribute('aria-pressed',b===btn?'true':'false');
          });
        };
      });
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);
  else mount();
  if(!saved&&window.matchMedia){
    matchMedia('(prefers-color-scheme: light)').addEventListener('change',function(e){
      var cur=null;try{cur=localStorage.getItem(KEY)}catch(x){}
      if(!cur)apply(e.matches?'fresh':'business');
    });
  }
})();
"""


def _switcher_css() -> str:
    return """
.theme-switch{display:flex;gap:6px;margin-top:16px;padding:2px 6px;flex-wrap:wrap}
.theme-switch .tsw{flex:1;min-width:0;border:1px solid var(--border-primary);background:var(--surface-secondary);color:var(--text-secondary);padding:7px 3px;border-radius:10px;font-size:11.5px;line-height:1;cursor:pointer;white-space:nowrap;transition:border-color .15s,color .15s,background .15s}
.theme-switch .tsw[aria-pressed="true"],.theme-switch .tsw:hover{background:var(--accent-soft);color:var(--accent-primary);border-color:var(--accent-primary)}
"""


def theme_head() -> str:
    return (
        "<style>" + _css_block() + _switcher_css()
        + "*{scrollbar-width:thin;scrollbar-color:var(--scrollbar-thumb) var(--scrollbar-track)}"
        + "*::-webkit-scrollbar{width:10px;height:10px}*::-webkit-scrollbar-track{background:var(--scrollbar-track)}"
        + "*::-webkit-scrollbar-thumb{background:var(--scrollbar-thumb);border-radius:6px}"
        + "</style>"
        + "<script>" + _js_block() + "</script>"
    )
