"""eqtable 테스트 — python test_eqtable.py   (프리플랍 표 + 포스트플랍/레인지 캐시. 표가 없으면 표 테스트는 SKIP)"""
import itertools
import os
import random
import shutil
import sys
import tempfile
import threading
import time

import eqtable
import evaluator as ev
import rangeq as rq

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

results = []


def test(name, ok, info=""):
    results.append((bool(ok), name, info))


def P(cards):
    return [ev.parse_card(x) for x in cards]


TMP = tempfile.mkdtemp(prefix="eqtest_")
STORE = eqtable.Store(os.path.join(TMP, "cache.sqlite"))
eqtable.set_store(STORE)          # 진짜 캐시 파일을 건드리지 않는다


def permute(cards, perm):
    return [(c >> 2) * 4 + perm[c & 3] for c in cards]


def equivalent(a, b):
    """(hero, board) a, b 가 무늬 순열로 서로 옮겨지는가 (전수 확인)."""
    for p in itertools.permutations(range(4)):
        if sorted(permute(a[0], p)) == sorted(b[0]) and sorted(permute(a[1], p)) == sorted(b[1]):
            return True
    return False


# --- evaluate_fast ----------------------------------------------------------------
r_ = random.Random(1)
ok = True
for _ in range(50000):
    cs = r_.sample(range(52), r_.choice([5, 6, 7]))
    if ev.evaluate(cs) != ev.evaluate_fast(cs):
        ok = False
        break
test("evaluate_fast == evaluate (5만 랜덤 핸드)", ok)

# --- 무늬 정규화 키 ------------------------------------------------------------------
r_ = random.Random(2)
ok, n_checked = True, 0
for _ in range(1500):
    nb = r_.choice([3, 4, 5])
    cs = r_.sample(range(52), 2 + nb)
    hero, board = cs[:2], cs[2:]
    opp = r_.randint(1, 6)
    k0 = eqtable.post_key(hero, board, opp)
    perm = r_.sample(range(4), 4)
    h2, b2 = permute(hero, perm), permute(board, perm)
    r_.shuffle(h2)
    r_.shuffle(b2)
    n_checked += 1
    if eqtable.post_key(h2, b2, opp) != k0:
        ok = False
        break
test("post_key: 무늬 순열/카드 순서에 불변", ok, f"{n_checked}건")

pairs_same = pairs_diff = 0
ok = True
for _ in range(3000):
    nb = r_.choice([3, 4])
    a = r_.sample(range(52), 2 + nb)
    # 무늬가 몰린 보드를 일부러 섞어 서로 다른 구조도 충분히 만든다
    b = r_.sample(range(52), 2 + nb) if r_.random() < 0.5 else permute(a, r_.sample(range(4), 4))
    A, B = (a[:2], a[2:]), (b[:2], b[2:])
    same_key = eqtable.post_key(*A, 1) == eqtable.post_key(*B, 1)
    eq_ = equivalent(A, B)
    pairs_same += eq_
    pairs_diff += not eq_
    if same_key != eq_:
        ok = False
        break
test("post_key: 키가 같다 <=> 무늬 순열로 옮겨진다 (전수 대조)", ok, f"동치 {pairs_same} / 비동치 {pairs_diff}")
test("post_key: 같은 무늬 vs 다른 무늬는 다른 키",
     eqtable.post_key(P(["Ah", "Kh"]), P(["Qh", "Jh", "2c"]), 1) != eqtable.post_key(P(["Ah", "Kd"]), P(["Qh", "Jh", "2c"]), 1))
test("post_key: 상대 수가 다르면 다른 키",
     eqtable.post_key(P(["Ah", "Kh"]), P(["Qh", "Jh", "2c"]), 1) != eqtable.post_key(P(["Ah", "Kh"]), P(["Qh", "Jh", "2c"]), 2))

# 레인지 키
hero, board = P(["Ah", "Kd"]), P(["Qs", "Jc", "2h"])
combo_rng = {"AhKd": 1.0, "QsQh": 0.5, "9c9d": 1.0, "TsTh": 0.3}
cls_rng = rq.villain_range("open", "UTG", 9, 100)


def rkey(hero, board, ranges):
    return eqtable.range_key(hero, board, eqtable.range_descs(ranges, hero, board, rq.expand, None))


