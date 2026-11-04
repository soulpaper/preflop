"""handread 테스트 — python test_handread.py   (속도는 측정값만 출력, 단언하지 않는다)"""
import sys
import time

import evaluator as ev
import handread as hr
import rangeq as rq

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

results = []


def test(name, ok, info=""):
    results.append((bool(ok), name, info))


def one(seat, board, players=9, dead=(), profile=None, stack=100):
    return hr.estimate({"V": seat}, board, players, dead=dead, profiles={"V": profile} if profile else None,
                       stack_bb=stack)["V"]


def cl(res, *names):
    return sum(res["classes"][n] for n in names)


def mass_of(res, codes):
    """range 안에서 특정 핸드코드(예: 'AA')에 속한 확률 합 (%)"""
    tot = 0.0
    for k, w in res["range"].items():
        a, b = ev.parse_card(k[:2]), ev.parse_card(k[2:])
        r1, r2 = max(a >> 2, b >> 2), min(a >> 2, b >> 2)
        code = hr._RK[r1] + hr._RK[r2] + ("" if r1 == r2 else "s" if (a & 3) == (b & 3) else "o")
        if code in codes:
            tot += w
    return tot * 100


STRONG = ("스트레이트 이상", "셋/트립스", "투페어")
BOARD_AR = ["As", "7d", "2c"]

# --- 1. UTG 오픈 + 플랍 큰 벳 (A72 레인보우) ----------------------------------------------
base = one({"pos": "UTG", "preflop": ["open"]}, BOARD_AR)
cb = one({"pos": "UTG", "preflop": ["open"], "flop": [{"a": "bet", "size": 0.8}]}, BOARD_AR)
test("UTG 오픈+큰 c-bet: 탑페어+ 증가", cl(cb, "탑페어+") + cl(cb, *STRONG) > cl(base, "탑페어+") + cl(base, *STRONG) + 15,
     f"{cl(base, '탑페어+') + cl(base, *STRONG):.0f}% → {cl(cb, '탑페어+') + cl(cb, *STRONG):.0f}%")
test("UTG 오픈+큰 c-bet: 노페어(에어) 급감", cb["classes"]["노페어"] < base["classes"]["노페어"] * 0.75,
     f"{base['classes']['노페어']}% → {cb['classes']['노페어']}%")
# 2026-11-05 쇼다운 채점: 벳/레이즈한 상대의 중간·약한 페어가 예측보다 훨씬 많았다(31% vs 13%) → bet_big M 0.18 → 0.35.
# 그래서 '급감'(x0.6)이 아니라 '줄어듦'(x0.9 미만), 대신 탑페어+ 비중보다는 덜 남는다
test("UTG 오픈+큰 c-bet: 중간·약한 페어 줄어듦", cb["classes"]["중간·약한 페어"] < base["classes"]["중간·약한 페어"] * 0.9,
     f"{base['classes']['중간·약한 페어']}% → {cb['classes']['중간·약한 페어']}%")
test("UTG 오픈+큰 c-bet: 탑페어+ 가 최다 분류", max(cb["classes"], key=cb["classes"].get) == "탑페어+", str(cb["classes"]))
test("UTG 오픈 summary 표준 표기 (AQo+ 포함, 외 N%)", "AQo" in cb["summary"] and "외" in cb["summary"], cb["summary"])

# --- 2. BTN 림프/콜: 넓고 캡 ----------------------------------------------------------------
limp = one({"pos": "BTN", "preflop": ["limp", "call"]}, [])
test("BTN 림프: 넓다 (>25%)", limp["width_pct"] > 25, f"{limp['width_pct']}%")
test("BTN 림프: 캡 (AA-QQ 비중 <1.5%, 균등이면 ~2.7%)", mass_of(limp, {"AA", "KK", "QQ"}) < 1.5,
     f"{mass_of(limp, {'AA', 'KK', 'QQ'}):.2f}%")
test("프리플랍 전용: 분류 키가 프리플랍용", list(limp["classes"]) == hr.CLASSES_PRE)
test("프리플랍 분류 합 = 100", abs(sum(limp["classes"].values()) - 100) < 0.5)

# --- 3. 3벳 팟, 턴 체크 → 캡 -------------------------------------------------------------------
B4 = ["Qd", "9s", "3c", "6h"]
pot3 = {"pos": "CO", "preflop": ["open", "call"], "flop": [{"a": "check"}, {"a": "call", "size": 0.33}]}
t_bet = one(dict(pot3), B4)
t_chk = one(dict(pot3, turn=["check"]), B4)
test("3벳팟 턴 체크: 스트롱 핸드 비중 감소(캡)", cl(t_chk, *STRONG) < cl(t_bet, *STRONG) * 0.8,
     f"{cl(t_bet, *STRONG):.1f}% → {cl(t_chk, *STRONG):.1f}%")
test("3벳팟 턴 체크: 슬로플레이 잔존(0 아님)", cl(t_chk, *STRONG) > 0.3 and t_chk["width_pct"] > 0)
test("3벳팟 턴 체크: 탑페어+ 감소", cl(t_chk, "탑페어+") < cl(t_bet, "탑페어+"),
     f"{cl(t_bet, '탑페어+'):.1f}% → {cl(t_chk, '탑페어+'):.1f}%")

# --- 4. 리버 체크레이즈: 양극화 + 강함 -----------------------------------------------------------
B5 = ["Ks", "9h", "4d", "2c", "7s"]
r_base = one({"pos": "BTN", "preflop": ["open"]}, B5)
r_cr = one({"pos": "BTN", "preflop": ["open"], "river": ["check", "raise"]}, B5)
r_cr2 = one({"pos": "BTN", "preflop": ["open"], "river": ["checkraise"]}, B5)
test("체크 뒤 raise = checkraise 와 동일", r_cr["classes"] == r_cr2["classes"])
test("리버 체크레이즈: 강한 핸드 비중 대폭 증가", cl(r_cr, *STRONG, "탑페어+") > cl(r_base, *STRONG, "탑페어+") + 10,
     f"{cl(r_base, *STRONG, '탑페어+'):.0f}% → {cl(r_cr, *STRONG, '탑페어+'):.0f}%")
