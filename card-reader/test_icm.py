"""icm.py 테스트 — python test_icm.py"""
import random
import sys
import time

import icm

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

results = []


def ok(cond, name, info=""):
    results.append((bool(cond), name, info))


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


# --- 알려진 예 ----------------------------------------------------------------------
eq = icm.icm_equities([50, 30, 20], [0.5, 0.3, 0.2])
want = [0.38392857142857145, 0.3275, 0.28857142857142853]   # 손 계산 (Malmuth-Harville 교과서 예)
ok(all(close(a, b) for a, b in zip(eq, want)), "3명 50/30/20 · 상금 50/30/20 → 38.39 / 32.75 / 28.86%", eq)
eq = icm.icm_equities([1000, 1000], [0.65, 0.35])
ok(close(eq[0], 0.5) and close(eq[1], 0.5), "헤즈업 같은 스택 → 50:50", eq)
eq = icm.icm_equities([70, 30], [0.65, 0.35])
ok(close(eq[0], 0.35 + 0.30 * 0.7) and close(eq[1], 0.35 + 0.30 * 0.3), "헤즈업: 2등 상금 + 칩 비율 × 상금 차", eq)
eq = icm.icm_equities([50, 30, 20], [1.0])
ok(all(close(a, b) for a, b in zip(eq, [0.5, 0.3, 0.2])), "승자 독식 → 칩 비율 그대로 (칩EV)", eq)
eq = icm.icm_equities([40, 0, 60], [0.5, 0.3, 0.2])
ok(close(eq[1], 0.2) and close(eq[0] + eq[2], 0.8), "스택 0 = 탈락 → 남은 최하 상금", eq)

# --- 대칭 / 합 / 단조 ----------------------------------------------------------------
r = random.Random(7)
sym = True
for _ in range(20):
    n = r.randint(3, 9)
    st = [round(r.uniform(1, 100), 1) for _ in range(n)]
    pays = icm.payouts(r.randint(1, n))
    perm = list(range(n))
    r.shuffle(perm)
    a = icm.icm_equities(st, pays)
    b = icm.icm_equities([st[i] for i in perm], pays)
    sym = sym and all(close(b[k], a[perm[k]], 1e-12) for k in range(n))
ok(sym, "대칭: 자리 순서를 섞어도 같은 스택은 같은 에쿼티")
eq = icm.icm_equities([25] * 7, icm.payouts(3))
ok(max(eq) - min(eq) < 1e-12 and close(eq[0], 1 / 7), "같은 스택 7명 → 모두 1/7", eq)
pool = 633_250_000   # '총 상금 6억 3325만'
pays = icm.payouts(5, prize_pool=pool)
eq = icm.icm_equities([12, 40, 33, 7, 25, 60, 18], pays)
ok(abs(sum(eq) - pool) < 1e-3, "합 = 총 상금 (7명 남음, 5명 지급)", sum(eq))
eq = icm.icm_equities([12, 40, 33], icm.payouts(5, prize_pool=pool))
ok(abs(sum(eq) - sum(icm.payouts(5, prize_pool=pool)[:3])) < 1e-3, "합 = 남은 순위 상금 합 (3명 남음, 5명 지급)", sum(eq))
big = [r.uniform(5, 80) for _ in range(30)]
eq = icm.icm_equities(big, icm.payouts(27))
ok(abs(sum(eq) - 1.0) < 1e-8, "합 = 1 (30명 적분 경로)", sum(eq))
mono = True
for _ in range(20):
    n = r.randint(3, 14)
    st = sorted(r.uniform(1, 100) for _ in range(n))
    e = icm.icm_equities(st, icm.payouts(r.randint(1, n), structure=r.choice(["standard", "flat", "top_heavy"])))
    mono = mono and all(e[i] <= e[i + 1] + 1e-12 for i in range(n - 1))
ok(mono, "단조: 스택이 클수록 에쿼티가 크다 (정확/적분 경로)")
e1 = icm.icm_equities([10, 30, 60], [0.5, 0.3, 0.2])
ok(e1[2] / 0.6 < e1[0] / 0.1, "ICM 오목성: 칩당 $ 는 숏스택이 더 크다", e1)

# --- 상금 구조 -------------------------------------------------------------------------
for s in ("standard", "flat", "top_heavy"):
    p = icm.payouts(15, structure=s)
    ok(close(sum(p), 1.0) and all(p[i] >= p[i + 1] for i in range(14)), f"payouts {s}: 합 1, 내림차순", [round(x, 3) for x in p[:3]])
