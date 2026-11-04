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


# 사용자 불만 화면 (2026-11-05) 의 저장 배치: 왼쪽 열은 추천+에쿼티뿐, 가운데 열에 접힌 섹션이 몰려 큰 빈칸이 생겼음
COMPLAINT_LAYOUT = {"cols": [["top", "equity"], ["cards", "implied", "opp", "pot", "notes", "m"],
                             ["guide", "tabs", "fold", "spr"]], "hidden": []}
READ_LINES = {"seat_TC": ["CO 오픈: 예상 상위 18% (Ax·브로드웨이)", "보드 기준: 탑페어 31% · 세컨드 페어 18%",
                          "가능성 높은 핸드: AKo 9%, KQs 7%, QQ 6%", "flop 벳 → 탑페어 이상 많음 (신뢰도 보통)"],
              "seat_BL": ["BB 콜: 예상 상위 40%", "보드 기준: 원페어 35% · 노페어 40%", "액션 정보 부족 (신뢰도 낮음)"]}


def balance_unit_cases():
    """balance_cols 순수 함수: 접힌 섹션은 열 맨 아래, 추천·카드·에쿼티·열 맨 위는 그대로, 열 높이 차이가 줄어듦, 늘 같은 결과."""
    fails = []
    home = COMPLAINT_LAYOUT["cols"]
    units = {"top": 450, "equity": 250, "cards": 108, "implied": 35, "opp": 35, "pot": 35, "notes": 35, "m": 126,
             "guide": 520, "tabs": 126, "fold": 35, "spr": 175}
    present = {"top", "equity", "cards", "m", "guide", "tabs", "spr"}
    cols = reader.balance_cols(home, units, present)
    naive = reader.cols_height(home, units)
    after = reader.cols_height(cols, units)
    heights = [sum(units[i] for i in c) + reader.GAP * (len(c) - 1) for c in cols]
    checks = {
        "추천·에쿼티 첫 열 (추천 맨 위)": cols[0][0] == "top" and "equity" in cols[0],
        "열 맨 위 그대로": cols[1][0] == "cards" and cols[2][0] == "guide",
        "접힌 섹션은 열 맨 아래": all(not any(i in present for i in c[k:]) for c in cols for k in range(len(c))
                                    if c[k] not in present),
        "가장 긴 열이 짧아짐": after < naive - reader.BAL_GAIN,
        "열 높이 비율 1.35 이하": max(heights) <= 1.35 * min(heights),
        "같은 입력 → 같은 배치": reader.balance_cols(home, units, present) == cols,
        "pinned 는 안 옮김": reader.balance_cols([["top"], ["a", "equity", "b"]], {"top": 10, "a": 10, "equity": 500, "b": 5},
                                               {"top", "a", "equity", "b"})[1][:2] == ["a", "equity"],
    }
    for name, ok in checks.items():
        print("ok  " if ok else "FAIL", "balance_cols:", name, "|", cols, heights)
        if not ok:
            fails.append(name)
    return fails


def layout_state(app, kind):
    """배치 점검용 상태: 프리플랍(상대 예상·입력 있음) / 플랍(상대 예상·질 수 있는 패) / 폴드 뒤 쉬는 중(상대 예상 없음)."""
    reads = {"key": 1, "sig": kind, "order": ["seat_TC", "seat_BL"], "ranges": {"seat_TC": {}, "seat_BL": {}},
             "lines": READ_LINES, "top": {"seat_TC": [("AKo", 9.2), ("KQs", 7.1), ("QQ", 6.0)], "seat_BL": [("K9s", 6.0), ("66", 5.2)]},
             "width": {"seat_TC": 18, "seat_BL": 40}}
    ts = {"to_act": True, "can_check": False, "call": 3.0, "bar": "call", "hero_in_hand": True}
    if kind == "pre":
        hero, board, pot, bet = ["Ah", "Qd"], [], "4.5BB", "3BB"
    elif kind == "flop":
        hero, board, pot, bet = ["Kd", "7h"], ["Qc", "Kh", "6d"], "10.3BB", "4BB"
    else:
        hero, board, pot, bet, ts = ["9h", "3d"], [], "", "", None
    for k, v in (("pot", pot), ("bet", bet), ("my_stack", "51.4BB"), ("my_bet", ""), ("sb", "0.5"), ("bb", "1"), ("ante", "0.12")):
        app.set_input(k, v)
    reads.update(hero=tuple(hero), board=tuple(board))
    app.track = {"reads": reads if kind != "rest" else None, "names": {}, "profiles": {}, "live": {}}
    app.table_state = ts
    state(app, hero, board, 2)
    if kind == "rest":
        app.view["folded"] = True
    app.advice_key = None
    app.compute_advice()
    app.render(force=True)
    app.root.update()


