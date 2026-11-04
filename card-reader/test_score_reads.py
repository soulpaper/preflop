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

# --- 8. handlog.derive: 프리플랍 정리본으로 3벳 판정 (합성 4 사례; test_handlog 에도 같은 시험) ----------------------------
def _derive(pre, seats):
    acts = []
    n = 0
    for k, act, to, allin in pre:
        a = {"seat": k, "act": act, "raises_before": n}
        if to is not None:
            a["to"] = to
        if allin:
            a["allin"] = True
        n += act == "raise"       # Tracker 처럼 정리 전 '레이즈' 수
        acts.append(a)
    rec = {"actions": {"preflop": acts}, "seats": {k: {} for k in seats}, "streets": "preflop"}
    return {k: s["stats"] for k, s in handlog.derive(rec)["seats"].items()}


_BL = [("S", "post", 0.5, False), ("B", "post", 1.0, False)]
d1 = _derive(_BL + [("C", "raise", 2.0, False), ("B", "raise", 2.1, False)], "SBC")
test("derive ①: 2.1 '레이즈'(올인 아님)는 콜 = B: 3벳기회 1, 3벳 0, pfr 0",
     (d1["B"]["threebet_opp"], d1["B"]["threebet"], d1["B"]["pfr"], d1["B"]["vpip"]) == (1, 0, 0, 1), str(d1["B"]))
d2 = _derive(_BL + [("C", "raise", 2.0, False), ("S", "raise", 2.1, True), ("B", "call", 2.1, False)], "SBC")
test("derive ②: 짧은 올인 S: 기회 1, 3벳 0 (pfr 1), 뒤의 B 콜: 기회 1",
     (d2["S"]["threebet_opp"], d2["S"]["threebet"], d2["S"]["pfr"]) == (1, 0, 1) and d2["B"]["threebet_opp"] == 1
     and d2["B"]["threebet"] == 0, str((d2["S"], d2["B"])))
d3 = _derive(_BL + [("C", "raise", 2.0, False), ("B", "raise", 6.0, False)], "SBC")
test("derive ③: 진짜 3벳 B: 3벳 1, 오프너 C: 기회 0",
     (d3["B"]["threebet"], d3["B"]["threebet_opp"], d3["C"]["threebet_opp"], d3["C"]["pfr"]) == (1, 1, 0, 1), str(d3))
d4 = _derive(_BL + [("C", "raise", 1.3, False), ("C", "raise", 2.0, False), ("D", "raise", 6.0, False)], "SBCD")
test("derive ④: 오픈 두 번 읽음 → D: 3벳 1, C: 기회 0·pfr 1",
     (d4["D"]["threebet"], d4["D"]["threebet_opp"], d4["C"]["threebet_opp"], d4["C"]["pfr"]) == (1, 1, 0, 1), str(d4))

# --- 9. 기여 분리 (ablation): 합성 기록 ---------------------------------------------------------------------
rows_a, _, _ = sr.score_hands([TYP, OLD, OLD2], [DEC_T], ablation=True)
ba = {(r["hand"], r["street"]): r for r in rows_a}
test("abl: 모든 행에 변형 5개 (순서 고정)", all(list(r["abl"]) == list(sr.ABL_NAMES) and len(r["abl"]) == 5 for r in rows_a)
     and sr.ABL_NAMES == ("base", "+tail", "+mult", "+style", "+style1"))
test("abl: 재생 뒤 스위치 복원", (hr.MULT_SET, hr.PRE_FLOOR, hr.STYLE_STRENGTH) == ("current", 0.07, 0.5))
test("abl: 유형 없는 행은 +style == +mult == +style1",
     all(r["abl"]["+style"] == r["abl"]["+mult"] == r["abl"]["+style1"] for r in rows_a if r["type"] == "없음"))
test("abl: 프리플랍 행은 +mult == +tail, +style == +mult (MULT/유형은 프리플랍에 안 쓰임)",
     all(r["abl"]["+mult"] == r["abl"]["+tail"] and r["abl"]["+style"] == r["abl"]["+mult"]
         for r in rows_a if r["street"] == "preflop"))
test("abl: 유형 있는 포스트플랍 행은 +style != +mult, 0.5 와 1.0 이 다르고 +style == live",
     ba[("t-0001", "flop")]["abl"]["+style"]["p"] != ba[("t-0001", "flop")]["abl"]["+mult"]["p"]
     and ba[("t-0001", "flop")]["abl"]["+style1"]["p"] != ba[("t-0001", "flop")]["abl"]["+style"]["p"]
     and ba[("t-0001", "flop")]["abl"]["+style"]["p"] == ba[("t-0001", "flop")]["live"]["p"])
test("abl: base 는 차트 밖 꼬리가 없어 +tail 과 프리플랍에서 다름",
     any(r["abl"]["base"]["p"] != r["abl"]["+tail"]["p"] for r in rows_a if r["street"] == "preflop"))
test("abl: 기본 호출은 abl 없음 (기존 JSON 형태)", all("abl" not in r for r in rows_t))