p = icm.payouts(15)
ok(0.2 < p[0] < 0.3 and p[0] > icm.payouts(15, structure="flat")[0] and p[0] < icm.payouts(15, structure="top_heavy")[0],
   "standard 15명 지급: 1등 20~30%, flat < standard < top_heavy", round(p[0], 3))
ok(p[9] == p[10] == p[11] and p[12] == p[14], "10등 아래는 구간 지급 (10~12, 13~15)")
ok(icm.payouts(3, structure=[50, 30, 20]) == [0.5, 0.3, 0.2], "custom 리스트 정규화")
ok(icm.paid_places(180) == 27 and icm.paid_places(9) == 3 and icm.paid_places(2) == 1, "paid_places: 15%, 작은 필드 최소 3명",
   (icm.paid_places(180), icm.paid_places(9)))

# --- 정확 재귀 vs 적분 (10명) --------------------------------------------------------------
worst = 0.0
for _ in range(25):
    n = 10
    st = [r.choice([r.uniform(1, 100), r.uniform(0.3, 5), r.uniform(50, 500)]) for _ in range(n)]
    for s in ("standard", "flat", "top_heavy"):
        pays = icm.payouts(r.randint(1, 10), structure=s)
        ex = icm._exact(st, pays)
        for i in range(n):
            g, _ = icm._groups([st[j] for j in range(n) if j != i])
            d = icm._place_dist(st[i], g, min(n, len(pays)))
            worst = max(worst, abs(sum(d[k] * pays[k] for k in range(len(d))) - ex[i]))
ok(worst < 1e-8, f"적분(지수 경주) vs 정확 재귀 10명 최대 오차 {worst:.1e} (< 1e-8)")
# 같은 스택 묶음(이항) 경로 vs 한 명씩
g1 = icm._place_dist(20.0, [(30.0, 1), (25.0, 12)], 10)
g2 = icm._place_dist(20.0, [(30.0, 1)] + [(25.0, 1)] * 12, 10)
ok(max(abs(a - b) for a, b in zip(g1, g2)) < 1e-10, "같은 스택 이항 합성 = 한 명씩 합성")

# --- 필드 축소 모델 오차 (테이블 4명만 알고 6명은 평균 스택) --------------------------------------
err_eq, err_bf = 0.0, 0.0
for _ in range(15):
    st = [r.uniform(5, 60) for _ in range(10)]
    pays = icm.payouts(r.choice([3, 5, 7]))
    hero, vil, known = st[0], st[1], st[2:4]
    true_rest = st[4:]
    avg = sum(st) / 10
    model_rest = icm.rest_of_field(10, [hero, vil] + known, avg)
    t = icm.hero_equity(hero, [vil] + known + true_rest, pays)
    m = icm.hero_equity(hero, [vil] + known + model_rest, pays)
    err_eq = max(err_eq, abs(t - m))
    bt = icm.bubble_factor(hero, vil, known + true_rest, pays)
    bm = icm.bubble_factor(hero, vil, known + model_rest, pays)
    err_bf = max(err_bf, abs(bt - bm))
ok(err_eq < 0.01 and err_bf < 0.25, f"필드 축소 모델: 내 에쿼티 오차 최대 {err_eq * 100:.2f}%p, 버블 팩터 오차 최대 {err_bf:.2f}")

# --- 버블 팩터 / 필요 승률 -------------------------------------------------------------------
bf_bubble = icm.bubble_factor(25, 25, [25, 25], [0.5, 0.3, 0.2])
ok(1.7 < bf_bubble < 2.0, f"SNG 버블 (4명 같은 스택, 3명 지급) 버블 팩터 {bf_bubble:.2f} > 1.7", bf_bubble)
bf_sng = icm.bubble_factor(1500, 1500, [1500] * 7, [0.5, 0.3, 0.2])
ok(1.1 < bf_sng < 1.3, f"SNG 시작 (9명 같은 스택) 버블 팩터 {bf_sng:.2f} ≈ 1.2", bf_sng)
early = icm.tournament_state(900, 1000, 300, [30] * 9, avg_stack=30)
ok(early["stage"] == "early" and early["bubble_factor"] < 1.1, f"초반 (900/1000) 버블 팩터 {early['bubble_factor']:.2f} ≈ 1",
   early)
ok(icm.bubble_factor(25, 25, [(25, 2)], [1.0]) == 1.0 or close(icm.bubble_factor(25, 25, [(25, 2)], [1.0]), 1.0, 1e-9),
   "승자 독식 → 버블 팩터 1")
