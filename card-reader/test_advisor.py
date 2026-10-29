"""advisor.recommend 시나리오 테스트 — python test_advisor.py"""
import contextlib
import gzip
import json
import os
import random
import re
import sys
import time

import advisor
import evaluator as ev
from advisor import recommend

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

KEYS = {"action", "size_bb", "size_text", "reason", "detail", "confidence", "color_key", "plan"}
ACTIONS = {"폴드", "체크", "콜", "벳", "레이즈", "올인", "대기"}
COLORS = {"raise", "call", "fold", "jam", "check"}
results = []
advisor.GTO_ENABLED = False   # GTO 연동 섹션 밖의 테스트는 풀린 플랍(gto_lib 상태)과 무관해야 한다


def eqv(hero, board, opponents):
    """랜덤 핸드 opponents 명 대비 에쿼티 — 고정 시드라 결정적."""
    h = [ev.parse_card(x) for x in hero]
    b = [ev.parse_card(x) for x in board]
    return ev.equity(h, b, opponents=opponents, iterations=3000, rng=random.Random(1))["equity"]


def check(name, ctx, action, size=None, tol=0.3, confidence=None, reason_has=None, detail_has=None, max_conf=None):
    r = recommend(ctx)
    assert set(r) == KEYS, (name, r)
    assert r["action"] in ACTIONS and r["color_key"] in COLORS, (name, r)
    assert r["confidence"] in ("high", "medium", "low"), (name, r)
    assert isinstance(r["detail"], list) and len(r["detail"]) <= 3, (name, r)
    assert isinstance(r["plan"], list) and len(r["plan"]) <= 3, (name, r)
    assert r["reason"], (name, r)
    assert recommend(ctx) == r, f"{name}: 결정적이지 않음"
    ok = r["action"] == action
    if size is not None:
        ok = ok and r["size_bb"] is not None and abs(r["size_bb"] - size) <= tol
    if confidence is not None:
        ok = ok and r["confidence"] == confidence
    if reason_has is not None:
        ok = ok and reason_has in r["reason"]
    if detail_has is not None:
        ok = ok and any(detail_has in d for d in r["detail"])
    if max_conf is not None:  # 멀티웨이는 신뢰도가 이 수준을 넘으면 안 된다
        order = ["low", "medium", "high"]
        ok = ok and order.index(r["confidence"]) <= order.index(max_conf)
    results.append((ok, name, r))
    return r


PF = {"street": "preflop", "board": [], "to_act": True}

# --- 실제 사례 -----------------------------------------------------------------
check("실제: 10인 82s vs 올인 2명 → 폴드",
      dict(PF, hero=["8h", "2h"], players=10, opponents_in_hand=2, hero_pos="CO", situation="vsopen",
           villain_pos="UTG", stack_bb=75.8, my_stack=75.8, call=24.1, pot_total=63.7, can_check=False), "폴드")
check("실제: 82s 상황/포지션 정보 없음 → 폴드",
      dict(PF, hero=["8h", "2h"], players=10, opponents_in_hand=2, my_stack=75.8, call=24.1, pot_total=63.7), "폴드")
check("실제: 87s BTN 첫 진입 → 레이즈 2.5BB",
      dict(PF, hero=["8s", "7s"], players=9, hero_pos="BTN", situation="open", stack_bb=100, my_stack=100,
           call=1, pot_total=1.5, can_check=False), "레이즈", 2.5, 0.01)

# --- 프리플랍 딥 -----------------------------------------------------------------
check("AA UTG 오픈", dict(PF, hero=["Ad", "Ac"], players=6, hero_pos="UTG", situation="open", stack_bb=100,
                          call=1, pot_total=1.5), "레이즈", 2.5, 0.01, confidence="high")
check("72o UTG 폴드", dict(PF, hero=["7d", "2c"], players=6, hero_pos="UTG", situation="open", stack_bb=100,
                          call=1, pot_total=1.5), "폴드")
check("림퍼 2명 BTN AJs → 4.5BB", dict(PF, hero=["Ah", "Jh"], players=8, hero_pos="BTN", stack_bb=100,
                                     call=1, pot_total=3.5), "레이즈", 4.5, 0.01)
check("BTN AKs vs CO 오픈 → 3벳 3배", dict(PF, hero=["As", "Ks"], players=6, hero_pos="BTN", situation="vsopen",
                                      villain_pos="CO", stack_bb=100, call=2.5, pot_total=4.0), "레이즈", 7.5)
check("BB 98s vs BTN 오픈 → 콜", dict(PF, hero=["9c", "8c"], players=6, hero_pos="BB", situation="vsopen",
                                   villain_pos="BTN", stack_bb=100, call=1.5, pot_total=4.0), "콜", 1.5, 0.01)
check("BB AA vs BTN → 3벳 OOP 3.5배", dict(PF, hero=["Ah", "As"], players=6, hero_pos="BB", situation="vsopen",
                                       villain_pos="BTN", stack_bb=100, call=1.5, pot_total=4.0), "레이즈", 8.75)
check("CO QQ vs BTN 3벳 → 4벳 OOP", dict(PF, hero=["Qh", "Qd"], players=6, hero_pos="CO", situation="vs3bet",
                                     villain_pos="BTN", stack_bb=100, call=5, pot_total=11.5), "레이즈", 18.75)
check("KK vs 4벳 → 5벳 올인", dict(PF, hero=["Kh", "Kd"], players=6, hero_pos="CO", situation="vs4bet",
                                 stack_bb=100, call=14, pot_total=33), "올인", 100)
check("큰 레이즈 압박: 76s 콜 레인지 → 폴드", dict(PF, hero=["7h", "6h"], players=6, hero_pos="BTN",
                                         situation="vsopen", villain_pos="CO", stack_bb=100, call=35,
                                         pot_total=36.5), "폴드")
check("QQ 40BB 올인에 → 올인 콜", dict(PF, hero=["Qs", "Qc"], players=6, hero_pos="BTN", situation="vsopen",
                                  villain_pos="CO", stack_bb=40, my_stack=120, call=40, pot_total=41.5,
                                  facing_allin=True), "콜", 40)
check("BB 72o 림프 팟 → 체크", dict(PF, hero=["7s", "2d"], players=6, hero_pos="BB", stack_bb=100, call=0,
                                 can_check=True, pot_total=3.0), "체크")

# --- 숏스택 ---------------------------------------------------------------------
check("12BB BTN A9o → 잼", dict(PF, hero=["Ad", "9s"], players=9, hero_pos="BTN", stack_bb=12, call=1,
                              pot_total=1.5), "올인", 12)
check("BB KQo vs BTN 10BB 잼 → 콜", dict(PF, hero=["Kd", "Qs"], players=6, hero_pos="BB", situation="jamcall",
                                      villain_pos="BTN", stack_bb=10, call=9, pot_total=11.5), "콜", 9)
check("15BB UTG 94o → 폴드", dict(PF, hero=["9d", "4s"], players=9, hero_pos="UTG", stack_bb=15, call=1,
                               pot_total=1.5), "폴드")

# --- 플랍 -----------------------------------------------------------------------
check("셋, 체크로 넘어옴 → 2/3 벳", dict(hero=["7h", "7d"], board=["7c", "Kd", "2s"], opponents_in_hand=1, pot=6,
                                   stack_bb=97, call=0, can_check=True, to_act=True, equity=0.93), "벳", 4.0)
check("넛 플러시 드로우 vs 2/3 벳 → 콜", dict(hero=["Ah", "5h"], board=["Kh", "8h", "2c"], opponents_in_hand=1,
                                        pot=6, pot_total=10, call=4, stack_bb=90, to_act=True, equity=0.48), "콜", 4)
check("에어 vs 벳 → 폴드", dict(hero=["9c", "4d"], board=["Ah", "Ks", "7h"], opponents_in_hand=1, pot=6, call=5,
                            stack_bb=90, to_act=True, equity=0.12), "폴드")
check("콤보 드로우 헤즈업 체크 → 세미블러프 벳", dict(hero=["9h", "8h"], board=["Th", "7h", "2c"], opponents_in_hand=1,
                                           pot=6, stack_bb=97, call=0, can_check=True, to_act=True, equity=0.55),
      "벳", 3.0)
check("멀티웨이 젖은 보드 탑페어 약키커 → 체크", dict(hero=["Jh", "9c"], board=["Jd", "Tc", "8h"], opponents_in_hand=3,
                                           pot=8, stack_bb=100, call=0, can_check=True, to_act=True,
                                           equity=0.45), "체크")
check("멀티웨이 오버페어 QQ vs 벳 → 콜", dict(hero=["Qh", "Qd"], board=["Ks", "7c", "2d"], opponents_in_hand=3,
                                        pot=10, pot_total=14, call=4, stack_bb=100, to_act=True, equity=0.45), "콜")
# 솔버(HU_GTO): 플랍 탑페어 약키커는 그 자체로 밸류 벳 기준(랜덤 에쿼티 85%) 미만 — IP + 프리플랍 레이저일 때 레인지 c-벳으로 1/3
check("드라이 보드 탑페어 약키커 헤즈업 IP 주도권 → 1/3 c-벳", dict(hero=["Kd", "6c"], board=["Kh", "9s", "3d"],
                                             opponents_in_hand=1, pot=5.5, stack_bb=95, call=0, can_check=True,
                                             to_act=True, equity=0.83, hero_ip=True, aggressor=True), "벳", 1.8)
check("같은 탑페어 약키커 OOP (먼저 행동) → 체크", dict(hero=["Kd", "6c"], board=["Kh", "9s", "3d"],
                                             opponents_in_hand=1, pot=5.5, stack_bb=95, call=0, can_check=True,
                                             to_act=True, equity=0.83, hero_ip=False), "체크")
check("낮은 SPR 탑페어 → 올인", dict(hero=["As", "Qd"], board=["Qc", "7d", "3s"], opponents_in_hand=1, pot=20,
                               stack_bb=25, call=0, can_check=True, to_act=True, equity=0.85), "올인", 25)
check("거트샷 vs 팟 벳 → 폴드", dict(hero=["9s", "8s"], board=["Jc", "7d", "2h"], opponents_in_hand=1, pot=6,
                                pot_total=12, call=6, stack_bb=100, to_act=True, equity=0.22), "폴드")
# 솔버(HU_GTO implied=0): 팟 벳에 아웃츠 8장 드로우는 폴드가 최빈 (플랍 에어+드로우 vs 75%+ 벳: 폴드 54% / 콜 31%)
check("양차 vs 팟 벳 딥스택 → 폴드 (솔버: 내재 배당 콜 없음)", dict(hero=["6s", "5s"], board=["8d", "7c", "Kh"], opponents_in_hand=1,
                                         pot=6, pot_total=12, call=6, stack_bb=200, to_act=True, equity=0.43), "폴드")

# --- 턴 / 리버 ------------------------------------------------------------------
check("턴 투페어 드라이 보드 vs 벳 → 레이즈", dict(hero=["Kc", "9d"], board=["Kh", "9s", "4c", "2d"],
                                          opponents_in_hand=1, pot=10, pot_total=17, call=7, stack_bb=80,
                                          to_act=True, equity=0.85), "레이즈", 21)
check("리버 TPTK vs 하프팟 올인 → 올인 콜", dict(hero=["Ac", "Kd"], board=["As", "Qh", "7d", "3c", "2s"],
                                     opponents_in_hand=1, pot=40, pot_total=60, call=20, stack_bb=20, my_stack=20,
                                     to_act=True, equity=0.89), "콜", 20)
# 솔버: 1.25팟 오버벳 올인에 TPTK(블러프캐처)는 폴드가 근소 우세 (리버 올인 + 원페어 랜덤 에쿼티 ~90%: 폴드 52%)
check("리버 TPTK vs 1.25팟 오버벳 올인 → 폴드", dict(hero=["Ac", "Kd"], board=["As", "Qh", "7d", "3c", "2s"],
                                     opponents_in_hand=1, pot=40, pot_total=90, call=50, stack_bb=50, my_stack=50,
                                     to_act=True, equity=0.89), "폴드")
check("리버 바텀페어 vs 올인 → 폴드", dict(hero=["4c", "3c"], board=["As", "Qh", "7d", "3s", "2s"],
                                     opponents_in_hand=1, pot=40, pot_total=90, call=50, stack_bb=50, my_stack=50,
                                     to_act=True, equity=0.35), "폴드")
check("리버 세컨드 페어 체크로 넘어옴 → 체크", dict(hero=["Qc", "Jc"], board=["As", "Qh", "7d", "3c", "2s"],
                                           opponents_in_hand=1, pot=12, stack_bb=80, call=0, can_check=True,
                                           to_act=True, equity=0.5), "체크")

# --- 멀티웨이 (상대 3명 이상 / 림퍼·콜러가 있는 프리플랍) ---------------------------------
# 프리플랍: 림퍼/콜러 수는 팟에서 역산한다 (opponents_in_hand 는 아직 안 낸 사람도 포함)
OPEN6 = dict(PF, players=6, hero_pos="BTN", situation="vsopen", villain_pos="CO", stack_bb=100, call=2.5,
             can_check=False)
OPEN9 = dict(PF, players=9, hero_pos="HJ", situation="vsopen", villain_pos="UTG", stack_bb=100, call=2.5,
             can_check=False)
THREEBET = dict(PF, players=6, hero_pos="CO", situation="vs3bet", villain_pos="BTN", stack_bb=100, call=5,
                can_check=False)
