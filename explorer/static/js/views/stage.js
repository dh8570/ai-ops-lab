// 단계 페이지(#/run/<id>/<stage>): 8칸을 항상 같은 순서로 보여 준다.
// 1 한 줄 요약  2 입력→출력  3 그림  4 직접 만져 보기  5 생길 수 있는 실패  6 설명(세 단계)  7 증거  8 확인하지 못한 것
import { getRun, getStageContent, getStages, RUN_ID_RE, state, STAGE_ID_RE } from '../data.js';
import * as decode from '../render/decode.js';
import * as input from '../render/input.js';
import * as load from '../render/load.js';
import * as prefill from '../render/prefill.js';
import * as restore from '../render/restore.js';
import * as tokenize from '../render/tokenize.js';
import { append, badge, el, fieldTable, fmtMs, GRADES, note } from '../ui.js';
import { stepper } from './run.js';

const RENDERERS = { input, tokenize, load, prefill, decode, restore };
const LEVELS = [['summary', '한 줄'], ['easy', '쉬운 말'], ['precise', '정확하게']];
const LEVEL_KEY = 'pipeline-explain-level';

function savedLevel() {
  try { const v = window.localStorage.getItem(LEVEL_KEY); return LEVELS.some(([k]) => k === v) ? v : 'easy'; } catch (e) { return 'easy'; }
}
function saveLevel(v) {
  try { window.localStorage.setItem(LEVEL_KEY, v); } catch (e) { /* 저장이 안 되어도 동작한다 */ }
}

const slot = (n, title, ...body) => el('section', { class: 'slot', dataset: { slot: String(n) }, 'aria-label': `${n}. ${title}` },
  el('h2', {}, el('span', { class: 'n', text: String(n) }), title), ...body);
const list = (items, empty) => (items && items.length)
  ? el('ul', { class: 'plain' }, ...items.map((t) => el('li', { text: t }))) : el('p', { class: 'meta', text: empty });

function explainSlot(content) {
  const p = el('p', { class: 'explain-text', id: 'explain-text', 'aria-live': 'polite' });
  const btns = [];
  const show = (level) => {
    p.textContent = content[level] || '이 설명은 아직 없습니다.';
    btns.forEach(([k, b]) => b.setAttribute('aria-pressed', String(k === level)));
  };
  const group = el('div', { class: 'seg', role: 'group', 'aria-label': '설명 수준' });
  for (const [k, label] of LEVELS) {
    const b = el('button', { type: 'button', id: `lvl-${k}`, 'aria-pressed': 'false', on: { click: () => { saveLevel(k); show(k); } } }, label);
    btns.push([k, b]);
    group.append(b);
  }
  show(savedLevel());                                    // 기본은 "쉬운 말"
  return slot(6, '설명', group, p);
}

// 걸린 시간 문장. 값이 있으면 "걸린 시간(duration_ms) 190.7ms", 없으면 이유를 말로 쓴다(null을 그대로 찍지 않는다).
function durationText(stage) {
  if (!stage) return '이 실행에는 이 단계의 기록이 없어 걸린 시간(duration_ms)이 없습니다.';
  if (typeof stage.duration_ms === 'number') return `걸린 시간(duration_ms) ${fmtMs(stage.duration_ms)}`;
  return '이 단계는 걸린 시간(duration_ms)을 따로 재지 않았습니다.';
}

function evidenceSlot(run, stage) {
  const legend = el('ul', { class: 'plain' }, ...Object.entries(GRADES).map(([g, [name, mean]]) => el('li', {}, badge(g), ` ${name}: ${mean}`)));
  const ev = stage ? Object.entries(stage.evidence).map(([k, g]) => el('li', {}, el('code', { text: k === '*' ? '* (기본값)' : k }), ' ', badge(g))) : [];
  return slot(7, '증거',
    el('p', { id: 'evidence-duration' }, durationText(stage)),
    stage ? el('div', {}, el('div', { class: 'meta', text: '필드별 증거 등급' }), el('ul', { class: 'plain' }, ...ev)) : null,
    el('div', { class: 'meta', text: '배지 범례' }), legend,
    el('p', { class: 'meta' }, `run_id: ${run.run_id}  · 원본 JSON: `, el('a', { href: `./data/runs/${run.run_id}.json`, text: `./data/runs/${run.run_id}.json` })));
}

