// 토큰화 단계 렌더러: 토큰 칩(점선 칩 = 바이트 조각), 통계, 검사 결과. 놀이터의 토큰화 미리보기도 이 칩을 쓴다.
import { el, attachTip, fieldTable, fmtNum, note, txt } from '../ui.js';

const visible = (p) => p.replace(/ /g, '·').replace(/\n/g, '↵').replace(/\r/g, '⏎').replace(/\t/g, '→');

export function chips(tokens) {
  const box = el('div', { class: 'chips', 'aria-label': '토큰 목록' });
  for (const t of tokens) {
    const c = el('span', { class: `chip c${t.index % 6}${t.partial ? ' frag' : ''}${t.special ? ' special' : ''}`, tabIndex: 0 }, visible(t.piece));
    let tip = `#${t.index}  id ${t.id}\n바이트 ${t.hex}\n단독으로 올바른 UTF-8: ${t.partial ? '아니오 (깨진 글자 조각)' : '예'}`;
    if (t.special) tip += '\n특수 토큰';
    if (t.fragment_of && t.fragment_of.length) tip += `\n글자 ${t.fragment_of.map((x) => `‘${x}’`).join(' ')}의 중간을 자름`;
    c.setAttribute('aria-label', tip.replace(/\n/g, ', '));
    attachTip(c, tip);
    box.append(c);
  }
  return box;
}

export function legend() {
  return el('div', { class: 'meta', text: '공백은 ·, 줄바꿈은 ↵, 탭은 →로 표시합니다. 점선 칩 = 단독으로는 올바른 UTF-8이 아닌 바이트 조각, 파란 테두리 = 특수 토큰. 칩에 마우스를 올리거나 Tab으로 이동하면 id와 바이트가 보입니다.' });
}

export function stats(o) {
  const s = (v, label) => el('div', { class: 'stat' }, el('b', { text: fmtNum(v, 0) }), el('span', { text: label }));
  return el('div', { class: 'stats' }, s(o.text_chars, '글자 수'), s(o.text_bytes, 'UTF-8 바이트 수'), s(o.token_count, '토큰 수'),
    s(o.partial_count, '깨진 글자(바이트 조각) 토큰'));
}

export function notes(o) {
  const box = el('div');
  if (o.partial_count) box.append(note('warn', `토큰 ${o.partial_count}개는 글자의 일부 바이트만 담고 있습니다(깨진 글자). 이 조각들이 이어져야 글자 하나가 됩니다.`));
  const where = typeof o.normalized_bytes === 'number' ? ` (${fmtNum(o.normalized_bytes, 0)}바이트)` : '';
  box.append(note(o.bytes_match ? 'ok' : 'bad', o.bytes_match
    ? `토큰 바이트를 이은 값 = 입력(NFC 정규화 후)의 UTF-8 바이트${where} — 일치`
    : '토큰 바이트를 이은 값이 입력과 다릅니다. 이 값은 일치해야 정상이므로 확인이 필요합니다.'));
  if (o.nfc_changed) box.append(note('warn', '입력이 NFC 정규화로 바뀌었습니다. 토큰은 정규화된 글을 자른 결과입니다.'));
  return box;
}

export function io(stage) {
  const o = stage.output;
  return el('div', {}, stats(o), notes(o), fieldTable([
    { name: 'text_chars', value: fmtNum(o.text_chars, 0) }, { name: 'text_bytes', value: fmtNum(o.text_bytes, 0) },
    { name: 'nfc_changed', value: txt(o.nfc_changed) }, { name: 'token_count', value: fmtNum(o.token_count, 0) },
    { name: 'partial_count', value: fmtNum(o.partial_count, 0) }, { name: 'bytes_match', value: txt(o.bytes_match) },
  ], stage.evidence), stage.status !== 'ok' ? null : el('div', { class: 'meta', text: `토큰 ${o.tokens.length}개의 상세는 아래 그림의 칩과 원본 JSON(tokens)에 있습니다.` }));
}

export function figure(stage) {
  if (!stage.output.tokens) return null;
  return el('div', {}, chips(stage.output.tokens), legend());
}

// 직접 만져 보기(live): 글을 바꿔 토큰이 어떻게 잘리는지 본다. 기록에는 남지 않는다.
export function interactive(stage) {
  const ta = el('textarea', { id: 'try-text', 'aria-label': '토큰으로 잘라 볼 글' });
  ta.value = '';
  const out = el('div', { id: 'try-out' });
  const btn = el('button', { type: 'button', class: 'primary', id: 'try-btn', on: { click: async () => {
    out.replaceChildren();
    btn.disabled = true;
    try {
      const res = await fetch('./api/tokenize', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: ta.value }) });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      out.append(stats(body), notes(body), chips(body.tokens), legend());
    } catch (e) { out.append(note('bad', `토큰화 실패: ${e.message}`)); }
    btn.disabled = false;
  } } }, '토큰으로 자르기');
  return el('div', {}, el('p', { class: 'meta', text: '글을 바꿔 보세요. 서버의 토크나이저로 다시 자르며, 결과는 기록에 저장되지 않습니다.' }), ta, el('div', { class: 'row' }, btn), out);
}