test("리버 체크레이즈: 중간 페어 거의 없음 (양극화)", r_cr["classes"]["중간·약한 페어"] < r_base["classes"]["중간·약한 페어"] * 0.4
     and r_cr["classes"]["중간·약한 페어"] < r_cr["classes"]["노페어"],
     f"M {r_cr['classes']['중간·약한 페어']}% / air {r_cr['classes']['노페어']}%")
test("리버: 드로우 분류 없음", r_cr["classes"]["드로우"] == 0.0)

# --- 5. 프로필: 콜링스테이션 vs 어그레시브 ------------------------------------------------------
# 프리플랍 스탯은 같게 두고 포스트플랍 성향(fold_to_bet / af)만 다르게 → 포스트플랍 보정만 비교
STATION = {"hands": 300, "vpip": 30, "pfr": 20, "threebet": 6, "fold_to_bet": 20, "af": 0.7, "wtsd": 38, "reliable": True}
MANIAC = dict(STATION, fold_to_bet=62, af=4.5, wtsd=25)
line_bet = {"pos": "CO", "preflop": ["open"], "flop": [{"a": "bet", "size": 0.5}]}
b_st = one(line_bet, BOARD_AR, profile=STATION)
b_mn = one(line_bet, BOARD_AR, profile=MANIAC)
b_none = one(line_bet, BOARD_AR)
test("같은 벳 라인: 콜링스테이션 벳은 더 강하다 (노페어+드로우 적음)",
     cl(b_st, "노페어", "드로우") < cl(b_mn, "노페어", "드로우"),
     f"station {cl(b_st, '노페어', '드로우'):.1f}% vs aggro {cl(b_mn, '노페어', '드로우'):.1f}%")
NEUTRAL = dict(STATION, fold_to_bet=45, af=2.0)
b_neu = one(line_bet, BOARD_AR, profile=NEUTRAL)
test("스테이션 벳: 스트롱 비중이 중립 프로필보다 높다", cl(b_st, *STRONG) >= cl(b_neu, *STRONG) > cl(b_mn, *STRONG),
     f"{cl(b_st, *STRONG):.1f} > {cl(b_neu, *STRONG):.1f} > {cl(b_mn, *STRONG):.1f}")
line_call = {"pos": "CO", "preflop": ["open"], "flop": [{"a": "call", "size": 0.5}]}
c_st = one(line_call, BOARD_AR, profile=STATION)
c_mn = one(line_call, BOARD_AR, profile=MANIAC)
test("같은 콜 라인: 스테이션 콜이 더 약하다 (중간·에어 많음)",
     cl(c_st, "중간·약한 페어", "노페어") > cl(c_mn, "중간·약한 페어", "노페어"),
     f"station {cl(c_st, '중간·약한 페어', '노페어'):.1f}% vs aggro {cl(c_mn, '중간·약한 페어', '노페어'):.1f}%")
unrel = dict(STATION, hands=10, reliable=False)
test("신뢰 못 하는 프로필은 무시", one(line_bet, BOARD_AR, profile=unrel)["classes"] == b_none["classes"])

# --- 6. 죽은 카드 -----------------------------------------------------------------------------
dd = one({"pos": "UTG", "preflop": ["open"]}, ["Ac", "7d", "2c"], dead=["As", "Ah"])
aces = [k for k in dd["range"] if k.startswith("A") and k[2] == "A"]
test("죽은 카드(As,Ah)·보드(Ac) 제외 → AA 콤보 없음(남은 Ad 하나뿐)", not aces, str(aces))
test("레인지에 죽은/보드 카드 없음",
     all(c not in k for k in dd["range"] for c in ("As", "Ah", "Ac", "7d", "2c")), "")
test("죽은 카드: 레인지 합 = 1", abs(sum(dd["range"].values()) - 1) < 1e-9)
d2 = one({"pos": "UTG", "preflop": ["open"]}, [], dead=["Ah", "Ad"])
test("프리플랍 AA 콤보 1개 (Ah,Ad 죽음)", len([k for k in d2["range"] if k[0] == "A" and k[2] == "A"]) == 1,
     str([k for k in d2["range"] if k[0] == "A" and k[2] == "A"]))

# --- 7. 정보 부족 ----------------------------------------------------------------------------
m1 = hr.estimate({"X": {}}, ["As", "7d", "2c"], 9)["X"]
test("빈 좌석 정보: 결과 반환 + 신뢰도 low", m1["confidence"] == "low" and m1["range"] and "error" not in m1, m1["note"])
m2 = one({"pos": "CO"}, [])
test("액션 없음 + 포지션만: low, 분류 합 100", m2["confidence"] == "low" and abs(sum(m2["classes"].values()) - 100) < 0.5)
m3 = one({"pos": "CO", "preflop": ["open"], "turn": ["bet"], "river": ["bet"]}, ["As", "7d", "2c"])
test("보드에 없는 스트리트 액션은 건너뜀(에러 없이)", "error" not in m3 and m3["n_actions"] == 0, m3["note"])
m4 = one({"pos": "ZZ", "preflop": ["weird", {"a": "???"}], "flop": [None, {"a": "bet"}, "fold", {"a": "bet", "size": "x"}]},
         ["As", "7d", "2c"])
test("이상한 액션/포지션도 죽지 않음", "error" not in m4 and abs(sum(m4["range"].values()) - 1) < 1e-9)
m5 = hr.estimate({"A": {"pos": "BTN", "preflop": ["open"]}}, ["As", "zz", "7d", "As", "2c"], 9, dead=["Qq", "Kh"])
test("잘못된/중복 카드는 무시", "error" not in m5["A"])
test("seats 비어 있으면 {}", hr.estimate({}, [], 9) == {} and hr.estimate(None, [], 9) == {})

