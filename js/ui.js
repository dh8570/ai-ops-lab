// 화면 도우미: 요소 만들기, 증거 배지, 표, 시간 막대, 조각 간격 그래프.
// 규칙: 모델이 만든 글과 기록 안의 문자열은 textContent(또는 텍스트 노드)로만 넣는다. innerHTML은 쓰지 않는다.
// 인라인 스타일은 el.style.prop(CSSOM)로만 바꾼다. style 속성 문자열을 통째로 넣는 방식은 CSP 때문에 쓰지 않는다.

const PROPS = new Set(['id', 'href', 'type', 'value', 'hidden', 'disabled', 'min', 'max', 'step', 'title', 'htmlFor',
  'name', 'placeholder', 'checked', 'tabIndex', 'rows', 'readOnly', 'inputMode', 'download']);

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'style') Object.assign(node.style, v);
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (PROPS.has(k)) node[k] = v;
    else node.setAttribute(k, String(v));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export const note = (cls, text) => el('div', { class: `note ${cls}`, text });

// ---- 증거 배지: 실측 / 계산 / 설명용 / 미확인
export const GRADES = {
  measured: ['실측', '직접 재거나 받은 값'],
  computed: ['계산', '잰 값으로 계산한 값'],
  illustrative: ['설명용', '이해를 돕기 위한 예시. 실제 값이 아님'],
  unverified: ['미확인', '확인하지 못한 값'],
  input: ['입력', '사용자가 정한 값(계산이나 측정이 아님)'],
};
export function badge(grade) {
  const g = GRADES[grade] ? grade : 'unverified';
  return el('span', { class: `ev ev-${g}`, title: GRADES[g][1], dataset: { grade: g } }, GRADES[g][0]);
}
// stage.evidence에서 필드의 등급을 찾는다. "*"는 기본값.
export const gradeFor = (evidence, field) => (evidence && (evidence[field] ?? evidence['*'])) || 'unverified';

export const fmtNum = (n, digits = 1) => (typeof n === 'number' && Number.isFinite(n))
  ? n.toLocaleString('ko-KR', { maximumFractionDigits: digits }) : '';          // 숫자가 아니면(null, undefined, NaN) 아무것도 찍지 않는다
export const fmtMs = (n) => (typeof n === 'number' && Number.isFinite(n)) ? `${fmtNum(n, 1)}ms` : '';
// 값이 없으면(null, undefined) 빈 글자. String(null)이 글자 "null"이 되는 실수를 막는다.
export const txt = (v) => (v === null || v === undefined) ? '' : String(v);
export const utf8Len = (s) => new TextEncoder().encode(s).length;

// 필드 표: rows = [{name, value(문자열|숫자|Node), key?(증거 키, 기본은 name), note?}]
export function fieldTable(rows, evidence) {
  const tb = el('tbody');
  for (const r of rows) {
    const v = r.value;
    const shown = typeof v === 'number' ? fmtNum(v, 3) : v;
    // 값이 없으면(null, undefined, 빈 글자) 표의 값 칸에만 "기록에 없음"을 흐리게 보여 준다. 제목·문장 같은 다른 곳은 아무것도 찍지 않는다.
    const empty = shown === null || shown === undefined || shown === '';
    const td = el('td', { class: typeof v === 'number' ? 'num' : '' }, empty ? el('span', { class: 'meta', text: '기록에 없음' }) : shown);
    if (r.note) append(td, [el('div', { class: 'meta', text: r.note })]);
    tb.append(el('tr', {}, el('th', { scope: 'row' }, r.name, badge(gradeFor(evidence, r.key || r.name))), td));
  }
  return el('table', { class: 'fields' }, tb);
}

export const stageOf = (run, id) => run.stages.find((s) => s.id === id) || null;
export const outputOf = (run, id) => (stageOf(run, id) || {}).output || {};

