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
pyinstaller --onefile --windowed --name CardReader --collect-all winrt --hidden-import masterlock --hidden-import masterlock_build --hidden-import names --hidden-import handlog --hidden-import profiles --hidden-import handread --hidden-import rangeq --hidden-import recorder --hidden-import userinfo --hidden-import advisor --hidden-import tablestate --hidden-import solverlib --hidden-import icm --hidden-import eqtable --hidden-import decisionlog --hidden-import showdown --hidden-import gto_update --hidden-import gto_dist_common --add-data "eqtable/preflop_random.json;eqtable" reader.py
```

exe 옆에 `templates/`, `templates_shown/`, `font_glyphs/`, `gto_lib/` 를 둔다.

복기 도구 exe:

```
pyinstaller --onefile --name ReviewApp --hidden-import masterlock --hidden-import masterlock_build review_app.py
```

`reader.py`와 `review_app.py`는 시작할 때 마스터 비밀번호를 묻는다 (`masterlock.py`). 취소하거나 3회 틀리면 종료한다.
`masterlock`은 `_optional`로 불러오지 않는다. 빠지면 잠금이 풀리기 때문이다.
솔트와 해시는 코드에도 저장소에도 없다. 값이 없거나 형식이 틀리거나 반복 수가 60만 회 미만이면 입력창을 띄우기 전에 실행을 거부한다.

### 마스터 잠금 설치

값은 `masterlock_setup.py`(표준 라이브러리만)로 만든다. 비밀번호는 getpass로 두 번 입력하거나 `--stdin`(첫 줄)으로만 받고, 명령행 인자로는 받지 않는다. 해시와 비밀번호는 출력하지 않는다.

소스 실행 (`python reader.py`, `python review_app.py`): 사용자 설정 파일을 읽는다.

- Linux: `~/.config/card-reader/masterlock.json` (`XDG_CONFIG_HOME`이 있으면 그 아래 `card-reader/`)
- Windows: `%APPDATA%\card-reader\masterlock.json`

```
python masterlock_setup.py            # 위 위치에 만든다. 이미 있으면 --force 없이는 거부
```

Linux에서는 폴더 700, 파일 600으로 만든다. Windows에서는 `chmod 600`이 ACL을 대신하지 않으므로 `%APPDATA%` 폴더가 다른 계정에 열려 있지 않은지 직접 확인한다.

exe: 실행 중에는 사용자 설정 파일을 읽지 않고, 빌드 때 넣은 `masterlock_build` 모듈의 값만 쓴다 (파일 하나 놓는 것으로 비밀번호가 바뀌면 안 되기 때문).

1. 빌드 전에 `python masterlock_setup.py --write-build-module`. 사용자 설정 파일이 있으면 그 값을 옮기고, 없으면 새로 만든다. 같은 PC에서 만든 두 빌드(CardReader, ReviewApp)는 이 파일 하나를 공유한다.
2. 위 pyinstaller 명령 2개를 실행한다 (`--hidden-import masterlock_build` 포함).
3. 빌드 후 `masterlock_build.py`를 지우거나 안전한 곳에 보관한다. `.gitignore`에 들어 있어 커밋되지는 않는다.

빌드 PC에 `masterlock_build.py`가 없으면 exe는 만들어지더라도 실행을 거부한다.

한계: 코드가 공개이므로 소스에서 `reader.py`의 잠금 호출을 지우거나 모듈을 직접 불러 쓰면 우회할 수 있다. exe 안의 해시도 꺼낼 수 있어서, 약한 비밀번호는 오프라인으로 추측당할 수 있다. 이 잠금은 우연한 실행을 막는 정도이고 강한 보안 경계가 아니다.

`reader.py`는 위 모듈들을 "있으면 사용"(`_optional`)으로 불러오므로 PyInstaller가 자동으로 찾지 못한다.
`--hidden-import`를 빼면 exe에서 추천·상황 인식·기록이 조용히 꺼진다.

`config.json`과 `templates/`는 exe 옆에 생성된다.

## GTO 데이터 공유 (sharebox 공간)

서버 PC가 계산한 `gto_lib`을 sharebox의 GTO 전용 공간에 올리고, 각 PC의 CardReader가 변경분만 받아 간다.
설계는 `홀덤연구소/sharebox-gto-design.md`. 공간 API는 sharebox 쪽(`/api/spaces/gto/...`, 토큰 역할 `space:gto:read|write`)이 구현되어 있어야 한다.
`gto_update.json`을 읽는 쪽은 로컬 파일을 삭제하지 않고 파일 단위로만 교체한다. 서버가 꺼져 있어도 기존 데이터로 동작한다.

**서버 PC (업로더, 15분 타이머)** — 러너(`build_gto_lib.py`)와는 프로세스, 잠금, 쓰기를 공유하지 않고 `gto_lib`을 읽기만 한다.
1. sharebox에서 `shareboxd token add gto-uploader space:gto:write`로 토큰을 발급한다. 토큰은 저장소에 넣지 않는다.
2. `~/.config/gto-dist/sync.json`(디렉터리 700, 파일 600)을 만든다.
   `{"url":"http://127.0.0.1:8737","token":"<업로드 토큰>","lib_dir":"<gto_lib 경로>","cache":"~/.local/state/gto-dist/sync_cache.json"}`
   (업로더는 루프백 http 주소만 받는다.)
3. `gto_dist_common.py`, `gto_dist_sync.py`를 `~/.local/share/gto-dist/app/`에 복사하고 `deploy/gto-dist-sync.service`, `.timer`를 `~/.config/systemd/user/`에 복사한다.
4. `systemctl --user daemon-reload && systemctl --user enable --now gto-dist-sync.timer`, `journalctl --user -u gto-dist-sync`로 첫 게시를 확인한다.
- `sync.json`이 있는 PC에서는 `gto_update`가 시작을 거부한다(러너의 원본 폴더 보호).
- 업로드 실패는 exit 1이고 다음 타이머가 재시도한다. 계산에는 영향이 없다.

**각 PC (CardReader)** — exe 옆에 `gto_update.json`을 둔다(git 제외).
```
{"enabled":true,
 "urls":["https://<LAN 주소>:1213","https://<Tailscale 주소>:1213","https://<공인 주소>:1213"],
 "token":"<읽기 토큰, PC별 발급>","cert_sha256":"<shareboxd cert-pin 값>",
 "check_interval_sec":1800,"max_bytes_per_sec":5242880}
