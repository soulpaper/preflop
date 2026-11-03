#!/bin/bash
# deploy 스크립트 시험. 가짜 환경과 가짜 러너(echo)만 쓰며 실제 러너·systemctl 을 호출하지 않는다.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
fail=0
check() { if [ "$2" = "$3" ]; then echo "[PASS] $1"; else echo "[FAIL] $1: '$2' != '$3'"; fail=1; fi; }
. "$here/gto-mode-lib.sh"
for pair in 0:low 3:low 4:full 12:full 20:full 21:low 23:low 09:full 04:full; do
    check "시각 ${pair%%:*}시 -> ${pair##*:}" "$(GTO_HOUR=${pair%%:*} gto_mode)" "${pair##*:}"
done
mkdir -p "$t/tools" "$t/cr"
printf '#!/bin/sh\necho "FAKE-RUNNER $*"\n' >"$t/fake"; chmod +x "$t/fake"
export PREFLOP_TOOLS="$t/tools" CARD_READER_DIR="$t/cr" GTO_RUNNER_CMD="$t/fake"
log="$t/tools/solves/batch-service.log"
GTO_HOUR=10 bash "$here/gto-runner-start.sh"; check "시작 스크립트 10시: exit 0" $? 0
grep -q -- "FAKE-RUNNER --full --workers 2" "$log"; check "10시 -> --full --workers 2" $? 0
GTO_HOUR=22 GTO_WORKERS=3 bash "$here/gto-runner-start.sh"
grep -q -- "FAKE-RUNNER --low --workers 3" "$log"; check "22시, GTO_WORKERS=3 -> --low --workers 3" $? 0
touch "$t/tools/solves/STOP"; : >"$log"
GTO_HOUR=10 bash "$here/gto-runner-start.sh"; check "STOP 있으면 exit 0" $? 0
grep -q FAKE-RUNNER "$log"; check "STOP 있으면 러너 호출 안 함" $? 1
env -u PREFLOP_TOOLS bash "$here/gto-runner-start.sh" 2>/dev/null; check "PREFLOP_TOOLS 없으면 실패" $? 1
printf '#!/bin/sh\n[ "$3" = active ] && exit "${ACTIVE_RC:-0}"; exit "${ACTIVE_RC:-0}"\n' >"$t/sysctl"; chmod +x "$t/sysctl"
export GTO_SYSTEMCTL="$t/sysctl"
out="$(ACTIVE_RC=0 GTO_HOUR=4 bash "$here/gto-mode-sync.sh")"; check "active 4시 -> --set-mode full" "$out" "FAKE-RUNNER --set-mode full"
out="$(ACTIVE_RC=0 GTO_HOUR=21 bash "$here/gto-mode-sync.sh")"; check "active 21시 -> --set-mode low" "$out" "FAKE-RUNNER --set-mode low"
out="$(ACTIVE_RC=3 GTO_HOUR=21 bash "$here/gto-mode-sync.sh")"; rc=$?
check "inactive: 러너 호출 없음" "$(echo "$out" | grep -c FAKE-RUNNER)" 0
grep -q "^\[Install\]" "$here/gto-runner.service"; check "gto-runner.service 에 [Install] 있음" $? 0
grep -q "^WantedBy=default.target" "$here/gto-runner.service"; check "gto-runner.service WantedBy=default.target" $? 0
exit $fail