# --- 8. 프리플랍 전용 --------------------------------------------------------------------------
p1 = one({"pos": "UTG", "preflop": ["open"]}, [])
p2 = one({"pos": "BTN", "preflop": ["open"]}, [])
test("프리플랍 UTG 오픈이 BTN 오픈보다 좁다", p1["width_pct"] < p2["width_pct"], f"{p1['width_pct']} vs {p2['width_pct']}")
# 차트 밖 꼬리(PRE_FLOOR, 최대 PRE_TAIL_MAX)가 생겨 3벳 범위의 프리미엄 비중이 예전(~35%)보다 낮다 → 기준 25 → 20
test("프리플랍 3벳: 프리미엄 페어 비중 큼", one({"pos": "BTN", "preflop": ["3bet"]}, [])["classes"]["프리미엄 페어"] > 20)
test("프리플랍 4벳: 프리미엄 페어 비중 > 오픈", one({"pos": "BTN", "preflop": ["4bet"]}, [])["classes"]["프리미엄 페어"] > one({"pos": "BTN", "preflop": ["open"]}, [])["classes"]["프리미엄 페어"] * 3)
test("프리플랍 summary 표준 표기 (22+, AJs+ 등)", "+" in p1["summary"], p1["summary"])
test("top_hands <= 5, 확률 내림차순", len(p1["top_hands"]) <= 5 and
     all(p1["top_hands"][i][1] >= p1["top_hands"][i + 1][1] for i in range(len(p1["top_hands"]) - 1)))
test("프리플랍 오픈 = 신뢰도 medium", p1["confidence"] == "medium")

# --- 9. 액션 크기/종류 ------------------------------------------------------------------------
sm = one({"pos": "CO", "preflop": ["open"], "flop": [{"a": "bet", "size": 0.25}]}, BOARD_AR)
bg = one({"pos": "CO", "preflop": ["open"], "flop": [{"a": "bet", "size": 0.9}]}, BOARD_AR)
test("작은 벳은 큰 벳보다 넓다(병합)", sm["width_pct"] > bg["width_pct"] * 1.1 and sm["classes"]["중간·약한 페어"] > bg["classes"]["중간·약한 페어"] * 1.5,
     f"{sm['width_pct']} vs {bg['width_pct']}")
test("큰 벳은 중간 페어를 더 많이 뺀다", bg["classes"]["중간·약한 페어"] < sm["classes"]["중간·약한 페어"])
co_base = one({"pos": "CO", "preflop": ["open"]}, BOARD_AR)
ck = one({"pos": "CO", "preflop": ["open"], "flop": ["check"]}, BOARD_AR)
test("체크: 최강 핸드 줄고 중간 페어·에어는 상대적으로 유지", cl(ck, *STRONG) < cl(co_base, *STRONG) * 0.8 and
     cl(ck, *STRONG) > 0 and ck["classes"]["중간·약한 페어"] > co_base["classes"]["중간·약한 페어"],
     f"strong {cl(co_base, *STRONG):.1f}→{cl(ck, *STRONG):.1f}")
cal = one({"pos": "CO", "preflop": ["open"], "flop": [{"a": "call", "size": 0.5}]}, BOARD_AR)
test("콜: 에어 급감, 중간 페어 비중 증가", cal["classes"]["노페어"] < co_base["classes"]["노페어"] * 0.5 and
     cal["classes"]["중간·약한 페어"] > co_base["classes"]["중간·약한 페어"],
     f"air {co_base['classes']['노페어']}→{cal['classes']['노페어']}, M {co_base['classes']['중간·약한 페어']}→{cal['classes']['중간·약한 페어']}")

# 드로우: 플러시 드로우 보드에서 벳 → 드로우 비중 유지
FD = ["Ks", "9s", "4d"]
fd_base = one({"pos": "BTN", "preflop": ["open"]}, FD)
fd_bet = one({"pos": "BTN", "preflop": ["open"], "flop": [{"a": "bet", "size": 0.8}]}, FD)
test("벳 레인지에서 드로우는 에어보다 덜 줄어든다",
     fd_bet["classes"]["드로우"] / fd_base["classes"]["드로우"] > fd_bet["classes"]["노페어"] / fd_base["classes"]["노페어"],
     f"draw {fd_base['classes']['드로우']}→{fd_bet['classes']['드로우']}, air {fd_base['classes']['노페어']}→{fd_bet['classes']['노페어']}")
test("플러시 드로우 보드에 드로우 분류 존재", fd_base["classes"]["드로우"] > 5)

# --- 10. 보드 형식/멀티웨이/스트리트별 보드 ------------------------------------------------------
a = hr.estimate({"A": {"pos": "CO", "preflop": ["open"], "flop": ["bet"], "turn": ["bet"]}}, "Ks9s4d2c", 9)["A"]
b = hr.estimate({"A": {"pos": "CO", "preflop": ["open"], "flop": ["bet"], "turn": ["bet"]}},
                {"flop": ["Ks", "9s", "4d"], "turn": "2c"}, 9)["A"]
c = hr.estimate({"A": {"pos": "CO", "preflop": ["open"], "flop": ["bet"], "turn": ["bet"]}},
                [ev.parse_card(x) for x in ("Ks", "9s", "4d", "2c")], 9)["A"]
test("보드 형식(문자열/dict/정수) 동일 결과", a["classes"] == b["classes"] == c["classes"])
multi = hr.estimate({"s1": {"pos": "UTG", "preflop": ["open"], "flop": ["bet"]},
                     "s2": {"pos": "BTN", "preflop": ["call"], "flop": ["call"]},
                     "s3": {"pos": "BB", "preflop": ["check"], "flop": ["check"]}}, BOARD_AR, 9, dead=["Kh", "Kd"])
test("멀티웨이: 좌석마다 독립 결과", set(multi) == {"s1", "s2", "s3"} and
     len({tuple(r["classes"].values()) for r in multi.values()}) == 3)
t_s = one({"pos": "CO", "preflop": ["open"], "flop": ["bet"], "turn": ["bet"]}, ["Ks", "9s", "4d", "2c"])
f_s = one({"pos": "CO", "preflop": ["open"], "flop": ["bet"]}, ["Ks", "9s", "4d", "2c"])
test("스트리트별 적용: 턴 벳을 더하면 에어 추가 감소", t_s["classes"]["노페어"] < f_s["classes"]["노페어"])
test("confidence: 액션이 많을수록 높다", {"low": 0, "medium": 1, "high": 2}[t_s["confidence"]]
     >= {"low": 0, "medium": 1, "high": 2}[f_s["confidence"]] and t_s["confidence"] == "high", t_s["confidence"])
