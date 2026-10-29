"""rangeq 테스트 — python test_rangeq.py   (속도는 측정값만 출력, 단언하지 않는다)"""
import random
import sys
import time

import evaluator as ev
import preflop as pf
import rangeq as rq

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

results = []


def P(cards):
    return [ev.parse_card(x) for x in cards]


def eqr(hero, board, ranges, iters=10000, seed=7):
    return rq.equity_vs_ranges(P(hero), P(board), ranges, iters, random.Random(seed))


def test(name, ok, info=""):
    results.append((bool(ok), name, info))


RANDOM = rq.random_range()
UTG9 = rq.villain_range("open", "UTG", 9, 100)
TIGHT3 = rq.villain_range("3bet", "BTN", 6, 100)

# --- 레인지 생성 ---------------------------------------------------------------------
test("UTG(9인) 오픈 폭 ≈ 12% (차트)", 11 <= rq.width_pct(UTG9) <= 13.5, f"{rq.width_pct(UTG9):.1f}%")
test("BTN 오픈이 UTG 오픈보다 넓다", rq.width_pct(rq.villain_range("open", "BTN", 6, 100)) > rq.width_pct(UTG9) * 2)
test("3벳 레인지는 타이트 (< 8%)", rq.width_pct(TIGHT3) < 8, f"{rq.width_pct(TIGHT3):.1f}%")
test("AA 는 오픈/3벳/4벳/잼콜 레인지에 모두 있다",
     all("AA" in rq.villain_range(a, "CO", 6, 100) for a in ("open", "3bet", "4bet", "call_jam")))
test("랜덤 레인지 = 169핸드, 100%", len(RANDOM) == 169 and abs(rq.width_pct(RANDOM) - 100) < 0.01)
test("action None → 랜덤", rq.width_pct(rq.villain_range(None, "CO", 6, 100)) > 99.9)
test("blind 레인지는 넓고 약한 핸드 가중치가 낮다",
     rq.villain_range("blind", "BB", 6, 100)["72o"] < rq.villain_range("blind", "BB", 6, 100)["AA"])
test("limp 레인지: 프리미엄은 낮은 가중치", rq.villain_range("limp", "CO", 6, 100)["AA"] < 0.5)
test("모든 action 이 유효한 핸드코드/가중치 (0,1]",
     all(0 < w <= 1 and h in pf.DATA["G"]
         for a in ("open", "call_open", "3bet", "call_3bet", "4bet", "limp", "jam", "call_jam", "blind", None)
         for h, w in rq.villain_range(a, "CO", 6, 40).items()))
test("잼 레인지는 스택이 깊을수록 좁다",
     rq.width_pct(rq.villain_range("jam", "CO", 6, 10)) > rq.width_pct(rq.villain_range("jam", "CO", 6, 40))
     > rq.width_pct(rq.villain_range("jam", "CO", 6, 100)))

# --- 프로필 ------------------------------------------------------------------------
BASE_OPEN = rq.width_pct(rq.villain_range("open", "BTN", 6, 100))
tight = {"vpip": 12, "pfr": 9, "threebet": 3, "hands": 200, "reliable": True}
loose = {"vpip": 45, "pfr": 8, "threebet": 2, "hands": 64, "reliable": True}
w_open_tight = rq.width_pct(rq.villain_range("open", "BTN", 6, 100, tight))
test("신뢰 가능한 타이트 프로필 → 오픈 범위 축소", w_open_tight < BASE_OPEN * 0.5, f"{BASE_OPEN:.0f}% → {w_open_tight:.0f}%")
test("hands < 30 이면 프로필 무시",
     rq.villain_range("open", "BTN", 6, 100, dict(tight, hands=20, reliable=None)) == rq.villain_range("open", "BTN", 6, 100))
test("reliable=False 면 프로필 무시",
     rq.villain_range("open", "BTN", 6, 100, dict(tight, reliable=False)) == rq.villain_range("open", "BTN", 6, 100))
test("블렌드 가중치 hands/(hands+50)", abs(rq.profile_weight({"hands": 50, "reliable": True}) - 0.5) < 1e-9)
more = rq.width_pct(rq.villain_range("open", "BTN", 6, 100, dict(tight, hands=2000)))
test("표본이 많을수록 프로필 쪽으로 더 이동", more < w_open_tight)
call_base = rq.width_pct(rq.villain_range("call_open", "BTN", 6, 100))
call_loose = rq.width_pct(rq.villain_range("call_open", "BTN", 6, 100, loose))
test("루즈 프로필(VPIP 45/PFR 8) → 콜 범위 확대", call_loose > call_base * 2, f"{call_base:.0f}% → {call_loose:.0f}%")
# 회귀 2026-10-30: 프로필 없는 SB 콜 범위가 GTO 차트(3% 남짓)라 리버에 '상위 2%' 로 좁혀졌다 → 최소 폭
_sbc = rq.width_pct(rq.villain_range("call_open", "SB", 6, 100))
test(f"프로필 없는 SB 콜 범위 ≥ 최소 폭 {rq.CALL_OPEN_FLOOR:g}%", _sbc >= rq.CALL_OPEN_FLOOR - 0.5, f"{_sbc:.1f}%")
test("콜 범위 최소 폭은 3벳 범위(프리미엄)를 넣지 않음",
     not (set(rq.villain_range("call_open", "SB", 6, 100)) & set(rq.villain_range("3bet", "SB", 6, 100))))
