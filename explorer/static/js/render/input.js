// 입력 단계 렌더러: 보낸 글과 이 실행의 설정. 직접 만져 보기(live)는 설정 컨트롤 체험이다.
import { getSettingsHelp, state } from '../data.js';
import { createSettings, saveInitial } from '../settings.js';
import { el, fieldTable, fmtNum, txt } from '../ui.js';

export function io(stage, run) {
  const o = stage.output, s = run.settings, env = run.environment;
  const wrap = el('div');
  wrap.append(fieldTable([
    { name: 'prompt', value: el('div', { class: 'block', text: o.prompt }) },
    { name: 'prompt_chars', value: fmtNum(o.prompt_chars, 0) },
    { name: 'prompt_bytes', value: fmtNum(o.prompt_bytes, 0), note: 'UTF-8 바이트 수' },
  ], stage.evidence));
  wrap.append(el('h3', { text: '이 실행의 설정 (서버가 검사한 값)' }), fieldTable([
    { name: 'model', value: env.model, key: '*' },
    { name: 'temperature', value: txt(s.temperature), key: '*' },
    { name: 'seed', value: 'seed' in s ? txt(s.seed) : '임의 (요청에 넣지 않음)', key: '*' },
    { name: 'num_predict', value: txt(s.num_predict), key: '*' },
    { name: 'num_ctx', value: txt(s.num_ctx), key: '*' },
  ], { '*': 'input' }));       // 설정은 기록의 settings에서 오고 단계의 evidence 키로 가리킬 대상이 없다 → 항상 '입력'(사용자가 정한 값)
  return wrap;
}

export const figure = () => null;

// 설정 컨트롤을 만져 보며 "Ollama에 보낼 options"가 어떻게 바뀌는지 본다. 이 실행의 설정으로 시작한다.
export async function interactive(stage, run) {
  const help = await getSettingsHelp();
  const s = run.settings;
  const settings = createSettings({ models: (state.config && state.config.models) || [run.environment.model], help,
    initial: { model: run.environment.model, temperature: s.temperature, seed: s.seed, num_predict: s.num_predict, num_ctx: s.num_ctx } });
  return el('div', {}, settings.root,
    el('div', { class: 'row' }, el('button', { type: 'button', class: 'primary', on: { click: () => {
      saveInitial({ ...settings.getOptions(), model: settings.getModel(), prompt: stage.output.prompt });
      window.location.hash = '#/play';
    } } }, '이 설정으로 놀이터 열기')),
    el('p', { class: 'meta', text: '여기서 바꾼 값은 기록에 영향을 주지 않습니다. 놀이터에서 실행해야 새 기록이 만들어집니다.' }));
}
