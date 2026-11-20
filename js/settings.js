// 설정 컨트롤(놀이터, 입력 단계 공용). 서버 검증(limits.py)은 그대로이고, 화면의 제한은 편의일 뿐이다.
// 요청 본문의 옵션과 "Ollama에 보낼 options" 미리보기는 getOptions() 하나에서 나온다(그래서 항상 같다).
import { latestTokS } from './data.js';
import { el, fmtNum } from './ui.js';

export const LIMITS = { temperature: [0, 1.5], num_predict: [1, 512], num_ctx: [2048, 4096, 8192], seed: [0, 2147483647] };
export const DEFAULT_TOKS = 131;              // 가장 최근 실행 기록이 없을 때 쓰는 1회 실측값(tok/s)
export const KV_KIB_PER_TOKEN = 56;           // 2 × 28층 × 4 KV헤드 × 128 × 2바이트(bf16) ÷ 1024 (config.json 기준)
const SESSION_KEY = 'pipeline-play-settings';

export const round1 = (v) => Math.round(v * 10) / 10;

export function tempHint(t) {
  if (t === 0) return '항상 1등 후보만 고릅니다. 같은 입력이면 같은 답입니다.';
  if (t <= 0.3) return '거의 같은 답이 나옵니다. 분석이나 채점처럼 일정해야 할 때 씁니다.';
  if (t <= 0.8) return '보통입니다. 답이 조금씩 달라집니다.';
  return '다양해지지만 엉뚱한 말이나 다른 언어가 섞일 수 있습니다.';
}
export const kvMiB = (numCtx) => (numCtx * KV_KIB_PER_TOKEN) / 1024;

// 소요 시간 표시: 1초 미만은 밀리초 정수("46ms"), 1~10초는 소수 한 자리 초("1.5초"), 10초 이상은 정수 초("26초").
export function fmtEta(seconds) {
  const ms = Math.round(seconds * 1000);
  if (ms < 1000) return `${ms}ms`;
  const tenth = Math.round(seconds * 10) / 10;
  return tenth < 10 ? `${tenth.toFixed(1)}초` : `${Math.round(seconds)}초`;
}

// 숫자만 있는 정수 문자열을 [min, max]로 검사한다. 잘못되면 {error}, 올바르면 {value}.
export function parseIntText(text, min, max) {
  const t = String(text).trim();
  if (!/^\d+$/.test(t)) return { error: `${min}~${max} 범위의 정수(숫자만)를 입력하세요` };
  const n = Number(t);
  if (!Number.isSafeInteger(n) || n < min || n > max) return { error: `${min}~${max} 범위의 정수를 입력하세요` };
  return { value: n };
}

export function saveInitial(obj) {
  try { window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(obj)); } catch (e) { /* 저장소를 못 쓰면 건너뜀 */ }
}
export function loadInitial() {
  try {
    const raw = window.sessionStorage.getItem(SESSION_KEY);
    window.sessionStorage.removeItem(SESSION_KEY);       // 한 번만 쓴다
    return raw ? JSON.parse(raw) : {};
  } catch (e) { return {}; }
}