test("결과 키 완비", all(k in cb for k in ("range", "width_pct", "summary", "classes", "top_hands", "confidence", "note")))
test("range 합 1, 포스트플랍 분류 합 100", abs(sum(cb["range"].values()) - 1) < 1e-9 and abs(sum(cb["classes"].values()) - 100) < 0.6)
test("분류 순서 고정", list(cb["classes"]) == hr.CLASSES_POST)

# 정답성 샘플: 직접 분류
bi = hr._board_info([ev.parse_card(x) for x in ("As", "7d", "2c")])


def K(s):
    return hr._classify(bi, *sorted((ev.parse_card(s[:2]), ev.parse_card(s[2:])), reverse=True))[1]


test("분류: AhKd 탑페어", K("AhKd") == 3)
test("분류: 77 셋", K("7h7c") == 1)
test("분류: A7 투페어", K("Ah7h") == 2)
test("분류: KK 오버페어는 아님(A 보드) → 중간·약한", K("KhKd") == 4)
test("분류: 99 아래 페어 = 중간·약한", K("9h9c") == 4)
test("분류: J5 노페어", K("Jh5d") == 6)
test("분류: 65s 는 A72 에서 노페어/드로우(갓샷 아님) 중 하나", K("6h5h") in (5, 6))
bi2 = hr._board_info([ev.parse_card(x) for x in ("9h", "8h", "2c")])
test("분류: 76 on 98 = 양차 드로우", hr._classify(bi2, ev.parse_card("7d"), ev.parse_card("6d"))[1:] == (5, 1.0))
test("분류: Ah5h on 9h8h = 플러시 드로우", hr._classify(bi2, ev.parse_card("Ah"), ev.parse_card("5h"))[1] == 5)

# --- describe ---------------------------------------------------------------------------------
lines = hr.describe(cb)
test("describe: 2~4줄, 한국어", 2 <= len(lines) <= 4 and any("예상 상위" in l for l in lines) and any("보드 기준" in l for l in lines), "\n".join(lines))
pl = hr.describe(limp)
test("describe(프리플랍): 2~4줄", 2 <= len(pl) <= 4 and any("프리플랍 기준" in l for l in pl))
test("describe: 빈/실패 결과도 안전", len(hr.describe({})) >= 2 and len(hr.describe(hr._fallback({}, [], [], "e"))) >= 2)

# --- 속도 (측정만) ------------------------------------------------------------------------------
print("\n속도 (5장 보드, 3 스트리트 액션)")
hr._BOARD_CACHE.clear()
seat5 = {"pos": "CO", "preflop": ["open"], "flop": [{"a": "bet", "size": 0.5}], "turn": [{"a": "bet", "size": 0.66}],
         "river": [{"a": "bet", "size": 0.75}]}
B5T = ["Ks", "9h", "4d", "2c", "7s"]
for label, pos in (("CO 콜드(캐시 없음)", "CO"),):
    t = time.perf_counter()
    hr.estimate({"V": dict(seat5, pos=pos)}, B5T, 9)
    cold = (time.perf_counter() - t) * 1000
    print(f"  {label}: {cold:.1f} ms")
t = time.perf_counter()
for _ in range(20):
    hr.estimate({"V": seat5}, B5T, 9)
warm = (time.perf_counter() - t) / 20 * 1000
print(f"  동일 보드 재호출(캐시): {warm:.1f} ms / 상대")
wide = {"pos": "BTN", "preflop": ["limp"], "flop": ["check"], "turn": ["check"], "river": [{"a": "bet", "size": 0.5}]}
hr._BOARD_CACHE.clear()
t = time.perf_counter()
hr.estimate({"V": wide}, B5T, 9)
print(f"  넓은 레인지(BTN 림프 ~31%) 콜드: {(time.perf_counter() - t) * 1000:.1f} ms")
hr._BOARD_CACHE.clear()
many = {f"s{i}": dict(seat5, pos=p) for i, p in enumerate(("UTG", "CO", "BTN", "SB", "BB"))}
t = time.perf_counter()
hr.estimate(many, B5T, 9, dead=["Ah", "Qd"])
print(f"  5명 동시(콜드): {(time.perf_counter() - t) * 1000:.1f} ms 총 / {(time.perf_counter() - t) * 200:.1f} ms 상대당")

# --- 회귀: 보드 dict 의 문자열 플랍 --------------------------------------------------------------------
_r1 = hr.estimate({"V": {"pos": "CO", "preflop": ["open"], "flop": ["bet"]}}, {"flop": "As7d2c"}, 9)["V"]
_r2 = hr.estimate({"V": {"pos": "CO", "preflop": ["open"], "flop": ["bet"]}}, {"flop": ["As", "7d", "2c"]}, 9)["V"]
test("board dict flop 문자열 'As7d2c' 도 플랍으로 반영", _r1["n_actions"] == 1 and _r1["range"] == _r2["range"],
     f"{_r1['street']} {_r1['n_actions']}")

# --- 회귀 2026-10-30: 페어 보드에서 '보드 페어 + 내 원페어' 를 투페어로 봐서 '투페어 91%' 로 좁혀짐 ------------------
_bp = hr._board_info([ev.parse_card(x) for x in ("Ah", "9d", "4c", "3d", "9h")])


def KP(s):
    return hr._classify(_bp, *sorted((ev.parse_card(s[:2]), ev.parse_card(s[2:])), reverse=True))[:2]


