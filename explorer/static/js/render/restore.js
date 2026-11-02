// 글자 복원 단계 렌더러: 서버가 이은 글, Ollama 마지막 줄 response와의 비교, (같은 브라우저에서 방금 실행했다면) 브라우저 비교.
import { browserCompare } from '../data.js';
import { el, fieldTable, fmtNum, note, txt } from '../ui.js';

export function io(stage, run) {
  const o = stage.output;
  if (stage.status !== 'ok' || o.server_joined === undefined) return el('p', { class: 'meta', text: '이 단계는 건너뛰었습니다. 아래 알림을 보세요.' });
  const wrap = el('div', {}, fieldTable([
    { name: 'server_joined', value: el('div', { class: 'block', text: o.server_joined }), note: '서버가 조각을 이어 붙인 글' },
    { name: 'chars', value: fmtNum(o.chars, 0) }, { name: 'bytes', value: fmtNum(o.bytes, 0), note: 'UTF-8 바이트 수' },
    { name: 'final_response', value: o.final_response === null ? '(필드 없음)' : (o.final_response === '' ? '(빈 문자열)' : o.final_response), note: 'Ollama 마지막 줄의 response 필드' },
    { name: 'final_response_empty', value: txt(o.final_response_empty) },
  ], stage.evidence));
  if (o.final_response_empty) {
    wrap.append(note('warn', '스트리밍에서는 마지막 줄의 response가 비어 있어, Ollama가 주는 최종 글과 직접 비교할 수 없습니다.'));
  } else {
    wrap.append(note(o.final_response === o.server_joined ? 'ok' : 'bad', o.final_response === o.server_joined
      ? 'Ollama 마지막 줄의 response가 서버가 이은 글과 같습니다.' : 'Ollama 마지막 줄의 response가 서버가 이은 글과 다릅니다.'));
  }
  if (o.server_joined.includes('�')) wrap.append(note('warn', '이어 붙인 글에 대체 문자(�)가 들어 있습니다.'));
  const bc = browserCompare.get(run.run_id);
  if (bc) {
    wrap.append(el('h3', { text: '브라우저 비교 (이 브라우저에서 방금 한 실행에서만, 기록 파일에는 없음)' }),
      note(bc.equal ? 'ok' : 'bad', bc.equal
        ? `서버가 이은 글과 브라우저가 이은 글이 같습니다 (${fmtNum(bc.chars, 0)}자 · ${fmtNum(bc.bytes, 0)}바이트). 전달 중에 글이 바뀌지 않았습니다.`
        : '서버가 이은 글과 브라우저가 이은 글이 다릅니다. 확인이 필요합니다.'));
  }
  return wrap;
}
export const figure = () => null;
export const interactive = () => null;