LIMP8 = dict(PF, players=8, stack_bb=100, call=1, can_check=False)
check("다자 프리: 66 BTN vs 오픈 + 콜러 2명 → 셋마이닝 콜", dict(OPEN6, hero=["6h", "6d"], pot_total=9.0), "콜", 2.5, 0.01,
      detail_has="콜러 2명", max_conf="medium")
check("다자 프리: 22 HJ vs UTG 오픈 + 콜러 2명 100BB → 셋마이닝 (레인지 밖이던 핸드)",
      dict(OPEN9, hero=["2h", "2d"], pot_total=9.0), "콜", 2.5, 0.01, reason_has="다자 팟", detail_has="셋마이닝")
check("다자 프리: 22 같은 상황 25BB → 유효 스택 부족 폴드", dict(OPEN9, hero=["2h", "2d"], pot_total=9.0, stack_bb=25), "폴드")
check("다자 프리: A4s BTN vs UTG 오픈 + 콜러 2명 → 수딧 에이스 콜",
      dict(OPEN6, hero=["Ah", "4h"], villain_pos="UTG", pot_total=9.0), "콜", 2.5, 0.01, reason_has="다자 팟")
check("다자 프리: KJo BTN vs CO 오픈 (콜러 없음) → 콜", dict(OPEN6, hero=["Kh", "Jd"], pot_total=4.0), "콜", 2.5, 0.01)
check("다자 프리: KJo BTN vs CO 오픈 + 콜러 1명 → 오프수트 브로드웨이 폴드",
      dict(OPEN6, hero=["Kh", "Jd"], pot_total=6.5), "폴드", detail_has="콜러 1명")
check("다자 프리: AQo BTN 오픈 + 콜러 1명 → 3벳 대신 콜", dict(OPEN6, hero=["Ah", "Qd"], pot_total=6.5), "콜", 2.5, 0.01)
check("다자 프리: BB AA vs BTN 오픈 + 콜러 1명 → 3벳 콜러 가산 사이즈",
      dict(PF, hero=["Ah", "As"], players=6, hero_pos="BB", situation="vsopen", villain_pos="BTN", stack_bb=100,
           call=1.5, pot_total=6.5), "레이즈", 2.5 * 3.5 + 2.5, 0.3)
check("다자 프리: AK CO vs BTN 3벳 + 콜드콜러 → 콜 (블러프 4벳 아님)", dict(THREEBET, hero=["Ah", "Kd"], pot_total=19.0), "콜", 5,
      0.01, detail_has="콜드콜 1명")
check("다자 프리: AA CO vs 3벳 + 콜드콜러 → 4벳 사이즈 +1x", dict(THREEBET, hero=["Ah", "Ad"], pot_total=19.0), "레이즈",
      7.5 * 2.5 + 7.5, 0.3)
check("다자 프리: A5s CO vs 3벳 + 콜드콜러 → 블러프 4벳 포기", dict(THREEBET, hero=["Ah", "5h"], pot_total=19.0), "폴드")
check("다자 프리: A5s CO vs 3벳 (콜드콜러 없음) → 4벳", dict(THREEBET, hero=["Ah", "5h"], pot_total=11.5), "레이즈", 18.75)
check("다자 프리: ATo BTN 림퍼 1명 → 오프수트 타이트닝 폴드", dict(LIMP8, hero=["Ah", "Td"], hero_pos="BTN", pot_total=2.5), "폴드")
check("다자 프리: ATo BTN 림퍼 없음 → 오픈 레이즈", dict(LIMP8, hero=["Ah", "Td"], hero_pos="BTN", pot_total=1.5), "레이즈", 2.5,
      0.01)
check("다자 프리: A2s LJ 림퍼 2명 → 림프 합류", dict(LIMP8, hero=["Ah", "2h"], players=9, hero_pos="LJ", pot_total=3.5), "콜", 1,
      0.01, detail_has="림퍼 2명")
check("다자 프리: BB AKo 림퍼 2명 → 아이솔레이션 5BB", dict(PF, hero=["Ah", "Kd"], players=6, hero_pos="BB", stack_bb=100, call=0,
                                                   can_check=True, pot_total=3.5), "레이즈", 5, 0.01)
check("다자 프리: BB KJo 림퍼 2명 → 체크 (오프수트 타이트닝)", dict(PF, hero=["Kh", "Jd"], players=6, hero_pos="BB", stack_bb=100,
                                                         call=0, can_check=True, pot_total=3.5), "체크")


# 포스트플랍: 에쿼티는 랜덤 N명 대비 (고정 시드)
def mw(hero, board, n, **kw):
    return dict(hero=hero, board=board, opponents_in_hand=n, to_act=True, equity=eqv(hero, board, n), **kw)


K6, K93 = ["Kd", "6c"], ["Kh", "9s", "3d"]
check("다자 플랍: 3인 탑페어 약키커 vs 1/3 벳 → 콜", mw(K6, K93, 2, pot=9, pot_total=12, call=3, stack_bb=100), "콜",
      detail_has="상대 2명")
check("다자 플랍: 4인 탑페어 약키커 vs 팟 벳 → 폴드", mw(K6, K93, 3, pot=10, pot_total=20, call=10, stack_bb=100), "폴드",
      detail_has="상대 3명")
check("헤즈업 비교: 탑페어 약키커 vs 팟 벳 → 콜", mw(K6, K93, 1, pot=10, pot_total=20, call=10, stack_bb=100), "콜")
check("다자 플랍: 4인 너트 플러시 드로우 좋은 오즈 → 콜", mw(["Ah", "5h"], ["Kh", "8h", "2c"], 3, pot=12, pot_total=18, call=3,
                                                   stack_bb=100), "콜", 3, detail_has="상대 3명")
check("다자 플랍: 5인 오버페어 젖은 보드 체크로 넘어옴 → 보호 벳 1/2", mw(["Ah", "Ad"], ["9h", "8h", "2c"], 4, pot=10, call=0,
                                                         can_check=True, stack_bb=100), "벳", 5, 0.3, detail_has="상대 4명")
check("다자 플랍: 4인 TPGK 드라이 보드 → 작은 벳 1/3", mw(["Ad", "Qc"], ["Qh", "7s", "2d"], 3, pot=8, call=0, can_check=True,
                                                 stack_bb=100), "벳", 2.7, 0.3)
check("다자 플랍: 5인 TPGK 드라이 보드 → 체크", mw(["Ad", "Qc"], ["Qh", "7s", "2d"], 4, pot=8, call=0, can_check=True,
                                           stack_bb=100), "체크", detail_has="상대 4명")
check("헤즈업 비교: TPGK 드라이 보드 → 2/3 벳", mw(["Ad", "Qc"], ["Qh", "7s", "2d"], 1, pot=8, call=0, can_check=True,
                                          stack_bb=100), "벳", 5.3)
check("다자 플랍: 4인 투페어 젖은 보드 → 3/4 팟 벳", mw(["Jh", "9c"], ["Jd", "9h", "8h"], 3, pot=10, call=0, can_check=True,
                                                 stack_bb=100), "벳", 7.5, 0.3, max_conf="medium")
check("다자 플랍: 4인 양차 드로우 체크로 넘어옴 → 블러프 자제 체크", mw(["9s", "8s"], ["7d", "6c", "Kh"], 3, pot=8, call=0,
                                                         can_check=True, stack_bb=100), "체크", reason_has="블러프 자제")
check("다자 플랍: 3인 너트 플러시 드로우 → 세미블러프 벳", mw(["Ah", "5h"], ["Kh", "8h", "2c"], 2, pot=8, call=0, can_check=True,
                                                    stack_bb=100), "벳", 4, 0.3, confidence="low")
check("다자 플랍: 4인 에어 체크로 넘어옴 → 체크 (블러프 금지)", mw(["9c", "4d"], ["Ah", "Ks", "7h"], 3, pot=8, call=0,
                                                      can_check=True, stack_bb=100), "체크", reason_has="블러프 자제")
check("다자 턴: 3인 셋 vs 벳 → 밸류 레이즈", mw(["7h", "7d"], ["7c", "Kd", "2s", "3h"], 2, pot=14, pot_total=26, call=12,
                                        stack_bb=90), "레이즈", 36, 0.5)
check("다자 턴: 5인 바텀페어 vs 벳 → 폴드", mw(["3h", "4d"], ["Kc", "Jd", "3s", "9h"], 4, pot=12, pot_total=22, call=10,
                                        stack_bb=90), "폴드")
check("다자 리버: 3인 TPGK 체크로 넘어옴 → 얇은 밸류 1/3", mw(["Ad", "Qc"], ["Qh", "7s", "2d", "4c", "9h"], 2, pot=14, call=0,
                                                  can_check=True, stack_bb=80), "벳", 4.7, 0.3, confidence="low")
check("다자 리버: 4인 TPGK 체크로 넘어옴 → 체크 (얇은 밸류 포기)", mw(["Ad", "Qc"], ["Qh", "7s", "2d", "4c", "9h"], 3, pot=14,
                                                        call=0, can_check=True, stack_bb=80), "체크")
check("다자 리버: 상대 3명 블러프캐처 TPGK vs 큰 벳 → 폴드", mw(["Ad", "Jc"], ["Jh", "7s", "2d", "4c", "9h"], 3, pot=20,
                                                      pot_total=35, call=15, stack_bb=200), "폴드", detail_has="상대 3명")
check("헤즈업 비교: 같은 TPGK 상대 1명 → 콜", mw(["Ad", "Jc"], ["Jh", "7s", "2d", "4c", "9h"], 1, pot=20, pot_total=35, call=15,
                                       stack_bb=200), "콜", 15)
check("다자 리버: 상대 3명 세컨드 페어 vs 팟 벳 → 폴드", mw(["Tc", "9c"], ["Ad", "Td", "5s", "2h", "8c"], 3, pot=20, pot_total=40,
                                                  call=20, stack_bb=100), "폴드")
check("다자 신뢰도: 상대 3명 밸류 벳은 high 아님", mw(["7h", "7d"], ["7c", "Kd", "2s"], 3, pot=8, call=0, can_check=True,
                                           stack_bb=100), "벳", max_conf="medium")

# --- 기타 / 성능 저하 ------------------------------------------------------------
r = check("내 차례 아님 → 계획만, low", dict(PF, hero=["Ah", "Kc"], players=6, hero_pos="CO", stack_bb=100, call=1,
                                         pot_total=1.5, to_act=False), "레이즈", confidence="low",
          reason_has="내 차례 아님")
check("카드 없음 → 판단 보류", {"call": 0, "can_check": True}, "체크", confidence="low")
check("빈 ctx", {}, "폴드", confidence="low")
check("에쿼티·팟 없음 플랍 셋 → 벳 (low)", dict(hero=["7h", "7d"], board=["7c", "Kd", "2s"], call=0, can_check=True),
      "벳", confidence="low")
check("보드 읽는 중 (1장)", dict(hero=["Ah", "Kh"], board=["2c"], call=0, can_check=True), "체크", confidence="low")

# --- 상대 범위 기준 에쿼티 + 프로필 (rangeq) ---------------------------------------------
import rangeq

RB = dict(equity_basis="range")


def prof(**kw):
    return dict({"hands": 80, "reliable": True}, **kw)


def no_prof_note(name, ctx):
    r = recommend(ctx)
    results.append((not any("핸드)" in d for d in r["detail"]), name, r))


TPWK = dict(hero=["Kd", "6c"], board=["Kh", "9s", "3d"], opponents_in_hand=1, pot=10, pot_total=20, call=10,
            stack_bb=100, to_act=True, equity=0.50)
# 솔버(HU_GTO): 플랍 범위 기준 콜은 팟 오즈 + 14%p (실현 보정) — 같은 에쿼티 비교를 50% 로
check("범위: 같은 에쿼티 50%, 랜덤 기준 → 팟 벳에 폴드", TPWK, "폴드", detail_has="랜덤")
check("범위: 같은 에쿼티 50%, 상대 범위 기준 → 랜덤 할인 없이 콜", dict(TPWK, **RB), "콜", 10, detail_has="상대 범위 기준")
check("범위: 3인 팟(상대 2명) 범위 기준은 추가 할인이 작다 → 콜", dict(TPWK, **RB, opponents_in_hand=2, equity=0.56, pot=12, pot_total=24), "콜",
      detail_has="상대 범위 기준")
AIR = dict(hero=["9c", "4d"], board=["Ah", "Ks", "7h"], opponents_in_hand=1, pot=8, stack_bb=100, call=0, can_check=True,
           to_act=True, equity=0.15)
check("프로필: 벳 폴드율 70% 헤즈업 에어 → 블러프 벳", dict(AIR, villain_profile=prof(vpip=30, pfr=15, fold_to_bet=70)), "벳", 2.7,
      0.2, reason_has="블러프 가치", detail_has="VPIP 30 / PFR 15 (80핸드)")
check("프로필: 벳 폴드율 25% 에어 → 체크", dict(AIR, villain_profile=prof(vpip=30, pfr=15, fold_to_bet=25)), "체크")
check("프로필 없음: 같은 에어 → 체크", AIR, "체크")
UNREL = dict(AIR, villain_profile=prof(fold_to_bet=70, hands=15, reliable=False))
check("프로필: 표본 부족(15핸드) → 무시하고 체크", UNREL, "체크")
no_prof_note("프로필: 표본 부족이면 detail 에 프로필 표기 없음", UNREL)
check("프로필: 폴드율 70% 라도 상대 2명이면 블러프 안 함", dict(AIR, opponents_in_hand=2, villain_profile=prof(fold_to_bet=70)), "체크")
FD = dict(hero=["Qh", "5h"], board=["Kh", "8h", "2c"], opponents_in_hand=1, pot=8, stack_bb=100, call=0, can_check=True,
          to_act=True, equity=0.40)