test("페어 보드 A9439: A4 = 탑페어(+보드 페어) → 탑페어+", KP("As4s") == (hr.T, 3), KP("As4s"))
test("페어 보드 A9439: 88 (보드 아래 포켓) → 중간·약한 페어", KP("8s8c") == (hr.M, 4), KP("8s8c"))
test("페어 보드 A9439: KK (A 아래 포켓) → 중간·약한 페어", KP("KsKc") == (hr.M, 4), KP("KsKc"))
test("페어 보드 A9439: 43 (보드 페어 99 + 44 가 최선) → 중간·약한 페어", KP("4s3s") == (hr.M, 4), KP("4s3s"))
_bq = hr._board_info([ev.parse_card(x) for x in ("Ah", "9d", "4c")])
test("페어 없는 보드 A94: A4 = 진짜 투페어 (그대로)", hr._classify(_bq, ev.parse_card("As"), ev.parse_card("4s"))[:2] == (hr.W, 2))
test("페어 보드 A9439: 9x = 트립스", KP("9s8c")[1] == 1, KP("9s8c"))
_sb = one({"pos": "SB", "preflop": ["blind", "call"], "flop": ["check"], "turn": ["check"], "river": ["bet"]},
          ["Ah", "9d", "4c", "3d", "9h"], players=6, dead=["Kh", "Kc"])
test("SB 블라인드→콜, 체크·체크·리버 벳 (A9439): 투페어 비중이 압도적이지 않음 (< 50%)",
     _sb["classes"]["투페어"] < 50, f"{_sb['classes']} 폭 {_sb['width_pct']}%")
test("SB 블라인드→콜, 체크·체크·리버 벳 (A9439): 범위 폭 5% 이상 (상위 2% 로 좁혀지지 않음)",
     _sb["width_pct"] >= 5, f"{_sb['width_pct']}%")

# --- 주도권: 리드 vs C-벳 / 딜레이드 벳 ------------------------------------------------------
_B = [{"a": "bet", "size": 0.66}]
_lead = one({"pos": "BB", "preflop": ["blind", "call"], "flop": [{"a": "lead", "size": 0.66}]}, ["Kd", "8h", "3c"])
_cbet = one({"pos": "BB", "preflop": ["blind", "call"], "flop": [{"a": "cbet", "size": 0.66}]}, ["Kd", "8h", "3c"])
_plain = one({"pos": "BB", "preflop": ["blind", "call"], "flop": [{"a": "bet", "size": 0.66}]}, ["Kd", "8h", "3c"])
_MADE = ("스트레이트 이상", "셋/트립스", "투페어", "탑페어+", "중간·약한 페어")
test("같은 사이즈 플랍 벳: 리드가 C-벳보다 made 비중 높음", cl(_lead, *_MADE) > cl(_cbet, *_MADE) + 8,
     f"리드 {cl(_lead, *_MADE):.0f}% vs c-bet {cl(_cbet, *_MADE):.0f}%")
test("같은 사이즈 플랍 벳: 리드가 C-벳보다 에어(노페어) 적음", _lead["classes"]["노페어"] < _cbet["classes"]["노페어"] * 0.7,
     f"리드 {_lead['classes']['노페어']}% vs c-bet {_cbet['classes']['노페어']}%")
test("C-벳은 일반 벳보다 넓다 (에어 유지), 리드는 일반 벳보다 좁다",
     _cbet["classes"]["노페어"] > _plain["classes"]["노페어"] > _lead["classes"]["노페어"]
     and _cbet["width_pct"] > _plain["width_pct"] > _lead["width_pct"],
     f"폭 {_cbet['width_pct']} / {_plain['width_pct']} / {_lead['width_pct']}")
test("리드 벳 노트에 설명이 나온다", "콜하고 먼저 벳" in _lead["note"] and "리드 벳" in _lead["note"], _lead["note"])
test("MULT 행: 모든 역할 x 크기", all(f"{r}_{z}" in hr.MULT and f"{r}_{z}" in hr.KIND_KO and f"{r}_{z}" in hr.KIND_NOTE
                                   for r in hr.ROLE_ADJ for z in ("small", "med", "big")))
_seatr = one({"pos": "BB", "preflop": ["blind", "call"], "flop": [{"a": "bet", "size": 0.66}], "bet_role": {"flop": "lead"}},
             ["Kd", "8h", "3c"])
test("좌석 bet_role 로 지정해도 리드와 같다", abs(_seatr["width_pct"] - _lead["width_pct"]) < 1e-6)
_BR = ["Kd", "8h", "3c", "Ts"]       # 턴에 T: 87/T9/QJ 등 좋아진 패
_xb = {"pos": "CO", "preflop": ["open"], "flop": ["check"]}
_delay = one(dict(_xb, turn=[{"a": "delay", "size": 0.66}]), _BR)
_cb_t = one(dict(_xb, turn=[{"a": "cbet", "size": 0.66}]), _BR)
test("플랍 체크 후 딜레이드 턴 벳: 턴에 좋아진 패(탑페어+) 비중이 C-벳보다 높음", cl(_delay, "탑페어+", *STRONG) > cl(_cb_t, "탑페어+", *STRONG) + 3,
     f"{cl(_delay, '탑페어+', *STRONG):.0f}% vs {cl(_cb_t, '탑페어+', *STRONG):.0f}%")
_probe = one({"pos": "BB", "preflop": ["blind", "call"], "flop": ["check"], "turn": [{"a": "probe", "size": 0.66}]}, _BR)
_lead_t = one({"pos": "BB", "preflop": ["blind", "call"], "flop": ["check"], "turn": [{"a": "lead", "size": 0.66}]}, _BR)
test("프로브(체크 넘어온 턴 벳): 좋아진 패가 리드보다 높음", cl(_probe, "탑페어+", *STRONG) > cl(_lead_t, "탑페어+", *STRONG),
     f"{cl(_probe, '탑페어+', *STRONG):.0f}% vs {cl(_lead_t, '탑페어+', *STRONG):.0f}%")
# handlog.to_handread 가 역할을 붙인다
import handlog as _hl
_pos = {"A": "CO", "B": "BB", "C": "BTN"}
def _a(seat, act, **kw):
    return dict(seat=seat, act=act, **kw)
_h1 = _hl.to_handread({"preflop": [_a("A", "raise", raises_before=0), _a("B", "call", raises_before=1)],
                       "flop": [_a("B", "bet"), _a("A", "call")], "turn": [_a("B", "check"), _a("A", "bet"), _a("B", "call")]}, _pos)