def permute_combo_range(rg, perm):
    out = {}
    for k, w in rg.items():
        cs = permute([ev.parse_card(k[:2]), ev.parse_card(k[2:])], perm)
        out[ev.card_str(cs[0]) + ev.card_str(cs[1])] = w
    return out


ok = True
for perm in itertools.permutations(range(4)):
    h2, b2 = permute(hero, perm), permute(board, perm)
    if rkey(h2, b2, [permute_combo_range(combo_rng, perm), cls_rng]) != rkey(hero, board, [combo_rng, cls_rng]):
        ok = False
test("range_key: 콤보 레인지를 같은 순열로 옮기면 키 불변 (24가지)", ok)
test("range_key: 클래스 레인지는 순열에 불변, 상대 순서 무관",
     rkey(permute(hero, (1, 0, 3, 2)), permute(board, (1, 0, 3, 2)), [cls_rng, rq.random_range()])
     == rkey(hero, board, [rq.random_range(), cls_rng]))
test("range_key: 가중치 배율 불변 / 가중치가 다르면 다른 키",
     rkey(hero, board, [{"AA": 2.0, "KK": 1.0}]) == rkey(hero, board, [{"AA": 1.0, "KK": 0.5}])
     and rkey(hero, board, [{"AA": 1.0, "KK": 0.5}]) != rkey(hero, board, [{"AA": 1.0, "KK": 0.4}]))

# --- 저장소: 병합 / exact / 제거 / 스레드 --------------------------------------------------
st = eqtable.Store(os.path.join(TMP, "unit.sqlite"), post_cap=100, range_cap=100)
mk = lambda eq, n, exact=False: {"equity": eq, "win": eq - 0.01, "tie": 0.02, "lose": 1 - eq - 0.01, "samples": n, "exact": exact}
st.add("post", "k1", mk(0.60, 1000))
st.add("post", "k1", mk(0.70, 3000))
g = st.get("post", "k1")
test("저장소: 표본 수 가중 병합", abs(g["equity"] - 0.675) < 1e-9 and g["samples"] == 4000 and not g["exact"], str(g))
st.add("post", "ex", mk(0.5, 990, True))
st.add("post", "ex", mk(0.9, 5000))
g = st.get("post", "ex")
test("저장소: exact 항목은 몬테카를로 결과로 안 바뀐다", g["exact"] and g["equity"] == 0.5 and g["samples"] == 990)
test("저장소: 없는 키는 None, 재오픈 후에도 유지", st.get("post", "nope") is None)
st.close()
st2 = eqtable.Store(os.path.join(TMP, "unit.sqlite"), post_cap=100, range_cap=100)
test("저장소: 파일에 영속", st2.get("post", "k1")["samples"] == 4000)
for i in range(1400):
    st2.add("post", f"e{i}", mk(0.5, 10))
    if i % 20 == 0:
        st2.get("post", "k1")           # k1 을 계속 최근 사용으로
        st2._flush_touch()
st2.evict("post")
test("저장소: 상한 초과 시 오래된 것부터 제거", st2.count("post") <= 100 and st2.get("post", "e1399") is not None
     and st2.get("post", "e5") is None, f"count={st2.count('post')}")
test("저장소: 최근 사용한 항목은 살아남는다 (LRU)", st2.get("post", "k1") is not None)

# 스레드 안전 스모크
ts = eqtable.Store(os.path.join(TMP, "threads.sqlite"))
errors = []


def worker(tid):
    try:
        rr = random.Random(tid)
        for i in range(150):
            key = f"shared{rr.randint(0, 9)}"
            ts.add("post", key, mk(0.5, 10))
            ts.get("post", key)
            ts.add("rng", f"r{tid}-{i % 5}", mk(0.4, 5))
    except Exception as e:           # noqa: BLE001
        errors.append(repr(e))


threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
t0 = time.perf_counter()
[t.start() for t in threads]
[t.join() for t in threads]
tot = sum((ts.get("post", f"shared{i}") or {"samples": 0})["samples"] for i in range(10))
test("스레드 안전: 8 스레드 x 150회 동시 읽기/쓰기, 오류 없음, 합계 표본 보존", not errors and tot == 8 * 150 * 10,
     f"errors={errors[:2]} total={tot} ({(time.perf_counter() - t0) * 1000:.0f}ms)")

