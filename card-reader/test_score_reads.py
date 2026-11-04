"""score_reads 테스트 — python test_score_reads.py   (합성 기록만 사용, 실제 데이터 없음)"""
import json
import math
import os
import sys
import tempfile

import evaluator as ev
import handlog
import handread as hr
import score_reads as sr

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

results = []


def test(name, ok, info=""):
    results.append((bool(ok), name, info))


def A(seat, act, **kw):
    d = {"seat": seat, "act": act, "src": "bet", "facing": False, "raises_before": kw.pop("rb", 0)}
    d.update(kw)
    return d


def hand(hid, actions, board, shown, hero=("2c", "3d"), conf="ok", pos=None, folded=None):
    pos = pos or {"V": "BTN", "W": "BB", "hero": "CO"}
    folded = folded or {}
    seats = {k: {"pos": p, "hero": k == "hero", "folded": folded.get(k), "allin": False,
                 "shown": shown.get(k)} for k, p in pos.items()}
    return {"id": hid, "start": 1.0, "players": 6, "pos_players": 6, "hero_cards": list(hero), "board": list(board),
            "actions": actions, "showdown": {"seats": list(shown), "shown": shown}, "seats": seats, "issues": [],
            "confidence": conf}


# --- 1. 벳 크기 계산 ------------------------------------------------------------------------------
REC = hand("s-0001", {
    "preflop": [A("W", "post", to=1.0, blind="BB"), A("V", "raise", to=3.0), A("hero", "fold", rb=1),
                A("W", "call", to=3.0, rb=1)],
    "flop": [A("W", "check"), A("V", "bet", to=4.5), A("W", "call", to=4.5, facing=True)],
    "turn": [A("W", "check"), A("V", "bet", to=15.0), A("W", "call", to=15.0, facing=True)],
    "river": [A("W", "check"), A("V", "check")],
}, ["Ah", "7d", "2s", "Kc", "9h"], {"V": ["As", "Kd"], "W": ["7h", "7c"]}, folded={"hero": "preflop"})
sz = handlog.bet_sizes(REC["actions"])
test("bet_sizes: 플랍 4.5 into 6 = 0.75", abs(sz[("flop", 1)] - 0.75) < 1e-9, str(sz))
test("bet_sizes: 콜은 받은 벳 크기", abs(sz[("flop", 2)] - 0.75) < 1e-9)
test("bet_sizes: 턴 15 into 15 = 1.0", abs(sz[("turn", 1)] - 1.0) < 1e-9)

live = sr.replay_seats(REC)
raw = sr.raw_seats(REC)
test("replay_seats(앱 입력): 벳 단어에 크기가 붙음", live["V"]["flop"][0] == {"a": "bet", "size": 0.75}, str(live["V"]["flop"]))
test("raw_seats(예전 입력): 크기 없음", raw["V"]["flop"] == ["bet"])
test("_kind_label: 크기 반영 cbet (0.75 → big)", sr._kind_label(live["V"], "flop") == "cbet_big",
     sr._kind_label(live["V"], "flop"))
test("_kind_label: 크기 모름 → '_?'", sr._kind_label(raw["V"], "flop") == "cbet_?", sr._kind_label(raw["V"], "flop"))
test("truncate: 턴까지만", sr.truncate(live["V"], "turn")["river"] == [] and sr.truncate(live["V"], "turn")["turn"])

# --- 2. 올인 뒤 '체크' 스트리트 ----------------------------------------------------------------------
JAM = hand("s-0002", {
    "preflop": [A("W", "post", to=1.0), A("V", "raise", to=12.0, allin=True), A("hero", "fold", rb=1),
                A("W", "call", to=12.0, rb=1)],
    "flop": [A("W", "check")], "turn": [A("W", "check")], "river": [A("W", "check")],
}, ["Ah", "7d", "2s", "Kc", "9h"], {"V": ["Qs", "Qd"], "W": ["Jh", "Tc"]}, folded={"hero": "preflop"})
test("phantom_streets: 올인 뒤 체크는 실제 선택이 아님", sr.phantom_streets(JAM) == {"flop", "turn", "river"},
     str(sr.phantom_streets(JAM)))
