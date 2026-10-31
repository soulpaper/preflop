"""포스트플랍 상대 포지션 · 3명 팟 참고 줄 회귀 테스트 — python test_postflop_ctx.py
실제 함수(App.postflop_ctx, summarize_hand, advisor.recommend, solverlib.gto_advice)를 쓴다. 창은 열지 않는다.
GTO_LIB_DIR 에 풀린 라이브러리가 있으면 실제 조회도 확인 (없으면 SKIP)."""
import os
import random
import sys
from collections import Counter
from contextlib import contextmanager
from types import SimpleNamespace

import advisor
import decisionlog
import handlog
import reader
import solverlib

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

results = []
REF = "3명 → 1명 근사"
HERO = ["Ah", "Kd"]
BOARD = ["Kh", "7d", "2c"]


def test(name, ok, info=""):
    results.append(("PASS" if ok else "FAIL", name, info))


def skip(name, info=""):
    results.append(("SKIP", name, info))


def seat(key, pos, in_hand=True, bet=None, bubble=None, stack=None):
    return {"key": key, "slot": key.replace("seat_", ""), "pos": pos, "occupied": True, "dealt": True, "in_hand": in_hand,
            "folded": not in_hand, "allin": False, "bet": bet, "stack": stack, "bubble": bubble}


def table(*opps, hero_pos="BB", players=6):
    return {"hero_pos": hero_pos, "pos_players": players, "seats": list(opps) + [seat("hero", hero_pos)]}


def hist_of(hero=HERO, street=1, reliable=True, pos=None, live=None, pre_raises=1, opener="seat_TC", last=None):
    return {"hero_cards": tuple(hero), "street": street, "reliable": reliable, "pre_raises": pre_raises, "pre_seen": True,
            "opener": opener, "flop": [], "opp_raise": False, "last_aggr": dict({"preflop": opener}, **(last or {})),
            "pos": pos or {}, "live": live if live is not None else ["hero"]}


def app_stub(hist=None, memo=None, pre_memo=None, ocr=None):
    """App.postflop_ctx 가 쓰는 것만 가진 대역 (저장된 프리플랍 선택값 cfg 는 일부러 지난 핸드 값)."""
    return SimpleNamespace(track={"hist": hist}, pre_auto_memo=memo, pre_memo=pre_memo,
                           cfg={"preflop": {"villain": "UTG", "hero": "CO", "situation": "vs3bet"}},
                           ocr_opp_bb=lambda: dict(ocr or {}))


def pctx(ts, opponents, hist=None, memo=None, board=BOARD, ocr=None, hero=HERO):
    return reader.App.postflop_ctx(app_stub(hist, memo, ocr=ocr), ts, hero, len(board), opponents, 6.0)


# --- 작업 1: 헤즈업 상대 포지션 ----------------------------------------------------------------
ts_hu = table(seat("seat_TC", "BTN"), seat("seat_UL", "CO", in_hand=False))
test("헤즈업: 테이블 상태의 남은 상대 포지션", pctx(ts_hu, 1)["villain_pos"] == "BTN")

stale_hist = hist_of(hero=["2c", "3d"], pos={"seat_TC": "UTG"}, live=["hero", "seat_TC"])
stale_memo = (("2c", "3d"), {"villain": "UTG", "raises": 3, "hero": "SB"})
ts_nopos = table(seat("seat_TC", None), seat("seat_UL", None, in_hand=False))
out = pctx(ts_nopos, 1, stale_hist, stale_memo)
test("지난 핸드 값 금지: 포지션 모름 + 지난 핸드 추적기/프리플랍 기억/저장 선택값 → None",
     out["villain_pos"] is None and out.get("pre_raises") is None and out["situation"] is None, str(out))
out = pctx(dict(ts_nopos, hero_pos=None), 1, stale_hist, stale_memo)
test("지난 핸드 값 금지: 내 포지션도 지난 값을 쓰지 않음", out["hero_pos"] is None, str(out))
out = pctx(ts_hu, 1, stale_hist, stale_memo)
test("지난 핸드 추적기가 있어도 이번 테이블 상태 값 (UTG 아님)", out["villain_pos"] == "BTN" and out.get("pre_raises") is None, str(out))
same_cards_old_street = hist_of(street=0, pos={"seat_TC": "UTG"}, live=["hero", "seat_TC"])
test("같은 카드라도 다른 스트리트 요약은 안 씀", pctx(ts_nopos, 1, same_cards_old_street)["villain_pos"] is None)

