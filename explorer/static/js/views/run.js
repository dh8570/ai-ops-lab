// 실행 보기(#/run/<id>): 실행 요약, 단계 스텝퍼, 원본 JSON 링크.
import { getRun, getStages, RUN_ID_RE } from '../data.js';
import { el, fmtMs } from '../ui.js';

const SYMBOL = { ok: '✓', error: '✗', skipped: '–' };

// 단계 스텝퍼: 이 실행에 있는 단계를 눌러서 이동한다(링크라 Tab·Enter로 조작). current는 현재 단계 id.
// titles는 {id: 한국어 제목}(content/stages.json). 제목을 크게, id는 작은 보조 글자로 보여 준다.
export function stepper(run, current, titles = {}) {
  const ol = el('ol', { class: 'stepper', 'aria-label': '단계 목록' });
  for (const s of run.stages) {
    const title = titles[s.id] || s.id;
    const a = el('a', { href: `#/run/${run.run_id}/${s.id}`, 'aria-label': `${title}(${s.id}) 단계, 상태 ${s.status}` },
      el('span', { class: 'st-sym', text: SYMBOL[s.status] || '?' }), ' ', el('span', { class: 'st-title', text: title }), ' ',
      el('small', { class: 'st-id', text: s.id }), ' ', el('span', { class: 'st-status', text: s.status }));   // 공백 글자: textContent가 "입력 input ok"로 읽힘
    if (s.id === current) a.setAttribute('aria-current', 'step');
    ol.append(el('li', { class: `st-${s.status}` }, a));
  }
  return ol;
}

export function summary(run) {
  const s = run.settings, env = run.environment;
  const rows = [
    ['run_id', run.run_id], ['시각(UTC)', run.created_at], ['종류', run.kind], ['제목', run.title],
    ['모델', env.model], ...(env.ollama_version ? [['Ollama', env.ollama_version]] : []),
    ['설정', `temperature ${s.temperature} · seed ${'seed' in s ? s.seed : '임의'} · num_predict ${s.num_predict} · num_ctx ${s.num_ctx}`],
    ['상태', el('span', { class: `status ${run.status}`, text: run.status })], ['오류', run.error || '없음'],
    ['공개 가능(exportable)', String(run.exportable)], ['마스킹', `${run.redaction.applied ? '적용' : '적용 안 함'} — ${run.redaction.note}`],
  ];
  const dl = el('dl', { class: 'summary' });
  for (const [k, v] of rows) dl.append(el('dt', { text: k }), el('dd', {}, v));
  return dl;
}

export async function render(root, ctx) {
  const { runId } = ctx.route;
  if (!RUN_ID_RE.test(runId)) throw new Error(`올바르지 않은 run_id: ${runId}`);
  const [run, stages] = await Promise.all([getRun(runId), getStages()]);
  if (ctx.isStale()) return;
  const titles = Object.fromEntries(stages.map((s) => [s.id, s.title]));
  const total = run.stages.reduce((a, s) => a + (typeof s.duration_ms === 'number' ? s.duration_ms : 0), 0);
  root.append(
    el('h1', { text: '실행 보기' }),
    el('p', { class: 'lead', text: '실행 하나 = 기록 파일 하나. 이 화면은 그 파일만 읽습니다.' }),
    el('section', { class: 'card', 'aria-label': '실행 요약' }, summary(run),
      el('p', { class: 'meta' }, '원본 JSON: ', el('a', { href: `./data/runs/${run.run_id}.json`, text: `./data/runs/${run.run_id}.json` }),
        `  · 단계 시간 합 ${fmtMs(total)}`)),
    el('h2', { text: '단계' }), stepper(run, null, titles),
    el('p', { class: 'meta', text: '단계를 누르면 그 단계의 상세 페이지(8칸)로 이동합니다.' }));
}