export function createSettings({ models = [], help = {}, initial = {}, getTokS = latestTokS } = {}) {
  const st = {
    model: models.includes(initial.model) ? initial.model : (models[0] || ''),
    temperature: typeof initial.temperature === 'number' ? round1(initial.temperature) : 0.2,
    seedMode: Number.isInteger(initial.seed) ? 'fixed' : 'random',
    seed: Number.isInteger(initial.seed) ? initial.seed : null,
    lastSeed: Number.isInteger(initial.seed) ? initial.seed : 42,
    np: LIMITS.num_predict[0] <= initial.num_predict && initial.num_predict <= LIMITS.num_predict[1] ? initial.num_predict : 200,
    ctx: LIMITS.num_ctx.includes(initial.num_ctx) ? initial.num_ctx : 4096,
    tokS: null,
  };
  const listeners = [];

  // ---- 공용: 이름 줄(라벨, 현재 값, ⓘ 버튼)과 펼쳐지는 도움말
  function cell(key, labelText, forId, valId, body) {
    const helpBox = el('div', { class: 'opt-help', id: `help-${key}`, hidden: true, text: help[key] || '' });
    const wrap = el('div', { class: 'opt', dataset: { opt: key } });
    const btn = el('button', {
      type: 'button', class: 'info', 'aria-expanded': 'false', 'aria-controls': `help-${key}`,
      'aria-label': `${key} 도움말 열기·닫기`,
      on: { click: () => {
        const open = helpBox.hidden;
        helpBox.hidden = !open;
        wrap.classList.toggle('open', open);
        btn.setAttribute('aria-expanded', String(open));
      } },
    }, 'ⓘ');
    wrap.append(el('div', { class: 'optname' }, el('label', { htmlFor: forId }, labelText), btn,
      el('span', { class: 'val', id: valId, 'aria-live': 'polite' })), ...body, helpBox);
    return wrap;
  }

  // ---- model
  const modelSel = el('select', { id: 'set-model', on: { change: () => { st.model = modelSel.value; refresh(); } } },
    ...models.map((m) => el('option', { value: m, text: m })));
  modelSel.value = st.model;
  const modelCell = cell('model', 'model', 'set-model', 'model-val', [modelSel]);

  // ---- temperature: 슬라이더 0~1.5, step 0.1
  const tRange = el('input', { type: 'range', id: 'set-temperature', min: 0, max: 1.5, step: 0.1, value: st.temperature,
    on: { input: () => { st.temperature = round1(Number(tRange.value)); refresh(); } } });
  const tHint = el('div', { class: 'hint', id: 'temp-hint' });
  const tempCell = cell('temperature', 'temperature', 'set-temperature', 'temp-val',
    [tRange, el('div', { class: 'ends' }, el('span', { text: '0 · 일정' }), el('span', { text: '1.5 · 다양' })), tHint]);

  // ---- seed: 슬라이더 없음. 임의/고정 버튼 + 숫자 칸
  const seedInput = el('input', { type: 'text', id: 'seed-input', inputMode: 'numeric', placeholder: '임의', 'aria-describedby': 'seed-err',
    value: st.seed === null ? '' : String(st.seed) });
  const seedErr = el('div', { class: 'field-err', id: 'seed-err', role: 'alert' });
  const bRandom = el('button', { type: 'button', id: 'seed-random', 'aria-pressed': 'false' }, '임의');
  const bFixed = el('button', { type: 'button', id: 'seed-fixed', 'aria-pressed': 'false' }, '고정');
  const setFixed = (n) => { st.seedMode = 'fixed'; st.seed = n; st.lastSeed = n; seedInput.value = String(n); seedErr.textContent = ''; refresh(); };
  bRandom.addEventListener('click', () => {
    if (st.seed !== null) st.lastSeed = st.seed;
    st.seedMode = 'random'; st.seed = null; seedInput.value = ''; seedErr.textContent = ''; refresh();
  });
  bFixed.addEventListener('click', () => setFixed(st.seed ?? st.lastSeed));
  seedInput.addEventListener('input', () => {
    if (seedInput.value.trim() === '') { st.seedMode = 'random'; st.seed = null; seedErr.textContent = ''; refresh(); return; }
    const r = parseIntText(seedInput.value, ...LIMITS.seed);
    if (r.error) { seedErr.textContent = r.error; return; }           // 잘못된 값: 오류만 보이고 요청 값은 그대로
    setFixed(r.value);                                                 // 숫자를 입력하면 자동으로 '고정'
  });
  const seedCell = cell('seed', 'seed (비우면 임의)', 'seed-input', 'seed-val', [
    el('div', { class: 'row' }, el('span', { class: 'seg', role: 'group', 'aria-label': 'seed 방식' }, bRandom, bFixed), seedInput),
    seedErr,
    el('div', { class: 'row' },
      el('button', { type: 'button', id: 'seed-42', on: { click: () => setFixed(42) } }, '42 쓰기'),
      el('button', { type: 'button', id: 'seed-reroll', on: { click: () => setFixed(Math.floor(Math.random() * (LIMITS.seed[1] + 1))) } }, '다시 뽑기')),
  ]);

  // ---- num_predict: 슬라이더와 숫자 칸 동기화 + 프리셋 + 소요 시간 추정
  const npRange = el('input', { type: 'range', id: 'np-range', min: LIMITS.num_predict[0], max: LIMITS.num_predict[1], step: 1, value: st.np,
    on: { input: () => { st.np = Number(npRange.value); npInput.value = String(st.np); npErr.textContent = ''; refresh(); } } });
  const npInput = el('input', { type: 'text', id: 'np-input', inputMode: 'numeric', value: String(st.np), 'aria-describedby': 'np-err',
    on: { input: () => {
      const r = parseIntText(npInput.value, ...LIMITS.num_predict);
      if (r.error) { npErr.textContent = `${r.error} (지금 값 ${st.np} 유지)`; return; }   // 슬라이더와 요청 값은 그대로
      st.np = r.value; npRange.value = String(r.value); npErr.textContent = ''; refresh();
    } } });
  const npErr = el('div', { class: 'field-err', id: 'np-err', role: 'alert' });
  const preset = (n) => el('button', { type: 'button', id: `np-${n}`, on: { click: () => {
    st.np = n; npRange.value = String(n); npInput.value = String(n); npErr.textContent = ''; refresh();
  } } }, String(n));
  const npEta = el('div', { class: 'hint', id: 'np-eta' });
  const npCell = cell('num_predict', 'num_predict', 'np-input', 'np-val', [
    npRange, el('div', { class: 'row' }, npInput, preset(50), preset(200), preset(512)), npErr, npEta]);

  // ---- num_ctx: 선택 버튼 3개 + KV 캐시 추정
  const ctxBtns = LIMITS.num_ctx.map((n) => el('button', { type: 'button', id: `ctx-${n}`, 'aria-pressed': 'false',
    on: { click: () => { st.ctx = n; refresh(); } } }, String(n)));
  const ctxKv = el('div', { class: 'hint', id: 'ctx-kv' });
  const ctxCell = cell('num_ctx', 'num_ctx', 'ctx-2048', 'ctx-val', [
    el('div', { class: 'seg', role: 'group', 'aria-label': 'num_ctx 선택' }, ...ctxBtns), ctxKv]);

  const previewEl = el('div', { class: 'preview', id: 'opt-preview', 'aria-label': 'Ollama에 보낼 options (읽기 전용)' });
  const root = el('div', {},
    el('div', { class: 'opts' }, modelCell, tempCell, seedCell, npCell, ctxCell),
    el('div', { class: 'meta', text: 'Ollama에 보낼 options (읽기 전용 미리보기)' }), previewEl);

  // 요청 본문의 옵션과 미리보기가 공유하는 단 하나의 값
  function getOptions() {
    const o = { temperature: round1(st.temperature) };
    if (st.seedMode === 'fixed' && st.seed !== null) o.seed = st.seed;          // 임의이면 seed 키 자체가 없다
    o.num_predict = st.np;
    o.num_ctx = st.ctx;
    return o;
  }
  const getBody = (prompt) => ({ prompt, model: st.model, ...getOptions() });

  function refreshEta() {
    const known = typeof st.tokS === 'number' && st.tokS > 0;
    const toks = known ? st.tokS : DEFAULT_TOKS;
    npEta.textContent = `끝까지 쓰면 최대 약 ${fmtEta(st.np / toks)} (${known ? `가장 최근 실행 ${fmtNum(toks, 1)} tok/s 기준` : `${DEFAULT_TOKS} tok/s, 1회 실측 기준`})`;
  }
  function refresh() {
    const set = (id, t) => { root.querySelector(`#${id}`).textContent = t; };
    set('model-val', st.model);
    set('temp-val', st.temperature.toFixed(1));
    tRange.value = String(st.temperature);
    tHint.textContent = tempHint(st.temperature);
    set('seed-val', st.seedMode === 'fixed' ? String(st.seed) : '임의');
    bRandom.setAttribute('aria-pressed', String(st.seedMode === 'random'));
    bFixed.setAttribute('aria-pressed', String(st.seedMode === 'fixed'));
    set('np-val', String(st.np));
    refreshEta();
    set('ctx-val', String(st.ctx));
    ctxBtns.forEach((b, i) => b.setAttribute('aria-pressed', String(LIMITS.num_ctx[i] === st.ctx)));
    ctxKv.textContent = `KV 캐시 약 ${fmtNum(kvMiB(st.ctx), 1)} MiB(추정)`;
    previewEl.textContent = JSON.stringify(getOptions());
    listeners.forEach((fn) => fn(getOptions()));
  }

  refresh();
  Promise.resolve(getTokS()).then((r) => { st.tokS = r && r.tokS; refreshEta(); }).catch(() => {});

  return { root, getOptions, getBody, getModel: () => st.model, onChange: (fn) => listeners.push(fn),
    state: () => ({ ...st }), previewText: () => previewEl.textContent };
}
