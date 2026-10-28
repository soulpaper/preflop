"""녹화 폴더(recorder.py)를 다시 돌려 핸드 기록을 출력 — 실제 플레이로 검증할 때.

    python replay.py recordings/20261029_010203            # 핸드 기록 요약 출력
    python replay.py recordings/20261029_010203 --json     # 기록 JSON 그대로
    python replay.py <folder> --store test_profiles.db     # 통계 저장소에 넣어 보고 마지막에 통계 출력
    python replay.py <folder> --frames                     # 프레임별 상태 한 줄씩 (디버그)

calib.json 이 없으면 첫 프레임으로 layout.calibrate. 카드(내 카드/보드)는 frames.jsonl 의 meta 가 있으면 그것을,
없으면 reader 의 카드 인식(templates/)으로 읽는다. 시간은 파일 이름의 ms 시각.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2

import handlog
import layout
import names as names_mod
import tablestate


def frames(folder):
    out = []
    for p in sorted(Path(folder).glob("*.jpg")):
        parts = p.stem.split("_")
        try:
            t = int(parts[1]) / 1000.0
        except (IndexError, ValueError):
            t = p.stat().st_mtime
        out.append((p, t))
    return out


def load_meta(folder):
    meta = {}
    path = Path(folder) / "frames.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                m = json.loads(line)
                meta[m["frame"]] = m
            except Exception:
                pass
    return meta


class CardReader:
    """reader.py 의 카드 인식을 그대로 씀 (templates/ 필요)."""

    def __init__(self):
        import reader
        self.reader = reader
        self.matcher = reader.Matcher()
        self.threshold = reader.load_config().get("threshold", 0.95)

    def read(self, img, rect):
        x, y, w, h = rect
        crop = img[y:y + h, x:x + w]
        out = []
        for c in self.reader.detect_cards(crop):
            if self.reader.is_card_back(c):
                out.append(self.reader.BACK)
            else:
                out.append(self.matcher.match(c, self.threshold)[0])
        return out


def run(folder, store=None, show_frames=False, as_json=False, limit=None):
    folder = Path(folder)
    calib_path = folder / "calib.json"
    meta = load_meta(folder)
    fl = frames(folder)[:limit] if limit else frames(folder)
    if not fl:
        print("프레임 없음:", folder)
        return []
    first = cv2.imread(str(fl[0][0]))
    if calib_path.exists():
        calib = json.loads(calib_path.read_text(encoding="utf-8"))
    else:
        print("calib.json 없음 → 첫 프레임으로 자동 지정")
        calib = layout.calibrate(first)
        calib.pop("debug", None)
    cards = None
    if not meta:
        try:
            cards = CardReader()
        except Exception as exc:
            print("카드 인식 불가 (내 카드/보드 없이 진행):", exc)
    tr = tablestate.TableReader()
    nr = names_mod.NameReader(resolver=store.resolve_name if store else None,
                              on_confirm=store.learn_fingerprint if store else None)
    tracker = handlog.Tracker(session=folder.name, on_hand=store.observe if store else None)
    if store:
        store.new_session(tracker.session)
    records, ms = [], []
    for path, t in fl:
        img = cv2.imread(str(path))
        if img is None:
            continue
        t0 = time.perf_counter()
        state = tr.read(img, calib)
        t1 = time.perf_counter()
        nm = nr.read(img, calib, state)
        t2 = time.perf_counter()
        m = meta.get(path.stem)
        if m is not None:
            hero, board, pot = m.get("hero") or [], m.get("board") or [], m.get("pot")
        elif cards is not None:
            hero = cards.read(img, calib["hero"]) if "hero" in calib else []
            board = cards.read(img, calib["board"]) if "board" in calib else []
            board = [c for c in board if c != cards.reader.BACK]
            pot = None
        else:
            hero, board, pot = [], [], None
        done = tracker.update(state, hero, board, nm, t, pot=pot)
        ms.append(((t1 - t0) * 1000, (t2 - t1) * 1000, tracker.last_ms))
        if show_frames:
            seats = " ".join(f"{s['key'].replace('seat_', '')}:{'F' if s['folded'] else 'D' if s['dealt'] else '-'}"
                             f"{'' if s['bet'] is None else s['bet']}{'/' + s['bubble'] if s['bubble'] else ''}"
                             for s in state["seats"] if s["occupied"])
            print(f"{path.stem} hero={hero} board={board} dealer={state['dealer']} {seats}")
        for rec in done:
            records.append(rec)
            print(json.dumps(rec, ensure_ascii=False) if as_json else format_record(rec))
    for rec in tracker.flush():
        records.append(rec)
        print(json.dumps(rec, ensure_ascii=False) if as_json else format_record(rec))
    if ms:
        n = len(ms)
        print(f"\n{n} frames, avg ms: tablestate {sum(a for a, _, _ in ms) / n:.1f}, names {sum(b for _, b, _ in ms) / n:.1f}"
              f", tracker {sum(c for _, _, c in ms) / n:.2f}; hands {len(records)}"
              f" (low confidence {sum(1 for r in records if r['confidence'] != 'ok')}); tracker errors {tracker.errors}")
    if store:
        print("\n통계:")
        for k, v in sorted(nr.confirmed().items()):
            st = store.stats(v[0], v[1], seat=k, session=tracker.session)
            print(f"  {k:9s} {names_mod.display(*v):12s} hands {st['hands']:3d} vpip {st['vpip']} pfr {st['pfr']} "
                  f"3b {st['threebet']} f2b {st['fold_to_bet']} af {st['af']} wtsd {st['wtsd']}")
    return records


def format_record(rec):
    lines = [f"== {rec['id']} [{rec['confidence']}] {rec['streets']} board={' '.join(rec['board'] or [])} "
             f"hero={rec['hero_cards']} frames={rec['frames']} end={rec['end_reason']}"]
    for k, s in rec["seats"].items():
        nm = names_mod.display(s["name"], s["truncated"]) if s["name"] else "?"
        st = s.get("stats", {})
        flags = [f for f in ("vpip", "pfr", "threebet", "showdown") if st.get(f)]
        lines.append(f"   {k:9s} {s['pos'] or '-':4s} {nm:12s} {' '.join(flags)}")
    for street, acts in rec["actions"].items():
        lines.append(f"   {street:7s} " + ", ".join(
            f"{a['seat'].replace('seat_', '')} {a['act']}{' ' + format(a['to'], 'g') if 'to' in a else ''}"
            f"{' allin' if a.get('allin') else ''}{'*' if a['src'] == 'inferred' else ''}" for a in acts))
    if rec["issues"]:
        lines.append("   issues: " + "; ".join(i["code"] + (f" ({i['detail']})" if i.get("detail") else "")
                                         for i in rec["issues"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--store", help="통계를 넣어 볼 SQLite 파일 (기본: 넣지 않음 — 실제 profiles.db 를 건드리지 않게)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--frames", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    store = None
    if a.store:
        import profiles
        store = profiles.Store(a.store, hands_dir=Path(a.store).resolve().parent / "replay_hands")
    run(a.folder, store, a.frames, a.json, a.limit)


if __name__ == "__main__":
    main()
