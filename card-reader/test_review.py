"""python test_review.py — lossreview (덜 잃기): 범위 기준 '나를 이기는 패' 경고, 손해 한도, 핸드 뒤 복기."""
import json
import sys

import evaluator as ev
import lossreview as lr

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
results = []


def test(name, ok, info=""):
    results.append((bool(ok), name, info))


# --- 1) beat_info: 범위 가중 '나를 이기는 패' ------------------------------------------------------
BOARD = ["2h", "5h", "8h", "9h", "3d"]
HERO = ["Jh", "4c"]                                   # J 플러시
info = lr.beat_info(HERO, BOARD, [{"AhQs": 1.0, "7s7d": 1.0}])
test("A♥ 가진 상대 반, 77 반 → 이기는 패 50%", info and abs(info["p"] - 0.5) < 1e-9, info)
g = info["groups"][0]
test("묶음 이름 '더 높은 플러시', 예시 A♥", g["name"] == "더 높은 플러시" and g["ex"] == ["A♥"], g)
txt = lr.beat_text(info)
test("글: '더 높은 플러시 (A♥ 가진 사람) 50%'", "더 높은 플러시 (A♥ 가진 사람) 50%" in txt and txt.startswith("나를 이기는 패 50%"), txt)
test("내 족보 글 'J 플러시'", info["hand"] == "J 플러시", info["hand"])

info = lr.beat_info(["As", "Kd"], ["Ks", "7d", "7c"], [{"KhKc": 1.0, "7h7s": 1.0, "QsQd": 2.0}])
names = {g["name"]: g for g in info["groups"]}
test("KK = 풀하우스, 77 = 포카드, QQ 는 안 이김 (25% + 25%)",
     abs(names["풀하우스"]["pct"] - 0.25) < 1e-9 and abs(names["포카드"]["pct"] - 0.25) < 1e-9 and abs(info["p"] - 0.5) < 1e-9,
     info)
test("풀하우스 예시는 랭크 표기 'KK'", names["풀하우스"]["ex"] == ["KK"], names["풀하우스"])

two = lr.beat_info(HERO, BOARD, [{"AhQs": 1.0, "7s7d": 1.0}, {"KhQd": 1.0, "6c6d": 1.0}])
test("상대 2명 (각 50%) → 적어도 한 명 75%", abs(two["p"] - 0.75) < 1e-9 and two["n"] == 2, two)
test("멀티웨이 글: '상대 2명 중 누군가'", lr.beat_text(two).startswith("상대 2명 중 누군가 나를 이길 패 75%"), lr.beat_text(two))
test("핸드코드 범위('AKs')도 받음", lr.beat_info(HERO, BOARD, [{"AKs": 1.0}])["p"] > 0.2)
test("메이드 패가 아니면 None (A 하이)", lr.beat_info(["Ac", "Qd"], ["2h", "7s", "9d"], [{"KhKs": 1}]) is None)
test("보드 3장 미만이면 None", lr.beat_info(HERO, ["2h", "5h"], [{"AhQs": 1}]) is None)
nut = lr.beat_info(["Ah", "Kh"], BOARD, [{"QsQd": 1.0}])
test("넛 → '거의 없음'", nut["p"] == 0 and "거의 없음" in lr.beat_text(nut), lr.beat_text(nut))
tie = lr.beat_info(["Ac", "Kd"], ["Ah", "Ks", "7d", "7c", "2s"], [{"AsKh": 1.0}])
test("같은 패는 이기는 패가 아니라 비김", tie["p"] == 0 and abs(tie["tie"] - 1.0) < 1e-9, tie)

# --- 2) 손해 한도 / 추천과 맞추기 -------------------------------------------------------------------
for w, pot, call in ((0.4, 10.0, 0.0), (0.3, 20.0, 5.0), (0.45, 7.0, 2.0)):
    a = lr.max_more(w, pot, call)
    ev_a = w * (pot + 2 * a - call) - a
    test(f"max_more 본전 (w={w}, 팟 {pot}, 콜 {call}) → {a:.2f}", abs(ev_a) < 1e-9, ev_a)
test("w >= 50% → 제한 없음", lr.max_more(0.5, 10) is None and lr.max_more(0.8, 10) is None)