cover = icm.bubble_factor(60, 20, [(25, 2), (20, 26)], icm.payouts(27))
covered = icm.bubble_factor(20, 60, [(25, 2), (20, 26)], icm.payouts(27))
ok(covered > cover > 1.0, f"버블(30명/27명): 덮인 쪽 {covered:.2f} > 덮는 쪽 {cover:.2f} > 1")
need = icm.required_equity_icm(10, 15, 30, 20, [(25, 3)], [1.0])
ok(close(need, 10 / 25, 1e-9), "승자 독식이면 ICM 필요 승률 = 칩 팟 오즈", need)
d = icm.required_equity_icm(20, 22, 20, 30, [(20, 27)], icm.payouts(27), detail=True)
ok(d["icm"] > d["chip"] + 0.05 and 0 <= d["icm"] < 1, f"버블 콜: 필요 승률 {d['chip']:.0%} → {d['icm']:.0%}", d)
d2 = icm.required_equity_icm(20, 22, 20, 30, [(20, 27)], icm.payouts(27), detail=True)
ok(d2 == d, "결정적 (캐시)")
rr = random.Random(11)
rng_ok = True
for _ in range(300):
    st = [rr.uniform(0.5, 100) for _ in range(rr.randint(1, 12))]
    v = rr.choice([0.0, rr.uniform(0, 100)])
    q = icm.required_equity_icm(rr.uniform(0, 100), rr.uniform(0, 100), rr.uniform(0.1, 100), v, st,
                                icm.payouts(rr.randint(1, 20), structure=rr.choice(["standard", "flat", "top_heavy"])))
    rng_ok = rng_ok and 0.0 <= q < 1.0
ok(rng_ok, "무작위 300건: ICM 필요 승률 ∈ [0, 1)")

# --- 단계 -------------------------------------------------------------------------------
stages = [icm.tournament_state(n, None, None, [], avg_stack=30, paid=27)["stage"] for n in (200, 60, 40, 30, 27, 12, 9)]
ok(stages == ["early", "mid", "near_bubble", "bubble", "itm", "itm", "final"], "단계: 초반→중반→버블 근처→버블→ITM→파이널", stages)
st = icm.tournament_state(32, None, 6, {1: 30, 2: 22, 3: 18})
ok(st["stage"] == "unknown" and st["risk_premium"] == 0.0, "지급 인원 모르면 unknown (보정 없음)", st["stage"])
st = icm.tournament_state(32, 200, 6, {1: 30, 2: 22, 3: 18})
ok(st["paid"] == 30 and st["stage"] == "bubble" and abs(st["avg_stack"] - 70 / 3) < 1e-9 and st["risk_premium"] > 0.05,
   f"평균 스택 추정 + 버블 위험 프리미엄 {st['risk_premium'] * 100:.0f}%p", st)
itm_lo = icm.tournament_state(26, None, None, [], avg_stack=675 / 26, paid=27)
itm_hi = icm.tournament_state(5, None, None, [], avg_stack=675 / 5, paid=27)
ok(itm_hi["bubble_factor"] > itm_lo["bubble_factor"] > 1.0, f"인 더 머니: 깊을수록(5명 {itm_hi['bubble_factor']:.2f}) > 막 진입(26명 "
   f"{itm_lo['bubble_factor']:.2f})")

# --- 속도 -------------------------------------------------------------------------------
t0 = time.perf_counter()
for k in range(20):
    icm.hero_equity(20 + k * 0.37, [(30, 1), (25, 7), (22, 23)], icm.payouts(27))
ms32 = (time.perf_counter() - t0) / 20 * 1000
ok(ms32 < 10, f"32명 필드 에쿼티 {ms32:.2f} ms (< 10)")
t0 = time.perf_counter()
icm._exact([r.uniform(1, 50) for _ in range(10)], icm.payouts(10))
ms10 = (time.perf_counter() - t0) * 1000
ok(ms10 < 30, f"정확 재귀 10명 {ms10:.1f} ms (< 30)")
t0 = time.perf_counter()
for k in range(5):
    icm.bubble_factor(18 + k, 40, [(25, 7), (24, 300)], icm.payouts(45))
ms300 = (time.perf_counter() - t0) / 5 * 1000
ok(ms300 < 30, f"버블 팩터 310명 필드 {ms300:.1f} ms (< 30)")

failed = 0
for good, name, info in results:
    failed += not good
    print(f"[{'PASS' if good else 'FAIL'}] {name}" + ("" if good else f"  — {info}"))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