# 같은 키를 evaluator.equity 로 여러 스레드가 동시에
hero, board = P(["Ad", "Qd"]), P(["Kc", "7d", "2s"])
res_t = []


def eq_worker():
    res_t.append(ev.equity(hero, board, 2, 10000)["equity"])


threads = [threading.Thread(target=eq_worker) for _ in range(4)]
[t.start() for t in threads]
[t.join() for t in threads]
test("스레드 안전: evaluator.equity 동시 호출 4개 정상", len(res_t) == 4 and all(0 < e < 1 for e in res_t))

# --- evaluator 포스트플랍 캐시 ----------------------------------------------------------
STORE.close()
eqtable.set_store(eqtable.Store(os.path.join(TMP, "post.sqlite")))
hero, board = P(["Ah", "Kd"]), P(["Qs", "Jc", "2h"])
t = time.perf_counter()
a = ev.equity(hero, board, 2, 10000)
t_miss = time.perf_counter() - t
t = time.perf_counter()
b = ev.equity(hero, board, 2, 10000)
t_hit = time.perf_counter() - t
test("캐시 히트: 두 번째 호출은 같은 값 (source=cache)", b["source"] == "cache" and all(a[k] == b[k] for k in ("equity", "win", "tie", "lose", "samples")),
     f"miss {t_miss * 1000:.0f}ms -> hit {t_hit * 1000:.2f}ms")
perm = (2, 3, 0, 1)
h2, b2 = permute(hero, perm), permute(board[::-1], perm)
c = ev.equity(h2, b2, 2, 10000)
test("캐시 히트: 무늬를 바꾸고 보드 순서를 섞은 같은 스팟도 동일 결과", c["source"] == "cache" and c["equity"] == a["equity"])
test("캐시 결과의 키 구성이 기존과 같다 (+source)", set(a) == {"equity", "win", "tie", "lose", "samples", "exact"}
     and set(b) == set(a) | {"source"}, str(sorted(b)))
d = ev.equity(hero, board, 2, 10000, rng=random.Random(5))
e = ev.equity(hero, board, 2, 10000, rng=random.Random(5))
test("고정 시드(rng 지정)는 캐시/표를 우회 -> 예전과 같은 결정적 시뮬레이션", d == e and "source" not in d)
f = ev.equity(hero, board, 2, 3000, use_cache=False)
test("use_cache=False 우회", "source" not in f and f["samples"] == 3000)
# 표본이 모자라면: 새 표본만 반환 + 캐시에 합쳐 쌓임
h3, b3 = P(["9s", "9h"]), P(["4c", "5d", "Tc"])
x1 = ev.equity(h3, b3, 1, 2000, exact_limit=0)
x2 = ev.equity(h3, b3, 1, 2000, exact_limit=0)
raw = eqtable.get_store().get("post", eqtable.post_key(h3, b3, 1))
test("표본 부족 항목: 호출자에겐 새 표본(2000)만, 캐시엔 합쳐서 4000", x1["samples"] == 2000 and x2["samples"] == 2000 and raw["samples"] == 4000, str(raw))
for _ in range(4):
    ev.equity(h3, b3, 1, 2000, exact_limit=0)
x3 = ev.equity(h3, b3, 1, 1500, exact_limit=0)
test("표본이 TRUST(10000) 이상 쌓이면 즉시 반환", x3["source"] == "cache" and x3["samples"] >= 10000, f"samples={x3['samples']}")
# 리버 1:1 은 전수 -> exact 캐시
rv, rb = P(["Ah", "Ad"]), P(["Kc", "7d", "2s", "Jh", "3c"])
y1 = ev.equity(rv, rb, 1)
y2 = ev.equity(rv, rb, 1)
test("리버 1:1 전수 결과(exact)도 캐시", y1["exact"] and y2["source"] == "cache" and y2["exact"] and y2["equity"] == y1["equity"])