check("비교: 논너트 플러시 드로우 헤즈업 → 세미블러프 벳", FD, "벳", 4.0, 0.2)
check("프로필: 폴드율 25% 상대엔 논콤보 드로우 세미블러프 금지 → 체크", dict(FD, villain_profile=prof(fold_to_bet=25)), "체크",
      reason_has="블러프 금지")
check("프로필: 폴드율 25% 라도 콤보 드로우는 벳 유지", dict(hero=["9h", "8h"], board=["Th", "7h", "2c"], opponents_in_hand=1, pot=6,
                                                    stack_bb=97, call=0, can_check=True, to_act=True, equity=0.55,
                                                    villain_profile=prof(fold_to_bet=25)), "벳", 3.0, 0.2)
TP = dict(hero=["Ad", "Qc"], board=["Qh", "7s", "2d"], opponents_in_hand=1, pot=8, stack_bb=100, call=0, can_check=True,
          to_act=True, equity=0.88)   # evaluator.equity 실제 값 (예전 0.78 은 실제보다 낮았다)
check("비교: TPGK 헤즈업 프로필 없음 → 2/3 벳", TP, "벳", 5.3, 0.1)
check("프로필: VPIP 42 루즈 → 밸류 벳 사이즈업 3/4", dict(TP, villain_profile=prof(vpip=42, pfr=8, hands=64)), "벳", 6.0, 0.1,
      detail_has="VPIP 42 / PFR 8 (64핸드)")
RIV = dict(hero=["Jc", "Jh"], board=["As", "7d", "4c", "3h", "2s"], opponents_in_hand=1, pot=12, stack_bb=80, call=0,
           can_check=True, to_act=True, equity=0.63)   # evaluator.equity 실제 값
check("비교: 리버 포켓 세컨드 페어 에쿼티 63% → 체크", RIV, "체크")
check("프로필: 폴드율 25% 상대엔 얇은 밸류 벳 1/3", dict(RIV, villain_profile=prof(fold_to_bet=25)), "벳", 4.0, 0.2,
      reason_has="얇은 밸류", detail_has="80핸드")
FACE = dict(hero=["Kd", "6c"], board=["Kh", "9s", "3d"], opponents_in_hand=1, pot=10, pot_total=17, call=7, stack_bb=100,
            to_act=True, equity=0.50, **RB)   # 플랍 범위 기준 콜 여유 +14%p(솔버) 만큼 예전 36% 에서 올림
check("비교: 탑페어 약키커 vs 0.7팟 벳 (범위 기준 50%) → 콜", FACE, "콜", 7)
check("프로필: 타이트(VPIP 12) 상대의 레이즈 → 존중해서 폴드", dict(FACE, villain_raised=True, villain_profile=prof(vpip=12, pfr=9)),
      "폴드", detail_has="VPIP 12 / PFR 9 (80핸드)")
check("프로필: 타이트 상대의 단순 벳은 콜 유지", dict(FACE, villain_profile=prof(vpip=12, pfr=9)), "콜", 7)
MED = dict(hero=["Td", "8c"], board=["Ac", "Th", "4d"], opponents_in_hand=1, pot=10, pot_total=20, call=12, stack_bb=100,
           to_act=True, equity=0.50, **RB)
check("비교: 세컨드 페어 vs 큰 벳 → 폴드", MED, "폴드")
check("프로필: AF 3.5 공격적 상대 → 블러프캐치 콜", dict(MED, villain_profile=prof(vpip=30, pfr=20, af=3.5)), "콜", 12,
      detail_has="AF 3.5")
PASSIVE = dict(FACE, equity=0.48, villain_raised=True)
check("비교: 에쿼티 48% 레이즈 (프로필 없음) → 콜", dict(PASSIVE, villain_raised=False), "콜", 7)
check("프로필: AF 0.8 패시브 상대의 레이즈 = 강함 → 폴드", dict(PASSIVE, villain_profile=prof(vpip=30, pfr=10, af=0.8)), "폴드",
      detail_has="AF 0.8")
# rangeq 연동: 실제로 범위 에쿼티를 계산해서 넣는다
_b3 = [ev.parse_card(x) for x in ["Kh", "9s", "3d"]]
_vr = rangeq.narrow(rangeq.villain_range("open", "BTN", 6, 100), _b3, "bet")
_eq = rangeq.equity_vs_ranges([ev.parse_card("Kd"), ev.parse_card("6c")], _b3, [_vr], 3000, random.Random(5))["equity"]
_r = recommend(dict(TPWK, equity=_eq, equity_basis="range", villain_range=_vr))
results.append((_r["action"] in ("콜", "폴드") and any("상대 범위" in d for d in _r["detail"]) and any("범위" in d and "%" in d
                for d in _r["detail"]), f"rangeq 연동: 실제 범위 에쿼티 {_eq:.2f} → {_r['action']}", _r))

# --- 프리플랍: 차트 vs 수학 (범위 기준 에쿼티가 있을 때) -----------------------------------
PFM = dict(PF, equity_basis="range", can_check=False)
BB_MIN = dict(PFM, players=6, hero_pos="BB", situation="vsopen", villain_pos="BTN", stack_bb=100, call=1, pot_total=3.5)
# [변경] Q6o 에쿼티 0.40 -> 0.44: 블라인드 에쿼티 실현율을 강도 순위에 따라 낮췄다 (R = 0.92 - 0.0043 x 강도, Q6o(82위) 0.57).
#        예전엔 R=0.83 이라 40% 로도 콜이었지만 이제 실현 승률 0.40x0.57=23% < 필요 22%+여유 2% -> 폴드. Q6o 의 실제 범위 에쿼티는 ~44%.
check("수학: BB Q6o vs 2BB 오픈 — 차트 폴드지만 팟 오즈 22% < 범위 승률 → 콜", dict(BB_MIN, hero=["Qh", "6d"], equity=0.44), "콜", 1,
      0.01, detail_has="차트: 폴드 → 팟 오즈로 콜")
# [변경] 예전: 에쿼티가 랜덤 기준(equity_basis 없음)이면 수학을 건너뛰고 차트대로 폴드.
#        이제: 전달된 랜덤 기준 에쿼티(0.60)는 무시하고 BB/SB 방어는 오프너 레인지 대비 에쿼티를 직접 계산(rangeq, 2000회)해 팟 오즈로 검증 -> 콜.
check("수학: K8o 에쿼티가 랜덤 기준이어도 BB 방어는 범위 에쿼티를 직접 계산해 팟 오즈로 콜", dict(BB_MIN, hero=["Kh", "8d"], equity=0.60,
                                                          equity_basis=None), "콜", 1, 0.01, detail_has="팟 오즈로 콜")
check("수학: BB Q6o 범위 승률이 낮으면(27%) 폴드 유지", dict(BB_MIN, hero=["Qh", "6d"], equity=0.27), "폴드")
BTN_CO = dict(PFM, players=6, hero_pos="BTN", situation="vsopen", villain_pos="CO", stack_bb=100)
check("수학: 차트는 콜이지만 12BB 오픈(거대 사이즈) → 배당 부족 폴드", dict(BTN_CO, hero=["Kh", "Jd"], call=12, pot_total=13.5,
                                                          equity=0.40), "폴드", detail_has="차트: 콜 → 배당 부족")
check("수학: KJo 평범한 2.5BB 오픈 — 차트와 일치", dict(BTN_CO, hero=["Kh", "Jd"], call=2.5, pot_total=4.0, equity=0.46), "콜", 2.5,
      0.01, detail_has="일치")
JAM = dict(PFM, players=6, hero_pos="BB", situation="jamcall", villain_pos="BTN", facing_allin=True)
check("수학: 올인은 승률 vs 팟 오즈로만 — A9o 범위 승률 46% > 43% → 콜", dict(JAM, hero=["Ad", "9s"], stack_bb=9, my_stack=9, call=8,
                                                              pot_total=10.5, equity=0.46), "콜", 8, detail_has="올인도 수학")
check("수학: 올인 — 차트는 잼콜이어도 범위 승률 38% < 44% → 폴드", dict(JAM, hero=["Kd", "Qs"], stack_bb=10, my_stack=10, call=9,
                                                            pot_total=11.5, equity=0.38), "폴드", reason_has="올인")
check("수학: 4벳 올인에 JJ — 차트 폴드지만 범위 승률 45% > 39% → 콜", dict(PFM, hero=["Jh", "Jd"], players=6, hero_pos="CO",
                                                              situation="vs4bet", stack_bb=40, my_stack=40, call=40,
                                                              pot_total=62, equity=0.45, facing_allin=True), "콜", 40)
HJ_UTG = dict(PFM, players=9, hero_pos="HJ", situation="vsopen", villain_pos="UTG", call=2.5, pot_total=4.0)
check("셋마이닝: 22 HJ vs UTG 100BB — 배당 40배 ✓ → 콜", dict(HJ_UTG, hero=["2h", "2d"], stack_bb=100, equity=0.36), "콜", 2.5, 0.01,
      detail_has="셋마이닝 배당 40배 ✓")
check("셋마이닝: 같은 22 유효 25BB (10배) → 내재 배당 부족 폴드", dict(HJ_UTG, hero=["2h", "2d"], stack_bb=25, equity=0.36), "폴드")
BTN_UTG = dict(PFM, players=6, hero_pos="BTN", situation="vsopen", villain_pos="UTG", call=2.5, pot_total=4.0)
check("내재 배당: 65s BTN vs UTG 100BB — 차트 폴드지만 내재 배당 반영 → 콜", dict(BTN_UTG, hero=["6h", "5h"], stack_bb=100,
                                                                 equity=0.33), "콜", 2.5, 0.01, detail_has="내재 배당 40배")
check("내재 배당: 65s 유효 20BB — 내재 배당 부족 폴드", dict(BTN_UTG, hero=["6h", "5h"], stack_bb=20, equity=0.33), "폴드")
check("콜 후 SPR 표기: KJo 콜 detail 에 SPR", dict(BTN_CO, hero=["Kh", "Jd"], call=2.5, pot_total=4.0, equity=0.46), "콜", 2.5, 0.01,
      detail_has="콜 후 SPR")
check("다자 가격: 65s BTN vs UTG 오픈 + 콜러 2명 (팟 9BB) — 투기 콜 + 수학 표기",
      dict(BTN_UTG, hero=["6h", "5h"], stack_bb=100, equity=0.27, pot_total=9.0), "콜", 2.5, 0.01, detail_has="콜러 2명")

# --- 앤티 / 방어 / 스택 깊이 / 스타일(느슨함) / 멀티웨이 보정 (재보정) -----------------------------------
import advisor
import preflop as pf

advisor.DEFENCE_ITER = 1000   # 169핸드 x 여러 상황을 도는 아래 테스트는 반복 수를 줄여 시간 절약 (허용 오차 안에서 검증)
NA = dict(action="-", size_text="", confidence="-", reason="", detail=[])


def hc(h):
    """핸드 코드 → 카드 2장 (오프수트는 서로 다른 무늬)."""
    if len(h) == 2:
        return [h[0] + "h", h[1] + "d"]
    return [h[0] + "h", h[1] + ("h" if h[2] == "s" else "d")]


def bbctx(hand, vs="BTN", mult=2.0, stack=40, hero="BB", **kw):
    """9max 앤티(합계 1BB) 방어 상황: 오픈 mult×."""
    posted = 1.0 if hero == "BB" else 0.5
    return dict(PF, hero=hc(hand), players=9, hero_pos=hero, situation="vsopen", villain_pos=vs, stack_bb=stack,
                my_stack=stack, call=mult - posted, pot_total=1.5 + 1.0 + mult, ante=1.0, can_check=False, **kw)


def defend_pct(vs="BTN", mult=2.0, stack=40, hero="BB", **kw):
    """advisor 가 169핸드 각각에 대해 낸 (총 방어 %, 3벳/잼 %) — 콤보 가중."""
    tot = d = a = 0
    for h in pf.DATA["G"]:
        r = recommend(bbctx(h, vs, mult, stack, hero, **kw))
        n = pf.combos(h)
        tot += n
        d += n * (r["action"] != "폴드")
        a += n * (r["action"] in ("레이즈", "올인"))
    return d / tot * 100, a / tot * 100


check("앤티 BB K7o vs BTN 민레이즈 → 방어(콜)", bbctx("K7o"), "콜", 1, 0.01)
check("앤티 BB J8s vs BTN 민레이즈 → 방어(콜)", bbctx("J8s"), "콜", 1, 0.01)
check("앤티 BB 72o vs BTN 민레이즈 → 폴드", bbctx("72o"), "폴드")
check("앤티 SB A5s vs BTN → 3벳", bbctx("A5s", hero="SB"), "레이즈")
check("앤티 SB 72o vs BTN → 폴드", bbctx("72o", hero="SB"), "폴드")
_d, _a = defend_pct()
results.append((50 <= _d <= 68 and 11 <= _a <= 18, f"BB vs BTN 2x 앤티 40BB 방어 {_d:.1f}% (3벳 {_a:.1f}%) — 목표 50~68% / 3벳 11~18%", NA))
_dc, _ = defend_pct("CO", 2.5)   # CO/UTG 목표(~50 / ~35)는 2~2.5x 오픈 기준
_du, _ = defend_pct("UTG", 2.5)
results.append((42 <= _dc <= 58 and 28 <= _du <= 44 and _du < _dc < _d,
                f"BB 방어 vs CO {_dc:.1f}% / vs UTG {_du:.1f}% (목표 ~50 / ~35, 앞 포지션일수록 좁게)", NA))