def layout_check(app, label, min_scale):
    """한 화면: 잘림 없음 / (자리 유지가 끝난 뒤) 빈자리 없음 / 접힌 섹션은 열 맨 아래 / 열 높이 비율 / 배율."""
    for recs in app.sticky.values():   # 자리 유지 시간이 지난 것으로
        for rec in recs.values():
            rec[1] -= reader.HOLD_S + 1
    app.render(force=True)
    app.root.update()
    secs = {sec[0]: sec for sec in app.placed_secs}
    tall = [rs[-1][2] - rs[0][1] for rs in app.slot_rects if rs]
    checks = {
        "잘림 없음": max(app.col_bottoms) <= app.win_h,
        "빈자리 없음": all(sec[3] == sec[5] for sec in app.placed_secs),
        "접힌 섹션은 열 맨 아래": all([secs[i][4] for i in col] == sorted((secs[i][4] for i in col), reverse=True)
                                    for col in app.slot_cols),
        "열 높이 비율 1.35 이하": max(tall) <= 1.35 * min(tall),
        f"배율 {min_scale} 이상": app.scale >= min_scale,
    }
    bad = [name for name, ok in checks.items() if not ok]
    print("ok  " if not bad else "FAIL", "배치", label, "| 배율", app.scale, "| 열", app.slot_cols, "| 높이", tall,
          "| 창", app.win_w, app.win_h, "|", bad)
    return [f"{label}: {b}" for b in bad]


def layout_cases(app):
    """불만 화면의 저장 배치·창 크기(1605x1130)로 프리플랍 → 플랍 → 폴드 뒤 → 다음 핸드. 창은 보조 모니터에 들어가게
    dpi 와 창을 같은 비율로 줄여 그린다 (배율 1.0 기준 단위의 배치는 같음). A−/A+ 보정, 핸드마다 배율 고정도 본다."""
    fails = balance_unit_cases()
    saved = app.cfg.get("section_layout"), app.cfg.get("ui_bias"), app.dpi, app.win_w, app.win_h, app.root.geometry(), app.track
    f = 0.6
    try:
        app.cfg["section_layout"] = {"cols": [list(c) for c in COMPLAINT_LAYOUT["cols"]], "hidden": []}
        app.cfg["ui_bias"] = 0.0
        app.dpi = saved[2] * f
        w, h = round(1605 * f), round(1130 * f)
        app.root.minsize(100, 100)
        app.root.geometry(f"{w}x{h}")
        app.root.update()
        app.win_w, app.win_h = w, h
        app.reset_lock()
        scales = []
        for rnd in range(2):
            for kind in ("pre", "flop", "rest"):
                layout_state(app, kind)
                fails += layout_check(app, f"{kind}#{rnd + 1}", 1.0)
                scales.append(app.scale)
        # 두 번째 핸드부터는 배율이 핸드·스트리트마다 흔들리지 않음
        ok = max(scales[3:]) - min(scales[3:]) <= reader.REFIT_TOL + 1e-6
        print("ok  " if ok else "FAIL", "배율 고정 (두 번째 핸드)", scales)
        if not ok:
            fails.append("배율 흔들림")
        # A− / A+: 같은 창에서 글자만 0.1 작게 / 다시 원래대로
        s0 = app.scale
        app.change_scale(-reader.SCALE_STEP)
        s1, b1 = app.scale, app.cfg.get("ui_bias")
        app.change_scale(reader.SCALE_STEP)
        s2, b2 = app.scale, app.cfg.get("ui_bias")
        ok = s1 <= s0 - 0.08 and b1 == -0.1 and abs(s2 - s0) <= 0.021 and b2 == 0.0 and (app.win_w, app.win_h) == (w, h)
        print("ok  " if ok else "FAIL", "A−/A+ 보정", s0, "→", s1, b1, "→", s2, b2)
        if not ok:
            fails.append("A−/A+")
        # 끌어 옮긴 섹션은 보이는 그대로 저장되고, 균형 맞추기가 다시 옮기지 않음 (pin)
        for kind in ("옮긴 직후", "다음 핸드 플랍"):
            if kind == "옮긴 직후":
                app.move_section("spr", 0, 99)
            else:
                layout_state(app, "flop")
            ok = "spr" in app.slot_cols[0] and "spr" in (app.cfg["section_layout"].get("pin") or [])
            print("ok  " if ok else "FAIL", "끌어 옮긴 섹션 고정", kind, app.slot_cols, app.cfg["section_layout"].get("pin"))
            if not ok:
                fails.append(f"pin {kind}")
    finally:
        app.cfg["section_layout"], app.cfg["ui_bias"], app.dpi = saved[:3]
        if saved[0] is None:
            app.cfg.pop("section_layout", None)
        app.win_w, app.win_h = saved[3], saved[4]
        app.root.geometry(saved[5])
        app.root.minsize(app.min_w, app.min_h)
        app.reset_lock()
        app.track = saved[6]
        app.render(force=True)
        app.root.update()
    return fails


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    reader.save_config = lambda *a, **k: None   # 점검이 config.json 을 바꾸지 않게
    # 실제 화면을 읽는 스레드·주기 갱신(tick)을 꺼서 점검이 만든 상태만 남게 한다 (게임 중 화면이 view/table_state 를 덮어써 흔들리던 문제)
    real_optional = reader._optional
    reader._optional = lambda name, *a, **k: None if name in ("tablestate", "gto_update") else real_optional(name, *a, **k)
    reader.ocr.available = lambda *a, **k: False
    reader.App.tick = lambda self: None
    app = reader.App()
    reader._optional = real_optional
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
        fails += len(layout_cases(app))
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
