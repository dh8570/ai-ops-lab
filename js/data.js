// 데이터 계층: live/static 판별과 파일 읽기. 화면은 ./data, ./content 의 파일만 읽는다.
// 모든 주소는 ./로 시작하는 상대 경로다(하위 경로에 올려도 동작해야 한다).
export const RUN_ID_RE = /^r-\d{8}-\d{6}-[0-9a-f]{4}$/;     // 서버(runs.py)와 같은 식
export const STAGE_ID_RE = /^[a-z][a-z0-9_]*$/;
export const state = { mode: 'static', config: null, modeReason: '' };
// 이 브라우저에서 방금 한 실행의 '브라우저가 이은 글과 서버 글 비교'. 기록 파일에는 서버 쪽 값만 있으므로 기록에는 없다.
export const browserCompare = new Map();

// ./api/config 가 JSON으로 성공하면 live, 아니면(404, 네트워크 오류, JSON이 아닌 응답) static.
export async function detectMode() {
  try {
    const res = await fetch('./api/config');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.config = await res.json();
    state.mode = 'live';
    state.modeReason = '';
  } catch (e) {
    state.config = null;
    state.mode = 'static';
    state.modeReason = String((e && e.message) || e);
  }
  return state.mode;
}

// 못 읽으면 이유를 담은 Error를 던진다(화면이 그대로 보여 준다).
export async function getJSON(path) {
  let res;
  try { res = await fetch(path); } catch (e) { throw new Error(`${path}: 요청 실패 (${(e && e.message) || e})`); }
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}${res.statusText ? ` ${res.statusText}` : ''}`);
  try { return await res.json(); } catch (e) { throw new Error(`${path}: JSON을 읽지 못함 (${(e && e.message) || e})`); }
}

export const getIndex = () => getJSON('./data/index.json');
export function getRun(id) {
  if (!RUN_ID_RE.test(id)) return Promise.reject(new Error(`올바르지 않은 run_id: ${id}`));
  return getJSON(`./data/runs/${id}.json`);
}
export const getStages = () => getJSON('./content/stages.json');
export function getStageContent(id) {
  if (!STAGE_ID_RE.test(id)) return Promise.reject(new Error(`올바르지 않은 단계 id: ${id}`));
  return getJSON(`./content/stages/${id}.json`);
}
export const getSettingsHelp = () => getJSON('./content/settings.json');

// 가장 최근 성공 실행의 tok/s (num_predict 소요 시간 추정에 쓴다). 없으면 tokS: null.
let latest = null;
export const invalidateLatest = () => { latest = null; };
export async function latestTokS() {
  if (latest) return latest;
  latest = { tokS: null, runId: null };
  try {
    const idx = await getIndex();
    const hit = idx.find((e) => e.kind === 'playground' && e.status === 'ok');
    if (hit) {
      const run = await getRun(hit.run_id);
      const d = run.stages.find((s) => s.id === 'decode');
      const v = d && d.output && d.output.tok_s;
      if (typeof v === 'number' && v > 0) latest = { tokS: v, runId: hit.run_id };
    }
  } catch (e) { /* 기록을 못 읽으면 기본값을 쓴다 */ }
  return latest;
}