test("to_handread: 플랍을 콜러 B가 리드하면 주도권이 B로 넘어가 턴 A의 벳은 lead (cbet 아님)",
     _h1["B"].get("bet_role") == {"flop": "lead"} and _h1["A"].get("bet_role") == {"turn": "lead"},
     str({k: v.get("bet_role") for k, v in _h1.items()}))
_h2 = _hl.to_handread({"preflop": [_a("A", "raise", raises_before=0), _a("B", "call", raises_before=1)],
                       "flop": [_a("B", "check"), _a("A", "bet"), _a("B", "call")],
                       "turn": [_a("B", "bet"), _a("A", "call")]}, _pos)
test("to_handread: 플랍 어그레서 cbet, 턴 콜러가 어그레서에게 선벳 = lead",
     _h2["A"]["bet_role"] == {"flop": "cbet"} and _h2["B"]["bet_role"] == {"turn": "lead"}, str(_h2))
_h3 = _hl.to_handread({"preflop": [_a("A", "raise", raises_before=0), _a("B", "call", raises_before=1)],
                       "flop": [_a("B", "check"), _a("A", "check")],
                       "turn": [_a("B", "bet"), _a("A", "call")]}, _pos)
_h4 = _hl.to_handread({"preflop": [_a("A", "raise", raises_before=0), _a("B", "call", raises_before=1)],
                       "flop": [_a("B", "check"), _a("A", "check")],
                       "turn": [_a("B", "check"), _a("A", "bet"), _a("B", "call")]}, _pos)
test("to_handread: 체크 넘어온 턴 — 콜러 = probe, 어그레서 = delay",
     _h3["B"]["bet_role"] == {"turn": "probe"} and _h4["A"]["bet_role"] == {"turn": "delay"})
_h5 = _hl.to_handread({"preflop": [_a("A", "call", raises_before=0), _a("B", "check")], "flop": [_a("B", "bet")]}, _pos)
test("to_handread: 림프 팟(프리플랍 레이저 없음)은 역할 없음", "bet_role" not in _h5["B"])

# --- 2026-11-05 쇼다운 채점 반영: 프리플랍 꼬리 / 올인 스택 / 체크 배율 / 화면 표기 -----------------------------
_utg = one({"pos": "UTG", "preflop": ["open"]}, [])
_mass = {}
for _k, _p in _utg["range"].items():
    _hi, _lo = hr._cards(_k)
    _mass[hr._combo_code(_hi, _lo)] = _mass.get(hr._combo_code(_hi, _lo), 0) + _p
test("프리플랍 꼬리: UTG 오픈에도 169 핸드 모두 확률 > 0 (72o 포함)", len(_mass) == 169 and _mass.get("72o", 0) > 0,
     f"{len(_mass)} 핸드")
_tail = sum(p for c, p in _mass.items() if c not in rq.villain_range("open", "UTG", 9, 100))
test("프리플랍 꼬리: 차트 밖 질량은 PRE_TAIL_MAX 이하, 그래도 10% 이상", 0.10 < _tail <= hr.PRE_TAIL_MAX + 0.02, f"{_tail:.2f}")
_4b = one({"pos": "CO", "preflop": ["open", "4bet"]}, [])
_4bm = {}
for _k, _p in _4b["range"].items():
    _hi, _lo = hr._cards(_k)
    _4bm[hr._combo_code(_hi, _lo)] = _4bm.get(hr._combo_code(_hi, _lo), 0) + _p
_4bt = sum(p for c, p in _4bm.items() if c not in rq.villain_range("4bet", "CO", 9, 100))
test("좁은 4벳 범위도 꼬리는 PRE_TAIL_MAX 까지만 (아무 패나 4벳으로 보지 않음)", _4bt <= hr.PRE_TAIL_MAX + 0.02, f"{_4bt:.2f}")
_j100 = one({"pos": "SB", "preflop": ["blind", "jam"]}, [])
_j12 = hr.estimate({"X": {"pos": "SB", "preflop": ["blind", "jam"], "stack_bb": 12}}, [], 9, stack_bb=100)["X"]
_j300 = hr.estimate({"X": {"pos": "SB", "preflop": ["blind", "jam"], "stack_bb": 300}}, [], 9, stack_bb=40)["X"]
_j40 = hr.estimate({"X": {"pos": "SB", "preflop": ["blind", "jam"]}}, [], 9, stack_bb=40)["X"]
test("올인 범위는 상대의 올인 금액(12BB)으로 — 내 스택(100BB)보다 훨씬 넓다", _j12["width_pct"] > _j100["width_pct"] + 10,
     f"{_j100['width_pct']} → {_j12['width_pct']}")
test("올인 금액이 내 스택보다 크면 유효 스택(내 스택)으로", abs(_j300["width_pct"] - _j40["width_pct"]) < 1e-6)
_xbase = one({"pos": "CO", "preflop": ["open"]}, ["Kd", "8h", "3c"])
_xchk = one({"pos": "CO", "preflop": ["open"], "flop": ["check"]}, ["Kd", "8h", "3c"])
test("체크: 탑페어+ 비중이 확실히 줄어듦 (체크한 상대의 탑페어+ 실측 13% vs 예측 25%)",
     cl(_xchk, "탑페어+") < cl(_xbase, "탑페어+") * 0.75, f"{cl(_xbase, '탑페어+'):.0f}% → {cl(_xchk, '탑페어+'):.0f}%")
test("요약: 표기한 핸드의 모델 확률과 화면 확률(실측 보정, 5% 단위)",
     cb["summary_pct"] is not None and cb["summary_hit_pct"] == int(5 * round(cb["summary_pct"] * hr.SUMMARY_CAL / 5))
     and cb["summary_hit_pct"] < cb["summary_pct"], f"{cb['summary_pct']} / {cb['summary_hit_pct']}")
_d0 = hr.describe(cb)[0]
test("describe: '외 N%'(= 나머지 전부 덮는다는 오해) 대신 '중 하나일 확률 약 N%'",
     "중 하나일 확률 약" in _d0 and " 외 " not in _d0 and f"약 {cb['summary_hit_pct']}%" in _d0, _d0)
