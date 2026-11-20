// 장애 분석 기록(kind "incident")의 단계 렌더러: evidence, prompt, llm_call, parse, notify가 함께 쓴다.
// output의 값을 표(증거 등급 배지 포함)로 보인다. 값의 모양마다 보이는 방식이 다르다.
//   긴 글(줄바꿈이 있거나 80자를 넘는 문자열) → 줄바꿈을 보존하는 상자(.block). 모든 글은 textContent로만 넣는다.
//   목록 → 접는 목록(<details>), 객체 → 키별 줄글, 값이 없음(null) → 표의 "기록에 없음".
import { el, fieldTable, fmtNum } from '../ui.js';

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const LONG_TEXT = 80;

// 한 줄 글. 객체는 "키 값 · 키 값", 목록은 " / "로 잇는다. 값이 없으면 "없음"(null 글자를 찍지 않는다).
export function inline(v) {
  if (v === null || v === undefined) return '없음';
  if (Array.isArray(v)) return v.map(inline).join(' / ');
  if (isObj(v)) return Object.entries(v).map(([k, x]) => `${k} ${isObj(x) ? `(${inline(x)})` : inline(x)}`).join(' · ');
  if (typeof v === 'boolean') return v ? '예' : '아니오';
  if (typeof v === 'number') return fmtNum(v, 3) || '없음';       // NaN·Infinity는 "없음"
  return String(v);
}

// 값 하나를 표의 값 칸에 넣을 내용으로 바꾼다. 값이 없으면 null(표가 "기록에 없음"을 보여 준다).
export function cell(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === 'string') {
    if (v === '') return null;
    return (v.includes('\n') || v.length > LONG_TEXT) ? el('div', { class: 'block', text: v }) : v;
  }
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'boolean') return inline(v);
  if (Array.isArray(v)) {
    if (!v.length) return '(없음)';
    if (v.every((x) => x === null || typeof x !== 'object')) return inline(v);          // 문자열·숫자 목록은 한 줄
    return el('details', {}, el('summary', { text: `${v.length}개 (눌러서 펼치기)` }),
      el('ul', { class: 'plain' }, ...v.map((x) => el('li', { text: inline(x) }))));
  }
  const entries = Object.entries(v);                                                      // 객체는 키별로 한 줄씩
  if (!entries.length) return '(없음)';
  return el('ul', { class: 'plain' }, ...entries.map(([k, x]) => el('li', {}, el('code', { text: k }), ' ',
    Array.isArray(x) && x.length && x.some((y) => y !== null && typeof y === 'object')
      ? el('details', {}, el('summary', { text: `${x.length}개` }), el('ul', { class: 'plain' }, ...x.map((y) => el('li', { text: inline(y) }))))
      : inline(x))));
}

export function io(stage) {
  const entries = Object.entries(stage.output || {});
  if (!entries.length) return el('p', { class: 'meta', text: '이 단계의 값이 기록에 없습니다. 위의 알림을 보세요.' });
  return fieldTable(entries.map(([name, value]) => ({ name, value: cell(value) })), stage.evidence);
}

export const figure = () => null;
export const interactive = () => null;