def srow(h, seat, lls, typ="LP", uni=5.0):
    return {"hand": h, "seat": seat, "street": "flop", "type": typ, "uniform": {"ll": uni},
            "abl": {n: {"ll": ll} for n, ll in zip(sr.ABL_NAMES, lls)}}


def synth(steps, n=30, typ="LP"):
    """steps: 변형마다 ll 이 줄어드는 양 (양수 = 좋아짐). 묶음마다 약간의 차이."""
    out = []
    for j in range(n):
        base = 5.0 + (j % 5) * 0.1
        lls = [base]
        for st_ in steps:
            lls.append(lls[-1] - st_ - ((j % 3) * 0.01 if st_ else 0))
        out.append(srow(f"h{j}", "V", lls, typ))
    return out


good = synth([0.3, 0.3, 0.3, 0.3])
b1 = sr.bootstrap_ablation(good, B=200)
b2 = sr.bootstrap_ablation(good, B=200)
b3 = sr.bootstrap_ablation(good, B=200, seed=1)
test("bootstrap: 같은 seed 면 같은 결과, 다른 seed 면 구간이 다름", b1 == b2 and b1["delta"] != b3["delta"])
test("bootstrap: Δ 추정치 = 0.3/ln2 근처, 구간 안에 추정치",
     all(abs(d[0] - (0.3 + 0.01) / math.log(2)) < 0.05 and d[1] <= d[0] <= d[2] for d in b1["delta"].values()))
test("bootstrap: 변형별 정보이득은 변형 순서대로 커짐", [b1["bits"][n][0] for n in sr.ABL_NAMES]
     == sorted(b1["bits"][n][0] for n in sr.ABL_NAMES))
dup = good + [dict(good[0]), dict(good[0]), dict(good[0], street="turn")]      # 같은 (hand, seat) 행 복제 → 묶음 수 그대로
test("bootstrap: 묶음 단위 (같은 좌석 행을 복제해도 묶음 수 같음)",
     sr.bootstrap_ablation(dup, B=20)["n_bundles"] == sr.bootstrap_ablation(good, B=20)["n_bundles"] == 30)
test("bootstrap: subset (포스트플랍만)", sr.bootstrap_ablation(good, B=20, subset=lambda r: r["street"] == "river")["n_bundles"] == 0)
mixed = synth([0.3, 0.3, 0.3, 0.3], 10, "없음") + [dict(r, hand="x" + r["hand"]) for r in synth([0.3] * 4, 5)]
bm = sr.bootstrap_ablation(mixed, B=20)
test("bootstrap: STYLE Δ 는 유형 있는 묶음만 (유형 5 / 전체 15)", bm["n_bundles"] == 15 and bm["n_typed"] == 5)
test("decide: 분명히 좋아지면 MULT·STYLE 유지 + 1.0 제안", (lambda d: d["mult"]["verdict"] == "유지" and d["style"]["verdict"] == "유지"
     and d["style_strength_suggest"] == 1.0)(sr.decide(b1)), str(sr.decide(b1)))
flat = sr.bootstrap_ablation(synth([0, 0, 0, 0]), B=200)
df = sr.decide(flat)
test("decide: Δ 가 0 이면 보류(되돌림 권고), 1.0 제안 없음", df["mult"]["verdict"] == "보류" and df["style"]["verdict"] == "보류"
     and "되돌림" in df["mult"]["action"] and df["style_strength_suggest"] is None, str(df))
bad = sr.decide(sr.bootstrap_ablation(synth([0.3, -0.3, -0.3, 0.0]), B=200))
test("decide: 분명히 나빠지면 되돌림", bad["mult"]["verdict"] == "되돌림" and bad["style"]["verdict"] == "되돌림", str(bad))
test("wilson: 0/10 하한 0, 5/10 은 0.5 중심", sr.wilson(0, 10)[0] == 0.0 and abs(sum(sr.wilson(5, 10)) / 2 - 0.5) < 1e-9
     and sr.wilson(0, 0) == (None, None))

# --- 10. 벳 후 폴드율 -------------------------------------------------------------------------------------------
def bf_hand(hid, flop, typ_v="LP", typ_w="TP", folded=None, showdown=None, conf="ok", turn=None):
    acts = {"preflop": [], "flop": flop}
    if turn:
        acts["turn"] = turn
    h = hand(hid, acts, ["Ah", "7d", "2s"], {}, conf=conf, folded=folded or {"hero": "preflop"})
    h["showdown"] = showdown
    h["seats"]["V"]["type"] = typ_v
    h["seats"]["W"]["type"] = typ_w
    return h


f1 = bf_hand("b1", [A("W", "check"), A("V", "bet", to=3.0), A("W", "fold", facing=True)],
             folded={"hero": "preflop", "W": "flop"})
r1 = sr.bet_fold_rates([f1], [])
test("bet_fold: 벳 후 전원 폴드 = 분자 1 (LP/flop 1/1)", r1["LP"]["flop"]["k"] == 1 and r1["LP"]["flop"]["n"] == 1
     and r1["LP"]["all"]["rate"] == 1.0 and r1["LP"]["flop"]["lo"] < 1.0, str(r1))