```
- 읽기 토큰은 `shareboxd token add gto-read-<PC이름> space:gto:read`로 PC마다 발급한다. 폐기하면 즉시 401이 된다.
- 인증서 지문이 다르면 토큰을 보내기 전에 연결을 끊는다. 주소가 바뀌면 JSON만 고친다(exe 재빌드 불필요).
- 첫 설치는 5MiB/s 기준 수 분이 걸린다. 여러 PC가 동시에 처음 받는 것은 피한다.
- 테스트: `python3 test_gto_dist.py` (실제 shareboxd 통합 시험은 `GTO_TEST_SHAREBOXD=<바이너리 경로>`가 있을 때만 실행).

## GTO 계산 러너 서비스 (systemd 사용자 유닛)

`deploy/`의 `gto-runner.service`(러너), `gto-mode.service` + `gto-mode.timer`(04:00 full, 21:00 low 전환)로 `build_gto_lib.py`를 서비스로 돌린다.
시각별 모드는 04~21시 `--full`, 그 외 `--low`이고 워커 수는 기본 2(`GTO_WORKERS`로 변경)다. 경로는 환경 파일로 받는다.

환경 파일 `~/.config/gto-runner/env` (디렉터리 700, 파일 600, 저장소 제외):
```
PREFLOP_TOOLS=<TexasSolver와 solves/가 있는 폴더>
GTO_LIB_DIR=<gto_lib 출력 폴더>
CARD_READER_DIR=<card-reader 저장소 폴더>
```
- `solves/STOP`이 있으면 시작 스크립트가 아무것도 하지 않고 exit 0으로 끝난다(`Restart=on-failure`라 재시작되지 않음). 로그는 `solves/batch-service.log`에 이어 쓴다.
- 러너는 잠금 PID가 실제 `build_gto_lib` 러너인지 `/proc/<pid>/cmdline`으로 확인하고, 아니면 낡은 잠금으로 처리한다. `--stop --now`와 `--set-mode`도 `console_solver`로 확인된 PID에만 작용한다. 그래서 유닛에 `runner.lock` 삭제 단계가 없다.
- 시험: `python3 test_build_gto_safety.py`, `bash deploy/test_gto_deploy.sh`. 문법: `systemd-analyze --user verify deploy/gto-runner.service deploy/gto-mode.service deploy/gto-mode.timer`

**수동 실행 중인 러너에서 이전** (진행 중 솔브를 잃지 않으려면 `--now`를 쓰지 않는다)
1. `python3 build_gto_lib.py --stop` (STOP 파일 생성). 진행 중 솔브가 모두 끝나 러너가 스스로 종료할 때까지 기다린다(`--status`).
2. 러너 프로세스와 `console_solver`가 없고 `runner.lock`의 PID가 죽었는지 확인한다(`pgrep -af 'build_gto_lib|console_solver'`).
3. 환경 파일을 만들고 유닛 3개를 `~/.config/systemd/user/`에 복사, `systemctl --user daemon-reload`.
4. `solves/STOP`을 지운다.
5. `systemctl --user start gto-runner.service` 후 `python3 build_gto_lib.py --status`와 `solves/batch-service.log`로 확인한다.
6. 정상이면 `systemctl --user enable gto-runner.service` (`[Install] WantedBy=default.target`이라 부팅 때 자동 시작)와 `systemctl --user enable --now gto-mode.timer`. 로그인하지 않은 부팅에서도 돌리려면 `loginctl enable-linger "$USER"`가 필요하다(`loginctl show-user "$USER" -p Linger`로 확인).

주의: `systemctl --user stop gto-runner.service`(와 재부팅)는 SIGTERM 뒤 control-group 전체를 정리하므로 진행 중 솔브가 사라진다. 완료된 솔브는 보존되고 다음 시작 때 이어서 푼다. 솔브를 지키며 멈추려면 `--stop`을 쓴다. 이미 돌고 있는 러너에는 코드 수정이 적용되지 않으므로 재시작 때 반영된다.

### 온도 적응 모드 (`deploy/gto-thermal.service`)

`deploy/gto_thermal.py`가 10초마다 CPU 온도(k10temp Tctl)를 읽어 돌고 있는 `console_solver`의 CPU 선호도(affinity)만 조절한다. 스레드 수와 nice는 건드리지 않는다. 설계와 실측 근거는 `홀덤연구소/thermal-design.md`.

| 단계 | 이름 | 솔버 CPU 선호도 | 새 솔브 배정 |
|---|---|---|---|
| 3 | full | 0-11 | 함 |
| 2 | normal | 0-9 | 함 |
| 1 | cool | 0,2,4 | 함 |
| 0 | hold | 0,2,4 | 안 함 (진행 중 솔브는 계속) |

- 상한(ceiling): `control.json`의 `low=true` → normal, `low=false` → full (파일이 없으면 normal). 실제 단계 = min(상한, 온도 단계)
- 감속: 5분 평균 > 91°C → 한 단계 내림(최소 120초 간격). 30초 평균 ≥ 93°C → 즉시 cool
- 배정 중지: 30초 평균 ≥ 95°C가 60초 이어지면 hold. 30초 평균 < 90°C면 풀림
- 복귀: 5분 평균 < 88.5°C, 30초 평균 < 91°C, 현재 단계에 300초 이상 있었을 때 한 단계 올림
- 시작 후 50분이 지난 솔브는 cool/hold에서도 normal(0-9) 선호도를 준다(러너의 75분 제한 보호). 온도 읽기가 3번 연속 실패하면 cool + hold 해제
- 임계값·시간은 `GTO_THERMAL_<이름>` 환경변수로 덮어쓴다(`deploy/gto_thermal.py`의 `DEFAULTS` 참고, 예: `GTO_THERMAL_SLOW_AVG5=90`)
- `$PREFLOP_TOOLS/solves/thermal.json`: `{"level","name","hold","temp","avg5m","updated"}` (원자적 쓰기). 러너는 `updated`가 120초 안이고 `hold=true`일 때만 새 솔브 배정을 미룬다. 제어기가 죽거나 파일이 낡으면 러너는 그대로 돈다. `--status`에도 한 줄 나온다
- 기록: `~/.local/state/gto-thermal/thermal.csv` (`ts,tctl_c,ppt_w,avg30s,avg5m,level,hold,ceiling,solver_pcpu`, 10초마다). 하루 지나면 `thermal-YYYYMMDD.csv`로 넘기고 14일 지난 것은 삭제. 폴더는 `GTO_THERMAL_STATE_DIR`로 변경
- SIGTERM(서비스 중지) 때는 솔버 선호도를 상한 단계 것으로 되돌리고 `hold=false`를 쓴다
- 시험/확인: `python3 deploy/test_gto_thermal.py`, `python3 deploy/gto_thermal.py --dry-run --once`(온도만 읽고 선호도·파일은 건드리지 않음), 온도 주입은 `GTO_THERMAL_TEMP_FILE=<파일>`
- 설치: `cp deploy/gto-thermal.service ~/.config/systemd/user/ && systemctl --user enable --now gto-thermal.service`. 선호도 조절은 바로 동작하고, hold(배정 중지)는 러너가 이 코드로 재시작된 뒤부터 동작한다. 온도로 제어하므로 `gto-mode.timer`는 꺼 둔다(상한은 `control.json` 그대로)
- 러너 시작 모드 고정: `~/.config/gto-runner/env`에 `GTO_MODE=low`(또는 `full`)를 두면 시작 스크립트가 시각 대신 그 모드로 시작한다. 없으면 이전처럼 시각(04~21시 full)으로 고른다. 실측상 low(5코어)가 full(6코어)보다 5% 느리고 4°C 시원해서 온도 적응 모드에서는 low를 권장

## 학습 데이터

`templates/`에 게임 화면에서 뽑은 실제 카드 템플릿이 들어 있다 (30종). exe로 쓸 때는 이 폴더를 exe 옆에 두면 된다.
나머지 카드는 실행 중 `[카드 학습]`으로 추가한다.

전체 카드 매칭이 실패하면 숫자와 무늬를 따로 인식해 조합한다 (`glyphs.py`, 예: 6♦와 A♣를 학습했으면 6♣ 인식).
원형은 시작할 때 `templates/`의 실제 템플릿으로 만든다. 실제 샘플이 없는 숫자(7 등)는 `font_glyphs/`의 글꼴 숫자가
경쟁자로만 쓰인다 (그 숫자가 이기면 `?`). exe로 쓸 때는 `font_glyphs/`도 exe 옆에 둔다.

라벨이 틀린 템플릿은 `templates_quarantine/`으로 옮겨 둔다 (예: 7c로 저장됐던 J♣).

### 쇼다운 공개 카드 (`showdown.py`)

쇼다운에서 상대가 보여 준 카드 2장(그 좌석 아바타 위에 작게 뜸)을 읽어 핸드 기록의 `seats[k].shown` /
`showdown.shown` 에 넣는다 (나중에 상대 예상을 실제 패로 채점). 리버/올인 등 쇼다운일 수 있을 때만
(`handlog.Tracker.showdown_watch`) 남은 상대 자리만 보고, 좌석마다 확정되면 그 핸드에서는 더 안 읽는다.
템플릿은 `templates/`(보드 카드) + `templates_shown/`(공개 카드, 기록 화면에서 뽑아 눈으로 확인한 48종).
`templates_shown/`에 없는 카드는 같은 색 다른 무늬(♣↔♠, ♦↔♥)와 헷갈릴 수 있어 무늬 판별이 따로 확인할 때만 인정한다.
공개 카드가 아바타를 덮으면 흑백(폴드)처럼 보이므로, 카드를 보여 준 좌석의 그 폴드는 기록에서 되돌린다.

## 추천 엔진 (`advisor.py`)

- 프리플랍: `preflop.py` 레인지 (앤티·유효 스택에 따라 연속 변화, `looseness` -1~+1) + 팟 오즈·범위 승률 (`rangeq.py`)
- 포스트플랍(헤즈업): 솔버 데이터로 맞춘 기준표 `HU_GTO`. 다자 팟은 그 위에 다자 보정
- 상대 패 예상: `handread.py` (행동·성향 기반 범위), 행동 기록 `handlog.py`, 성향 DB `profiles.py`, 게임 '유저 정보' 팝업 읽기 `userinfo.py`
- 테스트: `python test_advisor.py`, `test_rangeq.py`, `test_handread.py`, `test_handlog.py`, `test_solverlib.py`
- 상대 패 예상 전환 스위치 (`handread.py`): `MULT_SET`("current"/"prev" = b538c3d 이전 배율), `PRE_FLOOR`(0.07, 0 이면 차트 밖 패 가중치 없음), `STYLE_STRENGTH`(유형 보정 세기). `hr.configure(...)` / `with hr.switches(...)` 로 바꾸고 복원한다. `score_reads.py` 는 이 스위치로 기여를 하나씩 켜 보고(`--boot B`, `--no-ablation`) 부트스트랩 구간으로 유지/되돌림을 권고한다.
- 동작 변경: `STYLE_STRENGTH` 기본값이 0.5 라서 상대 유형(LAG/LP/TAG/TP) 보정이 e815500(1.0)보다 절반 세기로 적용된다. 앱의 상대 패 예상이 e815500 과 달라진다 (`STYLE_STRENGTH=1.0` 으로 되돌릴 수 있음). `handlog.derive` 의 3벳 집계는 정리된 프리플랍(`clean_preflop`)을 쓴다.

### GTO 데이터

- 포스트플랍 기준 보정: [jevonmao/postflop-solver-reasoning-traces-1m](https://huggingface.co/datasets/jevonmao/postflop-solver-reasoning-traces-1m) (CC-BY-4.0)
  으로 채점·보정 (`bench_gto.py`, `tune_gto.py`, 데이터 경로는 스크립트 상단).
- 토너먼트 상황 라이브러리: [TexasSolver](https://github.com/bupticybee/TexasSolver) (AGPL-3.0, 별도 설치)로 직접 풀어
  `gto_lib/`에 저장 (`python build_gto_lib.py`, `--status`, `--stop`, `--set-mode low|full`). 생성물이라 저장소에는 넣지 않는다.
  헤즈업 플랍·턴에서 같거나 아주 비슷한 상황이면 GTO 빈도를 주 추천으로, 아니면 'GTO 참고'로 보여준다.