// ---- 시간 막대: 올리기(load) / 읽기(prefill) / 쓰기(decode) / 기타(total − 위 셋)
export function timeBarFor(run, highlight) {
  const l = outputOf(run, 'load').load_ms, r = outputOf(run, 'prefill').prompt_eval_ms;
  const w = outputOf(run, 'decode').eval_ms, t = outputOf(run, 'decode').total_ms;
  if (![l, r, w, t].every((v) => typeof v === 'number')) {
    return note('warn', '시간 값이 기록에 없습니다(실행이 중단되었거나 마지막 통계를 받지 못했습니다).');
  }
  const other = t - l - r - w;
  const parts = [['load', '올리기 (load_duration)', l, 'seg-load'], ['prefill', '읽기 (prompt_eval_duration)', r, 'seg-read'],
    ['decode', '쓰기 (eval_duration)', w, 'seg-write'], ['other', '기타 (total − 위 셋)', Math.max(other, 0), 'seg-other']];
  const bar = el('div', { class: 'bar', role: 'img', 'aria-label': '올리기 읽기 쓰기 시간 막대' });
  const legend = el('div', { class: 'legend' });
  for (const [id, label, ms, cls] of parts) {
    const pct = t ? (ms / t) * 100 : 0;
    const seg = el('div', { class: `${cls}${highlight && highlight !== id ? ' dim' : ''}`, title: `${label}: ${fmtMs(ms)}` });
    seg.style.width = `${pct}%`;
    if (ms > 0) seg.style.minWidth = '2px';          // 0.8%처럼 아주 좁은 구간도 보이게 표시 폭만 보정한다(숫자와 비율은 그대로)
    bar.append(seg);
    const sw = el('i', { class: cls });
    legend.append(el('span', {}, sw, `${label} ${fmtMs(ms)} (${fmtNum(pct, 1)}%)`));
  }
  const wrap = el('div', {}, bar, legend);
  // 지금 보는 단계만 진하게(.dim이 없음), 나머지는 옅게(.dim) 보이는 것은 의도한 강조다. 그 뜻을 범례 아래에 한 줄로 알린다.
  if (highlight) wrap.append(el('div', { class: 'meta', id: 'bar-note', text: '진하게 = 지금 보는 단계, 옅게 = 다른 단계' }));
  if (other < 0) wrap.append(note('warn', `세 구간의 합이 total_duration보다 ${fmtMs(-other)} 큽니다(Ollama가 준 값 그대로).`));
  return wrap;
}

// ---- 조각 간격 막대 그래프(SVG, 외부 라이브러리 없음). 색은 클래스로 칠해 테마 변수를 따른다.
const SVG_NS = 'http://www.w3.org/2000/svg';
export function gapChart(gaps, { skipFirst = false } = {}) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'gap');
  svg.setAttribute('viewBox', '0 0 640 150');
  svg.setAttribute('width', '100%');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', '조각별 간격 막대 그래프');
  drawGaps(svg, gaps, skipFirst);
  return svg;
}
export function drawGaps(svg, gaps, skipFirst) {
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const W = 640, H = 150, L = 46, B = 22, T = 22;       // 위쪽 T는 첫 조각 라벨 자리
  const mk = (tag, attrs, text) => {
    const n = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (text !== undefined) n.textContent = text;
    svg.appendChild(n);
    return n;
  };
  mk('line', { class: 'axis', x1: L, y1: H - B, x2: W, y2: H - B, stroke: 'currentColor' });
  const data = gaps.map((g, i) => ({ g, i })).filter((d) => !(skipFirst && d.i === 0));
  if (!data.length) return;
  const max = Math.max(...data.map((d) => d.g), 1), bw = (W - L - 4) / data.length;
  mk('text', { x: 2, y: T + 10 }, `${fmtNum(max)}ms`);
  mk('text', { x: 2, y: H - B }, '0');
  mk('text', { x: L, y: H - 6 }, `조각 ${data[0].i + 1} → ${data[data.length - 1].i + 1}`);
  // 첫 조각에는 올리기·읽기 시간이 섞여 있다. 시간 막대의 "올리기" 색과 구분하려고 윤곽선만 있는 막대로 그리고 막대 위에 직접 라벨을 단다.
  if (data[0].i === 0) mk('text', { x: L, y: 14, class: 'first-label' }, `첫 조각 ${fmtMs(gaps[0])} (올리기·읽기 시간 섞임)`);
  for (const [k, d] of data.entries()) {
    const h = (d.g / max) * (H - B - T);
    const r = mk('rect', { class: d.i === 0 ? 'bar-first' : 'bar-rect', x: L + k * bw, y: H - B - h, width: Math.max(bw - 1, 0.5), height: h });
    const t = document.createElementNS(SVG_NS, 'title');
    t.textContent = `조각 ${d.i + 1}: ${fmtMs(d.g)}${d.i === 0 ? ' (올리기·읽기 시간 섞임)' : ''}`;
    r.appendChild(t);
  }
}

// ---- 칩 툴팁(마우스를 올리거나 키보드로 포커스하면 보인다)
let tipEl = null;
export function attachTip(node, text) {
  if (!tipEl) { tipEl = el('div', { class: 'tip', role: 'tooltip' }); document.body.append(tipEl); }
  const show = (e) => {
    tipEl.textContent = text;
    tipEl.style.display = 'block';
    const r = node.getBoundingClientRect();
    const x = (e && e.clientX) || r.left, y = (e && e.clientY) || r.bottom;
    tipEl.style.left = `${Math.max(0, Math.min(x + 12, window.innerWidth - 360))}px`;
    tipEl.style.top = `${y + 14}px`;
  };
  const hide = () => { tipEl.style.display = 'none'; };
  node.addEventListener('mouseenter', show); node.addEventListener('mousemove', show);
  node.addEventListener('mouseleave', hide); node.addEventListener('focus', show); node.addEventListener('blur', hide);
}
