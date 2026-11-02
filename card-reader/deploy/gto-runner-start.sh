#!/bin/bash
# gto-runner.service 의 ExecStart. 환경은 EnvironmentFile(~/.config/gto-runner/env)에서 받는다.
#   PREFLOP_TOOLS   (필수) TexasSolver 와 solves/ 가 있는 폴더
#   CARD_READER_DIR (필수) build_gto_lib.py 가 있는 폴더
#   GTO_LIB_DIR     (선택) gto_lib 출력 폴더 (build_gto_lib/solverlib 가 직접 읽는다)
#   GTO_WORKERS     (선택) 동시 솔브 수, 기본 2
# solves/STOP 이 있으면 아무것도 하지 않고 exit 0 (Restart=on-failure 라 재시작되지 않는다).
# 시험용: GTO_HOUR 로 시각 주입, GTO_RUNNER_CMD 로 러너 명령 교체(기본 python3 $CARD_READER_DIR/build_gto_lib.py).
set -eu
: "${PREFLOP_TOOLS:?PREFLOP_TOOLS 가 없습니다 (~/.config/gto-runner/env)}"
: "${CARD_READER_DIR:?CARD_READER_DIR 가 없습니다 (~/.config/gto-runner/env)}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/gto-mode-lib.sh"
solves="$PREFLOP_TOOLS/solves"
mkdir -p "$solves"
exec >>"$solves/batch-service.log" 2>&1
if [ -e "$solves/STOP" ]; then
    echo "$(date '+%F %T') STOP 파일이 있어 시작하지 않습니다 (exit 0)"
    exit 0
fi
mode="$(gto_mode)"
workers="${GTO_WORKERS:-2}"
echo "$(date '+%F %T') 시작: --$mode --workers $workers"
cd "$CARD_READER_DIR"
if [ -n "${GTO_RUNNER_CMD:-}" ]; then
    exec $GTO_RUNNER_CMD "--$mode" --workers "$workers"
fi
exec python3 -u build_gto_lib.py "--$mode" --workers "$workers"
