"""handread 테스트 — python test_handread.py   (속도는 측정값만 출력, 단언하지 않는다)"""
import sys
import time

import evaluator as ev
import handread as hr

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
test("UTG 오픈+큰 c-bet: 중간·약한 페어 급감", cb["classes"]["중간·약한 페어"] < base["classes"]["중간·약한 페어"] * 0.6)
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
test("프리플랍 3벳: 프리미엄 페어 비중 큼", one({"pos": "BTN", "preflop": ["3bet"]}, [])["classes"]["프리미엄 페어"] > 25)
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

# --- 결과 ---------------------------------------------------------------------------------------
failed = 0
for ok, name, info in results:
    failed += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info else ""))
print(f"\n{len(results) - failed}/{len(results)} passed")
assert failed == 0, f"{failed} failed"
