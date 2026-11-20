// 저장된 색상 선택('light'|'dark')을 처음 그리기 전에 <html data-theme>에 반영한다(깜빡임 방지).
// 선택이 없거나 저장소를 쓸 수 없으면 data-theme 없이 둔다 = 브라우저·OS 설정을 따른다.
// 모듈이 아닌 classic 스크립트다(<head>에서 바로 실행되어야 해서).
(function () {
  try {
    var saved = window.localStorage.getItem('pipeline-theme');
    if (saved === 'light' || saved === 'dark') {
      document.documentElement.setAttribute('data-theme', saved);
    }
  } catch (e) {
    /* localStorage 접근이 막혀 있음: 시스템 설정을 그대로 따른다 */
  }
})();
