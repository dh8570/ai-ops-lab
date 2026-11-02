// 쓰기(decode) 단계 렌더러: 받은 조각, 조각 간격 그래프, tok/s, 첫 글자까지의 시간, 조각 수와 eval_count 비교.
import { browserCompare } from '../data.js';
import { drawGaps, el, fieldTable, fmtMs, fmtNum, gapChart, note, timeBarFor, txt } from '../ui.js';

export function io(stage, run) {
  const o = stage.output;
  if (!o.chunks) return el('p', { class: 'meta', text: '이 단계의 값이 기록에 없습니다. 아래 알림을 보세요.' });
  const wrap = el('div');
  if (stage.status === 'ok') {
    const same = o.chunk_count === o.eval_count;
    wrap.append(fieldTable([
      { name: 'chunk_count', value: fmtNum(o.chunk_count, 0), note: '서버가 스트림에서 받은 조각 수' },
      { name: 'eval_count', value: fmtNum(o.eval_count, 0), note: 'Ollama가 센 생성 토큰 수' },
      { name: 'eval_ms', value: fmtMs(o.eval_ms) },
      { name: 'tok_s', value: fmtNum(o.tok_s, 1), note: 'eval_count ÷ eval_duration (계산)' },
      { name: 'first_chunk_ms', value: fmtMs(o.first_chunk_ms), note: '요청을 보낸 뒤 첫 조각이 도착할 때까지(서버 기준)' },
      { name: 'done_reason', value: txt(o.done_reason) },
      { name: 'total_ms', value: fmtMs(o.total_ms), note: 'Ollama total_duration(ns)을 ms로 바꾼 값' },
    ], stage.evidence));
    wrap.append(note(same ? 'ok' : 'warn', same
      ? `받은 조각 수와 eval_count가 같습니다 (${fmtNum(o.chunk_count, 0)}).`
      : `받은 조각 수 ${fmtNum(o.chunk_count, 0)} vs eval_count ${fmtNum(o.eval_count, 0)}: 다릅니다(차이 ${fmtNum(o.chunk_count - o.eval_count, 0)}). 조각 하나가 토큰 하나라고 단정할 수 없습니다.`));
  } else {
    wrap.append(el('p', { class: 'meta', text: `받은 조각 ${fmtNum(o.chunk_count, 0)}개까지만 기록되었습니다(통계 줄을 받지 못함).` }));
  }
  wrap.append(el('h3', { text: '받은 조각을 이어 붙인 글' }), el('div', { class: 'block', text: o.chunks.map((c) => c.text).join('') }));
  const bc = browserCompare.get(run.run_id);
  if (bc && bc.firstBrowserMs !== null) {
    wrap.append(note('ok', `이 브라우저에서 방금 한 실행의 첫 글자까지(브라우저 기준): ${fmtMs(bc.firstBrowserMs)} (이 값은 기록 파일에 없습니다)`));
  }
  return wrap;
}

export function figure(stage, run) {
  const o = stage.output;
  const wrap = el('div', {}, timeBarFor(run, 'decode'));
  if (o.chunks && o.chunks.length) {
    const gaps = o.chunks.map((c) => c.gap_ms);
    const svg = gapChart(gaps, { skipFirst: false });
    const cb = el('input', { type: 'checkbox', id: 'skip-first', on: { change: () => drawGaps(svg, gaps, cb.checked) } });
    wrap.append(el('h3', { text: '조각별 간격 (직전 조각과의 시간)' }),
      el('label', { class: 'meta' }, cb, ' 첫 조각 제외 (첫 조각에는 올리기·읽기 시간이 섞여 있음)'), svg);
  }
  return wrap;
}
export const interactive = () => null;