export async function render(root, ctx) {
  const { runId, stageId } = ctx.route;
  if (!RUN_ID_RE.test(runId)) throw new Error(`올바르지 않은 run_id: ${runId}`);
  if (!STAGE_ID_RE.test(stageId)) throw new Error(`올바르지 않은 단계 id: ${stageId}`);
  const [stages, run, content] = await Promise.all([getStages(), getRun(runId), getStageContent(stageId)]);
  if (ctx.isStale()) return;
  const meta = stages.find((s) => s.id === stageId);
  if (!meta) throw new Error(`알 수 없는 단계: ${stageId}`);
  const stage = run.stages.find((s) => s.id === stageId) || null;
  const planned = !meta.implemented;
  const titles = Object.fromEntries(stages.map((s) => [s.id, s.title]));
  const renderer = RENDERERS[stageId];

  // 기본 root.append(null)은 글자 "null"을 붙인다. 값이 없을 수 있는 항목이 있으므로 null을 건너뛰는 append()를 쓴다.
  append(root, [
    el('p', { class: 'meta' }, el('a', { href: `#/run/${run.run_id}`, text: '← 실행 보기' }), `  · ${run.title.slice(0, 40)}`),
    stepper(run, stage ? stageId : null, titles),
    el('h1', {}, `${meta.title} `, el('span', { class: `tag ${planned ? 'plan' : 'impl'}`, text: planned ? `예정 (${meta.week}주)` : '구현됨' }),
      stage ? el('span', { class: `status ${stage.status}`, text: `  ${stage.status}` }) : null),
    stage && typeof stage.duration_ms === 'number'
      ? el('p', { class: 'meta', id: 'stage-duration' }, `걸린 시간 ${fmtMs(stage.duration_ms)}`) : null,
    planned ? el('div', { class: 'big-plan', role: 'status', text: `예정 (${meta.week}주)` }) : null]);

  // 1 한 줄 요약
  root.append(slot(1, '한 줄 요약', el('p', { text: content.summary })));

  // 2 입력 → 출력
  let io;
  if (planned) io = el('p', { class: 'meta', text: `아직 구현되지 않은 단계입니다(예정 ${meta.week}주). 이 단계의 입출력 기록은 아직 없습니다.` });
  else if (!stage) io = note('warn', '이 실행에는 이 단계가 없습니다.');
  else {
    io = el('div');
    if (stage.status !== 'ok') io.append(note(stage.status === 'error' ? 'bad' : 'warn', `이 단계의 상태: ${stage.status}`));
    if (stage.notes && stage.notes.length) io.append(el('ul', { class: 'plain' }, ...stage.notes.map((t) => el('li', { text: t }))));
    io.append(renderer ? renderer.io(stage, run) : fieldTable(Object.entries(stage.output).map(([k, v]) => ({ name: k, value: JSON.stringify(v) })), stage.evidence));
  }
  root.append(slot(2, '입력 → 출력', io));

  // 3 그림
  const fig = !planned && stage && renderer ? renderer.figure(stage, run) : null;
  root.append(slot(3, '그림', fig || el('p', { class: 'meta', text: '이 단계의 그림은 아직 없습니다.' })));

  // 4 직접 만져 보기 (live이고 렌더러가 제공할 때만)
  let tryBody;
  if (state.mode !== 'live') tryBody = el('p', { class: 'meta', text: '재생 전용(static)이라 직접 만져 볼 수 없습니다. 실행 서버가 있는 live 화면에서만 열립니다.' });
  else if (planned || !stage || !renderer || !renderer.interactive) tryBody = el('p', { class: 'meta', text: '이 단계에는 직접 만져 볼 수 있는 기능이 아직 없습니다.' });
  else {
    const made = await renderer.interactive(stage, run);
    tryBody = made || el('p', { class: 'meta', text: '이 단계에는 직접 만져 볼 수 있는 기능이 아직 없습니다.' });
  }
  if (ctx.isStale()) return;
  root.append(slot(4, '직접 만져 보기', tryBody));

  // 5 생길 수 있는 실패, 6 설명, 7 증거, 8 확인하지 못한 것
  root.append(slot(5, '생길 수 있는 실패', list(content.failures, '기록된 항목이 없습니다.')));
  root.append(explainSlot(content));
  root.append(evidenceSlot(run, stage));
  root.append(slot(8, '확인하지 못한 것', list(content.unverified, '기록된 항목이 없습니다.')));
}
