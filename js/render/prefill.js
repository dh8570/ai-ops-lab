// 읽기(prefill) 단계 렌더러: prompt_eval_*, 로컬 토큰 수와의 비교. 직접 만져 보기(live)는 모델 template 확인이다.
import { el, fieldTable, fmtMs, fmtNum, note, timeBarFor } from '../ui.js';

export function io(stage) {
  const o = stage.output;
  if (stage.status !== 'ok' || o.prompt_eval_count === undefined) return el('p', { class: 'meta', text: '이 단계의 값이 기록에 없습니다. 아래 알림을 보세요.' });
  const diff = typeof o.count_diff === 'number' ? `${o.count_diff > 0 ? '+' : ''}${fmtNum(o.count_diff, 0)}` : '';
  const rows = [
    { name: 'prompt_eval_count', value: fmtNum(o.prompt_eval_count, 0), note: 'Ollama가 읽었다고 센 입력 토큰 수' },
    { name: 'prompt_eval_ms', value: fmtMs(o.prompt_eval_ms), note: 'prompt_eval_duration(ns)을 ms로 바꾼 값' },
    { name: 'local_token_count', value: fmtNum(o.local_token_count, 0), note: '이 서버의 토크나이저가 우리 글만 잘랐을 때의 토큰 수' },
    { name: 'count_diff', value: diff, note: 'prompt_eval_count − local_token_count' },
  ];
  if ('prompt_eval_cached_count' in o) rows.push({ name: 'prompt_eval_cached_count', value: fmtNum(o.prompt_eval_cached_count, 0), note: 'Ollama가 준 값 그대로' });
  return el('div', {}, fieldTable(rows, stage.evidence),
    el('div', { class: 'meta', text: `로컬 토큰 수 vs prompt_eval_count: ${fmtNum(o.local_token_count, 0)} vs ${fmtNum(o.prompt_eval_count, 0)}${diff ? ` (차이 ${diff})` : ''}` }));
}
export const figure = (stage, run) => timeBarFor(run, 'prefill');

export function interactive(stage, run) {
  const pre = el('pre', { id: 'tmpl-out', text: '-' });
  const btn = el('button', { type: 'button', class: 'primary', id: 'tmpl-btn', on: { click: async () => {
    btn.disabled = true;
    try {
      const res = await fetch(`./api/show?model=${encodeURIComponent(run.environment.model)}`);
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      pre.textContent = body.template ?? '(template 없음)';
    } catch (e) { pre.textContent = `가져오지 못함: ${e.message}`; }
    btn.disabled = false;
  } } }, '모델의 template 문자열 보기');
  return el('div', {}, el('p', { class: 'meta', text: 'Ollama가 모델마다 갖고 있는 채팅 틀(template)을 그대로 가져옵니다(확인용).' }), el('div', { class: 'row' }, btn), pre,
    note('warn', '이 문자열은 확인용 원문입니다. 실제로 붙은 토큰은 이 화면이 확인하지 않았습니다(확인하지 못한 것 참고).'));
}