# 차트(앤티) 수준: SB vs BTN 은 3벳 위주, BTN vs CO 콜+3벳 ~23%
_sb = pf.ranges("vsopen", "SB", "BTN", 9, 40, True)
_bc = pf.ranges("vsopen", "BTN", "CO", 9, 40, True)
results.append((18 <= pf.range_pct(_sb[0][1]) <= 26 and pf.range_pct(_sb[1][1]) < pf.range_pct(_sb[0][1]) / 2,
                f"SB vs BTN 앤티: 3벳 {pf.range_pct(_sb[0][1]):.1f}% (목표 ~20%) 콜 {pf.range_pct(_sb[1][1]):.1f}% (3벳 위주)", NA))
_t = pf.range_pct(_bc[0][1] | _bc[1][1])
results.append((20 <= _t <= 26, f"BTN vs CO 앤티: 3벳+콜 {_t:.1f}% (목표 20~25%)", NA))
# 오픈: 앤티가 있으면 LJ/HJ/CO/SB 가 넓어지고 UTG 는 거의 그대로
_op = {q: (pf.range_pct(pf.ranges("open", q, None, 9, 40, False)[0][1]), pf.range_pct(pf.ranges("open", q, None, 9, 40, True)[0][1]))
       for q in ("UTG", "LJ", "HJ", "CO", "BTN", "SB")}
results.append((all(_op[q][1] > _op[q][0] for q in ("LJ", "HJ", "CO", "SB")) and _op["UTG"][1] <= _op["UTG"][0] * 1.12 and
                _op["HJ"][1] >= _op["HJ"][0] * 1.08 and _op["BTN"][1] <= 58,
                "앤티 오픈 폭 (없음→있음): " + ", ".join(f"{q} {a:.1f}→{b:.1f}" for q, (a, b) in _op.items()), NA))

# 멀티웨이: 플랍 하프팟 벳 vs 상대 3명, 플러시 드로우 + 오버카드 → 콜
check("다자 플랍: 상대 3명 하프팟 벳 vs 플러시 드로우 + 오버카드 (AQ 하트, 9h5h2c) → 콜",
      mw(["Ah", "Qh"], ["9h", "5h", "2c"], 3, pot=10, pot_total=15, call=5, stack_bb=100), "콜", 5, detail_has="상대 3명")
check("다자 플랍: 상대 3명 하프팟 벳 vs 넛 플러시 드로우 + 오버카드 (AK 스페이드) → 콜",
      mw(["As", "Ks"], ["9s", "5s", "2c"], 3, pot=10, pot_total=15, call=5, stack_bb=100), "콜", 5)

# 랜덤 핸드 플랍 하프팟 벳 폴드율 (고정 시드): 헤즈업 ~22% / 2명 ~45% / 3명 ~64%  (재보정 전: 14% / 58% / 75%)
_rng = random.Random(7)
_deck = list(range(52))
_deals = {n_: [] for n_ in (1, 2, 3)}
for n_ in _deals:
    for _ in range(250):
        cs = _rng.sample(_deck, 5)
        _deals[n_].append((cs[:2], cs[2:], ev.equity(cs[:2], cs[2:], opponents=n_, iterations=200, rng=_rng)["equity"]))


def fold_rate(n_, **kw):
    f_ = sum(recommend(dict(hero=h_, board=b_, opponents_in_hand=n_, pot=10, pot_total=15, call=5, stack_bb=100, to_act=True,
                            equity=e_, **kw))["action"] == "폴드" for h_, b_, e_ in _deals[n_])
    return f_ / len(_deals[n_]) * 100


_fr = [fold_rate(n_) for n_ in (1, 2, 3)]
results.append((16 <= _fr[0] <= 32 and 36 <= _fr[1] <= 53 and 54 <= _fr[2] <= 69,
                f"랜덤 핸드 플랍 하프팟 벳 폴드율 헤즈업 {_fr[0]:.0f}% / 2명 {_fr[1]:.0f}% / 3명 {_fr[2]:.0f}% (목표 ~22 / ~45 / ~64)", NA))

# --- 느슨함(looseness) / 스타일 ---------------------------------------------------------------------
LS = (-1.0, -0.5, 0.0, 0.5, 1.0)
_dd = [defend_pct(looseness=L_)[0] for L_ in LS]
results.append((all(b_ >= a_ - 0.3 for a_, b_ in zip(_dd, _dd[1:])) and _dd[-1] > _dd[0] + 10,
                "느슨함 단조: BB vs BTN 방어% " + " ≤ ".join(f"{x:.0f}" for x in _dd) + " (L=-1..+1)", NA))
_fo = [fold_rate(2, looseness=L_) for L_ in LS]
_f1 = [fold_rate(1, looseness=L_) for L_ in LS]
results.append((all(b_ <= a_ + 0.5 for a_, b_ in zip(_fo, _fo[1:])) and all(b_ <= a_ + 0.5 for a_, b_ in zip(_f1, _f1[1:]))
                and _fo[0] > _fo[-1] + 10,
                "느슨함 단조: 플랍 하프팟 벳 폴드율(콜률의 반대) 2명 " + " ≥ ".join(f"{x:.0f}" for x in _fo) + " / 헤즈업 " + " ≥ ".join(f"{x:.0f}" for x in _f1), NA))
_ow = [[pf.range_pct(pf.ranges("open", q, None, 9, 40, True, L_)[0][1]) for q in ("UTG", "HJ", "CO", "BTN")] for L_ in LS]
results.append((all(all(b_ >= a_ - 1e-9 for a_, b_ in zip(r0, r1)) for r0, r1 in zip(_ow, _ow[1:])) and _ow[-1][1] > _ow[0][1] + 5,
                "느슨함 단조: 오픈 폭 HJ " + " ≤ ".join(f"{r_[1]:.1f}" for r_ in _ow), NA))
_tbw = [pf.range_pct(pf.ranges("vsopen", "BB", "BTN", 9, 40, True, L_)[0][1]) for L_ in LS]
results.append((all(b_ >= a_ - 1e-9 for a_, b_ in zip(_tbw, _tbw[1:])) and _tbw[-1] > _tbw[0] + 6,
                "느슨함 단조: BB 3벳 폭 " + " ≤ ".join(f"{x:.1f}" for x in _tbw), NA))
# 이름 별칭 = 숫자
_x = bbctx("K4o")
results.append((recommend(dict(_x, style="aggressive")) == recommend(dict(_x, looseness=0.6)) and
                recommend(dict(_x, style="tight")) == recommend(dict(_x, looseness=-0.6)) and
                recommend(dict(_x, style="standard")) == recommend(dict(_x, looseness=0)) == recommend(_x),
                "style 별칭: tight=-0.6 / standard=0(기본) / aggressive=+0.6", recommend(_x)))
# 표시: 0 이 아니면 detail 에 스타일
_r0, _r1 = recommend(bbctx("K7o")), recommend(bbctx("K7o", looseness=0.4))
results.append((not any("스타일" in d_ for d_ in _r0["detail"]) and any("스타일" in d_ and "+0.4" in d_ for d_ in _r1["detail"]),
                "느슨함 0 이 아니면 detail 에 '스타일' 표기 (0 이면 없음)", _r1))
# 방향: 프리플랍
check("스타일 tight: 앤티 BB 94s vs BTN → 폴드", bbctx("94s", style="tight"), "폴드", detail_has="스타일: 타이트")
check("스타일 standard: 앤티 BB 94s vs BTN → 콜", bbctx("94s"), "콜", 1, 0.01)
check("스타일 aggressive: 앤티 BB A9o vs BTN → 3벳 (표준은 콜)", bbctx("A9o", style="aggressive"), "레이즈", detail_has="스타일: 공격적")
check("표준: 앤티 BB A9o vs BTN → 콜", bbctx("A9o"), "콜", 1, 0.01)
# 방향: 포스트플랍 — 아웃츠 있는 에어 헤즈업 (공격적만 세미블러프), 콜 임계
GUT = dict(hero=["Ad", "Jc"], board=["Kh", "Ts", "2d"], opponents_in_hand=1, pot=8, stack_bb=100, call=0, can_check=True, to_act=True,
           equity=0.35)
check("포스트 표준: 헤즈업 오버카드+거트샷 (7아웃츠) → 체크", GUT, "체크")
check("포스트 aggressive: 같은 핸드 헤즈업 → 세미블러프 벳 (강한 드로우 기준 아웃츠 8→6)", dict(GUT, style="aggressive"), "벳", 4, 0.3, reason_has="세미블러프")
BORDER = dict(hero=["Kd", "6c"], board=["Kh", "9s", "3d"], opponents_in_hand=1, pot=10, pot_total=20, call=10, stack_bb=100, to_act=True)
_bd = {e_: [recommend(dict(BORDER, equity=e_, looseness=L_))["action"] for L_ in LS] for e_ in (0.74, 0.78, 0.82, 0.86)}
_ok = all(all(not (a_ == "콜" and b_ == "폴드") for a_, b_ in zip(v_, v_[1:])) for v_ in _bd.values())  # 느슨해질수록 콜이 폴드로 바뀌면 안 됨
_ok = _ok and any(v_[0] == "폴드" and v_[-1] == "콜" for v_ in _bd.values())
results.append((_ok, "포스트 느슨함 단조: 경계 에쿼티에서 -1 은 폴드 → +1 은 콜 (역전 없음) " + str(_bd), NA))

# --- 스택 깊이 (연속) -------------------------------------------------------------------------------
_ds = {s_: defend_pct(stack=s_)[0] for s_ in (20, 30, 40, 60)}
results.append((_ds[20] < _ds[30] < _ds[40] < _ds[60] and 40 <= _ds[20] <= 56 and 50 <= _ds[30] <= 62 and 52 <= _ds[40] <= 68 and _ds[60] <= 72,
                "BB 방어% vs BTN 2x: " + " < ".join(f"{s_}BB {_ds[s_]:.1f}" for s_ in (20, 30, 40, 60)) + " (얕을수록 좁게)", NA))
_cont = [pf.range_pct(pf.ranges("vsopen", "BB", "BTN", 9, s_, True)[1][1]) for s_ in range(16, 61, 2)]
_jump = max(abs(b_ - a_) for a_, b_ in zip(_cont, _cont[1:]))
results.append((_jump < 6.5 and all(b_ >= a_ - 0.3 for a_, b_ in zip(_cont, _cont[1:])),
                f"BB 콜 폭은 스택에 연속 (2BB 간격 최대 변화 {_jump:.1f}%p, 역전 없음)", NA))
CO_OPEN = dict(PF, players=9, hero_pos="BTN", situation="vsopen", villain_pos="CO", ante=1.0, call=2.2, pot_total=1.5 + 1.0 + 2.2,
               can_check=False)
for hand_ in ("22", "65s"):
    check(f"깊은 스택 60BB: {hand_} BTN vs CO 오픈 → 콜 (투기 핸드 내재 배당)", dict(CO_OPEN, hero=hc(hand_), stack_bb=60, my_stack=60), "콜", 2.2, 0.01)
    check(f"얕은 스택 18BB: {hand_} BTN vs CO 오픈 → 폴드 (내재 배당 없음, 리슛 아니면 폴드)", dict(CO_OPEN, hero=hc(hand_), stack_bb=18, my_stack=18), "폴드")
check("18BB 에서도 강한 핸드는 계속: AQs BTN vs CO → 3벳 잼", dict(CO_OPEN, hero=hc("AQs"), stack_bb=18, my_stack=18), "올인", 18)
check("60BB 같은 AQs → 3벳 레이즈 (잼 아님)", dict(CO_OPEN, hero=hc("AQs"), stack_bb=60, my_stack=60), "레이즈", 6.6, 0.3)
CO12 = dict(PF, players=9, hero_pos="CO", situation="open", stack_bb=12, my_stack=12, call=1, pot_total=2.5, ante=1.0, can_check=False)
check("12BB CO A9o 첫 진입 → 잼 (푸시/폴드)", dict(CO12, hero=hc("A9o")), "올인", 12)
check("12BB CO 72o 첫 진입 → 폴드", dict(CO12, hero=hc("72o")), "폴드")
_o = [pf.range_pct(pf.ranges("open", "CO", None, 9, s_, True)[0][1]) for s_ in (15, 20, 25, 30, 40, 100)]
results.append((_o[0] < _o[1] < _o[2] < _o[3] <= _o[4] and abs(_o[4] - _o[5]) < 0.01 and _o[0] > 0.85 * _o[4],
                "CO 오픈 폭 15/20/25/30/40/100BB: " + " / ".join(f"{x:.1f}" for x in _o) + " (15~30BB 에서 점점 좁게, 40BB 이상 동일)", NA))
