# Card Reader

화면의 지정 영역(보드, 내 카드)에서 카드를 인식해 승률·에쿼티·아우츠를 보여주는 PC 테스트 도구.

- 카드 인식: 템플릿 매칭 (`[카드 학습]`으로 카드 이미지 등록)
- 포스트플랍: 에쿼티, 아우츠(4-2 법칙), 팟 오즈, 내재 배당/셋마이닝, SPR, 폴드 에쿼티, M-ratio
- 프리플랍: 포지션별 오픈/콜/3벳/4벳 대응, 숏스택 잼/콜 범위
- 팟·스택 자동 입력: Windows 내장 OCR (한국어 언어 팩 필요)

## 실행

```
pip install -r requirements.txt
python reader.py
```

## exe 빌드

```
pyinstaller --onefile --windowed --name CardReader --collect-all winrt reader.py
```

`config.json`과 `templates/`는 exe 옆에 생성된다.

## 학습 데이터

`templates/`에 게임 화면에서 뽑은 실제 카드 템플릿이 들어 있다 (30종). exe로 쓸 때는 이 폴더를 exe 옆에 두면 된다.
나머지 카드는 실행 중 `[카드 학습]`으로 추가한다.

전체 카드 매칭이 실패하면 숫자와 무늬를 따로 인식해 조합한다 (`glyphs.py`, 예: 6♦와 A♣를 학습했으면 6♣ 인식).
원형은 시작할 때 `templates/`의 실제 템플릿으로 만든다. 실제 샘플이 없는 숫자(7 등)는 `font_glyphs/`의 글꼴 숫자가
경쟁자로만 쓰인다 (그 숫자가 이기면 `?`). exe로 쓸 때는 `font_glyphs/`도 exe 옆에 둔다.

라벨이 틀린 템플릿은 `templates_quarantine/`으로 옮겨 둔다 (예: 7c로 저장됐던 J♣).
