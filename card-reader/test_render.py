"""실제 App 화면 그리기 점검 — 프리플랍/플랍/턴/리버, 내 차례 앞뒤. 그리기 예외가 나면 실패 (창은 config.json 의 보조 모니터 위치).
헤즈업 상대 포지션과 3명 팟 참고 줄은 실제 advice_ctx → recommend → render 경로로 그려서 화면 글자까지 확인한다."""
import sys

import advisor
import evaluator as ev
import reader
import solverlib

REF = "3명 → 1명 근사"


def state(app, hero, board, opponents=1):
    app.view = {"hero": hero, "board": board}
    h, b = tuple(ev.parse_card(c) for c in hero), tuple(ev.parse_card(c) for c in board)
    app.result = reader.analyze(list(h), list(b), opponents, 2000)
    app.result_state = app.computed_state = (h, b, app.opponents())
    app.computing = False


def seat(key, pos, in_hand=True, bet=None, bubble=None):
    return {"key": key, "slot": key.replace("seat_", ""), "pos": pos, "occupied": True, "dealt": True, "in_hand": in_hand,
            "folded": not in_hand, "allin": False, "bet": bet, "stack": None, "bubble": bubble}


def table(opps, call=0.0):
    seats = opps + [seat("hero", "BB")]
    return {"to_act": True, "can_check": not call, "call": call, "bar": "call" if call else "check", "hero_in_hand": True,
            "layout_ok": True, "hero_pos": "BB", "pos_players": 6, "players": 6, "seats": seats, "preflop": None,
            "opponents_in_hand": sum(1 for x in opps if x["in_hand"])}


def canvas_text(app):
    c = app.canvas
    return " ".join(c.itemcget(i, "text") for i in c.find_all() if c.type(i) == "text")


def fake_gto(ctx):
    return {"action": "벳", "size_bb": 2.0, "size_text": "팟의 33%", "reason": "GTO: 벳 33% 70% · 체크 30%", "detail": ["솔버"],
            "confidence": "low", "color_key": "raise", "plan": [], "source": "gto",
            "gto": {"actions": {"bet33": 0.7, "check": 0.3}, "exact_flop": True, "mapped_flop": "Ks7h2d", "street": "flop"}}


def postflop_cases(app):
    """실제 그리기로: 헤즈업 상대 포지션 / 3명 팟 참고 줄 (플랍·턴) / 약한 패·깊은 스택·상대 3명엔 줄 없음 / 지난 선택값 없음."""
    fails = []
    saved = advisor.GTO_ENABLED, solverlib.gto_advice, app.cfg.get("preflop") or {}, app.track, getattr(app, "pre_auto_memo", None)
    advisor.GTO_ENABLED, solverlib.gto_advice = True, fake_gto
    app.cfg["preflop"] = {"players": 6, "situation": "vsopen", "hero": "CO", "villain": "UTG"}   # 지난 핸드의 저장 선택값
    app.declog = None   # 점검이 결정 기록·스크린샷을 남기지 않게
    flop = ["Kh", "7d", "2c"]
    turn = flop + ["5s"]

    def run(name, hero, board, opps, call, opponents, stack, want_line, want_vp=None):
        advisor.gto_cache_clear()
        app.track = {"hist": {"hero_cards": tuple(hero), "street": {3: 1, 4: 2}[len(board)], "reliable": True, "pre_raises": 1,
                              "pre_seen": True, "opener": "seat_TC", "flop": [], "opp_raise": False,
                              "last_aggr": {"preflop": "seat_TC"}, "pos": {}, "live": []}}
        app.pre_auto_memo = None
        app.set_input("my_stack", f"{stack}BB")
        app.set_input("pot", "9BB")
        app.set_input("bet", f"{call}BB" if call else "")
        app.table_state = table(opps, call)
        state(app, hero, board, opponents)
        app.advice_key = None
        app.compute_advice()
        app.render(force=True)
        app.root.update()
        ctx = app.advice_ctx()
        shown = REF in canvas_text(app)
        detail = (app.advice or {}).get("detail") or []
        ok = shown == want_line and any(REF in d for d in detail) == want_line and ctx["villain_pos"] == want_vp and len(detail) <= 3
        print("ok  " if ok else "FAIL", name, "| 상대", ctx["villain_pos"], "| 참고", ctx.get("ref3_villain_pos"), "|", detail)
        if not ok:
            fails.append(name)

    two = [seat("seat_TC", "BTN", bet=None), seat("seat_UL", "SB")]
    three = two + [seat("seat_R", "CO")]
    try:
        run("헤즈업 플랍", ["Ah", "Kd"], flop, [seat("seat_TC", "BTN"), seat("seat_UL", "SB", in_hand=False)], 0, 1, 33, False, "BTN")
        run("헤즈업: 지난 계산 상대 수 2 + 지금 1명", ["Ah", "Kd"], flop, [seat("seat_TC", "BTN"), seat("seat_UL", "SB", in_hand=False)],
            0, 2, 33, False, "BTN")
        run("헤즈업 포지션 모름 → 저장 선택값 안 씀", ["Ah", "Kd"], flop, [seat("seat_TC", None)], 0, 1, 33, False, None)
        run("3명 플랍 강한 패 (체크)", ["Ah", "Kd"], flop, two, 0, 2, 33, True)
        run("3명 턴 강한 패 (벳 받음)", ["Ah", "Kd"], turn, [seat("seat_TC", "BTN", bet=4.0), seat("seat_UL", "SB")], 4.0, 2, 33, True)
        run("3명 약한 패 → 줄 없음", ["9c", "3d"], flop, two, 0, 2, 33, False)
        run("3명 깊은 스택 → 줄 없음", ["Ah", "Kd"], flop, two, 0, 2, 200, False)
        run("상대 3명 → 줄 없음", ["Ah", "Kd"], flop, three, 0, 3, 33, False)
        # postflop_ctx 가 실패해도 지난 핸드 선택값이 새지 않고, 그대로 그려진다
        keep = app.postflop_ctx

        def boom(*a):
            raise RuntimeError("test")
        app.postflop_ctx = boom
        app.table_state = table(two)
        state(app, ["Ah", "Kd"], flop, 2)
        ctx = app.advice_ctx()
        app.postflop_ctx = keep
        app.compute_advice()
        app.render(force=True)
        app.root.update()
        ok = ctx["villain_pos"] is None and ctx["situation"] is None
        print("ok  " if ok else "FAIL", "postflop_ctx 실패 → 지난 선택값 없음", ctx["villain_pos"], ctx["situation"])
        if not ok:
            fails.append("postflop_ctx 실패")
    finally:
        advisor.GTO_ENABLED, solverlib.gto_advice, app.cfg["preflop"], app.track, app.pre_auto_memo = saved
        advisor.gto_cache_clear()
    return fails