test("3벳% 프로필 반영 (3bet 12%)",
     rq.width_pct(rq.villain_range("3bet", "BTN", 6, 100, {"threebet": 12, "hands": 500, "reliable": True}))
     > rq.width_pct(TIGHT3) + 3)

# --- 에쿼티 -------------------------------------------------------------------------
aa_rand = eqr(["Ah", "Ad"], [], [RANDOM])["equity"]
aa_utg = eqr(["Ah", "Ad"], [], [UTG9])["equity"]
test("AA vs UTG 오픈 < AA vs 랜덤", aa_utg < aa_rand, f"{aa_utg:.3f} < {aa_rand:.3f}")
o72 = eqr(["7h", "2d"], [], [TIGHT3])["equity"]
test("72o vs 타이트 3벳 레인지는 매우 낮다 (< 0.30)", o72 < 0.30, f"{o72:.3f}")
test("72o vs 3벳 < 72o vs 랜덤", o72 < eqr(["7h", "2d"], [], [RANDOM])["equity"])

diffs = []
for hero, board in ((["Ah", "Kd"], []), (["7c", "7d"], ["Ks", "9h", "2c"]), (["Jh", "Th"], ["Qh", "9d", "2c", "5s"]),
                    (["Ad", "Qs"], ["Qh", "7d", "2c", "Ts", "3c"])):
    for n in (1, 3):
        a = eqr(hero, board, [RANDOM] * n, 12000)["equity"]
        b = ev.equity(P(hero), P(board), opponents=n, iterations=12000, exact_limit=0, rng=random.Random(11))["equity"]
        diffs.append((abs(a - b), hero, board, n))
worst = max(diffs, key=lambda d: d[0])
test("랜덤 레인지 에쿼티 ≈ evaluator.equity (±2%)", worst[0] <= 0.02, f"최대 차이 {worst[0]:.4f} {worst[1:]}")

r = eqr(["Ah", "Kd"], ["Qs", "Jc", "2h"], [RANDOM])
test("win+tie+lose = 1, samples = iterations", abs(r["win"] + r["tie"] + r["lose"] - 1) < 1e-9 and r["samples"] == 10000
     and r["exact"] is False, str(r))
r_full = eqr(["Ah", "Kd"], ["Qs", "Jc", "2h", "3d", "9s"], [UTG9], 3000)
test("리버(보드 5장)도 동작", 0 <= r_full["equity"] <= 1)

# --- 멀티웨이 -----------------------------------------------------------------------
e1 = eqr(["Kh", "Kd"], [], [UTG9])["equity"]
e2 = eqr(["Kh", "Kd"], [], [UTG9, UTG9])["equity"]
e4 = eqr(["Kh", "Kd"], [], [UTG9] * 4)["equity"]
test("멀티웨이: 상대가 늘수록 에쿼티 감소", e1 > e2 > e4, f"{e1:.3f} > {e2:.3f} > {e4:.3f}")
mix = eqr(["Ah", "Qh"], ["Kh", "7h", "2c"], [rq.villain_range("open", "UTG", 9, 100), TIGHT3, RANDOM])
test("서로 다른 레인지 3명", 0 < mix["equity"] < 1 and mix["samples"] == 10000, f"{mix['equity']:.3f}")
sym = [eqr(["Ah", "Kd"], [], [RANDOM] * 2, 20000, seed=s)["equity"] for s in (1, 2)]
test("같은 레인지 둘 → 시드가 달라도 비슷", abs(sym[0] - sym[1]) < 0.02)

# --- 죽은 카드 ----------------------------------------------------------------------
aa_dead = eqr(["Ah", "Ad"], [], [{"AA": 1.0}], 4000)
test("죽은 카드 제외: AhAd vs {AA} → 남은 AsAc 한 콤보 (대칭이라 50%)", abs(aa_dead["equity"] - 0.5) < 0.02,
     f"{aa_dead['equity']:.3f}")
fb = eqr(["Ah", "Kh"], ["Qc", "2d", "7s"], [{"AhKd": 1.0}], 2000)
test("레인지가 전부 죽은 카드와 겹치면 랜덤으로 대체 (예외 없음)", 0 < fb["equity"] < 1)
try:
    eqr(["Kh", "Kd"], [], [{"AsKs": 1.0}, {"AsKs": 1.0}], 500)
    test("상대 둘이 같은 콤보뿐이면 ValueError", False)
except ValueError:
    test("상대 둘이 같은 콤보뿐이면 ValueError", True)
try:
    rq.equity_vs_ranges(P(["Ah", "Ah"]), [], [RANDOM], 100)
    test("중복 카드 → ValueError", False)