# 포스트플랍 커밋은 현재 스택(SPR) 을 쓴다
TPTK = dict(hero=["Ac", "Qs"], board=["Qd", "7h", "2c"], opponents_in_hand=1, pot=14, call=0, can_check=True, to_act=True, equity=0.88)
check("SPR: 스택 20BB (SPR 1.4) 탑페어 굿키커 → 올인", dict(TPTK, stack_bb=20), "올인", 20)
check("SPR: 스택 120BB (SPR 8.6) 같은 핸드 → 일반 밸류 벳", dict(TPTK, stack_bb=120), "벳", 9.3, 0.3)

# --- plan: 대응 계획 (내 차례 아님 / 콜 금액 모름 / 후속 대응) ---------------------------------
def plan_of(name, ctx, has=(), absent=(), action=None, color=None, max_lines=3, lines=None):
    r = recommend(ctx)
    txt = " | ".join(r["plan"])
    ok = 1 <= len(r["plan"]) <= max_lines and all(h in txt for h in has) and not any(a in txt for a in absent)
    if action is not None:
        ok = ok and r["action"] == action
    if color is not None:
        ok = ok and r["color_key"] == color
    if lines is not None:
        ok = ok and len(r["plan"]) == lines
    results.append((ok, name, dict(r, reason=r["reason"] + " ‖ " + txt)))
    return r


FLP = dict(board=["Kh", "9s", "3d"], opponents_in_hand=1, pot=6, stack_bb=100, to_act=False)
# 솔버(HU_GTO): 범위 에쿼티 36% 탑페어 약키커는 체크로 와도 체크 (밸류 벳 기준 80%), 벳이 오면 최대 콜 표시
r = plan_of("plan: 대기 중 체크로 오면 체크 + 벳이 오면 최대 콜 (범위 36%)", dict(FLP, hero=["Kd", "6c"], equity=0.36, equity_basis="range"),
            has=("체크로 오면: 체크", "벳이 오면: 팟의", "까지 콜", "그 이상 폴드"), lines=2)
# 최대 콜 비율 수학: 헤즈업 범위 에쿼티 e, 플랍 pen 0 + 콜 여유 m → f/(1+2f) <= e - m → f ≈ e'/(1-2e'),  e' = 0.36 - m
_e = 0.36 * (1 - advisor.HU_GTO["range"]["pen"]["flop"]) - advisor.HU_GTO["range"]["call"]["flop"]
_f = _e / (1 - 2 * _e)
_pct_got = int([x for x in r["plan"] if "벳이 오면: 팟의" in x][0].split("팟의 ")[1].split("%")[0])
results.append((abs(_pct_got - _f * 100) <= 6, f"plan: 최대 콜 수학 e/(1-2e)={_f * 100:.0f}% ≈ 표시 {_pct_got}%", r))
lo = recommend(dict(FLP, hero=["Kd", "6c"], equity=0.33, equity_basis="range"))
hi = recommend(dict(FLP, hero=["Kd", "6c"], equity=0.38, equity_basis="range"))
pc = lambda rr: int([x for x in rr["plan"] if "벳이 오면: 팟의" in x][0].split("팟의 ")[1].split("%")[0])
results.append((pc(lo) < pc(r) < pc(hi), f"plan: 에쿼티가 높을수록 최대 콜이 커진다 ({pc(lo)}<{pc(r)}<{pc(hi)}%)", r))
rm = recommend(dict(FLP, hero=["Kd", "6c"], equity=0.36, equity_basis="range", opponents_in_hand=3))
results.append((pc(rm) < pc(r), f"plan: 다자 팟은 최대 콜이 줄어든다 ({pc(rm)}<{pc(r)}%)", rm))
rb = recommend(dict(FLP, hero=["Kd", "6c"], equity=0.36, equity_basis="range", pot=10))
results.append(("(≈" in " ".join(rb["plan"]) and "BB)" in " ".join(rb["plan"]), "plan: 최대 콜에 BB 환산 표기 (팟의 N%(≈xBB))", rb))
plan_of("plan: 강한 패 (셋) → 벳이 오면 레이즈 3배", dict(FLP, hero=["9h", "9d"], equity=0.92),
        has=("체크로 오면: 벳", "벳이 오면: 레이즈 3배"))
plan_of("plan: 에어 → 벳이 오면 폴드 (체크로 무료 카드)", dict(FLP, hero=["7c", "2d"], equity=0.10),
        has=("체크로 오면: 체크", "폴드", "무료 카드"))
plan_of("plan: 드로우는 내재 배당을 넣은 한도 표기", dict(FLP, board=["Kh", "8h", "2c"], hero=["Qh", "5h"], opponents_in_hand=3,
                                              equity=0.35), has=("까지 콜", "드로우 9장"))
plan_of("plan: 벳 금액 모름 (앞에 벳 있음) → 대기 + 콜 아님", dict(FLP, board=["Kh", "8h", "2c"], hero=["Ah", "5h"], equity=0.48,
                                                    to_act=True, can_check=False), has=("벳이 오면",), action="대기",
        color="check")
r = plan_of("plan: 약한 패 + 벳 금액 모름 → 대기 (콜/폴드 단정 안 함)", dict(FLP, hero=["7c", "2d"], equity=0.10, to_act=True,
                                                          can_check=False), has=("폴드",), action="대기")
results.append(("콜 금액 미확인" not in r["reason"] or "대기" == r["action"], "plan: 대기 사유에 금액 미확인 설명", r))
plan_of("plan: 내 차례 + 체크 가능 + 체크 → 체크 후 벳이 오면", dict(FLP, hero=["Qc", "Jc"], board=["As", "Qh", "7d", "3c", "2s"],
                                                         equity=0.5, to_act=True, can_check=True, call=0), has=("체크 후 벳이 오면",))
plan_of("plan: 내 차례 벳 후속 — 원페어는 레이즈 당하면 폴드 고려", dict(FLP, hero=["Ad", "Qc"], board=["Qh", "7s", "2d"], equity=0.88,
                                                          to_act=True, can_check=True, call=0), has=("레이즈 당하면",))
plan_of("plan: 내 차례 콜 후속 — 약한 콜은 레이즈 당하면 폴드", dict(FLP, hero=["Kd", "6c"], equity=0.5, to_act=True, call=3, pot_total=9),
        has=("레이즈 당하면: 폴드",), action="콜")
PRP = dict(board=[], players=9, stack_bb=100, opponents_in_hand=3, call=1, pot_total=1.5, to_act=False, situation="open",
           hero_pos="CO")
plan_of("plan 프리: CO AQo 내 차례 아님 → 레이즈가 오면 N BB까지 콜", dict(PRP, hero=["Ah", "Qd"]), has=("레이즈가 오면", "BB까지 콜"))
plan_of("plan 프리: CO KK → 레이즈가 오면 3벳 + 4벳엔 올인", dict(PRP, hero=["Kh", "Kd"]), has=("3벳 ≈ 7.5BB", "4벳이 오면: 올인"))
plan_of("plan 프리: CO 72o → 레이즈가 오면 폴드", dict(PRP, hero=["7h", "2d"]), has=("레이즈가 오면: 폴드",))
plan_of("plan 프리: 내 차례 오픈 후 3벳 대응", dict(PRP, hero=["Ah", "Jh"], to_act=True), has=("3벳 당하면",), action="레이즈")
plan_of("plan 프리: 내 차례 3벳 후 4벳 대응", dict(PRP, players=6, hero=["Ah", "Ks"], hero_pos="BTN", situation="vsopen", villain_pos="CO",
                                         call=2.5, pot_total=4, to_act=True), has=("4벳 당하면",), action="레이즈")
r = recommend(dict(PRP, hero=["Ah", "Qd"], hero_pos=None))
results.append((r["plan"] == [], "plan 프리: 포지션 모름 → plan 비어 있음 (추측 안 함)", r))
r = recommend({})
results.append((r["plan"] == [], "plan: 빈 ctx → plan []", r))

# --- 솔버 회귀 (GTO 데이터셋 검증 분할에서, 솔버 빈도 >= 0.8 · 튜닝 전 규칙은 틀렸던 노드) -----------------------------
# 출처: jevonmao/postflop-solver-reasoning-traces-1m (CC-BY-4.0), 헤즈업 BvB 200BB.  bench_gto.py 의 검증 분할(시드 20261029)에서 골랐고
# 에쿼티는 랜덤 핸드 기준(라이브 앱 기본), 위치(hero_ip)·주도권(aggressor) 포함.  기대 액션 = 솔버 정답 범주.
def gto(name, action, hero, board, pot, pot_total, call, stack, eq, ip, agg, raised=False, freq=None):
    ctx = dict(hero=hero, board=board, opponents_in_hand=1, pot=pot, pot_total=pot_total, call=call, can_check=call == 0,
               stack_bb=stack, my_stack=stack, to_act=True, hero_ip=ip, aggressor=agg, equity=eq)
    if raised:
        ctx["villain_raised"] = True
    check(f"솔버 회귀 (freq {freq}): {name}", ctx, action)


gto("플랍 IP 주도권 에어 → 레인지 c-벳", "벳", ["2h", "8h"], ["3c", "Tc", "Jd"], 5.0, 5.0, 0, 197.5, 0.223, True, True, freq=0.88)
gto("3벳팟 플랍 OOP 오버페어(A 보드) → 체크", "체크", ["Kh", "Ks"], ["5c", "Td", "Ah"], 20.0, 20.0, 0, 190.0, 0.808, False, True,
    freq=0.93)
gto("플랍 오버카드 vs 레이즈 → 폴드", "폴드", ["Jh", "Kd"], ["6c", "8d", "Qc"], 8.75, 20.0, 7.5, 193.75, 0.432, True, True, True,
    freq=1.0)
gto("플랍 탑투페어 vs 큰 레이즈 (SPR 1.6) → 콜 (올인 아님)", "콜", ["6h", "Kc"], ["6c", "Qd", "Kh"], 24.8, 64.4, 29.7, 182.65,
    0.928, False, False, True, freq=1.0)
gto("리버 IP 에어 → 폴라라이즈 블러프", "벳", ["9d", "Js"], ["4c", "Tc", "Qc", "Qh", "Ac"], 37.26, 37.26, 0, 181.37, 0.193,
    True, True, freq=0.99)
gto("리버 세컨드 페어 vs 레이즈 → 폴드", "폴드", ["7s", "Qd"], ["Jc", "Kc", "Ad", "7d", "5c"], 21.88, 50.02, 18.76, 184.37,
    0.555, False, False, True, freq=0.87)
gto("리버 OOP 플러시 먼저 행동 → 체크", "체크", ["Td", "Qc"], ["8c", "Tc", "Ac", "Ks", "5c"], 12.5, 12.5, 0, 193.75, 0.956,
    False, False, freq=0.92)
gto("턴 투페어 vs 레이즈 (SPR 1.9) → 콜", "콜", ["4c", "6c"], ["5c", "6d", "Ad", "4d"], 33.2, 58.1, 24.9, 183.4, 0.712,
    True, True, True, freq=0.91)
gto("턴 OOP 트립스(플러시 보드) 먼저 행동 → 체크", "체크", ["3d", "As"], ["Tc", "Ac", "Ad", "Qc"], 14.9, 14.9, 0, 192.55, 0.832,
    False, True, freq=0.96)
gto("리버 트립스 vs 레이즈 → 콜 (레이즈 아님)", "콜", ["9d", "Kh"], ["5c", "Kc", "Ad", "7h", "Ks"], 8.75, 20.0, 7.5, 193.75,
    0.968, True, None, True, freq=1.0)
gto("턴 보드 투페어 + 약한 드로우 vs 레이즈 → 폴드", "폴드", ["5d", "Jd"], ["Tc", "Kc", "Kd", "Th"], 26.08, 59.62, 22.36,
    181.37, 0.476, False, True, True, freq=0.98)
gto("4벳팟 리버 낮은 SPR(1.1) 탑페어 IP 비주도 → 체크", "체크", ["Qd", "Ad"], ["4c", "8d", "9h", "Jc", "Qc"], 125.0, 125.0, 0,
    137.5, 0.689, True, False, freq=1.0)
gto("4벳팟 턴 낮은 SPR(0.2) 페어+양차 vs 레이즈 → 올인", "올인", ["5h", "6h"], ["3c", "6d", "8c", "4h"], 87.5, 200.0, 75.0,
    137.5, 0.678, True, None, True, freq=0.88)

# --- 감사 회귀 (fuzz_advisor.py 로 찾은 버그) -------------------------------------------------------------------
import odds
import preflop as pf

# 1) 랜덤 기준 헤즈업 보정 에쿼티가 eq × (1 - 0.5) 에 묶여, 오버벳·올인에 리버 너트(KQ 스트레이트, 99.5%)도 폴드했다
NUT = dict(hero=["Kh", "Qd"], board=["2c", "7d", "9h", "Ts", "Js"], players=6, opponents_in_hand=1, hero_pos="BB",
           villain_pos="BTN", to_act=True, can_check=False, equity=eqv(["Kh", "Qd"], ["2c", "7d", "9h", "Ts", "Js"], 1))
_r = check("감사: 리버 너트 vs 팟 2배 오버벳 → 폴드 아님",
           dict(NUT, stack_bb=300, my_stack=300, pot=40, pot_total=120, call=80), "올인")
check("감사: 리버 너트 vs 팟 2배 올인 → 올인 콜", dict(NUT, stack_bb=80, my_stack=80, pot=40, pot_total=120, call=80,
                                              facing_allin=True), "콜", 80)
