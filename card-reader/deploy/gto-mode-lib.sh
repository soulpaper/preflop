# 시각으로 러너 모드를 고르는 함수 (gto-runner-start.sh, gto-mode-sync.sh 가 source 한다).
# 04~21시(21시 정각 전까지)는 full, 그 외는 low.
# 시험용: GTO_HOUR=0..23 이 있으면 현재 시각 대신 쓴다.
gto_mode() {
    local h="${GTO_HOUR:-$(date +%H)}"
    h=$((10#$h))
    if [ "$h" -ge 4 ] && [ "$h" -lt 21 ]; then echo full; else echo low; fi
}