i60 = {"p": 0.6, "tie": 0.0, "groups": [], "n": 1, "hand": "J 플러시", "cat": 5}
# 팟 10 (상대 벳 5 포함), 콜 5 → 필요 33%; 이길 확률 40% → 계속, 앞으로 최대 0.4*5/0.2 = 10BB
g = lr.guard_lines(i60, {"pot_total": 10.0, "call": 5.0, "street": "river"}, {"action": "call", "action_ko": "콜"})
test("이길 40% ≥ 필요 33% → '앞으로 최대 10BB까지만'", g["max_bb"] == 10.0 and "앞으로 최대 10BB까지만" in g["lines"][1]
     and not g["fold"], g)
i80 = dict(i60, p=0.8)
g = lr.guard_lines(i80, {"pot_total": 10.0, "call": 5.0, "street": "river"}, None)
test("이길 20% < 필요 33% (추천 없음) → '접는 게 덜 잃어요'", g["fold"] and "접는 게 덜 잃어요" in g["lines"][1], g)
g = lr.guard_lines(i80, {"pot_total": 10.0, "call": 5.0, "street": "river"}, {"action": "fold", "action_ko": "폴드"})
test("추천 폴드 + 계산 폴드 → 같은 말", "접는 게 덜 잃어요" in g["lines"][1], g)
g = lr.guard_lines(i80, {"pot_total": 10.0, "call": 5.0, "street": "turn", "outs": {"count": 9}},
                   {"action": "call", "action_ko": "콜"})
test("턴: 계산은 폴드인데 추천이 콜 → 반대로 말하지 않음 (아웃 9장 · 더 커지면 접기)",
     "접는 게" not in g["lines"][1] and "추천은 콜 (다음 카드 9장으로" in g["lines"][1] and "더 커지면 접기" in g["lines"][1], g)
g = lr.guard_lines(i80, {"pot_total": 10.0, "call": 5.0, "street": "river", "outs": {"count": 9}},
                   {"action": "raise", "action_ko": "레이즈"})
test("리버: 추천 레이즈면 '추천은 레이즈 — 더 커지면 접기' (리버엔 다음 카드 없음)",
     "추천은 레이즈 — 더 커지면 접기" in g["lines"][1] and "접는 게" not in g["lines"][1], g)
i20 = dict(i60, p=0.2)
g = lr.guard_lines(i20, {"pot": 8.0, "call": 0.0, "street": "flop"}, {"action": "bet", "action_ko": "벳"})
test("이길 80% → '끝까지 가도 괜찮아요'", "끝까지" in g["lines"][1], g)
g = lr.guard_lines(i60, {"pot_total": 10.0, "call": 2.0, "street": "flop"}, {"action": "fold", "action_ko": "폴드"})
test("계산은 콜인데 추천이 폴드(ICM 등) → '추천은 폴드'", "추천은 폴드" in g["lines"][1], g)
g = lr.guard_lines(i60, {}, None)
test("팟을 모르면 이기는 패 줄만", len(g["lines"]) == 1, g)
test("guard_lines(None) → None", lr.guard_lines(None, {}, None) is None)


# --- 3) 복기 ---------------------------------------------------------------------------------------
def mk(actions, hero, board, seats, shown=None, issues=(), conf="ok"):
    st = {k: {"hero": k == "hero", "pos": p, "folded": None, "shown": (shown or {}).get(k)} for k, p in seats.items()}
    for street, acts in actions.items():
        for a in acts:
            if a["act"] == "fold":
                st[a["seat"]]["folded"] = street
    return {"id": "t-1", "hero_cards": hero, "board": board, "actions": actions, "seats": st, "streets": "river",
            "showdown": {"seats": [k for k, s in st.items() if not s["folded"]], "shown": shown or {}},
            "issues": [{"code": c} for c in issues], "confidence": conf, "players": 3}


def A(seat, act, to=None, allin=False):
    a = {"seat": seat, "act": act}
    if to is not None:
        a["to"] = to
    if allin:
        a["allin"] = True
    return a