results.append((advisor._adj_equity(advisor._Ctx(dict(NUT, pot=40, call=80)), 0.40, "air", "하이카드", False, 1.0)[0] == 0.20,
                "감사: 너트 바닥(NUT_K)은 낮은 에쿼티의 보정에 영향 없음 (0.40 → 0.20)", _r))
# 2) 콜 금액이 스택보다 크면(숏스택 올인 콜) 실제로 내는 스택 기준 팟 오즈 — 예전엔 44BB 벳 기준 17% 로 계산해 폴드
check("감사: 4.4BB 남은 숏스택 vs 44BB 올인, 팟 178BB → 올인 콜 4.4BB",
      dict(hero=["Ac", "5c"], board=["9h", "9d", "Qh", "2h"], opponents_in_hand=1, stack_bb=4.4, my_stack=4.4, pot=178.1,
           pot_total=222.6, call=44.5, to_act=True, can_check=False, equity=0.45, facing_allin=True), "콜", 4.4, 0.05,
      detail_has="필요 승률 2%")
check("감사: 콜 사이즈는 유효 스택을 넘지 않음 (my_stack 없음)",
      dict(hero=["5c", "7s"], board=["7c", "Js", "3d", "6d"], opponents_in_hand=3, stack_bb=11.58, pot=79.26, pot_total=138.71,
           call=59.45, to_act=True, can_check=False, equity=0.9, facing_allin=True), "콜", 11.6, 0.05)
# 3) 필요 승률 표기에 스타일 가감(call_adj)이 빠져 '필요 20%+21% > 보정 에쿼티 44%' 처럼 숫자와 부등호가 어긋났다
check("감사: 필요 승률 표기에 스타일 가감 포함",
      dict(hero=["Kd", "6d"], board=["3h", "4s", "9d"], opponents_in_hand=1, pot=296.17, call=97.74, to_act=True,
           can_check=False, equity=0.5417, looseness=-0.94, aggressor=True), "폴드", detail_has="+3%(스타일)")
# 4) 헤즈업 올인 최소 에쿼티(턴 85%)를 팟 1/4 올인(필요 17%)에도 그대로 적용해 70% 에쿼티로 폴드 + 이유가 수치와 모순
check("감사: 팟 1/4 짜리 작은 올인 → 에쿼티 70% 로 콜",
      dict(hero=["Ah", "Td"], board=["2c", "7d", "9h", "Js"], opponents_in_hand=1, stack_bb=10, my_stack=10, pot=40,
           pot_total=50, call=10, to_act=True, can_check=False, equity=0.70, facing_allin=True), "콜", 10)
check("감사: 큰 올인을 최소 에쿼티로 접을 땐 이유에 그 기준을 적음",
      dict(hero=["Ah", "Td"], board=["2c", "7d", "9h", "Js"], opponents_in_hand=1, stack_bb=40, my_stack=40, pot=80,
           pot_total=120, call=40, to_act=True, can_check=False, equity=0.80, facing_allin=True), "폴드", reason_has="이상만")
# 5) 프리플랍 표: 3벳 폭을 줄일 때(타이트 스타일·깊은 스택) 빠진 3벳 핸드가 콜로 가지 않고 폴드가 됐다 (BB/SB vs UTG 의 AKo)
_bad = []
for _h, _v, _st in (("BB", "UTG", 40), ("SB", "UTG", 100), ("BB", "UTG", 100), ("SB", "MP", 60)):
    for _L in (-1.0, -0.8, -0.6, -0.3, 0.0):
        for _n in (6, 9):
            _v2 = _v if _v in pf.seats(_n) else "UTG"
            _u = set().union(*(a[1] for a in pf.ranges("vsopen", _h, _v2, _n, _st, False, _L)))
            if not {"AA", "KK", "QQ", "JJ", "AKs", "AKo"} <= _u:
                _bad.append((_h, _v2, _n, _st, _L))
results.append((not _bad, f"감사: vs 오픈 표에서 JJ+/AK 는 어떤 스타일에서도 폴드 아님 {_bad[:3]}", _r))
# 6) odds: 억·만 섞인 금액, format_amount 정밀도 (입력칸에 되써질 때 12345 → '1.2만' = 12000)
_pa = {"1억 2000만": 1.2e8, "1억2000만 5000": 120005000, "3억 5000": 300005000, "84만 7500": 847500, "1.5BB": 1.5,
       "0BB": 0.0, "13만": 130000, "1억": 1e8, "abc": None, "": None}
_got = {t: odds.parse_amount(t) for t in _pa}
results.append((_got == _pa, f"감사: parse_amount 억·만 혼합 {_got}", _r))
results.append((odds.format_amount(12345) == "1.23만" and odds.format_amount(847500) == "84.75만"
                and odds.format_amount(130000) == "13만" and odds.format_amount(1.2e8) == "1.2억",
                "감사: format_amount 만 단위 소수 둘째 자리", _r))

# --- GTO 라이브러리 연동 (solverlib 를 가짜로 바꿔 주/보조 게이팅 확인; 풀린 플랍과 무관) -------------------------------
import solverlib


@contextlib.contextmanager
def gto_on(fn=None, lookup=None):
    """advisor 의 GTO 사용을 켜고 solverlib.gto_advice (또는 lookup) 를 fn 으로 바꾼다. 끝나면 원래대로."""
    saved = (advisor.GTO_ENABLED, solverlib.gto_advice, solverlib.lookup)
    advisor.GTO_ENABLED = True
    advisor.gto_cache_clear()
    if fn is not None:
        solverlib.gto_advice = fn
    if lookup is not None:
        solverlib.lookup = lookup
    try:
        yield
    finally:
        advisor.GTO_ENABLED, solverlib.gto_advice, solverlib.lookup = saved
        advisor.gto_cache_clear()


def fake_res(**over):
    res = dict(actions={"bet33": 0.62, "check": 0.38}, ev=None, mapped_flop="Ks7h2d", actual_flop="Ks7h2d", exact_flop=True,
               exact_spot=True, spot="BTN_BB", stack_used=40, node="oop_first", role="oop", range_actions={"check": 0.8},
               combo="9c3d", combo_exact=True, notes=[], distance=0.0, exploitability=0.7,
               amounts={"check": 0.0, "bet33": 2.0}, pot=6.0, behind=37.8, stale=False, street="flop")
    res.update(over)
    return res


def fake_gto(action="벳", conf="high", **over):
    """solverlib.gto_advice 와 같은 모양의 가짜 (호출 횟수는 fn.calls)."""
    def fn(ctx):
        fn.calls += 1
        return {"action": action, "size_bb": 2.0, "size_text": "팟의 33% ≈ 2BB", "reason": "GTO: 벳 33% 62% · 체크 38%",
                "detail": ["솔버 BTN vs BB SRP 40BB · 플랍 Ks7h2d", "레인지 전체: 체크 80%"], "confidence": conf,
                "color_key": "raise", "plan": [], "source": "gto", "gto": fake_res(**over)}
    fn.calls = 0
    return fn


# 에어(9c 3d) — 규칙 기반은 체크.  BB(OOP) 가 BTN 을 상대로 먼저 행동, 스택 37BB
HU = dict(hero=["9c", "3d"], board=["Ks", "7h", "2d"], street="flop", players=9, hero_pos="BB", villain_pos="BTN",
          opponents_in_hand=1, stack_bb=37, my_stack=37, pot=5.8, pot_total=5.8, call=0, can_check=True, to_act=True,
          equity=0.30, hero_ip=False, aggressor=False, ante=True)
rule = recommend(HU)   # GTO 꺼짐 (모듈 기본값은 이 파일 위쪽에서 끔) = 규칙 기반 기준값


def mode_of(ctx, fn):
    with gto_on(fn):
        return recommend(ctx)


def is_primary(r):
    return r["reason"].startswith("GTO:") and any("규칙 기반 판단" in d for d in r["detail"])


def is_secondary(r):
    return (r["action"] == rule["action"] and r["reason"] == rule["reason"] and r["confidence"] == "low"
            and any("GTO 참고" in d for d in r["detail"]))


f = fake_gto()
r = mode_of(HU, f)
results.append((rule["action"] == "체크" and f.calls == 1, "GTO 기준값: 규칙 기반은 체크 (가짜 GTO 는 벳)", rule))
results.append((r["action"] == "벳" and is_primary(r) and r["confidence"] == "high" and len(r["detail"]) <= 3
                and set(r) == KEYS and r["size_text"] == "팟의 33% ≈ 2BB" and r["plan"] == [],
                "GTO 주: 플랍 정확 일치 → GTO 액션(벳)이 추천, 규칙 판단은 detail", r))
results.append((any("솔버" in d for d in r["detail"]) and any(rule["reason"] in d for d in r["detail"]),
                "GTO 주: detail 에 솔버 스팟 + 규칙 기반 이유", r))
r = mode_of(HU, fake_gto(exact_flop=False, distance=1.5, mapped_flop="Ks6h2d"))
results.append((r["action"] == "벳" and is_primary(r), "GTO 주: 가까운 플랍(거리 1.5 <= 2) 근사도 주 추천", r))
r = mode_of(HU, fake_gto(exact_flop=False, distance=2.5, mapped_flop="Ks6h2d"))
results.append((is_secondary(r) and any("비슷한 플랍 K62r" in d for d in r["detail"]),
                "GTO 보조: 먼 근사(거리 2.5) → 규칙 기반 유지 + 'GTO 참고(비슷한 플랍 K62r)' + 신뢰도 low", r))
r = mode_of(HU, fake_gto(stack_used=20, behind=17.8))
results.append((is_secondary(r) and any("같은 플랍 K72r" in d for d in r["detail"]), "GTO 보조: 스택 차이 > 10BB (37 vs 20)", r))
r = mode_of(HU, fake_gto(stack_used=60, behind=57.8))
results.append((is_secondary(r), "GTO 보조: 스택 차이 > 10BB (37 vs 60)", r))
r = mode_of(HU, fake_gto(stack_used=30, behind=27.8))
results.append((is_primary(r), "GTO 주: 스택 37 vs 풀린 30 (7BB 차이) 은 허용", r))
r = mode_of(HU, fake_gto(exact_spot=False, notes=["UTG→HJ 오프너"]))
results.append((is_secondary(r), "GTO 보조: 오프너 군이 다름", r))
r = mode_of(HU, fake_gto(exact_spot=False, notes=["CO_BB→BTN_BB 스팟"]))
results.append((is_secondary(r), "GTO 보조: 다른 스팟 파일로 대신 찾음", r))
r = mode_of(HU, fake_gto(exact_spot=False, notes=["6명 → 9명 레인지"]))
results.append((is_primary(r), "GTO 주: 인원 수 차이(9명 레인지 사용)만 있으면 주 추천 가능", r))
r = mode_of(HU, fake_gto(combo_exact=False, notes=["콤보 범위 밖 → 9c4d"]))
results.append((is_secondary(r), "GTO 보조: 내 핸드가 풀린 범위 밖", r))
r = mode_of(HU, fake_gto(notes=["랭크 근사 충돌"]))
results.append((is_secondary(r), "GTO 보조: 랭크 근사 충돌", r))
r = mode_of(HU, fake_gto(stale=True))
results.append((is_secondary(r), "GTO 보조: 예전 레인지로 푼 플랍(stale)", r))
r = mode_of(HU, fake_gto(conf="low"))
results.append((is_secondary(r), "GTO 보조: solverlib 신뢰도 low", r))
r = mode_of(HU, fake_gto(action="체크"))
results.append((r["action"] == "체크" and is_primary(r) and r["plan"] == rule["plan"], "GTO 주: 규칙과 같은 액션이면 plan 유지", r))

# looseness: 주 GTO 답이 표준 — 강하게 설정돼도 액션은 GTO, detail 에 표기
for L in (0.6, -0.8):
    r = mode_of(dict(HU, looseness=L), fake_gto())
    results.append((r["action"] == "벳" and is_primary(r) and any("느슨함" in d and "GTO 기준 유지" in d for d in r["detail"])
                    and len(r["detail"]) <= 3, f"GTO 주 + 느슨함 {L:+.1f}: 액션은 GTO 그대로, detail 에 표기", r))
rl = mode_of(dict(HU, looseness=0.6), fake_gto(distance=3.0, exact_flop=False))
results.append((any("느슨함" in d for d in rl["detail"]) and any("GTO 참고" in d for d in rl["detail"]), "GTO 보조 + 느슨함: 둘 다 표기", rl))


# 실패/없음 → 규칙 기반과 똑같아야 한다
def boom(ctx):
    raise RuntimeError("solverlib broke")


for label, fn in (("예외", boom), ("None", lambda ctx: None), ("dict 아님", lambda ctx: "x"), ("action 없음", lambda ctx: {"gto": {}})):
    r = mode_of(HU, fn)
    results.append((r == rule, f"GTO 실패 폴백: solverlib.gto_advice {label} → 규칙 기반 그대로", r))
r = mode_of(HU, fake_gto(action="무슨액션"))
results.append((r == rule, "GTO 실패 폴백: 알 수 없는 액션 이름 → 규칙 기반 그대로", r))
r = mode_of(HU, lambda ctx: {"action": "벳", "gto": None, "detail": None, "reason": None})
results.append((set(r) == KEYS and r["action"] in ACTIONS, "GTO 실패 폴백: 필드가 깨진 결과도 recommend 를 깨지 않음", r))
saved_lib = sys.modules.get("solverlib")
sys.modules["solverlib"] = None   # import solverlib -> ImportError (라이브러리/모듈 없음)
try:
    advisor.GTO_ENABLED = True
    advisor.gto_cache_clear()
    r = recommend(HU)
