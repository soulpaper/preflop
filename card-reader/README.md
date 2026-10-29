# Card Reader

화면의 지정 영역(보드, 내 카드)에서 카드를 인식해 승률·에쿼티·아우츠를 보여주는 PC 테스트 도구.

- 카드 인식: 템플릿 매칭 (`[카드 학습]`으로 카드 이미지 등록)
- 포스트플랍: 에쿼티, 아우츠(4-2 법칙), 팟 오즈, 내재 배당/셋마이닝, SPR, 폴드 에쿼티, M-ratio
- 프리플랍: 포지션별 오픈/콜/3벳/4벳 대응, 숏스택 잼/콜 범위
- 팟·스택 자동 입력: Windows 내장 OCR (한국어 언어 팩 필요)
- `[자동 지정]`: 게임 창을 한 번 드래그하면 보드·내 카드·팟·내 스택·상대 스택·콜 버튼 위치를 찾아 저장 (`layout.py`).
  팟 = 화면 POT − 콜 금액, 상대 벳 = 콜 버튼 금액. 게임 창을 옮기거나 크기를 바꾸면 다시 실행

## 실행

```
pip install -r requirements.txt
python reader.py
```

## exe 빌드

```
pyinstaller --onefile --windowed --name CardReader --collect-all winrt --hidden-import names --hidden-import handlog --hidden-import profiles --hidden-import handread --hidden-import rangeq --hidden-import recorder --hidden-import userinfo --hidden-import advisor --hidden-import tablestate --hidden-import solverlib --hidden-import icm --hidden-import eqtable --hidden-import decisionlog --add-data "eqtable/preflop_random.json;eqtable" reader.py
```

exe 옆에 `templates/`, `font_glyphs/`, `gto_lib/` 를 둔다.

`reader.py`는 위 모듈들을 "있으면 사용"(`_optional`)으로 불러오므로 PyInstaller가 자동으로 찾지 못한다.
`--hidden-import`를 빼면 exe에서 추천·상황 인식·기록이 조용히 꺼진다.

`config.json`과 `templates/`는 exe 옆에 생성된다.

## 학습 데이터

`templates/`에 게임 화면에서 뽑은 실제 카드 템플릿이 들어 있다 (30종). exe로 쓸 때는 이 폴더를 exe 옆에 두면 된다.
나머지 카드는 실행 중 `[카드 학습]`으로 추가한다.

전체 카드 매칭이 실패하면 숫자와 무늬를 따로 인식해 조합한다 (`glyphs.py`, 예: 6♦와 A♣를 학습했으면 6♣ 인식).
원형은 시작할 때 `templates/`의 실제 템플릿으로 만든다. 실제 샘플이 없는 숫자(7 등)는 `font_glyphs/`의 글꼴 숫자가
경쟁자로만 쓰인다 (그 숫자가 이기면 `?`). exe로 쓸 때는 `font_glyphs/`도 exe 옆에 둔다.

라벨이 틀린 템플릿은 `templates_quarantine/`으로 옮겨 둔다 (예: 7c로 저장됐던 J♣).

## 추천 엔진 (`advisor.py`)

- 프리플랍: `preflop.py` 레인지 (앤티·유효 스택에 따라 연속 변화, `looseness` -1~+1) + 팟 오즈·범위 승률 (`rangeq.py`)
- 포스트플랍(헤즈업): 솔버 데이터로 맞춘 기준표 `HU_GTO`. 다자 팟은 그 위에 다자 보정
- 상대 패 예상: `handread.py` (행동·성향 기반 범위), 행동 기록 `handlog.py`, 성향 DB `profiles.py`, 게임 '유저 정보' 팝업 읽기 `userinfo.py`
- 테스트: `python test_advisor.py`, `test_rangeq.py`, `test_handread.py`, `test_handlog.py`, `test_solverlib.py`

### GTO 데이터

- 포스트플랍 기준 보정: [jevonmao/postflop-solver-reasoning-traces-1m](https://huggingface.co/datasets/jevonmao/postflop-solver-reasoning-traces-1m) (CC-BY-4.0)
  으로 채점·보정 (`bench_gto.py`, `tune_gto.py`, 데이터 경로는 스크립트 상단).
- 토너먼트 상황 라이브러리: [TexasSolver](https://github.com/bupticybee/TexasSolver) (AGPL-3.0, 별도 설치)로 직접 풀어
  `gto_lib/`에 저장 (`python build_gto_lib.py`, `--status`, `--stop`, `--set-mode low|full`). 생성물이라 저장소에는 넣지 않는다.
  헤즈업 플랍·턴에서 같거나 아주 비슷한 상황이면 GTO 빈도를 주 추천으로, 아니면 'GTO 참고'로 보여준다.