test("요약 코드 집합 = 표기 토큰을 펼친 것", set(cb["summary_codes"]) and all(len(c) in (2, 3) for c in cb["summary_codes"]))

# --- 상대 유형(유저 정보 팝업 LAG/LP/TAG/TP) 포스트플랍 보정 -------------------------------------------------
_sw_style = hr.switches(style_strength=1.0)   # 아래 유형 시험은 세기 1.0 (e815500) 기준
_sw_style.__enter__()
NP, MP, TP_ = "노페어", "중간·약한 페어", "탑페어+"
_STY_BET = {"pos": "CO", "preflop": ["open"], "flop": [{"a": "bet", "size": 0.66}]}
_STY_CALL = {"pos": "BTN", "preflop": ["call"], "opener": "CO", "flop": [{"a": "call", "size": 0.5}]}
_STY_XR = {"pos": "BB", "preflop": ["call"], "flop": ["checkraise"]}
_STY_CHK = {"pos": "BB", "preflop": ["call"], "flop": ["check"]}


def sty(seat, style, board=BOARD_AR, **prof):
    s = dict(seat)
    if style is not None or prof:
        s["profile"] = dict(prof, style=style)
    return hr.estimate({"V": s}, board, 6)["V"]


_b = {t: sty(_STY_BET, t) for t in (None, "LP", "TP", "LAG", "TAG")}
_c = {t: sty(_STY_CALL, t) for t in (None, "LP", "TP", "LAG", "TAG")}
_x = {t: sty(_STY_XR, t, ["Ks", "8d", "4c"]) for t in (None, "LP", "LAG")}
_k = {t: sty(_STY_CHK, t, ["Ks", "8d", "4c"]) for t in (None, "LP", "TP", "LAG")}
_fmt = lambda d, n: " / ".join(f"{t}:{r['classes'][n]}" for t, r in d.items())
test("유형: LP 벳은 LAG 벳보다 에어(노페어)가 적다", cl(_b["LP"], NP) < cl(_b["LAG"], NP) - 10, _fmt(_b, NP))
test("유형: LP·TP 벳은 기본보다 에어 적고 탑페어+ 많음, LAG 벳은 에어 많음",
     cl(_b["LP"], NP) < cl(_b[None], NP) and cl(_b["TP"], NP) < cl(_b[None], NP) < cl(_b["LAG"], NP)
     and cl(_b["LP"], TP_) > cl(_b[None], TP_) > cl(_b["LAG"], TP_), _fmt(_b, TP_))
test("유형: LP 콜은 TP 콜보다 중간·약한 페어와 에어가 많다",
     cl(_c["LP"], MP) > cl(_c["TP"], MP) + 3 and cl(_c["LP"], NP) > cl(_c["TP"], NP), _fmt(_c, MP))
test("유형: TP 콜은 기본보다 탑페어+ 이상이 많다 (약한 패는 폴드)",
     cl(_c["TP"], TP_, *STRONG) > cl(_c[None], TP_, *STRONG) + 3, _fmt(_c, TP_))
test("유형: LAG 콜은 기본보다 조금 넓다 (약한 패 비중 증가, 크지 않게)",
     0 < cl(_c["LAG"], MP, NP) - cl(_c[None], MP, NP) < 10, _fmt(_c, MP))
test("유형: LP 체크레이즈는 셋 이상이 많고, LAG 체크레이즈는 에어가 많다",
     cl(_x["LP"], *STRONG) > cl(_x[None], *STRONG) > cl(_x["LAG"], *STRONG) and cl(_x["LAG"], NP) > cl(_x["LP"], NP),
     _fmt(_x, "셋/트립스"))
test("유형: TP·LAG 체크는 기본보다 약하고, LP 체크엔 강한 패가 더 남는다",
     cl(_k["TP"], TP_, *STRONG) < cl(_k[None], TP_, *STRONG) < cl(_k["LP"], TP_, *STRONG)
     and cl(_k["LAG"], TP_, *STRONG) < cl(_k[None], TP_, *STRONG), _fmt(_k, TP_))
test("유형: TAG 는 벳·콜에서 기본과 같고, 레이즈만 조금 강함",
     _b["TAG"]["classes"] == _b[None]["classes"] and _c["TAG"]["classes"] == _c[None]["classes"]
     and cl(sty(_STY_XR, "TAG", ["Ks", "8d", "4c"]), *STRONG) > cl(_x[None], *STRONG))
test("유형: 보정 크기는 완만 (벳 탑페어+ 차이 10%p 이내, 콜 중간 페어 차이 10%p 이내)",
     all(abs(cl(_b[t], TP_) - cl(_b[None], TP_)) < 10 for t in _b)
     and all(abs(cl(_c[t], MP) - cl(_c[None], MP)) < 10 for t in _c))
test("유형 설명이 note 와 style_note 에 붙음 ('루즈-패시브: 콜이 넓음')",
     _c["LP"]["style_note"].startswith("루즈-패시브: 콜이 넓음") and _c["LP"]["note"].endswith(_c["LP"]["style_note"])
     and _c["LP"]["style"] == "LP", _c["LP"]["note"])
test("유형 이름 변형 ('lp', '루즈-패시브', 좌석 type 필드)도 같은 결과",
     sty(_STY_CALL, "lp")["classes"] == sty(_STY_CALL, "루즈-패시브")["classes"] == _c["LP"]["classes"]
     == hr.estimate({"V": dict(_STY_CALL, type="LP")}, BOARD_AR, 6)["V"]["classes"])
# 유형 없음 = 예전 코드와 완전히 같은 출력 (b538c3d 기준으로 잰 값 고정 + 없음/None/모르는 값이 서로 같음)
_none = [sty(_STY_CALL, None), sty(_STY_CALL, ""), sty(_STY_CALL, "XYZ"), hr.estimate({"V": _STY_CALL}, BOARD_AR, 6)["V"]]
test("유형 없음/빈 값/모르는 값 → 출력 동일, style 키 없음",
     all(r == _none[0] for r in _none) and "style" not in _none[0] and "style_note" not in _none[0])
