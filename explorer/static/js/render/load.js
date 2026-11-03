// 모델 올리기(load) 단계 렌더러: load_duration을 ms로 바꾼 값과 전체 시간 막대.
import { el, fieldTable, fmtMs, timeBarFor } from '../ui.js';

export function io(stage) {
  const o = stage.output;
  if (stage.status !== 'ok' || o.load_ms === undefined) return el('p', { class: 'meta', text: '이 단계의 값이 기록에 없습니다. 아래 알림을 보세요.' });
  return fieldTable([{ name: 'load_ms', value: fmtMs(o.load_ms), note: 'Ollama load_duration(ns)을 ms로 바꾼 값. 이미 올라가 있으면 0에 가깝습니다.' }], stage.evidence);
}
export const figure = (stage, run) => timeBarFor(run, 'load');
export const interactive = () => null;
