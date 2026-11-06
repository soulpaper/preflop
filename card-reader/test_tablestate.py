"""tablestate 액션 바 판정 회귀 테스트 (실제 라이브 화면에서 잘라 낸 samples/actionbar/*.png). 실행: python test_tablestate.py

2026-10-30 라이브: 초록 '체크' 버튼 글자를 콜 금액 '41BB' 로 읽어 체크할 수 있는데 '폴드' 를 추천했다.
쇼다운 '공개' 버튼·나무 바닥 무늬를 액션 바로 봐서 내 차례/미리 선택으로 잘못 봤다.
"""
import sys
from pathlib import Path

import cv2
import numpy as np

import ocr
import tablestate

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent / "samples" / "actionbar"
# 게임 창 1200x879 보정의 action_bar (fold/call/raise) 를 잘라 낸 자리 (570, 740) 기준으로 옮긴 것
BAR = {"fold": [13, 55, 189, 64], "call": [215, 55, 189, 64], "raise": [415, 55, 189, 64]}
PW = 228.0
results = []


def check(name, ok, info=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {info}" if info else ""))


def load(name):
    img = cv2.imread(str(HERE / f"bar_{name}.png"))
    assert img is not None, name
    return img


def _seat(key, bet, in_hand=True):
    return {"key": key, "in_hand": in_hand, "bet": bet}


def reader_checks():
    """reader 의 액션 바/미리 계산/카드 기억 판정 (GUI 없이 함수만)."""
    import types
    import reader
    App = reader.App
    # QJo 라이브: 내 림프 1BB, BTN 레이즈 4, BB 4, MP 1 (아직 안 냄). 미리 선택 '콜 3BB' 버튼이 보임
    ts = {"to_act": False, "preaction": True, "bar": "call", "call": 3.0, "can_check": False,
          "seats": [_seat("hero", 1.0), _seat("seat_BL", 4.0), _seat("seat_TC", 4.0), _seat("seat_BR", 1.0),
                    _seat("seat_UL", 0.5, False)], "preflop": {"hero_bet": 1.0}}
    fake = types.SimpleNamespace(table_state=ts, estimated_call=App.estimated_call)
    ctx = App.anticipate_ctx(fake, {"call": None})
    check("미리 계산: 미리 선택 '콜 3BB' 버튼 금액을 그대로 (체크 아님)", ctx["call"] == 3.0 and ctx["can_check"] is False, ctx)
    check("예상 콜 = 최대 벳 4 − 내 림프 1 = 3", App.estimated_call(ts) == 3.0, App.estimated_call(ts))
    # 내 벳을 18 로 잘못 읽어도 버튼이 보이면 버튼 금액
    bad = dict(ts, seats=[_seat("hero", 18.0)] + ts["seats"][1:])
    ctx = App.anticipate_ctx(types.SimpleNamespace(table_state=bad, estimated_call=App.estimated_call), {})
    check("미리 계산: 내 벳 오인식(18)이어도 콜 버튼이 보이면 콜 3 (예전: 체크)", ctx["call"] == 3.0 and not ctx["can_check"], ctx)
    nocall = dict(bad, call=None)
    ctx = App.anticipate_ctx(types.SimpleNamespace(table_state=nocall, estimated_call=App.estimated_call), {})
    check("콜 버튼은 보이는데 금액을 못 읽음 + 바닥 칩 예상 0 → 체크로 보지 않음", not ctx["can_check"], ctx)
    chk = dict(ts, bar="check", call=0.0, can_check=True)
    ctx = App.anticipate_ctx(types.SimpleNamespace(table_state=chk, estimated_call=App.estimated_call), {})
    check("미리 선택 '체크' 버튼 → 체크 가능", ctx["call"] == 0.0 and ctx["can_check"] is True, ctx)
    check("프리플랍 내 낸 칩 = 1BB", App.hero_invested(ts) == 1.0)
    # 상황 추정: 림프 후 레이즈 1단계 → vsopen (저장된 '첫 진입' 대신), 레이즈 없음 → None, 내가 마지막 레이저 → None
    check("상황 추정: 림프 후 레이즈 → vsopen", reader.guess_situation(ts) == "vsopen", reader.guess_situation(ts))
    t3 = dict(ts, seats=[_seat("hero", 3.0), _seat("seat_BL", 9.0), _seat("seat_TC", 1.0)])
    check("상황 추정: 내 오픈 3 → 9 레이즈 = vs3bet", reader.guess_situation(t3) == "vs3bet", reader.guess_situation(t3))
    t0 = dict(ts, seats=[_seat("hero", 0.0), _seat("seat_BL", 1.0), _seat("seat_TC", 1.0)])
    check("상황 추정: 림프뿐 → None", reader.guess_situation(t0) is None)
    tme = dict(ts, seats=[_seat("hero", 4.0), _seat("seat_BL", 4.0), _seat("seat_TC", 1.0)])
    check("상황 추정: 내가 가장 크게 냄 → None", reader.guess_situation(tme) is None)
    # 카드 기억: 칩에 리버 한 장이 가려져도 보드 5장 유지, 내 카드 1장만 보여도 2장 유지, 다른 카드면 새 핸드
    app = types.SimpleNamespace(memory={})
    board = ["Th", "Ah", "9d", "6h", "3c"]
    App.hold(app, "board", board)
    check("가려진 리버: 보드 4장으로 보여도 5장 유지", App.hold(app, "board", board[:4]) == board)
    check("가운데 카드가 가려짐: 4장(9♦ 빠짐)도 유지", App.hold(app, "board", ["Th", "Ah", "6h", "3c"]) == board)
    App.hold(app, "hero", ["7h", "As"])
    check("기울어진 내 카드: 1장만 보여도 2장 유지", App.hold(app, "hero", ["As"]) == ["7h", "As"])
    check("다른 카드가 보이면 새 핸드", App.hold(app, "hero", ["Kd"]) == ["Kd"])
    check("보드가 다 사라지면 기억 지움", App.hold(app, "board", []) == [] and App.hold(app, "board", ["2c", "2d", "2h"])
          == ["2c", "2d", "2h"])


ORDER = ["seat_BL", "seat_L", "seat_UL", "seat_TL", "seat_TR", "seat_UR", "seat_R", "seat_BR", "hero"]  # 시계 방향, 나는 마지막


def _seats(dealt, folded=(), occupied=None):
    occ = set(ORDER if occupied is None else occupied)
    return [{"key": k, "dealt": k in dealt, "folded": k in folded, "occupied": k in occ} for k in ORDER]


def _pos(tr, dealt, dealer, discs=None, folded=()):
    order = _seats(dealt, folded)
    return tr._hand_positions(order, [s for s in order if s["dealt"]], dealer, dict(discs or {}), [])


def order_checks():
    """핸드마다 한 번 정하는 포지션 · 고정 버튼 자리 · 다음 차례 좌석 지켜보기 (2026-11-06 녹화에서 찾은 문제들)."""
    import types
    import reader
    TR = tablestate.TableReader
    # 딜링 첫 프레임: 블라인드 두 사람만 카드가 보임 (딜러는 아직 없음) → 헤즈업이 아니라 SB, BB 순서 그대로
    got = TR._positions([{"key": k} for k in ORDER], [{"key": "seat_TR"}, {"key": "seat_UR"}], "seat_TL", {}, [])
    check("딜링 첫 프레임 2명 (딜러 빠짐) → SB, BB 순서", got == {"seat_TR": "SB", "seat_UR": "BB"}, got)
    got = TR._positions([{"key": k} for k in ORDER], [{"key": "seat_UR"}, {"key": "hero"}], "hero", {}, [])
    check("진짜 헤즈업 (딜러 = SB) 은 그대로", got == {"hero": "SB", "seat_UR": "BB"}, got)

    tr = TR()
    full = ORDER[:]
    pos, n = _pos(tr, full, "seat_TL")
    check("9명: 딜러 TL → TR SB, UR BB, R UTG", (pos["seat_TL"], pos["seat_TR"], pos["seat_UR"], pos["seat_R"], n)
          == ("BTN", "SB", "BB", "UTG", 9), (pos, n))
    first = dict(pos)
    # 핸드 중 한 사람이 화면에서 빠짐 (공개 카드가 아바타를 가림 등) → 이름이 밀리지 않음
    pos, n = _pos(tr, [k for k in full if k != "seat_R"], "seat_TL", folded=["seat_BL"])
    check("핸드 중 8명으로 보여도 포지션 고정", n == 9 and all(pos[k] == first[k] for k in pos) and "seat_R" not in pos, (pos, n))
    # 딜러 원판이 잠깐 안 보여도 유지
    pos, n = _pos(tr, full, None, folded=["seat_BL"])
    check("딜러 원판이 가려져도 포지션 유지", pos == first and tr._lock["dealer"] == "seat_TL", pos)
    # 뒤늦게 '카드 받음' 으로 보인 빈자리 (이미 폴드가 있고 LOCK_GROW 번 지남) → 무시
    for _ in range(tablestate.LOCK_GROW):
        _pos(tr, full, "seat_TL", folded=["seat_BL"])
    tr._lock["keys"].discard("seat_L")
    tr._lock["pos"].pop("seat_L")
    pos, n = _pos(tr, full, "seat_TL", folded=["seat_BL"])
    check("늦은 오판 '카드 받음' 은 포지션 없음 (다른 사람 그대로)", "seat_L" not in pos and pos["seat_R"] == "UTG", pos)
    # 딜러가 움직임 = 새 핸드 → 다시 계산
    pos, n = _pos(tr, full, "seat_TR")
    check("딜러 이동 → 새 포지션", pos["seat_TR"] == "BTN" and pos["seat_UR"] == "SB", pos)
    # 핸드 사이 (카드 받은 사람 2명 미만) → 고정 풀림
    _pos(tr, [], "seat_TR")
    check("핸드 사이 → 고정 풀림", tr._lock is None)
    # 딜링 중: 처음엔 4명만 보이고 다음 프레임에 모두 → 처음 LOCK_GROW 번 안이라 다시 계산
    tr = TR()
    _pos(tr, ["seat_R", "seat_BR", "hero", "seat_BL"], "seat_UR", {"SB": "seat_R", "BB": "seat_BR"})
    pos, n = _pos(tr, full, "seat_UR")
    check("딜링 중 늘어난 사람 → 다시 계산 (9명 이름)", n == 9 and pos["seat_BL"] == "U1" and pos["hero"] == "UTG", (pos, n))
    # SB 원판 좌석은 카드가 아직 안 보여도 넣는다 (예전: 첫 프레임에 BB 가 'SB' → 'BB 0.5 블라인드 → 1 콜' 기록)
    tr = TR()
    dealt = [k for k in full if k != "hero"]
    pos, n = _pos(tr, dealt, "seat_BR", {"SB": "hero", "BB": "seat_BL"})
    check("SB(나) 카드가 아직 안 보여도 BB 는 BB", pos.get("seat_BL") == "BB" and "hero" not in pos and n == 9, (pos, n))
    # 딜러를 못 찾아 BB 원판으로 정한 포지션 → 딜러가 보이면 딜러 기준으로 다시
    tr = TR()
    pos, _ = _pos(tr, full, None, {"BB": "seat_UR"})
    check("딜러 없음 → BB 원판으로", pos.get("seat_UR") == "BB" and tr._lock["src"] == ("BB", "seat_UR"), pos)
    pos, _ = _pos(tr, full, "seat_UL")
    check("그 뒤 딜러가 보이면 딜러 기준", pos.get("seat_UL") == "BTN" and pos.get("seat_TR") == "BB", pos)

    # 딜러 버튼 → 좌석: 사람이 있는지와 상관없이 가장 가까운 고정 자리 (가려진 딜러 아바타)
    seats = [{"key": "seat_R", "occupied": False, "cfg": {"bet_text": (946, 441, 91, 27)}},
             {"key": "seat_BR", "occupied": True, "cfg": {"bet_text": (879, 559, 91, 27)}}]
    got = TR._button_seat((1001, 488), seats)
    check("버튼 → 가장 가까운 고정 자리 (그 자리가 비어 보여도)", got == "seat_R", got)

    # 버튼 찾기: 배운 자리만 확인, 모든 좌석 자리를 알면 펠트 전체는 DEALER_FULL_EVERY 프레임마다
    pw = 228.0
    t = tablestate._template(tablestate._D_B64, tablestate._D_SHAPE, pw, 1.0)
    rng = np.random.default_rng(1)
    base = rng.integers(40, 80, (600, 900)).astype(np.uint8)

    def with_d(x, y):
        g = base.copy()
        g[y - t.shape[0] // 2:y - t.shape[0] // 2 + t.shape[0], x - t.shape[1] // 2:x - t.shape[1] // 2 + t.shape[1]] = t
        return g
    geom = {"ring": [450, 300, 420, 260]}
    tr = TR()
    big = []
    orig = cv2.matchTemplate

    def mt(win, tpl, method):
        if win.shape[0] > 200 and tpl.shape == t.shape:   # 펠트 전체 찾기 1번 (배율 1.0 템플릿)
            big.append(win.shape)
        return orig(win, tpl, method)
    cv2.matchTemplate = mt
    try:
        a = tr._dealer(with_d(200, 200), geom, pw, ["a", "b"])
        tr._spots["a"] = tr.dealer_at
        b = tr._dealer(with_d(600, 350), geom, pw, ["a", "b"])
        tr._spots["b"] = tr.dealer_at
        n_learn = len(big)
        c = tr._dealer(with_d(200, 200), geom, pw, ["a", "b"])
        check("버튼이 배운 자리로 옮김 → 펠트 전체를 안 찾음", c and abs(c[0][1][0] - 200) < 3 and len(big) == n_learn,
              (c, len(big) - n_learn))
        misses = [bool(tr._dealer(base, geom, pw, ["a", "b"])) for _ in range(6)]
        check("버튼이 안 보임 (자리를 모두 앎) → 펠트 전체는 3프레임에 한 번", not any(misses) and len(big) - n_learn == 2,
              len(big) - n_learn)
        before = len(big)
        tr._dealer(base, geom, pw, ["a", "b", "c"])
        tr._dealer(base, geom, pw, ["a", "b", "c"])
        check("자리를 모르는 좌석이 있으면 매 프레임 펠트 전체", len(big) == before + 2, len(big) - before)
        check("처음/모르는 자리는 펠트 전체에서 찾음", a and b and abs(b[0][1][0] - 600) < 3, (a, b))
    finally:
        cv2.matchTemplate = orig

    # 상대 좌석 영역 (아바타 가운데·말풍선·벳): 나는 빼고, 말풍선 칸은 차례 고리(1.45r) 바깥
    calib = {"geom": {"pill_center": [600.0, 364.0], "pw": pw, "ring": [600.0, 478.0, 478.8, 273.6], "size": [1200, 900]},
             "my_stack": [459, 738, 80, 29], "seats": []}
    tr = TR()
    regs = tr.action_regions(calib)
    cfg = {s["key"]: s for s in tr._slot_cfg(calib)}
    ok = "hero" not in regs and len(regs) >= 8
    for k, rs in regs.items():
        ax, ay, ar = cfg[k]["avatar"]
        for x, y, w, h in rs[1:-1]:
            ok = ok and (x >= ax + 1.45 * ar or x + w <= ax - 1.45 * ar)
    check("좌석 영역: 상대만, 말풍선 칸은 고리 바깥", ok, regs.get("seat_R"))

    # SeatWatch: 마지막으로 읽은 화면과 비교, 다음 차례 좌석만 찍어 보고 멈춘 뒤에 True
    img = rng.integers(0, 255, (300, 400, 3)).astype(np.uint8)
    regions = {"s1": [(10, 10, 40, 40)], "s2": [(200, 20, 40, 40)]}
    w = reader.SeatWatch()
    check("SeatWatch: 처음엔 바뀜", w.changed(img, regions))
    w.accept()
    check("SeatWatch: 같은 화면 → 안 바뀜", not w.changed(img.copy(), regions))
    moved = img.copy()
    cv2.putText(moved, "2.5", (202, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    check("SeatWatch: 그 좌석 영역에 글자 → 바뀜", w.changed(moved, regions))
    w.changed(img, regions)
    w.target((1000, 500, 400, 300), ["s2", "nope"])
    check("SeatWatch: 지켜볼 영역 = 그 좌석 (화면 좌표)", w.box == (1200, 520, 40, 40) and list(w.parts) == ["s2"], w.box)
    screen = {"img": img}
    real_grab = reader.grab
    reader.grab = lambda sct, r: screen["img"][r[1] - 500:r[1] - 500 + r[3], r[0] - 1000:r[0] - 1000 + r[2]].copy()
    try:
        quiet = [w.poll(None) for _ in range(3)]
        screen["img"] = moved
        p1, p2 = w.poll(None), w.poll(None)
    finally:
        reader.grab = real_grab
    check("SeatWatch.poll: 그대로면 False, 바뀐 뒤 멈추면 True (첫 번째는 움직이는 중일 수 있어 False)",
          not any(quiet) and not p1 and p2, (quiet, p1, p2))
    # 지켜볼 좌석 고르기: 차례 고리 좌석 + 행동 순서상 다음, 다음이 나면 거기서 멈춤
    app = types.SimpleNamespace(tracker=types.SimpleNamespace(upcoming=lambda after=None, n=2: ["s2", "hero", "s9"]))
    ww = types.SimpleNamespace(target=lambda rect, keys: setattr(ww, "keys", keys))
    reader.App.watch_next(app, ww, (0, 0, 10, 10), {"acting_seat": "s1"})
    check("지켜볼 좌석: 고리 좌석 + 다음", ww.keys == ["s1", "s2"], ww.keys)
    reader.App.watch_next(app, ww, (0, 0, 10, 10), {"acting_seat": "hero"})
    check("내 차례면 상대 좌석은 안 봄", ww.keys == [], ww.keys)
    app.tracker = types.SimpleNamespace(upcoming=lambda after=None, n=2: ["hero", "s3"])
    reader.App.watch_next(app, ww, (0, 0, 10, 10), {"acting_seat": None})
    check("다음이 나면 안 봄 (액션 바가 잡음)", ww.keys == [], ww.keys)


def order_guard_checks():
    """순서 확인용 (handlog.OrderGuard): 버튼으로 정한 포지션은 원판이 달라도 유지 (state disc_conflict), 다시 계산한 후보
    (_rebuild), 블라인드 좌석으로 포지션 (positions_for), 후보로 고정 바꾸기 (adopt), 전부 다시 읽기의 버튼 찾기 (full),
    소수점을 놓친 OCR ('05BB' = 0.5)."""
    TR = tablestate.TableReader
    full = ORDER[:]
    tr = TR()
    first, _ = _pos(tr, full, "seat_TL")
    pos, _ = _pos(tr, full, None, {"SB": "seat_UR", "BB": "seat_R"}, folded=["seat_BL"])
    check("버튼으로 정한 포지션: 버튼이 가려지고 원판이 달라 보여도 유지 (예전: 원판으로 바꿔 2프레임 내 포지션 BB)",
          pos == first and tr._lock["dealer"] == "seat_TL", pos)
    tr2 = TR()
    _pos(tr2, full, None, {"BB": "seat_UR"})
    pos, _ = _pos(tr2, full, None, {"BB": "seat_R"})
    check("원판으로 정한 포지션은 새 원판을 따름", pos.get("seat_R") == "BB", pos)
    order = [{"key": k, "dealt": True, "folded": False, "occupied": True} for k in full]
    rb = tr._rebuild(order, order, "seat_UL", {})
    check("다시 계산한 후보: 이번 버튼 기준", rb and rb["src"] == "D" and rb["pos"]["seat_UL"] == "BTN" and rb["dealer"] == "seat_UL",
          rb)
    check("버튼·원판이 없으면 후보 없음", tr._rebuild(order, order, None, {}) is None)
    got = TR.positions_for(full, full, sb="seat_TR")
    check("positions_for: SB 좌석으로", got.get("seat_TR") == "SB" and got.get("seat_UR") == "BB" and got.get("seat_TL") == "BTN",
          got)
    check("adopt: 고정 바꿈", tr.adopt(rb) and tr._lock["pos"] == rb["pos"] and tr._lock["dealer"] == "seat_UL")
    pos, _ = _pos(tr, full, "seat_UL", folded=["seat_BL"])
    check("adopt 뒤 같은 버튼 → 그대로", pos == rb["pos"], pos)
    # 전부 다시 읽기: 배운 자리를 모두 알아도 펠트 전체에서 찾음
    pw = 228.0
    t = tablestate._template(tablestate._D_B64, tablestate._D_SHAPE, pw, 1.0)
    base = np.random.default_rng(2).integers(40, 80, (600, 900)).astype(np.uint8)
    g = base.copy()
    g[300 - t.shape[0] // 2:300 - t.shape[0] // 2 + t.shape[0], 700 - t.shape[1] // 2:700 - t.shape[1] // 2 + t.shape[1]] = t
    geom = {"ring": [450, 300, 420, 260]}
    tr = TR()
    tr._spots = {"a": ((200.0, 200.0), 1.0), "b": ((600.0, 350.0), 1.0)}
    tr._d_miss = 1
    quick = tr._dealer(base, geom, pw, ["a", "b"])
    found = tr._dealer(g, geom, pw, ["a", "b"], full=True)
    check("full: 배운 자리에 없으면 바로 펠트 전체", not quick and found and abs(found[0][1][0] - 700) < 3, (quick, found))
    check("OCR '05BB' = 0.5BB (예전: '058B' 와 글자 수를 맞춰 58BB)", ocr.candidates("05BB") == (0.5, "BB", True)
          and tablestate._resolve(["05BB", "05BB", "058B"], "BB", 4) == (0.5, "BB"), ocr.candidates("05BB"))
    check("OCR '10.5BB'·'0BB'·'105BB' 그대로", ocr.candidates("10.5BB")[0] == 10.5 and ocr.candidates("0BB")[0] == 0.0
          and ocr.candidates("105BB")[0] == 105.0)
    # 순서 문제 증거 저장 (reader.App.save_order_issue): hands/order_issues/<날짜>/ 에 이미지+JSON, 하루 ORDER_ISSUE_CAP 건
    import json as _json
    import shutil
    import tempfile
    import types
    import reader
    tmp = Path(tempfile.mkdtemp(prefix="order_issues_test_"))
    old_base = reader.BASE
    reader.BASE = tmp
    try:
        fake = types.SimpleNamespace(order_saved={})
        img = np.zeros((40, 60, 3), np.uint8)
        for i in range(reader.ORDER_ISSUE_CAP + 3):
            reader.App.save_order_issue(fake, {"key": f"20261106_001532-0001_{i}", "codes": ["order_blind"]}, img)
        reader.App.save_order_issue(fake, {"key": "20261106_001532-0001_0", "outcome": "order_healed"}, None)
        reader.App.save_order_issue(fake, {"key": "../escape", "codes": ["x"]}, img)
        days = list((tmp / "hands" / reader.ORDER_ISSUE_DIR).iterdir())
        jpgs = list(days[0].glob("*.jpg")) if days else []
        first = _json.loads((days[0] / "20261106_001532-0001_0.json").read_text(encoding="utf-8")) if days else {}
        check("증거 저장: 날짜 폴더 하나, 하루 최대 개수, 결과는 같은 JSON 에 다시 씀, 이상한 키는 무시",
              len(days) == 1 and len(jpgs) == reader.ORDER_ISSUE_CAP and first.get("outcome") == "order_healed"
              and not list(tmp.rglob("escape*")), (len(days), len(jpgs), first))
    finally:
        reader.BASE = old_base
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tr = tablestate.TableReader()
    have_ocr = ocr.available()
    cases = [
        # 이름, to_act, preaction, bar, can_check, call (None = 확인 안 함)
        ("check_turn", True, False, "check", True, 0.0),
        ("check_preaction", False, True, "check", True, 0.0),
        ("call1_turn", True, False, "call", False, 1.0),
        ("call1_preaction", False, True, "call", False, 1.0),
        ("call6.2_turn", True, False, "call", False, 6.2),
        ("show_buttons", False, False, None, False, None),
        ("no_bar", False, False, None, False, None),
    ]
    for name, act, pre, kind, chk, call in cases:
        st = tr.read_bar(load(name), BAR, PW)
        ok = st["to_act"] == act and st["preaction"] == pre and st["bar"] == kind and st["can_check"] == chk
        if kind == "check":
            ok = ok and st["call"] == 0.0
        elif kind is None:
            ok = ok and st["call"] is None
        elif have_ocr and call is not None:
            ok = ok and st["call"] is not None and abs(st["call"] - call) < 1e-6
        check(f"액션 바 {name}: 내 차례 {act} · 미리 선택 {pre} · {kind} · 체크 {chk} · 콜 {call}", ok, repr(st))
    # 체크 버튼은 절대 콜 금액을 만들지 않는다 (예전: '체크' 글자 → 41BB)
    st = tr.read_bar(load("check_turn"), BAR, PW)
    check("체크 버튼 → 콜 0, '41BB' 아님", st["call"] == 0.0 and st["can_check"], repr(st))
    # 나무 바닥 무늬가 더 거칠어도 (다른 창 크기·밝기에서 std > 12) 액션 바가 아님
    wood = load("no_bar").astype(np.float32)
    rough = np.clip((wood - wood.mean()) * 2.2 + wood.mean(), 0, 255).astype(np.uint8)
    gray = cv2.cvtColor(rough, cv2.COLOR_BGR2GRAY)
    x, y, w, h = BAR["call"]
    std = float(gray[y:y + h, x:x + w].std())
    st = tr.read_bar(rough, BAR, PW)
    check("거친 나무 바닥 (버튼 칸 std > 12) → 내 차례 아님", std > tablestate.BUTTON_STD and not st["to_act"]
          and not st["preaction"] and st["bar"] is None, f"std {std:.1f} {st}")
    # 체크 버튼 색을 파랑으로 바꾸면 '콜' 로 본다 (색 판정이 실제로 갈라 주는지)
    img = load("check_turn")
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    x, y, w, h = BAR["call"]
    sub = hsv[y:y + h, x:x + w]
    teal = (sub[:, :, 0] >= 70) & (sub[:, :, 0] < 100) & (sub[:, :, 1] >= 60)
    sub[:, :, 0][teal] = 112
    kind = tablestate.TableReader._mid_kind(hsv, BAR["call"])
    check("색 판정: 청록 → 체크, 파랑 → 콜", kind == "call", kind)
    reader_checks()
    order_checks()
    order_guard_checks()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
