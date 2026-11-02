#!/bin/bash
# gto-mode.timer 가 04:00, 21:00 에 호출한다. 러너 서비스가 active 일 때만 현재 시각의 모드로 --set-mode 를 호출한다.
# 환경: CARD_READER_DIR, PREFLOP_TOOLS (EnvironmentFile). 시험용: GTO_HOUR, GTO_SYSTEMCTL, GTO_RUNNER_CMD.
set -eu
: "${CARD_READER_DIR:?CARD_READER_DIR 가 없습니다 (~/.config/gto-runner/env)}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/gto-mode-lib.sh"
systemctl_cmd="${GTO_SYSTEMCTL:-systemctl}"
if ! $systemctl_cmd --user is-active --quiet gto-runner.service; then
    echo "gto-runner.service 가 active 가 아니어서 모드를 바꾸지 않습니다."
    exit 0
fi
mode="$(gto_mode)"
cd "$CARD_READER_DIR"
if [ -n "${GTO_RUNNER_CMD:-}" ]; then
    exec $GTO_RUNNER_CMD --set-mode "$mode"
fi
exec python3 build_gto_lib.py --set-mode "$mode"
