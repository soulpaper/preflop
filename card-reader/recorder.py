"""기록 모드: 게임 창 캡처를 약 1초마다 JPEG 로 저장 (나중에 replay.py 로 검증).

    rec = recorder.Recorder()            # BASE/recordings/
    rec.start(calib)                     # recordings/YYYYMMDD_HHMMSS/ 에 calib.json
    rec.add(img, meta={"hero": [...], "board": [...], "pot": 12.5})   # 1초 안에 다시 부르면 건너뜀
    rec.stop()

폴더 구성: calib.json, 000001_<ms>.jpg ..., frames.jsonl (프레임별 meta: 앱이 읽은 카드/팟 등, 선택)
전체 크기가 max_bytes(기본 2GB)를 넘으면 가장 오래된 녹화 폴더부터 지움 (지금 녹화 중인 폴더는 안 지움).
예외를 밖으로 내지 않음 (실패하면 errors 증가).
"""
import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import cv2

BASE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
MAX_BYTES = 2 * 1024 ** 3
CHECK_EVERY = 50           # 이만큼 저장할 때마다 전체 크기 확인
FOLDER = re.compile(r"\d{8}_\d{6}(_\d+)?")   # 녹화 폴더 이름 (용량 정리는 이 이름의 폴더만 지움)


def _json(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda o: o.item() if hasattr(o, "item") else str(o)))


class Recorder:
    def __init__(self, root=None, interval=1.0, quality=80, max_bytes=MAX_BYTES):
        self.root = Path(root) if root else BASE / "recordings"
        self.interval, self.quality, self.max_bytes = interval, quality, max_bytes
        self.folder = None
        self.count = 0
        self.errors = 0
        self.last_error = None
        self._last = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return self.folder is not None

    def start(self, calib=None):
        """새 녹화 폴더를 만들고 기록 시작 → 폴더 경로 (실패하면 None)."""
        try:
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True)
                name = time.strftime("%Y%m%d_%H%M%S")
                folder = self.root / name
                i = 1
                while folder.exists():
                    i += 1
                    folder = self.root / f"{name}_{i}"
                folder.mkdir()
                self.folder, self.count, self._last = folder, 0, 0.0
            if calib is not None:
                self.set_calib(calib)
            self.enforce_cap()
            return self.folder
        except Exception as exc:
            self._fail(exc)
            self.folder = None
            return None

    def stop(self):
        with self._lock:
            folder, self.folder = self.folder, None
        return folder

    def set_calib(self, calib):
        """보정 결과 저장 (다시 자동 지정하면 다시 부를 것 — replay 는 가장 최근 것을 씀)."""
        try:
            if self.folder is None:
                return
            calib = {k: v for k, v in calib.items() if k != "debug"}
            path = self.folder / "calib.json"
            if path.exists():  # 녹화 중에 다시 보정: 이전 것은 번호 붙여 보관
                path.rename(self.folder / f"calib_{int(time.time())}.json")
            path.write_text(json.dumps(_json(calib), ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self._fail(exc)

    def add(self, img, t=None, meta=None, force=False):
        """캡처 한 장 (BGR). 간격이 안 됐으면 건너뜀. 저장했으면 True."""
        if self.folder is None or img is None:
            return False
        t = time.time() if t is None else t
        if not force and t - self._last < self.interval * 0.95:
            return False
        try:
            with self._lock:
                if self.folder is None:
                    return False
                self._last = t
                self.count += 1
                stem = f"{self.count:06d}_{int(t * 1000)}"
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(self.quality)])
                if not ok:
                    raise RuntimeError("jpeg encode failed")
                (self.folder / f"{stem}.jpg").write_bytes(buf.tobytes())
                if meta is not None:
                    with open(self.folder / "frames.jsonl", "a", encoding="utf-8") as f:
                        f.write(json.dumps({"frame": stem, "t": t, **_json(meta)}, ensure_ascii=False) + "\n")
                check = self.count % CHECK_EVERY == 0
            if check:
                self.enforce_cap()
            return True
        except Exception as exc:
            self._fail(exc)
            return False

    def enforce_cap(self):
        """녹화 전체가 max_bytes 를 넘으면 오래된 폴더부터 지움. 지운 폴더 수."""
        try:
            if not self.root.exists():
                return 0
            # 이 모듈이 만든 폴더 이름(YYYYMMDD_HHMMSS[_n])만 대상으로
            folders = sorted((p for p in self.root.iterdir() if p.is_dir() and FOLDER.fullmatch(p.name)),
                             key=lambda p: p.stat().st_mtime)
            sizes = {p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) for p in folders}
            total, removed = sum(sizes.values()), 0
            for p in folders:
                if total <= self.max_bytes:
                    break
                if self.folder is not None and p.resolve() == self.folder.resolve():
                    continue
                shutil.rmtree(p, ignore_errors=True)
                total -= sizes[p]
                removed += 1
            return removed
        except Exception as exc:
            self._fail(exc)
            return 0

    def _fail(self, exc):
        self.errors += 1
        self.last_error = repr(exc)
