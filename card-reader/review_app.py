#!/usr/bin/env python3
"""복기·학습 도구 진입점 (리눅스, 표준 라이브러리만). 게임 없이 저장된 기록만 읽는다.

    python3 -I review_app.py synth <폴더>                   합성 기록 만들기 (실제 기록 아님)
    python3 -I review_app.py report --data <폴더>           글자로 요약
    python3 -I review_app.py serve --data <폴더>            화면 열기 (이 PC 안에서만, http://127.0.0.1:8765)
    python3 -I review_app.py check-gto <gto-data 폴더>      GTO 결과 검사

GTO 표 위치: --gto 또는 환경변수 GTO_LIB_DIR, 없으면 ../../gto-data/card-reader/gto_lib
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DEFAULT_GTO = os.path.normpath(os.path.join(HERE, "..", "..", "gto-data", "card-reader", "gto_lib"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("synth")
    p.add_argument("folder")
    p.add_argument("--random", type=int, default=120)
    p.add_argument("--seed", type=int, default=7)
    for name in ("report", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--data", required=True)
        p.add_argument("--gto")
        if name == "serve":
            p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("check-gto")
    p.add_argument("folder")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if a.cmd == "check-gto":
        from review import gtocheck
        return gtocheck.main(a.folder)
    if a.cmd == "synth":
        from review import synth
        print(synth.build(a.folder, n_random=a.random, seed=a.seed))
        return 0
    os.environ["GTO_LIB_DIR"] = a.gto or os.environ.get("GTO_LIB_DIR") or DEFAULT_GTO
    from review import engine
    rv = engine.Review(a.data)
    if a.cmd == "report":
        print(engine.text_report(rv))
        return 0
    from review import server
    return server.serve(rv, a.port)


if __name__ == "__main__":
    sys.exit(main())
