"""python test_beaters.py — evaluator.beaters: 지금 내 패를 이기는 상대 두 장 묶음."""
import evaluator as ev

P = lambda xs: [ev.parse_card(x) for x in xs]
results = []


def test(name, ok, info=""):
    results.append((ok, name, info))


def groups(hero, board):
    r = ev.beaters(P(hero), P(board))
    return r, {(g["name"], g["label"]): g["combos"] for g in r["groups"]}


r, g = groups(["Kd", "7h"], ["Qc", "Kh", "6d"])
test("K7 / QK6: 전체 1081 조합", r["total"] == 1081, r["total"])
test("셋 66·QQ 각 3조합, KK 1조합 (K 두 장이 보임)", g.get(("셋", "66")) == 3 and g.get(("셋", "QQ")) == 3 and g.get(("셋", "KK")) == 1, g)
test("투페어 Q6 9조합", g.get(("투페어", "Q6")) == 9, g)
test("같은 원페어는 '(더 높음)' 으로 (AK)", any(n == "원페어 (더 높음)" and l == "AK" for n, l in g), g)
test("약한 K (K5) 는 지는 패가 아님", not any(l == "K5" for _, l in g), g)
r, g = groups(["Ac", "Jd"], ["3h", "Tc", "9d"])
test("셋은 '한 장' 이 아니라 두 장 (33)", g.get(("셋", "33")) == 3 and not any(n == "트립스" for n, _ in g), g)
r, g = groups(["Qs", "Qd"], ["Jh", "8h", "4h", "2c", "7h"])
test("4장 플러시 보드: ♥ 한 장 플러시", g.get(("플러시", "♥ 한 장"), 0) > 300, g)
test("스트레이트 플러시 ♥♥ 2조합 (5♥6♥, T♥9♥)", g.get(("스트레이트 플러시", "♥♥")) == 2, g)
r, g = groups(["Ah", "Ad"], ["As", "Kd", "Kc", "Ac", "2h"])
test("A 포카드: 지는 패 없음 (넛)", r["count"] == 0 and not g, (r["count"], g))
order = [x["cat"] for x in ev.beaters(P(["9s", "9d"]), P(["9h", "Th", "Jc", "2h"]))["groups"]]
test("센 족보 먼저 정렬", order == sorted(order, reverse=True), order)

failed = sum(1 for ok, *_ in results if not ok)
for ok, name, info in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  — {info}"))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