hist_ok = hist_of(pos={"seat_TC": "BTN", "hero": "BB"}, live=["hero", "seat_TC"])
test("테이블 상태에 포지션이 없으면 이번 핸드 추적기", pctx(ts_nopos, 1, hist_ok)["villain_pos"] == "BTN")
out = pctx(dict(ts_nopos, hero_pos=None), 1, hist_ok)
test("내 포지션도 이번 핸드 추적기에서", out["hero_pos"] == "BB" and out["villain_pos"] == "BTN", str(out))
test("테이블 상태와 추적기 포지션이 다르면 None", pctx(ts_hu, 1, hist_of(pos={"seat_TC": "CO"}, live=["hero", "seat_TC"]))["villain_pos"] is None)
test("내 포지션과 같은 값이면 None", pctx(table(seat("seat_TC", "BB")), 1)["villain_pos"] is None)
test("상대 수가 1이 아니면 None", pctx(ts_hu, 2)["villain_pos"] is None)

# 10-30 기록 20261030_020619-0040: 추적기는 UTG 폴드로 적었지만 그 UTG 가 174BB 벳을 함 — 추적기로 상대 수를 줄이면 3명 팟을 헤즈업으로 착각
ts_two = table(seat("seat_BR", "UTG"), seat("seat_UL", "BTN"), hero_pos="MP")
false_fold = hist_of(pos={"seat_BR": "UTG", "seat_UL": "BTN", "hero": "MP"}, live=["hero", "seat_UL"])
test("추적기 폴드만으로 헤즈업이 되지 않음 (거짓 폴드)", pctx(ts_two, 2, false_fold)["villain_pos"] is None)
test("믿을 만한 추적기가 다른 상대가 살아 있다고 하면 None",
     pctx(ts_hu, 1, hist_of(pos={"seat_TC": "BTN"}, live=["hero", "seat_TC", "seat_R"]))["villain_pos"] is None)
test("추적기를 못 믿으면 (FATAL 이슈) 살아 있는 좌석 정보는 무시",
     pctx(ts_hu, 1, hist_of(reliable=False, pos={"seat_TC": "BTN"}, live=["hero", "seat_R"]))["villain_pos"] == "BTN")
ts_none = table(seat("seat_TC", "BTN", in_hand=False))
test("테이블 상태가 아무도 못 찾으면 추적기의 남은 한 명",
     pctx(ts_none, 1, hist_of(pos={"seat_TC": "BTN", "hero": "BB"}, live=["hero", "seat_TC"]))["villain_pos"] == "BTN")
test("둘 다 못 찾으면 None (10-30 기록 0035/3)", pctx(ts_none, 1, hist_of(pos={"seat_TC": "BTN"}, live=["hero"]))["villain_pos"] is None)


def tracked_hand(pre, hero=HERO):
    h = handlog._Hand("t-1", "t", 0.0)
    h.hero_cards, h.street = list(hero), 1
    h.actions["preflop"] = [dict(a) for a in pre]
    for k, p in (("seat_TC", "BTN"), ("seat_UL", "SB"), ("hero", "BB")):
        s = h.seats[k] = handlog._Seat(k)
        s.pos = Counter({p: 5, "CO": 1})
    h.seats["seat_UL"].folded = True
    return h


S = reader.summarize_hand(tracked_hand([
    {"seat": "seat_TC", "act": "raise", "to": 3.9}, {"seat": "hero", "act": "call", "to": 1.0},
    {"seat": "seat_TC", "act": "raise", "to": 4.0}, {"seat": "hero", "act": "call", "to": 4.0}]))
test("칩 글자를 고쳐 읽은 같은 좌석 레이즈 (3.9→4) 는 3벳이 아님 (10-30 기록 0030)", S["pre_raises"] == 1 and S["opener"] == "seat_TC", str(S))
out = reader.App.postflop_ctx(app_stub(dict(S, street=1)), ts_hu, HERO, 3, 1, 6.0)
test("  → situation 이 vs3bet 으로 안 붙음", out["situation"] == "vsopen" and out["pre_raises"] == 1, str(out))
S = reader.summarize_hand(tracked_hand([
    {"seat": "seat_TC", "act": "raise", "to": 3.0}, {"seat": "hero", "act": "raise", "to": 9.0},
    {"seat": "seat_TC", "act": "call", "to": 9.0}]))
test("진짜 3벳은 그대로 2단계", S["pre_raises"] == 2 and S["pos"]["seat_TC"] == "BTN" and "seat_UL" not in S["live"], str(S))