test("유형 없음 → b538c3d 와 같은 값 (BTN 콜→플랍 콜, A72: 탑페어+ 31.6 / 중간·약한 페어 47.5 / 노페어 13.7)",
     (_none[0]["classes"][TP_], _none[0]["classes"][MP], _none[0]["classes"][NP]) == (31.6, 47.5, 13.7),
     str(_none[0]["classes"]))
_pre_lp = sty({"pos": "CO", "preflop": ["open"]}, "LP", [])
_pre_no = sty({"pos": "CO", "preflop": ["open"]}, None, [])
test("유형은 프리플랍 폭에 영향 없음 (VPIP/PFR 만 프리플랍에 — 이중 반영 방지)",
     _pre_lp["range"] == _pre_no["range"] and "style_note" not in _pre_lp)
# 직접 본 포스트플랍 통계(fold_to_bet/AF)가 있으면 유형 보정은 그 가중치만큼 약해진다
_obs = dict(hands=150, vpip=45, pfr=8, fold_to_bet=45, af=2.0, reliable=True)   # 중립 통계 → 프로필 보정 0
_c_obs_lp = sty(_STY_CALL, "LP", **_obs)
_c_obs_no = sty(_STY_CALL, None, **_obs)
test("직접 관측 통계가 많으면 유형 보정이 약해진다 (150핸드 → 세기 25%)",
     0 < cl(_c_obs_lp, MP) - cl(_c_obs_no, MP) < (cl(_c["LP"], MP) - cl(_c[None], MP)) * 0.5,
     f"{cl(_c_obs_no, MP)} → {cl(_c_obs_lp, MP)}")

_sw_style.__exit__(None, None, None)

# --- 전환 스위치 (MULT_SET / PRE_FLOOR / STYLE_STRENGTH) ---------------------------------------------
_orig_mult = {k: (list(v[0]), v[1]) for k, v in hr.MULT.items()}
_cb_cur = hr._kind_mults_base("cbet_med", 0.0, 0.0)
with hr.switches(mult_set="prev"):
    _cb_prev = hr._kind_mults_base("cbet_med", 0.0, 0.0)
    _prev_mid = hr.MULT["bet_med"]
_cb_back = hr._kind_mults_base("cbet_med", 0.0, 0.0)
test("스위치: prev 로 바꾸면 cbet_med 가 다시 계산되고 (M 0.50 기준), 돌아오면 MULT 가 원본과 같다",
     _prev_mid == ([1.00, 1.00, 1.00, 0.50, 0.30], 0.90) and abs(_cb_prev[0][3] - 0.50 * 1.15) < 1e-12
     and _cb_prev != _cb_cur and _cb_back == _cb_cur and hr.MULT == _orig_mult and hr.MULT_SET == "current",
     f"{_cb_prev[0]} / {_cb_cur[0]}")
hr.MULT_SET = "prev"        # 직접 대입도 동작
_direct = hr._kind_mults_base("bet_big", 0.0, 0.0)[0][3]
hr.MULT_SET = "current"
test("스위치: MULT_SET 직접 대입도 반영", abs(_direct - 0.18) < 1e-12 and abs(hr._kind_mults_base("bet_big", 0, 0)[0][3] - 0.35) < 1e-12)
_old = hr.configure(mult_set="prev", pre_floor=0.2, style_strength=0.3)
_mid = (hr.MULT_SET, hr.PRE_FLOOR, hr.STYLE_STRENGTH)
hr.configure(**_old)
test("스위치: configure 는 이전 값을 돌려주고 switches/configure 후 복원",
     _old == {"mult_set": "current", "pre_floor": 0.07, "style_strength": 0.5} and _mid == ("prev", 0.2, 0.3)
     and (hr.MULT_SET, hr.PRE_FLOOR, hr.STYLE_STRENGTH) == ("current", hr.PRE_FLOOR_DEFAULT, 0.5))
_bad = 0
for kw in ({"mult_set": "x"}, {"pre_floor": -1}, {"style_strength": -0.1}, {"pre_floor": "a"}):
    try:
        hr.configure(**kw)
    except ValueError:
        _bad += 1
test("스위치: 잘못된 값은 ValueError, 상태는 그대로", _bad == 4 and hr.MULT_SET == "current" and hr.STYLE_STRENGTH == 0.5)
try:
    with hr.switches(mult_set="prev", style_strength=0):
        raise RuntimeError
except RuntimeError:
    pass
test("스위치: 예외가 나도 복원", hr.MULT_SET == "current" and hr.STYLE_STRENGTH == 0.5 and hr.PRE_FLOOR == 0.07)

_pre_open = {"pos": "CO", "preflop": ["open"]}
_r_floor = one(_pre_open, [])
with hr.switches(pre_floor=0):
    _r_nofloor = one(_pre_open, [])
test("PRE_FLOOR=0: 차트 밖 패는 0 (기본은 차트 밖 패도 > 0)",
     len(_r_nofloor["range"]) < len(_r_floor["range"]) and len(_r_floor["range"]) >= 1200
     and all(p > 0 for p in _r_nofloor["range"].values()), f"{len(_r_nofloor['range'])} < {len(_r_floor['range'])}")

with hr.switches(style_strength=0):
    _s0 = sty(_STY_CALL, "LP")
test("STYLE 0: 유형 있어도 유형 없음과 출력이 완전히 같고 style 키 없음",
     _s0 == sty(_STY_CALL, None) and "style" not in _s0 and "style_note" not in _s0)
with hr.switches(style_strength=1.0):
    _s1 = sty(_STY_CALL, "LP")
_s05 = sty(_STY_CALL, "LP")
_d0, _d1, _d05 = (cl(r, MP) for r in (_s0, _s1, _s05))
test("STYLE 0.5 (기본): LP 콜 중간 페어 차이가 0 과 1.0 사이", 0 < _d05 - cl(sty(_STY_CALL, None), MP) < _d1 - _d0 and _s05["style"] == "LP",
     f"{_d0} / {_d05} / {_d1}")

# --- 결과 ---------------------------------------------------------------------------------------
failed = 0
for ok, name, info in results:
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info else ""))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