ACTS = {"preflop": [A("seat_S", "post", 0.5), A("seat_B", "post", 1.0), A("hero", "raise", 2.5), A("seat_S", "fold"),
                    A("seat_B", "call", 2.5)],
        "flop": [A("seat_B", "check"), A("hero", "bet", 3.0), A("seat_B", "call", 3.0)],
        "turn": [A("seat_B", "check"), A("hero", "check")],
        "river": [A("seat_B", "bet", 12.0), A("hero", "call", 12.0)]}
SEATS = {"seat_S": "SB", "seat_B": "BB", "hero": "BTN"}
rec = mk(ACTS, HERO, BOARD, SEATS, shown={"seat_B": ["Ah", "Qs"]})
rv = lr.review_hand(rec)
test("쇼다운 패배: 손익 = -내가 낸 17.5BB", rv["result_bb"] == -17.5 and rv["result_src"] == "showdown", rv)
test("첫 줄: 상대 A♥Q♠ A 플러시 — 내 J 플러시보다 높음",
     rv["lines"][0] == "상대(BB) A♥Q♠ A 플러시 — 내 J 플러시보다 높음", rv["lines"])
lt = rv["lost_to"]
test("기록: 이긴 패가 정말 내 패보다 높음", ev.evaluate([ev.parse_card(c) for c in lt["cards"] + BOARD])
     > ev.evaluate([ev.parse_card(c) for c in HERO + BOARD]), lt)
test("둘째 줄: 잃은 칩 대부분은 리버 콜 12BB + 그 벳 범위 판단", rv["lines"][1].startswith("잃은 칩 대부분은 리버 콜 12BB")
     and ("덜 잃음" in rv["lines"][1] or "맞는 콜" in rv["lines"][1]), rv["lines"])
test("이기는 패에 갚아 준 돈 = 리버 콜 12 (플랍 벳은 내 패가 메이드 패가 아니라 제외)", rv["paid_beaters_bb"] == 12.0, rv)
test("줄은 최대 3개", len(rv["lines"]) <= 3, rv["lines"])

win = lr.review_hand(mk(ACTS, HERO, BOARD, SEATS, shown={"seat_B": ["Qc", "Qs"]}))
test("내가 이긴 쇼다운 → 줄 없음, 손익 +", win["result_bb"] > 0 and not win["lines"] and "lost_to" not in win, win)

low = lr.review_hand(mk(ACTS, HERO, BOARD, SEATS, shown={"seat_B": ["Ah", "Qs"]}, issues=("mid_hand_start",), conf="low"))
test("기록이 일부 빠짐 → 카드 줄만, 금액 줄 없음", len(low["lines"]) == 1 and low["lines"][0].startswith("상대(BB)")
     and "biggest" not in low, low)

# 같은 사람이 그 사이 다른 벳/레이즈 없이 또 올림 = 올라가는 칩을 다시 읽음 → 한 액션 (166.4 → 243 올인)
re_acts = {"preflop": ACTS["preflop"], "flop": [A("seat_B", "check"), A("hero", "check")],
           "turn": [A("seat_B", "check"), A("hero", "check")],
           "river": [A("seat_B", "bet", 5.0), A("hero", "raise", 30.0, True), A("hero", "raise", 60.0, True),
                     A("seat_B", "call", 60.0)]}
rows, total, pot = lr.timeline(mk(re_acts, HERO, BOARD, SEATS))
riv = [r for r in rows if r["street"] == "river" and r["seat"] == "hero"]
test("재읽은 올인은 한 줄 (to 60, added 60)", len(riv) == 1 and riv[0]["to"] == 60.0 and riv[0]["added"] == 60.0, riv)
test("합계: hero 62.5", total["hero"] == 62.5, total)

# 폴드 표: 노페어는 큰 벳에 잘 접고, 투페어 이상은 거의 안 접음
f_air, _ = lr.fold_split({"Kc4d": 1.0}, ["2h", "7s", "9d", "Jc", "3s"], 1)
f_nut, _ = lr.fold_split({"9c9s": 1.0}, ["2h", "7s", "9d", "Jc", "3s"], 1)
f_raise, _ = lr.fold_split({"Kc4d": 1.0}, ["2h", "7s", "9d", "Jc", "3s"], 1, raise_=True)
test("폴드 표: 노페어 88% / 셋 4% / 레이즈면 덜 접음", abs(f_air - 0.88) < 1e-9 and abs(f_nut - 0.04) < 1e-9
     and f_raise < f_air, (f_air, f_nut, f_raise))