# 입력칸 pot 이 지난 스트리트 값일 때 (10-30 기록 0017/5: pot 7.5, 화면 POT 11.5): 벳이 없으면 화면 POT 기준
seen = {}
real_lookup = solverlib.lookup


def spy_lookup(*a, **k):
    seen.update(k)
    return None


solverlib.lookup = spy_lookup
base = {"hero": HERO, "board": BOARD, "street": "flop", "players": 6, "opponents_in_hand": 1, "hero_pos": "BB",
        "villain_pos": "BTN", "stack_bb": 40, "call": 0.0, "can_check": True, "to_act": True}
solverlib.gto_advice(dict(base, pot=4.0, pot_total=8.0))
test("gto_advice: 벳 없으면 화면 POT 로 SPR", abs((seen.get("spr") or 0) - 5.0) < 1e-6, str(seen.get("spr")))
seen.clear()
solverlib.gto_advice(dict(base, pot=8.0, pot_total=None))
test("gto_advice: 화면 POT 가 없으면 pot", abs((seen.get("spr") or 0) - 5.0) < 1e-6, str(seen.get("spr")))
solverlib.lookup = real_lookup

# ICM: 숫자 영역 키(opp1)와 테이블 상태 좌석 키(seat_BL)
fake = SimpleNamespace(cfg={"auto_numbers": True, "layout": {"seats": [{"key": "opp1", "slot": "BL"}, {"key": "opp2", "slot": "UL"},
                                                                       {"key": "hero", "slot": "HERO"}]}},
                       ocr_values={"opp1": (40.0, "BB"), "pot_total": (9.0, "BB")}, bb_value=lambda v, u: v)
bb = reader.App.ocr_opp_bb(fake)
test("ICM: 화면 스택 읽기를 좌석 키로도 찾음", bb.get("opp1") == 40.0 and bb.get("seat_BL") == 40.0 and "seat_UL" not in bb, str(bb))
ts_icm = {"seats": [seat("seat_BL", "BTN"), seat("hero", "BB")]}
test("ICM: table_stacks_from 이 상대 스택을 받음", reader.table_stacks_from(ts_icm, bb, 33.0) == {"seat_BL": 40.0, "hero": 33.0})
test("ICM: villain_stack_from 이 상대 스택을 받음", reader.villain_stack_from(ts_icm, "seat_BL", bb) == 40.0)


# --- 작업 2: 주 상대 고르기 --------------------------------------------------------------------
def three(ts, opponents=2, hist=None, memo=None, nboard=3, hero_pos="BB", ocr=None):
    return reader.three_way_ref(ts, hist, memo, nboard, opponents, hero_pos, ocr)


t3 = table(seat("seat_TC", "BTN", bet=4.0), seat("seat_UL", "SB"))
test("주 상대: 벳한 사람", three(t3)["ref3_villain_pos"] == "BTN")
t3c = table(seat("seat_TC", "BTN", bet=4.0, bubble="call"), seat("seat_UL", "SB", bet=4.0, bubble="bet"))
r3 = three(t3c)
test("주 상대: 같은 금액이면 콜 말풍선이 아닌 사람, 다른 상대 칩은 따로", r3["ref3_villain_pos"] == "SB" and r3["ref3_other_bet"] == 4.0, str(r3))
t3n = table(seat("seat_TC", "BTN"), seat("seat_UL", "SB"))
test("주 상대: 벳 없으면 마지막 공격자", three(t3n, hist=hist_of(opener="seat_UL"))["ref3_villain_pos"] == "SB")
test("주 상대: 추적기를 못 믿으면 프리플랍 기억", three(t3n, hist=hist_of(reliable=False), memo={"villain": "BTN"})["ref3_villain_pos"] == "BTN")
test("주 상대: 내가 레이저면 표에 있는 블라인드",
     three(table(seat("seat_TC", "BTN"), seat("seat_UL", "BB"), hero_pos="CO"), hist=hist_of(opener="hero"), hero_pos="CO")["ref3_villain_pos"] == "BB")
