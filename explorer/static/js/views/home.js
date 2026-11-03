// 홈(#/): 단계 지도와 최근 실행 목록.
import { getIndex, getStages, state } from '../data.js';
import { el } from '../ui.js';

export async function render(root, ctx) {
  const stages = await getStages();
  let index = [], indexError = null;
  try { index = await getIndex(); } catch (e) { indexError = e.message; }
  if (ctx.isStale()) return;
  const latest = index.find((e) => e.kind === 'playground') || index[0] || null;

  root.append(el('h1', { text: '파이프라인 탐험' }),
    el('p', { class: 'lead', text: '문장 하나가 AI에게 읽히고 답이 쓰여 글자로 돌아오기까지를, 실제로 잰 실행 기록으로 한 단계씩 봅니다.' }));

  // 단계 지도
  root.append(el('h2', { text: '단계 지도' }));
  for (const lane of ['A', 'B']) {
    const items = stages.filter((s) => s.lane === lane);
    if (!items.length) continue;
    root.append(el('div', { class: 'lane', text: lane === 'A' ? '레인 A' : '레인 B (검색)' }));
    const ul = el('ul', { class: 'map' });
    for (const s of items) {
      const tag = el('span', { class: `tag ${s.implemented ? 'impl' : 'plan'}`, text: s.implemented ? '구현됨' : `예정 ${s.week}주` });
      const title = latest
        ? el('a', { class: 't', href: `#/run/${latest.run_id}/${s.id}`, text: s.title })     // 최근 실행의 단계 페이지로 이어진다
        : el('span', { class: 't', text: s.title });
      ul.append(el('li', { class: s.implemented ? '' : 'planned' }, title, tag, el('span', { class: 'meta', text: `  ${s.id}` })));
    }
    root.append(ul);
  }

  // 최근 실행
  root.append(el('h2', { text: '최근 실행' }));
  if (indexError) root.append(el('div', { class: 'note bad', text: `실행 목록을 읽지 못했습니다: ${indexError}` }));
  else if (!index.length) {
    root.append(el('p', { class: 'meta' }, state.mode === 'live'
      ? [el('a', { href: '#/play', text: '놀이터에서 실행해 보세요' }), '.'] : '공개된 실행이 없습니다.'));
  } else {
    const tb = el('tbody');
    for (const e of index.slice(0, 20)) {
      tb.append(el('tr', {}, el('td', {}, el('a', { href: `#/run/${e.run_id}`, text: e.created_at })), el('td', { text: e.title }),
        el('td', { text: e.model }), el('td', {}, el('span', { class: `status ${e.status}`, text: e.status }))));
    }
    root.append(el('table', {}, el('thead', {}, el('tr', {}, ...['시각(UTC)', '제목', '모델', '상태'].map((t) => el('th', { text: t })))), tb));
  }
  if (state.mode !== 'live') root.append(el('p', { class: 'meta', text: '재생 전용: 이 화면은 실행할 수 없고 기록 파일만 보여 줍니다.' }));
}