# 폴드 에쿼티 줄: 상대 카드가 그 벳에 접지 않았을 패(셋)면 말하지 않는다
air_acts = {"preflop": [A("seat_S", "post", 0.5), A("seat_B", "post", 1.0), A("hero", "raise", 3.0), A("seat_S", "fold"),
                        A("seat_B", "call", 3.0)],
            "flop": [A("seat_B", "check"), A("hero", "check")],
            "turn": [A("seat_B", "check"), A("hero", "check")],
            "river": [A("seat_B", "bet", 6.0), A("hero", "fold")]}
AIR_HERO, AIR_BOARD = ["Kc", "Qd"], ["2h", "7s", "9d", "4c", "3s"]
strong = lr.review_hand(mk(air_acts, AIR_HERO, AIR_BOARD, SEATS, shown={"seat_B": ["9c", "9s"]}))
test("상대가 셋(보임) → 폴드 에쿼티 줄 없음", "miss" not in strong and not any("접을 확률" in x for x in strong["lines"]), strong)
# 큰 팟에서 노페어로 체크-체크 → 리버 벳에 폴드: 플랍 팟 크기 벳이면 3벳 범위가 꽤 접는다 (모델 추정 줄)
fe_acts = {"preflop": [A("seat_S", "post", 0.5), A("seat_B", "post", 1.0), A("hero", "raise", 12.0), A("seat_S", "fold"),
                       A("seat_B", "call", 12.0)],
           "flop": [A("seat_B", "check"), A("hero", "check")],
           "turn": [A("seat_B", "check"), A("hero", "check")],
           "river": [A("seat_B", "bet", 24.0), A("hero", "fold")]}
fe = lr.review_hand(mk(fe_acts, ["Jc", "Td"], ["Kh", "8d", "3s", "2c", "6h"], SEATS))
line = next((x for x in fe["lines"] if "접을 확률" in x), "")
test("폴드 에쿼티 줄: '플랍에 팟 크기 벳 했으면 상대가 접을 확률 약 75% → 평균 +…BB (추정)'",
     line.startswith("플랍에 팟 크기 벳 했으면 상대가 접을 확률 약 ") and line.endswith("(추정)")
     and fe["miss"]["ev_bb"] >= lr.MISS_MIN_BB and fe["miss"]["fold_pct"] >= 100 * lr.MISS_MIN_FOLD, fe)
test("스트리트·크기별 계산은 기록 (fe_spots)", len(fe.get("fe_spots") or []) == 4, fe.get("fe_spots"))
same = lr.review_hand(mk(fe_acts, ["Jc", "Td"], ["Kh", "8d", "3s", "2c", "6h"], SEATS, shown={"seat_B": ["Kc", "Qc"]}))
test("같은 판인데 상대가 K 탑페어(보임) → 그 벳에 안 접었을 패라 줄 없음", "miss" not in same, same.get("lines"))

# 닉네임은 어디에도 쓰지 않는다
named = mk(ACTS, HERO, BOARD, SEATS, shown={"seat_B": ["Ah", "Qs"]})
named["seats"]["seat_B"]["name"] = "테스트닉네임"
fields = lr.log_fields(lr.review_hand(named))
test("기록용 필드에 닉네임 없음", "테스트닉네임" not in json.dumps(fields, ensure_ascii=False) and fields["lines"], fields)
test("기록용 필드는 JSON 으로 저장 가능", json.loads(json.dumps(fields, ensure_ascii=False)) == fields)

# 깨진 기록도 예외 없이
for bad in (None, {}, {"seats": {"hero": {"hero": True}}}, {"hero_cards": ["Ah"], "seats": {}}, {"seats": "x"}):
    out = lr.review_hand(bad)
    test(f"깨진 기록 {str(bad)[:30]} → 빈 줄", isinstance(out, dict) and out.get("lines") == [], out)

# 결정적: 같은 입력 → 같은 결과 (고정 시드)
test("같은 입력 → 같은 복기", lr.review_hand(rec) == lr.review_hand(rec))

failed = sum(1 for ok, *_ in results if not ok)
for ok, name, info in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  — {info}"))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