test("주 상대 모름 (림프 팟, 벳 없음) → 없음", three(t3n, hist=hist_of(opener=None, pre_raises=0)) == {})
test("주 상대 포지션 모름 → 없음", three(table(seat("seat_TC", None, bet=4.0), seat("seat_UL", "SB"))) == {})
test("상대 3명 → 없음", three(table(seat("seat_TC", "BTN", bet=4.0), seat("seat_UL", "SB"), seat("seat_R", "CO")), opponents=3) == {})
test("좌석 수와 상대 수가 다르면 없음", three(table(seat("seat_TC", "BTN", bet=4.0)), opponents=2) == {})
test("리버는 없음", three(t3, nboard=5) == {})
test("추적기가 3명 이상 살아 있다고 하면 없음", three(t3, hist=hist_of(live=["hero", "seat_TC", "seat_UL", "seat_R"])) == {})
test("상대 스택 (좌석 키 화면 읽기) + 벳", three(t3, ocr={"seat_TC": 30.0})["ref3_villain_stack"] == 34.0)
out = pctx(t3, 2, hist_of(pos={"seat_TC": "BTN", "seat_UL": "SB"}, live=["hero", "seat_TC", "seat_UL"]))
test("postflop_ctx: 3명 팟은 villain_pos 없음 + ref3_villain_pos", out["villain_pos"] is None and out["ref3_villain_pos"] == "BTN", str(out))


# --- 작업 2: 참고 줄 --------------------------------------------------------------------------
def fake_gto(actions, conf="high", calls=None):
    def fn(ctx):
        if calls is not None:
            calls.append(ctx)
        return {"action": "벳", "size_bb": 2.0, "size_text": "팟의 33%", "reason": "GTO: x", "detail": ["솔버"], "confidence": conf,
                "color_key": "raise", "plan": [], "source": "gto",
                "gto": {"actions": actions, "exact_flop": True, "exact_spot": True, "combo_exact": True, "distance": 0.0,
                        "stack_used": 40, "behind": 37.8, "mapped_flop": "Ks7h2d", "notes": [], "stale": False, "street": "flop"}}
    return fn


@contextmanager
def gto_on(fn):
    saved = advisor.GTO_ENABLED, solverlib.gto_advice
    advisor.GTO_ENABLED, solverlib.gto_advice = True, fn
    advisor.gto_cache_clear()
    try:
        yield
    finally:
        advisor.GTO_ENABLED, solverlib.gto_advice = saved
        advisor.gto_cache_clear()


def rule_only(ctx):
    saved = advisor.GTO_ENABLED
    advisor.GTO_ENABLED = False
    try:
        return advisor.recommend(ctx)
    finally:
        advisor.GTO_ENABLED = saved


def has_line(r):
    return any(REF in d for d in r["detail"])


def same_decision(a, b):
    return all(a[k] == b[k] for k in ("action", "size_bb", "size_text", "color_key", "reason", "plan"))


# 탑페어 좋은 키커 (강한 메이드), 스택 33BB, BB 가 BTN 레이즈에 콜 + SB 콜
STRONG = dict(hero=["Ah", "Kd"], board=["Kh", "7d", "2c"], street="flop", players=6, hero_pos="BB", opponents_in_hand=2,
              stack_bb=33, my_stack=33, pot=9.0, pot_total=9.0, call=0, can_check=True, to_act=True, equity=0.7, hero_ip=False,
              aggressor=False, situation="vsopen", pre_raises=1, ref3_villain_pos="BTN", ref3_other_bet=0.0)
AIR = dict(STRONG, hero=["9c", "3d"], equity=0.2)
BET_HEAVY = {"bet33": 0.7, "check": 0.3}
CHECK_HEAVY = {"bet33": 0.2, "check": 0.8}

calls = []
with gto_on(fake_gto(BET_HEAVY, calls=calls)):
    r = advisor.recommend(STRONG)
base_r = rule_only(STRONG)
line = next((d for d in r["detail"] if REF in d), "")
test("강한 패: 참고 줄 표시 (라벨 + 주 상대 + 빈도)", "GTO 참고(3명 → 1명 근사): vs BTN" in line and "벳 33% 70%" in line and "체크 30%" in line, line)
test("강한 패: 액션·크기·색·이유는 규칙 기반 그대로", same_decision(r, base_r), f"{r['action']} vs {base_r['action']}")
test("신뢰도 low", r["confidence"] == "low", r["confidence"])
test("detail 3줄 제한", len(r["detail"]) <= 3, str(len(r["detail"])))
test("조회는 주 상대와 헤즈업으로", calls and calls[0]["opponents_in_hand"] == 1 and calls[0]["villain_pos"] == "BTN" and calls[0]["stack_bb"] == 33)
test("참고 줄 때문에 GTO 주 추천이 되지 않음", not r["reason"].startswith("GTO") and not any("규칙 기반 판단" in d for d in r["detail"]))
test("결정 기록의 gto_mode = ref3", decisionlog._advice_summary(r)["gto_mode"] == "ref3")