# --- rangeq 캐시 ---------------------------------------------------------------------
UTG = rq.villain_range("open", "UTG", 9, 100)
hero, board = P(["Ah", "Qd"]), P(["Kc", "7d", "2s"])
r1 = rq.equity_vs_ranges(hero, board, [UTG], 6000)
r2 = rq.equity_vs_ranges(hero, board, [UTG], 6000)
test("rangeq 캐시 히트: 같은 값", r2["source"] == "cache" and all(r1[k] == r2[k] for k in ("equity", "win", "tie", "lose", "samples")))
r3 = rq.equity_vs_ranges(permute(hero, (3, 2, 1, 0)), permute(board, (3, 2, 1, 0)), [UTG], 6000)
test("rangeq 캐시 히트: 무늬 순열한 같은 스팟", r3.get("source") == "cache" and r3["equity"] == r1["equity"])
r4 = rq.equity_vs_ranges(hero, board, [UTG], 6000, random.Random(3))
test("rangeq: rng 를 주면 캐시 우회(결정적), cache=True 로 강제 가능",
     "source" not in r4 and rq.equity_vs_ranges(hero, board, [UTG], 6000, random.Random(3)) == r4
     and rq.equity_vs_ranges(hero, board, [UTG], 6000, random.Random(3), cache=True).get("source") == "cache")
cr = {"AhKd": 1.0, "QsQh": 0.5, "9c9d": 1.0}
z1 = rq.equity_vs_ranges(hero, board, [cr], 6000)
z2 = rq.equity_vs_ranges(hero, board, [cr], 6000)
test("rangeq 콤보 레인지 캐시", z2.get("source") == "cache" and z2["equity"] == z1["equity"])
w1 = rq.equity_vs_ranges(hero, board, [UTG, cr], 6000)
w2 = rq.equity_vs_ranges(hero, board, [cr, UTG], 6000)
test("rangeq: 상대 순서가 달라도 같은 캐시 항목", w2.get("source") == "cache" and w2["equity"] == w1["equity"])
try:
    rq.equity_vs_ranges(P(["Ah", "Ah"]), [], [UTG])
    test("rangeq: 잘못된 카드는 여전히 ValueError", False)
except ValueError:
    test("rangeq: 잘못된 카드는 여전히 ValueError", True)

# --- 프리플랍 표 ------------------------------------------------------------------------
HAVE = eqtable.table_available()
if not HAVE:
    print("SKIP: eqtable/preflop_random.json 이 아직 없음 (python build_eqtable.py 로 만든 뒤 다시 실행)")