def icm_case(app):
    """ICM 켜지는 경로: max_left 0 + 참가자 수 빈칸이어도 머리글을 읽으면 자동 추정으로 켜짐. 머리글을 못 읽으면 꺼짐 (tournament 없음)."""
    t = app.tcfg()
    t.update(max_left=0, entrants="", paid="", entrants_auto=False)
    app.t_max_left = 0
    app.t_set("entrants", "")
    app.t_set("paid", "")
    app.tourney = None
    off = app.tournament_ctx() is None and app.icm_line() is None
    app.tourney_queue.put({"rank": (43, 145), "prize": (500000000, False)})
    app.apply_tourney()
    d = app.tournament_ctx() or {}
    on = d.get("players_left") == 145 and d.get("entrants") == 145 and d.get("my_rank") == 43
    app.table_state = table([seat("seat_TC", "BTN"), seat("seat_UL", "SB", in_hand=False)])
    state(app, ["Ah", "Kd"], ["Kh", "7d", "2c"])
    app.compute_advice()
    app.render(force=True)
    app.root.update()
    ctx = app.advice_ctx()
    drawn = "ICM:" in canvas_text(app) and bool(ctx.get("tournament"))
    print("ok  " if off and on and drawn else "FAIL", "ICM 머리글 없음 → 꺼짐", off, "| 있음 → 자동 추정으로 켜짐", on, "| 그려짐", drawn, app.icm_line())
    app.tourney_queue.put({"clear": True})
    app.apply_tourney()
    return [] if off and on and drawn else ["icm"]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    reader.save_config = lambda *a, **k: None   # 점검이 config.json 을 바꾸지 않게
    app = reader.App()
    print("window", app.root.geometry())
    fails = 0
    cases = [("preflop", []), ("flop", ["7h", "4s", "2d"]), ("turn", ["7h", "4s", "2d", "Jc"]),
             ("river", ["7h", "4s", "2d", "Jc", "9d"])]
    for name, board in cases:
        for ts in (None, {"to_act": True, "can_check": True, "call": 0.0, "bar": "check", "hero_in_hand": True},
                   {"to_act": True, "can_check": False, "call": 3.0, "bar": "call", "hero_in_hand": True}):
            try:
                state(app, ["Ah", "Kd"], board)
                app.table_state = ts
                app.compute_advice()
                app.render(force=True)
                app.root.update()
                print("ok  ", name, (ts or {}).get("bar"))
            except Exception as e:
                import traceback
                traceback.print_exc()
                print("FAIL", name, (ts or {}).get("bar"), repr(e))
                fails += 1
    try:
        fails += len(postflop_cases(app))
        fails += len(icm_case(app))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("FAIL postflop_cases", repr(e))
        fails += 1
    app.root.destroy()
    print("render fails:", fails)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