with gto_on(fake_gto(BET_HEAVY)):
    r = advisor.recommend(AIR)
test("블러프 금지: 약한 패 + 표가 벳 쪽 → 줄 없음", not has_line(r) and same_decision(r, rule_only(AIR)), str(r["detail"]))
test("블러프 금지: 줄이 없으면 규칙 기반 신뢰도 그대로", r["confidence"] == rule_only(AIR)["confidence"])
with gto_on(fake_gto(CHECK_HEAVY)):
    r = advisor.recommend(AIR)
line = next((d for d in r["detail"] if REF in d), "")
test("약한 패 + 표가 체크 쪽 → 체크만 표시, 벳 빈도는 뺌", "체크 80%" in line and "벳" not in line.split(REF)[1], line)
with gto_on(fake_gto({"raise": 0.6, "call": 0.2, "fold": 0.2})):
    r = advisor.recommend(dict(AIR, can_check=False, call=3.0, pot_total=12.0))
test("약한 패 + 표가 레이즈 쪽 → 줄 없음", not has_line(r), str(r["detail"]))
with gto_on(fake_gto({"raise": 0.1, "call": 0.3, "fold": 0.6})):
    r = advisor.recommend(dict(AIR, can_check=False, call=3.0, pot_total=12.0))
line = next((d for d in r["detail"] if REF in d), "")
test("약한 패 + 표가 폴드/콜 쪽 → 수동 방향만", "폴드 60%" in line and "콜 30%" in line and "레이즈" not in line, line)
MEDIUM = dict(STRONG, hero=["7h", "5s"], equity=0.45)   # 세컨드 페어 = 강한 메이드 아님
with gto_on(fake_gto(BET_HEAVY)):
    r = advisor.recommend(MEDIUM)
test("강한 메이드가 아닌 페어도 공격 조언 없음", not has_line(r), str(r["detail"]))

for name, ctx in (("상대 3명", dict(STRONG, opponents_in_hand=3)),
                  ("3벳 팟 (pre_raises 2)", dict(STRONG, pre_raises=2)),
                  ("3벳 팟 (situation)", dict(STRONG, situation="vs3bet")),
                  ("림프 팟", dict(STRONG, pre_raises=0)),
                  ("팟 종류 모름", dict(STRONG, pre_raises=None)),
                  ("스택 초과 (61BB)", dict(STRONG, stack_bb=61, my_stack=61)),
                  ("스택 초과 (250BB)", dict(STRONG, stack_bb=250, my_stack=250)),
                  ("주 상대 포지션 모름", dict(STRONG, ref3_villain_pos=None)),
                  ("내 차례 아님", dict(STRONG, to_act=False)),
                  ("올인 받음", dict(STRONG, facing_allin=True, can_check=False, call=20.0, pot_total=29.0)),
                  ("리버", dict(STRONG, board=["Kh", "7d", "2c", "5s", "9h"], street="river"))):
    calls = []
    with gto_on(fake_gto(BET_HEAVY, calls=calls)):
        r = advisor.recommend(ctx)
    test(f"줄 없음: {name}", not has_line(r) and not calls and same_decision(r, rule_only(ctx)), str(r["detail"]))

calls = []
with gto_on(fake_gto(BET_HEAVY, calls=calls)):
    r = advisor.recommend(dict(STRONG, stack_bb=200, my_stack=200, ref3_villain_stack=45.0))
test("내가 깊어도 주 상대가 짧으면 그 스택으로", has_line(r) and calls and calls[0]["stack_bb"] == 45.0)
calls = []
with gto_on(fake_gto(BET_HEAVY, calls=calls)):
    r = advisor.recommend(dict(STRONG, can_check=False, call=4.0, pot=9.0, pot_total=17.0, ref3_other_bet=4.0))
test("먼저 콜한 상대 칩은 벳 크기 계산에서 뺌", calls and calls[0]["pot_total"] == 13.0, str(calls and calls[0].get("pot_total")))
calls = []
with gto_on(lambda ctx: None):
    r = advisor.recommend(STRONG)
test("표에 없으면 줄 없음 + 규칙 기반 그대로", not has_line(r) and r["confidence"] == base_r["confidence"] and same_decision(r, base_r))


def boom(ctx):
    raise RuntimeError("x")


