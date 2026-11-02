// 놀이터(#/play): 입력 + 설정 컨트롤 + 실행. 실행이 끝나면 그 실행의 기록 보기(#/run/<id>)로 이동한다.
// live에서만 동작한다. static이면 실행·토큰화 버튼을 숨기고 "재생 전용"만 보여 준다.
import { browserCompare, getSettingsHelp, invalidateLatest, RUN_ID_RE, state } from '../data.js';
import { chips, legend, notes as tokenNotes, stats as tokenStats } from '../render/tokenize.js';
import { createSettings, loadInitial } from '../settings.js';
import { drawGaps, el, fmtMs, fmtNum, gapChart, note, utf8Len } from '../ui.js';

const DEFAULT_PROMPT = '웹 서비스에서 1분 동안 요청의 37%가 HTTP 500 에러였다. 원인 후보 두 가지를 한국어로 한 줄씩 써라.';

export async function render(root, ctx) {
  root.append(el('h1', { text: '놀이터' }));
  if (state.mode !== 'live') {
    root.append(el('div', { class: 'card', id: 'readonly-notice' },
      note('warn', '재생 전용(실행할 수 없습니다)'),
      el('p', { class: 'meta', text: '이 화면은 실행 서버 없이 기록 파일만 보여 주는 모드입니다. 실행과 토큰화는 실행 서버가 있는 live 화면에서만 할 수 있습니다.' }),
      el('p', {}, el('a', { href: '#/', text: '홈에서 공개된 실행 보기' }))));
    return undefined;
  }
  const help = await getSettingsHelp();
  if (ctx.isStale()) return undefined;
  const cfg = state.config, init = loadInitial();
  const settings = createSettings({ models: cfg.models, help, initial: init });
  const promptEl = el('textarea', { id: 'prompt', 'aria-label': '프롬프트' });
  promptEl.value = typeof init.prompt === 'string' ? init.prompt : DEFAULT_PROMPT;
  const count = el('div', { class: 'meta', id: 'live-count' });
  const updateCount = () => { count.textContent = `${[...promptEl.value].length.toLocaleString('ko-KR')}자 · ${utf8Len(promptEl.value).toLocaleString('ko-KR')}바이트 · 최대 ${cfg.limits.prompt_max}자`; };
  promptEl.addEventListener('input', updateCount);
  updateCount();

  const btnRun = el('button', { type: 'button', class: 'primary', id: 'btn-run' }, '실행');
  const btnTok = el('button', { type: 'button', id: 'btn-tok' }, '토큰화만 보기');
  const btnProbe = el('button', { type: 'button', id: 'btn-probe' }, 'logprobs 지원 확인');
  const msg = el('div', { id: 'play-msg', role: 'alert' });
  const tokOut = el('div', { id: 'tok-out' });
  const probeOut = el('div', { id: 'probe-out' });
  const progress = el('section', { class: 'card', id: 'progress', hidden: true });

  root.append(
    el('section', { class: 'card' }, el('h2', { text: '입력' }), promptEl, count, settings.root,
      el('div', { class: 'row' }, btnRun, btnTok, btnProbe), msg, probeOut),
    tokOut, progress);

  // ---- 토큰화만 보기 (기록을 남기지 않는 미리보기)
  btnTok.addEventListener('click', async () => {
    msg.replaceChildren(); tokOut.replaceChildren();
    try {
      const res = await fetch('./api/tokenize', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: promptEl.value }) });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      tokOut.append(el('section', { class: 'card' }, el('h2', { text: '토큰화 미리보기 (기록되지 않음)' }), tokenStats(body), tokenNotes(body), chips(body.tokens), legend()));
    } catch (e) { msg.append(note('bad', `토큰화 실패: ${e.message}`)); }
  });

  // ---- logprobs 지원 확인
  btnProbe.addEventListener('click', async () => {
    btnProbe.disabled = true; probeOut.replaceChildren(note('', '확인 중…'));
    try {
      const res = await fetch(`./api/probe?model=${encodeURIComponent(settings.getModel())}`);
      const r = await res.json();
      if (!res.ok) throw new Error(r.error || `HTTP ${res.status}`);
      const v = { supported: ['ok', '지원: 응답에 logprobs 필드가 있습니다.'], no_field: ['warn', '이 버전은 미지원: 요청은 성공했지만 응답에 확률 필드가 없습니다.'],
        error: ['bad', '오류: 확률 요청이 거절되었습니다(미지원일 수 있으나 단정하지 않음).'] }[r.verdict];
      probeOut.replaceChildren(note(v[0], v[1]), el('details', {}, el('summary', { class: 'meta', text: '서버가 돌려준 원문(JSON)' }), el('pre', { text: JSON.stringify(r, null, 2) })));
    } catch (e) { probeOut.replaceChildren(note('bad', `확인 실패: ${e.message}`)); }
    btnProbe.disabled = false;
  });

  // ---- 실행: 스트림을 읽으며 진행을 보여 주고, 끝나면 기록 보기로 이동
  let controller = null;
  btnRun.addEventListener('click', async () => {
    msg.replaceChildren(); progress.replaceChildren(); progress.hidden = false;
    btnRun.disabled = true;
    controller = new AbortController();
    const liveOut = el('div', { id: 'live-out', 'aria-live': 'polite' });
    const outNode = document.createTextNode('');
    liveOut.append(outNode);
    const gaps = []; const svg = gapChart(gaps);
    const compare = el('div', { id: 'live-compare', class: 'meta' });
    const info = el('div', { id: 'live-info', class: 'meta' });
    progress.append(el('h2', { text: '실행 중' }), info, liveOut, el('h3', { text: '조각별 간격 (직전 조각과의 시간)' }), svg, compare);
    let runId = null, done = null, errMsg = null, joined = '', firstBrowserMs = null;
    const tStart = performance.now();
    const body = settings.getBody(promptEl.value);
    try {
      const res = await fetch('./api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: controller.signal });
      if (!res.ok) {
        let m = `HTTP ${res.status}`;
        try { m = (await res.json()).error || m; } catch (e) { /* JSON이 아닌 오류 응답 */ }
        throw new Error(m);
      }
      const reader = res.body.getReader(), dec = new TextDecoder('utf-8');
      let buf = '';
      const handle = (line) => {
        if (!line.trim()) return;
        const ev = JSON.parse(line);
        if (ev.type === 'start') {
          runId = ev.run_id;
          info.textContent = `run_id ${ev.run_id} · model ${ev.model} · options ${JSON.stringify(ev.options)} · 로컬 토큰 ${ev.local_tokens}`;
        } else if (ev.type === 'chunk') {
          if (firstBrowserMs === null) firstBrowserMs = performance.now() - tStart;
          joined += ev.text; outNode.appendData(ev.text); gaps.push(ev.gap_ms); drawGaps(svg, gaps, false);
          compare.textContent = `브라우저가 이은 글: ${[...joined].length}자 · ${utf8Len(joined)}바이트 (실행 중에만 보이는 비교)`;
        } else if (ev.type === 'done') { done = ev; runId = ev.run_id || runId; }
        else if (ev.type === 'error') { errMsg = ev.message; runId = ev.run_id || runId; }
      };
      for (;;) {
        const { value, done: fin } = await reader.read();
        if (fin) { buf += dec.decode(); break; }
        buf += dec.decode(value, { stream: true });
        const parts = buf.split('\n'); buf = parts.pop();
        parts.forEach(handle);
      }
      handle(buf);
    } catch (e) { errMsg = errMsg || e.message; }
    if (done) {
      const equal = joined === done.joined;
      compare.textContent = `서버가 이은 글과 ${equal ? '같음' : '다름'} · 브라우저 ${[...joined].length}자 · ${utf8Len(joined)}바이트${firstBrowserMs === null ? '' : ` · 첫 글자까지(브라우저 기준) ${fmtMs(firstBrowserMs)}`}`;
      if (runId) browserCompare.set(runId, { equal, chars: [...joined].length, bytes: utf8Len(joined), firstBrowserMs });
    }
    if (errMsg) msg.append(note('bad', `오류: ${errMsg}`));
    btnRun.disabled = false;
    invalidateLatest();
    if (runId && RUN_ID_RE.test(runId)) window.location.hash = `#/run/${runId}`;    // 기록은 done/error 사건 전에 이미 쓰였다
    else info.append(document.createTextNode(` · 기록 없음(${fmtNum(gaps.length, 0)}개 조각)`));
  });

  return () => { if (controller) controller.abort(); };       // 화면을 떠나면 진행 중인 요청을 끊는다(서버는 aborted로 기록)
}