test("phantom_streets: 정상 핸드는 없음", sr.phantom_streets(REC) == set())

# --- 3. 프리플랍 입력 정리 (handlog.clean_preflop / to_handread — 앱과 같은 경로) ----------------------------------
pre = [A("W", "post", to=1.0), A("V", "raise", to=1.3), A("V", "raise", to=2.0, rb=1), A("hero", "call", to=2.0, rb=2)]
cl, nfix = handlog.clean_preflop(pre)
test("clean_preflop: 같은 좌석 연속 레이즈 = 한 번", [a["act"] for a in cl].count("raise") == 1 and nfix >= 1)
test("clean_preflop: 뒤 콜의 raises_before 다시 셈 (2 → 1)", cl[-1]["raises_before"] == 1, str(cl))
test("clean_preflop: 합친 레이즈 금액은 큰 쪽", cl[1]["to"] == 2.0)
pre2 = [A("W", "post", to=1.0), A("V", "raise", to=2.0), A("hero", "raise", to=2.1, allin=True, rb=1),
        A("W", "call", to=2.1, rb=2)]
cl2, _ = handlog.clean_preflop(pre2)
test("clean_preflop: 최소 레이즈 미만 올인은 레이즈로 안 셈", cl2[-1]["raises_before"] == 1, str(cl2[-1]))
pre3 = [A("W", "post", to=1.0), A("V", "raise", to=12.0, allin=True), A("W", "call", to=12.0, rb=1)]
cl3, _ = handlog.clean_preflop(pre3)
test("clean_preflop: 올인에 콜 = 올인콜 표시", cl3[-1].get("vs_allin") is True)
seats_jam = sr.replay_seats(JAM)
test("replay: 올인에 콜한 BB → call_jam", seats_jam["W"]["preflop"][-1] == "call_jam", str(seats_jam["W"]))
test("replay: 올인/올인콜 상대 스택 = 올인 금액",
     seats_jam["V"].get("stack_bb") == 12.0 and seats_jam["W"].get("stack_bb") == 12.0)
test("replay: 올인 아닌 상대는 스택 없음 (앱이 넘기는 내 스택 사용)", "stack_bb" not in sr.replay_seats(REC)["V"])
test("raw: 예전 입력은 '콜'", sr.raw_seats(JAM)["W"]["preflop"][-1] == "call")
test("원본 기록은 바뀌지 않음", "vs_allin" not in JAM["actions"]["preflop"][-1] and pre[2]["act"] == "raise")

# --- 4. 점수 계산 --------------------------------------------------------------------------------
board = [ev.parse_card(c) for c in ("Ah", "7d", "2s")]
key, hi, lo = sr.combo_key(ev.parse_card("Kd"), ev.parse_card("As"))
test("combo_key: 높은 카드 먼저 (handread range 키와 같은 모양)", key == "AsKd", key)
names, y = sr.actual_class(board, hi, lo)
test("actual_class: AK on A72 = 탑페어+", names[y] == "탑페어+", names[y])
names_p, yp = sr.actual_class([], hi, lo)
test("actual_class: 프리플랍 AKo = 브로드웨이", names_p[yp] == "브로드웨이")
uc = sr.uniform_classes(board, [])
test("uniform_classes: 합 1", abs(sum(uc) - 1) < 1e-9)

fake = {"range": {"AsKd": 0.5, "AhKh": 0.25, "QsQd": 0.25}, "classes": {n: 0.0 for n in hr.CLASSES_POST},
        "top_hands": [("AKo", 50.0), ("QQ", 25.0)], "width_pct": 1.0}