with gto_on(boom):
    r = advisor.recommend(STRONG)
test("조회 실패해도 규칙 기반 그대로", not has_line(r) and same_decision(r, base_r))

HU = dict(STRONG, opponents_in_hand=1, villain_pos="BTN", ref3_villain_pos=None)
with gto_on(fake_gto(CHECK_HEAVY)):
    r = advisor.recommend(HU)
test("헤즈업 동작 그대로: 주 추천이 GTO", r["reason"].startswith("GTO"), r["reason"])
calls = []
with gto_on(fake_gto(BET_HEAVY, calls=calls)):
    advisor.recommend(dict(STRONG, opponents_in_hand=2, ref3_villain_pos=None))
test("상대 2명 + 참고 값 없음: 조회 안 함", not calls)

# 성질: 어떤 3명 팟 ctx 든 액션·크기·색이 그대로이고 줄은 3줄 안, 약한 패에 공격 빈도 없음
rng = random.Random(7)
deck = [r + s for r in "23456789TJQKA" for s in "cdhs"]
bad, shown = [], 0
tables = [BET_HEAVY, CHECK_HEAVY, {"check": 1.0}, {"allin": 0.9, "fold": 0.1}, {"raise": 0.4, "call": 0.5, "fold": 0.1}]
for i in range(1500):
    cards = rng.sample(deck, 6)
    nb = rng.choice((3, 4))
    facing = rng.random() < 0.5
    call = round(rng.uniform(1, 12), 1) if facing else 0.0
    ctx = dict(STRONG, hero=cards[:2], board=cards[2:2 + nb], street={3: "flop", 4: "turn"}[nb], equity=rng.random(),
               stack_bb=rng.uniform(5, 60), my_stack=rng.uniform(5, 60), pot=rng.uniform(4, 40), call=call, can_check=not facing,
               hero_pos=rng.choice(("BB", "SB", "BTN", "CO")), ref3_villain_pos=rng.choice(("BTN", "BB", "UTG")),
               looseness=rng.choice((0.0, 0.2, -0.3)))
    ctx["pot_total"] = ctx["pot"] + call
    plain = rule_only(ctx)
    with gto_on(fake_gto(rng.choice(tables))):
        got = advisor.recommend(ctx)
    keep = all(got[k] == plain[k] for k in ("action", "size_bb", "size_text", "color_key"))
    line = next((d for d in got["detail"] if REF in d), None)
    if line:
        shown += 1
        norm = advisor._Ctx(ctx)
        strong = advisor._classify(norm.hero, norm.board)[0] in ("monster", "strong")
        tail = line.split(REF)[1]
        if got["confidence"] != "low" or (not strong and any(w in tail for w in ("벳", "레이즈", "올인"))):
            keep = False
    if not keep or len(got["detail"]) > 3:
        bad.append((ctx, got))
test(f"무작위 3명 팟 1500개: 액션·크기·색 불변, 블러프 줄 없음 (줄 표시 {shown}개)", not bad and shown > 50, str(bad[:1]))

# --- 실제 라이브러리 (있을 때만) -----------------------------------------------------------------
if solverlib.available():
    advisor.GTO_ENABLED = True
    advisor.gto_cache_clear()
    ctx = dict(HU, pot=6.0, pot_total=6.0)
    ctx.update({k: v for k, v in pctx(ts_hu, 1).items() if k in ("villain_pos", "hero_ip")})
    adv = solverlib.gto_advice(ctx)
    test("실제 표: 헤즈업 SRP 조회 성공", bool(adv), str(adv and adv["reason"]))
    r = advisor.recommend(dict(STRONG, pot=9.0, pot_total=9.0))
    line = next((d for d in r["detail"] if REF in d), "")
    test("실제 표: 3명 팟 참고 줄", bool(line), str(r["detail"]))
    print("  예:", line)
    test("실제 표: 액션 그대로", same_decision(r, base_r))
    advisor.GTO_ENABLED = False
    advisor.gto_cache_clear()
else:
    skip("실제 라이브러리 조회", "GTO_LIB_DIR 없음")

failed = 0
for mark, name, info in results:
    failed += mark == "FAIL"
    print(f"[{mark}] {name}" + (f" — {info}" if mark != "PASS" and info else ""))
print(f"\n{sum(1 for m, _, _ in results if m == 'PASS')}/{len(results)} passed, {sum(1 for m, _, _ in results if m == 'SKIP')} skipped")
sys.exit(1 if failed else 0)