except ValueError:
    test("중복 카드 → ValueError", True)
board_dead = rq.narrow(RANDOM, P(["Kh", "7d", "2c"]), "bet")
bad = [k for k in board_dead if k[:2] in ("Kh", "7d", "2c") or k[2:] in ("Kh", "7d", "2c")]
test("narrow 결과에 보드 카드 콤보 없음", not bad)

# --- narrow -------------------------------------------------------------------------
BOARD = ["Kh", "7d", "2c"]
full = rq.narrow(RANDOM, P(BOARD), "check", keep=1.0)
nb, nr = rq.narrow(RANDOM, P(BOARD), "bet"), rq.narrow(RANDOM, P(BOARD), "raise")
test("narrow: bet 60% / raise 35% 근처로 남김",
     abs(len(nb) / len(full) - 0.6) < 0.06 and abs(len(nr) / len(full) - 0.35) < 0.06,
     f"{len(nb)}/{len(full)}  {len(nr)}/{len(full)}")
test("narrow: 레이즈 범위 ⊂ 벳 범위", set(nr) <= set(nb))
test("narrow: 약한 하이카드(예: 9h8c)는 제거, 탑페어(Kc9s)는 유지", "9h8c" not in nr and any(k in nb for k in ("Kc9s", "Ks9s")))
eq_full = eqr(["Ks", "Qd"], BOARD, [RANDOM], 6000)["equity"]
eq_bet = eqr(["Ks", "Qd"], BOARD, [nb], 6000)["equity"]
eq_raise = eqr(["Ks", "Qd"], BOARD, [nr], 6000)["equity"]
test("narrow: 좁힐수록 내 에쿼티 하락 (랜덤 > 벳 > 레이즈)", eq_full > eq_bet > eq_raise,
     f"{eq_full:.3f} > {eq_bet:.3f} > {eq_raise:.3f}")
fd = rq.narrow({"AhJh": 1.0, "9c8d": 1.0, "QcQd": 1.0}, P(["Kh", "7h", "2h"]), "raise", keep=0.66)
test("narrow: 플러시(AhJh)·오버페어는 유지, 에어(9c8d)는 제거", "9c8d" not in fd and "AhJh" in fd, str(sorted(fd)))

# --- 속도 (측정만) -------------------------------------------------------------------
print("\n속도 (6000 iterations, ms)  evaluator.equity vs rangeq.equity_vs_ranges(랜덤 레인지)")
for nb_ in (0, 3):
    for n in (1, 2, 5):
        board = P(["Kc", "7d", "2s"]) if nb_ else []
        hero = P(["Ah", "Qd"])
        t = time.perf_counter()
        ev.equity(hero, board, opponents=n, iterations=6000, exact_limit=0, rng=random.Random(1))
        t_ev = time.perf_counter() - t
        t = time.perf_counter()
        rq.equity_vs_ranges(hero, board, [RANDOM] * n, 6000, random.Random(1))
        t_rq = time.perf_counter() - t
        t = time.perf_counter()
        rq.equity_vs_ranges(hero, board, [UTG9] * n, 6000, random.Random(1))
        t_utg = time.perf_counter() - t
        print(f"  보드 {nb_}장 상대 {n}명: evaluator {t_ev * 1000:6.0f}  rangeq(랜덤) {t_rq * 1000:6.0f} "
              f"({t_rq / t_ev:.2f}x)  rangeq(UTG범위) {t_utg * 1000:6.0f}")

# --- 앤티 / 스택 깊이 반영 (재보정) -------------------------------------------------------
_w0 = rq.width_pct(rq.villain_range("open", "HJ", 9, 40))
_w1 = rq.width_pct(rq.villain_range("open", "HJ", 9, 40, ante=True))
test("ante: 앤티가 있으면 오픈 레인지가 넓어진다 (HJ)", _w1 > _w0 * 1.05, f"{_w0:.1f}% -> {_w1:.1f}%")
_c0 = rq.width_pct(rq.villain_range("call_open", "BB", 9, 40, opener="BTN"))
_c1 = rq.width_pct(rq.villain_range("call_open", "BB", 9, 40, opener="BTN", ante=True))
test("ante: BB 의 BTN 오픈 콜 레인지도 넓어진다", _c1 > _c0 * 1.2, f"{_c0:.1f}% -> {_c1:.1f}%")
_s = [rq.width_pct(rq.villain_range("call_open", "BB", 9, st, opener="BTN", ante=True)) for st in (18, 30, 60)]
test("깊이: 같은 BB 콜 레인지는 스택이 얕을수록 좁다 (18 < 30 < 60BB)", _s[0] < _s[1] < _s[2], " < ".join(f"{x:.1f}" for x in _s))
test("결정적: 같은 입력이면 같은 레인지(키 순서 포함)", list(rq.villain_range("open", "CO", 9, 40, ante=True)) == list(rq.villain_range("open", "CO", 9, 40, ante=True)))

# --- 결과 ---------------------------------------------------------------------------
failed = 0
for ok, name, info in results:
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info else ""))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
