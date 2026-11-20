# 공개 사이트 내보내기와 배포 절차

장애 분석 기록, 문서, 그림 중 **사람이 고른 것만** 정적 사이트(`dist/` 아래)로 만들어 GitHub Pages로 공개하는 순서다.
이 문서의 명령은 모두 예시이며, `<...>`로 쓴 자리는 각자의 값으로 바꾼다.

## 먼저 알아둘 것

- 내보내기는 **git에 커밋된 내용(HEAD)** 만 읽는다. 고치는 중인 파일은 반영되지 않으니, 공개할 문서와 그림은 먼저 커밋한다.
- 실행 기록에는 원문 로그가 그대로 들어 있을 수 있다. 내보내기는 허용 목록과 패턴 검사로 막지만 마스킹은 하지 않는다. 사람이 기록을 읽고 고른다.
- 검사에 걸리면 `dist/`에는 아무것도 만들어지지 않는다. 출력에는 파일, 줄 번호, 패턴 이름만 나오고 값은 나오지 않는다.
- `dist/`는 `.gitignore`에 있어 실수로 커밋되지 않는다.

## 1. 운영에서 기록 꺼내기

기록을 **레포 밖** 폴더에 복사한다. 레포 안에 두면 실수로 커밋될 수 있다.

```bash
mkdir -p <기록 폴더>/runs
docker compose cp watcher:/data/llm_runs.jsonl <기록 폴더>/llm_runs.jsonl
docker compose cp watcher:/data/llm_runs.jsonl.1 <기록 폴더>/llm_runs.jsonl.1   # 파일이 돌려졌을 때만 있다
docker compose cp explorer:/data/runs/. <기록 폴더>/runs/                      # 놀이터 기록(선택)
```

## 2. 공개할 기록 고르기

```bash
python3 scripts/export/export_site.py --runs-src <기록 폴더> --list
```

run_id, 종류, 시각, 상태, 모델, 프롬프트 버전, 장애 번호만 표로 나온다(요약 글과 모델 답은 나오지 않는다).
공개할 기록의 run_id를 적어 두고, 필요하면 기록 파일을 직접 열어 내용을 확인한다.

## 3. allowlist.json 작성

예시 파일 `scripts/export/allowlist.example.json`을 복사해 레포 밖에 두고 고친다.

- `runs`: 공개할 기록. `source`는 `incident` 또는 `playground`, `why`에 공개 이유를 적는다.
- `docs`: 공개할 마크다운 문서(레포 안, git이 추적하는 `.md`)와 제목.
- `figures`: 공개할 그림(레포 안 `.png`). `png_text_reviewed`는 **그림을 눈으로 직접 보고 글자(주소, 이름 등)가 없음을 확인한 뒤에만** `true`로 쓴다.
- `deny_extra`: 공개되면 안 되는 단어(내부 이름 등, 3자 이상). 기록과 문서와 출력 전체에서 대소문자 무시로 찾는다.

## 4. 내보내기 실행

```bash
python3 scripts/export/export_site.py --allowlist <허용 목록 파일> --runs-src <기록 폴더> --out dist/site
```

- 이미 `dist/site`가 있으면 거부한다. 다시 만들 때만 `--force`를 붙인다(그 폴더만 지운다).
- 성공하면 기록, 문서, 그림 개수와 전체 크기만 출력한다. 실패(종료 코드 1)하면 문제 목록이 나오고 `dist/site`는 만들어지지 않는다.

## 5. 결과 확인

1. `dist/site/EXPORT_MANIFEST.json`에서 파일 목록, 크기, sha256, 소스 경로, 커밋 해시를 확인한다. 의도하지 않은 파일이 없어야 한다.
2. 배포될 주소와 같은 **하위 경로** 구조로 띄워 본다.

```bash
mkdir -p <확인 폴더>
cp -a dist/site <확인 폴더>/ai-ops-lab
python3 -m http.server 8080 --bind 127.0.0.1 -d <확인 폴더>
# 브라우저에서 http://127.0.0.1:8080/ai-ops-lab/ 를 열어 홈, 장애 기록, 단계 페이지, 자료 모음 링크를 눌러 본다
```

## 6. gh-pages 고아 브랜치 만들기와 푸시

`gh-pages`는 기존 기록과 이어지지 않는 **고아 브랜치**로 만들어, 코드 이력이 섞이지 않게 한다(git 2.42 이상).

```bash
git worktree add --orphan -b gh-pages <배포 폴더>
cd <배포 폴더>
cp -a <레포>/dist/site/. .            # 점으로 시작하는 .nojekyll까지 복사된다
git add -A
git commit -m "공개 사이트 갱신"
git push origin gh-pages
```

다시 배포할 때는 `<배포 폴더>`의 파일을 `git rm -rf .`로 지운 뒤 같은 방식으로 복사한다(없어진 파일이 남지 않게).

## 7. GitHub Pages 설정

저장소 Settings → Pages에서 Source를 **Deploy from a branch**, Branch를 `gh-pages` / `(root)`로 정하고 저장한다.
잠시 뒤 `https://<계정>.github.io/<저장소>/` 로 열린다. 5번과 같은 화면을 한 번 더 확인한다.

## 8. 되돌리기

1. Settings → Pages에서 Source를 **None**으로 바꿔 공개를 끈다.
2. 원격 브랜치를 지운다: `git push origin --delete gh-pages`
3. 로컬을 정리한다: `git worktree remove <배포 폴더>` 그리고 `git branch -D gh-pages`
4. 한 번 공개된 내용은 복사되거나 검색 서비스에 남을 수 있다. 비밀이 나갔다면 해당 값부터 폐기(재발급)한다.
