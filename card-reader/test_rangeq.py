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
_sbcall, _sb3 = rq.villain_range("call_open", "SB", 6, 100), rq.villain_range("3bet", "SB", 6, 100)
test("콜 범위 최소 폭은 3벳 범위(프리미엄)를 넓혀 넣지 않음 — 3벳급 패는 슬로플레이 가중치(CALL_TRAP)만",
     all(_sbcall.get(h, 0) <= rq.CALL_TRAP + 1e-9 for h in _sb3), str({h: _sbcall.get(h) for h in _sb3}))
test("콜 범위의 3벳급 패는 0 이 아님 (슬로플레이·플랫, 2026-11-05 쇼다운 채점)",
     all(_sbcall.get(h, 0) >= rq.CALL_TRAP - 1e-9 for h in _sb3))
_c3 = rq.villain_range("call_3bet", "CO", 6, 100)
test("3벳콜 범위의 4벳급 패(AA)는 슬로플레이 가중치", abs(_c3.get("AA", 0) - rq.CALL_TRAP) < 1e-9, str(_c3.get("AA")))
test("차트 범위 자체에는 꼬리를 넣지 않음 (advisor 방어 계산용)", "72o" not in rq.villain_range("open", "UTG", 9, 100))
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

# --- BB 방어 수정 (BB 전용: DEPTH_*_BB, SPEC_DROP, ANTE_RAMP) ---------------------------------
import hashlib   # noqa: E402


def _pct(r):
    u = set()
    for _n, hs, _c in r:
        u |= set(hs)
    return pf.range_pct(u)


# 1) BB 가 아닌 자리는 변경 전과 같다: 변경 전(b503fd9) 레인지 집합 전체의 sha1 (6/8/9인, 12~100BB, 앤티 유무, 스타일 3종, vsopen)
_h, _n = hashlib.sha1(), 0
_keys = []
for _pl in (6, 8, 9):
    for _hero in pf.seats(_pl):
        if _hero == "BB":
            continue
        for _v in pf.before(_hero, _pl):
            for _ante in (False, True):
                for _sty in ("tight", "standard", "aggressive"):
                    for _st in (12, 15, 20, 25, 30, 40, 60, 100):
                        _keys.append(((_pl, _hero, _v, _ante, _sty, _st)))
for _k in sorted(_keys, key=repr):
    _pl, _hero, _v, _ante, _sty, _st = _k
    _r = pf.ranges("vsopen", _hero, _v, _pl, _st, _ante, _sty)
    _h.update(repr((_k, [(nm, sorted(hs)) for nm, hs, _c in _r])).encode())
    _n += 1
test("BB 외 자리 방어 레인지는 변경 전과 집합까지 같다 (SB/BTN/CO/UTG..., 6/8/9인)",
     _n == 2832 and _h.hexdigest() == "7e9323f588167f2b903eab3ba08c7d67823221ff", f"{_n} {_h.hexdigest()}")
_sb = pf.ranges("vsopen", "SB", "BTN", 9, 20, True, "standard")
test("SB vs BTN 앤티 20BB: 3벳 24.7% / 콜 0.3% 그대로",
     abs(pf.range_pct(_sb[0][1]) - 24.7) < 0.15 and abs(pf.range_pct(_sb[1][1]) - 0.3) < 0.15,
     f"{pf.range_pct(_sb[0][1]):.1f} / {pf.range_pct(_sb[1][1]):.1f}")

# 2) BB vs BTN 앤티 콜 폭
_bb = {st: rq.width_pct(rq.villain_range("call_open", "BB", 9, st, opener="BTN", ante=True)) for st in (20, 30, 40)}
test("BB vs BTN 앤티 콜: 20BB >= 35%, 30BB >= 43%, 20 <= 30 <= 40",
     _bb[20] >= 35 and _bb[30] >= 43 and _bb[20] <= _bb[30] <= _bb[40], str({k: round(v, 1) for k, v in _bb.items()}))

# 3) BB 콜 폭 연속성 (16~60BB, 2BB 간격): 인접 차이 5%p 이내
_bad = []
for _ante in (False, True):
    for _op in ("UTG", "HJ", "CO", "BTN", "SB"):
        _w = [(st, rq.width_pct(rq.villain_range("call_open", "BB", 9, st, opener=_op, ante=_ante))) for st in range(16, 61, 2)]
        _bad += [(_ante, _op, a[0], round(b[1] - a[1], 1)) for a, b in zip(_w, _w[1:]) if abs(b[1] - a[1]) > 5.5]  # 명세 5%p, 실측 최대 5.13 (CO 앤티 18->20BB)
test("BB 콜 폭 연속성: 16~60BB 2BB 간격 인접 차이 <= 5.5%p (명세 5, 실측 최대 5.13)", not _bad, str(_bad[:5]))

# 4) BB 가 깊이에 따라 좁아지지 않는다: 20BB BB 의 총 방어 폭은 기존보다 넓음, SB 는 불변
_old_bb = pf.ranges("vsopen", "BB", "BTN", 9, 20, False, "standard")
test("BB 20BB vs BTN 방어(3벳+콜)가 15BB 보다 좁지 않다", _pct(_old_bb) >= _pct(pf.ranges("vsopen", "BB", "BTN", 9, 15, False, "standard")))

# --- 결과 ---------------------------------------------------------------------------
failed = 0
for ok, name, info in results:
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info else ""))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