fake["classes"]["탑페어+"] = 75.0
fake["classes"]["중간·약한 페어"] = 25.0
n_live = math.comb(49, 2)
m = sr.score_result(fake, "AsKd", "AKo", hr.CLASSES_POST.index("탑페어+"), n_live)
test("score_result: p = 범위 확률", abs(m["p"] - 0.5) < 1e-12)
test("score_result: 로그손실 = -ln((1-eps)p + eps/N)", abs(m["ll"] + math.log(0.99 * 0.5 + 0.01 / n_live)) < 1e-12)
test("score_result: PIT = 더 가능성 높은 질량 + 같은 것 절반 = 0.25", abs(m["pit"] - 0.25) < 1e-12, str(m["pit"]))
test("score_result: 분류 1순위, top5 적중", m["cls_rank"] == 1 and m["cls_top1"] and m["top5"])
test("score_result: Brier = (0.75-1)^2 + 0.25^2", abs(m["brier"] - (0.0625 + 0.0625)) < 1e-9, str(m["brier"]))
m0 = sr.score_result(fake, "7h7c", "77", hr.CLASSES_POST.index("셋/트립스"), n_live)
test("score_result: 범위 밖 = 0확률, PIT 1, 순위 낮음", m0["zero"] and m0["pit"] == 1.0 and m0["cls_rank"] >= 3)
u = sr.score_uniform(board, [], y, n_live)
test("score_uniform: 로그손실 = ln N", abs(u["ll"] - math.log(n_live)) < 1e-12)

# --- 5. 기록된 리딩 줄 해석 -------------------------------------------------------------------------
test("_expand_token: TT+", sr._expand_token("TT+") == {"AA", "KK", "QQ", "JJ", "TT"})
test("_expand_token: AJs+", sr._expand_token("AJs+") == {"AKs", "AQs", "AJs"})
test("_expand_token: A9o-A7o", sr._expand_token("A9o-A7o") == {"A9o", "A8o", "A7o"})
test("_expand_token: 99-77", sr._expand_token("99-77") == {"99", "88", "77"})
test("_expand_token: KQs", sr._expand_token("KQs") == {"KQs"})
test("_expand_token: 이상한 토큰은 빈 집합", sr._expand_token("??") == set())
# handread._tokens 와 왕복
codes = {"AA", "KK", "QQ", "AKs", "AQs", "AJs", "A5s", "KQo", "KJo", "77"}
back = set()
for t, _ in hr._tokens(codes):
    back |= sr._expand_token(t)
test("_expand_token: handread._tokens 와 왕복", back == codes, str(back ^ codes))
LINE = "CO 오픈: 예상 상위 7% (TT-55, ATs-A8s, KQs 외 8%) | 보드 기준: 탑페어+ 40% · 드로우 30% · 노페어 12%"
p = sr.parse_logged(LINE)
test("parse_logged: 요약 코드", "77" in p["codes"] and "A9s" in p["codes"] and "AKs" not in p["codes"])
test("parse_logged: '외 8%' → 표기 92%", p["covered"] == 92.0)
test("parse_logged: 분류 3개", p["classes"] == {"탑페어+": 40.0, "드로우": 30.0, "노페어": 12.0} and p["board"] is True)
p2 = sr.parse_logged("BB 블라인드: 예상 상위 87% (22+, A2s+) | 프리플랍 기준: 기타 68% · 브로드웨이 15%")
test("parse_logged: '외' 없으면 100%, 프리플랍 기준", p2["covered"] == 100.0 and p2["board"] is False)
p3 = sr.parse_logged("CO 오픈: 예상 상위 12% (TT-55, ATs-A8s, KQs 중 하나일 확률 약 50%) | 보드 기준: 탑페어+ 40%")
test("parse_logged: 새 표기 '중 하나일 확률 약 50%' → 50%",
     p3["covered"] == 50.0 and "77" in p3["codes"] and "KQs" in p3["codes"], str(p3))
test("score_result: 요약 표기 없으면 포함 여부 None", m["sum_in"] is None and m["sum_claim"] is None)
fake2 = dict(fake, summary_codes=["AKo", "QQ"], summary_pct=75.0, summary_hit_pct=60)
m2 = sr.score_result(fake2, "AsKd", "AKo", hr.CLASSES_POST.index("탑페어+"), n_live)
test("score_result: 요약 포함 + 화면 확률(보정) / 모델 확률",
     m2["sum_in"] is True and m2["sum_claim"] == 0.6 and m2["sum_model"] == 0.75, str(m2))

