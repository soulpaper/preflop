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
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
