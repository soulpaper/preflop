"""실제 App 화면 그리기 점검 — 프리플랍/플랍/턴/리버, 내 차례 앞뒤. 그리기 예외가 나면 실패 (창은 config.json 의 보조 모니터 위치)."""
import sys

import evaluator as ev
import reader


def state(app, hero, board):
    app.view = {"hero": hero, "board": board}
    h, b = tuple(ev.parse_card(c) for c in hero), tuple(ev.parse_card(c) for c in board)
    app.result = reader.analyze(list(h), list(b), 1, 2000)
    app.result_state = app.computed_state = (h, b, app.opponents())
    app.computing = False


def main():
    app = reader.App()
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
    app.root.destroy()
    print("render fails:", fails)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
