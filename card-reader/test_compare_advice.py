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

failed = 0
for ok, name, info in results:
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info and not ok else ""))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