finally:
    sys.modules["solverlib"] = saved_lib
    advisor.GTO_ENABLED = False
    advisor.gto_cache_clear()
results.append((r == rule, "GTO 폴백: solverlib 를 import 할 수 없어도 규칙 기반", r))

# GTO 를 쓰면 안 되는 상황: 가짜가 한 번도 불리지 않아야 한다
f = fake_gto()
with gto_on(f):
    skips = {
        "멀티웨이 (상대 2명)": recommend(dict(HU, opponents_in_hand=2)),
        "멀티웨이 (상대 3명)": recommend(dict(HU, opponents_in_hand=3)),
        "상대 수 모름": recommend({k: v for k, v in HU.items() if k != "opponents_in_hand"}),
        "내 차례 아님": recommend(dict(HU, to_act=False)),
        "리버": recommend(dict(HU, board=["Ks", "7h", "2d", "Qc", "3s"], street="river")),
        "프리플랍": recommend(dict(HU, board=[], street="preflop", situation="open", hero_pos="BTN", villain_pos=None, call=1)),
    }
results.append((f.calls == 0, "GTO 미사용: 멀티웨이·내 차례 아님·리버·프리플랍에선 solverlib 를 부르지 않는다",
                skips["멀티웨이 (상대 2명)"]))
for name, r in skips.items():
    results.append((not r["reason"].startswith("GTO") and not any("GTO" in d for d in r["detail"]), f"GTO 미사용: {name}", r))
mw = skips["멀티웨이 (상대 2명)"]
results.append((mw == recommend(dict(HU, opponents_in_hand=2)), "멀티웨이 결과는 GTO 켜짐/꺼짐이 같다", mw))
with gto_on(fake_gto()):
    advisor.GTO_ENABLED = False
    r = recommend(HU)
results.append((r == rule, "advisor.GTO_ENABLED=False 면 규칙 기반", r))

# 캐시: 같은 입력은 한 번만 조회, 입력이 바뀌면 다시
f = fake_gto()
with gto_on(f):
    a = recommend(HU)
    b = recommend(HU)
    n1 = f.calls
    recommend(dict(HU, pot=6.5, pot_total=6.5))
    n2 = f.calls
    recommend(dict(HU, hero=["Ts", "3d"]))
    n3 = f.calls
results.append((a == b and (n1, n2, n3) == (1, 2, 3), f"GTO 캐시: 같은 (보드, 핸드, 상황) 은 1회 조회 ({n1},{n2},{n3})", a))

# 턴: 플랍 라인을 알고 턴 카드 근사가 없어야 주 추천
TURN = dict(HU, board=["Ks", "7h", "2d", "Qc"], street="turn", pot=6.0, pot_total=6.0, stack_bb=36, my_stack=36)
tres = dict(street="turn", line="x_x", mapped_turn="Qc", actual_turn="Qc")
r = mode_of(dict(TURN, flop_line="x_x"), fake_gto(**tres))
results.append((is_primary(r), "GTO 턴 주: 플랍 라인 x_x 를 알고 턴 카드 정확", r))
r = mode_of(TURN, fake_gto(**dict(tres, notes=["플랍 라인 추정 x_x"])))
results.append((not r["reason"].startswith("GTO:") and any("GTO 참고" in d and "턴" in d for d in r["detail"]),
                "GTO 턴 보조: flop_line 모름 (추정 라인)", r))
r = mode_of(dict(TURN, flop_line="x_x"), fake_gto(**dict(tres, notes=["턴 카드 근사"])))
results.append((not r["reason"].startswith("GTO:") and any("GTO 참고" in d for d in r["detail"]), "GTO 턴 보조: 턴 카드를 근사함", r))
r = mode_of(dict(TURN, flop_line="bet33_c"), fake_gto(**tres))
results.append((not r["reason"].startswith("GTO:"), "GTO 턴 보조: ctx 라인과 조회 라인이 다름", r))


# solverlib.lookup 만 가짜로 두고 진짜 gto_advice 를 거친다 (노드 선택·빈도 합산·신뢰도 계산 포함)
def fake_lookup(*a, **kw):
    fake_lookup.calls.append((a, kw))
    return fake_res(actions={"bet33": 0.1, "bet75": 0.1, "check": 0.8}, range_actions={"check": 0.8, "bet33": 0.2},
                    amounts={"check": 0.0, "bet33": 1.9, "bet75": 4.4})


fake_lookup.calls = []
with gto_on(lookup=fake_lookup):
    r = recommend(HU)
args = fake_lookup.calls[0][0] if fake_lookup.calls else ()
results.append((len(args) >= 6 and args[:2] == ("BB", "BTN") and args[5] == "oop_first" and r["action"] == "체크" and is_primary(r)
                and "체크 80%" in r["reason"], "GTO 주: lookup 만 가짜 → gto_advice 가 OOP 먼저 행동 노드를 조회, 체크 80% 가 추천", r))
# 3벳팟 / SRP 아닌 팟 / 상대 위치 모름 → 진짜 gto_advice 가 lookup 을 부르기 전에 None → 규칙 기반
fake_lookup.calls.clear()
with gto_on(lookup=fake_lookup):
    gates = [recommend(dict(HU, situation="vs3bet")), recommend(dict(HU, pot=30.0, pot_total=30.0)),
             recommend(dict(HU, villain_pos=None))]
results.append((not fake_lookup.calls and all(g["action"] == rule["action"] and not g["reason"].startswith("GTO") for g in gates),
                "GTO 미사용: 3벳팟 · SRP 아닌 팟 · 상대 위치 모름 (실제 gto_advice 게이트)", gates[0]))

# 진짜 라이브러리 통합: gto_lib/BTN_BB_40bb.json.gz 에 풀려 있는 플랍 하나로 (어느 플랍이 풀렸는지에 의존하지 않는다)
LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gto_lib", "BTN_BB_40bb.json.gz")
solved = []
if os.path.exists(LIB):
    try:
        with gzip.open(LIB, "rt", encoding="utf-8") as fh:
            solved = list(json.load(fh).get("flops") or {})
    except (OSError, ValueError, EOFError):
        solved = []
if solved:
    fk = solved[0]
    board = [fk[0:2], fk[2:4], fk[4:6]]
    hero = next(h for h in (["Ac", "Kd"], ["9c", "8d"], ["Tc", "Td"], ["3c", "2d"]) if not set(h) & set(board))
    real = dict(HU, hero=hero, board=board)
    with gto_on():
        r = recommend(real)
        adv = solverlib.gto_advice(real)
    results.append((adv is not None and adv["gto"]["exact_flop"] and adv["gto"]["mapped_flop"] == fk,
                    f"GTO 실제 라이브러리: 풀린 플랍 {fk} 를 정확히 찾음", r))
    results.append((set(r) == KEYS and (is_primary(r) or any("GTO 참고(같은 플랍" in d for d in r["detail"])),
                    f"GTO 실제 라이브러리: {fk} 에서 주(primary) 또는 보조(같은 플랍 참고) 로 반영", r))
    with gto_on():
        recommend(real)
        t0 = time.perf_counter()
        for _ in range(20):
            recommend(real)
        warm = (time.perf_counter() - t0) / 20 * 1000
    results.append((warm < 30, f"GTO 실제 라이브러리: 캐시된 recommend {warm:.2f} ms (< 30)", r))

# --- 토너먼트 ICM (icm.py) ----------------------------------------------------------------
# 9인 MTT, 앤티 1.35BB (BB 100 / 앤티 15 × 9).  버블: 30명 남음 / 27명 지급, 평균 25BB
TPF = dict(PF, ante=1.35, players=9)
BUB = {"players_left": 30, "paid": 27, "avg_stack_bb": 25}
TABLE = {1: 40, 2: 22, 3: 18, 4: 30, 5: 15, 6: 25, 7: 35, 8: 20}
FT = {"players_left": 5, "paid": 27, "avg_stack_bb": 40}
EARLY = {"players_left": 900, "entrants": 1000, "avg_stack_bb": 30}


# 버블 숏스택: 칩EV 로는 +EV 인 잼 콜 (범위 승률 45% > 팟 오즈 43%) 이 ICM 으로는 -EV → 폴드
JAMCALL = dict(TPF, hero=["Ad", "9c"], hero_pos="BB", villain_pos="CO", situation="jamcall", stack_bb=12, my_stack=12,
               call=11, pot_total=14.85, can_check=False, facing_allin=True, equity=0.45, equity_basis="range")
check("ICM 없음: 12BB A9o 잼 콜 (범위 승률 45% > 팟 오즈 43%) → 콜", JAMCALL, "콜")
check("ICM 버블: 같은 잼 콜 → 폴드 (버블 팩터로 필요 승률 43%→55%)",
      dict(JAMCALL, tournament=BUB, table_stacks_bb=TABLE, villain_stack_bb=28), "폴드", detail_has="ICM: 버블 팩터")
check("ICM 버블: 덮이는 12BB 는 범위 승률 50% 도 폴드 (필요 55%)",
      dict(JAMCALL, equity=0.50, tournament=BUB, table_stacks_bb=TABLE, villain_stack_bb=28), "폴드")
check("ICM 버블: 내가 덮는 쪽(60BB vs 12BB 잼)이면 범위 승률 50% → 콜 (버블 팩터 ~1.1, 필요 45%)",
      dict(JAMCALL, equity=0.50, my_stack=60, tournament=BUB, table_stacks_bb=TABLE, villain_stack_bb=0), "콜",
      detail_has="필요 승률 43%→45%")
# 포스트플랍 올인 콜도 같은 원리
TURN_AI = dict(hero=["Ah", "Jd"], board=["Jc", "8s", "4d", "2h"], street="turn", opponents_in_hand=1, hero_pos="BB",
               villain_pos="BTN", stack_bb=22, my_stack=22, pot=12, pot_total=34, call=22, to_act=True, can_check=False,
               facing_allin=True, equity=0.55, equity_basis="range")
check("ICM 없음: 턴 탑페어 올인 콜 (보정 에쿼티 52% > 필요 37%) → 콜", TURN_AI, "콜")
check("ICM 버블: 덮인 숏스택의 같은 올인 콜 → 폴드", dict(TURN_AI, tournament=BUB, table_stacks_bb=TABLE, villain_stack_bb=40),
      "폴드", detail_has="ICM: 버블 팩터", reason_has="(ICM)")

# 칩 리더는 버블에서 미디엄 스택 상대로 더 넓게 잼 (상대가 콜하기 어렵다), 미디엄 스택은 칩 리더 상대로 더 좁게
JAM = dict(TPF, hero_pos="CO", situation="jam", stack_bb=12, call=1, pot_total=2.85, can_check=False)
check("ICM 없음: CO 12BB K5o 첫 진입 → 폴드 (잼 범위 밖)", dict(JAM, hero=["Kc", "5d"], my_stack=80), "폴드")
check("ICM 버블 칩 리더(80BB) vs 12BB: K5o → 잼 (상대 콜 버블 팩터가 커서 폭 ×1.2)",
      dict(JAM, hero=["Kc", "5d"], my_stack=80, tournament=BUB, table_stacks_bb=TABLE, villain_stack_bb=12), "올인",
      detail_has="잼 폭")
check("ICM 없음: CO 12BB K9o 첫 진입 → 잼", dict(JAM, hero=["Kc", "9d"], my_stack=12), "올인")
check("ICM 버블 12BB vs 칩 리더 80BB: K9o → 폴드 (덮이는 잼은 좁게)",
      dict(JAM, hero=["Kc", "9d"], my_stack=12, tournament=BUB, table_stacks_bb={**TABLE, 9: 80}, villain_stack_bb=80), "폴드")
check("ICM 버블 칩 리더: AA 는 여전히 잼", dict(JAM, hero=["Ac", "Ad"], my_stack=80, tournament=BUB, table_stacks_bb=TABLE,
                                           villain_stack_bb=12), "올인")

# 파이널 테이블 페이 점프: 더 짧은 스택(6BB)이 있을 때 미디엄 스택이 빅스택 올인에 콜 → 폴드
FT_AI = dict(TURN_AI, equity=0.62)
check("ICM 없음: 턴 탑페어 62% 올인 콜 → 콜", FT_AI, "콜")
r = check("ICM 파이널 테이블(5명): 6BB 숏스택이 있으니 같은 콜 → 폴드 (페이 점프)",
          dict(FT_AI, tournament=FT, table_stacks_bb={1: 90, 2: 60, 3: 6, 4: 40}, villain_stack_bb=40), "폴드",
          detail_has="파이널 테이블")

# 초반 (멀리서): 동작 동일 / 지급 인원 모름: 동작 동일
same = True
for base in (JAMCALL, TURN_AI, dict(JAM, hero=["Kc", "9d"], my_stack=12),
             dict(TPF, hero=["Kd", "9c"], hero_pos="HJ", situation="open", stack_bb=25, my_stack=25, call=1, pot_total=2.85),
             dict(hero=["Kh", "Jd"], board=["Jc", "8s", "4d"], street="flop", opponents_in_hand=1, hero_pos="BB",
                  villain_pos="BTN", stack_bb=25, my_stack=25, pot=5, pot_total=8, call=3, to_act=False, can_check=False,
                  equity=0.62)):
    a = recommend(base)
    for t in (EARLY, {"players_left": 32, "my_rank": "6/32"}, {"my_rank": "6/32"}, {"players_left": 400, "paid": 27}):
        same = same and recommend(dict(base, tournament=t, table_stacks_bb=TABLE, villain_stack_bb=30)) == a