# --- 6. 끝까지: 합성 폴더로 실행 ---------------------------------------------------------------------
DEC = {"type": "decision", "id": "s-0001/1", "t": 2.0, "hand_id": "s-0001", "street": "flop", "stack_bb": 100,
       "ante": 0, "seats": {"V": {"profile": {"vpip": 25, "pfr": 20, "hands": 12}}},
       "reads": {"V": {"width": 20.0, "line": "BTN 오픈: 예상 상위 20% (AA, AKo) | 보드 기준: 탑페어+ 60% · 노페어 20%"}}}
with tempfile.TemporaryDirectory() as d:
    with open(os.path.join(d, "2026-01-01.jsonl"), "w", encoding="utf-8") as f:
        for r in (REC, JAM, dict(REC, id="s-0003")):     # s-0003 = 같은 보드·카드 중복
            f.write(json.dumps(r) + "\n")
        f.write("{broken\n")
    with open(os.path.join(d, "decisions_2026-01-01.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(DEC) + "\n")
    hands, decs = sr.load(d)
    test("load: 깨진 줄 건너뜀", len(hands) == 3 and len(decs) == 1)
    rows, lrows, skipped = sr.score_hands(hands, decs)
    test("score_hands: 중복 기록 제외", skipped.get("duplicate") == 2, str(dict(skipped)))
    test("score_hands: 올인 뒤 체크 스트리트 제외", skipped.get("phantom_street(올인 뒤 체크)", 0) >= 3, str(dict(skipped)))
    streets_v = [r["street"] for r in rows if r["hand"] == "s-0001" and r["seat"] == "V"]
    test("score_hands: 액션한 스트리트마다 채점", streets_v == ["preflop", "flop", "turn", "river"], str(streets_v))
    rv = next(r for r in rows if r["hand"] == "s-0001" and r["seat"] == "V" and r["street"] == "flop")
    test("score_hands: 실제 분류 기록", rv["actual_class"] == "탑페어+" and rv["kind"] == "cbet_big", str(rv["kind"]))
    test("score_hands: 프로필 종류 (12핸드 → ~보통)", rv["profile"] == "~보통", rv["profile"])
    test("score_hands: 기록 리딩 채점", len(lrows) == 1 and lrows[0]["in_top3"] and lrows[0]["top1"]
         and lrows[0]["in_summary"] is True, str(lrows))
    # 재생 결과가 handread.estimate 를 직접 부른 것과 같다
    seat = sr.truncate(sr.replay_seats(REC)["V"], "flop")
    dead = [ev.parse_card(c) for c in REC["hero_cards"]]
    direct = hr.estimate({"V": seat}, [ev.parse_card(c) for c in ("Ah", "7d", "2s")], 6, dead=dead,
                         profiles={"V": DEC["seats"]["V"]["profile"]}, stack_bb=100)["V"]
    test("score_hands: live 점수 = estimate 직접 호출", abs(rv["live"]["p"] - direct["range"].get("AsKd", 0)) < 1e-12)
    s = sr.summarize(rows, lrows, skipped)
    test("summarize: 스트리트별·액션별·보정표", "flop" in s["by_street"] and s["by_kind"] and s["calibration_postflop_all"]["n"] > 0)
    test("summarize: uniform 대비 정보이득은 uniform 자신에겐 없음", "bits_vs_uniform" not in s["by_street"]["flop"]["uniform"])
    out = os.path.join(d, "out.json")
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sr.main([d, "--json", out])
    test("main: 한국어 보고 + JSON", "상대 리딩 채점" in buf.getvalue() and os.path.exists(out))
    with open(out, encoding="utf-8") as f:
        js = json.load(f)
    test("main: JSON 에 summary/rows", "summary" in js and len(js["rows"]) == len(rows))
    os.environ["CARD_READER_DATA"] = d
    test("data_dir: 인자 > 환경변수", sr.data_dir("x") == "x" and sr.data_dir() == d)
    del os.environ["CARD_READER_DATA"]
    test("data_dir: 기본 = 이 파일 폴더/dist/hands", sr.data_dir().endswith(os.path.join("dist", "hands")))
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        sr.main([d, "--ok-only", "--quiet", "--json", out])
    test("main --ok-only --quiet: 출력 없음", buf2.getvalue() == "")

# --- 7. 상대 유형(seats[k].type: LAG/LP/TAG/TP)별 채점 --------------------------------------------------------
TYP = hand("t-0001", REC["actions"], ["Ah", "7d", "2s", "Kc", "9h"], {"V": ["Qs", "Qd"]}, folded={"hero": "preflop"})
TYP["seats"]["V"]["type"] = "LP"
OLD = hand("t-0002", REC["actions"], ["Ah", "7d", "2s", "Kc", "9h"], {"V": ["Js", "Jd"]}, folded={"hero": "preflop"})
DEC_T = {"type": "decision", "id": "t-0002/1", "t": 3.0, "hand_id": "t-0002", "street": "flop", "stack_bb": 100,
         "ante": 0, "seats": {"V": {"profile": {"vpip": 25, "pfr": 20, "hands": 12, "style": "LAG"}}}}
OLD2 = hand("t-0003", REC["actions"], ["Ah", "7d", "2s", "Kc", "9h"], {"V": ["Ts", "Td"]}, folded={"hero": "preflop"})
rows_t, _, _ = sr.score_hands([TYP, OLD, OLD2], [DEC_T])
by = {(r["hand"], r["street"]): r for r in rows_t}
test("유형: 핸드 기록 seats[k].type → 행의 type", by[("t-0001", "flop")]["type"] == "LP")
test("유형: 기록에 없으면 결정 기록 프로필의 style", by[("t-0002", "flop")]["type"] == "LAG")
test("유형: 둘 다 없으면 '없음' (예전 기록)", by[("t-0003", "flop")]["type"] == "없음")
test("유형: live 는 유형 보정, notype 은 보정 없음 (포스트플랍에서 다름)",
     by[("t-0001", "flop")]["live"]["p"] != by[("t-0001", "flop")]["notype"]["p"])
test("유형: 프리플랍은 유형 영향 없음 (live = notype)",
     by[("t-0001", "preflop")]["live"]["p"] == by[("t-0001", "preflop")]["notype"]["p"])
test("유형: 유형 없는 기록은 notype = live", by[("t-0003", "turn")]["live"] == by[("t-0003", "turn")]["notype"])
_dir = hr.estimate({"V": sr.truncate(sr.replay_seats(TYP)["V"], "flop")}, [ev.parse_card(c) for c in ("Ah", "7d", "2s")],
                   6, dead=[ev.parse_card(c) for c in TYP["hero_cards"]], profiles={"V": {"style": "LP"}})["V"]
test("유형: live = estimate(profile style=LP) 직접 호출",
     abs(by[("t-0001", "flop")]["live"]["p"]
         - _dir["range"].get(sr.combo_key(ev.parse_card("Qs"), ev.parse_card("Qd"))[0], -1)) < 1e-12)
s_t = sr.summarize(rows_t, [], {})
test("summarize: by_type (LP/LAG/없음) + 유형/액션별 + 보정표",
     set(s_t["by_type"]) == {"LP", "LAG", "없음"} and "LP/aggr" in s_t["by_type_family"]
     and "없음/aggr" not in s_t["by_type_family"] and s_t["calibration_by_type_family"]["LP/aggr"]["n"] > 0,
     str(list(s_t["by_type_family"])))
import io as _io
_buf = _io.StringIO()
sr.report(s_t, _buf)
test("report: 유형별 칸 (live vs notype)", "[상대 유형별" in _buf.getvalue() and "LP/aggr" in _buf.getvalue())
_buf2 = _io.StringIO()
sr.report(sr.summarize([r for r in rows_t if r["type"] == "없음"], [], {}), _buf2)
test("report: 유형 없는 데이터면 유형 칸 생략", "[상대 유형별" not in _buf2.getvalue())

# --- 결과 ---------------------------------------------------------------------------------------
failed = 0
for ok, name, info in results:
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info else ""))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
