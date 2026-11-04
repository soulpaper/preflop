"""compare_advice 테스트 — python test_compare_advice.py   (합성 기록만 사용, 실제 데이터 없음)
advisor 를 직접 부르는 시험만 numpy 가 필요하다 (없으면 SKIP). 나머지는 가짜 recommend 로 numpy 없이 돈다."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

import compare_advice as ca
import handread as hr
import rangeq as rq

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

results = []


def test(name, ok, info=""):
    results.append((bool(ok), name, info))


NICK, PID = "SECRETNICK", "pid-abc123"


def A(seat, act, **kw):
    d = {"seat": seat, "act": act, "src": "bet", "facing": False, "raises_before": kw.pop("rb", 0)}
    d.update(kw)
    return d


HAND = {"id": "h-0001", "start": 1.0, "players": 6, "pos_players": 6, "hero_cards": ["Ah", "Kd"],
        "board": ["7h", "8d", "2s"],
        "actions": {"preflop": [A("W", "post", to=1.0), A("V", "raise", to=3.0), A("hero", "call", to=3.0, rb=1),
                                A("W", "fold", rb=1)],
                    "flop": [A("V", "bet", to=4.5), A("hero", "call", to=4.5, facing=True)],
                    "turn": [A("V", "bet", to=15.0)], "river": []},
        "seats": {"V": {"pos": "BTN", "hero": False, "folded": None}, "W": {"pos": "BB", "hero": False, "folded": "preflop"},
                  "hero": {"pos": "CO", "hero": True, "folded": None}}, "confidence": "ok"}


def dec(**over):
    d = {"type": "decision", "id": "h-0001/1", "v": 1, "t": 10.0, "day": "2026-01-01", "hand_id": "h-0001",
         "street": "flop", "hero": ["Ah", "Kd"], "board": ["7h", "8d", "2s"], "players": 6, "hero_pos": "CO",
         "stack_bb": 40, "my_stack": 40, "pot_total": 10.5, "call": 4.5, "can_check": False, "ante": 0, "opponents": 1,
         "equity": 0.4, "equity_basis": "range",
         "seats": {"V": {"pos": "BTN", "hero": False, "folded": None, "pid": PID, "type": "LAG",
                         "acts": {"preflop": [{"act": "raise", "to": 3.0}], "flop": [{"act": "bet", "to": 4.5, "pct": 0.75}]}},
                   "W": {"pos": "BB", "hero": False, "folded": "preflop", "pid": "pid-w",
                         "acts": {"preflop": [{"act": "post", "to": 1.0}, {"act": "fold"}]}},
                   "hero": {"pos": "CO", "hero": True, "folded": None, "acts": {"preflop": [{"act": "call", "to": 3.0}]}}},
         "advice": {"action": "call", "action_ko": "콜"}, "nick": NICK}
    d.update(over)
    return d


# --- 1. 설정 파싱 / 경로 -----------------------------------------------------------------------------
test("parse_cfg: 쉼표 설정", ca.parse_cfg("mult_set=prev,pre_floor=0") == {"mult_set": "prev", "pre_floor": 0.0})
test("parse_cfg: 빈 값은 {}", ca.parse_cfg("") == {})
try:
    ca.parse_cfg("bogus=1")
    bad = False
except ValueError:
    bad = True
test("parse_cfg: 모르는 키는 ValueError", bad)
os.environ.pop("CARD_READER_DATA", None)
for label, fn in (("경로 없음", lambda: ca.data_dir(None)), ("없는 폴더", lambda: ca.data_dir("/nonexistent-xyz"))):
    try:
        fn()
        ok = False
    except SystemExit as e:
        ok = bool(str(e.code))
    test(f"data_dir: {label} → 오류로 끝남 (저장소 기본 경로 없음)", ok)
tmp = tempfile.mkdtemp(prefix="cmp-adv-")
os.environ["CARD_READER_DATA"] = tmp
test("data_dir: 환경변수 CARD_READER_DATA", ca.data_dir(None) == tmp)
os.environ.pop("CARD_READER_DATA")

# --- 2. 복원 ----------------------------------------------------------------------------------------
ctx, why = ca.restore_ctx(dec())
test("restore_ctx: 필드 복원", ctx and ctx["hero"] == ["Ah", "Kd"] and ctx["street"] == "flop" and ctx["call"] == 4.5
     and ctx["equity"] == 0.4 and ctx["opponents_in_hand"] == 1 and "nick" not in ctx and "seats" not in ctx)
for name, d_, w_ in (("카드 없음", dec(hero=["Ah"]), "no_hero_cards"), ("추천 없음", dec(advice=None), "no_advice"),
                     ("에쿼티 없음", dec(equity=None), "no_equity")):
    c_, why_ = ca.restore_ctx(d_)
    test(f"restore_ctx: {name} → 건너뜀({w_})", c_ is None and why_ == w_)
acts = ca.truncate_actions(HAND["actions"], dec()["seats"])
test("truncate_actions: 결정 순간까지만 (턴 벳·내 플랍 콜 제외)",
     len(acts["flop"]) == 1 and acts["turn"] == [] and len(acts["preflop"]) == 4 - 0 and acts["flop"][0]["seat"] == "V",
     str({k: len(v) for k, v in acts.items()}))
test("live_opponents: 폴드한 BB 제외", ca.live_opponents(acts) == ["V"], str(ca.live_opponents(acts)))

# --- 3. 재생 (가짜 recommend, numpy 불필요) ------------------------------------------------------------
seen = []


def probe(ctx):
    seen.append(ctx)
    return {"action": "call"}


hands = {"h-0001": HAND}
ca.replay([dec()], hands, {"mult_set": "current"}, {"mult_set": "prev"}, probe, iters=400)
eqs = [c["equity"] for c in seen]
test("replay: 설정마다 범위 에쿼티를 다시 재서 ctx 에 넣음", len(seen) == 2 and "villain_ranges" in seen[0] and eqs[0] != eqs[1],
     str(eqs))
thr = sum(eqs) / 2


def fake_adv(ctx):
    return {"action": "call" if ctx["equity"] > thr else "fold"}


res, sk = ca.replay([dec(), dec(id="x", hero=["Ah"]), dec(id="y", hand_id="nohand"), dec(id="z", equity_basis="random")],
                    hands, {"mult_set": "current"}, {"mult_set": "prev"}, fake_adv, iters=400)
test("replay: 바뀐 추천 1건 (스트리트 flop), 같은 입력이면 같은 결과",
     len(res) == 2 and sum(r["a"] != r["b"] for r in res) == 1 and res[0]["street"] == "flop"
     and res[0]["a"] != res[0]["b"], str(res))
test("replay: 건너뜀 이유 집계", sk["no_hero_cards"] == 1 and sk["no_hand_record"] == 1, str(dict(sk)))
res2, _ = ca.replay([dec()], hands, {"mult_set": "current"}, {"mult_set": "current"}, fake_adv, iters=400)
test("replay: 같은 설정 둘이면 바뀐 것 없음", res2[0]["a"] == res2[0]["b"])
test("replay: 스위치를 되돌림 (MULT_SET 기본 그대로)", hr.MULT_SET == "prev")


def boom(ctx):
    raise RuntimeError(NICK)


res3, sk3 = ca.replay([dec()], hands, {}, {}, boom, iters=400)
test("replay: recommend 예외 → replay_error 로 세고 계속", not res3 and sk3["replay_error"] == 1)

# --- 4. 집계 / 출력에 개인정보 없음 ---------------------------------------------------------------------
s = ca.summarize(res, 4, sk)
test("summarize: 합계 일관", s["total"] == 4 and s["replayed"] + s["skipped"] == 4 and s["changed"] == 1
     and s["changes"][0]["street"] == "flop" and s["changes"][0]["count"] == 1, str(s))
rep = ca.format_report(s, "mult_set=current", "mult_set=prev")
test("format_report: 닉네임·pid·카드 없음", all(x not in rep for x in (NICK, PID, "pid-w", "Ah", "Kd", "7h")))

# --- 5. main (실제 파일 읽기) ---------------------------------------------------------------------------
shutil.rmtree(tmp)
tmp = tempfile.mkdtemp(prefix="cmp-adv-")
os.makedirs(os.path.join(tmp, "hands"))
with open(os.path.join(tmp, "hands", "2026-01-01.jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps(HAND) + "\n")
with open(os.path.join(tmp, "hands", "decisions_2026-01-01.jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps(dec()) + "\n" + "깨진 줄\n" + json.dumps(dec(id="h-0001/2", rev=1, hero=["Ah"])) + "\n")
jpath = os.path.join(tmp, "out.json")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    s2 = ca.main([tmp, "--iters", "400", "--json", jpath], recommend=fake_adv)
out = buf.getvalue()
js = open(jpath, encoding="utf-8").read()
test("main: 총 2건, 재생 1, 건너뜀 1", (s2["total"], s2["replayed"], s2["skipped"]) == (2, 1, 1), str(s2))
test("main: 출력과 JSON 에 닉네임·pid·카드 없음",
     all(x not in out + js for x in (NICK, PID, "Ah", "Kd", "pid-w", "7h", "8d")))
test("main: 출력에 요약 숫자", "총 결정 2" in out and "재생 성공 1" in out)
shutil.rmtree(tmp, ignore_errors=True)

# --- 6. 실제 advisor (numpy 있을 때만) -----------------------------------------------------------------
try:
    import advisor
except ImportError as e:
    print(f"[SKIP] advisor 임포트 불가({e.name}) — 실제 advisor 재생 시험 건너뜀")
else:
    d_ = dec(street="preflop", board=[], equity_basis="random", situation="vsopen", villain_pos="BTN",
             hero_pos="BB", call=2.0, pot_total=5.0, equity=0.4)
    r_, s_ = ca.replay([d_], hands, {"mult_set": "current"}, {"mult_set": "prev"}, advisor.recommend, iters=300)
    test("advisor: 실제 recommend 로 재생 (프리플랍은 MULT 와 무관 → 안 바뀜; 복원 부족이면 건너뜀으로 셈)",
         len(r_) + sum(s_.values()) == 1 and all(x["a"] == x["b"] for x in r_), str((r_, dict(s_))))

# --- 7. 커밋 대 커밋 / 프리플랍 상대 수 다시 세기 ------------------------------------------------------------
# 프리플랍: UTG 폴드, MP 오픈 3, CO 콜 3, BTN 아직 안 함, SB 0.5 냄, BB 1 냄, 나(BTN 뒤 가정: HJ) 차례
PRE_SEATS = {"U": {"pos": "UTG", "hero": False, "folded": "preflop", "acts": {"preflop": [{"act": "fold"}]}},
             "M": {"pos": "MP", "hero": False, "folded": None, "acts": {"preflop": [{"act": "raise", "to": 3.0}]}},
             "C": {"pos": "CO", "hero": False, "folded": None, "acts": {"preflop": [{"act": "call", "to": 3.0}]}},
             "B": {"pos": "BTN", "hero": False, "folded": None, "acts": {}},
             "S": {"pos": "SB", "hero": False, "folded": None, "acts": {"preflop": [{"act": "post", "to": 0.5}]}},
             "G": {"pos": "BB", "hero": False, "folded": None, "acts": {"preflop": [{"act": "post", "to": 1.0}]}},
             "hero": {"pos": "HJ", "hero": True, "folded": None, "acts": {"preflop": [{"act": "call", "to": 1.0}]}}}
PRE_HAND = {"id": "h-0002", "start": 1.0, "players": 7, "pos_players": 7, "hero_cards": ["Ah", "Kd"], "board": [],
            "actions": {"preflop": [A("S", "post", to=0.5), A("G", "post", to=1.0), A("U", "fold"),
                                    A("M", "raise", to=3.0), A("C", "call", to=3.0, rb=1)],
                        "flop": [], "turn": [], "river": []},
            "seats": {k: {"pos": v["pos"], "hero": v["hero"], "folded": v["folded"]} for k, v in PRE_SEATS.items()},
            "confidence": "ok"}


def pdec(**over):
    d = dec(id="h-0002/1", hand_id="h-0002", street="preflop", board=[], hero_pos="HJ", situation="vsopen",
            villain_pos="MP", call=2.0, pot_total=8.5, opponents=5, equity=0.2, seats=PRE_SEATS)
    d.update(over)
    return d


ts = ca.table_state_from(pdec())
by = {s["key"]: s for s in ts["seats"]}
test("table_state_from: 바닥 칩·블라인드·핸드 안/밖",
     by["M"]["bet"] == 3.0 and by["C"]["bet"] == 3.0 and by["B"]["bet"] == 0.0 and by["S"]["bet"] == 0.5
     and not by["U"]["in_hand"] and by["hero"]["bet"] == 1.0 and ts["blind_seats"] == {"sb": "S", "bb": "G"}
     and ts["preflop"]["raises"] == 1, str(ts))
ctxp, _ = ca.restore_ctx(pdec())
test("restore_ctx: 프리플랍 hero_bet = 내 좌석 기록의 낸 칩", ctxp.get("hero_bet") == 1.0, str(ctxp.get("hero_bet")))
test("restore_ctx: 포스트플랍엔 hero_bet 없음", "hero_bet" not in ca.restore_ctx(dec())[0])
calls = []


def fake_eo(ts_, nboard):
    calls.append((ts_, nboard))
    return {"n": 2, "keys": ["M", "C"], "basis": "x", "dropped": []}


test("voluntary_opponents: (n, keys, 'voluntary')", ca.voluntary_opponents(pdec(), fake_eo) == (2, ["M", "C"], "voluntary")
     and calls[-1][1] == 0)
test("voluntary_opponents: 포스트플랍은 None (기록값)", ca.voluntary_opponents(dec(), fake_eo) is None)
test("voluntary_opponents: 정할 수 없음 → None", ca.voluntary_opponents(pdec(), lambda t, n: None) is None)
test("voluntary_opponents: 9명 넘으면 잘리고 basis 없음 (reader 와 같은 조건)",
     ca.voluntary_opponents(pdec(), lambda t, n: {"n": 12, "keys": [], "basis": "", "dropped": []}) == (5, [], None))
try:
    from reader import equity_opponents as real_eo
except ImportError as e:
    print(f"[SKIP] reader 임포트 불가({e.name}) — 실제 equity_opponents 시험 건너뜀")
else:
    test("voluntary_opponents: 실제 reader.equity_opponents (오프너 + 콜러, 뒤 BTN·블라인드 제외)",
         ca.voluntary_opponents(pdec(), real_eo) == (2, ["M", "C"], "voluntary"), str(ca.voluntary_opponents(pdec(), real_eo)))
seen_a, seen_b = [], []
hands2 = {"h-0002": PRE_HAND}
res7, sk7 = ca.replay([pdec()], hands2, {}, {}, lambda c: seen_a.append(c) or {"action": "call"}, iters=300,
                      recommend_b=lambda c: seen_b.append(c) or {"action": "fold"}, opponents="voluntary",
                      equity_opponents=lambda t, n: {"n": 5, "keys": ["C"], "basis": "x", "dropped": []})
ca_, cb_ = (seen_a or [{}])[0], (seen_b or [{}])[0]
test("replay(voluntary): 상대 수·basis 를 다시 넣고 두 쪽 ctx 가 같음",
     ca_.get("opponents_in_hand") == 5 and ca_.get("opponents_basis") == "voluntary" and ca_ == cb_, str((ca_.keys(), dict(sk7))))
test("replay(voluntary): keys 좌석 레인지 먼저, 모자라면 랜덤 핸드로 채움 (reader.range_analyze)",
     len(ca_.get("villain_ranges") or []) == 5 and ca_["villain_ranges"][-1] == rq.random_range() and ca_["villain_ranges"][0] != rq.random_range(), str(len(ca_.get("villain_ranges") or [])))
test("replay(voluntary): recommend_b 로 B 쪽 → 바뀐 추천 1 (결과에 번호 i)",
     len(res7) == 1 and res7[0]["a"] == "call" and res7[0]["b"] == "fold" and res7[0]["i"] == 0, str(res7))
seen_a.clear()
ca.replay([pdec()], hands2, {}, {}, lambda c: seen_a.append(c) or {"action": "call"}, iters=300)
test("replay(logged): 기록된 상대 수 그대로, basis 없음",
     seen_a and seen_a[0].get("opponents_in_hand") == 5 and "opponents_basis" not in seen_a[0], str(seen_a[:1]))
try:
    ca.load_advisor("no-such-rev-xyz")
    bad = False
except SystemExit:
    bad = True
test("load_advisor: 없는 커밋 → 오류로 끝남", bad)
try:
    import advisor as _adv
except ImportError as e:
    print(f"[SKIP] advisor 임포트 불가({e.name}) — load_advisor 시험 건너뜀")
else:
    rec_head = ca.load_advisor("HEAD")
    cx = {"hero": ["Ah", "Kd"], "board": [], "street": "preflop", "players": 6, "hero_pos": "BTN", "situation": "open",
          "stack_bb": 100, "call": 1.0, "pot_total": 1.5, "equity": 0.6, "opponents_in_hand": 1}
    test("load_advisor: HEAD 의 advisor 를 따로 불러 같은 추천 (작업 트리와 별개 모듈)",
         rec_head is not _adv.recommend and rec_head(dict(cx))["action"] == _adv.recommend(dict(cx))["action"])
    tmp = tempfile.mkdtemp(prefix="cmp-adv-")
    with open(os.path.join(tmp, "2026-01-02.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(PRE_HAND) + "\n")
    with open(os.path.join(tmp, "decisions_2026-01-02.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(pdec()) + "\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        s3 = ca.main([tmp, "--a-rev", "HEAD", "--b-rev", "HEAD", "--opponents", "voluntary", "--iters", "300"])
    out3 = buf.getvalue()
    test("main --a-rev/--b-rev: 같은 커밋 둘이면 바뀐 것 0, 스위치는 두 쪽 같게",
         s3["replayed"] + s3["skipped"] == 1 and s3["changed"] == 0 and "B=mult_set=current advisor@HEAD" in out3, out3)
    shutil.rmtree(tmp, ignore_errors=True)

failed = 0
for ok, name, info in results:
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info and not ok else ""))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