f2 = bf_hand("b2", [A("W", "check"), A("V", "bet", to=3.0), A("W", "call", to=3.0, facing=True)], typ_v="TAG",
             folded={"hero": "preflop"}, showdown={"seats": ["V", "W"], "shown": {}})
f2b = bf_hand("b2b", [A("W", "check"), A("V", "bet", to=3.0), A("W", "call", to=3.0, facing=True)], typ_v="TAG",
              folded={"hero": "preflop"})
r2 = sr.bet_fold_rates([f2, f2b], [])
test("bet_fold: 콜 / 쇼다운이면 분자 0 (분모에는 포함)", r2["TAG"]["flop"]["n"] == 2 and r2["TAG"]["flop"]["k"] == 0, str(r2))
f3 = bf_hand("b3", [A("W", "check"), A("V", "bet", to=3.0), A("W", "raise", to=9.0, facing=True), A("V", "fold", facing=True)],
             typ_v="LAG", typ_w="TAG", folded={"hero": "preflop", "V": "flop"})
r3 = sr.bet_fold_rates([f3], [])
test("bet_fold: 레이즈당한 벳은 0 (V 벳 0/1), 그 레이즈는 폴드로 끝 1/1",
     r3["LAG"]["flop"]["k"] == 0 and r3["LAG"]["flop"]["n"] == 1 and r3["TAG"]["flop"]["k"] == 1, str(r3))
f4 = bf_hand("b4", [A("W", "check"), A("V", "bet", to=3.0), A("W", "fold", facing=True)], typ_v="TP",
             folded={"hero": "preflop", "W": "flop"}, conf="low")
test("bet_fold: low confidence 제외", sr.bet_fold_rates([f4], []) == {})
f5 = bf_hand("b5", [A("W", "check"), A("V", "bet", to=3.0), A("W", "call", to=3.0, facing=True)], typ_v="TP",
             turn=[A("W", "check"), A("V", "bet", to=6.0), A("W", "fold", facing=True)], folded={"hero": "preflop", "W": "turn"})
r5 = sr.bet_fold_rates([f5], [])
test("bet_fold: 스트리트별 (플랍 벳은 콜 0/1, 턴 벳은 폴드 1/1, all 1/2)",
     (r5["TP"]["flop"]["k"], r5["TP"]["turn"]["k"], r5["TP"]["all"]["n"], r5["TP"]["all"]["k"]) == (0, 1, 2, 1), str(r5))
f6 = bf_hand("b6", [A("W", "check"), A("V", "bet", to=3.0), A("W", "fold", facing=True)], typ_v=None,
             folded={"hero": "preflop", "W": "flop"})
f6["seats"]["V"].pop("type")
r6 = sr.bet_fold_rates([f6], [dict(DEC_T, hand_id="b6", street="flop")])
test("bet_fold: 유형은 결정 기록 프로필의 style (LAG), 없으면 '없음'",
     "LAG" in r6 and "없음" in sr.bet_fold_rates([f6], []), str(r6))
f7 = bf_hand("b7", [A("hero", "bet", to=3.0), A("V", "fold", facing=True)], folded={"V": "flop"})
test("bet_fold: hero 의 벳은 제외", sr.bet_fold_rates([f7], []) == {})
sbf = sr.summarize(rows_a, [], {}, bet_fold=r1)
_bb = _io.StringIO()
sr.report(dict(sbf, ablation={"all": b1, "post": b1, "decision": sr.decide(b1)}), _bb)
test("report: 벳 후 폴드 절 + 기여 분리 절", "벳/레이즈 후 전원 폴드" in _bb.getvalue() and "기여 분리" in _bb.getvalue()
     and "MULT 행 변경" in _bb.getvalue() and sbf["bet_fold_by_type"] == r1)
with tempfile.TemporaryDirectory() as d:
    with open(os.path.join(d, "2026-01-01.jsonl"), "w", encoding="utf-8") as f:
        for r in (REC,):
            f.write(json.dumps(r) + "\n")
    outp = os.path.join(d, "o.json")
    sr.main([d, "--json", outp, "--quiet", "--boot", "20"])
    with open(outp, encoding="utf-8") as f:
        jj = json.load(f)
    test("main: 기본은 기여 분리 포함 (rows[].abl, summary.ablation)", "abl" in jj["rows"][0]
         and set(jj["summary"]["ablation"]) == {"all", "post", "decision"} and len(jj["rows"][0]["abl"]) == 5)
    sr.main([d, "--json", outp, "--quiet", "--no-ablation"])
    with open(outp, encoding="utf-8") as f:
        jj = json.load(f)
    test("main --no-ablation: abl 없음", "abl" not in jj["rows"][0] and "ablation" not in jj["summary"])

# --- 결과 ---------------------------------------------------------------------------------------
failed = 0
for ok, name, info in results:
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info else ""))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
