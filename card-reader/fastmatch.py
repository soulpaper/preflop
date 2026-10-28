"""Batched replacement for the per-template cv2.matchTemplate loop in reader.Matcher.

Each comparison (full / corner / upper) becomes one matrix product:
every shift window of the crop is a row, every template is a column.

TM_CCOEFF_NORMED for multi-channel images (verified against OpenCV 4.13):
  - the mean is subtracted per channel (window and template separately),
  - the numerator and both norms are summed over all channels,
  - zero-variance window -> 0, zero-variance template -> 1 at every shift.
Because the template columns are zero-mean per channel, the window does not
need to be mean-centred for the numerator; only its norm needs the per-channel
variance, which comes from summed-area tables.
"""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

NORM_SIZE = (52, 72)   # (w, h), same as reader.NORM_SIZE
INSET = 4
CORNER = (24, 36)      # (w, h)
_VAR_EPS = 1e-3        # uint8 input: any non-constant window has variance >= 0.5


def _specs(norm_size, inset, corner):
    """(crop slice, template slice) per comparison, identical to reader.Matcher.match."""
    w, h = norm_size
    cw, ch = corner
    top_h = h // 2
    i2 = inset // 2
    return {
        "full": ((slice(None), slice(None)),
                 (slice(inset, -inset), slice(inset, -inset))),
        "corner": ((slice(None, ch + i2), slice(None, cw + i2)),
                   (slice(i2, ch), slice(i2, cw))),
        "upper": ((slice(None, top_h + inset), slice(None)),
                  (slice(i2, top_h), slice(inset, -inset))),
    }


class _Comparison:
    def __init__(self, crop_sl, tpl_sl):
        self.crop_sl = crop_sl
        self.tpl_sl = tpl_sl
        self.shape = None        # template (th, tw)
        self.buf = None          # (capacity, K) float32, unit-norm zero-mean rows
        self.const = None        # (capacity,) bool, template had zero variance

    def add(self, tpl, n):
        t = tpl[self.tpl_sl].astype(np.float64)
        if self.shape is None:
            self.shape = t.shape[:2]
        elif t.shape[:2] != self.shape:
            raise ValueError(f"template slice {t.shape[:2]} != {self.shape}")
        t -= t.mean(axis=(0, 1))
        norm = np.sqrt((t * t).sum())
        is_const = norm < np.finfo(np.float64).eps
        # channel-major flattening, matching sliding_window_view's (..., C, th, tw) layout
        vec = np.zeros(t.size) if is_const else (t / norm).transpose(2, 0, 1).ravel()
        if self.buf is None:
            self.buf = np.empty((8, vec.size), np.float32)
            self.const = np.zeros(8, bool)
        elif n == len(self.buf):
            self.buf = np.concatenate([self.buf, np.empty_like(self.buf)])
            self.const = np.concatenate([self.const, np.zeros_like(self.const)])
        self.buf[n] = vec
        self.const[n] = is_const

    def scores(self, norm, n):
        region = norm[self.crop_sl].astype(np.float64)
        region -= region.mean(axis=(0, 1))       # keeps float32 matmul well conditioned
        th, tw = self.shape
        # window variance (summed over channels) from summed-area tables, in float64
        sat = np.zeros((region.shape[0] + 1, region.shape[1] + 1, 2, region.shape[2]))
        sat[1:, 1:, 0] = region.cumsum(0).cumsum(1)
        sat[1:, 1:, 1] = (region * region).cumsum(0).cumsum(1)
        box = sat[th:, tw:] - sat[:-th, tw:] - sat[th:, :-tw] + sat[:-th, :-tw]
        s1, s2 = box[:, :, 0], box[:, :, 1]                  # (sh, sw, C)
        var = (s2 - s1 * s1 / (th * tw)).sum(-1).ravel()     # (M,)
        win = sliding_window_view(region.astype(np.float32), (th, tw), axis=(0, 1))
        win = win.reshape(-1, win.shape[2] * th * tw)        # (M, K) copy
        num = win @ self.buf[:n].T                           # (M, N)
        den = np.sqrt(np.maximum(var, 0.0)).astype(np.float32)[:, None]
        ok = den > np.sqrt(_VAR_EPS)
        res = np.where(ok, num / np.where(ok, den, 1.0), 0.0)
        np.clip(res, -1.0, 1.0, out=res)
        out = res.max(axis=0)
        out[self.const[:n]] = 1.0
        return out


class FastMatcher:
    """Batched TM_CCOEFF_NORMED scores for all templates at once.

    fm = FastMatcher(items)            # items: iterable of (label, 52x72 BGR uint8)
    full, corner, upper = fm.scores(norm_crop)   # each np.ndarray[N], max over shifts
    fm.labels[i] is the label of template i.
    """

    def __init__(self, items=(), norm_size=NORM_SIZE, inset=INSET, corner=CORNER):
        self.labels = []
        specs = _specs(norm_size, inset, corner)
        self._cmp = [_Comparison(*specs[k]) for k in ("full", "corner", "upper")]
        for label, tpl in items:
            self.add(label, tpl)

    def __len__(self):
        return len(self.labels)

    def add(self, label, tpl):
        n = len(self.labels)
        for c in self._cmp:
            c.add(tpl, n)
        self.labels.append(label)

    def scores(self, norm_crop):
        n = len(self.labels)
        if n == 0:
            e = np.empty(0, np.float32)
            return e, e.copy(), e.copy()
        return tuple(c.scores(norm_crop, n) for c in self._cmp)