else:
    row = lambda code, k: eqtable.preflop(eqtable.code_cards(code), k)
    aa1, o72, aa9 = row("AA", 1), row("72o", 1), row("AA", 9)
    test("알려진 값: AA vs 1명 ≈ 85.2%", abs(aa1["equity"] - 0.852) < 0.003, f"{aa1['equity']:.4f}")
    test("알려진 값: 72o vs 1명 ≈ 34.6%", abs(o72["equity"] - 0.346) < 0.003, f"{o72['equity']:.4f}")
    test("알려진 값: AA vs 9명 ≈ 31%", abs(aa9["equity"] - 0.31) < 0.006, f"{aa9['equity']:.4f}")
    test("반환 형식: win+tie+lose=1, exact=False, samples>=200k, source=table",
         all(abs(r["win"] + r["tie"] + r["lose"] - 1) < 1e-4 and r["exact"] is False and r["samples"] >= 200000
             and r["source"] == "table" for r in (aa1, o72, aa9)))
    # 실제 카드 -> 코드 매핑
    test("카드 -> 169 코드 매핑", eqtable.hand_code(*P(["Ah", "Kh"])) == "AKs" and eqtable.hand_code(*P(["Kd", "Ac"])) == "AKo"
         and eqtable.hand_code(*P(["7s", "7c"])) == "77" and len(eqtable.all_codes()) == 169 == len(set(eqtable.all_codes())))
    test("같은 클래스(무늬만 다른 AKs)는 같은 값", eqtable.preflop(P(["As", "Ks"]), 3)["equity"] == eqtable.preflop(P(["Ac", "Kc"]), 3)["equity"])
    # 대칭성: 모든 169 핸드를 콤보 수로 가중 평균하면 정확히 1/(k+1)
    ok, worst = True, 0.0
    for k in range(1, 10):
        num = den = 0.0
        for code in eqtable.all_codes():
            nc = 6 if len(code) == 2 else (4 if code[2] == "s" else 12)
            num += nc * eqtable.preflop(eqtable.code_cards(code), k)["equity"]
            den += nc
        err = abs(num / den - 1 / (k + 1))
        worst = max(worst, err)
    test("일관성: 콤보 가중 평균 에쿼티 = 1/(k+1) (k=1..9, 오차 <0.1%)", worst < 0.001, f"최대 오차 {worst:.5f}")
    # 새 시뮬레이션과 비교
    cases = [("AKs", 1), ("72o", 1), ("QQ", 2), ("T9s", 3), ("A5o", 5), ("JJ", 8)]
    diffs = []
    t0 = time.perf_counter()
    for i, (code, k) in enumerate(cases):
        sim = ev.equity(eqtable.code_cards(code), [], k, 300000, exact_limit=0, rng=random.Random(100 + i), use_table=False)
        diffs.append((abs(sim["equity"] - row(code, k)["equity"]), code, k))
    worst = max(diffs)
    test("표 vs 새 시뮬레이션(300k) 일치 (0.3% 이내)", worst[0] < 0.003, f"최대 차이 {worst[0] * 100:.3f}% ({worst[1]} vs {worst[2]}명), {time.perf_counter() - t0:.0f}s")
    # evaluator 후크
    t = time.perf_counter()
    h = ev.equity(P(["Ah", "Ad"]), [], 1)
    t_tab = time.perf_counter() - t
    test("evaluator.equity: 보드 없음 -> 표 즉시 반환, 키 호환", h["source"] == "table" and {"equity", "win", "tie", "lose", "samples", "exact"} <= set(h)
         and h["equity"] == aa1["equity"], f"{t_tab * 1000:.3f}ms")
    t = time.perf_counter()
    nb = ev.equity(P(["Ah", "Ad"]), [], 1, 10000, use_table=False)
    t_sim = time.perf_counter() - t
    test("use_table=False / 고정 시드는 표 우회", "source" not in nb and "source" not in ev.equity(P(["Ah", "Ad"]), [], 1, 500, rng=random.Random(1)),
         f"{t_sim * 1000:.0f}ms")
    test("상대 10명 이상 / 0명은 표 밖 -> 시뮬레이션으로 폴백", ev.equity(P(["Ah", "Ad"]), [], 10, 300)["samples"] == 300)

# --- 속도 (측정만) ---------------------------------------------------------------------
print("\n속도 (ms)")
hero, board = P(["Ah", "Kd"]), P(["Qs", "Jc", "2h"])
for k in (1, 3, 9):
    t = time.perf_counter()
    ev.equity(hero, [], k, 10000, use_table=False)
    t_sim = time.perf_counter() - t
    t = time.perf_counter()
    ev.equity(hero, [], k, 10000)
    t_tab = time.perf_counter() - t
    print(f"  프리플랍 상대 {k}명: 시뮬레이션(10000) {t_sim * 1000:7.1f}  ->  기본 경로 {t_tab * 1000:7.3f}")
for k in (1, 3):
    t = time.perf_counter()
    ev.equity(hero, board, k, 10000, use_cache=False)
    t_u = time.perf_counter() - t
    ev.equity(hero, board, k, 10000)
    t = time.perf_counter()
    ev.equity(hero, board, k, 10000)
    t_c = time.perf_counter() - t
    print(f"  플랍 상대 {k}명: 캐시 없이 {t_u * 1000:7.1f}  ->  캐시 히트 {t_c * 1000:7.3f}")
t = time.perf_counter()
rq.equity_vs_ranges(hero, board, [UTG] * 2, 6000, cache=False)
t_u = time.perf_counter() - t
rq.equity_vs_ranges(hero, board, [UTG] * 2, 6000)
rq.equity_vs_ranges(hero, board, [UTG] * 2, 6000)
t = time.perf_counter()
rq.equity_vs_ranges(hero, board, [UTG] * 2, 6000)
print(f"  레인지(UTG x2, 6000): 캐시 없이 {t_u * 1000:7.1f}  ->  캐시 히트 {(time.perf_counter() - t) * 1000:7.3f}")

# --- 결과 ---------------------------------------------------------------------------
eqtable.set_store(None)
shutil.rmtree(TMP, ignore_errors=True)
failed = 0
for ok, name, info in results:
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info else ""))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