results.append((same, "ICM 초반(900/1000, 400/27명 지급)·지급 인원 모름: 추천이 ICM 없을 때와 완전히 같음", a))

# 인 더 머니: 막 진입 vs 깊이 (파이널) — 같은 콜이 깊을수록 무겁다
ITM_AI = dict(TURN_AI, equity=0.66)
r1 = check("ICM 인 더 머니 막 진입(26/27명): 탑페어 66% 올인 콜 → 콜",
           dict(ITM_AI, tournament={"players_left": 26, "paid": 27, "avg_stack_bb": 26}, table_stacks_bb={1: 40, 2: 26, 3: 30},
                villain_stack_bb=40), "콜", detail_has="인 더 머니")
r2 = check("ICM 인 더 머니 깊이(파이널 5명): 같은 콜 → 폴드",
           dict(ITM_AI, tournament=FT, table_stacks_bb={1: 90, 2: 60, 3: 30, 4: 40}, villain_stack_bb=40), "폴드",
           detail_has="파이널 테이블")


def icm_need(res):
    for d in res["detail"]:
        m = re.search(r"필요 승률 (\d+)%→(\d+)%", d)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


n1, n2 = icm_need(r1), icm_need(r2)
results.append((bool(n1 and n2 and n1[0] == n2[0] and n2[1] > n1[1] > n1[0]),
                f"ICM 필요 승률: 칩 {n1 and n1[0]}% → 막 진입 {n1 and n1[1]}% < 파이널 {n2 and n2[1]}%", r2))

# 올인 아닌 결정: 오픈 폭이 버블에서 좁아지고 (빅스택은 유지), 콜 여유에 위험 프리미엄
OPEN = dict(TPF, hero=["4h", "4d"], hero_pos="HJ", situation="open", stack_bb=25, my_stack=25, call=1, pot_total=2.85,
            can_check=False)
check("ICM 없음: HJ 25BB 44 첫 진입 → 레이즈", OPEN, "레이즈")
check("ICM 버블 미디엄 스택: 44 → 폴드 (레인지 × 0.74)", dict(OPEN, tournament=BUB, table_stacks_bb=TABLE), "폴드",
      detail_has="레인지 ×")
check("ICM 버블 빅스택(70BB, 덮음): 44 → 레이즈 (압박: 오픈 폭 유지/조금 넓게)",
      dict(OPEN, my_stack=70, tournament=BUB, table_stacks_bb=TABLE), "레이즈", detail_has="빅스택 압박")
FLOP_BET = dict(hero=["Kh", "Jd"], board=["Jc", "8s", "4d"], street="flop", opponents_in_hand=1, hero_pos="BB",
                villain_pos="BTN", stack_bb=25, my_stack=25, pot=5, pot_total=8, call=3, to_act=True, can_check=False,
                equity=0.62)
check("ICM 버블: 플랍 탑페어 벳에 콜 — 필요 승률에 위험 프리미엄 가산 표기",
      dict(FLOP_BET, tournament=BUB, table_stacks_bb=TABLE, villain_stack_bb=40), "콜", detail_has="(ICM)")

# 너트는 ICM 이 아무리 커도 접지 않는다 (리버 너트 올인 콜, 파이널 테이블 덮인 스택)
NUTS = dict(hero=["Ah", "Kh"], board=["Qh", "Jh", "Th", "2c", "3d"], street="river", opponents_in_hand=1, hero_pos="BB",
            villain_pos="BTN", stack_bb=22, my_stack=22, pot=12, pot_total=34, call=22, to_act=True, can_check=False,
            facing_allin=True, equity=1.0)
check("ICM 파이널: 리버 로열 플러시 올인 콜 → 콜",
      dict(NUTS, tournament=FT, table_stacks_bb={1: 300, 2: 150, 3: 2, 4: 40}, villain_stack_bb=300), "콜")
NUT98 = dict(NUTS, hero=["Ah", "5h"], board=["Kh", "Qh", "7h", "2c", "3d"], equity=0.985)
check("ICM 파이널: 리버 너트 플러시(98.5%) 올인 콜 → 콜",
      dict(NUT98, tournament=FT, table_stacks_bb={1: 300, 2: 150, 3: 2, 4: 40}, villain_stack_bb=300), "콜")

# 속도: ICM 포함 recommend (캐시 안 된 새 스택) < 30ms
t0 = time.perf_counter()
for k in range(5):
    recommend(dict(JAMCALL, my_stack=12 + k * 0.7, tournament={"players_left": 33 + k, "paid": 27, "avg_stack_bb": 25 + k},
                   table_stacks_bb=TABLE, villain_stack_bb=28 + k))
ms = (time.perf_counter() - t0) / 5 * 1000
results.append((ms < 30, f"ICM 포함 recommend (새 스택, 캐시 없음) {ms:.1f} ms (< 30)", recommend(JAMCALL)))

# --- 2026-10-30 라이브 회귀: 체크 가능한데 폴드 / 3벳·4벳 팟 KK 폴드 ------------------------------------------
# p_014947: CO 아이솔레이션 레이즈 5인 팟, 플랍 A♥9♦4♣ 에 K♥K♣, 넷 다 체크 → 내 차례 (체크 버튼).
# 예전: 체크 버튼 글자를 '콜 41BB' 로 읽음 + 상대 범위 기준 에쿼티 16% → '폴드'.
LIVE_KK = dict(hero=["Kh", "Kc"], board=["Ah", "9d", "4c"], street="flop", players=6, opponents_in_hand=4,
               hero_pos="CO", situation="open", pre_raises=1, stack_bb=371, my_stack=481.2, pot=25, pot_total=25,
               to_act=True, equity=0.164, equity_basis="range", hand_name="원페어", aggressor=True)
check("라이브 p_014947: 체크 버튼 + 엉뚱한 콜 41BB 가 같이 와도 → 체크 (폴드 아님)",
      dict(LIVE_KK, call=41, can_check=True), "체크")
check("라이브 p_014947: 체크 가능 (콜 0) → 체크", dict(LIVE_KK, call=0.0, can_check=True), "체크")
check("라이브 p_014947: can_check 모름 + 콜 0 → 체크", dict(LIVE_KK, call=0.0, can_check=None), "체크")
check("라이브 p_014947: 차례 전(미리 계산) 체크 가능 → 체크 계획", dict(LIVE_KK, call=0.0, can_check=True, to_act=False),
      "체크", detail_has="차례가 오면: 체크")
check("체크 가능: 에쿼티 0% 에어 멀티웨이도 폴드 아님",
      dict(LIVE_KK, hero=["7s", "2h"], equity=0.0, call=0.0, can_check=True), "체크")
check("프리플랍 BB 옵션 (림프 팟, 체크 가능) 72o → 체크 (폴드 아님)",
      dict(PF, hero=["7c", "2d"], players=6, opponents_in_hand=3, hero_pos="BB", situation="open", stack_bb=100,
           my_stack=100, pot=4.5, pot_total=4.5, call=0.0, can_check=True), "체크")
# 3벳/4벳 팟 낮은 보드 오버페어: 상대 범위를 좁게(셋·투페어 위주) 잘못 읽어 에쿼티가 낮게 나와도 보통 벳에는 콜
KK_4B = dict(hero=["Kh", "Kc"], board=["7d", "4s", "2c"], street="flop", players=6, opponents_in_hand=1, hero_pos="CO",
             villain_pos="BTN", situation="vs3bet", pre_raises=3, stack_bb=75, my_stack=75, pot=50, pot_total=75,
             call=25, to_act=True, can_check=False, equity=0.12, equity_basis="range", aggressor=True)
r = recommend(KK_4B)
results.append((r["action"] != "폴드", "4벳 팟 KK 742 헤즈업 하프 벳, 좁게 읽힌 범위 에쿼티 12% → 폴드 아님", r))
for frac in (0.33, 0.5, 0.75, 1.0):
    for eq in (0.05, 0.25, 0.6):
        ctx = dict(KK_4B, call=round(50 * frac, 1), pot_total=round(50 + 50 * frac, 1), equity=eq,
                   board=["8d", "5s", "3c", "2h"], street="turn", stack_bb=300, my_stack=300, pre_raises=2)
        r = recommend(ctx)
        results.append((r["action"] != "폴드", f"3벳 팟 KK 8532 턴 헤즈업 벳 {frac:.0%} 에쿼티 {eq:.0%} → 폴드 아님", r))
r = recommend(dict(KK_4B, situation=None, pre_raises=2, equity=0.05, call=50, pot_total=100, stack_bb=300, my_stack=300,
                   board=["9d", "7s", "2c", "2h", "4d"], street="river"))
results.append((r["action"] != "폴드", "3벳 팟(pre_raises=2, 상황 모름) KK 97224 리버 팟 벳 에쿼티 5% → 폴드 아님", r))
check("싱글 레이즈 팟은 대상 아님: 팟 벳·에쿼티 5% → 폴드 가능",
      dict(KK_4B, situation="open", pre_raises=1, equity=0.05, call=50, pot_total=100, stack_bb=300, my_stack=300),
      "폴드")
check("3벳 팟 오버페어 + 팟 2배 오버벳은 '보통 벳' 아님 → 불변식 대상 아님 (에쿼티 5% 폴드)",
      dict(KK_4B, equity=0.05, call=100, pot_total=150, stack_bb=300, my_stack=300), "폴드")
check("높은 보드(A 있음)는 대상 아님: 3벳 팟 KK A72 에쿼티 5% 팟 벳 → 폴드 가능",
      dict(KK_4B, board=["Ad", "7s", "2c"], equity=0.05, call=50, pot_total=100, stack_bb=300, my_stack=300), "폴드")
check("멀티웨이(상대 2명)는 대상 아님: 3벳 팟 KK 742 에쿼티 5% 팟 벳 → 폴드 가능",
      dict(KK_4B, opponents_in_hand=2, equity=0.05, call=50, pot_total=100, stack_bb=300, my_stack=300), "폴드")

# --- 2026-10-30 라이브 회귀: CO QJo 림프 → BTN 4BB 레이즈 (BB·MP 콜), 콜 3BB 남음 ----------------------------------
# 예전: 이미 낸 1BB 를 몰라 '첫 진입'·림퍼 7명으로 보고 폴드/체크. 맞는 답: 팟 11.5 에 3 콜 (필요 21%) → 콜
QJ_LIMP = dict(PF, hero=["Js", "Qc"], players=6, opponents_in_hand=3, hero_pos="CO", situation="open", stack_bb=238,
               my_stack=238, pot=11.5, pot_total=11.5, call=3.0, can_check=False, hero_bet=1.0, equity=0.24,
               equity_basis="range")
check("라이브 QJo 림프 후 BTN 레이즈 4BB (팟 11.5) → 콜 3BB", QJ_LIMP, "콜", 3.0, 0.01, reason_has="림프 후 레이즈")
check("QJo 림프-레이즈: 상황 정보가 vsopen(상대 BTN, 차트 밖)이어도 → 콜",
      dict(QJ_LIMP, situation="vsopen", villain_pos="BTN"), "콜", 3.0, 0.01)
check("QJo 림프-레이즈: 상황 모름 + 에쿼티 없음 → 팟 오즈로 콜", dict(QJ_LIMP, situation=None, equity=None,
                                                         equity_basis=None), "콜", 3.0, 0.01)
check("QJo 림프-레이즈: 차례 전(미리 계산) → '차례가 오면: 콜'", dict(QJ_LIMP, to_act=False), "콜",
      detail_has="차례가 오면: 콜")
check("림프-레이즈: 큰 레이즈 (15BB, 팟 20) + 약한 에쿼티 → 폴드",
      dict(QJ_LIMP, call=14.0, pot=20.0, pot_total=20.0, equity=0.2), "폴드")
check("림프-레이즈: AA 는 다시 레이즈", dict(QJ_LIMP, hero=["Ah", "Ad"]), "레이즈")
check("림프-레이즈: 72o 는 에쿼티 낮으면 폴드", dict(QJ_LIMP, hero=["7h", "2d"], equity=0.12), "폴드")
r = recommend(dict(QJ_LIMP, hero_bet=None))
results.append((r["action"] != "체크", "hero_bet 모름 + 콜 3: 체크 아님 (콜할 금액이 있음)", r))
check("BB 블라인드 1BB 는 림프가 아님 (BB vs 오픈 기존 경로)",
      dict(QJ_LIMP, hero_pos="BB", hero_bet=1.0, situation="vsopen", villain_pos="BTN", hero=["Js", "Qc"]),
      "콜")
check("림프 없이 첫 진입 (hero_bet 0, 콜 1) 은 그대로 오픈 판단",
      dict(PF, hero=["Ah", "Kd"], players=6, hero_pos="CO", situation="open", stack_bb=100, my_stack=100,
           pot=1.5, pot_total=1.5, call=1.0, hero_bet=0.0), "레이즈")

# --- 결과 -----------------------------------------------------------------------
failed = 0
for ok, name, res in results:
    mark = "PASS" if ok else "FAIL"
    failed += not ok
    print(f"[{mark}] {name}: {res['action']} {res['size_text']} ({res['confidence']}) — {res['reason']}")
    if not ok:
        for d in res["detail"]:
            print("        ·", d)
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
