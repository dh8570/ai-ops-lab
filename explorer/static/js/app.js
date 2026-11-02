// 라우터: 해시 라우팅(#/, #/play, #/run/<id>, #/run/<id>/<stage>)과 색상 토글.
// 화면은 파일(./data, ./content)만 읽고, 실행·토큰화 같은 '만드는' 기능만 ./api/...를 쓴다.
import { detectMode, state } from './data.js';
import * as home from './views/home.js';
import * as play from './views/play.js';
import * as runView from './views/run.js';
import * as stageView from './views/stage.js';
import { clear, el } from './ui.js';

let seq = 0;
let cleanup = null;
const THEME_KEY = 'pipeline-theme';

export function parseHash(hash) {
  const parts = String(hash || '').replace(/^#\/?/, '').split('/').filter(Boolean);
  if (parts.length === 0) return { name: 'home' };
  if (parts[0] === 'play' && parts.length === 1) return { name: 'play' };
  if (parts[0] === 'run' && parts.length === 2) return { name: 'run', runId: parts[1] };
  if (parts[0] === 'run' && parts.length === 3) return { name: 'stage', runId: parts[1], stageId: parts[2] };
  return { name: 'notfound', hash };
}

const VIEWS = { home, play, run: runView, stage: stageView };

function showError(root, e) {
  clear(root);
  root.append(el('h1', { text: '불러오지 못했습니다' }), el('div', { class: 'note bad', role: 'alert', text: String((e && e.message) || e) }),
    el('p', {}, el('a', { href: '#/', text: '홈으로' })));
}

async function route() {
  const my = ++seq;
  const root = document.getElementById('view');
  if (cleanup) { try { cleanup(); } catch (e) { /* 정리 실패는 무시 */ } cleanup = null; }
  clear(root);
  const r = parseHash(window.location.hash);
  document.querySelectorAll('[data-nav]').forEach((a) => {
    if (a.dataset.nav === (r.name === 'play' ? 'play' : 'home')) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  const view = VIEWS[r.name];
  if (!view) { showError(root, new Error(`알 수 없는 주소: ${r.hash}`)); return; }
  try {
    const result = await view.render(root, { route: r, mode: state.mode, isStale: () => my !== seq });
    if (my !== seq) { if (typeof result === 'function') result(); return; }
    cleanup = typeof result === 'function' ? result : null;
    root.focus({ preventScroll: true });
  } catch (e) { if (my === seq) showError(root, e); }
}

// ---- 색상 토글: 시스템 / 밝게 / 어둡게. 선택은 localStorage에 저장하되 저장이 안 되어도 동작한다.
function applyTheme(choice, save) {
  const root = document.documentElement;
  if (choice === 'light' || choice === 'dark') root.setAttribute('data-theme', choice); else root.removeAttribute('data-theme');
  document.querySelectorAll('[data-theme-choice]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.themeChoice === (choice === 'light' || choice === 'dark' ? choice : 'system'))));
  if (!save) return;
  try {
    if (choice === 'system') window.localStorage.removeItem(THEME_KEY); else window.localStorage.setItem(THEME_KEY, choice);
  } catch (e) { /* 저장소를 못 쓰면 이번 방문에만 적용된다 */ }
}
function initTheme() {
  document.querySelectorAll('[data-theme-choice]').forEach((b) => b.addEventListener('click', () => applyTheme(b.dataset.themeChoice, true)));
  const cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'light' || cur === 'dark' ? cur : 'system', false);      // theme.js가 이미 반영한 값에 버튼 상태를 맞춘다
}

export async function start() {
  initTheme();
  await detectMode();
  window.addEventListener('hashchange', route);
  await route();
}
start();
