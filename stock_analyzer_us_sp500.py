# -*- coding: utf-8 -*-
# ════════════════════════════════════════════════════════════════════
#  US技術分析全攻略 · 美股評分系統（Streamlit 版，POC 分價量表）
#  由 stock_analyzer_us_poc.html（美股 HTML 版）移植；技術指標、型態辨識、評分、回測統計
#  跟台股 PY 版完全同一套引擎（已逐點比對過 JS 原始邏輯）。資料來源：Financial Modeling Prep。
#
#  跟台股版的差異：
#   - 大盤＝SPY；可選「類股狀態濾網」（FMP sector → SPDR 11 檔類股 ETF 站上/跌破 20/60 日均線）。
#   - 營收用季報：近3季營收 YoY/QoQ、近3季均價 YoY；條件旗標用「近1季」乖離度／均價YoY。
#   - 沒有三大法人資料；多了「分析師評等／目標價／內部人交易」面板。
#   - 分價量表：2026-09-27 美股回測四個訊號單獨都沒有超額報酬，不當組合搜尋條件
#     （USE_VP_FLAGS_US=False；訊號仍顯示在表格與回測統計）。
#   - 夜星、母子懷抱(高檔)視為「高檔強勢整理」（美股「夜星剛形成」20日同日超額+6.6%、t=4.5）。
#   - 指定組合：S＝選股型（2023-10～2026-09 美股三年六段回測）／#＝高勝率（標⏱為擇時型）／M＝高標股。
#   - HTML 版回測的「近1季YoY乖離度」抓營收時參數順序寫反（實際抓不到資料），PY 版已修正。
#
#  執行方式：
#      pip install streamlit plotly pandas numpy requests openpyxl
#      streamlit run stock_analyzer_us_poc.py
#  每日自動追蹤命中組合（排程用，不開介面）：
#      python stock_analyzer_us_poc.py daily --list sp500 --token 你的FMP_API_Key
# ════════════════════════════════════════════════════════════════════
import math
import re
import json
import time
import io
import os
import datetime as dt
from itertools import combinations

import numpy as np
import pandas as pd
try:   # pandas 2.x：開啟 Copy-on-Write，改欄位時不必整張表複製（pandas 3 預設就是）
    pd.set_option('mode.copy_on_write', True)
except Exception:  # noqa
    pass
import requests

# ────────────────────────────────────────────────────────────────────
#  JS 相容小工具
# ────────────────────────────────────────────────────────────────────
def js_round(x):
    """JS Math.round（.5 一律進位），Python round 是銀行家捨入，結果會不同"""
    return int(math.floor(x + 0.5))


def fnum(v, d=1):
    return '--' if v is None or (isinstance(v, float) and math.isnan(v)) else f'{v:.{d}f}'


def isnum(v):
    return v is not None and not (isinstance(v, float) and math.isnan(v))


# ────────────────────────────────────────────────────────────────────
#  K 棒資料 + 技術指標（全部是「因果」算法：第 i 根的值只用到第 i 根以前的資料，
#  所以回測時整條序列算一次，任何評估點直接取用，不會偷看未來）
# ────────────────────────────────────────────────────────────────────
class Bars:
    __slots__ = ('date', 'open', 'high', 'low', 'close', 'volume', 'n',
                 'ma5', 'ma10', 'ma20', 'ma60', 'macd', 'macdSig', 'macdHist', 'rsi',
                 'bbU', 'bbL', 'vm5', 'vm20', 'kdK', 'kdD', 'kdjK', 'kdjD', 'kdjJ',
                 'plusDI', 'minusDI', 'adx', 'adxr', '_zz', '_good', '_filtered', '_fmap', '_ma100')

    def __init__(self, rows):
        # rows: list of dict(date, open, high, low, close, volume)，已依日期排序
        self.date = [r['date'] for r in rows]
        self.open = [float(r['open']) for r in rows]
        self.high = [float(r['high']) for r in rows]
        self.low = [float(r['low']) for r in rows]
        self.close = [float(r['close']) for r in rows]
        self.volume = [float(r['volume']) for r in rows]
        self.n = len(rows)
        self._zz = None
        self._good = None
        self._filtered = None
        self._fmap = None
        self._ma100 = None
        self._enrich()

    # ── 指標（逐一照 HTML 版的 JS 算法，連浮點運算順序都一致）──
    def _ma(self, arr, p):
        out = [None] * self.n
        for i in range(p - 1, self.n):
            s = 0.0
            for j in range(i - p + 1, i + 1):
                s += arr[j]
            out[i] = s / p
        return out

    def _ema(self, p):
        k = 2 / (p + 1)
        res = []
        e = self.close[0] if self.n else 0
        for i in range(self.n):
            e = self.close[i] if i == 0 else self.close[i] * k + e * (1 - k)
            res.append(e)
        return res

    def _enrich(self):
        n = self.n
        c = self.close
        self.ma5, self.ma10, self.ma20, self.ma60 = (self._ma(c, 5), self._ma(c, 10),
                                                     self._ma(c, 20), self._ma(c, 60))
        # MACD(12,26,9)
        e12, e26 = self._ema(12), self._ema(26)
        dif = [e12[i] - e26[i] for i in range(n)]
        k = 2 / 10
        sig = []
        s = dif[0] if n else 0
        for i in range(n):
            s = dif[i] if i == 0 else dif[i] * k + s * (1 - k)
            sig.append(s)
        self.macd = dif
        self.macdSig = sig
        self.macdHist = [dif[i] - sig[i] for i in range(n)]
        # RSI(14)
        p = 14
        rsi = [None] * n
        if n > p:
            ag = al = 0.0
            for i in range(1, p + 1):
                d = c[i] - c[i - 1]
                if d > 0:
                    ag += d
                else:
                    al += abs(d)
            ag /= p
            al /= p
            rsi[p] = 100 - 100 / (1 + ag / (al or 0.001))
            for i in range(p + 1, n):
                d = c[i] - c[i - 1]
                ag = (ag * (p - 1) + (d if d > 0 else 0)) / p
                al = (al * (p - 1) + (abs(d) if d < 0 else 0)) / p
                rsi[i] = 100 - 100 / (1 + ag / (al or 0.001))
        self.rsi = rsi
        # 布林通道(20,2)
        self.bbU = [None] * n
        self.bbL = [None] * n
        for i in range(19, n):
            m = self.ma20[i]
            s = 0.0
            for j in range(i - 19, i + 1):
                s += (c[j] - m) * (c[j] - m)
            sd = math.sqrt(s / 20)
            self.bbU[i] = m + 2 * sd
            self.bbL[i] = m - 2 * sd
        self.vm5 = self._ma(self.volume, 5)
        self.vm20 = self._ma(self.volume, 20)
        self.kdK, self.kdD, _ = self._kdj(9, 3, 3)
        self.kdjK, self.kdjD, self.kdjJ = self._kdj(6, 3, 3)
        self._dmi(14)

    def _kdj(self, period, kN, dN):
        kA, dA, jA = [], [], []
        pk = pd_ = 50.0
        for i in range(self.n):
            if i < period - 1:
                kA.append(None); dA.append(None); jA.append(None)
                continue
            lo = min(self.low[i - period + 1:i + 1])
            hi = max(self.high[i - period + 1:i + 1])
            rsv = 50 if hi == lo else (self.close[i] - lo) / (hi - lo) * 100
            if kN == 3 and dN == 3 and period == 9:
                k = pk * 2 / 3 + rsv * 1 / 3
                d = pd_ * 2 / 3 + k * 1 / 3
            else:
                k = pk * (kN - 1) / kN + rsv * 1 / kN
                d = pd_ * (dN - 1) / dN + k * 1 / dN
            kA.append(k); dA.append(d); jA.append(3 * k - 2 * d)
            pk, pd_ = k, d
        return kA, dA, jA

    def _dmi(self, p):
        """Wilder DMI，第 b 根的值＝把資料截到第 b 根時 calcDMI() 的回傳值"""
        n = self.n
        self.plusDI = [None] * n
        self.minusDI = [None] * n
        self.adx = [None] * n
        self.adxr = [None] * n
        if n < p + 1:
            return
        pdm, mdm, tr = [], [], []
        h, l, c = self.high, self.low, self.close
        for i in range(1, n):
            up = h[i] - h[i - 1]
            dn = l[i - 1] - l[i]
            pdm.append(up if (up > dn and up > 0) else 0)
            mdm.append(dn if (dn > up and dn > 0) else 0)
            tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))

        def wsum(arr):
            out = []
            s = 0.0
            for j in range(p):
                s += arr[j]
            out.append(s)
            for j in range(p, len(arr)):
                s = s - s / p + arr[j]
                out.append(s)
            return out

        sTR, sP, sM = wsum(tr), wsum(pdm), wsum(mdm)
        dx = []
        for i in range(len(sTR)):
            pdi = sP[i] / sTR[i] * 100 if sTR[i] else 0
            mdi = sM[i] / sTR[i] * 100 if sTR[i] else 0
            b = i + p
            self.plusDI[b] = pdi
            self.minusDI[b] = mdi
            ss = pdi + mdi
            dx.append(abs(pdi - mdi) / ss * 100 if ss else 0)
        if len(dx) >= p:
            avg = 0.0
            for v in dx[:p]:
                avg += v
            avg /= p
            self.adx[2 * p - 1] = avg
            for k in range(p, len(dx)):
                avg = (avg * (p - 1) + dx[k]) / p
                self.adx[k + p] = avg
        for b in range(3 * p - 1, n):
            if self.adx[b] is not None and self.adx[b - p] is not None:
                self.adxr[b] = (self.adx[b] + self.adx[b - p]) / 2

    # ── 壞資料過濾（型態辨識／圖表用，跟 JS 的 filter 一致）──
    def good(self, i):
        o, h, l, c = self.open[i], self.high[i], self.low[i], self.close[i]
        return (o > 0 and h > 0 and l > 0 and c > 0 and
                all(math.isfinite(x) for x in (o, h, l, c)))

    def filtered(self):
        """回傳 (filteredBars, fmap)：fmap[i] = 原始第 i 根在過濾後序列的位置（壞K棒為 -1）"""
        if self._filtered is None:
            keep = [i for i in range(self.n) if self.good(i)]
            if len(keep) == self.n:
                self._filtered = self
                self._fmap = list(range(self.n))
            else:
                fb = object.__new__(Bars)
                for attr in ('date', 'open', 'high', 'low', 'close', 'volume', 'ma5', 'ma10', 'ma20',
                             'ma60', 'macd', 'macdSig', 'macdHist', 'rsi', 'bbU', 'bbL', 'vm5', 'vm20',
                             'kdK', 'kdD', 'kdjK', 'kdjD', 'kdjJ', 'plusDI', 'minusDI', 'adx', 'adxr'):
                    src = getattr(self, attr)
                    setattr(fb, attr, [src[i] for i in keep])
                fb.n = len(keep)
                fb._zz = None
                fb._good = None
                fb._filtered = fb
                fb._fmap = list(range(fb.n))
                fmap = [-1] * self.n
                for pos, i in enumerate(keep):
                    fmap[i] = pos
                self._filtered = fb
                self._fmap = fmap
        return self._filtered, self._fmap

    # ── 轉折波：狀態機只跑一次，記下每根K棒處理完後的狀態，任何截止點都能 O(1) 重建 ──
    def _build_zz_state(self):
        n = self.n
        start = 0
        while start < n and (self.ma5[start] is None or not self._sane(start)):
            start += 1
        st = {'start': start, 'confirmed': [], 'snap': [None] * n}
        if start >= n:
            self._zz = st
            return
        state = 'above' if self.close[start] >= self.ma5[start] else 'below'
        conf = [(start, self.low[start] if state == 'above' else self.high[start],
                 'L' if state == 'above' else 'H')]
        ext_i = start
        ext_p = self.high[start] if state == 'above' else self.low[start]
        st['snap'][start] = (1, state, ext_i, ext_p)
        for i in range(start + 1, n):
            if self.ma5[i] is not None and self._sane(i):
                if state == 'above':
                    if self.high[i] > ext_p:
                        ext_p, ext_i = self.high[i], i
                    if self.close[i] < self.ma5[i]:
                        conf.append((ext_i, ext_p, 'H'))
                        state = 'below'
                        ext_i, ext_p = i, self.low[i]
                else:
                    if self.low[i] < ext_p:
                        ext_p, ext_i = self.low[i], i
                    if self.close[i] > self.ma5[i]:
                        conf.append((ext_i, ext_p, 'L'))
                        state = 'above'
                        ext_i, ext_p = i, self.high[i]
            st['snap'][i] = (len(conf), state, ext_i, ext_p)
        st['confirmed'] = conf
        self._zz = st

    def _sane(self, i):
        h, l, c, m5 = self.high[i], self.low[i], self.close[i], self.ma5[i]
        if not (h > 0) or not (l > 0) or not (c > 0):
            return False
        if not (math.isfinite(h) and math.isfinite(l) and math.isfinite(c)):
            return False
        if m5 is not None and m5 > 0:
            if l < m5 * 0.4 or h > m5 * 2.5:
                return False
        return True

    def zigzag(self, end):
        """等同 JS buildZigzag(data.slice(0, end+1))，回傳 [(idx, price, type), ...]"""
        if self._zz is None:
            self._build_zz_state()
        st = self._zz
        n = end + 1
        if st['start'] >= n - 1:
            return []
        cnt, state, ext_i, ext_p = st['snap'][end]
        pts = st['confirmed'][:cnt]
        if ext_i != end:
            pts.append((ext_i, ext_p, 'H' if state == 'above' else 'L'))
            pts.append((end, self.close[end], 'L' if state == 'above' else 'H'))
        else:
            pts.append((ext_i, self.close[end], 'H' if state == 'above' else 'L'))
        return pts


# ────────────────────────────────────────────────────────────────────
#  多方力道評分（DMI）
# ────────────────────────────────────────────────────────────────────
def score_dmi(b: Bars, e):
    pdi, mdi, adx, adxr = b.plusDI[e], b.minusDI[e], b.adx[e], b.adxr[e]
    if pdi is None or mdi is None:
        return dict(score=0, max=100, plusDI=None, minusDI=None, adx=None, adxr=None,
                    diPts=0, adxPts=0, adxrPts=0, tdir='資料不足',
                    sigs=[('DMI資料不足（可能資料天數太短）', 'neu')])
    s = pdi + mdi
    dom = pdi / s * 100 if s else 50
    diPts = dom * 0.5
    adxPts = min(adx, 40) / 40 * 30 if adx is not None else 0
    adxrPts = 20 if (adx is not None and adxr is not None and adx > adxr) else 0
    score = js_round(max(0, min(100, diPts + adxPts + adxrPts)))
    bull = pdi > mdi
    tdir = '多頭' if bull else ('空頭' if pdi < mdi else '盤整')
    sigs = [(f"+DI {pdi:.1f}{' > ' if bull else ' < '}-DI {mdi:.1f}（多方力道{'較強' if bull else '較弱'}）",
             'bull' if bull else 'bear')]
    if adx is not None:
        lbl = '趨勢明確' if adx >= 25 else ('趨勢成形中' if adx >= 20 else '盤整')
        sigs.append((f'ADX {adx:.1f}（{lbl}）', 'bull' if adx >= 25 else 'neu'))
    if adx is not None and adxr is not None:
        up = adx > adxr
        sigs.append((f"ADX{'>' if up else '<'}ADXR，趨勢{'轉強' if up else '轉弱'}", 'bull' if up else 'bear'))
    return dict(score=score, max=100, plusDI=pdi, minusDI=mdi, adx=adx, adxr=adxr,
                diPts=diPts, adxPts=adxPts, adxrPts=adxrPts, tdir=tdir, sigs=sigs)


# ────────────────────────────────────────────────────────────────────
#  回後買上漲 8 條件
# ────────────────────────────────────────────────────────────────────
def check_pullback_buy(b: Bars, e):
    C, O, H, L = b.close, b.open, b.high, b.low
    last = e
    prev = e - 1 if e >= 1 else e
    results = []
    allPass = True
    zz = b.zigzag(e)
    zH = [p for p in zz if p[2] == 'H']
    zL = [p for p in zz if p[2] == 'L']

    c1 = False
    if len(zH) >= 2 and len(zL) >= 2:
        c1 = zH[-1][1] > zH[-2][1] and zL[-1][1] > zL[-2][1]
    results.append(dict(label='①趨勢多頭（高高低低）', pass_=c1, required=True, detail=''))
    allPass &= c1

    s0 = max(0, e - 5)
    pbd = list(range(s0, e)) if e >= 1 else []  # data.slice(-6,-1)
    had = False
    for k, i in enumerate(pbd):
        ref = C[pbd[k - 1]] if k > 0 else C[i]
        if C[i] < ref or C[i] < O[i]:
            had = True
            break
    c2 = had and C[last] > C[prev]
    detail2 = ''
    if len(zH) >= 1:
        peak = zH[-1]
        pri = [p for p in zL if p[0] < peak[0]]
        if pri:
            priorLow = pri[-1][1]
            if peak[1] > priorLow:
                plow = min(L[peak[0]:e + 1])
                retr = (peak[1] - plow) / (peak[1] - priorLow)
                grade = '最強' if retr <= 0.382 else '強' if retr <= 0.5 else '弱' if retr <= 0.618 else '回檔過深'
                detail2 = f'回檔幅度{retr * 100:.1f}%（{grade}，費波0.382/0.5/0.618分級）'
    results.append(dict(label='②位置回後上漲（近期有回檔，今轉上）', pass_=c2, required=True, detail=detail2))
    allPass &= c2

    m5 = b.ma5[last]
    c3 = m5 is not None and C[last] > m5
    results.append(dict(label='③收盤站上5MA（平價不算）', pass_=c3, required=True,
                        detail=f'5MA={m5:.2f}  收盤={C[last]:.2f}' if m5 is not None else ''))
    allPass &= c3

    c4 = H[last] > H[prev]
    results.append(dict(label='④突破前一日高點（含上影線）', pass_=c4, required=True,
                        detail=f'今高={H[last]:.2f}  昨高={H[prev]:.2f}'))
    allPass &= c4

    chg = (C[last] - C[prev]) / C[prev] * 100 if C[prev] > 0 else 0
    c5 = chg >= 2.0
    results.append(dict(label='⑤漲幅2%以上', pass_=c5, required=True, detail=f'漲幅={chg:.2f}%'))
    allPass &= c5

    body = C[last] - O[last]
    maxSh = max(H[last] - C[last], O[last] - L[last])
    c6 = body > 0 and maxSh <= body
    results.append(dict(label='⑥實體紅K，影線不大於實體', pass_=c6, required=True,
                        detail=f'實體={body:.2f}  最大影線={maxSh:.2f}'))
    allPass &= c6

    vm20 = b.vm20[last]
    vr = b.volume[last] / vm20 if vm20 else 1
    c7 = vr >= 1.0
    results.append(dict(label='⑦成交量增（加分項）', pass_=c7, required=False, detail=f'量比MA20={vr:.2f}x'))

    kNow = b.kdK[last]
    kPrev = b.kdK[e - 1] if e >= 1 else None
    c8 = kNow is not None and kPrev is not None and kNow > kPrev
    results.append(dict(label='⑧指標確認（K值向上）', pass_=c8, required=True,
                        detail=(f"K={kNow:.1f}  昨K={kPrev:.1f}" if kPrev is not None else f"K={kNow:.1f}  昨K=--")
                        if kNow is not None else 'KD資料不足'))
    allPass &= c8

    reqT = sum(1 for r in results if r['required'])
    reqP = sum(1 for r in results if r['required'] and r['pass_'])
    bonus = sum(1 for r in results if not r['required'] and r['pass_'])
    return dict(results=results, allPass=bool(allPass), requiredPassed=reqP,
                requiredTotal=reqT, bonusPassed=bonus)


# ────────────────────────────────────────────────────────────────────
#  15 種進場型態
# ────────────────────────────────────────────────────────────────────
PATTERN_DEFS = [
    ('hs', '頭肩底'), ('chs', '複式頭肩底'), ('nb', 'N字底'), ('tb', '三重底'), ('rb', '圓弧底'),
    ('fb', '一字底(均線糾結)'), ('abc', '突破ABC修正下降切線'), ('channel', '突破上升軌道線'),
    ('blackk', '突破飆股大量黑K最高點'), ('kbp', 'K線橫盤的突破'),
    ('harami_bear', '母子懷抱(高檔)'), ('harami_bull', '母子懷抱(低檔)'),
    ('morning_star', '晨星'), ('evening_star', '夜星'),
]
# 夜星、母子懷抱(高檔)：原本視為看空而排除在彙總旗標外；2026-09-27 台股＋美股回測都顯示
# 強勢股出現這兩種型態多半是洗盤後續漲（美股「夜星剛形成」20日同日超額+6.6%、t=4.5），
# 改稱「高檔強勢整理」，照常計入彙總旗標。BEARISH_PATTERNS 留空保留介面，之後要排除某型態再加回來。
BEARISH_PATTERNS = set()
HIGH_CONSOLIDATION_PATTERNS = {'harami_bear', 'evening_star'}
EVENING_STAR_HIGH_POS = 0.70   # 夜星首日紅K須位於前20日收盤區間的70%以上（高檔）

_PDESC = {
    'hs': '左右肩低點相近，頭部最低，右肩不破1/2，突破頸線為買點',
    'chs': '多重肩部低點環繞單一最低頭部，最近肩部不破1/2，突破頸線為買點',
    'nb': '低點反彈後拉回不破1/2，再突破反彈高點為買點',
    'tb': '三個低點高度相近，中間兩高點形成水平頸線，突破頸線為買點',
    'rb': '價格緩跌後緩升成U型，突破起跌點高點為買點',
    'fb': '均線糾結、價格窄幅整理（區間範圍約10%內），帶量突破整理區間為買點',
    'abc': '多頭回檔呈ABC三段式下跌，A、B高點畫下降切線，C段拉回後帶量紅K突破切線為買點',
    'channel': '股價沿上升軌道緩步上漲，MA20上揚下帶量長紅收盤突破軌道上緣為買點',
    'blackk': '飆股急漲後出現大量黑K回檔，3日內帶量長紅突破其最高點為買點',
    'kbp': '三天以上(含首日)收盤未突破首日K線高低點，帶量紅K突破首日高點為買點',
    'harami_bear': '上漲高檔出現中長紅K，次日不過高不破低的黑K線為母子懷抱。回測顯示多為高檔強勢整理後續漲，不是反轉',
    'harami_bull': '下跌低檔出現中長黑K，次日不過高不破低的紅K線為母子懷抱，空頭下跌力道轉弱',
    'morning_star': '下跌出現中長黑K+變盤線+中長紅K，收盤站上首日實體中點為低檔轉折向上訊號',
    'evening_star': '高檔(前20日區間70%以上)出現中長紅K+變盤線+中長黑K。回測顯示強勢股出現此型態多為洗盤後續漲（高檔強勢整理），不是反轉',
}


def _tolerant(a, b, pct):
    base = max(abs(a), abs(b), 1e-6)
    return abs(a - b) / base <= pct


def _res(pid, name, formed=False, breakout=False, detail='尚未偵測到符合結構', line=None, line2=None, marker=None):
    return dict(id=pid, name=name, formed=formed, breakout=breakout, detail=detail,
                desc=_PDESC.get(pid, ''), line=line, line2=line2, marker=marker, justBroke=False)


def detect_patterns14(d: Bars, e):
    """14 種圖形型態（d 必須是已過濾壞K棒的序列），回傳 list[dict]"""
    O, H, L, C, V = d.open, d.high, d.low, d.close, d.volume
    last = e
    lastClose = C[last]
    vm20 = d.vm20[last]
    volConfirm = (V[last] / vm20 >= 1.3) if vm20 else False
    zz = d.zigzag(e)
    lows = [p[0] for p in zz if p[2] == 'L' and p[0] < last]
    highs = [p[0] for p in zz if p[2] == 'H' and p[0] < last]
    nlen = e + 1

    def bo_check(res):
        if res is None:
            return False, ''
        return lastClose > res, (f'頸線/壓力＝{res:.2f}　現價＝{lastClose:.2f}' + ('　(帶量突破)' if volConfirm else ''))

    def ma_up(arr, n):
        p = arr[max(0, last - n)]
        c = arr[last]
        if c is None or p is None:
            return False
        return c > p

    out = []

    # (1) 頭肩底
    r = None
    if len(lows) >= 3:
        l3 = lows[-3:]
        L1, L2, L3 = L[l3[0]], L[l3[1]], L[l3[2]]
        if _tolerant(L1, L3, 0.06) and L2 < L1 * 0.985 and L2 < L3 * 0.985:
            hb = [i for i in highs if l3[0] < i < l3[2]]
            neck = max(H[i] for i in hb) if hb else None
            if neck is not None and L3 > (L2 + neck) / 2:
                ok, det = bo_check(neck)
                r = _res('hs', '頭肩底', True, ok, det or '型態成形，等待突破頸線', line=(l3[0], neck, 0))
    out.append(r or _res('hs', '頭肩底'))

    # (2) 複式頭肩底
    r = None
    if len(lows) >= 4:
        ll = lows[-5:]
        vals = [L[i] for i in ll]
        mn = min(vals)
        hp = vals.index(mn)
        sv = [v for k, v in enumerate(vals) if k != hp]
        ok_sh = hp > 0 and hp < len(ll) - 1 and all(_tolerant(v, sv[0], 0.08) and v > mn * 1.02 for v in sv)
        if ok_sh:
            hb = [i for i in highs if ll[0] < i < ll[-1]]
            neck = max(H[i] for i in hb) if hb else None
            if neck is not None and vals[-1] > (mn + neck) / 2:
                ok, det = bo_check(neck)
                r = _res('chs', '複式頭肩底', True, ok, det or '型態成形，等待突破頸線', line=(ll[0], neck, 0))
    out.append(r or _res('chs', '複式頭肩底'))

    # (3) N字底
    r = None
    if len(lows) >= 2 and len(highs) >= 1:
        A, Cc = lows[-2], lows[-1]
        bc = [i for i in highs if A < i < Cc]
        if bc:
            B = bc[0]
            for i in bc:
                if H[i] > H[B]:
                    B = i
            half = (L[A] + H[B]) / 2
            if L[Cc] > half:
                ok, det = bo_check(H[B])
                r = _res('nb', 'N字底', True, ok, det or f'拉回未破1/2（{half:.2f}），等待突破反彈高點',
                         line=(B, H[B], 0))
    out.append(r or _res('nb', 'N字底'))

    # (4) 三重底
    r = None
    if len(lows) >= 3:
        l3 = lows[-3:]
        v = [L[i] for i in l3]
        if _tolerant(v[0], v[1], .08) and _tolerant(v[1], v[2], .08) and _tolerant(v[0], v[2], .08):
            h1 = [i for i in highs if l3[0] < i < l3[1]]
            h2 = [i for i in highs if l3[1] < i < l3[2]]
            p1 = max(H[i] for i in h1) if h1 else None
            p2 = max(H[i] for i in h2) if h2 else None
            if p1 is not None and p2 is not None and _tolerant(p1, p2, 0.05):
                res = max(p1, p2)
                ok, det = bo_check(res)
                r = _res('tb', '三重底', True, ok, det or '型態成形，等待突破壓力', line=(l3[0], res, 0))
    out.append(r or _res('tb', '三重底'))

    # (5) 圓弧底
    r = None
    ws = max(0, nlen - 40)
    wl = nlen - ws
    if wl >= 30:
        seg = wl // 3
        first = list(range(ws, ws + seg))
        mid = list(range(ws + seg, ws + wl - seg))
        tail = list(range(ws + wl - seg, ws + wl))

        def slope(idx):
            n = len(idx)
            sx = sy = sxy = sxx = 0.0
            for k, i in enumerate(idx):
                sx += k; sy += C[i]; sxy += k * C[i]; sxx += k * k
            den = (n * sxx - sx * sx) or 1
            return (n * sxy - sx * sy) / den

        def avgc(idx):
            s = 0.0
            for i in idx:
                s += C[i]
            return s / len(idx)

        midLow = min(L[i] for i in mid)
        convex = avgc(first) > midLow * 1.01 and avgc(tail) > midLow * 1.01
        shape = slope(first) < 0 and slope(tail) > 0 and convex
        ar = 0.0
        for i in range(ws, nlen):
            ar += (H[i] - L[i]) / C[i]
        ar /= wl
        if shape and ar < 0.05:
            res = max(H[i] for i in first)
            ok, det = bo_check(res)
            target = res + (res - midLow)
            r = _res('rb', '圓弧底', True, ok, (det or '弧形築底中，等待突破起跌壓力') + f'　目標價≈{target:.2f}',
                     line=(ws, res, 0))
    out.append(r or _res('rb', '圓弧底'))

    # (6) 一字底(均線糾結)
    r = None
    N = 10
    w0 = max(0, nlen - N - 1)
    win = list(range(w0, e))  # slice(-11,-1)
    if len(win) == N and all(d.ma5[i] is not None and d.ma10[i] is not None and d.ma20[i] is not None
                             and d.ma60[i] is not None for i in win):
        tangled = True
        for i in win:
            vals = [d.ma5[i], d.ma10[i], d.ma20[i], d.ma60[i]]
            if (max(vals) - min(vals)) / min(vals) > 0.05:
                tangled = False
                break
        wh = max(H[i] for i in win)
        wlw = min(L[i] for i in win)
        if tangled and (wh - wlw) / wlw <= 0.10:
            l4 = [d.ma5[last], d.ma10[last], d.ma20[last], d.ma60[last]]
            above = all(v is not None and lastClose > v for v in l4)
            bo = lastClose > wh and above and volConfirm
            t6 = wh + (wh - wlw)
            r = _res('fb', '一字底(均線糾結)', True, bo,
                     f'整理區間高點＝{wh:.2f}　現價＝{lastClose:.2f}' + ('　(帶量突破)' if volConfirm else '　(尚未帶量)')
                     + f'　目標價≈{t6:.2f}', line=(nlen - N - 1, wh, 0))
    out.append(r or _res('fb', '一字底(均線糾結)'))

    # (7) 突破ABC修正下降切線
    r = None
    rh = [i for i in highs if i >= last - 40]
    if len(rh) >= 2:
        h1, h2 = rh[-2], rh[-1]
        y1, y2 = H[h1], H[h2]
        low_between = any(h1 < li < h2 for li in lows)
        cLow = None
        for ci in range(h2 + 1, last):
            if cLow is None or L[ci] < cLow:
                cLow = L[ci]
        hasC = cLow is not None and cLow < C[h2] * 0.98
        if y2 < y1 and h2 > h1 and low_between and hasC and (h2 - h1) <= 20 and (last - h2) <= 20:
            sl = (y2 - y1) / (h2 - h1)
            line_at = y1 + sl * (last - h1)
            up20 = ma_up(d.ma20, 10)
            isRed = C[last] > O[last]
            bo = lastClose > line_at and up20 and isRed
            r = _res('abc', '突破ABC修正下降切線', True, bo,
                     f'下降切線位置≈{line_at:.2f}　C低點＝{cLow:.2f}　現價＝{lastClose:.2f}'
                     + ('　MA20上揚' if up20 else '　MA20未上揚'), line=(h1, y1, sl))
    out.append(r or _res('abc', '突破ABC修正下降切線'))

    # (8) 突破上升軌道線
    r = None
    fl = max(0, nlen - 60)
    li_ = [i for i in lows if i >= fl]
    hi_ = [i for i in highs if i >= fl]
    if len(li_) >= 2:
        l1, l2 = li_[-2], li_[-1]
        ly1, ly2 = L[l1], L[l2]
        if ly2 > ly1 and l2 > l1:
            s2 = (ly2 - ly1) / (l2 - l1)
            offs = [H[i] - (ly1 + s2 * (i - l1)) for i in hi_ if i > l1]
            offset = None
            if len(offs) >= 2:
                tol = lastClose * 0.03
                best, bc_ = None, 0
                for o in offs:
                    cnt = sum(1 for o2 in offs if abs(o - o2) <= tol)
                    if cnt > bc_ or (cnt == bc_ and (best is None or o > best)):
                        bc_, best = cnt, o
                if bc_ >= 2:
                    offset = best
            if offset is not None and offset > 0:
                upper = ly1 + s2 * (last - l1) + offset
                up20 = ma_up(d.ma20, 10)
                isRed = C[last] > O[last]
                bo = lastClose > upper and up20 and isRed and volConfirm
                r = _res('channel', '突破上升軌道線', True, bo,
                         f'軌道上緣≈{upper:.2f}　現價＝{lastClose:.2f}' + ('　帶量' if volConfirm else '　量未放大'),
                         line=(l1, ly1 + offset, s2), line2=(l1, ly1, s2))
    out.append(r or _res('channel', '突破上升軌道線'))

    # (9) 突破飆股大量黑K最高點
    r = None
    f3 = max(0, nlen - 1 - 10)
    cands = [i for i in range(f3, last) if C[i] < O[i] and d.vm20[i] and V[i] / d.vm20[i] >= 1.6]
    if cands:
        bk = cands[-1]
        if 0 <= last - bk <= 3:
            isRed = C[last] > O[last]
            up20 = ma_up(d.ma20, 10)
            bo = lastClose > H[bk] and isRed and volConfirm and up20
            r = _res('blackk', '突破飆股大量黑K最高點', True, bo,
                     f'大量黑K高點＝{H[bk]:.2f}　現價＝{lastClose:.2f}' + ('　帶量' if volConfirm else '　量未放大'),
                     line=(bk, H[bk], 0))
    out.append(r or _res('blackk', '突破飆股大量黑K最高點'))

    # (10) K線橫盤的突破
    r = None
    if nlen >= 4:
        anc = last - 3
        ok = all(not (C[j] > H[anc] or C[j] < L[anc]) for j in range(anc + 1, last))
        if ok:
            cand = anc - 1
            while cand >= 0 and (last - cand) <= 20:
                inr = all(not (C[k] > H[cand] or C[k] < L[cand]) for k in range(cand + 1, last))
                if not inr:
                    break
                anc = cand
                cand -= 1
            isRed = C[last] > O[last]
            bo = lastClose > H[anc] and isRed and volConfirm
            r = _res('kbp', 'K線橫盤的突破', True, bo,
                     f'首日K線高點＝{H[anc]:.2f}　整理天數＝{last - anc}天　現價＝{lastClose:.2f}'
                     + ('　(帶量)' if volConfirm else '　(量未放大)'), line=(anc, H[anc], 0))
    out.append(r or _res('kbp', 'K線橫盤的突破'))

    # (11) 母子懷抱(高檔)
    r = None
    if nlen >= 3:
        m, ch = last - 2, last - 1
        mb = abs(C[m] - O[m]) / C[m]
        contained = H[ch] <= H[m] and L[ch] >= L[m]
        smaller = abs(C[ch] - O[ch]) < abs(C[m] - O[m]) * 0.6
        atHigh = C[m] >= C[last - 3] if last - 3 >= 0 else True
        if C[m] > O[m] and mb >= 0.035 and contained and smaller and atHigh:
            bo = C[last] < C[ch]
            r = _res('harami_bear', '母子懷抱(高檔)', True, bo,
                     f'母K(中長紅)高點＝{H[m]:.2f}　子K收於母K範圍內　' + ('次日已確認轉折向下' if bo else '等待次日確認轉折向下'),
                     marker=(last - 1, H[m], 'up', '母子懷抱'))
    out.append(r or _res('harami_bear', '母子懷抱(高檔)'))

    # (12) 母子懷抱(低檔)
    r = None
    if nlen >= 3:
        m, ch = last - 2, last - 1
        mb = abs(C[m] - O[m]) / C[m]
        contained = H[ch] <= H[m] and L[ch] >= L[m]
        smaller = abs(C[ch] - O[ch]) < abs(C[m] - O[m]) * 0.6
        atLow = C[m] <= C[last - 3] if last - 3 >= 0 else True
        if C[m] < O[m] and mb >= 0.035 and contained and smaller and atLow:
            bo = C[last] > C[ch]
            r = _res('harami_bull', '母子懷抱(低檔)', True, bo,
                     f'母K(中長黑)低點＝{L[m]:.2f}　子K收於母K範圍內　' + ('次日已確認轉折向上' if bo else '等待次日確認轉折向上'),
                     marker=(last - 1, L[m], 'down', '母子懷抱'))
    out.append(r or _res('harami_bull', '母子懷抱(低檔)'))

    # (13) 晨星
    r = None
    if nlen >= 3:
        s1, s2, s3 = last - 2, last - 1, last
        mid1 = (O[s1] + C[s1]) / 2
        if (C[s1] < O[s1] and abs(C[s1] - O[s1]) / C[s1] >= 0.035 and abs(C[s2] - O[s2]) / C[s2] < 0.035
                and C[s3] > O[s3] and abs(C[s3] - O[s3]) / C[s3] >= 0.035 and C[s3] > mid1):
            r = _res('morning_star', '晨星', True, True,
                     f'首日黑K實體中點＝{mid1:.2f}　收盤＝{C[s3]:.2f}（已站上中點，轉折確認）',
                     marker=(last - 1, L[s2], 'down', '晨星'))
    out.append(r or _res('morning_star', '晨星'))

    # (14) 夜星（修正：限制在高檔）
    r = None
    if nlen >= 3:
        e1, e2, e3 = last - 2, last - 1, last
        mid1 = (O[e1] + C[e1]) / 2
        w = C[max(0, e1 - 19):e1 + 1]       # 含首日在內的前20日收盤
        rng = max(w) - min(w)
        pos = (C[e1] - min(w)) / rng if rng > 0 else 0
        atHigh = len(w) >= 10 and pos >= EVENING_STAR_HIGH_POS
        if (atHigh and C[e1] > O[e1] and abs(C[e1] - O[e1]) / C[e1] >= 0.035
                and abs(C[e2] - O[e2]) / C[e2] < 0.035
                and C[e3] < O[e3] and abs(C[e3] - O[e3]) / C[e3] >= 0.035 and C[e3] < mid1):
            r = _res('evening_star', '夜星', True, True,
                     f'首日紅K位於前20日區間{pos * 100:.0f}%　實體中點＝{mid1:.2f}　收盤＝{C[e3]:.2f}（已跌破中點，轉折確認）',
                     marker=(last - 1, H[e2], 'up', '夜星'))
    out.append(r or _res('evening_star', '夜星'))
    return out


def _pbup_entry(pb):
    formed = pb['allPass'] or pb['requiredPassed'] >= pb['requiredTotal'] - 1
    return dict(id='pbup', name='回後買上漲', formed=formed, breakout=pb['allPass'],
                detail=f"必要條件 {pb['requiredPassed']}/{pb['requiredTotal']} 通過" + ('　+成交量增加分' if pb['bonusPassed'] else ''),
                desc='趨勢多頭，回檔量縮價穩後，今日紅K放量突破前高為買點',
                line=None, line2=None, marker=None, justBroke=False)


class PatternCache:
    """同一檔股票逐日回測時，昨天的型態結果今天拿來判斷『剛突破』，不用重算"""

    def __init__(self, b: Bars):
        self.b = b
        self.fb, self.fmap = b.filtered()
        self.p14 = {}
        self.pbf = {}

    def p14_at(self, f):
        if f not in self.p14:
            self.p14[f] = detect_patterns14(self.fb, f)
        return self.p14[f]

    def pb_at(self, f):
        if f not in self.pbf:
            self.pbf[f] = check_pullback_buy(self.fb, f)
        return self.pbf[f]

    def detect(self, e, pb):
        """等同 JS detectPatterns(data.slice(0,e+1), pb)"""
        # 過濾後的截止位置＝e 以前（含）最後一根好K棒
        f = self.fmap[e]
        if f < 0:
            f = max([self.fmap[i] for i in range(e + 1) if self.fmap[i] >= 0] or [-1])
        if f < 0:
            return dict(results=[], anyBreakout=False, anyFormed=False, anyJustBroke=False)
        res = [dict(x) for x in self.p14_at(f)] + [_pbup_entry(pb)]
        if f >= 1:
            prev = self.p14_at(f - 1) + [_pbup_entry(self.pb_at(f - 1))]
            for r, pr in zip(res, prev):
                r['justBroke'] = bool(r['breakout'] and not pr['breakout'])
        bull = [r for r in res if r['id'] not in BEARISH_PATTERNS]
        return dict(results=res,
                    anyBreakout=any(r['breakout'] for r in bull),
                    anyFormed=any(r['formed'] for r in bull),
                    anyJustBroke=any(r['justBroke'] for r in bull))


# ────────────────────────────────────────────────────────────────────
#  布林／MACD／KDJ 綜合訊號
# ────────────────────────────────────────────────────────────────────
def core_signals(b: Bars, e):
    C = b.close
    prev = e - 1 if e >= 1 else e
    bbPos = None
    if b.bbU[e] is not None and b.bbL[e] is not None:
        w = b.bbU[e] - b.bbL[e]
        bbPos = (C[e] - b.bbL[e]) / w * 100 if w else 50
    macdState = None
    isBreakout = isPR = gcr = False
    div = False
    if b.macdHist[e] is not None and b.macdHist[prev] is not None:
        above = b.macd[e] > 0
        grow = b.macdHist[e] > b.macdHist[prev]
        if above and b.macdHist[e] > 0:
            macdState = '零軸上・紅柱增長' if grow else '零軸上・紅柱縮短'
        elif not above and b.macdHist[e] < 0:
            macdState = '零軸下・綠柱縮短' if grow else '零軸下・綠柱增長'
        else:
            macdState = '交叉轉換中'
        for i in range(max(1, e + 1 - 3), e + 1):
            if b.macd[i - 1] <= b.macdSig[i - 1] and b.macd[i] > b.macdSig[i]:
                gcr = True
                break
        widthExp = False
        if b.bbU[e] is not None and b.bbL[e] is not None:
            wn = (b.bbU[e] - b.bbL[e]) / C[e] * 100 if C[e] else 0
            ri = e - 5
            if ri >= 0 and b.bbU[ri] is not None and b.bbL[ri] is not None and C[ri]:
                widthExp = wn > (b.bbU[ri] - b.bbL[ri]) / C[ri] * 100
        isBreakout = bbPos is not None and bbPos >= 80 and widthExp and above and b.macdHist[e] > 0 and grow
        zl = [p for p in b.zigzag(e) if p[2] == 'L']
        if len(zl) >= 2:
            rl, pl = zl[-1], zl[-2]
            if rl[0] >= e - 60:
                div = rl[1] < pl[1] and b.macd[rl[0]] > b.macd[pl[0]]
        isPR = bbPos is not None and bbPos <= 20 and div and gcr
    kdjState = None
    kg = kd = False
    if b.kdjK[e] is not None and b.kdjD[e] is not None:
        kdjState = 'K>D（多方）' if b.kdjK[e] > b.kdjD[e] else ('K<D（空方）' if b.kdjK[e] < b.kdjD[e] else 'K=D')
        for i in range(max(1, e + 1 - 3), e + 1):
            a0, d0, a1, d1 = b.kdjK[i - 1], b.kdjD[i - 1], b.kdjK[i], b.kdjD[i]
            if None not in (a0, d0, a1, d1):
                if a0 <= d0 and a1 > d1:
                    kg = True
                if a0 >= d0 and a1 < d1:
                    kd = True
    return dict(bbPos=bbPos, macdState=macdState, isBreakout=isBreakout, isPullbackRebound=isPR,
                goldenCrossRecent=gcr, kdjState=kdjState, kdjGoldenCrossRecent=kg,
                kdjDeathCrossRecent=kd, divergence=div)


# ────────────────────────────────────────────────────────────────────
#  大盤（SPY）／類股ETF、相對強弱、量能
# ────────────────────────────────────────────────────────────────────
class Benchmark:
    def __init__(self, rows):
        rows = sorted(rows, key=lambda r: r['date'])
        self.date = [r['date'] for r in rows]
        self.close = [float(r['close']) for r in rows]
        n = len(rows)
        self.ma20 = [None] * n
        self.ma60 = [None] * n
        for i in range(n):
            if i >= 19:
                s = 0.0
                for j in range(i - 19, i + 1):
                    s += self.close[j]
                self.ma20[i] = s / 20
            if i >= 59:
                s = 0.0
                for j in range(i - 59, i + 1):
                    s += self.close[j]
                self.ma60[i] = s / 60
        self.idx = {d: i for i, d in enumerate(self.date)}

    def find(self, ds):
        if ds in self.idx:
            return self.idx[ds]
        import bisect
        return bisect.bisect_right(self.date, ds) - 1

    def regime(self, ds):
        i = self.find(ds)
        if i < 0:
            return None, None
        c = self.close[i]
        return (None if self.ma20[i] is None else c > self.ma20[i],
                None if self.ma60[i] is None else c > self.ma60[i])


def relative_strength(b: Bars, e, bm, lb=20):
    if bm is None or not bm.close or e + 1 <= lb:
        return None
    pc = b.close[e - lb]
    if not pc:
        return None
    sr = (b.close[e] - pc) / pc * 100
    bi = bm.find(b.date[e])
    if bi < lb:
        return None
    bp = bm.close[bi - lb]
    if not bp:
        return None
    return sr - (bm.close[bi] - bp) / bp * 100


def vol_ratio(b: Bars, e):
    v = b.vm20[e]
    return b.volume[e] / v if v else None


def vol_range_pos(b: Bars, e):
    if e + 1 < 20:
        return None
    w = b.volume[max(0, e + 1 - 252):e + 1]
    if len(w) < 20:
        return None
    t = b.volume[e]
    return sum(1 for v in w if v < t) / len(w) * 100


def vol_trend(b: Bars, e):
    if e + 1 < 10:
        return None
    a10 = sum(b.volume[e - 9:e + 1]) / 10
    a5 = sum(b.volume[e - 4:e + 1]) / 5
    if not a10:
        return None
    return (a5 - a10) / a10 * 100


# ────────────────────────────────────────────────────────────────────
#  個股分價量表（Volume Profile）── POC 版
# ────────────────────────────────────────────────────────────────────
VP_DEFAULTS = dict(lookback=120, bins=24, shadow_mult=1.0, vol_mult=1.2, body_mult=1.2, touch_tol=0.5)

VP_LABELS = {
    'vpBuySupport': '分價量表-守穩POC買進',
    'vpBuyBreakout': '分價量表-突破POC追價買進',
    'vpSellResistance': '分價量表-反彈POC遇壓賣出',
    'vpBreakdown': '分價量表-破位停損賣出',
}
VP_SIGTYPE_FIELD = {'buy_support': 'vpBuySupport', 'buy_breakout': 'vpBuyBreakout',
                    'sell_resistance': 'vpSellResistance', 'sell_breakdown': 'vpBreakdown'}


def volume_profile(b: Bars, e, lookback=120, bins=24):
    if e + 1 < 20:
        return None
    s = max(0, e + 1 - lookback)
    lo = min(b.low[s:e + 1])
    hi = max(b.high[s:e + 1])
    if not hi > lo:
        return None
    bs = (hi - lo) / bins
    vol = [0.0] * bins
    for i in range(s, e + 1):
        mid = (b.high[i] + b.low[i]) / 2
        k = min(bins - 1, max(0, int(math.floor((mid - lo) / bs))))
        vol[k] += b.volume[i]
    poc = 0
    for i in range(1, bins):
        if vol[i] > vol[poc]:
            poc = i
    tot = sum(vol)
    li = hi_i = poc
    inc = vol[poc]
    while inc < tot * 0.7 and (li > 0 or hi_i < bins - 1):
        nl = vol[li - 1] if li > 0 else -1
        nh = vol[hi_i + 1] if hi_i < bins - 1 else -1
        if nh >= nl:
            hi_i += 1
            inc += vol[hi_i]
        else:
            li -= 1
            inc += vol[li]
    return dict(pocLow=lo + poc * bs, pocHigh=lo + (poc + 1) * bs, pocPrice=lo + (poc + 0.5) * bs,
                valLow=lo + li * bs, valHigh=lo + (hi_i + 1) * bs, binSize=bs, lo=lo, hi=hi)


def vp_signal(b: Bars, e, prof, p=None):
    """以 POC 區（成交量最大價位區間）為界的4種訊號"""
    p = p or VP_DEFAULTS
    base = dict(signal=None, sigType=None, detail=None,
                pocPrice=prof['pocPrice'] if prof else None,
                pocLow=prof['pocLow'] if prof else None, pocHigh=prof['pocHigh'] if prof else None,
                vah=prof['valHigh'] if prof else None, val=prof['valLow'] if prof else None)
    if not prof or e + 1 < 22:
        return base
    O, H, L, C, V = b.open, b.high, b.low, b.close, b.volume
    pc = C[e - 1]
    isRed, isBlack = C[e] > O[e], C[e] < O[e]
    body = abs(C[e] - O[e])
    upSh = H[e] - max(O[e], C[e])
    dnSh = min(O[e], C[e]) - L[e]
    rb = range(e - 20, e)
    avgBody = sum(abs(C[i] - O[i]) for i in rb) / 20
    avgVol = sum(V[i] for i in rb) / 20
    longRed = isRed and body > avgBody * p['body_mult']
    longBlack = isBlack and body > avgBody * p['body_mult']
    hasVol = avgVol > 0 and V[e] > avgVol * p['vol_mult']
    pL, pH, pP = prof['pocLow'], prof['pocHigh'], prof['pocPrice']
    tol = prof['binSize'] * p['touch_tol']
    longLower = dnSh > 0 and dnSh >= body * p['shadow_mult']
    longUpper = upSh > 0 and upSh >= body * p['shadow_mult']

    support = pc > pH and L[e] <= pH and L[e] >= pL - tol and C[e] >= pP and longLower
    breakout = pc <= pH and C[e] > pH and longRed and hasVol
    resist = pc < pL and H[e] >= pL and H[e] <= pH + tol and C[e] <= pP and longUpper
    breakdown = pc >= pL and C[e] < pL and longBlack and hasVol

    if support:
        base.update(signal='buy', sigType='buy_support',
                    detail='守穩POC買進：股價由上方回測POC成交密集區，長下影線且收盤守住POC價（停損設POC區之下）')
    elif breakout:
        base.update(signal='buy', sigType='buy_breakout',
                    detail='突破POC追價買進：帶量長紅K由POC區突破到上方，買方消化最大套牢量')
    elif resist:
        base.update(signal='sell', sigType='sell_resistance',
                    detail='反彈POC遇壓賣出：股價由下方反彈到POC成交密集區，長上影線且收盤壓回POC價之下')
    elif breakdown:
        base.update(signal='sell', sigType='sell_breakdown',
                    detail='破位停損賣出：帶量長黑K跌破POC區，賣方潰散，進入成交量真空區（停損/減碼）')
    return base


# ────────────────────────────────────────────────────────────────────
#  旗標列（批次即時／回測共用）
# ────────────────────────────────────────────────────────────────────
PH_FIELDS = ['ph_' + pid for pid, _ in PATTERN_DEFS] + ['ph_pbup']



# ── 新增技術面欄位（2026-09-30）：52週高點、均線多頭排列、布林收窄、向上跳空 ──
LONG_HISTORY_DAYS = 400   # 52週高點需要約250根K棒；批次分析會多抓到400天只用來算這幾個欄位


def tech_extras(b, e):
    """52週高點距離／創新高、均線多頭排列、布林寬度在近半年的百分位、近3日向上跳空、
    3日內突破季線／半年線、連續放量、MACD零軸下金叉。資料不足時為 None"""
    out = dict(high52Dist=None, newHigh52=None, maBull=None, bbwRank=None, gapUp3=None,
               crossMa60_3=None, crossMa100_3=None, vol3Ratio=None, gcBelow0_3=None,
               udVolRatio20=None, cmf20=None, obvNewHigh60=None,
               bias20=None, pullMa60=None, limitUp3=None, drop5=None, nr7=None)
    if e < 0 or e >= b.n:
        return out
    if e >= 249:
        mx = max(b.high[e - 249:e + 1])
        prev = max(b.high[e - 249:e])
        out['high52Dist'] = (b.close[e] / mx - 1) * 100 if mx > 0 else None
        out['newHigh52'] = int(b.high[e] > prev)
    m5, m20, m60 = b.ma5[e], b.ma20[e], b.ma60[e]
    if None not in (m5, m20, m60):
        out['maBull'] = int(m5 > m20 > m60 and b.close[e] > m20)

    def bw(i):
        u, l = b.bbU[i], b.bbL[i]
        if u is None or l is None or (u + l) <= 0:
            return None
        return (u - l) / ((u + l) / 2)
    cur = bw(e)
    if cur is not None and e >= 119:
        ws = [w for w in (bw(i) for i in range(e - 119, e + 1)) if w is not None]
        if len(ws) >= 100:
            out['bbwRank'] = sum(1 for w in ws if w <= cur) / len(ws) * 100
    if e >= 3:
        out['gapUp3'] = int(any(b.low[e - k] > b.high[e - k - 1] for k in range(3)))
    # 3日內收盤由下往上突破季線(MA60)／半年線(MA100)，且今天仍站在線上（2026-10-03 參考 BBW+MACD 掃描器新增）
    if b._ma100 is None:
        b._ma100 = b._ma(b.close, 100)
    for fld, ma in (('crossMa60_3', b.ma60), ('crossMa100_3', b._ma100)):
        if e >= 3 and None not in (ma[e - 3], ma[e]):
            out[fld] = int(b.close[e] >= ma[e] and any(
                b.close[j - 1] < ma[j - 1] and b.close[j] >= ma[j] for j in range(e - 2, e + 1)))
    # 連續放量：近3日均量 ÷ 再往前20日均量
    if e >= 22:
        base = sum(b.volume[e - 22:e - 2]) / 20
        out['vol3Ratio'] = (sum(b.volume[e - 2:e + 1]) / 3) / base if base > 0 else None
    # MACD 零軸下（DIF<0）3日內黃金交叉＝低檔轉折
    if e >= 3:
        out['gcBelow0_3'] = int(any(b.macd[j - 1] <= b.macdSig[j - 1] and b.macd[j] > b.macdSig[j] and b.macd[j] < 0
                                    for j in range(e - 2, e + 1)))
    # 2026-10-04 買賣力道近似（日線沒有內外盤，用收盤在當日區間的位置／漲跌日量推估）
    out.update(dict(udVolRatio20=None, cmf20=None, obvNewHigh60=None))
    if e >= 20:
        up = dn = 0.0
        mfv = vs = 0.0
        for j in range(e - 19, e + 1):
            v = b.volume[j]
            if b.close[j] > b.close[j - 1]:
                up += v
            elif b.close[j] < b.close[j - 1]:
                dn += v
            rng = b.high[j] - b.low[j]
            if rng > 0:
                mfv += ((b.close[j] - b.low[j]) - (b.high[j] - b.close[j])) / rng * v
            vs += v
        out['udVolRatio20'] = min(up / dn, 99.0) if dn > 0 else (99.0 if up > 0 else None)
        out['cmf20'] = mfv / vs if vs > 0 else None
    if e >= 61:
        # OBV 從 e-60 起算（起點＝0），今天的 OBV 高於前60天每一天＝OBV創60日新高
        obv, mx = 0.0, 0.0
        for j in range(e - 59, e):
            obv += b.volume[j] if b.close[j] > b.close[j - 1] else (-b.volume[j] if b.close[j] < b.close[j - 1] else 0.0)
            mx = max(mx, obv)
        obv += b.volume[e] if b.close[e] > b.close[e - 1] else (-b.volume[e] if b.close[e] < b.close[e - 1] else 0.0)
        out['obvNewHigh60'] = int(obv > mx)
    # 2026-10-05 新增：短線乖離、多頭回測季線、近3日漲停（台股）、近5日跌幅與NR7窄幅日（美股）
    out.update(dict(bias20=None, pullMa60=None, limitUp3=None, drop5=None, nr7=None))
    if b.ma20[e]:
        out['bias20'] = (b.close[e] / b.ma20[e] - 1) * 100
    if e >= 10 and b.ma60[e] and b.ma60[e - 10]:
        out['pullMa60'] = int(b.ma60[e] > b.ma60[e - 10] and abs(b.close[e] / b.ma60[e] - 1) <= 0.03)
    if e >= 3:
        out['limitUp3'] = int(any(b.close[j - 1] > 0 and b.close[j] / b.close[j - 1] - 1 >= 0.095 for j in range(e - 2, e + 1)))
    if e >= 5 and b.close[e - 5]:
        out['drop5'] = (b.close[e] / b.close[e - 5] - 1) * 100
    if e >= 6:
        rg = [b.high[j] - b.low[j] for j in range(e - 6, e + 1)]
        out['nr7'] = int(rg[-1] < min(rg[:-1]))
    return out


def trim_rows(rows, days):
    """只保留最近 days 天（和直接抓 days 天的結果相同）"""
    cut = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return [r for r in rows if r['date'] >= cut]


def build_flag_row(b: Bars, e, bm, pc: PatternCache, vp_params=None, extras=None):
    """回傳 (row, info)：row 是回測／條件旗標用的欄位，info 是批次表格顯示用的明細"""
    vpp = vp_params or VP_DEFAULTS
    dm = score_dmi(b, e)
    sig = core_signals(b, e)
    rs = relative_strength(b, e, bm, 20)
    vr = vol_ratio(b, e)
    vrp = vol_range_pos(b, e)
    vt = vol_trend(b, e)
    prof = volume_profile(b, e, vpp['lookback'], vpp['bins'])
    vps = vp_signal(b, e, prof, vpp)
    a20, a60 = bm.regime(b.date[e]) if bm else (None, None)
    pb = check_pullback_buy(b, e)
    pt = pc.detect(e, pb)
    row = dict(
        evalDate=b.date[e], score=dm['score'], plusDI=dm['plusDI'], minusDI=dm['minusDI'],
        adx=dm['adx'], adxr=dm['adxr'], bbPos=sig['bbPos'],
        isBreakout=int(sig['isBreakout']), isPullbackRebound=int(sig['isPullbackRebound']),
        goldenCrossRecent=int(sig['goldenCrossRecent']),
        kdjGoldenCrossRecent=int(sig['kdjGoldenCrossRecent']), kdjDeathCrossRecent=int(sig['kdjDeathCrossRecent']),
        relStrength20=rs, volRatio=vr, volRangePos=vrp, volTrend=vt,
        vpPocPrice=vps['pocPrice'],
        benchmarkAbove20=None if a20 is None else int(a20), benchmarkAbove60=None if a60 is None else int(a60),
        patternFormed=int(pt['anyFormed']), patternBreakout=int(pt['anyBreakout']),
        patternJustBroke=int(pt['anyJustBroke']), pbAllPass=int(pb['allPass']),
    )
    for f in VP_LABELS:
        row[f] = 0
    if vps['sigType']:
        row[VP_SIGTYPE_FIELD[vps['sigType']]] = 1
    for r in pt['results']:
        row['ph_' + r['id']] = int(r['justBroke'])
    ex = extras or {}
    row['divTotal'] = ex.get('divTotal')
    row['priceYoy1q'] = ex.get('priceYoy1q')
    sb = ex.get('sector_bm')
    s20, s60 = sb.regime(b.date[e]) if sb else (None, None)
    row['sectorAbove20'] = None if s20 is None else int(s20)
    row['sectorAbove60'] = None if s60 is None else int(s60)
    row['confluenceCount'] = confluence_count(row)
    tx = (extras or {}).get('tech')
    if tx:
        row.update(tx)
    else:   # 回測：當下這根K棒算（中期動能需要大盤）
        row.update(tech_extras(b, e))
        row.update(mid_momentum(b, e, bm))
    for k in ('epsSurprise', 'earnDays', 'earnGap20'):
        if k in ex:
            row[k] = ex[k]
    info = dict(dm=dm, sig=sig, pb=pb, pt=pt, prof=prof, vps=vps, rs=rs, vr=vr)
    return row, info


def confluence_count(r):
    c = 0
    if r.get('patternJustBroke') == 1:
        c += 1
    if r.get('kdjGoldenCrossRecent') == 1 or r.get('kdjDeathCrossRecent') == 1:
        c += 1
    if r.get('goldenCrossRecent') == 1:
        c += 1
    bp = r.get('bbPos')
    if isnum(bp) and (bp <= 20 or bp >= 80):
        c += 1
    vr = r.get('volRatio')
    if isnum(vr) and vr >= 1.5:
        c += 1
    return c


# ────────────────────────────────────────────────────────────────────
#  條件旗標定義（多因子複選搜尋／飆股搜尋／指定組合共用）
# ────────────────────────────────────────────────────────────────────
def _col(df, c):
    return df[c] if c in df.columns else pd.Series(np.nan, index=df.index)


# 美股：2026-09-27 回測中分價量表四個訊號單獨看都沒有超額報酬，不當組合搜尋條件；要恢復改成 True
USE_VP_FLAGS_US = False


# 新增的技術面條件旗標（欄位, 名稱, 判斷式）
NEW_TECH_FLAGS = [
    ('newHigh52', '創52週新高', lambda s: s.eq(1)),
    ('high52Dist', '距52週高點≤5%', lambda s: s.ge(-5)),
    ('maBull', '均線多頭排列(5>20>60且站上月線)', lambda s: s.eq(1)),
    ('bbwRank', '布林通道收窄(寬度近半年最低20%)', lambda s: s.le(20)),
    ('gapUp3', '近3日向上跳空缺口', lambda s: s.eq(1)),
    # 2026-10-03 參考 BBW+MACD 掃描器新增；2026-10-04 三年三段回測：突破季線／半年線、MACD零軸下金叉無效或反向，
    # 已移除旗標（crossMa60_3／crossMa100_3／gcBelow0_3 欄位仍保留在回測紀錄裡）
    ('vol3Ratio', '連續放量(近3日均量≥1.5倍前20日均量)', lambda s: s.ge(1.5)),
    # 2026-10-04 買賣力道近似（待回測驗證）
    ('udVolRatio20', '漲時量≥跌時量1.5倍(近20日)', lambda s: s.ge(1.5)),
    ('cmf20', 'CMF資金流買方佔優(近20日≥0.1)', lambda s: s.ge(0.1)),
    ('obvNewHigh60', 'OBV能量潮創60日新高', lambda s: s.eq(1)),
    # 2026-10-05 新增（待回測驗證）
    ('pullMa60', '多頭回測季線(季線上揚、距季線±3%)', lambda s: s.eq(1)),
    ('bias20', '短線過熱(高於月線≥20%)', lambda s: s.ge(20)),
    ('drop5', '近5日跌幅≥8%(短線超跌)', lambda s: s.le(-8)),
    ('nr7', 'NR7窄幅日(近7日振幅最小)', lambda s: s.eq(1)),
]
# 不放進「多因子複選搜尋／飆股搜尋」的條件（回測貢獻極低或與個別型態重複；指定組合比對與統計仍可使用）
SEARCH_EXCLUDE_FLAGS = {'型態成形中', '型態突破確認', '型態剛形成(剛突破)', '突破上升軌道線剛形成',
                        '分價量表-守穩POC買進', '分價量表-突破POC追價買進', '分價量表-反彈POC遇壓賣出', '分價量表-破位停損賣出'}
# 2026-10-05 美股三年三段回測稽核：下列條件單獨無效（或只在單一行情有效），且幾乎不出現在三段都穩定的選股／飆股組合，
# 不再放進搜尋以減少雜訊與運算量（指定組合比對、追蹤統計、單一條件表仍保留）
SEARCH_EXCLUDE_FLAGS |= {'跌深反彈盤', '頭肩底剛形成', '複式頭肩底剛形成', '一字底(均線糾結)剛形成', '三重底剛形成', '圓弧底剛形成', '地量(≤0.5倍均量)', '量能區間低檔(≤10百分位)', '突破飆股大量黑K最高點剛形成', 'K線橫盤的突破剛形成', '突破ABC修正下降切線剛形成', 'N字底剛形成'}
# 2026-10-05 三年六段回測：多頭回測季線、中期弱勢 三年都明顯負報酬，不再放進搜尋（單一條件表仍顯示，可當警示）
SEARCH_EXCLUDE_FLAGS |= {'多頭回測季線(季線上揚、距季線±3%)', '中期弱勢(12-1月輸大盤≥20%)'}


def search_flags(df):
    return {k: v for k, v in build_condition_flags(df).items() if k not in SEARCH_EXCLUDE_FLAGS}


def _period_masks(dates):
    """資料跨度 ≥18個月切三段（配合「三年分三次回測」合併後逐段驗證），否則切前後兩半；同一天不會被切開。
    回傳 ([(段名, 布林遮罩), ...], 說明文字)"""
    ud = np.unique(dates)
    if len(ud) < 4:
        return [], ''
    span = (dt.date.fromisoformat(str(ud[-1])[:10]) - dt.date.fromisoformat(str(ud[0])[:10])).days
    k = 3 if span >= 540 and len(ud) >= 6 else 2
    cuts = [ud[len(ud) * i // k] for i in range(1, k)]
    # 三段的命名和「三年分三段」回測一致：第1段＝最近1年、第3段＝最早（欄位由舊到新排列）
    labels = ['前半', '後半'] if k == 2 else ['第3段(最早)', '第2段', '第1段(最近)']
    parts = []
    for i in range(k):
        m = np.ones(len(dates), bool)
        if i > 0:
            m &= dates >= cuts[i - 1]
        if i < k - 1:
            m &= dates < cuts[i]
        parts.append((labels[i], m))
    desc = (f'前後半段以 {cuts[0]} 為界' if k == 2 else f'依評估日分三段（以 {cuts[0]}、{cuts[1]} 為界）')
    return parts, desc


def _t_of(ex):
    n = len(ex)
    if n < 5:
        return None
    sd = ex.std(ddof=1)
    return round(float(ex.mean() / (sd / math.sqrt(n))), 2) if sd > 0 else None


def build_condition_flags(df: pd.DataFrame):
    """回傳 {旗標名稱: bool Series}；欄位全部是空值的旗標不會出現（跟 HTML 版規則一致）"""
    F = {}

    def has(c):
        return c in df.columns and df[c].notna().any()

    def eq1(c):
        return _col(df, c).eq(1)

    sc = _col(df, 'score')
    F['多方力道≥65'] = sc.ge(65)
    F['多方力道≥80'] = sc.ge(80)
    F['強勢突破盤'] = eq1('isBreakout')
    F['跌深反彈盤'] = eq1('isPullbackRebound')
    bp = _col(df, 'bbPos')
    F['布林通道低檔(≤20%)'] = bp.le(20)
    F['布林通道高檔(≥80%)'] = bp.ge(80)
    F['MACD近3日內黃金交叉'] = eq1('goldenCrossRecent')
    if has('kdjGoldenCrossRecent'):
        F['KDJ近3日內黃金交叉'] = eq1('kdjGoldenCrossRecent')
    if has('kdjDeathCrossRecent'):
        F['KDJ近3日內死亡交叉'] = eq1('kdjDeathCrossRecent')
    if has('relStrength20'):
        F['相對強弱為正(強於大盤)'] = df['relStrength20'].gt(0)
        F['相對強弱為負(弱於大盤)'] = df['relStrength20'].lt(0)
        F['近20日強於大盤≥10%'] = df['relStrength20'].ge(10)
    if has('volRatio'):
        F['爆量(≥1.5倍均量)'] = df['volRatio'].ge(1.5)
        F['爆量(≥2倍均量)'] = df['volRatio'].ge(2)
        F['地量(≤0.5倍均量)'] = df['volRatio'].le(0.5)
    if has('volRangePos'):
        F['量能區間高檔(≥90百分位)'] = df['volRangePos'].ge(90)
        F['量能區間低檔(≤10百分位)'] = df['volRangePos'].le(10)
    if has('volTrend'):
        F['量能斜率轉強(近5日均量>近10日均量20%以上)'] = df['volTrend'].ge(20)
        F['量能斜率轉弱(近5日均量<近10日均量20%以上)'] = df['volTrend'].le(-20)
    if USE_VP_FLAGS_US and has('vpBuySupport'):
        for fld, lbl in VP_LABELS.items():
            F[lbl] = eq1(fld)
    if has('benchmarkAbove20'):
        F['大盤站上20日均線'] = eq1('benchmarkAbove20')
        F['大盤跌破20日均線'] = _col(df, 'benchmarkAbove20').eq(0)
    if has('benchmarkAbove60'):
        F['大盤站上60日均線'] = eq1('benchmarkAbove60')
        F['大盤跌破60日均線'] = _col(df, 'benchmarkAbove60').eq(0)
    if has('sectorAbove20'):
        F['類股站上20日均線'] = eq1('sectorAbove20')
        F['類股跌破20日均線'] = _col(df, 'sectorAbove20').eq(0)
    if has('sectorAbove60'):
        F['類股站上60日均線'] = eq1('sectorAbove60')
        F['類股跌破60日均線'] = _col(df, 'sectorAbove60').eq(0)
    if has('patternFormed'):
        F['型態成形中'] = eq1('patternFormed')
    if has('patternBreakout'):
        F['型態突破確認'] = eq1('patternBreakout')
    if has('patternJustBroke'):
        F['型態剛形成(剛突破)'] = eq1('patternJustBroke')
    if has('pbAllPass'):
        F['回後買上漲全通過'] = eq1('pbAllPass')
    # 2026-10-04 美股新增：財報驚喜／財報後跳空（PEAD）、中期動能（扣近1月）
    if has('epsSurprise') and has('earnDays'):
        es, ed = df['epsSurprise'], df['earnDays']
        recent = ed.le(63)
        F['近1季財報EPS優於預期'] = es.gt(0) & recent
        F['近1季財報EPS大幅優於預期(≥10%)'] = es.ge(10) & recent
        F['近1季財報EPS低於預期'] = es.lt(0) & recent
    if has('earnGap20'):
        F['近20日財報跳空上漲'] = eq1('earnGap20')
    if has('rs12_1'):
        F['中期強勢(12-1月勝大盤≥20%)'] = df['rs12_1'].ge(20)
        F['中期弱勢(12-1月輸大盤≥20%)'] = df['rs12_1'].le(-20)
    if has('rs6_1'):
        F['半年強勢(6-1月勝大盤≥10%)'] = df['rs6_1'].ge(10)
    for fld, lbl, fn in NEW_TECH_FLAGS:
        if has(fld):
            F[lbl] = fn(df[fld])
    if has('divTotal'):
        F['近1季乖離度為正(營收優於股價)'] = df['divTotal'].gt(0)
        F['近1季乖離度為負(股價超前營收)'] = df['divTotal'].lt(0)
    if has('priceYoy1q'):
        F['近1季均價YoY為正'] = df['priceYoy1q'].gt(0)
        F['近1季均價YoY為負'] = df['priceYoy1q'].lt(0)
    if any(has(f) for f in PH_FIELDS):
        for pid, name in PATTERN_DEFS:
            F[name + '剛形成'] = eq1('ph_' + pid)
    return {k: v.fillna(False).astype(bool) for k, v in F.items()}


# ────────────────────────────────────────────────────────────────────
#  回測統計
# ────────────────────────────────────────────────────────────────────
HORIZONS = [5, 10, 20]


def stat_row(label_key, label_val, g: pd.DataFrame):
    row = {label_key: label_val, '樣本數': len(g)}
    for h in HORIZONS:
        v = g[f'ret{h}d'].dropna()
        row[f'{h}日平均報酬%'] = round(v.mean(), 2) if len(v) else None
        row[f'{h}日勝率%'] = round((v > 0).mean() * 100, 1) if len(v) else None
        row[f'{h}日中位數%'] = round(v.median(), 2) if len(v) else None
    return row


def score_buckets(df):
    def bk(s):
        return '80-100（積極做多）' if s >= 80 else '65-79（可考慮進場）' if s >= 65 else '50-64（觀望）' if s >= 50 else '0-49（不建議）'
    order = ['0-49（不建議）', '50-64（觀望）', '65-79（可考慮進場）', '80-100（積極做多）']
    b = df['score'].map(bk)
    return pd.DataFrame([stat_row('評分區間', k, df[b == k]) for k in order if (b == k).any()])


def tag_hitrate(df, field, label):
    if field not in df.columns or df[field].isna().all():
        return pd.DataFrame()
    rows = []
    for val, nm in ((0, '否'), (1, '是')):
        g = df[df[field] == val]
        if len(g):
            rows.append(stat_row('標記', f'{label}＝{nm}', g))
    return pd.DataFrame(rows)


def baseline_row(df):
    return pd.DataFrame([stat_row('標記', '全體基準', df)])


def new_tech_table(df):
    rows = []
    flg = build_condition_flags(df)
    for fld, lbl, _ in NEW_TECH_FLAGS:
        if lbl not in flg or fld not in df.columns:
            continue
        ok = df[fld].notna().values
        yes = flg[lbl].values & ok
        for nm, m in (('是', yes), ('否', ok & ~yes)):
            if m.sum():
                rows.append(stat_row('條件', f'{lbl}＝{nm}', df[m]))
    return pd.DataFrame(rows)


def pattern_hits(df):
    rows = []
    for pid, name in PATTERN_DEFS:
        c = 'ph_' + pid
        if c not in df.columns:
            continue
        g = df[df[c] == 1]
        if len(g) >= 10:
            rows.append(stat_row('型態', name + '剛形成', g))
    rows.sort(key=lambda r: -r['樣本數'])
    return pd.DataFrame(rows)


def confluence_table(df):
    rows = []
    for n in range(6):
        g = df[df['confluenceCount'] == n]
        if len(g):
            rows.append(stat_row('訊號堆疊數量', f'{n}個訊號同時觸發', g))
    return pd.DataFrame(rows)


def grid_search(df, h):
    col = f'ret{h}d'
    v = df.dropna(subset=[col, 'plusDI', 'minusDI', 'adx'])
    if not len(v):
        return pd.DataFrame()
    pdi, mdi, adx = v['plusDI'].values, v['minusDI'].values, v['adx'].values
    adxr = v['adxr'].values
    ret = v[col].values
    out = []
    for cap in (30, 40, 50):
        for bonus in (15, 20, 25):
            for w in (0.4, 0.5, 0.6):
                amax = max(0, 100 - 100 * w - bonus)
                s = pdi + mdi
                dom = np.where(s > 0, pdi / np.where(s > 0, s, 1) * 100, 50)
                sc = dom * w + np.minimum(adx, cap) / cap * amax + np.where(~np.isnan(adxr) & (adx > adxr), bonus, 0)
                sc = np.clip(sc, 0, 100)
                for thr in (50, 65, 80):
                    m = sc >= thr
                    if m.sum() < 20:
                        continue
                    r = ret[m]
                    out.append({'方向性權重': w, 'ADX滿分上限': cap, 'ADXR加分': bonus, '買進門檻': thr,
                                '訊號數': int(m.sum()), f'{h}日平均報酬%': round(r.mean(), 2),
                                f'{h}日勝率%': round((r > 0).mean() * 100, 1)})
    out.sort(key=lambda r: -r[f'{h}日平均報酬%'])
    return pd.DataFrame(out[:15])


def _flag_matrix(df, flags):
    names = list(flags.keys())
    M = np.column_stack([flags[n].values for n in names]) if names else np.zeros((len(df), 0), bool)
    return names, M


def combo_search(df, h, max_size=3, min_samples=50, min_winrate=50.0, skip_redundant=True, sort_by='t'):
    """多因子複選搜尋：窮舉1~3個條件的AND組合；向量化，50個旗標約數秒完成"""
    col = f'ret{h}d'
    valid = df[col].notna().values
    v = df[valid].reset_index(drop=True)
    flags = {kk: pd.Series(ff.values[valid]) for kk, ff in search_flags(df).items()}
    names, M = _flag_matrix(v, flags)
    k = len(names)
    if not len(v) or not k:
        return pd.DataFrame(), 0, {}
    ret = v[col].values.astype(float)
    pos = ret > 0
    base = dict(n=len(v), avg=ret.mean(), win=pos.mean() * 100, median=float(np.median(ret)))
    # 同日超額報酬：扣掉同一評估日全部紀錄的平均報酬（去除大盤齊漲齊跌的共同成分）
    if 'evalDate' in v.columns:
        exr = ret - v.groupby('evalDate')[col].transform('mean').values.astype(float)
    else:
        exr = ret - ret.mean()
    Mi = M.astype(np.int32)
    single = Mi.sum(0)
    pair = Mi.T @ Mi
    tested = 0
    hitsz = {1: [], 2: [], 3: []}   # (combo tuple, n, sum, winrate)，依條件數分開保持窮舉順序

    def consider(combo, n, s, w):
        if n < min_samples:
            return
        wr = w / n * 100
        if wr < min_winrate:
            return
        hitsz[len(combo)].append((combo, n, s, wr))

    # size 1
    for i in range(k):
        tested += 1
        n = single[i]
        if n:
            m = M[:, i]
            consider((i,), int(n), ret[m].sum(), int(pos[m].sum()))
    if max_size >= 2:
        for i in range(k):
            idx_i = np.flatnonzero(M[:, i])
            if not len(idx_i):
                tested += k - i - 1
                continue
            sub = M[idx_i]
            rs = ret[idx_i]
            ps = pos[idx_i].astype(np.int64)
            cnt = sub.sum(0)
            sm = rs @ sub
            wn = ps @ sub
            for j in range(i + 1, k):
                tested += 1
                n = int(cnt[j])
                if skip_redundant and (n == single[i] or n == single[j]):
                    continue
                consider((i, j), n, sm[j], int(wn[j]))
            if max_size >= 3:
                for j in range(i + 1, k):
                    nij = int(cnt[j])
                    if nij < min_samples:
                        tested += k - j - 1
                        continue
                    sel = sub[:, j]
                    sub2 = sub[sel]
                    rs2 = rs[sel]
                    ps2 = ps[sel]
                    c2 = sub2.sum(0)
                    s2 = rs2 @ sub2
                    w2 = ps2 @ sub2
                    for l in range(j + 1, k):
                        tested += 1
                        n = int(c2[l])
                        if skip_redundant and (n == nij or n == pair[i, l] or n == pair[j, l]):
                            continue
                        consider((i, j, l), n, s2[l], int(w2[l]))
    rows = []
    # 前後半段驗證：依評估日切兩半，各自算同日調整 t 值；兩段都 ≥2 才比較像真訊號、不是某段行情的巧合
    parts, pdesc = _period_masks(v['evalDate'].astype(str).values) if 'evalDate' in v.columns else ([], '')
    base['parts'] = pdesc
    for combo, n, s, wr in hitsz[1] + hitsz[2] + hitsz[3]:
        m = np.all(M[:, list(combo)], axis=1)
        r = ret[m]
        avg = r.mean()
        ex = exr[m]
        exm = ex.mean()
        sd = ex.std(ddof=1) if n > 1 else float('nan')
        t = exm / (sd / math.sqrt(n)) if sd and sd > 0 else float('nan')
        rows.append({'條件組合': ' ＋ '.join(names[i] for i in combo), '條件數': len(combo), '樣本數': n,
                     f'{h}日平均報酬%': round(avg, 2), f'{h}日勝率%': round(wr, 1),
                     '同日超額報酬%': round(exm, 2), '中位數報酬%': round(float(np.median(r)), 2),
                     't值(同日調整)': round(t, 2) if not math.isnan(t) else None})
        if parts:
            ts = [_t_of(exr[m & pm]) for _, pm in parts]
            ok = [x is not None and x >= 2 for x in ts]
            for (lbl, _), t_ in zip(parts, ts):
                rows[-1][f't值({lbl})'] = t_
            rows[-1]['各段一致'] = '✅' if all(ok) else (f'⚠️{sum(ok)}/{len(ok)}' if any(ok) else '')
    out = pd.DataFrame(rows)
    if len(out):
        if sort_by == 't':   # 依同日調整t值排序：優先列出「真的比同一天其他股票強」的選股型組合
            out = out.sort_values('t值(同日調整)', ascending=False, kind='mergesort', na_position='last').reset_index(drop=True)
        else:
            out = out.sort_values(f'{h}日勝率%', ascending=False, kind='mergesort').reset_index(drop=True)
    return out, tested, base


def moonshot_search(df, h, threshold=30.0, max_size=3, min_samples=20, min_pct=10.0, skip_redundant=True):
    col = f'ret{h}d'
    valid = df[col].notna().values
    v = df[valid].reset_index(drop=True)
    flags = {kk: pd.Series(ff.values[valid]) for kk, ff in search_flags(df).items()}
    names, M = _flag_matrix(v, flags)
    k = len(names)
    if not len(v) or not k:
        return pd.DataFrame(), 0, {}
    ret = v[col].values.astype(float)
    moon = ret > threshold
    base = dict(n=len(v), moonN=int(moon.sum()), pct=moon.mean() * 100,
                avg=ret[moon].mean() if moon.any() else float('nan'))
    Mi = M.astype(np.int32)
    single = Mi.sum(0)
    pair = Mi.T @ Mi
    tested = 0
    hitsz = {1: [], 2: [], 3: []}

    def consider(combo, n, mn):
        if n < min_samples or mn == 0:
            return
        if mn / n * 100 < min_pct:
            return
        hitsz[len(combo)].append((combo, n, mn))

    for i in range(k):
        tested += 1
        if single[i]:
            consider((i,), int(single[i]), int(moon[M[:, i]].sum()))
    if max_size >= 2:
        for i in range(k):
            idx_i = np.flatnonzero(M[:, i])
            if not len(idx_i):
                tested += k - i - 1
                continue
            sub = M[idx_i]
            ms = moon[idx_i].astype(np.int64)
            cnt = sub.sum(0)
            mc = ms @ sub
            for j in range(i + 1, k):
                tested += 1
                n = int(cnt[j])
                if skip_redundant and (n == single[i] or n == single[j]):
                    continue
                consider((i, j), n, int(mc[j]))
            if max_size >= 3:
                for j in range(i + 1, k):
                    nij = int(cnt[j])
                    if nij < min_samples:
                        tested += k - j - 1
                        continue
                    sel = sub[:, j]
                    sub2 = sub[sel]
                    ms2 = ms[sel]
                    c2 = sub2.sum(0)
                    m2 = ms2 @ sub2
                    for l in range(j + 1, k):
                        tested += 1
                        n = int(c2[l])
                        if skip_redundant and (n == nij or n == pair[i, l] or n == pair[j, l]):
                            continue
                        consider((i, j, l), n, int(m2[l]))
    rows = []
    parts, pdesc = _period_masks(v['evalDate'].astype(str).values) if 'evalDate' in v.columns else ([], '')
    base['parts'] = pdesc
    for combo, n, mn in hitsz[1] + hitsz[2] + hitsz[3]:
        m = np.all(M[:, list(combo)], axis=1)
        r = ret[m & moon]
        pct = mn / n * 100
        rows.append({'條件組合': ' ＋ '.join(names[i] for i in combo), '條件數': len(combo), '樣本數': n,
                     '飆股次數': mn, '飆股比例%': round(pct, 1), '飆股平均漲幅%': round(r.mean(), 1),
                     '倍數(vs基準)': round(pct / base['pct'], 2) if base['pct'] > 0 else None})
        if parts:
            for lbl, hm in parts:
                nn = int((m & hm).sum())
                rows[-1][f'飆股比例({lbl})%'] = round((m & hm & moon).sum() / nn * 100, 1) if nn else None
    out = pd.DataFrame(rows)
    if len(out):
        out = out.sort_values('飆股比例%', ascending=False, kind='mergesort').reset_index(drop=True)
    return out, tested, base


# 擇時型因子：這些條件本身描述的是「大盤／整體位置」，含這些條件的高勝率組合，勝率多半來自
# 「出現在大盤反彈的日子」（2026-09-27 台股回測：扣掉同日大盤後超額報酬≈0），
# 適合拿來判斷大盤是否落底，不適合當選股依據。
TIMING_FACTORS = {'布林通道低檔(≤20%)', '大盤跌破20日均線', '大盤跌破60日均線', '類股跌破20日均線', '類股跌破60日均線'}


def is_timing_combo(combo):
    return any(c in TIMING_FACTORS for c in combo)


def combo_type_label(combo):
    return '擇時型' if is_timing_combo(combo) else ''


def pinned_combo_stats(df, combos, winrates, h, stats_fmt=None, type_label=None):
    """指定組合在目前回測資料的表現；多了同日超額報酬、t值(同日調整)與「判讀」（選股有效／擇時／無效）"""
    col = f'ret{h}d'
    valid = df[col].notna().values
    v = df[valid]
    flags = {kk: pd.Series(ff.values[valid]) for kk, ff in build_condition_flags(df).items()}
    ret_all = v[col].values.astype(float)
    exr = ret_all - (v.groupby('evalDate')[col].transform('mean').values.astype(float)
                     if 'evalDate' in v.columns else ret_all.mean())
    out = []
    for combo in combos:
        key = ' ＋ '.join(combo)
        star = '⭐ ' if ('KDJ近3日內黃金交叉' in combo and '晨星剛形成' in combo) else ''
        rec_txt = stats_fmt(key) if stats_fmt else format_winrates(winrates.get(key))
        row = {'條件組合': star + key, '類型': type_label or combo_type_label(combo), '樣本數': 0, '歷史記錄': rec_txt,
               f'{h}日平均報酬%': None, f'{h}日勝率%': None, '同日超額報酬%': None, 't值(同日調整)': None,
               '判讀': '', '備註': ''}
        miss = [c for c in combo if c not in flags]
        if miss:
            row['備註'] = '缺少欄位（可能沒勾選對應的可選資料）：' + '、'.join(miss)
            out.append(row)
            continue
        m = np.logical_and.reduce([flags[c].values for c in combo])
        r = v[col].values[m]
        row['樣本數'] = int(m.sum())
        if not len(r):
            row['備註'] = '目前回測資料裡沒有符合這個組合的樣本'
        else:
            row[f'{h}日平均報酬%'] = round(r.mean(), 2)
            row[f'{h}日勝率%'] = round((r > 0).mean() * 100, 1)
            ex = exr[m]
            exm = ex.mean()
            sd = ex.std(ddof=1) if len(ex) > 1 else 0
            t = exm / (sd / math.sqrt(len(ex))) if sd > 0 else None
            row['同日超額報酬%'] = round(exm, 2)
            row['t值(同日調整)'] = round(t, 2) if t is not None else None
            if len(r) < 30:
                row['判讀'] = '樣本不足'
            elif t is not None and t >= 2:
                row['判讀'] = '✅ 選股有效'
            elif row[f'{h}日勝率%'] >= 60:
                row['判讀'] = '⏱ 擇時（勝率來自大盤）'
            else:
                row['判讀'] = '❌ 無效'
            row['備註'] = '' if len(r) >= 50 else '⚠️樣本數偏少，僅供參考'
        out.append(row)
    return pd.DataFrame(out)


def format_stockpick_stats(st_):
    if not st_:
        return ''
    return '　｜　'.join(f"{h}日：{e['n']}筆 勝率{e['win']}% 同日超額{e['ex']:+.2f}% t={e['t']}"
                        for h, e in sorted(st_.items(), key=lambda kv: int(kv[0])))


def format_winrates(wr):
    if not wr:
        return ''
    return ' / '.join(f"{h}日{wr[str(h)]}%" for h in (5, 10, 20) if str(h) in wr and wr[str(h)] is not None)


def format_moonshot_stats(st_):
    if not st_:
        return ''
    parts = []
    for h in (5, 10, 20):
        e = st_.get(str(h))
        if e:
            parts.append(f"{h}日：{e['n']}筆中{e['moonshotN']}次飆股({e['pct']}%)，平均漲幅+{e['avg']}%")
    return '　｜　'.join(parts)


# ════════════════════════════════════════════════════════════════════
#  Financial Modeling Prep (FMP) 資料抓取
#  所有請求都經過同一個滑動視窗限流器（預設 280 次/分鐘，Starter 方案上限 300），
#  多執行緒平行查詢時也不會超過。
# ════════════════════════════════════════════════════════════════════
import threading
from concurrent.futures import ThreadPoolExecutor

FMP_BASE = 'https://financialmodelingprep.com/stable'
RATE_LIMIT_PER_MIN = 280
MARKET_SCAN_CONCURRENCY = 20
_http = requests.Session()
_tls = threading.local()


def _session():
    """每個執行緒各用一個 Session（requests.Session 跨執行緒共用不保證安全，平行回測時可能卡住）"""
    s = getattr(_tls, 's', None)
    if s is None:
        s = requests.Session()
        ad = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=4)
        s.mount('https://', ad)
        s.mount('http://', ad)
        _tls.s = s
    return s
_rl_lock = threading.Lock()
_rl_times = []


def set_rate_limit(n):
    global RATE_LIMIT_PER_MIN
    RATE_LIMIT_PER_MIN = int(n)


def rate_limit_acquire():
    while True:
        with _rl_lock:
            now = time.time()
            while _rl_times and now - _rl_times[0] >= 60:
                _rl_times.pop(0)
            if len(_rl_times) < RATE_LIMIT_PER_MIN:
                _rl_times.append(now)
                return
            wait = 60 - (now - _rl_times[0]) + 0.02
        time.sleep(max(wait, 0.05))


def to_fmp_symbol(sid):
    """S&P500 清單裡的 BRK.B / BF.B，FMP 慣例用「-」"""
    return sid.replace('.', '-')


def fmp_get(path, params, token, timeout=(10, 25), retries=2):
    """連線逾時／中斷、429（超過每分鐘上限）、5xx 會自動重試 2 次（間隔遞增）"""
    for attempt in range(retries + 1):
        rate_limit_acquire()
        try:
            r = _session().get(FMP_BASE + path, params={**params, 'apikey': token}, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as ex:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f'連線逾時／中斷（{type(ex).__name__}）')
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(5 * (attempt + 1) if r.status_code == 429 else 2 * (attempt + 1))
            continue
        break
    try:
        j = r.json()
    except Exception:  # noqa
        j = None
    if r.status_code != 200:
        msg = (j.get('Error Message') or j.get('error') or j.get('message')) if isinstance(j, dict) else None
        raise RuntimeError(msg or f'HTTP {r.status_code}：{r.text[:200]}')
    if isinstance(j, dict) and j.get('Error Message'):
        raise RuntimeError(j['Error Message'])
    return j


def _ds(d):
    return d.isoformat()


def fetch_price(sid, token, days=None, start=None):
    end = dt.date.today()
    st_ = start or (end - dt.timedelta(days=days))
    j = fmp_get('/historical-price-eod/full', dict(symbol=to_fmp_symbol(sid), **{'from': _ds(st_), 'to': _ds(end)}), token)
    rows = j if isinstance(j, list) else (j or {}).get('historical')
    if not rows:
        raise RuntimeError('無資料（可能代號錯誤，或 API 額度已用完）')
    out = [dict(date=str(r['date'])[:10], open=float(r['open']), high=float(r['high']), low=float(r['low']),
                close=float(r['close']), volume=float(r.get('volume') or 0)) for r in rows]
    out = [r for r in out if r['close'] > 0 and r['high'] > 0 and r['low'] > 0]   # 0價（無成交／暫停交易）的日子會讓指標失真
    out.sort(key=lambda r: r['date'])
    return out


BENCHMARK_ID = 'SPY'


def fetch_benchmark(token, days, bid=BENCHMARK_ID):
    rows = fetch_price(bid, token, days)
    return Benchmark([dict(date=r['date'], close=r['close']) for r in rows])


# ── 類股狀態濾網：FMP sector → SPDR 類股 ETF ──
SECTOR_ETF_MAP = {
    'Technology': 'XLK', 'Information Technology': 'XLK', 'Financial Services': 'XLF', 'Financials': 'XLF',
    'Energy': 'XLE', 'Healthcare': 'XLV', 'Health Care': 'XLV', 'Consumer Cyclical': 'XLY',
    'Consumer Discretionary': 'XLY', 'Consumer Defensive': 'XLP', 'Consumer Staples': 'XLP',
    'Industrials': 'XLI', 'Basic Materials': 'XLB', 'Materials': 'XLB', 'Utilities': 'XLU',
    'Real Estate': 'XLRE', 'Communication Services': 'XLC',
}
_profile_cache = {}
_sector_bm_cache = {}


# ── 下一個財報公布日 ──
# 先用 /earnings-calendar 一次抓未來約150天全市場的財報日（每次最多90天，分兩段），
# 清單裡找不到的個股再用 /earnings?symbol= 單檔補抓（ETF 等沒有財報的會顯示「--」）。
EARNINGS_DAYS_AHEAD = 150


def _earn_item(x):
    d = str(x.get('date') or '')[:10]
    t = str(x.get('time') or '').lower()
    return dict(date=d, time=('盤前' if t == 'bmo' else ('盤後' if t == 'amc' else '')),
                epsEst=x.get('epsEstimated'), revEst=x.get('revenueEstimated'))


def fetch_earnings_calendar(token, days_ahead=EARNINGS_DAYS_AHEAD):
    """回傳 {FMP代號: {date, time, epsEst, revEst}}（只留今天以後最近的一次）；整段失敗時丟出例外"""
    today = dt.date.today()
    last = today + dt.timedelta(days=days_ahead)
    out, start, ok = {}, today, False
    while start <= last:
        end = min(start + dt.timedelta(days=89), last)
        try:
            j = fmp_get('/earnings-calendar', {'from': _ds(start), 'to': _ds(end)}, token)
            ok = True
        except Exception:  # noqa
            j = None
        for x in (j if isinstance(j, list) else []):
            sym = str(x.get('symbol') or '').upper()
            it = _earn_item(x)
            if sym and it['date'] >= _ds(today) and (sym not in out or it['date'] < out[sym]['date']):
                out[sym] = it
        start = end + dt.timedelta(days=1)
    if not ok:
        raise RuntimeError('財報行事曆讀取失敗')
    return out


def fetch_next_earnings(sid, token):
    """單檔補抓：/earnings 含未來預定的財報（epsActual 為空），取今天以後最近的一次"""
    j = fmp_get('/earnings', dict(symbol=to_fmp_symbol(sid), limit=12), token)
    today = _ds(dt.date.today())
    fut = sorted((_earn_item(x) for x in (j if isinstance(j, list) else []) if str(x.get('date') or '')[:10] >= today),
                 key=lambda it: it['date'])
    return fut[0] if fut else None


def fetch_earnings_hist(sid, token, limit=24):
    """/earnings 歷史（含未來預定），回傳依日期排序的 [{date, act, est}]；act 為空＝還沒公布"""
    j = fmp_get('/earnings', dict(symbol=to_fmp_symbol(sid), limit=limit), token)
    out = []
    for x in (j if isinstance(j, list) else []):
        d = str(x.get('date') or '')[:10]
        if len(d) < 10:
            continue

        def f(v):
            try:
                return float(v) if v is not None else None
            except Exception:  # noqa
                return None
        out.append(dict(date=d, act=f(x.get('epsActual')), est=f(x.get('epsEstimated')), raw=x))
    out.sort(key=lambda h: h['date'])
    return out


def next_earnings_from_hist(hist):
    today = _ds(dt.date.today())
    fut = [h for h in hist if h['date'] >= today]
    return _earn_item(fut[0]['raw']) if fut else None


def earnings_feats(b, e, hist):
    """截至第 e 根K棒（含）最近一次「已公布」的財報（日期≤當天且有實際EPS），只用當時已知的資料：
    epsSurprise＝(實際EPS−預估EPS)÷|預估|×100（限 ±500）；earnDays＝距財報日的交易日數；
    earnGap20＝財報反應日（財報當天或隔天）向上跳空、且距今≤20個交易日。沒有財報資料時全部 None"""
    out = dict(epsSurprise=None, earnDays=None, earnGap20=None)
    if not hist or e < 1:
        return out
    ds = b.date[e]
    past = [h for h in hist if h['date'] <= ds and h['act'] is not None]
    if not past:
        return out
    last = past[-1]
    import bisect
    k = bisect.bisect_left(b.date, last['date'])
    if k > e:
        return out
    out['earnDays'] = e - k
    if last['est'] is not None and abs(last['est']) > 1e-9:
        out['epsSurprise'] = max(-500.0, min(500.0, (last['act'] - last['est']) / abs(last['est']) * 100))
    gap = any(1 <= j <= e and b.low[j] > b.high[j - 1] for j in (k, k + 1))
    out['earnGap20'] = int(gap and (e - k) <= 20)
    return out


def mid_momentum(b, e, bm):
    """中期動能（扣掉最近1個月，避開短線反轉）：12-1月＝第e-252到e-21根的漲幅、6-1月＝e-126到e-21；
    rs＝扣掉同期間大盤(SPY)漲幅（百分點）"""
    out = dict(rs12_1=None, rs6_1=None)
    for fld, far in (('rs12_1', 252), ('rs6_1', 126)):
        if e < far or not b.close[e - far]:
            continue
        m = b.close[e - 21] / b.close[e - far] - 1
        if bm is None:
            continue
        i1, i0 = bm.find(b.date[e - 21]), bm.find(b.date[e - far])
        if i0 >= 0 and i1 >= 0 and bm.close[i0]:
            out[fld] = (m - (bm.close[i1] / bm.close[i0] - 1)) * 100
    return out


def earnings_label(e):
    """財報日欄位文字：2026-10-28（27天）盤後；今天／明天特別標示"""
    if not e or not e.get('date'):
        return '--'
    try:
        n = (dt.date.fromisoformat(e['date']) - dt.date.today()).days
    except Exception:  # noqa
        return e['date']
    tail = '今天' if n == 0 else ('明天' if n == 1 else f'{n}天')
    return f"{e['date']}（{tail}）" + (e.get('time') or '')


def fetch_profile(sid, token):
    if sid in _profile_cache:
        return _profile_cache[sid]
    try:
        j = fmp_get('/profile', dict(symbol=to_fmp_symbol(sid)), token)
        row = j[0] if isinstance(j, list) and j else {}
    except Exception:  # noqa
        row = {}
    _profile_cache[sid] = row
    return row


def fetch_name(sid, token, names=None):
    if names and sid in names:
        return names[sid]
    return fetch_profile(sid, token).get('companyName') or sid


def get_sector_benchmark(token, days, sector):
    etf = SECTOR_ETF_MAP.get(sector or '')
    if not etf:
        return None
    key = (etf, days)
    if key not in _sector_bm_cache:
        try:
            _sector_bm_cache[key] = fetch_benchmark(token, days, etf)
        except Exception:  # noqa
            _sector_bm_cache[key] = None
    return _sector_bm_cache[key]


def reset_sector_cache():
    _sector_bm_cache.clear()


def fetch_pe_range(sid, token, years=3):
    """近3年 P/E 區間（/ratios 年頻；季頻在基本方案會回 402）"""
    j = fmp_get('/ratios', dict(symbol=to_fmp_symbol(sid), period='annual', limit=years + 1), token)
    if not isinstance(j, list) or not j:
        raise RuntimeError('回傳空陣列（可能此方案未開通 /ratios）')
    j = sorted(j, key=lambda r: str(r.get('date')))
    vals = []
    for r in j:
        v = r.get('priceToEarningsRatio', r.get('priceEarningsRatio', r.get('peRatio')))
        try:
            v = float(v)
        except Exception:  # noqa
            continue
        if math.isfinite(v) and v > 0:
            vals.append(v)
    if not vals:
        raise RuntimeError('解析不出P/E欄位')
    return dict(min=min(vals), max=max(vals), current=vals[-1])


def _q_of(datestr):
    y, mo = int(datestr[:4]), int(datestr[5:7])
    return y, (mo - 1) // 3 + 1


def _prev_q(y, q):
    return (y - 1, 4) if q == 1 else (y, q - 1)


def fetch_revenue_yoy_qoq(sid, token):
    """近3季營收 YoY／QoQ（/income-statement 季報；季度以財報期末日推算日曆季）"""
    j = fmp_get('/income-statement', dict(symbol=to_fmp_symbol(sid), period='quarter', limit=20), token)
    if not isinstance(j, list) or not j:
        return None
    items = []
    for r in j:
        if r.get('revenue') is None:
            continue
        d = str(r['date'])[:10]
        y, q = _q_of(d)
        items.append((d, y, q, float(r['revenue'])))
    if not items:
        return None
    items.sort()
    mp = {(y, q): v for _, y, q, v in items}
    out = []
    for _, y, q, v in items[-3:]:
        pv = mp.get(_prev_q(y, q))
        ly = mp.get((y - 1, q))
        out.append(dict(year=y, quarter=q, revenue=v, qoq=(v - pv) / pv * 100 if pv else None,
                        yoy=(v - ly) / ly * 100 if ly else None))
    return out


def last_n_calendar_quarters(n):
    t = dt.date.today()
    y, q = t.year, (t.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.insert(0, dict(year=y, quarter=q))
        y, q = _prev_q(y, q)
    return out


def fetch_quarterly_avg_price_yoy(sid, token, quarters=None):
    quarters = quarters or last_n_calendar_quarters(3)
    rows = fetch_price(sid, token, start=dt.date(quarters[0]['year'] - 1, 1, 1))
    sums, cnts = {}, {}
    for r in rows:
        k = _q_of(r['date'])
        if r['close'] > 0:
            sums[k] = sums.get(k, 0) + r['close']
            cnts[k] = cnts.get(k, 0) + 1
    avg = {k: sums[k] / cnts[k] for k in sums}
    out = []
    for qq in quarters:
        ta, la = avg.get((qq['year'], qq['quarter'])), avg.get((qq['year'] - 1, qq['quarter']))
        out.append(dict(year=qq['year'], quarter=qq['quarter'], avg=ta, avgLastYear=la,
                        yoy=(ta - la) / la * 100 if (ta is not None and la) else None))
    return out


def fetch_quote(sid, token):
    try:
        j = fmp_get('/quote', dict(symbol=to_fmp_symbol(sid)), token)
        return j[0] if isinstance(j, list) and j else None
    except Exception:  # noqa
        return None


def merge_realtime_quote(rows, q):
    """把 /quote 的最新報價合成一根今天的K棒（日期由 timestamp 換算，收盤前後可能有日期邊界誤差）"""
    if not q or q.get('timestamp') is None or q.get('price') is None:
        return rows, False
    try:
        today = dt.datetime.utcfromtimestamp(float(q['timestamp'])).date().isoformat()
        c = float(q['price'])
    except Exception:  # noqa
        return rows, False
    if (rows and today <= rows[-1]['date']) or not c or not math.isfinite(c):
        return rows, False

    def g(k):
        try:
            v = float(q.get(k))
            return v if (math.isfinite(v) and v) else c
        except Exception:  # noqa
            return c
    try:
        vol = float(q.get('volume') or 0)
    except Exception:  # noqa
        vol = 0
    return rows + [dict(date=today, open=g('open'), high=g('dayHigh'), low=g('dayLow'), close=c,
                        volume=vol if math.isfinite(vol) else 0)], True


UNIVERSE_MIN_PRICE = 10.0        # 全美股股票池：股價 ≥10 美元
UNIVERSE_MIN_DOLLAR_VOL = 1e7     # 成交金額（股價×成交量）≥1000 萬美元
UNIVERSE_MAX = 2000               # 依成交金額由大到小取前 2000 檔


def filter_universe_rows(rows, min_price=UNIVERSE_MIN_PRICE, min_dv=UNIVERSE_MIN_DOLLAR_VOL, cap=UNIVERSE_MAX):
    """company-screener 結果 → 排除股價 <10、成交金額 <1000萬美元，依成交金額排序取前 cap 檔"""
    out = []
    for r in rows or []:
        sym = r.get('symbol')
        if not sym:
            continue
        try:
            px = float(r.get('price') or 0)
        except Exception:  # noqa
            px = 0.0
        try:
            vol = float(r.get('volume') or 0)
        except Exception:  # noqa
            vol = 0.0
        if px and px < min_price:
            continue
        dv = px * vol
        if px and vol and dv < min_dv:
            continue
        out.append((dv, sym, r.get('companyName') or sym))
    out.sort(key=lambda x: -x[0])
    return [dict(id=sym, name=nm) for _, sym, nm in out[:cap]]


def fetch_market_universe(token):
    # volumeMoreThan 只是粗篩（高價股成交股數少也可能成交金額很大），真正門檻用 股價×成交量 ≥1000萬美元
    j = fmp_get('/company-screener', dict(exchange='NYSE,NASDAQ', country='US', isEtf='false', isFund='false',
                                          isActivelyTrading='true', priceMoreThan=int(UNIVERSE_MIN_PRICE),
                                          volumeMoreThan=5000, limit=6000), token, timeout=60)
    if not isinstance(j, list) or not j:
        raise RuntimeError('company-screener 回傳空清單，請確認 API Key 是否有效')
    out = filter_universe_rows(j)
    if not out:
        raise RuntimeError('company-screener 篩選後沒有股票（股價≥10、成交金額≥1000萬美元）')
    return out


def fetch_market_snapshot(token, progress=None):
    """全市場（NYSE+NASDAQ，約2000檔）逐檔查 /quote，平行查詢＋限流；回傳 [{id,name,pct,volume}]"""
    uni = fetch_market_universe(token)
    names = {u['id']: u['name'] for u in uni}
    out, done = [], [0]
    lock = threading.Lock()

    def one(u):
        q = fetch_quote(u['id'], token)
        with lock:
            done[0] += 1
            if progress:
                progress(done[0], len(uni))
        if not q:
            return None
        pct = q.get('changesPercentage', q.get('changePercentage'))
        try:
            pct = float(pct)
        except Exception:  # noqa
            pct = None
        try:
            vol = float(q.get('volume'))
        except Exception:  # noqa
            vol = None
        return dict(id=u['id'], name=names.get(u['id']) or q.get('name') or u['id'], pct=pct, volume=vol)
    with ThreadPoolExecutor(MARKET_SCAN_CONCURRENCY) as ex:
        for r in ex.map(one, uni):
            if r:
                out.append(r)
    if not out:
        raise RuntimeError('未取得任何報價資料，請確認 API Key 是否有效')
    return out, len(uni)


# ── 分析師評等／目標價／內部人交易（美股專屬；FMP 不提供空單比例）──
def fetch_analyst_bundle(sid, token):
    sym = to_fmp_symbol(sid)
    out, errs = {}, []
    for key, path, params, first in [
        ('consensus', '/grades-consensus', {}, True), ('grades', '/grades', {}, False),
        ('pt_consensus', '/price-target-consensus', {}, True), ('pt_summary', '/price-target-summary', {}, True),
        ('insider', '/insider-trading/search', dict(page=0, limit=20), False)]:
        try:
            j = fmp_get(path, dict(symbol=sym, **params), token)
            j = j if isinstance(j, list) else []
            out[key] = (j[0] if j else None) if first else j
        except Exception as ex:  # noqa
            out[key] = None if first else []
            errs.append(f'{key}：{ex}')
    out['errs'] = errs
    return out


# ── 回測用：季營收（當時已公告判斷用季末+45天估計）──
def fetch_revenue_hist_us(sid, token, quarters_back):
    j = fmp_get('/income-statement', dict(symbol=to_fmp_symbol(sid), period='quarter',
                                          limit=min(40, quarters_back + 4)), token)
    mp = {}
    for r in j if isinstance(j, list) else []:
        if r.get('revenue') is None:
            continue
        mp[_q_of(str(r['date'])[:10])] = float(r['revenue'])
    return mp


def quarter_end(y, q):
    m = q * 3
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def revenue_known_by_us(y, q, asof):
    return dt.date.fromisoformat(asof) >= quarter_end(y, q) + dt.timedelta(days=45)


def last_n_known_quarters_us(asof, n):
    d = dt.date.fromisoformat(asof)
    y, q = d.year, (d.month - 1) // 3 + 1
    out = []
    for _ in range(8):
        if revenue_known_by_us(y, q, asof):
            out.append((y, q))
            if len(out) >= n:
                break
        y, q = _prev_q(y, q)
    return out[::-1]


def price_quarter_avg_map(b: Bars):
    sums, cnts = {}, {}
    for d, c in zip(b.date, b.close):
        k = _q_of(d)
        sums[k] = sums.get(k, 0) + c
        cnts[k] = cnts.get(k, 0) + 1
    return {k: sums[k] / cnts[k] for k in sums}


def divergence_asof_us(rev, pxq, asof):
    """評估日當下可見的最近1季：營收YoY − 均價YoY（divTotal）與單獨的均價YoY（priceYoy1q）"""
    qs = last_n_known_quarters_us(asof, 1)
    if not qs:
        return None, None
    y, q = qs[0]
    rt, rl = rev.get((y, q)), rev.get((y - 1, q))
    pt_, pl = pxq.get((y, q)), pxq.get((y - 1, q))
    pyoy = (pt_ - pl) / pl * 100 if (pl and pt_ is not None) else None
    div = None
    if rl and rt is not None and pyoy is not None:
        div = (rt - rl) / rl * 100 - pyoy
    return div, pyoy


# ════════════════════════════════════════════════════════════════════
#  歷史回測
# ════════════════════════════════════════════════════════════════════
# ── 回測記憶體：每檔結果轉成「欄位→numpy 陣列」，最後逐欄合併（合併完一欄就釋放該欄），
# 記憶體高峰約只有最終資料的 1 倍，不會像 pd.concat＋排序＋複製那樣疊到 3 倍 ──
DEAD_BT_COLS = ('crossMa60_3', 'crossMa100_3', 'gcBelow0_3')   # 已確認無效、旗標已移除的欄位，不再存進回測紀錄


def frame_arrays(d):
    return {c: d[c].to_numpy(copy=True) for c in d.columns if c not in DEAD_BT_COLS}


def low_mem_concat(frames, sort=True):
    """frames：list of {欄位: 陣列}；依 evalDate、stockId 排序後組成 DataFrame（數值欄一律 float32）"""
    frames = [f for f in frames if f and len(next(iter(f.values()))) > 0]
    if not frames:
        return pd.DataFrame()
    cols = []
    seen = set()
    for f in frames:
        for c in f:
            if c not in seen:
                seen.add(c)
                cols.append(c)
    front = [c for c in ('evalDate', 'stockId', 'name') if c in seen]
    cols = front + [c for c in cols if c not in front]
    order = None
    if sort and 'evalDate' in seen and 'stockId' in seen:
        ed = np.concatenate([np.asarray(f['evalDate']).astype(str) for f in frames])
        sid = np.concatenate([np.asarray(f['stockId']).astype(str) for f in frames])
        order = np.lexsort((sid, ed))
        del ed, sid
    lens = [len(next(iter(f.values()))) for f in frames]
    out = {}
    for c in cols:
        parts = []
        for f, n in zip(frames, lens):
            v = f.pop(c, None)
            if v is None:
                v = np.full(n, np.nan, dtype=np.float32) if c not in front else np.array([''] * n, dtype=object)
            parts.append(v)
        arr = np.concatenate(parts)
        del parts
        if order is not None:
            arr = arr[order]
        if c not in front:
            arr = pd.to_numeric(pd.Series(arr), errors='coerce').to_numpy(dtype=np.float32)
        out[c] = arr
    frames.clear()
    return pd.DataFrame(out, copy=False)


def add_derived(df, inplace=False):
    if df.attrs.get('derived'):
        return df
    if not inplace:
        df = df.copy()

    def b01(s, cond):
        return np.where(s.isna(), np.nan, cond.astype(float))
    if 'relStrength20' in df:
        df['relStrengthPositive'] = b01(df['relStrength20'], df['relStrength20'] > 0)
    if 'volRatio' in df:
        df['volSurge15'] = b01(df['volRatio'], df['volRatio'] >= 1.5)
        df['volSurge2'] = b01(df['volRatio'], df['volRatio'] >= 2)
        df['volLow05'] = b01(df['volRatio'], df['volRatio'] <= 0.5)
    if 'volRangePos' in df:
        df['volRangeHigh90'] = b01(df['volRangePos'], df['volRangePos'] >= 90)
        df['volRangeLow10'] = b01(df['volRangePos'], df['volRangePos'] <= 10)
    return df



def store_bt_df(df):
    """回測資料存進 session 前先整理一次：衍生欄位只算一次、數值轉 float32、代號／名稱轉 category，
    全美股約20萬筆時可省下大半記憶體（Streamlit Cloud 記憶體上限約 1GB，超過會整個 App 當掉）"""
    if df is None or not len(df):
        return df
    df = add_derived(df, inplace=True)   # 剛跑完／剛載入的資料不再複製一份，省一倍記憶體高峰
    for c in [c for c in DEAD_BT_COLS if c in df.columns]:
        del df[c]
    for c in df.columns:
        if c in ('evalDate', 'stockId', 'name'):
            continue
        if (pd.api.types.is_float_dtype(df[c]) or pd.api.types.is_integer_dtype(df[c])) and df[c].dtype != np.float32:
            df[c] = df[c].astype(np.float32)
    for c in ('stockId', 'name'):
        if c in df.columns and not isinstance(df[c].dtype, pd.CategoricalDtype):
            df[c] = df[c].astype(str).astype('category')
    df['evalDate'] = df['evalDate'].astype(str)
    df.attrs['derived'] = True
    return df


def release_bt_state(ss):
    """清掉上一份回測資料與衍生的快取／壓縮檔，釋放記憶體"""
    import gc
    for k in ('bt_df', '_bt_memo', '_bt_csv', '_bt_csv_sig', 'live_stats', 'live_stats_sig'):
        ss.pop(k, None)
    gc.collect()


def bt_memo_ready(ss, name, params):
    """這組參數是否已經算過（不觸發計算）"""
    df = ss.get('bt_df')
    sig = (ss.get('bt_ver'), id(df), len(df) if df is not None else 0)
    memo = ss.get('_bt_memo')
    return bool(memo) and memo.get('_sig') == sig and ((name,) + tuple(params)) in memo


def bt_memo(ss, name, params, fn):
    """回測分析結果快取（存在 session）：同一份回測資料、同樣參數只算一次，
    改任何一個選單時 Streamlit 會整頁重跑，沒有快取的話每次都要重算幾十秒。"""
    df = ss.get('bt_df')
    sig = (ss.get('bt_ver'), id(df), len(df) if df is not None else 0)
    memo = ss.get('_bt_memo')
    if memo is None or memo.get('_sig') != sig:
        memo = {'_sig': sig}
        ss['_bt_memo'] = memo
    key = (name,) + tuple(params)
    if key not in memo:
        memo[key] = fn()
    return memo[key]


def bt_csv_gz(df):
    import gzip
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=5) as gz:
        with io.TextIOWrapper(gz, encoding='utf-8-sig', newline='') as tw:
            df.to_csv(tw, index=False, float_format='%.9g')   # float32 需要9位有效數字才能原值還原
    return buf.getvalue()


def load_bt_files(files):
    """一次載入一個或多個回測原始紀錄檔（例如三年分六段各一個），逐檔轉成欄位陣列後低記憶體合併，再去除重複（同日同檔）"""
    parts = []
    for f in files:
        if hasattr(f, 'seek'):
            f.seek(0)
        d = pd.read_csv(f, compression='gzip' if f.name.endswith('.gz') else None, dtype={'stockId': str})
        for c in d.columns:   # 先逐檔轉 float32，合併時記憶體高峰較低
            if c not in ('evalDate', 'stockId', 'name') and (pd.api.types.is_float_dtype(d[c]) or pd.api.types.is_integer_dtype(d[c])):
                d[c] = d[c].astype(np.float32)
        d['evalDate'] = d['evalDate'].astype(str)
        parts.append(frame_arrays(d))
        del d
        __import__("gc").collect()
    df = low_mem_concat(parts, sort=len(parts) > 1)
    del parts
    if len(files) > 1 and len(df):
        dup = df.duplicated(['evalDate', 'stockId'], keep='last')   # 段與段交界同一天可能重複
        if dup.any():
            df = df[~dup.to_numpy()].reset_index(drop=True)
        del dup
    return store_bt_df(df)


BT_SEG_MONTHS = 6   # 三年分六段：每段半年（全美股一年一段容易超過 Streamlit Cloud 記憶體）
BT_SEG_OPTIONS = ['自訂月數'] + [f'三年分六段・第{i + 1}段（{i * 6}～{i * 6 + 6}個月前）' for i in range(6)]

WINSOR_OPTIONS = {'截尾 1%／99%（建議）': 0.01, '截尾 0.5%／99.5%': 0.005, '不處理': 0.0}
ANALYSIS_DEFAULTS = dict(min_px=10.0, wq=0.01)


def prep_analysis_df(df, min_px=10.0, wq=0.01):
    """全美股資料含大量小型股：先濾掉低價股，再把 5/10/20 日報酬截尾（winsorize），
    避免少數暴漲暴跌（雞蛋水餃股、反向分割資料錯誤）把平均報酬／t值拉歪。
    回傳 (截尾後df, 未截尾df［飆股搜尋用］, {h: (下限, 上限)}, 被濾掉的筆數)"""
    n0 = len(df)
    if min_px and min_px > 0 and 'entryClose' in df.columns:
        df = df[df['entryClose'] >= min_px]
    raw = df
    cuts = {}
    if wq and wq > 0 and len(df):
        new = {}
        for h in HORIZONS:
            c = f'ret{h}d'
            if c in df.columns and df[c].notna().any():
                lo, hi = df[c].quantile([wq, 1 - wq])
                new[c] = df[c].clip(lo, hi)
                cuts[h] = (float(lo), float(hi))
        df = df.assign(**new)   # 只換報酬欄，其他欄位共用（Copy-on-Write），不整張表複製
    return df, raw, cuts, n0 - len(raw)


def run_backtest(token, universe, months_back, include_div, include_sector, min_liq, vp_params,
                 delay=0.0, on_progress=None, should_stop=None, names=None, workers=8, min_px=0.0,
                 stock_timeout=150, stall_timeout=300, end_offset_months=0, include_earn=True):
    """回測：多執行緒平行抓資料（所有請求共用限流器），每檔算完就轉成 DataFrame 以節省記憶體。
    universe 可以是全美股約2000檔（fetch_market_universe），時間主要花在 API：2000檔約需 8～10 分鐘。"""
    buf = 60
    maxh = max(HORIZONS)
    off = int(end_offset_months or 0)   # 分段回測：評估區間往前推 off 個月（例：第2段＝1～2年前）
    price_days = (months_back + off) * 30 + buf + maxh + 10 + 400   # +400天：52週高點、布林收窄需要約一年歷史（營收乖離也夠用）
    q_back = math.ceil((months_back + off) / 3) + 5
    bm_err = None
    try:
        bm = fetch_benchmark(token, price_days)
    except Exception as ex:  # noqa
        bm, bm_err = None, str(ex)
    if include_sector:
        reset_sector_cache()
    names = names or {}
    total = len(universe)

    def one(sid):
        """回傳 (DataFrame 或 None, 失敗訊息或 None)"""
        try:
            rows = fetch_price(sid, token, price_days)
            b = Bars(rows)
            n = b.n
            ev_end = n - maxh
            if ev_end <= buf:
                return None, f'資料天數不夠（僅{n}筆，需要至少{buf + maxh + 1}筆）'
            # 評估區間起點用「實際抓到的最新一天」往回推（FMP 資料有時不是到今天）
            win_start = _ds(dt.date.fromisoformat(b.date[-1]) - dt.timedelta(days=(months_back + off) * 30))
            win_end = _ds(dt.date.fromisoformat(b.date[-1]) - dt.timedelta(days=off * 30)) if off else None
            rev, pxq = {}, {}
            if include_div:
                try:
                    rev = fetch_revenue_hist_us(sid, token, q_back)
                    pxq = price_quarter_avg_map(b)
                except Exception:  # noqa
                    rev = {}
            sector_bm = None
            if include_sector:
                try:
                    sector_bm = get_sector_benchmark(token, price_days, fetch_profile(sid, token).get('sector'))
                except Exception:  # noqa
                    sector_bm = None
            ehist = None
            if include_earn:
                try:   # 財報歷史（每季一筆，抓約6年份就夠三段回測用）
                    ehist = fetch_earnings_hist(sid, token, limit=math.ceil((months_back + off) / 3) + 12)
                except Exception:  # noqa
                    ehist = []
            pc = PatternCache(b)
            recs = []
            for e in range(buf, ev_end):
                ds = b.date[e]
                if ds < win_start or (win_end and ds >= win_end):
                    continue
                if min_px > 0 and b.close[e] < min_px:
                    continue
                if min_liq > 0:
                    s0 = max(0, e - 19)
                    liq = sum(b.close[k] * b.volume[k] for k in range(s0, e + 1)) / (e + 1 - s0)
                    if liq < min_liq:
                        continue
                ex = {'sector_bm': sector_bm}
                if include_div:
                    ex['divTotal'], ex['priceYoy1q'] = divergence_asof_us(rev, pxq, ds)
                if ehist is not None:
                    ex.update(earnings_feats(b, e, ehist))
                row, _ = build_flag_row(b, e, bm, pc, vp_params, ex)
                ec = b.close[e]
                row['entryClose'] = ec
                for h in HORIZONS:
                    row[f'ret{h}d'] = (b.close[e + h] - ec) / ec * 100 if (e + h < n and ec) else None
                recs.append(row)
            if not recs:
                return None, f'資料抓到了（{n}筆，最新到{b.date[-1]}），但沒有任何一天落在回測天數／流動性門檻範圍內'
            d = pd.DataFrame(recs)
            for c in d.columns:
                if c != 'evalDate':
                    d[c] = pd.to_numeric(d[c], errors='coerce').astype('float32')
            d.insert(1, 'stockId', sid)
            d.insert(2, 'name', names.get(sid, sid))
            if delay:
                time.sleep(delay)
            return d, None
        except Exception as ex:  # noqa
            return None, str(ex)

    frames, fail_reasons = [], {}
    saved = failed = done = 0
    from concurrent.futures import wait, FIRST_COMPLETED
    running = {}                      # sid -> 開始處理的時間（看門狗用）
    rlock = threading.Lock()

    def job(sid):
        with rlock:
            running[sid] = time.time()
        try:
            return one(sid)
        finally:
            with rlock:
                running.pop(sid, None)

    def fail(err):
        nonlocal failed
        failed += 1
        fail_reasons[err] = fail_reasons.get(err, 0) + 1

    # 看門狗：單檔處理超過 stock_timeout 秒就略過（不再等它）；整體超過 stall_timeout 秒沒有任何一檔完成就中止，
    # 並把卡住的代號回報出來——避免畫面停在某個進度卻看不出是慢還是卡死。
    t_start = last_progress = time.time()
    stopped_msg = None
    pool = ThreadPoolExecutor(max(1, workers))
    try:
        fut_sid = {pool.submit(job, sid): sid for sid in universe}
        pending = set(fut_sid)
        abandoned = set()
        while pending:
            done_set, pending = wait(pending, timeout=3, return_when=FIRST_COMPLETED)
            for fut in done_set:
                sid = fut_sid[fut]
                if sid in abandoned or fut.cancelled():
                    continue
                try:
                    d, err = fut.result()
                except Exception as ex:  # noqa
                    d, err = None, str(ex)
                done += 1
                last_progress = time.time()
                if d is not None:
                    saved += len(d)
                    frames.append(frame_arrays(d))
                    del d
                else:
                    fail(err)
                if on_progress:
                    on_progress(done, total, saved, failed, sid)
            now = time.time()
            with rlock:
                slow = [(sid_, now - t0) for sid_, t0 in running.items() if now - t0 > stock_timeout and sid_ not in abandoned]
            for sid_, _ in slow:      # 放棄等待這一檔（執行緒本身無法強制中止，但結果會被忽略）
                abandoned.add(sid_)
                done += 1
                fail(f'逾時（超過{stock_timeout}秒沒有回應，已略過）')
                pending = {f for f in pending if fut_sid[f] != sid_}
                last_progress = now
            if on_progress:
                with rlock:
                    busy = sorted([kv for kv in running.items() if kv[0] not in abandoned], key=lambda kv: kv[1])
                el = now - t_start
                eta = el / done * (total - done) if done else None
                note = (f'已用 {el / 60:.1f} 分' + (f'，預估剩 {eta / 60:.0f} 分' if eta is not None else '')
                        + (f'｜處理中 {len(busy)} 檔，最久 {busy[0][0]} {now - busy[0][1]:.0f}秒' if busy else ''))
                on_progress(done, total, saved, failed, None, note)
            if should_stop and should_stop():
                stopped_msg = '使用者中止'
            elif done >= 10 and saved == 0 and failed >= 10:
                stopped_msg = '前10檔全部失敗（多半是 API Key／額度／方案問題）'
            elif now - last_progress > stall_timeout:
                with rlock:
                    stuck = ', '.join(k for k in running if k not in abandoned)
                stopped_msg = f'超過{stall_timeout // 60}分鐘沒有任何一檔完成（卡住的代號：{stuck or "無"}），已中止並保留已完成的結果'
            if stopped_msg:
                for f in pending:
                    f.cancel()
                if '前10檔' not in stopped_msg:
                    fail_reasons[stopped_msg] = fail_reasons.get(stopped_msg, 0) + 1
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # 不等卡住的執行緒
        fut_sid = pending = None                         # 釋放 future 持有的結果，避免和 frames 重複佔記憶體
    if on_progress:
        on_progress(total, total, saved, failed, None, '')
    df = low_mem_concat(frames)   # 已依 evalDate、stockId 排序
    import gc
    gc.collect()
    if len(df):
        df['evalDate'] = df['evalDate'].astype(str)
    top = sorted(fail_reasons.items(), key=lambda kv: -kv[1])[:5]
    return df, dict(saved=saved, failed=failed, total=total, bm_err=bm_err, stopped=stopped_msg,
                    top_fail=[f'{m}（{c}次）' for m, c in top])


# ════════════════════════════════════════════════════════════════════
#  批次分析（單檔）
# ════════════════════════════════════════════════════════════════════
def analyze_stock(sid, name, rows, bm, vp_params, extras_data):
    b = Bars(rows)
    e = b.n - 1
    ex = {'sector_bm': extras_data.get('sectorBenchmark'), 'tech': extras_data.get('tech')}
    rv, py_ = extras_data.get('revRange'), extras_data.get('priceYoYRange')
    # 旗標用「最新1季」：營收YoY − 均價YoY（跟回測的近1季定義一致）
    if py_ and py_[-1]['yoy'] is not None:
        ex['priceYoy1q'] = py_[-1]['yoy']
        if rv and rv[-1]['yoy'] is not None:
            ex['divTotal'] = rv[-1]['yoy'] - py_[-1]['yoy']
    pc = PatternCache(b)
    row, info = build_flag_row(b, e, bm, pc, vp_params, ex)
    ext = {k: v for k, v in extras_data.items() if k != 'sectorBenchmark'}
    return dict(stockId=sid, name=name, bars=b, row=row, info=info, total=info['dm']['score'], **ext)


def div_3q_total(r):
    rv, py_ = r.get('revRange'), r.get('priceYoYRange')
    if not (rv and py_):
        return None
    pk = {(p['year'], p['quarter']): p for p in py_}
    vals = [x['yoy'] - pk[(x['year'], x['quarter'])]['yoy'] for x in rv
            if x['yoy'] is not None and (x['year'], x['quarter']) in pk and pk[(x['year'], x['quarter'])]['yoy'] is not None]
    return sum(vals) if vals else None


def matched_combos(row, combos):
    df = pd.DataFrame([row])
    flags = build_condition_flags(df)
    out = []
    for idx, combo in enumerate(combos):
        if all(c in flags and bool(flags[c].iloc[0]) for c in combo):
            out.append(idx)
    return out


def score_label(t):
    return '積極做多' if t >= 80 else '可考慮進場' if t >= 65 else '觀望' if t >= 50 else '不建議進場'


# ── 組合型態：依命中類別（S/#/M/W/F）的組合分級，依據三年回測（20日報酬，含 t(依日) 檢驗，2026-10-05 三年六段回測重新統計） ──
COMBO_STYLE_RULES = {'SWF': '進攻', 'S#W': '進攻', 'WF': '進攻', 'S': '進攻', 'SW': '穩健', 'W': '穩健', '#W': '穩健', 'F': '彩券', 'SF': '彩券', '#M': '彩券', 'S#MWF': '彩券', 'SMW': '彩券'}
COMBO_STYLE_ICON = {'進攻': '🚀進攻', '穩健': '🛡️穩健', '彩券': '🎲彩券'}
COMBO_STYLE_NOTE = {'進攻': '美股三年六段回測（2023-10～2026-09，約1660檔）：20日超額+0.4~3.8%、t(依日)2.9~6.6，SWF最佳（超額+2.6%、飆股率約7倍）；波動大（跌>10%約13~24%），宜分散',
                    '穩健': '美股三年六段回測：20日勝率55~67%、跌>10%約9~15%（全體12%）；SW最穩（六個半年都為正、t(依日)10.3），#W勝率最高（67%）',
                    '彩券': '美股三年六段回測：20日勝率43~51%、跌>10%約20~34%，飆股率高但期望值為負或接近0，宜避開或小部位'}


def combo_style(ms, mp, mm, mw, mf):
    key = ''.join(k for k, x in (('S', ms), ('#', mp), ('M', mm), ('W', mw), ('F', mf)) if x)
    return COMBO_STYLE_ICON.get(COMBO_STYLE_RULES.get(key, ''), '')


# ── 大盤狀態：先判斷大盤，再看命中的組合在「這種大盤」下回測是否有效 ──
MARKET_REGIMES = {1: ('①多頭', 'SPY 站上月線（MA20）與季線（MA60）'),
                  2: ('②多頭回檔', 'SPY 跌破月線、仍守住季線'),
                  3: ('③空頭反彈', 'SPY 站上月線、但仍在季線下'),
                  4: ('④空頭', 'SPY 跌破月線與季線')}
REGIME_ADVICE = {1: '追強勢最有效：S1/S2/S5、F飆股、CMF資金流、近20日強於大盤都有效；布林收窄類最差',
                 2: '最危險的狀態：動能股與飆股組合（F）幾乎全部轉負，只剩收斂型 S3（布林收窄）、S4（量縮）有效，宜降低部位',
                 3: '三年回測只有約20天，樣本太少，標籤僅供參考',
                 4: '買「中期強勢」與「短線超跌」最好，W擇時型在此時發揮作用；短線過熱、資金流等追高條件失效'}
REGIME_MIN_N = 50   # 該大盤狀態下回測樣本少於此數 → 不標示


def market_regime(row):
    """依 benchmarkAbove20／benchmarkAbove60 判斷大盤狀態 1～4；沒有大盤資料回傳 None"""
    a20, a60 = (row or {}).get('benchmarkAbove20'), (row or {}).get('benchmarkAbove60')
    if a20 is None or a60 is None:
        return None
    try:
        a20, a60 = int(a20) == 1, int(a60) == 1
    except Exception:  # noqa
        return None
    return 1 if (a20 and a60) else (2 if a60 else (3 if a20 else 4))


def regime_tag(combo, reg, K):
    """✅ 該大盤狀態下 t(依日)≥2 且超額為正／❌ 超額為負／⚠️ 其他；樣本不足回傳 ''"""
    if not reg:
        return ''
    row = (K.get('REGIME_STATS') or {}).get(combo_norm_key(combo))
    s = row[reg - 1] if row and len(row) >= reg else None
    if not s or s[0] < REGIME_MIN_N:
        return ''
    if s[3] is not None and s[3] >= 2 and s[2] > 0:
        return '✅'
    return '❌' if s[2] < 0 else '⚠️'


def regime_fit(groups, reg, K, show=4):
    """groups：[(代號前綴, 組合清單, 命中索引), ...] → ('✅2 S1 W3｜❌1 F12', ✅數, ❌數)"""
    if not reg:
        return '', 0, 0
    buckets = {'✅': [], '⚠️': [], '❌': []}
    for code, lst, idxs in groups:
        for i in idxs:
            t = regime_tag(lst[i], reg, K)
            if t:
                buckets[t].append(f'{code}{i + 1}')
    parts = []
    for t in ('✅', '❌', '⚠️'):
        v = buckets[t]
        if v:
            parts.append(f"{t}{len(v)} " + ' '.join(v[:show]) + (f' +{len(v) - show}' if len(v) > show else ''))
    return '｜'.join(parts), len(buckets['✅']), len(buckets['❌'])


def batch_market_regime(results):
    for r in results or []:
        reg = market_regime(r.get('row'))
        if reg:
            return reg
    return None


def regime_banner(reg):
    nm, desc = MARKET_REGIMES[reg]
    return f"🧭 今日大盤狀態：**{nm}**（{desc}）　→　{REGIME_ADVICE[reg]}。摘要表「大盤適用」欄：✅＝這種大盤下回測有效、❌＝回測為負、⚠️＝普通。"


def summary_frame(results, extras_flags, K, live=None):
    recs = []
    for r in results:
        b, info = r['bars'], r['info']
        e = b.n - 1
        last_c = b.close[e]
        prev_c = b.close[e - 1] if e >= 1 else last_c
        chg = (last_c - prev_c) / prev_c * 100 if prev_c else 0
        dm, sig, pb, pt = info['dm'], info['sig'], info['pb'], info['pt']
        bbw = (b.bbU[e] - b.bbL[e]) / last_c * 100 if (b.bbU[e] is not None and last_c) else None
        combo_tag = ''
        if sig['isBreakout']:
            combo_tag = '🚀 強勢突破盤'
        elif sig['isPullbackRebound']:
            combo_tag = '🎯 跌深反彈盤'
        kd = info['sig']['kdjState'] or ''
        vps, prof = info['vps'], info['prof']
        vp_txt = ''
        if prof:
            vp_txt = f"POC {prof['pocPrice']:.1f}（{prof['pocLow']:.1f}~{prof['pocHigh']:.1f}）"
        vp_sig = ''
        if vps['sigType']:
            vp_sig = ('🟢 ' if vps['signal'] == 'buy' else '🔴 ') + vps['detail'].split('：')[0]
        pnames = [('🔥' if x['justBroke'] else '') + x['name'] for x in pt['results']
                  if (x['breakout'] if pt['anyBreakout'] else x['formed'])]
        pt_icon = '🔥' if pt['anyJustBroke'] else ('✅' if pt['anyBreakout'] else ('🕒' if pt['anyFormed'] else '－'))
        mp = matched_combos(r['row'], K['PINNED_COMBOS'])
        mm = matched_combos(r['row'], K['MOONSHOT_COMBOS'])
        ms = matched_combos(r['row'], K['STOCK_PICK_COMBOS'])
        rec = {
            '股票': r['stockId'], '名稱': r['name'] + (' 🔴即時' if r.get('realtime') else ''),
            '多方力道': r['total'], '評等': score_label(r['total']),
            '+DI': dm['plusDI'], '-DI': dm['minusDI'], 'ADX': dm['adx'], 'ADXR': dm['adxr'],
            '布林位置%': sig['bbPos'], '布林寬度%': bbw,
            'MACD狀態': (sig['macdState'] or '') + (('　' + combo_tag) if combo_tag else ''),
            'KDJ狀態': kd, 'RS(vs SPY)%': info['rs'], '量比': info['vr'],
            '分價量表(POC)': vp_txt, '分價訊號': vp_sig,
            '成交量': b.volume[e], '漲跌幅%': chg, '收盤': last_c,
        }
        if extras_flags.get('pe'):
            p = r.get('peRange')
            rec['P/E'] = p['current'] if p else None
            rec['P/E區間'] = f"{p['min']:.1f}~{p['max']:.1f}" if p else ('⚠️ 讀取失敗' if r.get('peRangeErr') else '無資料')
        if extras_flags.get('rev'):
            rv = r.get('revRange')
            rec['營收YoY%(最新季)'] = rv[-1]['yoy'] if rv else None
            rec['營收YoY(近3季)'] = '　'.join(f"Q{m['quarter']} {fnum(m['yoy'])}%" for m in rv) if rv else '無資料'
            rec['營收QoQ%(最新季)'] = rv[-1]['qoq'] if rv else None
        if extras_flags.get('pxyoy'):
            py_ = r.get('priceYoYRange')
            rec['均價YoY%(最新季)'] = py_[-1]['yoy'] if py_ else None
        if extras_flags.get('rev') and extras_flags.get('pxyoy'):
            rec['YoY乖離度(近3季合計)pp'] = div_3q_total(r)
        if extras_flags.get('sector'):
            rec['類股'] = (r.get('sector') or '') + (f"({SECTOR_ETF_MAP.get(r.get('sector') or '', '')})" if r.get('sector') else '')
            rec['類股站上20/60MA'] = ('／'.join('站上' if v == 1 else ('跌破' if v == 0 else '--')
                                                for v in (r['row'].get('sectorAbove20'), r['row'].get('sectorAbove60'))))
        rec['回後買上漲'] = '✅ 符合進場' if pb['allPass'] else f"❌ {pb['requiredPassed']}/{pb['requiredTotal']}"
        rec['型態確認'] = pt_icon + ' ' + ('、'.join(pnames) if pnames else '無')
        def _t(c, kind):
            bt = best_t(combo_stats(c, kind, K, live))
            return f'(t{bt:.1f})' if bt is not None else ''
        rec['選股型命中'] = ' '.join(f'S{i + 1}' + _t(K['STOCK_PICK_COMBOS'][i], 'S') for i in ms)
        rec['指定組合命中'] = ' '.join([f'#{i + 1}' + ('⏱' if is_timing_combo(K['PINNED_COMBOS'][i]) else '')
                                    + _t(K['PINNED_COMBOS'][i], '#') for i in mp]
                                   + [f'M{i + 1}' + _t(K['MOONSHOT_COMBOS'][i], 'M') for i in mm])
        mw = matched_combos(r['row'], K.get('BT_WIN_COMBOS', []))
        mf = matched_combos(r['row'], K.get('BT_HOT_COMBOS', []))

        def _short(idxs, code, lst, kind, key):
            st_list = [(i, combo_stats(lst[i], kind, K, live)) for i in idxs]
            if kind == 'W':
                st_list.sort(key=lambda x: -(best_t(x[1]) or 0))
                txt = [f'{code}{i + 1}' + ('⏱' if is_timing_combo(lst[i]) else '') + (f'(t{best_t(s_):.1f})' if best_t(s_) is not None else '')
                       for i, s_ in st_list[:BT_HIT_SHOW]]
            else:
                st_list.sort(key=lambda x: -max([v for v in x[1]['moon'].values() if v is not None] or [0]))
                txt = [f'{code}{i + 1}(' + str(max([v for v in s_['moon'].values() if v is not None] or [0])) + '%)'
                       for i, s_ in st_list[:BT_HIT_SHOW]]
            more = len(idxs) - BT_HIT_SHOW
            return ' '.join(txt) + (f' +{more}' if more > 0 else '')
        rec['回測高勝率命中'] = _short(mw, 'W', K.get('BT_WIN_COMBOS', []), 'W', 'win')
        rec['回測飆股命中'] = _short(mf, 'F', K.get('BT_HOT_COMBOS', []), 'F', 'moon')
        rec['命中數'] = len(ms) + len(mp) + len(mm)
        # 命中類別數：S/#/M/W/F 五類中命中幾類（同類多個組合常共用同一訊號，類別數比總命中數更能反映訊號強弱）
        rec['命中類別數'] = sum(1 for x in (ms, mp, mm, mw, mf) if x)
        rec['組合型態'] = combo_style(ms, mp, mm, mw, mf)
        _reg = market_regime(r['row'])
        rec['大盤適用'], rec['_regOk'], rec['_regBad'] = regime_fit(
            [('S', K['STOCK_PICK_COMBOS'], ms), ('#', K['PINNED_COMBOS'], mp), ('M', K['MOONSHOT_COMBOS'], mm),
             ('W', K.get('BT_WIN_COMBOS', []), mw), ('F', K.get('BT_HOT_COMBOS', []), mf)], _reg, K)
        rec['財報日'] = earnings_label(r.get('earnings'))
        rec['_pbPass'] = pb['allPass']
        rec['_pt'] = 'justbreak' if pt['anyJustBroke'] else ('breakout' if pt['anyBreakout'] else ('forming' if pt['anyFormed'] else 'none'))
        recs.append(rec)
    df = pd.DataFrame(recs)
    if not len(df):
        return df
    # 欄位順序：股票、名稱之後依序放 股價、漲跌幅%、選股型命中、指定組合命中、型態確認；不顯示 評等、命中數、+DI/-DI/ADX/ADXR
    df = df.rename(columns={'收盤': '股價'}).drop(columns=['評等', '命中數', '+DI', '-DI', 'ADX', 'ADXR'], errors='ignore')
    front = ['股票', '名稱', '股價', '漲跌幅%', '命中類別數', '組合型態', '大盤適用', '選股型命中', '指定組合命中', '回測高勝率命中', '回測飆股命中', '型態確認', '財報日']
    return df[[c for c in front if c in df.columns] + [c for c in df.columns if c not in front]]


def _xl_col(i):
    """0 → A, 25 → Z, 26 → AA"""
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _auto_widths(df):
    out = []
    for c in df.columns:
        vals = [str(c)] + [str(v) for v in df[c].tolist()[:2000]]
        out.append(min(max(max(len(x) for x in vals) + 2, 8), 40))
    return out


def _xlsx_builtin(sheets, widths=None, wrap=False):
    """沒有 openpyxl／xlsxwriter 時的備援：用標準函式庫 zipfile 直接產生 .xlsx（Excel 可正常開啟）"""
    import zipfile
    from xml.sax.saxutils import escape

    sa = ' s="1"' if wrap else ''

    def cell(ref, v):
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            return ''
        if isinstance(v, (bool, np.bool_)):
            return f'<c r="{ref}" t="b"><v>{int(v)}</v></c>'
        if isinstance(v, (int, float, np.integer, np.floating)):
            return f'<c r="{ref}"{sa}><v>{v}</v></c>'
        txt = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', escape(str(v)))
        return f'<c r="{ref}" t="inlineStr"{sa}><is><t xml:space="preserve">{txt}</t></is></c>'

    bio = io.BytesIO()
    with zipfile.ZipFile(bio, 'w', zipfile.ZIP_DEFLATED) as z:
        names = [str(n)[:31] for n, _ in sheets]
        z.writestr('[Content_Types].xml',
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + ''.join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(len(sheets))) + '</Types>')
        z.writestr('_rels/.rels',
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr('xl/workbook.xml',
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + ''.join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
                   + '</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels',
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + ''.join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                             for i in range(len(sheets)))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   '</Relationships>')
        z.writestr('xl/styles.xml',
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
                   '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf></cellXfs>'
                   '</styleSheet>')
        for si, (_, df) in enumerate(sheets):
            ws = widths if widths else _auto_widths(df)
            cols = ''.join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(ws))
            rows = ['<row r="1">' + ''.join(cell(f'{_xl_col(j)}1', str(c)) for j, c in enumerate(df.columns)) + '</row>']
            for ri, rec in enumerate(df.itertuples(index=False), start=2):
                rows.append(f'<row r="{ri}">' + ''.join(cell(f'{_xl_col(j)}{ri}', v) for j, v in enumerate(rec)) + '</row>')
            z.writestr(f'xl/worksheets/sheet{si + 1}.xml',
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       + (f'<cols>{cols}</cols>' if cols else '') + '<sheetData>' + ''.join(rows) + '</sheetData></worksheet>')
    return bio.getvalue()


def sheets_to_xlsx(sheets, widths=None, wrap=False):
    """[(工作表名, DataFrame), ...] → .xlsx bytes。
    依序嘗試 openpyxl → xlsxwriter → 內建備援（標準函式庫），任何環境都不會因為少裝套件而整頁當掉。
    widths：各欄寬（None＝依內容自動）；wrap：內容自動換行、靠上對齊。"""
    bad = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')   # Excel 不接受的控制字元

    def clean(d):
        d = d if isinstance(d, pd.DataFrame) else pd.DataFrame(d)
        d = d.copy()
        for c in d.columns:
            if d[c].dtype == object:
                d[c] = d[c].map(lambda v: bad.sub('', v) if isinstance(v, str) else v)
        return d
    sheets = [(str(n)[:31], clean(d)) for n, d in sheets]
    for engine in ('openpyxl', 'xlsxwriter'):
        try:
            __import__(engine)
        except ImportError:
            continue
        try:
            return _xlsx_with_engine(sheets, engine, widths, wrap)
        except Exception:  # noqa  這個引擎失敗就換下一個，最後用內建備援
            continue
    return _xlsx_builtin(sheets, widths, wrap)


def _xlsx_with_engine(sheets, engine, widths, wrap):
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine=engine) as w:
        for name, df in sheets:
            df.to_excel(w, index=False, sheet_name=name)
            ws_ = widths if widths else _auto_widths(df)
            sh = w.sheets[name]
            if engine == 'openpyxl':
                from openpyxl.styles import Alignment
                for i, wd in enumerate(ws_):
                    sh.column_dimensions[_xl_col(i)].width = wd
                if wrap:
                    for row in sh.iter_rows(min_row=2):
                        for c in row:
                            c.alignment = Alignment(wrap_text=True, vertical='top')
            else:
                fmt = w.book.add_format({'text_wrap': True, 'valign': 'top'}) if wrap else None
                for i, wd in enumerate(ws_):
                    sh.set_column(i, i, wd, fmt)
    return bio.getvalue()


def df_to_excel_bytes(df, sheet):
    return sheets_to_xlsx([(sheet, df)])


# ════════════════════════════════════════════════════════════════════
#  AI 分析（OpenAI）
# ════════════════════════════════════════════════════════════════════
AI_SYSTEM = ('你是一位精通美股技術分析的資深操盤手，熟悉DMI趨向指標（+DI／-DI／ADX／ADXR）多方力道評分方法論，'
             '以及朱家泓《技術分析全攻略》的回後買上漲、頭肩底等進場型態判斷，並將此方法論套用於美國股市個股分析。請根據使用者提供的個股技術數據摘要，'
             '用繁體中文給出：1) 整體技術面研判（3-4句） 2) 進場時機與風險提示 3) 綜合建議（積極做多／可考慮／觀望／不建議）。'
             '語氣專業、精簡、避免空泛用詞，並提醒這僅為技術面參考，非投資建議。')


def build_ai_prompt(r):
    b, info = r['bars'], r['info']
    e = b.n - 1
    c, p = b.close[e], b.close[e - 1] if e >= 1 else b.close[e]
    chg = c - p
    chgp = chg / p * 100 if p else 0
    dm, pb, pt = info['dm'], info['pb'], info['pt']
    L = [f"股票：{r['stockId']} {r['name']}",
         f"資料日期：{b.date[e]}　收盤：{c}　漲跌：{chg:+.2f} ({chgp:+.2f}%)",
         f"成交量：{b.volume[e]:.0f}　量比(vs MA20量)：" + (f"{b.volume[e] / b.vm20[e]:.2f}" if b.vm20[e] else 'N/A') + 'x']
    L.append(f"MACD：DIF={b.macd[e]:.2f}　Signal={b.macdSig[e]:.2f}　柱狀={b.macdHist[e]:.2f}"
             + ('（偏多）' if b.macd[e] > b.macdSig[e] else '（偏空）'))
    if b.bbU[e] is not None:
        w = b.bbU[e] - b.bbL[e]
        L.append(f"布林通道：上軌={b.bbU[e]:.2f}　下軌={b.bbL[e]:.2f}　股價位置={((c - b.bbL[e]) / w * 100 if w else 50):.0f}%")
    prof, vps = info['prof'], info['vps']
    if prof:
        L.append(f"分價量表：POC={prof['pocPrice']:.2f}（區間 {prof['pocLow']:.2f}~{prof['pocHigh']:.2f}）"
                 + (f"　訊號：{vps['detail']}" if vps['detail'] else '　無訊號'))
    L += ['', f"【DMI多方力道評分】總分 {r['total']}/100",
          f"+DI={fnum(dm['plusDI'])}　-DI={fnum(dm['minusDI'])}　ADX={fnum(dm['adx'])}　ADXR={fnum(dm['adxr'])}",
          f"方向性 {dm['diPts']:.1f}/50、趨勢強度 {dm['adxPts']:.1f}/30、趨勢動能 {dm['adxrPts']:.1f}/20"]
    bull = [s[0] for s in dm['sigs'] if s[1] == 'bull']
    bear = [s[0] for s in dm['sigs'] if s[1] == 'bear']
    if bull:
        L.append('多頭訊號：' + '、'.join(bull))
    if bear:
        L.append('空頭訊號：' + '、'.join(bear))
    L += ['', f"【回後買上漲 8條件核對】必要條件通過 {pb['requiredPassed']}/{pb['requiredTotal']}" + ('（全數通過）' if pb['allPass'] else ''),
          '', '【型態確認，15種進場型態】']
    for x in pt['results']:
        stt = '🔥剛突破（較前一交易日新增）' if x['justBroke'] else ('✅已突破' if x['breakout'] else ('🕒成形中未突破' if x['formed'] else '－未偵測到'))
        L.append(f"・{x['name']}：{stt}" + (f"（{x['detail']}）" if x['detail'] else ''))
    return '\n'.join(L)


def run_ai(prompt, key, model):
    r = requests.post('https://api.openai.com/v1/chat/completions',
                      headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
                      json=dict(model=model, temperature=0.4, max_tokens=900,
                                messages=[{'role': 'system', 'content': AI_SYSTEM}, {'role': 'user', 'content': prompt}]),
                      timeout=90)
    j = r.json()
    if r.status_code != 200:
        raise RuntimeError((j.get('error') or {}).get('message') or f'HTTP {r.status_code}')
    return j['choices'][0]['message']['content']


# ════════════════════════════════════════════════════════════════════
#  圖表
# ════════════════════════════════════════════════════════════════════
LINE_STYLE = {
    'hs': ('#ffd54f', '頭肩底頸線'), 'chs': ('#ff8a65', '複式頭肩底頸線'), 'nb': ('#81c784', 'N字底壓力'),
    'tb': ('#4dd0e1', '三重底壓力'), 'rb': ('#64b5f6', '圓弧底壓力'), 'fb': ('#ba68c8', '一字底整理區間高點'),
    'abc': ('#ff2ecc', 'ABC下降切線起點'), 'channel': ('#ffa726', '上升軌道線'),
    'blackk': ('#e57373', '大量黑K高點'), 'kbp': ('rgba(255,255,255,.85)', 'K線橫盤首日高點'),
}
MARKER_STYLE = {'harami_bear': '#ff8a65', 'harami_bull': '#81c784', 'morning_star': '#4dd0e1', 'evening_star': '#ff5252'}


def draw_chart(r, vp_params):
    import plotly.graph_objects as go
    b0 = r['bars']
    fb, _ = b0.filtered()
    e = fb.n - 1
    x = fb.date
    pt = r['info']['pt']
    shapes, ann = [], []
    for p in pt['results']:
        if not p['formed']:
            continue
        if p['id'] in LINE_STYLE:
            col, lbl = LINE_STYLE[p['id']]
            for k, ln in enumerate([p.get('line'), p.get('line2')]):
                if not ln or ln[0] < 0 or ln[0] >= fb.n:
                    continue
                i1, p1, sl = ln
                shapes.append(dict(type='line', xref='x', yref='y', x0=x[i1], y0=p1, x1=x[e], y1=p1 + sl * (e - i1),
                                   line=dict(color=col, width=2 if k == 0 else 1.3, dash='solid' if p['breakout'] else 'dash')))
                if k == 0:
                    ann.append(dict(x=x[i1], y=p1, text=lbl, showarrow=True, arrowhead=2, arrowcolor=col,
                                    font=dict(color=col, size=10), ax=-10, ay=-30))
                    if p['breakout']:
                        ann.append(dict(x=x[e], y=fb.close[e], text='★ 突破' + p['name'], showarrow=True, arrowhead=2,
                                        arrowcolor=col, font=dict(color=col, size=11), ax=10, ay=-35))
        elif p['id'] in MARKER_STYLE and p.get('marker'):
            mi, mp_, dr, lb = p['marker']
            if 0 <= mi < fb.n:
                col = MARKER_STYLE[p['id']]
                ann.append(dict(x=x[mi], y=mp_, text=('★ ' if p['breakout'] else '') + lb, showarrow=True, arrowhead=2,
                                arrowcolor=col, font=dict(color=col, size=11), ax=0, ay=-32 if dr == 'up' else 32))
    prof = r['info']['prof']
    if prof:
        x0 = x[max(0, e + 1 - vp_params['lookback'])]
        shapes.append(dict(type='rect', xref='x', yref='y', x0=x0, x1=x[e], y0=prof['pocLow'], y1=prof['pocHigh'],
                           fillcolor='rgba(255,215,0,.10)', line=dict(width=0)))
        shapes.append(dict(type='line', xref='x', yref='y', x0=x0, x1=x[e], y0=prof['pocPrice'], y1=prof['pocPrice'],
                           line=dict(color='#ffd700', width=1.2, dash='dot')))
        ann.append(dict(x=x[e], y=prof['pocPrice'], text=f"POC {prof['pocPrice']:.1f}", showarrow=False,
                        xanchor='left', font=dict(color='#ffd700', size=10)))
    zz = fb.zigzag(e)
    vc = ['#ef5350' if fb.close[i] >= fb.open[i] else '#26a69a' for i in range(fb.n)]
    hc = ['#ef5350' if (fb.macdHist[i] or 0) >= 0 else '#26a69a' for i in range(fb.n)]
    fig = go.Figure([
        go.Candlestick(x=x, open=fb.open, high=fb.high, low=fb.low, close=fb.close, name='K線',
                       increasing=dict(line=dict(color='#ef5350'), fillcolor='#ef5350'),
                       decreasing=dict(line=dict(color='#26a69a'), fillcolor='#26a69a')),
        go.Scatter(x=x, y=fb.ma5, name='MA5', line=dict(color='#ffeb3b', width=1.2)),
        go.Scatter(x=x, y=fb.ma10, name='MA10', line=dict(color='#ff9800', width=1.2)),
        go.Scatter(x=x, y=fb.ma20, name='MA20', line=dict(color='#2196f3', width=1.2)),
        go.Scatter(x=x, y=fb.ma60, name='MA60', line=dict(color='#9c27b0', width=1.2)),
        go.Scatter(x=[x[p[0]] for p in zz], y=[p[1] for p in zz], name='轉折波', mode='lines+markers',
                   line=dict(color='#00e5ff', width=1.8), marker=dict(size=5, color='#00e5ff')),
        go.Scatter(x=x, y=fb.bbU, name='BB上軌', line=dict(color='rgba(100,200,255,.4)', width=1, dash='dot'), showlegend=False),
        go.Scatter(x=x, y=fb.bbL, name='BB下軌', line=dict(color='rgba(100,200,255,.4)', width=1, dash='dot'),
                   fill='tonexty', fillcolor='rgba(100,200,255,.04)', showlegend=False),
        go.Bar(x=x, y=fb.volume, marker_color=vc, name='成交量', opacity=.7, yaxis='y2'),
        go.Scatter(x=x, y=fb.vm20, name='量MA20', line=dict(color='#ff9800', width=1.5), yaxis='y2'),
        go.Bar(x=x, y=fb.macdHist, marker_color=hc, name='MACD柱', opacity=.8, yaxis='y3'),
        go.Scatter(x=x, y=fb.macd, name='MACD', line=dict(color='#2196f3', width=1.5), yaxis='y3'),
        go.Scatter(x=x, y=fb.macdSig, name='Signal', line=dict(color='#ff9800', width=1.5), yaxis='y3'),
    ])
    fig.update_layout(
        title=dict(text=f"{r['stockId']} {r['name']} 技術分析圖", font=dict(size=14)),
        template='plotly_dark', paper_bgcolor='rgba(10,14,26,1)', plot_bgcolor='rgba(15,20,35,1)',
        height=680, margin=dict(l=50, r=130, t=45, b=25), shapes=shapes, annotations=ann,
        xaxis=dict(rangeslider=dict(visible=False), gridcolor='rgba(255,255,255,.04)', type='category',
                   nticks=12, anchor='free', position=0),
        yaxis=dict(domain=[0.42, 1], gridcolor='rgba(255,255,255,.04)'),
        yaxis2=dict(domain=[0.22, 0.40], gridcolor='rgba(255,255,255,.04)'),
        yaxis3=dict(domain=[0, 0.20], gridcolor='rgba(255,255,255,.04)'),
        legend=dict(orientation='v', x=1.01, y=1, xanchor='left', bgcolor='rgba(0,0,0,0)', font=dict(size=10)),
    )
    return fig


# ════════════════════════════════════════════════════════════════════
#  批次分析主流程（Streamlit 與每日排程共用）
# ════════════════════════════════════════════════════════════════════
def batch_analyze(stocks, token, days, use_rt, ex_flags, vpp, delay=0.3, log=None, progress=None, names=None):
    log = log or (lambda m: None)
    try:
        bm = fetch_benchmark(token, max(days, LONG_HISTORY_DAYS))   # 中期動能（12-1月）需要約一年的大盤資料
    except Exception as ex:  # noqa
        bm = None
        log(f'⚠️ 抓不到大盤(SPY)資料，相對強弱／大盤濾網這次不會出現：{ex}')
    if ex_flags.get('sector'):
        reset_sector_cache()
    try:
        earn_cal = fetch_earnings_calendar(token)
    except Exception as ex:  # noqa
        earn_cal = None
        log(f'⚠️ 財報行事曆讀取失敗，改為逐檔查詢財報日：{ex}')
    results = []
    for i, sid in enumerate(stocks):
        try:
            rows_full = fetch_price(sid, token, max(days, LONG_HISTORY_DAYS))
            rt = False
            if use_rt:
                rows_full, rt = merge_realtime_quote(rows_full, fetch_quote(sid, token))
            rows = trim_rows(rows_full, days)
            bl = Bars(rows_full)
            tech = tech_extras(bl, bl.n - 1)
            tech.update(mid_momentum(bl, bl.n - 1, bm))
            try:   # 財報歷史：算財報驚喜／財報跳空，也順便取得下一次財報日
                ehist = fetch_earnings_hist(sid, token)
            except Exception:  # noqa
                ehist = []
            tech.update(earnings_feats(bl, bl.n - 1, ehist))
            extras = dict(realtime=rt, peRange=None, peRangeErr=None, revRange=None, priceYoYRange=None,
                          sector=None, sectorBenchmark=None, tech=tech)
            extras['earnings'] = (earn_cal or {}).get(to_fmp_symbol(sid).upper()) or next_earnings_from_hist(ehist)
            if ex_flags.get('pe'):
                try:
                    extras['peRange'] = fetch_pe_range(sid, token)
                except Exception as ex2:  # noqa
                    extras['peRangeErr'] = str(ex2)
            if ex_flags.get('rev'):
                try:
                    extras['revRange'] = fetch_revenue_yoy_qoq(sid, token)
                except Exception:  # noqa
                    pass
            if ex_flags.get('pxyoy'):
                try:
                    qs = [dict(year=q['year'], quarter=q['quarter']) for q in extras['revRange']] if extras['revRange'] else None
                    extras['priceYoYRange'] = fetch_quarterly_avg_price_yoy(sid, token, qs)
                except Exception:  # noqa
                    pass
            if ex_flags.get('sector'):
                try:
                    extras['sector'] = fetch_profile(sid, token).get('sector')
                    extras['sectorBenchmark'] = get_sector_benchmark(token, days, extras['sector'])
                except Exception:  # noqa
                    pass
            r = analyze_stock(sid, fetch_name(sid, token, names), rows, bm, vpp, extras)
            results.append(r)
            log(f"✅ {sid} {r['name']}  得分:{r['total']}" + ('　🔴即時' if rt else ''))
        except Exception as ex:  # noqa
            log(f'❌ {sid} 失敗：{ex}')
        if progress:
            progress(i + 1, len(stocks))
        if i < len(stocks) - 1 and delay:
            time.sleep(delay)
    results.sort(key=lambda r: -r['total'])
    return results


# ════════════════════════════════════════════════════════════════════
#  命中組合：匯出與每日追蹤
#  追蹤紀錄存在程式同資料夾的 combo_hits_log_us.csv（每次批次分析自動累加，
#  同一天同一檔只記一次），之後用「更新追蹤報酬」抓最新股價，計算命中後的
#  實際表現，並依組合彙總「實盤」勝率，跟回測記錄的勝率對照。
# ════════════════════════════════════════════════════════════════════
HIT_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'combo_hits_log_us.csv')
HIT_COLS = ['日期', '股票代號', '股票名', '股價', '漲跌%', '命中組合', '勝率', '飆股比例', 't值', '命中編號']


# ── 每個組合的勝率／飆股比例／t值(同日調整) ──
# 優先用「這次程式裡跑過的回測」即時計算；沒跑回測時，退回 2026-09-27 美股回測檔案的記錄值
# （COMBO_REF_STATS，只有當時進榜的組合才有），再退回各組合原本的歷史記錄。
def combo_norm_key(combo):
    return ' ＋ '.join(sorted(combo))


def compute_live_combo_stats(df, combos, moon_threshold=30.0, moon_df=None):
    """用回測原始紀錄算每個組合 5/10/20日 的勝率、t值(同日調整)、飆股比例，回傳 {正規化key: stats}"""
    out = {}
    if df is None or not len(df):
        return out
    flags = build_condition_flags(df)
    uniq = {combo_norm_key(c): c for c in combos}
    for h in HORIZONS:
        col = f'ret{h}d'
        if col not in df.columns:
            continue
        valid = df[col].notna().values
        ret = df[col].values.astype(float)
        ret_raw = moon_df[col].values.astype(float) if moon_df is not None else ret
        dm = df.groupby('evalDate')[col].transform('mean').values.astype(float) if 'evalDate' in df.columns else np.nanmean(ret)
        exr = ret - dm
        for k, c in uniq.items():
            if any(x not in flags for x in c):
                continue
            m = valid & np.logical_and.reduce([flags[x].values for x in c])
            n = int(m.sum())
            if n < 5:
                continue
            r, ex, rr = ret[m], exr[m], ret_raw[m]
            sd = ex.std(ddof=1) if n > 1 else 0
            d = out.setdefault(k, {'win': {}, 't': {}, 'moon': {}, 'n': {}, 'src': '本次回測'})
            d['n'][str(h)] = n
            d['win'][str(h)] = round((r > 0).mean() * 100, 1)
            d['t'][str(h)] = round(ex.mean() / (sd / math.sqrt(n)), 2) if sd > 0 else None
            d['moon'][str(h)] = round((rr > moon_threshold).mean() * 100, 1)
    return out


def combo_stats(combo, kind, K, live=None):
    """kind: 'S' / '#' / 'M'；回傳 {'win':{h:v}, 't':{h:v}, 'moon':{h:v}, 'src':...}"""
    k = combo_norm_key(combo)
    if live and k in live:
        return live[k]
    ref = K.get('COMBO_REF_STATS', {}).get(k, {})
    st_ = {'win': dict(ref.get('win', {})), 't': dict(ref.get('t', {})), 'moon': dict(ref.get('moon', {})),
           'src': '回測記錄值' if ref else ''}
    key = ' ＋ '.join(combo)
    if kind == 'S':
        for h, e in (K['STOCK_PICK_STATS'].get(key) or {}).items():
            st_['win'].setdefault(h, e['win'])
            st_['t'].setdefault(h, e['t'])
        st_['src'] = st_['src'] or '選股型記錄'
    elif kind == '#' and not st_['win']:
        for h, v in (K['PINNED_COMBO_WINRATES'].get(key) or {}).items():
            st_['win'][h] = v
        st_['src'] = st_['src'] or '舊版記錄'
    elif kind == 'M' and not st_['moon']:
        for h, e in (K['MOONSHOT_COMBO_STATS'].get(key) or {}).items():
            st_['moon'][h] = e['pct']
        st_['src'] = st_['src'] or '舊版記錄'
    elif kind == 'W':
        for h, e in (K.get('BT_WIN_STATS', {}).get(key) or {}).items():
            st_['win'].setdefault(h, e['win'])
            st_['t'].setdefault(h, e['t'])
        st_['src'] = st_['src'] or '三年回測'
    elif kind == 'F':
        for h, e in (K.get('BT_HOT_STATS', {}).get(key) or {}).items():
            st_['moon'].setdefault(h, e['pct'])
        st_['src'] = st_['src'] or '三年回測'
    return st_


def fmt_hz(d, pct=True):
    if not d:
        return '—'
    parts = [f"{h}日{v}{'%' if pct else ''}" for h, v in sorted(d.items(), key=lambda kv: int(kv[0])) if v is not None]
    return '/'.join(parts) if parts else '—'


def best_t(st_):
    vals = [v for v in (st_.get('t') or {}).values() if v is not None]
    return max(vals) if vals else None


def hit_records(results, K, live=None):
    """批次分析結果中有命中指定組合的股票（欄位：日期/代號/名稱/股價/漲跌%/命中組合/勝率/飆股比例/t值）
    「勝率／飆股比例／t值」每一行對應「命中組合」的同一行。"""
    out = []
    for r in results:
        mp = matched_combos(r['row'], K['PINNED_COMBOS'])
        mm = matched_combos(r['row'], K['MOONSHOT_COMBOS'])
        ms = matched_combos(r['row'], K['STOCK_PICK_COMBOS'])
        mw = matched_combos(r['row'], K.get('BT_WIN_COMBOS', []))
        mf = matched_combos(r['row'], K.get('BT_HOT_COMBOS', []))
        if not mp and not mm and not ms and not mw and not mf:
            continue
        b = r['bars']
        e = b.n - 1
        if not b.close[e] or b.close[e] <= 0:   # 0價（無成交／暫停交易）不列入命中
            continue
        pc = b.close[e - 1] if e >= 1 else b.close[e]
        lines, codes, wins, moons, ts = [], [], [], [], []

        def add(code, tag, c, kind):
            st_ = combo_stats(c, kind, K, live)
            bt = best_t(st_)
            lines.append(f"{code}{tag} " + '＋'.join(c) + (f'（t={bt}）' if bt is not None else ''))
            codes.append(code)
            wins.append(fmt_hz(st_['win']))
            moons.append(fmt_hz(st_['moon']))
            ts.append(fmt_hz(st_['t'], pct=False))
        for i in ms:
            add(f'S{i + 1}', '[選股型]', K['STOCK_PICK_COMBOS'][i], 'S')
        for i in mp:
            c = K['PINNED_COMBOS'][i]
            add(f'#{i + 1}', '[高勝率·擇時型]' if is_timing_combo(c) else '[高勝率]', c, '#')
        for i in mm:
            add(f'M{i + 1}', '[高標股]', K['MOONSHOT_COMBOS'][i], 'M')
        # 回測自動入選：每類只取最強的5組（避免一檔命中幾十組時紀錄過長）
        for i in sorted(mw, key=lambda i: -(best_t(combo_stats(K['BT_WIN_COMBOS'][i], 'W', K, live)) or 0))[:5]:
            c = K['BT_WIN_COMBOS'][i]
            add(f'W{i + 1}', '[回測高勝率·擇時型]' if is_timing_combo(c) else '[回測高勝率]', c, 'W')
        for i in sorted(mf, key=lambda i: -max(list(combo_stats(K['BT_HOT_COMBOS'][i], 'F', K, live)['moon'].values()) or [0]))[:5]:
            add(f'F{i + 1}', '[回測飆股]', K['BT_HOT_COMBOS'][i], 'F')
        out.append({'日期': b.date[e], '股票代號': r['stockId'], '股票名': r['name'], '股價': round(b.close[e], 2),
                    '漲跌%': round((b.close[e] - pc) / pc * 100, 2) if pc else None,
                    '命中組合': '\n'.join(lines), '勝率': '\n'.join(wins), '飆股比例': '\n'.join(moons),
                    't值': '\n'.join(ts), '命中編號': ' '.join(codes)})
    return out


def hits_excel_bytes(recs, sheet='命中組合股票'):
    df = pd.DataFrame(recs, columns=HIT_COLS)
    return sheets_to_xlsx([(sheet, df)], widths=[12, 10, 12, 10, 9, 80, 26, 26, 26, 22], wrap=True)


def load_hit_log():
    try:
        df = pd.read_csv(HIT_LOG_FILE, dtype={'股票代號': str, '日期': str})
        # 股價≤0 的紀錄是資料源當天回傳0價（無成交／暫停交易）造成的假命中，直接排除
        px_ = pd.to_numeric(df['股價'], errors='coerce')
        return df[~(px_ <= 0)].reset_index(drop=True)
    except Exception:  # noqa
        return pd.DataFrame(columns=HIT_COLS)


def append_hit_log(recs):
    """把命中紀錄累加到追蹤檔（同一天同一檔只保留一筆，以最新的為準），回傳新增筆數"""
    if not recs:
        return 0
    old = load_hit_log()
    new = pd.DataFrame(recs, columns=HIT_COLS)
    before = set(zip(old['日期'], old['股票代號'])) if len(old) else set()
    df = pd.concat([old, new], ignore_index=True).drop_duplicates(['日期', '股票代號'], keep='last')
    df = df.sort_values(['日期', '股票代號']).reset_index(drop=True)
    df.to_csv(HIT_LOG_FILE, index=False, encoding='utf-8-sig')
    return sum(1 for r in recs if (r['日期'], r['股票代號']) not in before)


def track_performance(log_df, token, delay=0.3, progress=None):
    """抓每檔追蹤股票從命中日到今天的股價，算命中後的表現"""
    if not len(log_df):
        return pd.DataFrame()
    first = min(log_df['日期'])
    days = (dt.date.today() - dt.date.fromisoformat(first)).days + 10
    sids = sorted(set(log_df['股票代號']))
    px = {}
    for i, sid in enumerate(sids):
        try:
            px[sid] = fetch_price(sid, token, days)
        except Exception:  # noqa
            px[sid] = []
        if progress:
            progress(i + 1, len(sids))
        if delay and i < len(sids) - 1:
            time.sleep(delay)
    out = []
    for r in log_df.to_dict('records'):
        rows = px.get(r['股票代號']) or []
        dates = [x['date'] for x in rows]
        rec = {k: r.get(k) for k in ('日期', '股票代號', '股票名', '股價', '命中編號', '命中組合')}
        import bisect
        k = bisect.bisect_right(dates, str(r['日期'])) - 1   # 命中日（或之前最近的交易日）
        if k < 0:
            rec.update({'持有天數': None, '最新價': None, '目前報酬%': None})
            out.append(rec)
            continue
        ent = rows[k]['close']
        after = rows[k + 1:]
        rec['持有天數'] = len(after)
        rec['最新價'] = rows[-1]['close']
        rec['目前報酬%'] = round((rows[-1]['close'] - ent) / ent * 100, 2)
        for h in (5, 10, 20):
            rec[f'{h}日報酬%'] = round((after[h - 1]['close'] - ent) / ent * 100, 2) if len(after) >= h else None
        rec['最大漲幅%'] = round((max(x['high'] for x in after) - ent) / ent * 100, 2) if after else None
        rec['最大回檔%'] = round((min(x['low'] for x in after) - ent) / ent * 100, 2) if after else None
        out.append(rec)
    return pd.DataFrame(out)


def combo_track_summary(perf, K):
    """依組合彙總命中後的實際表現，並附上回測記錄值對照"""
    if perf is None or not len(perf):
        return pd.DataFrame()
    rows = []
    ex = perf.assign(編號=perf['命中編號'].fillna('').str.split()).explode('編號')
    ex = ex[ex['編號'].astype(str).str.len() > 0].copy()

    def cur_combo(code):
        idx = int(code[1:]) - 1
        lst = (K['STOCK_PICK_COMBOS'] if code.startswith('S') else K['PINNED_COMBOS'] if code.startswith('#')
               else K.get('BT_WIN_COMBOS', []) if code.startswith('W') else K.get('BT_HOT_COMBOS', []) if code.startswith('F')
               else K['MOONSHOT_COMBOS'])
        return lst[idx] if 0 <= idx < len(lst) else None

    def logged_combo(code, txt):
        """從追蹤紀錄當時寫下的「命中組合」文字取出這個編號的組合（清單改版後舊紀錄仍對得上）"""
        for line in str(txt or '').split('\n'):
            m = re.match(r'^' + re.escape(code) + r'\[[^\]]*\]\s*(.+?)(（t=.*）)?$', line.strip())
            if m:
                return m.group(1).strip()
        return None
    ex['_combo'] = [logged_combo(c, t) or ('＋'.join(cur_combo(c)) if cur_combo(c) else '') for c, t in
                    zip(ex['編號'], ex['命中組合'] if '命中組合' in ex.columns else [None] * len(ex))]
    for (code, ctext), g in ex.groupby(['編號', '_combo']):
        combo = cur_combo(code)
        if combo is None or '＋'.join(combo) != ctext:   # 清單已改版：顯示當時的組合，不套用現在的回測記錄
            row_code, combo_txt, rec_txt = code + '(舊版)', ctext, '（舊版清單組合）'
        else:
            row_code, combo_txt = code, ctext
            if code.startswith('S'):
                rec_txt = format_stockpick_stats(K['STOCK_PICK_STATS'].get(' ＋ '.join(combo)))
            elif code.startswith('#'):
                rec_txt = ('[擇時型] ' if is_timing_combo(combo) else '') + format_winrates(K['PINNED_COMBO_WINRATES'].get(' ＋ '.join(combo)))
            elif code.startswith('W'):
                rec_txt = '　'.join(f"{h}日 勝率{e['win']}% t={e['t']}" for h, e in sorted(K['BT_WIN_STATS'].get(' ＋ '.join(combo), {}).items(), key=lambda kv: int(kv[0])))
            elif code.startswith('F'):
                rec_txt = format_moonshot_stats(K['BT_HOT_STATS'].get(' ＋ '.join(combo)))
            else:
                rec_txt = format_moonshot_stats(K['MOONSHOT_COMBO_STATS'].get(' ＋ '.join(combo)))
        row = {'編號': row_code, '條件組合': combo_txt, '命中次數': len(g),
               '目前平均報酬%': round(g['目前報酬%'].mean(), 2) if g['目前報酬%'].notna().any() else None}
        for h in (5, 10, 20):
            v = g[f'{h}日報酬%'].dropna() if f'{h}日報酬%' in g else pd.Series(dtype=float)
            row[f'{h}日已滿筆數'] = len(v)
            row[f'{h}日平均%'] = round(v.mean(), 2) if len(v) else None
            row[f'{h}日勝率%'] = round((v > 0).mean() * 100, 1) if len(v) else None
        row['回測記錄'] = rec_txt
        rows.append(row)
    df = pd.DataFrame(rows)
    return df.sort_values(['命中次數'], ascending=False).reset_index(drop=True)


# ── 每日排程（命令列模式）──
DAILY_USAGE = """每日追蹤（命令列）：
  python stock_analyzer_us_poc.py daily [--list sp500|nasdaq100|sox|top100|我的清單] \\
                                        [--token XXX] [--days 180] [--no-extras] [--out 資料夾]
  token 也可用環境變數 FMP_API_KEY。流程：抓清單 → 批次分析 → 命中組合累加到 combo_hits_log_us.csv
  → 更新所有追蹤股票的報酬 → 輸出「美股命中組合_日期.xlsx」（今日命中／追蹤明細／組合彙總）。
  top100＝全市場約2000檔逐檔查報價後取漲幅前100＋成交量前100（限流280次/分，約需7～10分鐘）。
  建議排程在美股收盤後（台灣時間早上 6:00 之後）執行。"""


def run_daily(argv):
    import argparse
    ap = argparse.ArgumentParser(description=DAILY_USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--list', default='sp500')
    ap.add_argument('--token', default=os.environ.get('FMP_API_KEY', ''))
    ap.add_argument('--days', type=int, default=180)
    ap.add_argument('--no-extras', action='store_true')
    ap.add_argument('--delay', type=float, default=0.0)
    ap.add_argument('--rate', type=int, default=280, help='每分鐘API上限')
    ap.add_argument('--out', default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args(argv)
    if not a.token:
        print('❌ 請用 --token 或環境變數 FMP_API_KEY 提供 FMP API Key')
        return 1
    set_rate_limit(a.rate)
    K = CONSTANTS()
    if a.list == 'top100':
        res, n_uni = fetch_market_snapshot(a.token, progress=lambda i, n: print(f'  報價掃描 {i}/{n}', end='\r'))
        gain = [r['id'] for r in sorted([r for r in res if r['pct'] is not None], key=lambda r: -r['pct'])[:100]]
        vol = [r['id'] for r in sorted([r for r in res if r['volume'] is not None], key=lambda r: -r['volume'])[:100]]
        stocks = list(dict.fromkeys(gain + vol))
        print(f'\n✅ 掃描 {n_uni} 檔，漲幅前100＋成交量前100，合併去重 {len(stocks)} 檔')
    else:
        src = {'sp500': K['SP500_LIST'], 'nasdaq100': K['NASDAQ100_LIST'], 'sox': K['SOX_LIST']}.get(a.list)
        if src is None:
            src = load_custom_lists(K).get(a.list, [])
        stocks = list(src)
    ex = dict(pe=False, rev=not a.no_extras, pxyoy=not a.no_extras, sector=not a.no_extras)
    results = batch_analyze(stocks, a.token, a.days, False, ex, VP_DEFAULTS, a.delay, log=print, names=K['SP500_NAMES'])
    _reg = batch_market_regime(results)
    if _reg:
        print(regime_banner(_reg).replace('**', ''))
    recs = hit_records(results, K)
    added = append_hit_log(recs)
    print(f'📌 今日命中 {len(recs)} 檔，新增 {added} 筆追蹤紀錄 → {HIT_LOG_FILE}')
    perf = track_performance(load_hit_log(), a.token, delay=0.0)
    summ = combo_track_summary(perf, K)
    fn = os.path.join(a.out, f'美股命中組合_{dt.date.today()}.xlsx')
    with open(fn, 'wb') as f_:
        f_.write(sheets_to_xlsx([('今日命中', pd.DataFrame(recs, columns=HIT_COLS)), ('追蹤明細', perf), ('組合彙總', summ)]))
    print(f'📥 已輸出 {fn}')
    return 0


# ════════════════════════════════════════════════════════════════════
#  Streamlit 介面
# ════════════════════════════════════════════════════════════════════
LISTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stock_lists_us.json')


def load_custom_lists(K):
    try:
        with open(LISTS_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except Exception:  # noqa
        d = {}
    d.setdefault('我的清單', K['MY_LIST_DEFAULT'])
    for k in ('我的清單1', '我的清單2', '我的清單3'):
        d.setdefault(k, [])
    return d


def save_custom_lists(d):
    try:
        with open(LISTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        return True
    except Exception:  # noqa
        return False


def parse_stocks(txt):
    import re
    return [s.strip().upper() for s in re.split(r'[\n,，、\s]+', txt or '') if s.strip()]


CSS = """
<style>
.block-container{padding-top:1.4rem}
.tagb{display:inline-block;padding:2px 9px;margin:2px 4px 2px 0;border-radius:10px;font-size:12px}
.bull{color:#00c864;border:1px solid #00c86466}
.bear{color:#ff5555;border:1px solid #ff555566}
.neu{color:#aaa;border:1px solid #aaaaaa55}
.verdict{border-radius:10px;padding:10px 16px;font-weight:700;margin-bottom:10px}
.small{color:#888;font-size:12px}
</style>
"""


def main():
    import streamlit as st
    st.set_page_config(page_title='US技術分析全攻略 · 美股評分系統', page_icon='📊', layout='wide')
    st.markdown(CSS, unsafe_allow_html=True)
    K = CONSTANTS()
    ss = st.session_state
    ss.setdefault('lists', load_custom_lists(K))
    ss.setdefault('stocks_text', '\n'.join(K['SP500_LIST']))
    ss.setdefault('batch', None)
    ss.setdefault('bt_df', None)
    ss.setdefault('ai', {})
    ss.setdefault('msg', '')

    def list_choice_changed():
        k = ss['list_choice']
        src = {'S&P 500': K['SP500_LIST'], 'Nasdaq-100': K['NASDAQ100_LIST'], 'SOX半導體': K['SOX_LIST']}.get(k)
        if src is None:
            src = ss['lists'].get(k, [])
        ss['stocks_text'] = '\n'.join(src)

    # ── 漲幅／成交量前100（全市場約2000檔逐檔查報價）：在畫出輸入框前先把清單換掉 ──
    pending = ss.pop('pending_top100', None)
    if pending:
        tok = ss.get('token', '').strip()
        if not tok:
            ss['msg'] = '請先輸入 FMP API Key'
        else:
            try:
                set_rate_limit(ss.get('rate_limit', 280))
                bar = st.progress(0.0, text='📡 抓取全市場股票池中…')
                res, n_uni = fetch_market_snapshot(
                    tok, progress=lambda i, n: bar.progress(i / n, text=f'📡 全市場報價掃描中：{i}/{n}（限流 {RATE_LIMIT_PER_MIN} 次/分鐘）'))
                bar.empty()
                key = 'pct' if pending == 'gain' else 'volume'
                top = sorted([r for r in res if r[key] is not None], key=lambda r: -r[key])[:100]
                if not top:
                    raise RuntimeError('取得報價但解析不出漲跌幅／成交量欄位')
                ss['stocks_text'] = '\n'.join(r['id'] for r in top)
                t0 = top[0]
                ss['msg'] = (f"✅ 已掃描全市場 {n_uni} 檔，取得今日{'漲幅' if pending == 'gain' else '成交量'}前{len(top)}"
                             f"（最高：{t0['id']} {t0['name']} "
                             + (f"+{t0['pct']:.2f}%）" if pending == 'gain' else f"量={t0['volume']:,.0f} 股）"))
                ss['auto_batch'] = True
            except Exception as ex:  # noqa
                ss['msg'] = f'❌ {ex}'

    # ────────────────────────────── 側邊欄 ──────────────────────────────
    with st.sidebar:
        st.markdown('### 📊 US技術分析全攻略')
        st.caption('朱家泓方法論 · 美股評分系統（POC 分價量表版）')
        st.text_input('Financial Modeling Prep API Key', type='password', key='token')
        st.caption('還沒有 Key？到 financialmodelingprep.com 註冊')
        lst_names = ['S&P 500', 'Nasdaq-100', 'SOX半導體', '我的清單', '我的清單1', '我的清單2', '我的清單3']
        st.selectbox('載入股票清單', lst_names, key='list_choice', on_change=list_choice_changed,
                     format_func=lambda k: k + (f"({len(ss['lists'].get(k, []))})" if k.startswith('我的') else ''))
        st.text_area('批次股票代號（每行一個或逗號分隔）', key='stocks_text', height=140)
        c1, c2 = st.columns(2)
        save_to = c1.selectbox('存到', ['我的清單', '我的清單1', '我的清單2', '我的清單3'], label_visibility='collapsed', key='save_to')
        if c2.button('💾 儲存清單', use_container_width=True):
            stocks = parse_stocks(ss['stocks_text'])
            if stocks:
                ss['lists'][save_to] = stocks
                save_custom_lists(ss['lists'])
                ss['msg'] = f'✅ 已存入「{save_to}」（{len(stocks)}檔）'
        days = st.slider('分析天數', 90, 365, 180, 30, key='days')
        use_rt = st.checkbox('🔴 加入盤中即時股價（FMP /quote，每檔多1次API）', value=True, key='use_rt')
        st.caption('以下每勾一項，每檔股票多打1次API：')
        ex_flags = dict(pe=st.checkbox('📐 近3年P/E區間', key='x_pe'),
                        rev=st.checkbox('📈 近3季營收YoY/QoQ', key='x_rev'),
                        pxyoy=st.checkbox('💹 近3季均價YoY', key='x_pxyoy'),
                        sector=st.checkbox('🏭 類股狀態濾網（所屬類股ETF站上/跌破20、60日均線）', key='x_sector'))
        st.number_input('API 每分鐘上限（Starter 300，保留安全邊界）', 30, 3000, 280, 10, key='rate_limit')
        req_delay = st.number_input('每檔間隔秒數（已有限流器，通常0即可）', 0.0, 5.0, 0.0, 0.1, key='req_delay')
        st.checkbox('📌 批次分析後自動把命中組合股票加入追蹤', value=True, key='auto_track')
        run_batch = st.button('🔍 批次分析', type='primary', use_container_width=True, key='run_batch')
        cg, cv = st.columns(2)
        if cg.button('🔥 漲幅前100', use_container_width=True, help='全市場約2000檔逐檔查報價，約需7～10分鐘'):
            ss['pending_top100'] = 'gain'
            st.rerun()
        if cv.button('📊 成交量前100', use_container_width=True, help='全市場約2000檔逐檔查報價，約需7～10分鐘'):
            ss['pending_top100'] = 'vol'
            st.rerun()
        if ss['msg']:
            st.caption(ss['msg'])

        with st.expander('📐 分價量表(POC)參數', expanded=False):
            vpp = dict(
                lookback=st.number_input('回看天數', 40, 250, VP_DEFAULTS['lookback'], 10, key='vp_lb'),
                bins=st.number_input('價格區間數', 10, 60, VP_DEFAULTS['bins'], 2, key='vp_bins'),
                shadow_mult=st.number_input('長影線：影線 ≥ 實體 × ', 0.3, 3.0, VP_DEFAULTS['shadow_mult'], 0.1, key='vp_sh'),
                vol_mult=st.number_input('帶量：成交量 > 20日均量 × ', 1.0, 3.0, VP_DEFAULTS['vol_mult'], 0.1, key='vp_vol'),
                body_mult=st.number_input('長K：實體 > 20日平均實體 × ', 1.0, 3.0, VP_DEFAULTS['body_mult'], 0.1, key='vp_body'),
                touch_tol=st.number_input('觸及容忍（區間寬度倍數）', 0.0, 2.0, VP_DEFAULTS['touch_tol'], 0.1, key='vp_tol'),
            )
            st.caption('⚠️ 美股回測中分價量表訊號單獨都沒有超額報酬，僅供參考，不當組合搜尋條件。')

        st.divider()
        st.markdown('**🔬 歷史回測分析（美股，預設全美股約2000檔）**')
        bt_univ = st.selectbox('回測股票池', ['全美股（約2000檔）', 'S&P 500', 'Nasdaq-100', 'SOX半導體', '目前輸入框清單'],
                               key='bt_univ', help='全美股＝FMP company-screener：NYSE＋NASDAQ 可交易個股（排除ETF／基金，股價≥10美元、成交金額≥1000萬美元，依成交金額取前2000檔）')
        bt_workers = st.number_input('回測平行抓取數', 1, 32, 8, 1, key='bt_workers', help='同時抓幾檔；總請求數仍受每分鐘上限控制')
        bt_seg = st.selectbox('回測期間', BT_SEG_OPTIONS, key='bt_seg',
                              help='三年一次跑完資料量太大（容易記憶體不足當掉），改成分六次：每次跑半年，各自下載原始紀錄檔，最後六個檔一起載入合併分析')
        if bt_seg == BT_SEG_OPTIONS[0]:
            bt_months = st.number_input('回測天數（月）', 1, 36, 3, 1, key='bt_months')
            bt_off = 0
        else:
            bt_months, bt_off = BT_SEG_MONTHS, (BT_SEG_OPTIONS.index(bt_seg) - 1) * BT_SEG_MONTHS
            st.caption(f'本次評估區間：{bt_off}～{bt_off + BT_SEG_MONTHS} 個月前。每段跑完到「🔬 歷史回測」分頁按「產生回測原始紀錄檔」下載；'
                       '六段都下載後，六個檔一起拖進下方「載入」即可合併分析，多因子／飆股搜尋仍依評估日切成三段（每年一段）驗證。')
        bt_div = st.checkbox('📈 近1季YoY乖離度（股價歷史要抓超過1年，明顯拉長時間）', key='bt_div')
        bt_sector = st.checkbox('🏭 類股狀態濾網（每檔多一次 sector 查詢）', key='bt_sector')
        bt_earn = st.checkbox('📊 財報驚喜／財報跳空（每檔多一次 earnings 查詢）', True, key='bt_earn',
                              help='美股最穩定的異常之一：財報 EPS 優於預期、財報日向上跳空的股票，之後1～3個月常持續走強（PEAD）')
        bt_minpx = st.number_input('最低股價（美元，0＝不篩）', 0.0, 1000.0, 10.0, 1.0, key='bt_minpx',
                                   help='評估當天收盤價低於此價的紀錄不收（排除雞蛋水餃股，也省記憶體）')
        bt_liq = st.number_input('最低近20日均成交金額（萬美元，0＝不篩）', 0, 1000000, 1000, 100, key='bt_liq',
                                 help='全美股預設 1000 萬美元：近20日平均成交金額低於此值的評估點不收')
        bt_delay = st.number_input('回測每檔間隔秒數（已有限流器）', 0.0, 5.0, 0.0, 0.1, key='bt_delay')
        run_bt = st.button('🔬 執行歷史回測', use_container_width=True, key='run_bt')
        st.caption('營收YoY用季報，公告延遲以季末+45天估計（非精確申報日）；美股沒有三大法人資料。')
        ups = st.file_uploader('或載入先前匯出的回測原始紀錄（.csv / .csv.gz，可一次選多個檔合併，例如六段各一個）',
                               type=['csv', 'gz'], accept_multiple_files=True)
        up_sig = tuple(sorted((u.name, u.size) for u in ups)) if ups else None
        if up_sig and ss.get('bt_loaded_name') != up_sig:
            try:
                with st.spinner(f'載入 {len(ups)} 個檔案中…'):
                    release_bt_state(ss)
                    ss['bt_df'] = load_bt_files(ups)
                    ss['bt_ver'] = ss.get('bt_ver', 0) + 1
                ss['bt_loaded_name'] = up_sig
                ss['bt_seg_label'] = ''
            except Exception as ex:  # noqa
                st.error(f'讀取失敗：{ex}')

        st.divider()
        st.text_input('OpenAI API Key（選填）', type='password', key='openai_key')
        st.selectbox('AI 模型', ['gpt-4o-mini', 'gpt-4o'], key='openai_model')
        st.caption('評分：🟢80+ 積極做多　🔵65-79 可考慮　🟡50-64 觀望　🔴<50 不適合')

    st.title('📊 US技術分析全攻略 · 美股評分分析系統')
    st.caption('多方力道＝DMI（+DI／-DI／ADX／ADXR）評分；回後買上漲、15種進場型態沿用朱家泓《技術分析全攻略》；'
               '資料來源：Financial Modeling Prep；大盤＝SPY')

    token = ss.get('token', '').strip()
    set_rate_limit(ss.get('rate_limit', 280))

    # ────────────────────────────── 批次分析 ──────────────────────────────
    if run_batch or ss.pop('auto_batch', False):
        stocks = parse_stocks(ss['stocks_text'])
        if not token:
            st.error('請輸入 Financial Modeling Prep API Key')
        elif not stocks:
            st.error('請輸入至少一個股票代號')
        else:
            try:
                j = fmp_get('/profile', dict(symbol='AAPL'), token)
                if not (isinstance(j, list) and j):
                    raise RuntimeError('API Key 無效或額度已用完')
            except Exception as ex:  # noqa
                st.error(f'❌ 無法連線至 Financial Modeling Prep API：{ex}')
                st.stop()
            prog = st.progress(0.0, text='批次分析中...')
            logbox = st.empty()
            logs = []

            def _log(msg):
                logs.append(msg)
                logbox.caption('\n\n'.join(logs[-6:]))

            def _prog(i, n):
                prog.progress(i / n, text=f'批次分析中：{i} / {n}')
            results = batch_analyze(stocks, token, days, use_rt, ex_flags, vpp, req_delay, _log, _prog,
                                    names=K['SP500_NAMES'])
            prog.empty()
            logbox.empty()
            ss['batch'] = dict(results=results, extras=ex_flags, vpp=vpp)
            if not results:
                st.error('所有股票均無法取得資料')
            elif ss.get('auto_track', True):
                recs = hit_records(results, K, get_live_combo_stats(ss, K))
                added = append_hit_log(recs)
                ss['msg'] = f'📌 命中組合 {len(recs)} 檔，新增 {added} 筆到追蹤紀錄'

    # ────────────────────────────── 歷史回測 ──────────────────────────────
    if run_bt:
        if not token:
            st.error('請先輸入 Financial Modeling Prep API Key')
        else:
            bt_names = dict(K['SP500_NAMES'])
            if bt_univ.startswith('全美股'):
                try:
                    with st.spinner('📡 抓取全美股股票池（company-screener）中…'):
                        uni = fetch_market_universe(token)
                    univ = [u['id'] for u in uni]
                    bt_names.update({u['id']: u['name'] for u in uni})
                except Exception as ex:  # noqa
                    st.error(f'❌ 抓取全美股股票池失敗：{ex}')
                    st.stop()
            else:
                univ = {'S&P 500': K['SP500_LIST'], 'Nasdaq-100': K['NASDAQ100_LIST'], 'SOX半導體': K['SOX_LIST'],
                        '目前輸入框清單': parse_stocks(ss['stocks_text'])}[bt_univ]
            st.caption(f'回測股票池：{bt_univ}，共 {len(univ):,} 檔'
                       + ('（約需 11～15 分鐘；回測月數拉長時記憶體用量也會增加，建議搭配流動性門檻）' if len(univ) > 1000 else ''))
            # 先釋放上一段回測留在 session 的資料，避免第2、3段跑的時候記憶體疊加而當掉
            release_bt_state(ss)
            prog = st.progress(0.0, text='🔬 歷史回測執行中...')
            det = st.empty()

            last_sid = {'v': ''}

            def onp(i, total, saved, failed, sid, note=None):
                if sid:
                    last_sid['v'] = sid
                prog.progress(min(1.0, i / max(total, 1)),
                              text=f'🔬 歷史回測執行中（{bt_seg if bt_seg != BT_SEG_OPTIONS[0] else f"過去{bt_months}個月"}）：{i} / {total}')
                det.caption((f'最近完成：{last_sid["v"]}　' if i < total else '完成　') + f'已存 {saved:,} 筆　失敗 {failed} 檔'
                            + (f'　｜{note}' if note else ''))
            df, stt = run_backtest(token, univ, int(bt_months), bt_div, bt_sector, bt_liq * 10000, vpp,
                                   delay=bt_delay, on_progress=onp, names=bt_names, workers=int(bt_workers), min_px=float(bt_minpx),
                                   end_offset_months=bt_off, include_earn=bt_earn)
            ss['bt_seg_label'] = '' if bt_seg == BT_SEG_OPTIONS[0] else bt_seg.split('・')[1][:3]
            prog.empty()
            if stt.get('stopped'):
                st.warning(f"⚠️ 回測提前結束：{stt['stopped']}")
            if stt.get('bm_err'):
                st.warning(f"⚠️ 這次回測抓不到大盤(SPY)資料，相對強弱／大盤濾網不會出現：{stt['bm_err']}")
            if not len(df):
                st.error(f"❌ 這次回測沒有產生任何評估紀錄（{stt['total']}檔裡失敗了{stt['failed']}檔）。最常見的失敗原因："
                         + '；'.join(stt['top_fail'])
                         + '。常見排查：API Key 是否正確／方案每日額度／方案是否支援 historical-price-eod。')
            else:
                ss['bt_df'] = store_bt_df(df)
                ss['bt_ver'] = ss.get('bt_ver', 0) + 1
                ss['bt_loaded_name'] = None
                det.caption(f"回測完成：{stt['saved']:,} 筆評估紀錄，失敗 {stt['failed']} 檔")

    tab_batch, tab_track, tab_bt = st.tabs(['📋 批次分析', '📈 命中追蹤', '🔬 歷史回測'])
    with tab_batch:
        render_batch(st, ss, K)
    with tab_track:
        render_tracking(st, ss, K)
    with tab_bt:
        render_backtest(st, ss, K)


# ────────────────────────────────────────────────────────────────────
def get_live_combo_stats(ss, K):
    """有回測資料時，算一次所有指定組合的即時統計並快取（回測資料換了才重算）"""
    df = ss.get('bt_df')
    if df is None or not len(df):
        return None
    mp = float(ss.get('an_minpx', ANALYSIS_DEFAULTS['min_px']))
    wq = WINSOR_OPTIONS.get(ss.get('an_wins'), ANALYSIS_DEFAULTS['wq'])
    sig = (ss.get('bt_ver'), id(df), len(df), mp, wq)
    if ss.get('live_stats_sig') != sig:
        combos = K['STOCK_PICK_COMBOS'] + K['PINNED_COMBOS'] + K['MOONSHOT_COMBOS']
        dw, draw, _, _ = prep_analysis_df(add_derived(df), mp, wq)
        ss['live_stats'] = compute_live_combo_stats(dw, combos, moon_df=draw)
        ss['live_stats_sig'] = sig
    return ss['live_stats']


def render_batch(st, ss, K):
    B = ss.get('batch')
    if not B or not B['results']:
        st.info('在左側輸入 FMP API Key 與美股代號，點擊「🔍 批次分析」即可開始。')
        return
    results, exf, vpp = B['results'], B['extras'], B['vpp']
    live = get_live_combo_stats(ss, K)
    if live:
        st.caption('🧮 命中組合的 t值／勝率／飆股比例：使用本次程式內的回測即時計算（t值為同日調整，括號內為5/10/20日中最高的t）。')
    else:
        st.caption('🧮 命中組合的 t值／勝率／飆股比例：尚未跑回測，先用回測記錄值（選股型 S 為 2023-10～2026-09 三年回測；沒進榜的組合沒有 t 值）；跑過回測或載入回測紀錄後會改用即時計算。')
    df = summary_frame(results, exf, K, live)
    _reg = batch_market_regime(results)
    if _reg:
        (st.warning if _reg == 2 else st.info)(regime_banner(_reg))

    with st.expander(f"「指定組合命中」編號對照：[選股型] S1~S{len(K['STOCK_PICK_COMBOS'])}　／　[高勝率] #1~#{len(K['PINNED_COMBOS'])}　／　[高標股] M1~M{len(K['MOONSHOT_COMBOS'])}"):
        st.caption('S＝選股型：2023-10～2026-09 美股三年回測（分六個半年），每一年扣掉同一天大盤後都仍有超額報酬（依日 t≥2）、六個半年至少五個為正，最值得看（約每20日 +1.0～1.5%）。'
                   '#＝高勝率；標 ⏱ 的是擇時型（含布林低檔／大盤或類股跌破均線），勝率主要來自大盤反彈，適合判斷大盤落底，不適合當選股依據。'
                   '⚠️ #、M 的歷史統計是 2026-09-21 美股回測（舊版分價量表、夜星未限高檔）記錄的。')
        t0 = pd.DataFrame([{'編號': f'S{i + 1}', '條件組合': '＋'.join(c),
                            '回測記錄(三年，2026-09-30)': format_stockpick_stats(K['STOCK_PICK_STATS'].get(' ＋ '.join(c)))}
                           for i, c in enumerate(K['STOCK_PICK_COMBOS'])])
        st.dataframe(t0, hide_index=True, use_container_width=True)
        t1 = pd.DataFrame([{'編號': f'#{i + 1}', '類型': combo_type_label(c), '條件組合': '＋'.join(c),
                            '歷史勝率': format_winrates(K['PINNED_COMBO_WINRATES'].get(' ＋ '.join(c)))}
                           for i, c in enumerate(K['PINNED_COMBOS'])])
        t2 = pd.DataFrame([{'編號': f'M{i + 1}', '條件組合': '＋'.join(c),
                            '歷史飆股統計': format_moonshot_stats(K['MOONSHOT_COMBO_STATS'].get(' ＋ '.join(c)))}
                           for i, c in enumerate(K['MOONSHOT_COMBOS'])])
        st.dataframe(t1, hide_index=True, use_container_width=True, height=240)
        st.dataframe(t2, hide_index=True, use_container_width=True, height=240)
        st.caption(f"🔵 W＝回測高勝率（{len(K.get('BT_WIN_COMBOS', []))}組，青色）：三年六段回測任一天期勝率>55%，且三年各自依日 t≥2；標 ⏱ 的是擇時型。"
                   f"　🔴 F＝回測飆股（{len(K.get('BT_HOT_COMBOS', []))}組，粉紅色）：10/20日飆股比例≥3倍基準、飆股次數≥10，且三段飆股比例都≥2倍基準。摘要表每類只顯示最強的{BT_HIT_SHOW}個，其餘以 +N 表示。"
                   "　🔢 命中類別數＝S/#/M/W/F 五類中命中幾類；同類組合常共用同一訊號，類別數比總命中數更能反映強度（回測：≥3類 台股20日超額約+2~4%、美股+0.6~1.8%，但波動也較大）。"
                   "　🏷️ 組合型態（依命中類別組合分級）：" + '　'.join(f"{COMBO_STYLE_ICON[k]}＝{'、'.join(c for c, v in COMBO_STYLE_RULES.items() if v == k)}（{COMBO_STYLE_NOTE[k]}）" for k in ('進攻', '穩健', '彩券')) + "；其他組合不標示。")
        tw_ = pd.DataFrame([{'編號': f'W{i + 1}', '類型': combo_type_label(c), '條件組合': '＋'.join(c),
                             '三年回測': '　'.join(f"{h}日 勝率{e['win']}% t={e['t']}" for h, e in sorted(K['BT_WIN_STATS'].get(' ＋ '.join(c), {}).items(), key=lambda kv: int(kv[0])))}
                            for i, c in enumerate(K.get('BT_WIN_COMBOS', []))])
        tf_ = pd.DataFrame([{'編號': f'F{i + 1}', '條件組合': '＋'.join(c),
                             '三年回測': format_moonshot_stats(K['BT_HOT_STATS'].get(' ＋ '.join(c)))}
                            for i, c in enumerate(K.get('BT_HOT_COMBOS', []))])
        st.dataframe(tw_, hide_index=True, use_container_width=True, height=240)
        st.dataframe(tf_, hide_index=True, use_container_width=True, height=240)

    c1, c2, c3, c5, c4 = st.columns([1.2, 1.4, 1.4, 1.1, 1])
    f_rg = c5.radio('大盤適用', ['全部', '有✅', '不含❌'], horizontal=True,
                    help='依今日大盤狀態，看命中的組合在這種大盤下的回測：有✅＝至少一個組合有效；不含❌＝排除命中「這種大盤下回測為負」組合的股票')
    f_pb = c1.radio('進場條件', ['全部', '✅ 符合進場', '❌ 不符合'], horizontal=True)
    f_sc = c2.radio('評分', ['全部', '80+', '65-79', '50-64', '<50'], horizontal=True)
    f_pt = c3.radio('型態確認', ['全部', '✅ 已突破', '🔥 剛突破', '🕒 成形中', '－ 無'], horizontal=True)
    kw = c4.text_input('搜尋代號/名稱')
    m = pd.Series(True, index=df.index)
    if f_pb != '全部':
        m &= df['_pbPass'] == (f_pb == '✅ 符合進場')
    sc = df['多方力道']
    m &= {'全部': True, '80+': sc >= 80, '65-79': (sc >= 65) & (sc < 80), '50-64': (sc >= 50) & (sc < 65),
          '<50': sc < 50}[f_sc]
    ptm = {'全部': None, '✅ 已突破': ['breakout', 'justbreak'], '🔥 剛突破': ['justbreak'],
           '🕒 成形中': ['forming'], '－ 無': ['none']}[f_pt]
    if ptm:
        m &= df['_pt'].isin(ptm)
    if kw:
        m &= df['股票'].str.contains(kw) | df['名稱'].str.contains(kw)
    if f_rg == '有✅' and '_regOk' in df.columns:
        m &= df['_regOk'] > 0
    elif f_rg == '不含❌' and '_regBad' in df.columns:
        m &= df['_regBad'] == 0
    view = df[m].drop(columns=['_pbPass', '_pt', '_regOk', '_regBad'], errors='ignore')
    st.caption(f'顯示 {len(view)} / {len(df)} 檔（點欄位標題可排序）')
    colcfg = {c: st.column_config.NumberColumn(format='%.1f') for c in
              ['+DI', '-DI', 'ADX', 'ADXR', '布林位置%', '布林寬度%', 'RS(vs SPY)%', 'P/E', '營收YoY%(最新季)',
               '營收QoQ%(最新季)', '均價YoY%(最新季)', 'YoY乖離度(近3季合計)pp'] if c in view.columns}
    colcfg['量比'] = st.column_config.NumberColumn(format='%.2f')
    colcfg['漲跌幅%'] = st.column_config.NumberColumn(format='%.2f')
    colcfg['股價'] = st.column_config.NumberColumn(format='%.2f')
    colcfg['成交量'] = st.column_config.NumberColumn(format='%d')
    # 命中欄分色：選股型S＝綠、指定組合#/M＝紫、回測高勝率W＝青、回測飆股F＝粉紅
    hit_colors = {'選股型命中': '#00a050', '指定組合命中': '#7c3aed', '回測高勝率命中': '#0097a7', '回測飆股命中': '#d81b60'}
    try:
        sty = view.style
        for c, colr in hit_colors.items():
            if c in view.columns:
                sty = sty.set_properties(subset=[c], **{'color': colr, 'font-weight': '600'})
        if '財報日' in view.columns:
            # 7天內要公布財報：橘紅色提醒（財報前後波動大）
            def _earn_css(v):
                m_ = re.search(r'（(\d+)天）', str(v))
                n_ = 0 if '（今天）' in str(v) else (1 if '（明天）' in str(v) else (int(m_.group(1)) if m_ else None))
                return 'color:#e65100;font-weight:700' if n_ is not None and n_ <= 7 else ''
            sty = sty.map(_earn_css, subset=['財報日']) if hasattr(sty, 'map') else sty.applymap(_earn_css, subset=['財報日'])
        if '組合型態' in view.columns:
            _sc = {'進攻': 'color:#d84315;font-weight:700', '穩健': 'color:#2e7d32;font-weight:700', '彩券': 'color:#8e24aa;font-weight:700'}
            def _style_css(v):
                return next((c for k, c in _sc.items() if k in str(v)), '')
            sty = sty.map(_style_css, subset=['組合型態']) if hasattr(sty, 'map') else sty.applymap(_style_css, subset=['組合型態'])
        show_obj = sty
    except Exception:  # noqa  沒有 jinja2 等套件時退回不上色
        show_obj = view
    st.dataframe(show_obj, hide_index=True, use_container_width=True, column_config=colcfg,
                 height=min(560, 38 + 35 * max(len(view), 1)))
    b1, b2, b3 = st.columns([1, 1.4, 2])
    b1.download_button('📥 匯出 Excel', df_to_excel_bytes(view, '批次分析摘要'),
                       file_name=f'美股批次分析摘要_{dt.date.today()}.xlsx')
    recs = hit_records(results, K, live)
    b2.download_button(f'🎯 匯出命中組合股票（{len(recs)}檔）', hits_excel_bytes(recs),
                       file_name=f'美股命中組合股票_{dt.date.today()}.xlsx', disabled=not recs, key='dl_hits')
    if b3.button('📌 將命中股票加入追蹤紀錄', disabled=not recs, key='add_track'):
        n = append_hit_log(recs)
        st.success(f'已加入 {n} 筆新紀錄（同一天同一檔不重複）')

    st.divider()
    opts = [f"{r['stockId']} {r['name']}（{r['total']}分）" for r in results]
    pick = st.selectbox('🏷️ 個股詳細分析', range(len(results)), format_func=lambda i: opts[i])
    render_single(st, ss, results[pick], vpp)


def _tags(sigs):
    return ''.join(f"<span class='tagb {t}'>{'▲' if t == 'bull' else '▼' if t == 'bear' else '─'} {s}</span>" for s, t in sigs)


def render_analyst(st, d):
    c = d.get('consensus')
    if c:
        n = sum(int(c.get(k) or 0) for k in ('strongBuy', 'buy', 'hold', 'sell', 'strongSell'))
        st.markdown('##### 🏦 分析師評等總覽')
        st.markdown(f"🟢 強力買進 {c.get('strongBuy', 0)}　🟢 買進 {c.get('buy', 0)}　🟡 持有 {c.get('hold', 0)}　"
                    f"🔴 賣出 {c.get('sell', 0)}　🔴 強力賣出 {c.get('strongSell', 0)}　（共 {n} 位分析師）")
    g = d.get('grades') or []
    if g:
        st.markdown('##### 🔔 最近評等異動')
        st.dataframe(pd.DataFrame([{'日期': x.get('date'), '機構': x.get('gradingCompany'), '原評等': x.get('previousGrade'),
                                    '新評等': x.get('newGrade'), '動作': x.get('action')} for x in g[:8]]),
                     hide_index=True, use_container_width=True)
    pc_, ps_ = d.get('pt_consensus'), d.get('pt_summary')
    if pc_ or ps_:
        st.markdown('##### 🎯 分析師目標價')
        txt = []
        if pc_:
            txt.append(f"共識目標價 **${fnum(pc_.get('targetConsensus'), 2) if isnum(pc_.get('targetConsensus')) else '--'}**　"
                       f"區間 ${pc_.get('targetLow', '--')} ~ ${pc_.get('targetHigh', '--')}")
        if ps_:
            try:
                q, y = float(ps_.get('lastQuarterAvgPriceTarget')), float(ps_.get('lastYearAvgPriceTarget'))
                txt.append(f"近一季平均目標價 vs 去年：{(q - y) / y * 100:+.1f}%（${y:.2f} → ${q:.2f}）")
            except Exception:  # noqa
                pass
        st.markdown('　｜　'.join(txt))
    ins = d.get('insider') or []
    if ins:
        st.markdown('##### 👤 最近內部人交易')
        st.dataframe(pd.DataFrame([{'日期': x.get('transactionDate') or x.get('filingDate'), '姓名': x.get('reportingName'),
                                    '職稱': x.get('typeOfOwner'), '類型': x.get('transactionType'),
                                    '股數': x.get('securitiesTransacted'), '價格': x.get('price')} for x in ins[:8]]),
                     hide_index=True, use_container_width=True)
    if not (c or g or pc_ or ps_ or ins):
        st.caption('目前查無分析師評等／目標價／內部人交易資料（可能沒有分析師覆蓋，或 FMP 無該類資料）。')
    st.caption('ⓘ FMP 目前不提供空單比例（short interest）資料，故無此項。')
    if d.get('errs'):
        st.warning('部分資料讀取失敗：' + '　'.join(d['errs']))


def render_single(st, ss, r, vpp):
    b, info = r['bars'], r['info']
    e = b.n - 1
    dm, pb, pt, sig = info['dm'], info['pb'], info['pt'], info['sig']
    total = r['total']
    last_c = b.close[e]
    prev_c = b.close[e - 1] if e >= 1 else last_c
    chg = last_c - prev_c
    chgp = chg / prev_c * 100 if prev_c else 0
    vr = b.volume[e] / b.vm20[e] if b.vm20[e] else 1
    st.subheader(f"🏷️ {r['stockId']} {r['name']}")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric('最新收盤', '$' + fnum(last_c, 2), f'{chg:+.2f} ({chgp:+.2f}%)', delta_color='inverse')
    m2.metric('當日成交量', f'{b.volume[e]:,.0f}')
    m3.metric('量比 vs MA20', f"{vr:.2f}x（{'放量' if vr > 1.2 else '縮量' if vr < 0.8 else '正常'}）")
    m4.metric('資料日期', b.date[e])

    st.markdown('#### 📊 多方力道評分')
    cA, cB = st.columns([1, 2])
    lbl = '強力買進訊號' if total >= 80 else '可考慮進場' if total >= 65 else '觀望為主' if total >= 50 else '不建議進場'
    col = '#00c864' if total >= 80 else '#2196f3' if total >= 65 else '#f0a500' if total >= 50 else '#ff3c3c'
    cA.markdown(f"<div style='font-size:56px;font-weight:700;color:{col};line-height:1'>{total}</div>"
                f"<div class='small'>綜合評分 / 100</div><div style='color:{col};font-weight:700'>{lbl}</div>",
                unsafe_allow_html=True)
    for t, s, mx, d in [('🧭 方向性 (+DI vs -DI)', dm['diPts'], 50, '+DI相對-DI的優勢程度'),
                        ('💪 趨勢強度 (ADX)', dm['adxPts'], 30, 'ADX值，越高趨勢越明確'),
                        ('🚀 趨勢動能 (ADX vs ADXR)', dm['adxrPts'], 20, 'ADX>ADXR代表趨勢正在轉強')]:
        cB.progress(min(1.0, s / mx), text=f'{t}　{s:.1f}/{mx}　（{d}）')
    st.markdown('#### 🔔 技術訊號')
    st.markdown(_tags(dm['sigs']), unsafe_allow_html=True)

    st.markdown('#### 💡 操作建議')
    ma20v = b.ma20[e] or last_c
    bbUp = b.bbU[e] or last_c * 1.1
    name = f"{r['stockId']} {r['name']}"
    if total >= 80:
        act = '🟢 積極做多'
        adv = [f'**{name}** 多方力道評分 {total} 分，DMI顯示多方力道強勁，建議積極做多。']
        if dm['tdir'] == '多頭':
            adv.append('+DI大於-DI且趨勢轉強，順勢操作，逢低分批佈局。')
        sl, tg = f'停損設於 **${ma20v * 0.97:.2f}**（20MA下方3%）', f'目標參考 **${bbUp:.2f}**（布林上軌）'
    elif total >= 65:
        act = '🔵 可考慮進場'
        adv = [f'**{name}** 評分 {total} 分，DMI偏多，可考慮分批進場。', '建議等待回測均線後再進場，降低風險。']
        sl, tg = f'停損建議 **${ma20v * 0.98:.2f}**（20MA下方2%）', f'短線目標 **${last_c * 1.08:.2f}**（+8%）'
    elif total >= 50:
        act = '🟡 觀望為主'
        adv = [f'**{name}** 評分 {total} 分，DMI訊號混雜或趨勢不明確，建議觀望。', '等待ADX轉強或+DI/-DI方向明確後再行動。']
        sl, tg = '暫不建議進場', '等待更佳時機'
    else:
        act = '🔴 不適合進場'
        adv = [f'**{name}** 評分 {total} 分，DMI偏空，不建議進場。',
               '目前-DI大於+DI，空方力道較強，切忌逆勢做多，等待趨勢反轉。' if dm['tdir'] == '空頭' else 'DMI指標偏弱或資料不足，應持現金等待機會。']
        sl, tg = '持倉者建議設停損出場', '等待多頭訊號出現'
    hints = [s[0] for s in dm['sigs'] if s[1] == 'bull'][:5]
    import re as _re

    def _b(t):
        return _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
    html = (f"<div style='background:rgba(33,150,243,.08);border:1px solid rgba(33,150,243,.3);border-radius:10px;"
            f"padding:12px 18px;line-height:1.9'><div style='font-size:17px;font-weight:700'>{act}</div>"
            + '<br>'.join(_b(a) for a in adv)
            + f"<div style='margin-top:6px'>🛑 <b>停損：</b>{_b(sl)}<br>🎯 <b>目標：</b>{_b(tg)}"
            + (f"<br>✅ <b>多頭訊號：</b>{' · '.join(hints)}" if hints else '') + '</div></div>')
    st.markdown(html, unsafe_allow_html=True)

    akey = r['stockId'] + b.date[e]
    if st.button('🤖 AI 智能綜合分析', key='ai_' + akey):
        k = ss.get('openai_key', '').strip()
        if not k:
            ss['ai'][akey] = '請先在左側輸入 OpenAI API Key，才能使用 AI 智能綜合分析。'
        else:
            with st.spinner('正在請 AI 綜合研判技術面數據，請稍候…'):
                try:
                    ss['ai'][akey] = run_ai(build_ai_prompt(r), k, ss.get('openai_model', 'gpt-4o-mini'))
                except Exception as ex:  # noqa
                    ss['ai'][akey] = f'❌ AI 分析失敗：{ex}\n請確認 API Key 是否正確、額度是否足夠，或稍後再試。'
    if akey in ss['ai']:
        st.markdown(ss['ai'][akey])

    fkey = 'fund_' + r['stockId']
    if st.button('🏦 載入分析師評等／目標價／內部人交易', key='btn_' + fkey):
        tok = ss.get('token', '').strip()
        if not tok:
            st.error('請先在左側輸入 FMP API Key。')
        else:
            with st.spinner('正在抓取分析師評等、目標價與內部人交易資料…'):
                ss[fkey] = fetch_analyst_bundle(r['stockId'], tok)
    if ss.get(fkey):
        render_analyst(st, ss[fkey])

    cL, cR = st.columns(2)
    with cL:
        st.markdown('#### 🎯 回後買上漲 · 進場條件核對')
        if pb['allPass']:
            st.success('✅ 符合進場條件（必要條件全部通過）' + ('  +成交量增加分' if pb['bonusPassed'] else ''))
        else:
            st.error(f"❌ 不符合進場條件（必要條件 {pb['requiredPassed']}/{pb['requiredTotal']} 通過）")
        for x in pb['results']:
            icon = '✅' if x['pass_'] else ('❌' if x['required'] else '—')
            st.markdown(f"{icon} {x['label']}" + ('' if x['required'] else '（加分）')
                        + (f"　<span class='small'>({x['detail']})</span>" if x['detail'] else ''), unsafe_allow_html=True)
    with cR:
        st.markdown('#### 🔍 型態確認（15種進場型態）')
        if pt['anyJustBroke']:
            st.warning('🔥 偵測到剛突破買點（較前一交易日新增）')
        elif pt['anyBreakout']:
            st.success('✅ 偵測到型態突破買點')
        elif pt['anyFormed']:
            st.info('🕒 型態成形中，尚未突破確認')
        else:
            st.caption('－ 目前未偵測到符合的進場型態')
        hc_hit = [x['name'] for x in pt['results'] if x['id'] in HIGH_CONSOLIDATION_PATTERNS and x['formed']]
        if hc_hit:
            st.info('📈 高檔強勢整理：' + '、'.join(hc_hit) + '（回測顯示強勢股出現多為洗盤後續漲，留意是否守住首日低點）')
        for x in pt['results']:
            icon = '🔥' if x['justBroke'] else ('✅' if x['breakout'] else ('🕒' if x['formed'] else '－'))
            st.markdown(f"{icon} **{x['name']}**" + ('（高檔強勢整理）' if x['id'] in HIGH_CONSOLIDATION_PATTERNS else '')
                        + f"<br><span class='small'>{x['desc']}" + (f"<br>{x['detail']}" if x['formed'] else '') + '</span>',
                        unsafe_allow_html=True)

    st.markdown('#### 📐 指標對照')
    g1, g2, g3, g4, g5 = st.columns(5)
    rv = b.rsi[e]
    g1.metric('RSI', fnum(rv))
    g1.caption('⚠️ 超買區（>70）' if (rv or 0) > 70 else '💚 超賣區（<30）' if (rv or 100) < 30 else '正常區間（30-70）')
    if b.kdK[e] is not None:
        g2.metric('KD', f"K={b.kdK[e]:.1f} D={b.kdD[e]:.1f}")
        g2.caption('K>D 偏多' if b.kdK[e] > b.kdD[e] else 'K<D 偏空')
    cross = ''
    if e >= 1:
        if b.macd[e - 1] <= b.macdSig[e - 1] and b.macd[e] > b.macdSig[e]:
            cross = '⚡黃金交叉'
        elif b.macd[e - 1] >= b.macdSig[e - 1] and b.macd[e] < b.macdSig[e]:
            cross = '⚡死亡交叉'
    g3.metric('MACD', f"DIF={b.macd[e]:.2f}")
    g3.caption(f"Signal={b.macdSig[e]:.2f}　柱={b.macdHist[e]:.2f}　"
               + ('DIF在Signal上方，偏多' if b.macd[e] > b.macdSig[e] else 'DIF在Signal下方，偏空') + (' ' + cross if cross else ''))
    if sig['bbPos'] is not None:
        bw = (b.bbU[e] - b.bbL[e]) / last_c * 100
        g4.metric('布林位置', f"{sig['bbPos']:.0f}%")
        g4.caption(f'上軌 {b.bbU[e]:.2f}　下軌 {b.bbL[e]:.2f}　寬度 {bw:.1f}%' + ('　🔸收窄，留意變盤' if bw < 8 else ''))
    prof, vps = info['prof'], info['vps']
    if prof:
        g5.metric('分價量表 POC', f"{prof['pocPrice']:.1f}")
        g5.caption(f"POC區 {prof['pocLow']:.1f}~{prof['pocHigh']:.1f}　"
                   + (vps['detail'].split('：')[0] if vps['detail'] else '無訊號'))
    ma_rows = [{'均線': nm, '數值': round(arr[e], 2), '股價偏離%': round((last_c - arr[e]) / arr[e] * 100, 2)}
               for nm, arr in (('MA5', b.ma5), ('MA10', b.ma10), ('MA20', b.ma20), ('MA60', b.ma60)) if arr[e] is not None]
    if vps['detail']:
        st.caption('分價量表訊號：' + vps['detail'])
    st.dataframe(pd.DataFrame(ma_rows), hide_index=True)

    st.markdown('#### 📉 技術分析圖表')
    st.plotly_chart(draw_chart(r, vpp), use_container_width=True, theme=None)
    with st.expander('📋 原始資料（最近20筆）'):
        s = max(0, e - 19)
        raw = pd.DataFrame({'日期': b.date[s:], '開盤': b.open[s:], '最高': b.high[s:], '最低': b.low[s:],
                            '收盤': b.close[s:], '成交量': b.volume[s:], 'MA5': b.ma5[s:], 'MA20': b.ma20[s:],
                            'MA60': b.ma60[s:], 'RSI': b.rsi[s:], 'KD-K': b.kdK[s:], 'KD-D': b.kdD[s:],
                            'MACD': b.macd[s:], 'Signal': b.macdSig[s:], 'BB上軌': b.bbU[s:], 'BB下軌': b.bbL[s:]})
        st.dataframe(raw.iloc[::-1].round(2), hide_index=True, use_container_width=True)



# ────────────────────────────────────────────────────────────────────
def render_tracking(st, ss, K):
    log = load_hit_log()
    st.subheader('📈 命中組合每日追蹤')
    st.caption(f'追蹤紀錄檔：{HIT_LOG_FILE}　｜　每次批次分析自動累加（可在側邊欄關閉），同一天同一檔只記一次。'
               '進場價＝命中當天收盤價；「5/10/20日報酬」要等滿天數才會出現。')
    if not len(log):
        st.info('目前沒有追蹤紀錄。跑一次批次分析（建議用「漲幅前100／成交量前100」），命中組合的股票會自動加入。')
        return
    c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.2, 1])
    c1.metric('追蹤紀錄', f'{len(log)} 筆')
    c2.metric('股票數', f"{log['股票代號'].nunique()} 檔")
    c3.metric('期間', f"{log['日期'].min()} ~ {log['日期'].max()}")
    if c4.button('🔄 更新追蹤報酬', type='primary', key='upd_track'):
        tok = ss.get('token', '').strip()
        if not tok:
            st.error('請先輸入 FMP API Key')
        else:
            prog = st.progress(0.0, text='抓取最新股價中...')
            ss['track_perf'] = track_performance(log, tok, delay=0.0,
                                                 progress=lambda i, n: prog.progress(i / n, text=f'抓取最新股價：{i}/{n}'))
            prog.empty()
    perf = ss.get('track_perf')
    days = sorted(log['日期'].unique(), reverse=True)
    pick = st.selectbox('檢視日期', ['全部'] + days, key='track_day')
    if perf is None or not len(perf):
        view = log if pick == '全部' else log[log['日期'] == pick]
        st.caption('尚未更新報酬，先顯示原始命中紀錄。按「🔄 更新追蹤報酬」抓最新股價。')
        st.dataframe(view.drop(columns=['命中組合']), hide_index=True, use_container_width=True)
    else:
        view = perf if pick == '全部' else perf[perf['日期'] == pick]
        st.markdown('##### 🧾 追蹤明細')
        st.dataframe(view, hide_index=True, use_container_width=True, height=380)
        st.markdown('##### 🧩 組合實盤表現（命中後實際報酬 vs 回測記錄）')
        summ = combo_track_summary(perf, K)
        st.caption('實盤勝率明顯低於回測記錄的組合，代表可能是過度適配，可考慮從指定組合移除；樣本少於10筆前先別下結論。')
        st.dataframe(summ, hide_index=True, use_container_width=True, height=380)
        st.download_button('📥 匯出追蹤報表', sheets_to_xlsx([('命中紀錄', log), ('追蹤明細', perf), ('組合彙總', summ)]), file_name=f'美股命中追蹤_{dt.date.today()}.xlsx', key='dl_track')
    with st.expander('🗂️ 管理追蹤紀錄'):
        keep_days = st.number_input('只保留最近幾天的紀錄', 5, 3650, 120, 5, key='keep_days')
        if st.button('🧹 刪除更早的紀錄', key='prune_track'):
            cut = str(dt.date.today() - dt.timedelta(days=int(keep_days)))
            log2 = log[log['日期'] >= cut]
            log2.to_csv(HIT_LOG_FILE, index=False, encoding='utf-8-sig')
            ss['track_perf'] = None
            st.success(f'已刪除 {len(log) - len(log2)} 筆')
        st.code(DAILY_USAGE, language='text')

# ────────────────────────────────────────────────────────────────────
def render_backtest(st, ss, K):
    df = ss.get('bt_df')
    if df is None or not len(df):
        st.info('點擊左側「🔬 執行歷史回測」，或載入先前匯出的回測原始紀錄。')
        return
    df_all = add_derived(df)
    d0, d1 = str(df_all['evalDate'].min()), str(df_all['evalDate'].max())
    st.subheader('🔬 歷史回測分析結果')
    st.caption(f"共 {len(df_all):,} 筆評估紀錄（{d0} ～ {d1}），{df_all['stockId'].nunique()} 檔股票")
    if len(df_all) > 450_000:
        st.warning(f'⚠️ 資料量 {len(df_all):,} 筆偏大，Streamlit Cloud（記憶體約1GB）可能跑不動而當掉。'
                   '可以先只載入其中兩段，或回測時提高流動性門檻／最低股價減少筆數。')
    # 原始紀錄檔按了才產生（全美股約20萬筆，每次重跑都先壓一次檔會多花十幾秒和幾百MB記憶體）
    if ss.get('_bt_csv_sig') == ss.get('bt_ver') and ss.get('_bt_csv'):
        st.download_button('💾 下載回測原始紀錄（.csv.gz，之後可直接載入，不用重抓）', ss['_bt_csv'],
                           file_name=f'美股回測原始紀錄{("_" + ss["bt_seg_label"]) if ss.get("bt_seg_label") else ""}_{dt.date.today()}.csv.gz', key='dl_btcsv')
    elif st.button('💾 產生回測原始紀錄檔（之後可直接載入，不用重抓）', key='mk_btcsv'):
        with st.spinner('壓縮中…'):
            ss['_bt_csv'] = bt_csv_gz(df_all)
            ss['_bt_csv_sig'] = ss.get('bt_ver')
        st.rerun()

    st.markdown('##### ⚙️ 分析篩選（套用到下面所有統計；改了不用重抓資料）')
    a1, a2 = st.columns(2)
    minpx = a1.number_input('最低股價（美元，0＝不篩）', 0.0, 1000.0, ANALYSIS_DEFAULTS['min_px'], 1.0, key='an_minpx',
                            help='以評估當天收盤價篩選，排除雞蛋水餃股')
    wlbl = a2.selectbox('報酬極端值處理', list(WINSOR_OPTIONS), key='an_wins',
                        help='截尾＝把最極端的報酬壓到分位數上下限（winsorize），平均報酬與t值不會被少數暴漲股拉歪；勝率與中位數不受影響。飆股搜尋一律用原始報酬。')
    df, df_raw, cuts, dropped = prep_analysis_df(df_all, float(minpx), WINSOR_OPTIONS[wlbl])
    fk = (float(minpx), WINSOR_OPTIONS[wlbl])   # 快取鍵：篩選條件不同就重算
    if not len(df):
        st.warning('篩選後沒有任何紀錄，請調低最低股價。')
        return
    cut_txt = '　'.join(f'{h}日 {lo:+.1f}%～{hi:+.1f}%' for h, (lo, hi) in cuts.items())
    st.caption(f"篩選後 {len(df):,} 筆（{df['stockId'].nunique()} 檔；股價<{minpx:g} 排除 {dropped:,} 筆）"
               + (f"｜截尾範圍：{cut_txt}" if cuts else '｜報酬未截尾'))

    def show(title, t, note=None):
        st.markdown(f'##### {title}')
        if note:
            st.caption(note)
        if t is None or not len(t):
            st.caption('（無資料）')
        else:
            st.dataframe(t, hide_index=True, use_container_width=True)

    show('📏 全體基準（所有評估紀錄）', baseline_row(df), '以下各訊號都跟這一列比，高於基準才代表訊號有用。平均報酬為截尾後；中位數不受極端值影響。')
    show('📊 評分區間 vs 實際報酬', score_buckets(df))
    show('🧮 訊號堆疊數量 vs 實際報酬（Confluence Count）', confluence_table(df),
         '同一時間點同時觸發的獨立訊號數（型態剛形成、KDJ交叉、MACD黃金交叉、布林極端位置、爆量，最多5個）。')
    for title, fld, lbl in [('🚀 「強勢突破盤」', 'isBreakout', '強勢突破盤'), ('🎯 「跌深反彈盤」', 'isPullbackRebound', '跌深反彈盤'),
                            ('⚡ 「MACD近3日內黃金交叉」', 'goldenCrossRecent', 'MACD黃金交叉'),
                            ('🟢 「KDJ近3日內黃金交叉」', 'kdjGoldenCrossRecent', 'KDJ黃金交叉'),
                            ('🔴 「KDJ近3日內死亡交叉」', 'kdjDeathCrossRecent', 'KDJ死亡交叉'),
                            ('💪 「相對強弱(vs大盤SPY，20日)」', 'relStrengthPositive', '相對強弱為正'),
                            ('📊 「爆量(≥1.5倍均量)」', 'volSurge15', '爆量1.5倍'), ('📊 「爆量(≥2倍均量)」', 'volSurge2', '爆量2倍'),
                            ('📉 「地量(≤0.5倍均量)」', 'volLow05', '地量'),
                            ('📈 「量能區間高檔(≥90百分位)」', 'volRangeHigh90', '量能區間高檔'),
                            ('📉 「量能區間低檔(≤10百分位)」', 'volRangeLow10', '量能區間低檔')]:
        t = tag_hitrate(df, fld, lbl)
        if len(t):
            show(title + '標記 vs 實際報酬', t)
    if 'vpBuySupport' in df:
        st.markdown('##### 📊 個股分價量表（POC）訊號 vs 實際報酬')
        st.caption('⚠️ 美股 2026-09-27 回測：四個分價量表訊號單獨都沒有超額報酬，已不當組合搜尋條件（USE_VP_FLAGS_US）。'
                   '以成交量最大價位區（POC）為界：守穩POC買進、突破POC追價買進、反彈POC遇壓賣出、破位停損賣出（帶量長黑跌破POC）。')
        st.dataframe(pd.concat([tag_hitrate(df, f, l) for f, l in VP_LABELS.items()], ignore_index=True),
                     hide_index=True, use_container_width=True)
    for title, fld, lbl in [('🌐 「大盤站上20日均線」', 'benchmarkAbove20', '大盤站上20日均線'),
                            ('🌐 「大盤站上60日均線」', 'benchmarkAbove60', '大盤站上60日均線'),
                            ('🏭 「類股站上20日均線」', 'sectorAbove20', '類股站上20日均線'),
                            ('🏭 「類股站上60日均線」', 'sectorAbove60', '類股站上60日均線'),
                            ('🔍 「型態突破確認」', 'patternBreakout', '型態突破確認'),
                            ('🔥 「型態剛形成(剛突破)」', 'patternJustBroke', '型態剛形成'),
                            ('✅ 「回後買上漲全通過」', 'pbAllPass', '回後買上漲全通過')]:
        t = tag_hitrate(df, fld, lbl)
        if len(t):
            show(title + '標記 vs 實際報酬', t)
    show('📐 15種型態各自「剛形成」vs 實際報酬', bt_memo(ss, 'pat', fk + (), lambda: pattern_hits(df)), '依樣本數排序，樣本數<10筆的不列出。')
    show('🆕 新增技術面條件 vs 實際報酬（52週高點／均線多頭排列／布林收窄／向上跳空）',
         bt_memo(ss, 'newtech', fk + (), lambda: new_tech_table(df)),
         '52週高點需要約一年的歷史，回測會自動多抓資料；資料不足的評估點不列入「是／否」。')

    st.markdown('##### 🎛️ 參數網格搜尋（單一評分公式的權重調整）')
    gh = st.selectbox('優化目標天數', HORIZONS, index=1, key='gh', format_func=lambda h: f'{h}日')
    st.caption('⚠️ 在已收集的歷史資料上找「表現較好」的參數組合，樣本有限時容易過度適配，僅供方向參考。')
    st.dataframe(bt_memo(ss, 'grid', fk + (gh,), lambda: grid_search(df, gh)), hide_index=True, use_container_width=True)

    # ── 多因子複選搜尋 ──
    st.markdown('##### 🧩 多因子複選搜尋（找出哪幾項欄位組合起來勝率最高）')
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    ch = c1.selectbox('優化目標天數', HORIZONS, index=1, key='ch', format_func=lambda h: f'{h}日')
    cwr = c2.number_input('勝率門檻(%)', 0.0, 100.0, 50.0, 5.0, key='cwr')
    cms = c3.number_input('最少樣本數', 5, 5000, 50, 5, key='cms')
    csort = c4.selectbox('排序依據', ['t值(同日調整)', '勝率'], key='csort',
                         help='t值(同日調整)：優先列出扣掉同一天大盤後仍然比較強的「選股型」組合；勝率：舊排序，容易被擇時型組合佔滿')
    cmt = c5.number_input('t值門檻', -10.0, 20.0, 1.0, 0.5, key='cmt',
                          help='排除 t值(同日調整) 低於門檻的組合（t<1：扣掉同一天大盤後幾乎沒有超額報酬，勝率多半只是跟著大盤）')
    cred = c6.checkbox('排除冗餘組合', True, key='cred', help='多加一個條件後樣本完全沒變（例如「母子懷抱剛形成」必然也是「型態剛形成」），這種組合不重複列出')
    if not bt_memo_ready(ss, 'combo', fk + (ch, int(cms), float(cwr), cred, csort)) and not st.button('🧩 開始多因子搜尋', key='run_combo', type='primary'):
        st.info(f'回測資料共 {len(df):,} 筆；多因子搜尋需要 1～3 分鐘、也比較吃記憶體，按上方按鈕才開始計算（同一組參數算過一次就會記住，改參數要再按一次）。')
    else:
        with st.spinner('多因子複選搜尋計算中…'):
            res, tested, base = bt_memo(ss, 'combo', fk + (ch, int(cms), float(cwr), cred, csort),
                                        lambda: combo_search(df, ch, 3, int(cms), float(cwr), cred, 't' if csort.startswith('t') else 'win'))
        n_before_t = len(res)
        if len(res) and 't值(同日調整)' in res.columns:
            res = res[res['t值(同日調整)'] >= float(cmt)].reset_index(drop=True)
        cboth = st.checkbox('只列各段 t值都 ≥ 2 的組合（✅ 各段一致）', False, key='cboth',
                            help='回測期間依評估日切段（資料超過18個月切三段，否則前後兩半），各段各自算同日調整 t 值。'
                                 '每段都 ≥2 代表不是單一段行情造成的巧合，最值得相信')
        if cboth and len(res) and '各段一致' in res.columns:
            res = res[res['各段一致'] == '✅'].reset_index(drop=True)
        if len(res):
            res.insert(1, '類型', res['條件組合'].map(lambda s: combo_type_label([x.strip() for x in s.split('＋')])))
        if base:
            st.caption(f"全體基準（{ch}日）：樣本 {base['n']:,}　平均報酬 {base['avg']:.2f}%　勝率 {base['win']:.1f}%　中位數 {base['median']:.2f}%　"
                       f"｜共測試 {tested:,} 種組合，勝率≥{cwr:.0f}% 的 {n_before_t:,} 組，再排除 t值<{cmt:g} 後剩 {len(res):,} 組"
                       + (f"｜{base['parts']}（✅＝每段 t 都 ≥2，⚠️k/n＝n段中有k段達標）。" if base.get('parts') else '。')
                       + '⚠️ 測試組合越多，純運氣突出的也越多。「同日超額報酬」＝每筆報酬扣掉同一天全部紀錄的平均，t值也用它算，已排除大盤齊漲齊跌；t>2 較可信。')
        st.dataframe(res, hide_index=True, use_container_width=True, height=420)
        if len(res):
            st.download_button('📥 匯出 Excel', df_to_excel_bytes(res, '多因子複選搜尋'),
                               file_name=f'多因子複選搜尋_勝率{cwr:.0f}%以上_t{cmt:g}以上_{dt.date.today()}.xlsx', key='dl_combo')


    # ── 飆股搜尋 ──
    st.markdown('##### 🚀 飆股搜尋（找出最容易出現大行情的組合）')
    d1, d2, d3, d4, d5 = st.columns(5)
    mh = d1.selectbox('天數', HORIZONS, index=1, key='mh', format_func=lambda h: f'{h}日')
    mthr = d2.number_input('漲幅門檻(%)', 5.0, 200.0, 30.0, 5.0, key='mthr')
    mpct = d3.number_input('飆股比例門檻(%)', 0.0, 100.0, 10.0, 1.0, key='mpct')
    mms = d4.number_input('最少樣本數', 5, 5000, 20, 5, key='mms')
    mred = d5.checkbox('排除冗餘組合', True, key='mred')
    if not bt_memo_ready(ss, 'moon', fk + (mh, float(mthr), int(mms), float(mpct), mred)) and not st.button('🚀 開始飆股搜尋', key='run_moon', type='primary'):
        st.info(f'回測資料共 {len(df):,} 筆；飆股搜尋需要 1～3 分鐘、也比較吃記憶體，按上方按鈕才開始計算（同一組參數算過一次就會記住，改參數要再按一次）。')
    else:
        with st.spinner('飆股搜尋計算中…'):
            mres, mtested, mbase = bt_memo(ss, 'moon', fk + (mh, float(mthr), int(mms), float(mpct), mred),
                                           lambda: moonshot_search(df_raw, mh, float(mthr), 3, int(mms), float(mpct), mred))
        if mbase:
            st.caption(f"全體基準（{mh}日漲幅>{mthr:.0f}%）：{mbase['n']:,} 筆中 {mbase['moonN']:,} 次飆股，"
                       f"基準飆股比例 {mbase['pct']:.2f}%　｜共測試 {mtested:,} 種組合，達標 {len(mres):,} 組"
                       + (f"｜{mbase['parts']}，每段飆股比例都高才可信。" if mbase.get('parts') else '。')
                       + '「倍數」＝組合飆股比例÷基準；飆股是稀有事件，飆股次數只有個位數的不建議當真。')
        st.dataframe(mres, hide_index=True, use_container_width=True, height=420)
        if len(mres):
            st.download_button('📥 匯出 Excel', df_to_excel_bytes(mres, '飆股搜尋'),
                               file_name=f'飆股搜尋_比例{mpct:.0f}%以上_{dt.date.today()}.xlsx', key='dl_moon')



# ════════════════════════════════════════════════════════════════════
#  內建清單與指定追蹤組合（由美股 HTML 版搬過來）
# ════════════════════════════════════════════════════════════════════
SP500_LIST = [
    "MMM", "AOS", "ABT", "ABBV", "ACN", "ADBE", "AMD", "AES", "AFL", "A", "APD", "ABNB", "AKAM", "ALB", "ARE", "ALGN", "ALLE", "LNT", "ALL", "GOOGL",
    "GOOG", "MO", "AMZN", "AMCR", "AEE", "AEP", "AXP", "AIG", "AMT", "AWK", "AMP", "AME", "AMGN", "APH", "ADI", "AON", "APA", "APO", "AAPL", "AMAT",
    "APP", "APTV", "ACGL", "ADM", "ARES", "ANET", "AJG", "AIZ", "T", "ATO", "ADSK", "ADP", "AZO", "AVB", "AVY", "AXON", "BKR", "BALL", "BAC", "BAX",
    "BDX", "BRK.B", "BBY", "TECH", "BIIB", "BLK", "BX", "XYZ", "BNY", "BA", "BKNG", "BSX", "BMY", "AVGO", "BR", "BRO", "BF.B", "BLDR", "BG", "BXP",
    "CHRW", "CDNS", "CPT", "COF", "CAH", "CCL", "CARR", "CVNA", "CASY", "CAT", "CBOE", "CBRE", "CDW", "COR", "CNC", "CNP", "CF", "CRL", "SCHW", "CHTR",
    "CVX", "CMG", "CB", "CHD", "CIEN", "CI", "CINF", "CTAS", "CSCO", "C", "CFG", "CLX", "CME", "CMS", "KO", "CTSH", "COHR", "COIN", "CL", "CMCSA",
    "FIX", "COP", "ED", "STZ", "CEG", "COO", "CPRT", "GLW", "CPAY", "CTVA", "CSGP", "COST", "CRH", "CRWD", "CCI", "CSX", "CMI", "CVS", "DHR", "DRI",
    "DDOG", "DVA", "DECK", "DE", "DELL", "DAL", "DVN", "DXCM", "FANG", "DLR", "DG", "DLTR", "D", "DPZ", "DASH", "DOV", "DOW", "DHI", "DTE", "DUK",
    "DD", "ETN", "EBAY", "ECHO", "ECL", "EIX", "EW", "EA", "ELV", "EME", "EMR", "ETR", "EOG", "EQT", "EFX", "EQIX", "EQR", "ERIE", "ESS", "EL",
    "EG", "EVRG", "ES", "EXC", "EXE", "EXPE", "EXPD", "EXR", "XOM", "FFIV", "FDS", "FICO", "FAST", "FRT", "FDX", "FDXF", "FIS", "FITB", "FSLR", "FE",
    "FISV", "FLEX", "F", "FTNT", "FTV", "FOXA", "FOX", "BEN", "FCX", "GRMN", "IT", "GE", "GEHC", "GEV", "GEN", "GNRC", "GD", "GIS", "GM", "GPC",
    "GILD", "GPN", "GL", "GDDY", "GS", "HAL", "HIG", "HAS", "HCA", "DOC", "HSIC", "HSY", "HPE", "HLT", "HD", "HONA", "HON", "HRL", "HST", "HWM",
    "HPQ", "HUBB", "HUM", "HBAN", "HII", "IBM", "IEX", "IDXX", "ITW", "INCY", "IR", "PODD", "INTC", "IBKR", "ICE", "IFF", "IP", "INTU", "ISRG", "IVZ",
    "INVH", "IQV", "IRM", "JBHT", "JBL", "JKHY", "J", "JNJ", "JCI", "JPM", "KVUE", "KDP", "KEY", "KEYS", "KMB", "KIM", "KMI", "KKR", "KLAC", "KHC",
    "KR", "LHX", "LH", "LRCX", "LVS", "LDOS", "LEN", "LII", "LLY", "LIN", "LYV", "LMT", "L", "LOW", "LULU", "LITE", "LYB", "MTB", "MPC", "MAR",
    "MRSH", "MLM", "MRVL", "MAS", "MA", "MKC", "MCD", "MCK", "MDT", "MRK", "META", "MET", "MTD", "MGM", "MCHP", "MU", "MSFT", "MAA", "MRNA", "TAP",
    "MDLZ", "MPWR", "MNST", "MCO", "MS", "MOS", "MSI", "MSCI", "NDAQ", "NTAP", "NFLX", "NEM", "NWSA", "NWS", "NEE", "NKE", "NI", "NDSN", "NSC", "NTRS",
    "NOC", "NCLH", "NRG", "NUE", "NVDA", "NVR", "NXPI", "ORLY", "OXY", "ODFL", "OMC", "ON", "OKE", "ORCL", "OTIS", "PCAR", "PKG", "PLTR", "PANW", "PSKY",
    "PH", "PAYX", "PYPL", "PNR", "PEP", "PFE", "PCG", "PM", "PSX", "PNW", "PNC", "PPG", "PPL", "PFG", "PG", "PGR", "PLD", "PRU", "PEG", "PTC",
    "PSA", "PHM", "PWR", "QCOM", "DGX", "Q", "RL", "RJF", "RTX", "O", "REG", "REGN", "RF", "RSG", "RMD", "RVTY", "HOOD", "ROK", "ROL", "ROP",
    "ROST", "RCL", "SPGI", "CRM", "SNDK", "SBAC", "SLB", "STX", "SRE", "NOW", "SHW", "SPG", "SWKS", "SJM", "SW", "SNA", "SOLV", "SO", "LUV", "SWK",
    "SBUX", "STT", "STLD", "STE", "SYK", "SMCI", "SYF", "SNPS", "SYY", "TMUS", "TROW", "TTWO", "TPR", "TRGP", "TGT", "TEL", "TDY", "TER", "TSLA", "TXN",
    "TPL", "TXT", "TMO", "TJX", "TKO", "TTD", "TSCO", "TT", "TDG", "TRV", "TRMB", "TFC", "TYL", "TSN", "USB", "UBER", "UDR", "ULTA", "UNP", "UAL",
    "UPS", "URI", "UNH", "UHS", "VLO", "VEEV", "VTR", "VLTO", "VRSN", "VRSK", "VZ", "VRTX", "VRT", "VTRS", "VICI", "V", "VST", "VMC", "WRB", "GWW",
    "WAB", "WMT", "DIS", "WBD", "WM", "WAT", "WEC", "WFC", "WELL", "WST", "WDC", "WY", "WSM", "WMB", "WTW", "WDAY", "WYNN", "XEL", "XYL", "YUM",
    "ZBRA", "ZBH", "ZTS",
]
SP500_NAMES = {
    "MMM": "3M",
    "AOS": "A. O. Smith",
    "ABT": "Abbott Laboratories",
    "ABBV": "AbbVie",
    "ACN": "Accenture",
    "ADBE": "Adobe Inc.",
    "AMD": "Advanced Micro Devices",
    "AES": "AES Corporation",
    "AFL": "Aflac",
    "A": "Agilent Technologies",
    "APD": "Air Products",
    "ABNB": "Airbnb",
    "AKAM": "Akamai Technologies",
    "ALB": "Albemarle Corporation",
    "ARE": "Alexandria Real Estate Equities",
    "ALGN": "Align Technology",
    "ALLE": "Allegion",
    "LNT": "Alliant Energy",
    "ALL": "Allstate",
    "GOOGL": "Alphabet Inc. (Class A)",
    "GOOG": "Alphabet Inc. (Class C)",
    "MO": "Altria",
    "AMZN": "Amazon",
    "AMCR": "Amcor",
    "AEE": "Ameren",
    "AEP": "American Electric Power",
    "AXP": "American Express",
    "AIG": "American International Group",
    "AMT": "American Tower",
    "AWK": "American Water Works",
    "AMP": "Ameriprise Financial",
    "AME": "Ametek",
    "AMGN": "Amgen",
    "APH": "Amphenol",
    "ADI": "Analog Devices",
    "AON": "Aon plc",
    "APA": "APA Corporation",
    "APO": "Apollo Global Management",
    "AAPL": "Apple Inc.",
    "AMAT": "Applied Materials",
    "APP": "AppLovin",
    "APTV": "Aptiv",
    "ACGL": "Arch Capital Group",
    "ADM": "Archer Daniels Midland",
    "ARES": "Ares Management",
    "ANET": "Arista Networks",
    "AJG": "Arthur J. Gallagher & Co.",
    "AIZ": "Assurant",
    "T": "AT&T",
    "ATO": "Atmos Energy",
    "ADSK": "Autodesk",
    "ADP": "Automatic Data Processing",
    "AZO": "AutoZone",
    "AVB": "AvalonBay Communities",
    "AVY": "Avery Dennison",
    "AXON": "Axon Enterprise",
    "BKR": "Baker Hughes",
    "BALL": "Ball Corporation",
    "BAC": "Bank of America",
    "BAX": "Baxter International",
    "BDX": "Becton Dickinson",
    "BRK.B": "Berkshire Hathaway",
    "BBY": "Best Buy",
    "TECH": "Bio-Techne",
    "BIIB": "Biogen",
    "BLK": "BlackRock",
    "BX": "Blackstone Inc.",
    "XYZ": "Block Inc.",
    "BNY": "BNY Mellon",
    "BA": "Boeing",
    "BKNG": "Booking Holdings",
    "BSX": "Boston Scientific",
    "BMY": "Bristol Myers Squibb",
    "AVGO": "Broadcom",
    "BR": "Broadridge Financial Solutions",
    "BRO": "Brown & Brown",
    "BF.B": "Brown-Forman",
    "BLDR": "Builders FirstSource",
    "BG": "Bunge Global",
    "BXP": "BXP Inc.",
    "CHRW": "C.H. Robinson",
    "CDNS": "Cadence Design Systems",
    "CPT": "Camden Property Trust",
    "COF": "Capital One",
    "CAH": "Cardinal Health",
    "CCL": "Carnival Corporation",
    "CARR": "Carrier Global",
    "CVNA": "Carvana",
    "CASY": "Casey's",
    "CAT": "Caterpillar Inc.",
    "CBOE": "Cboe Global Markets",
    "CBRE": "CBRE Group",
    "CDW": "CDW Corporation",
    "COR": "Cencora",
    "CNC": "Centene Corporation",
    "CNP": "CenterPoint Energy",
    "CF": "CF Industries",
    "CRL": "Charles River Laboratories",
    "SCHW": "Charles Schwab Corporation",
    "CHTR": "Charter Communications",
    "CVX": "Chevron Corporation",
    "CMG": "Chipotle Mexican Grill",
    "CB": "Chubb Limited",
    "CHD": "Church & Dwight",
    "CIEN": "Ciena",
    "CI": "Cigna",
    "CINF": "Cincinnati Financial",
    "CTAS": "Cintas",
    "CSCO": "Cisco",
    "C": "Citigroup",
    "CFG": "Citizens Financial Group",
    "CLX": "Clorox",
    "CME": "CME Group",
    "CMS": "CMS Energy",
    "KO": "Coca-Cola Company",
    "CTSH": "Cognizant",
    "COHR": "Coherent Corp.",
    "COIN": "Coinbase",
    "CL": "Colgate-Palmolive",
    "CMCSA": "Comcast",
    "FIX": "Comfort Systems USA",
    "COP": "ConocoPhillips",
    "ED": "Consolidated Edison",
    "STZ": "Constellation Brands",
    "CEG": "Constellation Energy",
    "COO": "Cooper Companies",
    "CPRT": "Copart",
    "GLW": "Corning Inc.",
    "CPAY": "Corpay",
    "CTVA": "Corteva",
    "CSGP": "CoStar Group",
    "COST": "Costco",
    "CRH": "CRH plc",
    "CRWD": "CrowdStrike",
    "CCI": "Crown Castle",
    "CSX": "CSX Corporation",
    "CMI": "Cummins",
    "CVS": "CVS Health",
    "DHR": "Danaher Corporation",
    "DRI": "Darden Restaurants",
    "DDOG": "Datadog",
    "DVA": "DaVita",
    "DECK": "Deckers Brands",
    "DE": "Deere & Company",
    "DELL": "Dell Technologies",
    "DAL": "Delta Air Lines",
    "DVN": "Devon Energy",
    "DXCM": "Dexcom",
    "FANG": "Diamondback Energy",
    "DLR": "Digital Realty",
    "DG": "Dollar General",
    "DLTR": "Dollar Tree",
    "D": "Dominion Energy",
    "DPZ": "Domino's",
    "DASH": "DoorDash",
    "DOV": "Dover Corporation",
    "DOW": "Dow Inc.",
    "DHI": "D.R. Horton",
    "DTE": "DTE Energy",
    "DUK": "Duke Energy",
    "DD": "DuPont",
    "ETN": "Eaton Corporation",
    "EBAY": "eBay Inc.",
    "ECHO": "EchoStar",
    "ECL": "Ecolab",
    "EIX": "Edison International",
    "EW": "Edwards Lifesciences",
    "EA": "Electronic Arts",
    "ELV": "Elevance Health",
    "EME": "Emcor",
    "EMR": "Emerson Electric",
    "ETR": "Entergy",
    "EOG": "EOG Resources",
    "EQT": "EQT Corporation",
    "EFX": "Equifax",
    "EQIX": "Equinix",
    "EQR": "Equity Residential",
    "ERIE": "Erie Indemnity",
    "ESS": "Essex Property Trust",
    "EL": "Estee Lauder Companies",
    "EG": "Everest Group",
    "EVRG": "Evergy",
    "ES": "Eversource Energy",
    "EXC": "Exelon",
    "EXE": "Expand Energy",
    "EXPE": "Expedia Group",
    "EXPD": "Expeditors International",
    "EXR": "Extra Space Storage",
    "XOM": "ExxonMobil",
    "FFIV": "F5 Inc.",
    "FDS": "FactSet",
    "FICO": "Fair Isaac",
    "FAST": "Fastenal",
    "FRT": "Federal Realty Investment Trust",
    "FDX": "FedEx",
    "FDXF": "FedEx Freight",
    "FIS": "Fidelity National Information Services",
    "FITB": "Fifth Third Bancorp",
    "FSLR": "First Solar",
    "FE": "FirstEnergy",
    "FISV": "Fiserv",
    "FLEX": "Flex Ltd.",
    "F": "Ford Motor Company",
    "FTNT": "Fortinet",
    "FTV": "Fortive",
    "FOXA": "Fox Corporation (Class A)",
    "FOX": "Fox Corporation (Class B)",
    "BEN": "Franklin Resources",
    "FCX": "Freeport-McMoRan",
    "GRMN": "Garmin",
    "IT": "Gartner",
    "GE": "GE Aerospace",
    "GEHC": "GE HealthCare",
    "GEV": "GE Vernova",
    "GEN": "Gen Digital",
    "GNRC": "Generac",
    "GD": "General Dynamics",
    "GIS": "General Mills",
    "GM": "General Motors",
    "GPC": "Genuine Parts Company",
    "GILD": "Gilead Sciences",
    "GPN": "Global Payments",
    "GL": "Globe Life",
    "GDDY": "GoDaddy",
    "GS": "Goldman Sachs",
    "HAL": "Halliburton",
    "HIG": "Hartford",
    "HAS": "Hasbro",
    "HCA": "HCA Healthcare",
    "DOC": "Healthpeak Properties",
    "HSIC": "Henry Schein",
    "HSY": "Hershey Company",
    "HPE": "Hewlett Packard Enterprise",
    "HLT": "Hilton Worldwide",
    "HD": "Home Depot",
    "HONA": "Honeywell Aerospace",
    "HON": "Honeywell Technologies",
    "HRL": "Hormel Foods",
    "HST": "Host Hotels & Resorts",
    "HWM": "Howmet Aerospace",
    "HPQ": "HP Inc.",
    "HUBB": "Hubbell Incorporated",
    "HUM": "Humana",
    "HBAN": "Huntington Bancshares",
    "HII": "Huntington Ingalls Industries",
    "IBM": "IBM",
    "IEX": "IDEX Corporation",
    "IDXX": "Idexx Laboratories",
    "ITW": "Illinois Tool Works",
    "INCY": "Incyte",
    "IR": "Ingersoll Rand",
    "PODD": "Insulet Corporation",
    "INTC": "Intel",
    "IBKR": "Interactive Brokers",
    "ICE": "Intercontinental Exchange",
    "IFF": "International Flavors & Fragrances",
    "IP": "International Paper",
    "INTU": "Intuit",
    "ISRG": "Intuitive Surgical",
    "IVZ": "Invesco",
    "INVH": "Invitation Homes",
    "IQV": "IQVIA",
    "IRM": "Iron Mountain",
    "JBHT": "J.B. Hunt",
    "JBL": "Jabil",
    "JKHY": "Jack Henry & Associates",
    "J": "Jacobs Solutions",
    "JNJ": "Johnson & Johnson",
    "JCI": "Johnson Controls",
    "JPM": "JPMorgan Chase",
    "KVUE": "Kenvue",
    "KDP": "Keurig Dr Pepper",
    "KEY": "KeyCorp",
    "KEYS": "Keysight Technologies",
    "KMB": "Kimberly-Clark",
    "KIM": "Kimco Realty",
    "KMI": "Kinder Morgan",
    "KKR": "KKR & Co.",
    "KLAC": "KLA Corporation",
    "KHC": "Kraft Heinz",
    "KR": "Kroger",
    "LHX": "L3Harris",
    "LH": "Labcorp",
    "LRCX": "Lam Research",
    "LVS": "Las Vegas Sands",
    "LDOS": "Leidos",
    "LEN": "Lennar",
    "LII": "Lennox International",
    "LLY": "Lilly (Eli)",
    "LIN": "Linde plc",
    "LYV": "Live Nation Entertainment",
    "LMT": "Lockheed Martin",
    "L": "Loews Corporation",
    "LOW": "Lowe's",
    "LULU": "Lululemon Athletica",
    "LITE": "Lumentum",
    "LYB": "LyondellBasell",
    "MTB": "M&T Bank",
    "MPC": "Marathon Petroleum",
    "MAR": "Marriott International",
    "MRSH": "Marsh McLennan",
    "MLM": "Martin Marietta Materials",
    "MRVL": "Marvell Technology",
    "MAS": "Masco",
    "MA": "Mastercard",
    "MKC": "McCormick & Company",
    "MCD": "McDonald's",
    "MCK": "McKesson Corporation",
    "MDT": "Medtronic",
    "MRK": "Merck & Co.",
    "META": "Meta Platforms",
    "MET": "MetLife",
    "MTD": "Mettler Toledo",
    "MGM": "MGM Resorts",
    "MCHP": "Microchip Technology",
    "MU": "Micron Technology",
    "MSFT": "Microsoft",
    "MAA": "Mid-America Apartment Communities",
    "MRNA": "Moderna",
    "TAP": "Molson Coors Beverage Company",
    "MDLZ": "Mondelez International",
    "MPWR": "Monolithic Power Systems",
    "MNST": "Monster Beverage",
    "MCO": "Moody's Corporation",
    "MS": "Morgan Stanley",
    "MOS": "Mosaic Company",
    "MSI": "Motorola Solutions",
    "MSCI": "MSCI Inc.",
    "NDAQ": "Nasdaq Inc.",
    "NTAP": "NetApp",
    "NFLX": "Netflix",
    "NEM": "Newmont",
    "NWSA": "News Corp (Class A)",
    "NWS": "News Corp (Class B)",
    "NEE": "NextEra Energy",
    "NKE": "Nike Inc.",
    "NI": "NiSource",
    "NDSN": "Nordson Corporation",
    "NSC": "Norfolk Southern",
    "NTRS": "Northern Trust",
    "NOC": "Northrop Grumman",
    "NCLH": "Norwegian Cruise Line Holdings",
    "NRG": "NRG Energy",
    "NUE": "Nucor",
    "NVDA": "Nvidia",
    "NVR": "NVR Inc.",
    "NXPI": "NXP Semiconductors",
    "ORLY": "O'Reilly Automotive",
    "OXY": "Occidental Petroleum",
    "ODFL": "Old Dominion",
    "OMC": "Omnicom Group",
    "ON": "ON Semiconductor",
    "OKE": "Oneok",
    "ORCL": "Oracle Corporation",
    "OTIS": "Otis Worldwide",
    "PCAR": "Paccar",
    "PKG": "Packaging Corporation of America",
    "PLTR": "Palantir Technologies",
    "PANW": "Palo Alto Networks",
    "PSKY": "Paramount Skydance Corporation",
    "PH": "Parker Hannifin",
    "PAYX": "Paychex",
    "PYPL": "PayPal",
    "PNR": "Pentair",
    "PEP": "PepsiCo",
    "PFE": "Pfizer",
    "PCG": "PG&E Corporation",
    "PM": "Philip Morris International",
    "PSX": "Phillips 66",
    "PNW": "Pinnacle West Capital",
    "PNC": "PNC Financial Services",
    "PPG": "PPG Industries",
    "PPL": "PPL Corporation",
    "PFG": "Principal Financial Group",
    "PG": "Procter & Gamble",
    "PGR": "Progressive Corporation",
    "PLD": "Prologis",
    "PRU": "Prudential Financial",
    "PEG": "Public Service Enterprise Group",
    "PTC": "PTC Inc.",
    "PSA": "Public Storage",
    "PHM": "PulteGroup",
    "PWR": "Quanta Services",
    "QCOM": "Qualcomm",
    "DGX": "Quest Diagnostics",
    "Q": "Qnity Electronics",
    "RL": "Ralph Lauren Corporation",
    "RJF": "Raymond James Financial",
    "RTX": "RTX Corporation",
    "O": "Realty Income",
    "REG": "Regency Centers",
    "REGN": "Regeneron Pharmaceuticals",
    "RF": "Regions Financial Corporation",
    "RSG": "Republic Services",
    "RMD": "ResMed",
    "RVTY": "Revvity",
    "HOOD": "Robinhood Markets",
    "ROK": "Rockwell Automation",
    "ROL": "Rollins Inc.",
    "ROP": "Roper Technologies",
    "ROST": "Ross Stores",
    "RCL": "Royal Caribbean Group",
    "SPGI": "S&P Global",
    "CRM": "Salesforce",
    "SNDK": "Sandisk",
    "SBAC": "SBA Communications",
    "SLB": "Schlumberger",
    "STX": "Seagate Technology",
    "SRE": "Sempra",
    "NOW": "ServiceNow",
    "SHW": "Sherwin-Williams",
    "SPG": "Simon Property Group",
    "SWKS": "Skyworks Solutions",
    "SJM": "J.M. Smucker Company",
    "SW": "Smurfit Westrock",
    "SNA": "Snap-on",
    "SOLV": "Solventum",
    "SO": "Southern Company",
    "LUV": "Southwest Airlines",
    "SWK": "Stanley Black & Decker",
    "SBUX": "Starbucks",
    "STT": "State Street Corporation",
    "STLD": "Steel Dynamics",
    "STE": "Steris",
    "SYK": "Stryker Corporation",
    "SMCI": "Supermicro",
    "SYF": "Synchrony Financial",
    "SNPS": "Synopsys",
    "SYY": "Sysco",
    "TMUS": "T-Mobile US",
    "TROW": "T. Rowe Price",
    "TTWO": "Take-Two Interactive",
    "TPR": "Tapestry Inc.",
    "TRGP": "Targa Resources",
    "TGT": "Target Corporation",
    "TEL": "TE Connectivity",
    "TDY": "Teledyne Technologies",
    "TER": "Teradyne",
    "TSLA": "Tesla Inc.",
    "TXN": "Texas Instruments",
    "TPL": "Texas Pacific Land Corporation",
    "TXT": "Textron",
    "TMO": "Thermo Fisher Scientific",
    "TJX": "TJX Companies",
    "TKO": "TKO Group Holdings",
    "TTD": "Trade Desk",
    "TSCO": "Tractor Supply",
    "TT": "Trane Technologies",
    "TDG": "TransDigm Group",
    "TRV": "Travelers Companies",
    "TRMB": "Trimble Inc.",
    "TFC": "Truist Financial",
    "TYL": "Tyler Technologies",
    "TSN": "Tyson Foods",
    "USB": "U.S. Bancorp",
    "UBER": "Uber",
    "UDR": "UDR Inc.",
    "ULTA": "Ulta Beauty",
    "UNP": "Union Pacific Corporation",
    "UAL": "United Airlines Holdings",
    "UPS": "United Parcel Service",
    "URI": "United Rentals",
    "UNH": "UnitedHealth Group",
    "UHS": "Universal Health Services",
    "VLO": "Valero Energy",
    "VEEV": "Veeva Systems",
    "VTR": "Ventas",
    "VLTO": "Veralto",
    "VRSN": "Verisign",
    "VRSK": "Verisk Analytics",
    "VZ": "Verizon",
    "VRTX": "Vertex Pharmaceuticals",
    "VRT": "Vertiv",
    "VTRS": "Viatris",
    "VICI": "Vici Properties",
    "V": "Visa Inc.",
    "VST": "Vistra Corp.",
    "VMC": "Vulcan Materials Company",
    "WRB": "W.R. Berkley Corporation",
    "GWW": "W.W. Grainger",
    "WAB": "Wabtec",
    "WMT": "Walmart",
    "DIS": "Walt Disney Company",
    "WBD": "Warner Bros. Discovery",
    "WM": "Waste Management",
    "WAT": "Waters Corporation",
    "WEC": "WEC Energy Group",
    "WFC": "Wells Fargo",
    "WELL": "Welltower",
    "WST": "West Pharmaceutical Services",
    "WDC": "Western Digital",
    "WY": "Weyerhaeuser",
    "WSM": "Williams-Sonoma Inc.",
    "WMB": "Williams Companies",
    "WTW": "Willis Towers Watson",
    "WDAY": "Workday Inc.",
    "WYNN": "Wynn Resorts",
    "XEL": "Xcel Energy",
    "XYL": "Xylem Inc.",
    "YUM": "Yum! Brands",
    "ZBRA": "Zebra Technologies",
    "ZBH": "Zimmer Biomet",
    "ZTS": "Zoetis",
}
NASDAQ100_LIST = [
    "ADBE", "ADP", "AMD", "ABNB", "ALNY", "GOOGL", "GOOG", "AMZN", "AEP", "AMGN", "ADI", "AAPL", "AMAT", "APP", "ARM", "ASML", "TEAM", "ADSK", "AXON", "BKR",
    "BKNG", "AVGO", "CDNS", "CHTR", "CTAS", "CSCO", "CCEP", "CTSH", "CMCSA", "CEG", "CPRT", "CSGP", "COST", "CRWD", "CSX", "DDOG", "DXCM", "FANG", "DASH", "EA",
    "EXC", "FAST", "FER", "FTNT", "GEHC", "GILD", "HON", "IDXX", "INSM", "INTC", "INTU", "ISRG", "KDP", "KLAC", "KHC", "LRCX", "LIN", "MAR", "MRVL", "MELI",
    "META", "MCHP", "MU", "MSFT", "MSTR", "MDLZ", "MPWR", "MNST", "NFLX", "NVDA", "NXPI", "ODFL", "ORLY", "PCAR", "PLTR", "PANW", "PAYX", "PYPL", "PDD", "PEP",
    "QCOM", "REGN", "ROP", "ROST", "STX", "SHOP", "SBUX", "SNPS", "TTWO", "TSLA", "TXN", "TRI", "TMUS", "VRSK", "VRTX", "WMT", "WBD", "WDC", "WDAY", "XEL",
    "ZS",
]
SOX_LIST = [
    "AMD", "ADI", "AMAT", "ARM", "ASML", "ALAB", "AVGO", "COHR", "CRDO", "ENTG", "GFS", "INTC", "KLAC", "LRCX", "MTSI", "MRVL", "MCHP", "MU", "MPWR", "NVDA",
    "NXPI", "ON", "QCOM", "QRVO", "SWKS", "TSM", "TER", "LSCC", "RMBS", "ACLS",
]
MY_LIST_DEFAULT = [
    "AXTI", "LITE", "COHR", "RCAT", "LWLG", "UMC", "AMKR", "AEHR", "ON", "SMR", "IREN", "HIMX", "TSEM", "CRDO", "PLTR", "BABA", "HOOD", "ALAB", "NVDA", "MU",
    "FTNT", "AEX", "OXY", "MRVL", "QCOM", "XYZ", "RKLB", "FN", "ORCL", "AVGO", "BE", "CRWV", "AMD", "SHOP", "VZ", "OWL", "TER", "GOOGL", "SMCI", "QBTS",
    "VRT", "TSM", "SNDK", "ONDS", "RCAT", "NBIS", "POET", "TSEM", "GLW", "DELL", "SPCX", "ON", "SMTC",
]
# 選股型指定組合（S編號）：2023-10～2026-09 美股三年回測（全美股、股價≥10美元、成交金額≥1000萬美元，分六段），三年依日 t 值都 ≥2、六個半年至少五個為正的組合
# （2026-09-30 改版；舊版 S1～S7 夜星類三年都沒有選股效果，夜星改列高標股 M）
STOCK_PICK_COMBOS = [
    ["近1季財報EPS大幅優於預期(≥10%)", "中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)"],
    ["近20日強於大盤≥10%", "中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)"],
    ["近1季財報EPS大幅優於預期(≥10%)", "中期強勢(12-1月勝大盤≥20%)", "布林通道收窄(寬度近半年最低20%)"],
    ["量能斜率轉弱(近5日均量<近10日均量20%以上)", "中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)"],
    ["近20日強於大盤≥10%", "大盤站上20日均線", "創52週新高"],

]
STOCK_PICK_STATS = {
    "近1季財報EPS大幅優於預期(≥10%) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1)": {"10": {"n": 29516, "win": 55.6, "ret": 1.35, "ex": 0.81, "t": 16.0}, "20": {"n": 29516, "win": 56.3, "ret": 2.65, "ex": 1.36, "t": 18.35}},
    "近20日強於大盤≥10% ＋ 中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1)": {"10": {"n": 23286, "win": 53.4, "ret": 1.21, "ex": 0.79, "t": 12.11}, "20": {"n": 23286, "win": 54.2, "ret": 2.36, "ex": 1.49, "t": 15.93}},
    "近1季財報EPS大幅優於預期(≥10%) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 布林通道收窄(寬度近半年最低20%)": {"10": {"n": 18896, "win": 53.3, "ret": 1.07, "ex": 0.45, "t": 7.41}, "20": {"n": 18896, "win": 54.6, "ret": 2.23, "ex": 1.06, "t": 11.75}},
    "量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1)": {"10": {"n": 7437, "win": 54.5, "ret": 1.2, "ex": 0.63, "t": 6.62}, "20": {"n": 7437, "win": 56.6, "ret": 2.47, "ex": 1.26, "t": 9.02}},
    "近20日強於大盤≥10% ＋ 大盤站上20日均線 ＋ 創52週新高": {"10": {"n": 17466, "win": 52.6, "ret": 0.74, "ex": 0.59, "t": 9.44}, "20": {"n": 17466, "win": 51.9, "ret": 1.38, "ex": 1.1, "t": 12.02}},

}
# 2026-09-27 美股回測（勝率榜5/10/20日＋飆股榜5/10/20日）中，指定組合的勝率／t值(同日調整)／飆股比例記錄值
# key＝條件依字元排序後以「 ＋ 」連接
COMBO_REF_STATS = {
    # ── 2026-10-05 三年六段回測（2023-10～2026-09，股價≥10、成交金額≥1000萬美元，約1660檔）：S1～S5 改為「中期強勢／財報優於預期／CMF資金流」系列，記錄值已更新 ──
    "大盤站上20日均線 ＋ 距52週高點≤5% ＋ 近3日向上跳空缺口": {"win": {"5": 52.7, "10": 54.3, "20": 53.7}, "t": {"5": 6.44, "10": 11.5, "20": 11.57}, "moon": {"10": 0.5, "20": 1.7}},
    "相對強弱為正(強於大盤) ＋ 近3日向上跳空缺口 ＋ 量能區間低檔(≤10百分位)": {"win": {"5": 54.4, "10": 54.7, "20": 55.7}, "t": {"5": 2.63, "10": 3.45, "20": 4.79}, "moon": {"10": 0.5, "20": 2.0}},
    "KDJ近3日內死亡交叉 ＋ 距52週高點≤5% ＋ 近3日向上跳空缺口": {"win": {"5": 52.6, "10": 52.7, "20": 54.2}, "t": {"5": 1.29, "10": 4.34, "20": 5.88}, "moon": {"10": 0.4, "20": 1.3}},
    "爆量(≥1.5倍均量) ＋ 距52週高點≤5% ＋ 近3日向上跳空缺口": {"win": {"5": 52.5, "10": 54.1, "20": 54.7}, "t": {"5": 0.38, "10": 3.4, "20": 5.27}, "moon": {"10": 1.0, "20": 2.5}},
    "大盤站上20日均線 ＋ 強勢突破盤 ＋ 距52週高點≤5%": {"win": {"5": 50.7, "10": 53.7, "20": 52.8}, "t": {"5": 2.85, "10": 10.29, "20": 10.64}, "moon": {"10": 0.5, "20": 1.7}},
    "夜星剛形成 ＋ 大盤跌破60日均線": {"win": {"5": 72.4, "10": 67.2, "20": 67.2}, "t": {"5": 2.54, "10": 2.86, "20": 4.26}, "moon": {"20": 17.2}},
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成 ＋ 大盤跌破60日均線": {"win": {"5": 59.1, "10": 54.8, "20": 61.3}, "t": {"5": 0.79, "10": 2.12, "20": 3.77}, "moon": {"10": 4.8, "20": 15.7}},
    "地量(≤0.5倍均量) ＋ 多方力道≥80 ＋ 量能區間高檔(≥90百分位)": {"win": {"5": 40.0, "10": 42.1, "20": 42.3}, "t": {"5": -4.31, "10": -4.37, "20": -3.68}, "moon": {"10": 7.0, "20": 12.1}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"win": {"5": 62.1, "10": 66.1, "20": 63.9}, "t": {"5": 7.76, "10": 9.62, "20": 10.81}, "moon": {"10": 1.7, "20": 4.7}},
    "KDJ近3日內死亡交叉 ＋ 地量(≤0.5倍均量) ＋ 量能區間低檔(≤10百分位)": {"win": {"20": 61.8}, "t": {"20": 3.48}},
    "KDJ近3日內死亡交叉 ＋ 型態成形中 ＋ 夜星剛形成": {"win": {"5": 65.5, "10": 60.2}, "t": {"5": 3.51, "10": 2.74}, "moon": {"20": 10.6}},
    "KDJ近3日內死亡交叉 ＋ 型態突破確認 ＋ 夜星剛形成": {"moon": {"10": 10.7, "20": 14.3}},
    "KDJ近3日內死亡交叉 ＋ 多方力道≥65 ＋ 夜星剛形成": {"moon": {"20": 13.6}},
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成": {"win": {"5": 64.6, "10": 60.5, "20": 63.3}, "t": {"5": 3.82, "10": 3.33, "20": 3.96}, "moon": {"20": 12.9}},
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成 ＋ 大盤站上20日均線": {"win": {"20": 62.4}, "t": {"20": 2.63}, "moon": {"20": 14.1}},
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成 ＋ 大盤站上60日均線": {"win": {"5": 61.0, "20": 63.8}, "t": {"5": 3.51, "20": 2.91}, "moon": {"20": 13.3}},
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成 ＋ 相對強弱為正(強於大盤)": {"win": {"5": 64.9, "20": 61.9}, "t": {"5": 3.17, "20": 3.63}, "moon": {"20": 15.5}},
    "KDJ近3日內黃金交叉 ＋ 夜星剛形成 ＋ 大盤站上20日均線": {"moon": {"20": 11.4}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成": {"win": {"5": 68.2, "10": 72.8, "20": 63.6}, "t": {"5": 1.89, "10": 2.9, "20": 2.41}},
    "KDJ近3日內黃金交叉 ＋ 布林通道低檔(≤20%) ＋ 母子懷抱(高檔)剛形成": {"win": {"10": 76.9, "20": 75.4}, "t": {"10": 3.03, "20": 3.13}, "moon": {"20": 16.9}},
    "KDJ近3日內黃金交叉 ＋ 母子懷抱(高檔)剛形成": {"win": {"10": 64.2, "20": 61.5}, "t": {"10": 3.64, "20": 2.91}},
    "K線橫盤的突破剛形成 ＋ 大盤站上20日均線 ＋ 突破飆股大量黑K最高點剛形成": {"win": {"20": 60.4}, "t": {"20": 1.38}},
    "K線橫盤的突破剛形成 ＋ 大盤跌破20日均線 ＋ 突破ABC修正下降切線剛形成": {"win": {"5": 60.7, "10": 62.5, "20": 67.9}, "t": {"5": 1.15, "10": 1.4, "20": 1.6}},
    "K線橫盤的突破剛形成 ＋ 大盤跌破60日均線 ＋ 相對強弱為負(弱於大盤)": {"win": {"20": 72.8}, "t": {"20": 0.87}},
    "MACD近3日內黃金交叉 ＋ 多方力道≥80 ＋ 突破ABC修正下降切線剛形成": {"win": {"20": 74.1}, "t": {"20": 1.98}},
    "N字底剛形成 ＋ 多方力道≥65 ＋ 爆量(≥1.5倍均量)": {"win": {"10": 60.2, "20": 60.8}, "t": {"10": 1.92, "20": 3.42}},
    "N字底剛形成 ＋ 爆量(≥1.5倍均量) ＋ 相對強弱為正(強於大盤)": {"win": {"20": 61.3}, "t": {"20": 3.35}},
    "型態成形中 ＋ 夜星剛形成 ＋ 大盤站上60日均線": {"win": {"5": 61.2, "10": 60.2}, "t": {"5": 3.67, "10": 3.01}, "moon": {"20": 11.7}},
    "型態成形中 ＋ 夜星剛形成 ＋ 大盤跌破20日均線": {"win": {"5": 73.4, "10": 70.3, "20": 67.2}, "t": {"5": 3.74, "10": 3.28, "20": 2.71}, "moon": {"20": 14.1}},
    "型態成形中 ＋ 夜星剛形成 ＋ 大盤跌破60日均線": {"moon": {"20": 15.4}},
    "型態成形中 ＋ 夜星剛形成 ＋ 爆量(≥1.5倍均量)": {"moon": {"20": 13.2}},
    "型態突破確認 ＋ 夜星剛形成 ＋ 大盤站上60日均線": {"moon": {"10": 11.1, "20": 14.8}},
    "多方力道≥65 ＋ 爆量(≥2倍均量) ＋ 突破ABC修正下降切線剛形成": {"win": {"10": 61.6}, "t": {"10": 1.08}},
    "多方力道≥80 ＋ 爆量(≥1.5倍均量) ＋ 突破ABC修正下降切線剛形成": {"win": {"20": 63.0}, "t": {"20": 1.21}},
    "多方力道≥80 ＋ 爆量(≥2倍均量) ＋ 突破飆股大量黑K最高點剛形成": {"win": {"20": 64.5}, "t": {"20": 2.11}},
    "夜星剛形成": {"win": {"5": 64.0, "10": 61.5, "20": 62.0}, "t": {"5": 4.8, "10": 4.14, "20": 4.48}, "moon": {"20": 13.5}},
    "夜星剛形成 ＋ 大盤站上20日均線": {"moon": {"20": 11.4}},
    "夜星剛形成 ＋ 大盤站上20日均線 ＋ 大盤站上60日均線": {"moon": {"20": 11.6}},
    "夜星剛形成 ＋ 大盤站上20日均線 ＋ 相對強弱為正(強於大盤)": {"moon": {"20": 13.9}},
    "夜星剛形成 ＋ 大盤站上60日均線": {"win": {"5": 60.6}, "t": {"5": 4.17}, "moon": {"20": 12.0}},
    "夜星剛形成 ＋ 大盤站上60日均線 ＋ 相對強弱為正(強於大盤)": {"moon": {"20": 14.0}},
    "夜星剛形成 ＋ 大盤跌破20日均線": {"win": {"5": 70.9, "10": 66.3, "20": 66.3}, "t": {"5": 3.72, "10": 3.62, "20": 3.8}, "moon": {"20": 16.3}},
    "夜星剛形成 ＋ 大盤跌破20日均線 ＋ 大盤跌破60日均線": {"win": {"5": 73.2, "10": 67.9, "20": 67.9}, "t": {"5": 2.58, "10": 3.01, "20": 4.27}, "moon": {"20": 17.9}},
    "夜星剛形成 ＋ 爆量(≥1.5倍均量)": {"moon": {"20": 13.0}},
    "夜星剛形成 ＋ 相對強弱為正(強於大盤)": {"win": {"5": 63.3, "10": 60.4, "20": 60.4}, "t": {"5": 4.12, "10": 3.58, "20": 3.91}, "moon": {"20": 15.8}},
    "夜星剛形成 ＋ 相對強弱為負(弱於大盤)": {"win": {"5": 65.6, "10": 63.9, "20": 65.6}, "t": {"5": 2.52, "10": 2.07, "20": 2.2}},
    "大盤站上20日均線 ＋ 大盤跌破60日均線 ＋ 爆量(≥2倍均量)": {"win": {"10": 61.8, "20": 74.3}, "t": {"10": 2.68, "20": 2.54}},
    "大盤站上20日均線 ＋ 大盤跌破60日均線 ＋ 突破ABC修正下降切線剛形成": {"win": {"20": 67.8}, "t": {"20": 0.97}},
    "大盤站上60日均線 ＋ 布林通道低檔(≤20%) ＋ 母子懷抱(高檔)剛形成": {"win": {"5": 69.2, "10": 61.5, "20": 61.5}, "t": {"5": 2.34, "10": 1.06, "20": 1.86}},
    "大盤跌破20日均線 ＋ 大盤跌破60日均線 ＋ 突破飆股大量黑K最高點剛形成": {"win": {"10": 67.5}, "t": {"10": 0.81}},
    "大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成 ＋ 相對強弱為正(強於大盤)": {"win": {"10": 65.9, "20": 64.5}, "t": {"10": 1.78, "20": 2.68}},
    "大盤跌破60日均線 ＋ 相對強弱為正(強於大盤) ＋ 突破飆股大量黑K最高點剛形成": {"win": {"10": 66.4}, "t": {"10": 0.93}},
    "布林通道低檔(≤20%) ＋ 爆量(≥1.5倍均量) ＋ 相對強弱為正(強於大盤)": {"win": {"5": 68.9, "10": 68.1, "20": 69.7}, "t": {"5": 2.76, "10": 2.52, "20": -0.0}},
    "布林通道低檔(≤20%) ＋ 爆量(≥2倍均量) ＋ 相對強弱為正(強於大盤)": {"win": {"5": 65.2, "10": 64.8, "20": 68.4}, "t": {"5": 1.73, "10": 1.32, "20": -1.08}},
    "布林通道收窄(寬度近半年最低20%) ＋ 近1季乖離度為負(股價超前營收) ＋ 近3日向上跳空缺口": {"win": {"5": 54.5, "10": 55.0, "20": 56.1}, "t": {"5": 4.84, "10": 6.38, "20": 5.84}, "moon": {"10": 0.5, "20": 1.6}},
    "布林通道收窄(寬度近半年最低20%) ＋ 近1季均價YoY為正 ＋ 近3日向上跳空缺口": {"win": {"5": 54.4, "10": 54.9, "20": 56.4}, "t": {"5": 5.28, "10": 6.19, "20": 6.63}, "moon": {"10": 0.5, "20": 1.7}},
    "KDJ近3日內黃金交叉 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 近3日向上跳空缺口": {"win": {"5": 54.7, "10": 55.1, "20": 55.4}, "t": {"5": 4.95, "10": 4.53, "20": 5.13}, "moon": {"10": 0.6, "20": 2.0}},
    "布林通道收窄(寬度近半年最低20%) ＋ 近3日向上跳空缺口": {"win": {"5": 53.8, "10": 54.1, "20": 55.6}, "t": {"5": 4.33, "10": 4.89, "20": 6.56}, "moon": {"10": 0.6, "20": 1.9}},
    "多方力道≥65 ＋ 距52週高點≤5% ＋ 近3日向上跳空缺口": {"win": {"5": 52.1, "10": 53.5, "20": 53.1}, "t": {"5": 3.76, "10": 6.05, "20": 8.04}, "moon": {"10": 0.5, "20": 1.5}},
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 近1季財報EPS大幅優於預期(≥10%)": {"win": {"5": 54.5, "10": 55.6, "20": 56.3}, "t": {"5": 12.28, "10": 16.0, "20": 18.35}, "moon": {"10": 1.6, "20": 4.6}},
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 近20日強於大盤≥10%": {"win": {"5": 53.5, "10": 53.4, "20": 54.2}, "t": {"5": 8.44, "10": 12.11, "20": 15.93}, "moon": {"10": 2.5, "20": 6.2}},
    "中期強勢(12-1月勝大盤≥20%) ＋ 布林通道收窄(寬度近半年最低20%) ＋ 近1季財報EPS大幅優於預期(≥10%)": {"win": {"5": 53.0, "10": 53.3, "20": 54.6}, "t": {"5": 6.22, "10": 7.41, "20": 11.75}, "moon": {"10": 1.3, "20": 3.7}},
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上)": {"win": {"5": 53.7, "10": 54.5, "20": 56.6}, "t": {"5": 4.35, "10": 6.62, "20": 9.02}, "moon": {"10": 1.3, "20": 3.5}},
    "創52週新高 ＋ 大盤站上20日均線 ＋ 近20日強於大盤≥10%": {"win": {"5": 50.6, "10": 52.6, "20": 51.9}, "t": {"5": 2.25, "10": 9.44, "20": 12.02}, "moon": {"10": 1.2, "20": 3.7}},

}
# 高勝率指定組合（#編號）與歷史勝率記錄（2026-09-21 美股回測）
PINNED_COMBOS = [
    ["相對強弱為負(弱於大盤)", "近1季均價YoY為正", "夜星剛形成"],
    ["爆量(≥2倍均量)", "大盤站上20日均線", "大盤跌破60日均線"],
    ["大盤跌破60日均線", "型態剛形成(剛突破)", "夜星剛形成"],
    ["大盤跌破60日均線", "夜星剛形成"],
    ["大盤跌破60日均線", "型態成形中", "夜星剛形成"],
    ["大盤跌破60日均線", "型態突破確認", "夜星剛形成"],
    ["KDJ近3日內死亡交叉", "近1季均價YoY為正", "夜星剛形成"],
    ["大盤跌破20日均線", "大盤跌破60日均線", "夜星剛形成"],
    ["KDJ近3日內黃金交叉", "大盤跌破60日均線", "母子懷抱(低檔)剛形成"],
    ["相對強弱為正(強於大盤)", "大盤跌破60日均線", "突破飆股大量黑K最高點剛形成"],
    ["布林通道低檔(≤20%)", "相對強弱為正(強於大盤)", "爆量(≥2倍均量)"],
    ["大盤跌破20日均線", "型態剛形成(剛突破)", "夜星剛形成"],
    ["大盤跌破20日均線", "型態突破確認", "夜星剛形成"],
    ["大盤跌破20日均線", "型態成形中", "夜星剛形成"],
    ["大盤跌破20日均線", "夜星剛形成"],
    ["相對強弱為正(強於大盤)", "大盤跌破60日均線", "母子懷抱(低檔)剛形成"],
    ["相對強弱為負(弱於大盤)", "型態剛形成(剛突破)", "夜星剛形成"],
    ["相對強弱為負(弱於大盤)", "型態成形中", "夜星剛形成"],
    ["相對強弱為負(弱於大盤)", "型態突破確認", "夜星剛形成"],
    ["相對強弱為負(弱於大盤)", "夜星剛形成"],
    ["大盤跌破20日均線", "大盤跌破60日均線", "突破飆股大量黑K最高點剛形成"],
    ["近1季乖離度為負(股價超前營收)", "近1季均價YoY為正", "夜星剛形成"],
    ["大盤跌破60日均線", "型態突破確認", "突破飆股大量黑K最高點剛形成"],
    ["多方力道≥80", "相對強弱為負(弱於大盤)", "近1季乖離度為正(營收優於股價)"],
    ["大盤站上20日均線", "大盤跌破60日均線", "突破ABC修正下降切線剛形成"],
    ["大盤站上60日均線", "型態剛形成(剛突破)", "夜星剛形成"],
    ["大盤站上60日均線", "夜星剛形成"],
    ["大盤站上60日均線", "型態成形中", "夜星剛形成"],
    ["大盤站上60日均線", "型態突破確認", "夜星剛形成"],
    ["近1季乖離度為正(營收優於股價)", "近1季均價YoY為正", "複式頭肩底剛形成"],
    ["KDJ近3日內死亡交叉", "型態剛形成(剛突破)", "夜星剛形成"],
    ["KDJ近3日內死亡交叉", "夜星剛形成"],
    ["KDJ近3日內死亡交叉", "型態成形中", "夜星剛形成"],
    ["KDJ近3日內死亡交叉", "型態突破確認", "夜星剛形成"],
    ["大盤站上20日均線", "突破飆股大量黑K最高點剛形成", "K線橫盤的突破剛形成"],
    ["布林通道低檔(≤20%)", "相對強弱為正(強於大盤)", "爆量(≥1.5倍均量)"],
    ["大盤站上20日均線", "夜星剛形成"],
    ["相對強弱為負(弱於大盤)", "大盤跌破60日均線", "K線橫盤的突破剛形成"],
    ["多方力道≥65", "強勢突破盤", "頭肩底剛形成"],
    ["相對強弱為正(強於大盤)", "大盤跌破20日均線", "複式頭肩底剛形成"],
    ["大盤跌破20日均線", "複式頭肩底剛形成"],
    # 2026-09-30 三年回測：大盤跌破季線時出現KDJ黃金交叉＋跳空上漲（擇時型，三段 t 都 ≥2；台股同型訊號也三年有效）
    ["KDJ近3日內黃金交叉", "大盤跌破60日均線", "近3日向上跳空缺口"],
]
PINNED_COMBO_WINRATES = {
    "相對強弱為負(弱於大盤) ＋ 近1季均價YoY為正 ＋ 夜星剛形成": {"5": 79.2, "10": 79.2, "20": 71.7},
    "爆量(≥2倍均量) ＋ 大盤站上20日均線 ＋ 大盤跌破60日均線": {"20": 78.4},
    "大盤跌破60日均線 ＋ 型態剛形成(剛突破) ＋ 夜星剛形成": {"5": 71.7, "10": 77.4},
    "大盤跌破60日均線 ＋ 夜星剛形成": {"5": 71.7, "10": 77.4},
    "大盤跌破60日均線 ＋ 型態成形中 ＋ 夜星剛形成": {"5": 71.7, "10": 77.4},
    "大盤跌破60日均線 ＋ 型態突破確認 ＋ 夜星剛形成": {"5": 71.7, "10": 77.4},
    "KDJ近3日內死亡交叉 ＋ 近1季均價YoY為正 ＋ 夜星剛形成": {"10": 77.4},
    "大盤跌破20日均線 ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"5": 70.6, "10": 76.5},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成": {"10": 76.5},
    "相對強弱為正(強於大盤) ＋ 大盤跌破60日均線 ＋ 突破飆股大量黑K最高點剛形成": {"10": 75.5},
    "布林通道低檔(≤20%) ＋ 相對強弱為正(強於大盤) ＋ 爆量(≥2倍均量)": {"5": 75},
    "大盤跌破20日均線 ＋ 型態剛形成(剛突破) ＋ 夜星剛形成": {"5": 70.1, "10": 74.6},
    "大盤跌破20日均線 ＋ 型態突破確認 ＋ 夜星剛形成": {"5": 70.1, "10": 74.6},
    "大盤跌破20日均線 ＋ 型態成形中 ＋ 夜星剛形成": {"5": 70.1, "10": 74.6},
    "大盤跌破20日均線 ＋ 夜星剛形成": {"5": 70.1, "10": 74.6},
    "相對強弱為正(強於大盤) ＋ 大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成": {"20": 74.5},
    "相對強弱為負(弱於大盤) ＋ 型態剛形成(剛突破) ＋ 夜星剛形成": {"5": 72.9, "10": 74.3, "20": 72.9},
    "相對強弱為負(弱於大盤) ＋ 型態成形中 ＋ 夜星剛形成": {"5": 72.9, "10": 74.3, "20": 72.9},
    "相對強弱為負(弱於大盤) ＋ 型態突破確認 ＋ 夜星剛形成": {"5": 72.9, "10": 74.3, "20": 72.9},
    "相對強弱為負(弱於大盤) ＋ 夜星剛形成": {"5": 72.9, "10": 74.3, "20": 72.9},
    "大盤跌破20日均線 ＋ 大盤跌破60日均線 ＋ 突破飆股大量黑K最高點剛形成": {"10": 74.1},
    "近1季乖離度為負(股價超前營收) ＋ 近1季均價YoY為正 ＋ 夜星剛形成": {"10": 73.7},
    "大盤跌破60日均線 ＋ 型態突破確認 ＋ 突破飆股大量黑K最高點剛形成": {"10": 73.7},
    "多方力道≥80 ＋ 相對強弱為負(弱於大盤) ＋ 近1季乖離度為正(營收優於股價)": {"20": 73.2},
    "大盤站上20日均線 ＋ 大盤跌破60日均線 ＋ 突破ABC修正下降切線剛形成": {"20": 72.6},
    "大盤站上60日均線 ＋ 型態剛形成(剛突破) ＋ 夜星剛形成": {"20": 72.6},
    "大盤站上60日均線 ＋ 夜星剛形成": {"20": 72.6},
    "大盤站上60日均線 ＋ 型態成形中 ＋ 夜星剛形成": {"20": 72.6},
    "大盤站上60日均線 ＋ 型態突破確認 ＋ 夜星剛形成": {"20": 72.6},
    "近1季乖離度為正(營收優於股價) ＋ 近1季均價YoY為正 ＋ 複式頭肩底剛形成": {"5": 72},
    "KDJ近3日內死亡交叉 ＋ 型態剛形成(剛突破) ＋ 夜星剛形成": {"20": 71.8},
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成": {"20": 71.8},
    "KDJ近3日內死亡交叉 ＋ 型態成形中 ＋ 夜星剛形成": {"20": 71.8},
    "KDJ近3日內死亡交叉 ＋ 型態突破確認 ＋ 夜星剛形成": {"20": 71.8},
    "大盤站上20日均線 ＋ 突破飆股大量黑K最高點剛形成 ＋ K線橫盤的突破剛形成": {"20": 71.4},
    "布林通道低檔(≤20%) ＋ 相對強弱為正(強於大盤) ＋ 爆量(≥1.5倍均量)": {"5": 71.2},
    "大盤站上20日均線 ＋ 夜星剛形成": {"20": 71.2},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線 ＋ K線橫盤的突破剛形成": {"20": 71.2},
    "多方力道≥65 ＋ 強勢突破盤 ＋ 頭肩底剛形成": {"5": 70.9},
    "相對強弱為正(強於大盤) ＋ 大盤跌破20日均線 ＋ 複式頭肩底剛形成": {"5": 69.4},
    "大盤跌破20日均線 ＋ 複式頭肩底剛形成": {"5": 69.4},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": 62.1, "10": 66.1, "20": 63.9},
}
# 高標股指定組合（M編號）與歷史飆股統計記錄
MOONSHOT_COMBOS = [
    ["爆量(≥1.5倍均量)", "近1季均價YoY為正", "夜星剛形成"],
    ["爆量(≥1.5倍均量)", "型態突破確認", "夜星剛形成"],
    ["爆量(≥1.5倍均量)", "夜星剛形成"],
    ["爆量(≥1.5倍均量)", "型態剛形成(剛突破)", "夜星剛形成"],
    ["爆量(≥1.5倍均量)", "型態成形中", "夜星剛形成"],
    ["布林通道低檔(≤20%)", "近1季均價YoY為正", "夜星剛形成"],
    ["相對強弱為正(強於大盤)", "近1季均價YoY為正", "夜星剛形成"],
    ["相對強弱為正(強於大盤)", "大盤站上20日均線", "夜星剛形成"],
    ["布林通道低檔(≤20%)", "大盤跌破60日均線", "夜星剛形成"],
    ["KDJ近3日內死亡交叉", "近1季均價YoY為正", "夜星剛形成"],
    ["布林通道低檔(≤20%)", "大盤站上60日均線", "母子懷抱(高檔)剛形成"],
    ["KDJ近3日內黃金交叉", "大盤站上20日均線", "夜星剛形成"],
    ["布林通道低檔(≤20%)", "大盤跌破20日均線", "夜星剛形成"],
    ["KDJ近3日內死亡交叉", "近1季乖離度為負(股價超前營收)", "夜星剛形成"],
    ["KDJ近3日內死亡交叉", "相對強弱為正(強於大盤)", "夜星剛形成"],
    ["KDJ近3日內死亡交叉", "大盤站上20日均線", "夜星剛形成"],
    ["布林通道低檔(≤20%)", "KDJ近3日內黃金交叉", "母子懷抱(高檔)剛形成"],
    ["大盤跌破60日均線", "近1季均價YoY為正", "夜星剛形成"],
    ["相對強弱為正(強於大盤)", "近1季乖離度為負(股價超前營收)", "夜星剛形成"],
    ["KDJ近3日內死亡交叉", "大盤站上60日均線", "夜星剛形成"],
    ["多方力道≥80", "MACD近3日內黃金交叉", "突破ABC修正下降切線剛形成"],
    ["爆量(≥1.5倍均量)", "大盤站上20日均線", "晨星剛形成"],
    ["大盤站上20日均線", "近1季乖離度為負(股價超前營收)", "夜星剛形成"],
    ["多方力道≥65", "爆量(≥2倍均量)", "突破ABC修正下降切線剛形成"],
    ["多方力道≥80", "爆量(≥1.5倍均量)", "突破ABC修正下降切線剛形成"],
    ["爆量(≥1.5倍均量)", "近1季均價YoY為正", "晨星剛形成"],
    ["大盤站上20日均線", "近1季均價YoY為正", "夜星剛形成"],
    ["大盤站上60日均線", "近1季乖離度為負(股價超前營收)", "夜星剛形成"],
    ["近1季乖離度為負(股價超前營收)", "突破ABC修正下降切線剛形成", "突破飆股大量黑K最高點剛形成"],
    ["爆量(≥1.5倍均量)", "大盤站上60日均線", "晨星剛形成"],
    ["近1季均價YoY為正", "突破ABC修正下降切線剛形成", "突破飆股大量黑K最高點剛形成"],
    ["大盤站上60日均線", "近1季均價YoY為正", "夜星剛形成"],
    ["相對強弱為正(強於大盤)", "大盤站上60日均線", "夜星剛形成"],
    ["強勢突破盤", "爆量(≥2倍均量)", "突破ABC修正下降切線剛形成"],
    ["爆量(≥1.5倍均量)", "型態突破確認", "晨星剛形成"],
    ["爆量(≥1.5倍均量)", "晨星剛形成"],
    ["爆量(≥1.5倍均量)", "型態剛形成(剛突破)", "晨星剛形成"],
    ["爆量(≥1.5倍均量)", "型態成形中", "晨星剛形成"],
    ["大盤站上60日均線", "突破ABC修正下降切線剛形成", "突破飆股大量黑K最高點剛形成"],
    ["大盤站上20日均線", "大盤站上60日均線", "夜星剛形成"],
    ["爆量(≥2倍均量)", "近1季乖離度為正(營收優於股價)", "突破ABC修正下降切線剛形成"],
    ["多方力道≥80", "爆量(≥2倍均量)", "突破飆股大量黑K最高點剛形成"],
    ["多方力道≥65", "突破ABC修正下降切線剛形成", "突破飆股大量黑K最高點剛形成"],
    ["相對強弱為正(強於大盤)", "突破ABC修正下降切線剛形成", "突破飆股大量黑K最高點剛形成"],
    ["多方力道≥65", "KDJ近3日內死亡交叉", "夜星剛形成"],
    ["型態成形中", "突破ABC修正下降切線剛形成", "突破飆股大量黑K最高點剛形成"],
    ["型態剛形成(剛突破)", "突破ABC修正下降切線剛形成", "突破飆股大量黑K最高點剛形成"],
    ["大盤跌破20日均線", "突破ABC修正下降切線剛形成", "K線橫盤的突破剛形成"],
    ["多方力道≥80", "回後買上漲全通過", "突破飆股大量黑K最高點剛形成"],
    ["爆量(≥2倍均量)", "圓弧底剛形成", "K線橫盤的突破剛形成"],
    ["型態突破確認", "突破ABC修正下降切線剛形成", "突破飆股大量黑K最高點剛形成"],
    ["突破ABC修正下降切線剛形成", "突破飆股大量黑K最高點剛形成"],
    # 2026-09-30 三年回測：20日飆股比例三段都 ≥2 倍基準。⚠️最後一組飆股比例高但平均報酬為負（勝率約42%），屬高風險彩券型
    ["大盤跌破60日均線", "夜星剛形成"],
    ["KDJ近3日內死亡交叉", "大盤跌破60日均線", "夜星剛形成"],
    ["多方力道≥80", "地量(≤0.5倍均量)", "量能區間高檔(≥90百分位)"],
]
MOONSHOT_COMBO_STATS = {
    "爆量(≥1.5倍均量) ＋ 近1季均價YoY為正 ＋ 夜星剛形成": {"20": {"n": 20, "moonshotN": 6, "pct": 30, "avg": 50.8}},
    "爆量(≥1.5倍均量) ＋ 型態突破確認 ＋ 夜星剛形成": {"20": {"n": 25, "moonshotN": 6, "pct": 24, "avg": 50.8}},
    "爆量(≥1.5倍均量) ＋ 夜星剛形成": {"20": {"n": 25, "moonshotN": 6, "pct": 24, "avg": 50.8}},
    "爆量(≥1.5倍均量) ＋ 型態剛形成(剛突破) ＋ 夜星剛形成": {"20": {"n": 25, "moonshotN": 6, "pct": 24, "avg": 50.8}},
    "爆量(≥1.5倍均量) ＋ 型態成形中 ＋ 夜星剛形成": {"20": {"n": 25, "moonshotN": 6, "pct": 24, "avg": 50.8}},
    "布林通道低檔(≤20%) ＋ 近1季均價YoY為正 ＋ 夜星剛形成": {"20": {"n": 30, "moonshotN": 6, "pct": 20, "avg": 50.6}},
    "相對強弱為正(強於大盤) ＋ 近1季均價YoY為正 ＋ 夜星剛形成": {"10": {"n": 36, "moonshotN": 2, "pct": 5.6, "avg": 44.3}, "20": {"n": 36, "moonshotN": 7, "pct": 19.4, "avg": 40.5}},
    "相對強弱為正(強於大盤) ＋ 大盤站上20日均線 ＋ 夜星剛形成": {"10": {"n": 26, "moonshotN": 2, "pct": 7.7, "avg": 44.3}, "20": {"n": 26, "moonshotN": 5, "pct": 19.2, "avg": 42}},
    "布林通道低檔(≤20%) ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"20": {"n": 21, "moonshotN": 4, "pct": 19, "avg": 35.6}},
    "KDJ近3日內死亡交叉 ＋ 近1季均價YoY為正 ＋ 夜星剛形成": {"20": {"n": 53, "moonshotN": 10, "pct": 18.9, "avg": 47.7}},
    "布林通道低檔(≤20%) ＋ 大盤站上60日均線 ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 22, "moonshotN": 1, "pct": 4.5, "avg": 57}, "20": {"n": 22, "moonshotN": 4, "pct": 18.2, "avg": 77.1}},
    "KDJ近3日內黃金交叉 ＋ 大盤站上20日均線 ＋ 夜星剛形成": {"20": {"n": 23, "moonshotN": 4, "pct": 17.4, "avg": 44.7}},
    "布林通道低檔(≤20%) ＋ 大盤跌破20日均線 ＋ 夜星剛形成": {"20": {"n": 29, "moonshotN": 5, "pct": 17.2, "avg": 54.4}},
    "KDJ近3日內死亡交叉 ＋ 近1季乖離度為負(股價超前營收) ＋ 夜星剛形成": {"20": {"n": 47, "moonshotN": 8, "pct": 17, "avg": 51.7}},
    "KDJ近3日內死亡交叉 ＋ 相對強弱為正(強於大盤) ＋ 夜星剛形成": {"20": {"n": 36, "moonshotN": 6, "pct": 16.7, "avg": 41.8}},
    "KDJ近3日內死亡交叉 ＋ 大盤站上20日均線 ＋ 夜星剛形成": {"20": {"n": 36, "moonshotN": 6, "pct": 16.7, "avg": 40.5}},
    "布林通道低檔(≤20%) ＋ KDJ近3日內黃金交叉 ＋ 母子懷抱(高檔)剛形成": {"20": {"n": 24, "moonshotN": 4, "pct": 16.7, "avg": 54.6}},
    "大盤跌破60日均線 ＋ 近1季均價YoY為正 ＋ 夜星剛形成": {"20": {"n": 37, "moonshotN": 6, "pct": 16.2, "avg": 35.8}},
    "相對強弱為正(強於大盤) ＋ 近1季乖離度為負(股價超前營收) ＋ 夜星剛形成": {"10": {"n": 31, "moonshotN": 2, "pct": 6.5, "avg": 44.3}, "20": {"n": 31, "moonshotN": 5, "pct": 16.1, "avg": 43.9}},
    "KDJ近3日內死亡交叉 ＋ 大盤站上60日均線 ＋ 夜星剛形成": {"20": {"n": 44, "moonshotN": 7, "pct": 15.9, "avg": 53.2}},
    "多方力道≥80 ＋ MACD近3日內黃金交叉 ＋ 突破ABC修正下降切線剛形成": {"5": {"n": 22, "moonshotN": 1, "pct": 4.5, "avg": 57.9}, "10": {"n": 22, "moonshotN": 2, "pct": 9.1, "avg": 33.4}},
    "爆量(≥1.5倍均量) ＋ 大盤站上20日均線 ＋ 晨星剛形成": {"10": {"n": 23, "moonshotN": 2, "pct": 8.7, "avg": 34}},
    "大盤站上20日均線 ＋ 近1季乖離度為負(股價超前營收) ＋ 夜星剛形成": {"10": {"n": 37, "moonshotN": 3, "pct": 8.1, "avg": 40.3}},
    "多方力道≥65 ＋ 爆量(≥2倍均量) ＋ 突破ABC修正下降切線剛形成": {"5": {"n": 39, "moonshotN": 3, "pct": 7.7, "avg": 40.5}},
    "多方力道≥80 ＋ 爆量(≥1.5倍均量) ＋ 突破ABC修正下降切線剛形成": {"5": {"n": 40, "moonshotN": 3, "pct": 7.5, "avg": 40.5}},
    "爆量(≥1.5倍均量) ＋ 近1季均價YoY為正 ＋ 晨星剛形成": {"10": {"n": 27, "moonshotN": 2, "pct": 7.4, "avg": 34}},
    "大盤站上20日均線 ＋ 近1季均價YoY為正 ＋ 夜星剛形成": {"10": {"n": 41, "moonshotN": 3, "pct": 7.3, "avg": 40.3}},
    "大盤站上60日均線 ＋ 近1季乖離度為負(股價超前營收) ＋ 夜星剛形成": {"10": {"n": 46, "moonshotN": 3, "pct": 6.5, "avg": 40.3}},
    "近1季乖離度為負(股價超前營收) ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 33, "moonshotN": 2, "pct": 6.1, "avg": 32.7}, "10": {"n": 33, "moonshotN": 2, "pct": 6.1, "avg": 34.7}},
    "爆量(≥1.5倍均量) ＋ 大盤站上60日均線 ＋ 晨星剛形成": {"10": {"n": 33, "moonshotN": 2, "pct": 6.1, "avg": 34}},
    "近1季均價YoY為正 ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 34, "moonshotN": 2, "pct": 5.9, "avg": 32.7}, "10": {"n": 34, "moonshotN": 2, "pct": 5.9, "avg": 34.7}},
    "大盤站上60日均線 ＋ 近1季均價YoY為正 ＋ 夜星剛形成": {"10": {"n": 52, "moonshotN": 3, "pct": 5.8, "avg": 40.3}},
    "相對強弱為正(強於大盤) ＋ 大盤站上60日均線 ＋ 夜星剛形成": {"10": {"n": 35, "moonshotN": 2, "pct": 5.7, "avg": 44.3}},
    "強勢突破盤 ＋ 爆量(≥2倍均量) ＋ 突破ABC修正下降切線剛形成": {"5": {"n": 54, "moonshotN": 3, "pct": 5.6, "avg": 40.5}},
    "爆量(≥1.5倍均量) ＋ 型態突破確認 ＋ 晨星剛形成": {"10": {"n": 36, "moonshotN": 2, "pct": 5.6, "avg": 34}},
    "爆量(≥1.5倍均量) ＋ 晨星剛形成": {"10": {"n": 36, "moonshotN": 2, "pct": 5.6, "avg": 34}},
    "爆量(≥1.5倍均量) ＋ 型態剛形成(剛突破) ＋ 晨星剛形成": {"10": {"n": 36, "moonshotN": 2, "pct": 5.6, "avg": 34}},
    "爆量(≥1.5倍均量) ＋ 型態成形中 ＋ 晨星剛形成": {"10": {"n": 36, "moonshotN": 2, "pct": 5.6, "avg": 34}},
    "大盤站上60日均線 ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 37, "moonshotN": 2, "pct": 5.4, "avg": 32.7}, "10": {"n": 37, "moonshotN": 2, "pct": 5.4, "avg": 34.7}},
    "大盤站上20日均線 ＋ 大盤站上60日均線 ＋ 夜星剛形成": {"10": {"n": 57, "moonshotN": 3, "pct": 5.3, "avg": 40.3}},
    "爆量(≥2倍均量) ＋ 近1季乖離度為正(營收優於股價) ＋ 突破ABC修正下降切線剛形成": {"5": {"n": 40, "moonshotN": 2, "pct": 5, "avg": 45.5}},
    "多方力道≥80 ＋ 爆量(≥2倍均量) ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 21, "moonshotN": 1, "pct": 4.8, "avg": 30.5}},
    "多方力道≥65 ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 21, "moonshotN": 1, "pct": 4.8, "avg": 30.5}},
    "相對強弱為正(強於大盤) ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 44, "moonshotN": 2, "pct": 4.5, "avg": 32.7}},
    "多方力道≥65 ＋ KDJ近3日內死亡交叉 ＋ 夜星剛形成": {"5": {"n": 22, "moonshotN": 1, "pct": 4.5, "avg": 34.3}},
    "型態成形中 ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 47, "moonshotN": 2, "pct": 4.3, "avg": 32.7}},
    "型態剛形成(剛突破) ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 47, "moonshotN": 2, "pct": 4.3, "avg": 32.7}},
    "大盤跌破20日均線 ＋ 突破ABC修正下降切線剛形成 ＋ K線橫盤的突破剛形成": {"5": {"n": 23, "moonshotN": 1, "pct": 4.3, "avg": 34.9}},
    "多方力道≥80 ＋ 回後買上漲全通過 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 23, "moonshotN": 1, "pct": 4.3, "avg": 30.5}},
    "爆量(≥2倍均量) ＋ 圓弧底剛形成 ＋ K線橫盤的突破剛形成": {"5": {"n": 23, "moonshotN": 1, "pct": 4.3, "avg": 30.9}},
    "型態突破確認 ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 47, "moonshotN": 2, "pct": 4.3, "avg": 32.7}},
    "突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 47, "moonshotN": 2, "pct": 4.3, "avg": 32.7}},
    "大盤跌破60日均線 ＋ 夜星剛形成": {"10": {"n": 350, "moonshotN": 19, "pct": 5.4, "avg": 42.6}, "20": {"n": 350, "moonshotN": 51, "pct": 14.6, "avg": 49.2}},
    "KDJ近3日內死亡交叉 ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"10": {"n": 230, "moonshotN": 11, "pct": 4.8, "avg": 44.2}, "20": {"n": 230, "moonshotN": 36, "pct": 15.7, "avg": 50.9}},
    "多方力道≥80 ＋ 地量(≤0.5倍均量) ＋ 量能區間高檔(≥90百分位)": {"10": {"n": 527, "moonshotN": 37, "pct": 7.0, "avg": 95.7}, "20": {"n": 527, "moonshotN": 64, "pct": 12.1, "avg": 70.2}},
}



# ── 回測自動入選組合（2026-10-05 三年六段回測，美股 2023-10～2026-09）──
# W＝回測高勝率：5/10/20日任一天期勝率>60% 且 t值(同日調整)>2（樣本數≥100）；依最高 t 值排序
# F＝回測飆股：10/20日飆股(漲幅>30%)比例>10% 且飆股次數≥10；依最高飆股比例排序
# 已在 S／#／M 清單裡的組合不重複列入
BT_WIN_COMBOS = [
    ["大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["大盤跌破60日均線", "近1季財報EPS大幅優於預期(≥10%)", "中期強勢(12-1月勝大盤≥20%)"],
    ["大盤跌破60日均線", "近1季財報EPS優於預期", "中期強勢(12-1月勝大盤≥20%)"],
    ["布林通道低檔(≤20%)", "大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["近1季財報EPS優於預期", "中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)"],
    ["大盤跌破20日均線", "大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["布林通道低檔(≤20%)", "大盤跌破20日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["相對強弱為負(弱於大盤)", "大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)"],
    ["大盤跌破60日均線", "近1季財報EPS大幅優於預期(≥10%)"],
    ["量能區間高檔(≥90百分位)", "大盤跌破60日均線", "近1季財報EPS大幅優於預期(≥10%)"],
    ["相對強弱為負(弱於大盤)", "大盤跌破20日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)", "近5日跌幅≥8%(短線超跌)"],
    ["相對強弱為正(強於大盤)", "大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["量能區間高檔(≥90百分位)", "大盤跌破60日均線", "近1季財報EPS優於預期"],
    ["大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)", "半年強勢(6-1月勝大盤≥10%)"],
    ["相對強弱為負(弱於大盤)", "大盤跌破20日均線", "近1季財報EPS大幅優於預期(≥10%)"],
    ["大盤跌破20日均線", "大盤跌破60日均線", "近1季財報EPS大幅優於預期(≥10%)"],
    ["大盤跌破60日均線", "近1季財報EPS低於預期", "中期強勢(12-1月勝大盤≥20%)"],
    ["量能區間高檔(≥90百分位)", "中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)"],
    ["多方力道≥80", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["大盤跌破20日均線", "近1季財報EPS低於預期", "中期強勢(12-1月勝大盤≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線", "近1季財報EPS大幅優於預期(≥10%)"],
    ["大盤跌破60日均線", "近1季財報EPS大幅優於預期(≥10%)", "半年強勢(6-1月勝大盤≥10%)"],
    ["相對強弱為負(弱於大盤)", "量能區間高檔(≥90百分位)", "大盤跌破60日均線"],
    ["大盤跌破60日均線", "近1季財報EPS大幅優於預期(≥10%)", "連續放量(近3日均量≥1.5倍前20日均量)"],
    ["多方力道≥80", "近20日財報跳空上漲", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["大盤跌破20日均線", "中期強勢(12-1月勝大盤≥20%)", "近5日跌幅≥8%(短線超跌)"],
    ["布林通道低檔(≤20%)", "近1季財報EPS優於預期", "中期強勢(12-1月勝大盤≥20%)"],
    ["近20日財報跳空上漲", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "近1季財報EPS大幅優於預期(≥10%)", "CMF資金流買方佔優(近20日≥0.1)"],
    ["量能區間高檔(≥90百分位)", "半年強勢(6-1月勝大盤≥10%)", "近5日跌幅≥8%(短線超跌)"],
    ["布林通道低檔(≤20%)", "相對強弱為負(弱於大盤)", "中期強勢(12-1月勝大盤≥20%)"],
    ["多方力道≥80", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["布林通道低檔(≤20%)", "大盤跌破20日均線", "半年強勢(6-1月勝大盤≥10%)"],
    ["近20日強於大盤≥10%", "大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["相對強弱為負(弱於大盤)", "大盤跌破60日均線", "近1季財報EPS大幅優於預期(≥10%)"],
    ["布林通道低檔(≤20%)", "中期強勢(12-1月勝大盤≥20%)"],
    ["近20日財報跳空上漲", "距52週高點≤5%", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)"],
    ["布林通道低檔(≤20%)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "中期強勢(12-1月勝大盤≥20%)"],
    ["大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)", "NR7窄幅日(近7日振幅最小)"],
    ["大盤跌破20日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破20日均線", "近1季財報EPS大幅優於預期(≥10%)"],
    ["布林通道低檔(≤20%)", "中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)"],
    ["量能區間高檔(≥90百分位)", "大盤跌破60日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破20日均線", "中期強勢(12-1月勝大盤≥20%)"],
    ["大盤跌破60日均線", "近1季財報EPS低於預期", "近5日跌幅≥8%(短線超跌)"],
    ["布林通道低檔(≤20%)", "中期強勢(12-1月勝大盤≥20%)", "半年強勢(6-1月勝大盤≥10%)"],

]
BT_WIN_STATS = {
    "大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 38347, "win": 58.0, "ex": 0.51, "t": 17.31}, "10": {"n": 38347, "win": 61.8, "ex": 1.01, "t": 23.74}, "20": {"n": 38347, "win": 60.8, "ex": 1.74, "t": 28.83}},
    "大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%) ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 17159, "win": 58.9, "ex": 0.65, "t": 14.13}, "10": {"n": 17159, "win": 62.8, "ex": 1.25, "t": 18.82}, "20": {"n": 17159, "win": 62.3, "ex": 2.25, "t": 23.91}},
    "大盤跌破60日均線 ＋ 近1季財報EPS優於預期 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 27783, "win": 58.8, "ex": 0.57, "t": 16.68}, "10": {"n": 27783, "win": 62.8, "ex": 1.09, "t": 22.43}, "20": {"n": 27783, "win": 61.8, "ex": 1.85, "t": 26.73}},
    "布林通道低檔(≤20%) ＋ 大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 11961, "win": 58.5, "ex": 0.89, "t": 17.32}, "10": {"n": 11961, "win": 64.7, "ex": 1.8, "t": 24.9}, "20": {"n": 11961, "win": 62.8, "ex": 2.06, "t": 20.54}},
    "近1季財報EPS優於預期 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1)": {"5": {"n": 45121, "win": 53.8, "ex": 0.33, "t": 12.15}, "10": {"n": 45121, "win": 55.0, "ex": 0.65, "t": 16.8}, "20": {"n": 45121, "win": 56.2, "ex": 1.09, "t": 19.38}},
    "大盤跌破20日均線 ＋ 大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 33732, "win": 57.4, "ex": 0.53, "t": 17.01}, "10": {"n": 33732, "win": 61.7, "ex": 1.06, "t": 23.77}, "20": {"n": 33732, "win": 60.2, "ex": 1.69, "t": 26.39}},
    "布林通道低檔(≤20%) ＋ 大盤跌破20日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 16957, "win": 57.3, "ex": 0.72, "t": 16.45}, "10": {"n": 16957, "win": 61.3, "ex": 1.38, "t": 22.22}, "20": {"n": 16957, "win": 61.7, "ex": 1.96, "t": 22.18}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 21120, "win": 58.4, "ex": 0.66, "t": 16.69}, "10": {"n": 21120, "win": 63.0, "ex": 1.37, "t": 24.58}, "20": {"n": 21120, "win": 60.8, "ex": 1.73, "t": 22.15}},
    "中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1)": {"5": {"n": 59747, "win": 53.2, "ex": 0.26, "t": 10.98}, "10": {"n": 59747, "win": 54.2, "ex": 0.56, "t": 16.25}, "20": {"n": 59747, "win": 55.2, "ex": 0.97, "t": 19.28}},
    "大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": {"5": {"n": 55699, "win": 56.9, "ex": 0.11, "t": 4.91}, "10": {"n": 55699, "win": 60.2, "ex": 0.24, "t": 7.41}, "20": {"n": 55699, "win": 59.7, "ex": 0.55, "t": 11.97}},
    "量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": {"5": {"n": 9549, "win": 57.2, "ex": 0.4, "t": 6.76}, "10": {"n": 9549, "win": 61.7, "ex": 0.81, "t": 10.3}, "20": {"n": 9549, "win": 60.8, "ex": 1.11, "t": 10.21}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 29210, "win": 57.1, "ex": 0.59, "t": 17.34}, "10": {"n": 29210, "win": 60.4, "ex": 1.22, "t": 24.98}, "20": {"n": 29210, "win": 59.3, "ex": 1.63, "t": 23.67}},
    "大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 近5日跌幅≥8%(短線超跌)": {"5": {"n": 5010, "win": 59.3, "ex": 1.56, "t": 16.21}, "10": {"n": 5010, "win": 65.6, "ex": 2.83, "t": 21.05}, "20": {"n": 5010, "win": 60.3, "ex": 3.08, "t": 16.9}},
    "相對強弱為正(強於大盤) ＋ 大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 17227, "win": 57.4, "ex": 0.33, "t": 7.44}, "10": {"n": 17227, "win": 60.2, "ex": 0.56, "t": 8.63}, "20": {"n": 17227, "win": 60.8, "ex": 1.76, "t": 18.61}},
    "量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線 ＋ 近1季財報EPS優於預期": {"5": {"n": 19880, "win": 57.4, "ex": 0.23, "t": 6.32}, "10": {"n": 19880, "win": 61.1, "ex": 0.41, "t": 8.3}, "20": {"n": 19880, "win": 60.2, "ex": 0.5, "t": 7.23}},
    "大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 半年強勢(6-1月勝大盤≥10%)": {"5": {"n": 28332, "win": 58.5, "ex": 0.6, "t": 17.01}, "10": {"n": 28332, "win": 62.4, "ex": 1.09, "t": 21.83}, "20": {"n": 28332, "win": 61.0, "ex": 1.75, "t": 24.62}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": {"5": {"n": 38886, "win": 56.7, "ex": 0.27, "t": 10.1}, "10": {"n": 38886, "win": 59.3, "ex": 0.56, "t": 14.32}, "20": {"n": 38886, "win": 59.7, "ex": 0.86, "t": 15.44}},
    "大盤跌破20日均線 ＋ 大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": {"5": {"n": 48604, "win": 56.4, "ex": 0.09, "t": 3.73}, "10": {"n": 48604, "win": 59.5, "ex": 0.17, "t": 4.96}, "20": {"n": 48604, "win": 58.9, "ex": 0.44, "t": 8.89}},
    "大盤跌破60日均線 ＋ 近1季財報EPS低於預期 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 7988, "win": 56.5, "ex": 0.36, "t": 5.5}, "10": {"n": 7988, "win": 60.9, "ex": 0.87, "t": 9.27}, "20": {"n": 7988, "win": 60.6, "ex": 1.67, "t": 12.06}},
    "量能區間高檔(≥90百分位) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1)": {"5": {"n": 8517, "win": 54.1, "ex": 0.3, "t": 3.99}, "10": {"n": 8517, "win": 55.1, "ex": 0.75, "t": 7.18}, "20": {"n": 8517, "win": 55.3, "ex": 1.26, "t": 8.57}},
    "多方力道≥80 ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"5": {"n": 1778, "win": 51.4, "ex": 0.46, "t": 2.26}, "10": {"n": 1778, "win": 53.5, "ex": 1.37, "t": 4.81}, "20": {"n": 1778, "win": 56.0, "ex": 3.15, "t": 7.85}},
    "大盤跌破20日均線 ＋ 近1季財報EPS低於預期 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 10828, "win": 55.0, "ex": 0.26, "t": 4.45}, "10": {"n": 10828, "win": 57.9, "ex": 0.7, "t": 8.16}, "20": {"n": 10828, "win": 58.2, "ex": 1.17, "t": 9.39}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 5200, "win": 58.2, "ex": 0.59, "t": 7.18}, "10": {"n": 5200, "win": 64.9, "ex": 1.34, "t": 12.07}, "20": {"n": 5200, "win": 62.2, "ex": 1.38, "t": 8.8}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": {"5": {"n": 7801, "win": 60.0, "ex": 0.25, "t": 4.09}, "10": {"n": 7801, "win": 65.1, "ex": 0.6, "t": 7.29}, "20": {"n": 7801, "win": 63.9, "ex": 0.63, "t": 5.39}},
    "大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%) ＋ 半年強勢(6-1月勝大盤≥10%)": {"5": {"n": 21180, "win": 58.4, "ex": 0.39, "t": 9.82}, "10": {"n": 21180, "win": 62.2, "ex": 0.71, "t": 12.41}, "20": {"n": 21180, "win": 61.0, "ex": 1.23, "t": 15.21}},
    "相對強弱為負(弱於大盤) ＋ 量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線": {"5": {"n": 15926, "win": 57.3, "ex": 0.32, "t": 7.62}, "10": {"n": 15926, "win": 62.8, "ex": 0.7, "t": 12.63}, "20": {"n": 15926, "win": 60.7, "ex": 0.57, "t": 7.51}},
    "大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": {"5": {"n": 6584, "win": 58.3, "ex": 0.28, "t": 4.14}, "10": {"n": 6584, "win": 63.9, "ex": 0.65, "t": 7.09}, "20": {"n": 6584, "win": 63.0, "ex": 0.83, "t": 6.35}},
    "多方力道≥80 ＋ 近20日財報跳空上漲 ＋ 短線過熱(高於月線≥20%)": {"5": {"n": 2054, "win": 54.5, "ex": 0.74, "t": 4.39}, "10": {"n": 2054, "win": 57.2, "ex": 2.14, "t": 9.28}, "20": {"n": 2054, "win": 59.0, "ex": 3.31, "t": 10.2}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 11438, "win": 58.5, "ex": 0.54, "t": 9.9}, "10": {"n": 11438, "win": 64.7, "ex": 1.13, "t": 14.97}, "20": {"n": 11438, "win": 61.5, "ex": 1.73, "t": 15.58}},
    "大盤跌破20日均線 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 近5日跌幅≥8%(短線超跌)": {"5": {"n": 7302, "win": 57.3, "ex": 1.09, "t": 13.28}, "10": {"n": 7302, "win": 61.1, "ex": 1.91, "t": 16.28}, "20": {"n": 7302, "win": 57.9, "ex": 2.2, "t": 13.6}},
    "布林通道低檔(≤20%) ＋ 近1季財報EPS優於預期 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 25701, "win": 54.9, "ex": 0.54, "t": 15.83}, "10": {"n": 25701, "win": 56.6, "ex": 0.78, "t": 15.95}, "20": {"n": 25701, "win": 56.9, "ex": 1.24, "t": 17.56}},
    "近20日財報跳空上漲 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"5": {"n": 2311, "win": 53.8, "ex": 0.55, "t": 3.5}, "10": {"n": 2311, "win": 56.6, "ex": 1.69, "t": 7.74}, "20": {"n": 2311, "win": 57.8, "ex": 2.76, "t": 9.0}},
    "大盤站上20日均線 ＋ 近1季財報EPS大幅優於預期(≥10%) ＋ CMF資金流買方佔優(近20日≥0.1)": {"5": {"n": 70035, "win": 53.3, "ex": 0.22, "t": 11.59}, "10": {"n": 70035, "win": 54.7, "ex": 0.47, "t": 16.99}, "20": {"n": 70035, "win": 55.1, "ex": 0.6, "t": 14.79}},
    "量能區間高檔(≥90百分位) ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 近5日跌幅≥8%(短線超跌)": {"5": {"n": 5085, "win": 56.5, "ex": 0.79, "t": 8.25}, "10": {"n": 5085, "win": 58.3, "ex": 1.32, "t": 10.02}, "20": {"n": 5085, "win": 52.9, "ex": 0.98, "t": 5.47}},
    "布林通道低檔(≤20%) ＋ 相對強弱為負(弱於大盤) ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 32441, "win": 53.7, "ex": 0.45, "t": 14.44}, "10": {"n": 32441, "win": 55.5, "ex": 0.7, "t": 15.68}, "20": {"n": 32441, "win": 55.7, "ex": 1.15, "t": 17.89}},
    "多方力道≥80 ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"5": {"n": 3696, "win": 52.9, "ex": 0.63, "t": 4.63}, "10": {"n": 3696, "win": 54.7, "ex": 1.71, "t": 8.94}, "20": {"n": 3696, "win": 55.7, "ex": 2.77, "t": 10.28}},
    "布林通道低檔(≤20%) ＋ 大盤跌破20日均線 ＋ 半年強勢(6-1月勝大盤≥10%)": {"5": {"n": 22026, "win": 57.0, "ex": 0.5, "t": 13.77}, "10": {"n": 22026, "win": 61.7, "ex": 1.06, "t": 20.63}, "20": {"n": 22026, "win": 62.3, "ex": 1.29, "t": 17.73}},
    "近20日強於大盤≥10% ＋ 大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 5685, "win": 56.3, "ex": 0.4, "t": 4.45}, "10": {"n": 5685, "win": 57.3, "ex": 0.62, "t": 4.77}, "20": {"n": 5685, "win": 58.7, "ex": 2.45, "t": 13.27}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": {"5": {"n": 27483, "win": 57.5, "ex": 0.26, "t": 8.0}, "10": {"n": 27483, "win": 62.1, "ex": 0.66, "t": 14.34}, "20": {"n": 27483, "win": 61.1, "ex": 0.88, "t": 13.37}},
    "布林通道低檔(≤20%) ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 34832, "win": 54.2, "ex": 0.46, "t": 15.29}, "10": {"n": 34832, "win": 56.0, "ex": 0.72, "t": 16.67}, "20": {"n": 34832, "win": 56.3, "ex": 1.18, "t": 18.94}},
    "近20日財報跳空上漲 ＋ 距52週高點≤5% ＋ 短線過熱(高於月線≥20%)": {"5": {"n": 1519, "win": 49.8, "ex": -0.05, "t": -0.27}, "10": {"n": 1519, "win": 53.6, "ex": 1.1, "t": 4.37}, "20": {"n": 1519, "win": 56.4, "ex": 2.34, "t": 6.62}},
    "多方力道≥65 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1)": {"5": {"n": 27110, "win": 53.7, "ex": 0.36, "t": 9.63}, "10": {"n": 27110, "win": 53.8, "ex": 0.72, "t": 13.57}, "20": {"n": 27110, "win": 55.0, "ex": 1.25, "t": 16.31}},
    "布林通道低檔(≤20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 7081, "win": 53.8, "ex": 0.35, "t": 5.08}, "10": {"n": 7081, "win": 57.2, "ex": 0.7, "t": 7.52}, "20": {"n": 7081, "win": 55.8, "ex": 0.87, "t": 6.58}},
    "大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ NR7窄幅日(近7日振幅最小)": {"5": {"n": 5565, "win": 59.7, "ex": 0.44, "t": 5.9}, "10": {"n": 5565, "win": 62.1, "ex": 0.77, "t": 7.11}, "20": {"n": 5565, "win": 60.0, "ex": 1.6, "t": 9.99}},
    "大盤跌破20日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 54495, "win": 56.2, "ex": 0.36, "t": 14.18}, "10": {"n": 54495, "win": 58.4, "ex": 0.69, "t": 18.83}, "20": {"n": 54495, "win": 58.1, "ex": 1.12, "t": 21.44}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破20日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": {"5": {"n": 11243, "win": 57.8, "ex": 0.09, "t": 1.69}, "10": {"n": 11243, "win": 60.3, "ex": 0.36, "t": 5.04}, "20": {"n": 11243, "win": 61.8, "ex": 0.39, "t": 3.91}},
    "布林通道低檔(≤20%) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1)": {"5": {"n": 3537, "win": 53.1, "ex": 0.5, "t": 5.69}, "10": {"n": 3537, "win": 56.0, "ex": 0.82, "t": 6.54}, "20": {"n": 3537, "win": 60.8, "ex": 1.85, "t": 10.16}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1)": {"5": {"n": 8191, "win": 53.4, "ex": 0.22, "t": 3.07}, "10": {"n": 8191, "win": 55.0, "ex": 0.69, "t": 7.03}, "20": {"n": 8191, "win": 55.7, "ex": 1.07, "t": 7.61}},
    "量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 6194, "win": 56.9, "ex": 0.72, "t": 8.88}, "10": {"n": 6194, "win": 62.3, "ex": 1.38, "t": 12.81}, "20": {"n": 6194, "win": 58.8, "ex": 1.92, "t": 12.93}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破20日均線 ＋ 中期強勢(12-1月勝大盤≥20%)": {"5": {"n": 7434, "win": 56.5, "ex": 0.32, "t": 4.43}, "10": {"n": 7434, "win": 60.0, "ex": 0.84, "t": 8.46}, "20": {"n": 7434, "win": 59.7, "ex": 0.82, "t": 5.98}},
    "大盤跌破60日均線 ＋ 近1季財報EPS低於預期 ＋ 近5日跌幅≥8%(短線超跌)": {"5": {"n": 4049, "win": 57.2, "ex": 0.4, "t": 4.24}, "10": {"n": 4049, "win": 63.1, "ex": 1.22, "t": 9.62}, "20": {"n": 4049, "win": 60.9, "ex": 0.95, "t": 5.36}},
    "布林通道低檔(≤20%) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 半年強勢(6-1月勝大盤≥10%)": {"5": {"n": 25131, "win": 54.1, "ex": 0.51, "t": 14.11}, "10": {"n": 25131, "win": 55.7, "ex": 0.77, "t": 14.69}, "20": {"n": 25131, "win": 55.7, "ex": 1.13, "t": 15.19}},

}
BT_HOT_COMBOS = [
    ["爆量(≥2倍均量)", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "量能區間高檔(≥90百分位)", "近5日跌幅≥8%(短線超跌)"],
    ["爆量(≥1.5倍均量)", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "爆量(≥1.5倍均量)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS大幅優於預期(≥10%)", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "爆量(≥2倍均量)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["中期強勢(12-1月勝大盤≥20%)", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["KDJ近3日內死亡交叉", "爆量(≥2倍均量)", "短線過熱(高於月線≥20%)"],
    ["中期強勢(12-1月勝大盤≥20%)", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "爆量(≥1.5倍均量)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "量能區間高檔(≥90百分位)", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["中期強勢(12-1月勝大盤≥20%)", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "爆量(≥1.5倍均量)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "KDJ近3日內黃金交叉", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "相對強弱為正(強於大盤)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "近20日強於大盤≥10%", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "爆量(≥1.5倍均量)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["大盤跌破60日均線", "布林通道收窄(寬度近半年最低20%)", "母子懷抱(低檔)剛形成"],
    ["KDJ近3日內黃金交叉", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS優於預期", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "KDJ近3日內黃金交叉", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "爆量(≥1.5倍均量)", "短線過熱(高於月線≥20%)"],
    ["回後買上漲全通過", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "爆量(≥1.5倍均量)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "量能區間高檔(≥90百分位)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["大盤站上60日均線", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "爆量(≥2倍均量)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "KDJ近3日內黃金交叉", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "爆量(≥2倍均量)", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "爆量(≥2倍均量)", "短線過熱(高於月線≥20%)"],
    ["強勢突破盤", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["連續放量(近3日均量≥1.5倍前20日均量)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "爆量(≥1.5倍均量)", "短線過熱(高於月線≥20%)"],
    ["MACD近3日內黃金交叉", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "量能區間高檔(≥90百分位)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "漲時量≥跌時量1.5倍(近20日)", "近5日跌幅≥8%(短線超跌)"],
    ["KDJ近3日內死亡交叉", "量能區間高檔(≥90百分位)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "爆量(≥2倍均量)", "短線過熱(高於月線≥20%)"],
    ["連續放量(近3日均量≥1.5倍前20日均量)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["大盤站上60日均線", "短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["量能區間高檔(≥90百分位)", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "中期強勢(12-1月勝大盤≥20%)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "爆量(≥2倍均量)", "短線過熱(高於月線≥20%)"],
    ["中期強勢(12-1月勝大盤≥20%)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "短線過熱(高於月線≥20%)"],
    ["中期強勢(12-1月勝大盤≥20%)", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "中期強勢(12-1月勝大盤≥20%)", "夜星剛形成"],
    ["布林通道高檔(≥80%)", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["強勢突破盤", "KDJ近3日內黃金交叉", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "量能區間高檔(≥90百分位)", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["回後買上漲全通過", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "近1季財報EPS低於預期", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS優於預期", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "短線過熱(高於月線≥20%)"],
    ["半年強勢(6-1月勝大盤≥10%)", "漲時量≥跌時量1.5倍(近20日)", "母子懷抱(高檔)剛形成"],
    ["近20日強於大盤≥10%", "量能區間高檔(≥90百分位)", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS低於預期", "均線多頭排列(5>20>60且站上月線)", "近5日跌幅≥8%(短線超跌)"],
    ["量能區間高檔(≥90百分位)", "均線多頭排列(5>20>60且站上月線)", "近5日跌幅≥8%(短線超跌)"],
    ["KDJ近3日內死亡交叉", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["連續放量(近3日均量≥1.5倍前20日均量)", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["回後買上漲全通過", "半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "相對強弱為正(強於大盤)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "近20日強於大盤≥10%", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "KDJ近3日內死亡交叉", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["半年強勢(6-1月勝大盤≥10%)", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["回後買上漲全通過", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["多方力道≥65", "短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["大盤站上20日均線", "短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["中期強勢(12-1月勝大盤≥20%)", "CMF資金流買方佔優(近20日≥0.1)", "夜星剛形成"],
    ["多方力道≥65", "KDJ近3日內死亡交叉", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "KDJ近3日內死亡交叉", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "量能區間高檔(≥90百分位)", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "量能區間高檔(≥90百分位)", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "均線多頭排列(5>20>60且站上月線)", "近5日跌幅≥8%(短線超跌)"],
    ["量能區間高檔(≥90百分位)", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "量能區間高檔(≥90百分位)", "短線過熱(高於月線≥20%)"],
    ["回後買上漲全通過", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["半年強勢(6-1月勝大盤≥10%)", "均線多頭排列(5>20>60且站上月線)", "近5日跌幅≥8%(短線超跌)"],
    ["KDJ近3日內黃金交叉", "KDJ近3日內死亡交叉", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["半年強勢(6-1月勝大盤≥10%)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["均線多頭排列(5>20>60且站上月線)", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["爆量(≥2倍均量)", "創52週新高", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS低於預期", "布林通道收窄(寬度近半年最低20%)", "近5日跌幅≥8%(短線超跌)"],
    ["CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["KDJ近3日內黃金交叉", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["爆量(≥1.5倍均量)", "近1季財報EPS大幅優於預期(≥10%)", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "近1季財報EPS低於預期", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["漲時量≥跌時量1.5倍(近20日)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["均線多頭排列(5>20>60且站上月線)", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["近3日向上跳空缺口", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "創52週新高", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["相對強弱為正(強於大盤)", "短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["近20日強於大盤≥10%", "半年強勢(6-1月勝大盤≥10%)", "近5日跌幅≥8%(短線超跌)"],
    ["近1季財報EPS低於預期", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "大盤站上60日均線", "近5日跌幅≥8%(短線超跌)"],
    ["KDJ近3日內黃金交叉", "近1季財報EPS低於預期", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["半年強勢(6-1月勝大盤≥10%)", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "距52週高點≤5%", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS優於預期", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS優於預期", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["均線多頭排列(5>20>60且站上月線)", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["CMF資金流買方佔優(近20日≥0.1)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "短線過熱(高於月線≥20%)"],
    ["近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "相對強弱為正(強於大盤)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "近20日強於大盤≥10%", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["大盤站上60日均線", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS優於預期", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS大幅優於預期(≥10%)", "中期強勢(12-1月勝大盤≥20%)", "夜星剛形成"],
    ["爆量(≥2倍均量)", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "創52週新高", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["大盤站上60日均線", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["MACD近3日內黃金交叉", "大盤站上20日均線", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "近20日強於大盤≥10%", "近5日跌幅≥8%(短線超跌)"],
    ["多方力道≥80", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["量能區間高檔(≥90百分位)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "短線過熱(高於月線≥20%)"],
    ["大盤站上60日均線", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "短線過熱(高於月線≥20%)"],
    ["漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "MACD近3日內黃金交叉", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "相對強弱為正(強於大盤)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "近20日強於大盤≥10%", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "相對強弱為正(強於大盤)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "近20日強於大盤≥10%", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["均線多頭排列(5>20>60且站上月線)", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS低於預期", "中期強勢(12-1月勝大盤≥20%)", "夜星剛形成"],
    ["布林通道高檔(≥80%)", "短線過熱(高於月線≥20%)", "母子懷抱(高檔)剛形成"],
    ["半年強勢(6-1月勝大盤≥10%)", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["MACD近3日內黃金交叉", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "半年強勢(6-1月勝大盤≥10%)", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內黃金交叉", "量能斜率轉強(近5日均量>近10日均量20%以上)", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS優於預期", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["大盤站上20日均線", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["回後買上漲全通過", "近3日向上跳空缺口", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS低於預期", "CMF資金流買方佔優(近20日≥0.1)", "短線過熱(高於月線≥20%)"],
    ["大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "量能斜率轉強(近5日均量>近10日均量20%以上)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "量能斜率轉強(近5日均量>近10日均量20%以上)", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "布林通道高檔(≥80%)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["KDJ近3日內死亡交叉", "距52週高點≤5%", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "近1季財報EPS低於預期", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["大盤站上60日均線", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["半年強勢(6-1月勝大盤≥10%)", "漲時量≥跌時量1.5倍(近20日)", "近5日跌幅≥8%(短線超跌)"],
    ["漲時量≥跌時量1.5倍(近20日)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["多方力道≥65", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["大盤站上60日均線", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "大盤站上20日均線", "近5日跌幅≥8%(短線超跌)"],
    ["布林通道高檔(≥80%)", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["大盤跌破60日均線", "近3日向上跳空缺口", "母子懷抱(低檔)剛形成"],
    ["近20日強於大盤≥10%", "回後買上漲全通過", "短線過熱(高於月線≥20%)"],
    ["MACD近3日內黃金交叉", "爆量(≥1.5倍均量)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "短線過熱(高於月線≥20%)"],
    ["相對強弱為正(強於大盤)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["近20日強於大盤≥10%", "量能斜率轉強(近5日均量>近10日均量20%以上)", "短線過熱(高於月線≥20%)"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["近1季財報EPS低於預期", "短線過熱(高於月線≥20%)"],
    ["OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["多方力道≥80", "近1季財報EPS優於預期", "短線過熱(高於月線≥20%)"],
    ["強勢突破盤", "連續放量(近3日均量≥1.5倍前20日均量)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "漲時量≥跌時量1.5倍(近20日)", "短線過熱(高於月線≥20%)"],
    ["布林通道高檔(≥80%)", "大盤站上60日均線", "短線過熱(高於月線≥20%)"],
    ["回後買上漲全通過", "均線多頭排列(5>20>60且站上月線)", "短線過熱(高於月線≥20%)"],
    ["半年強勢(6-1月勝大盤≥10%)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],
    ["均線多頭排列(5>20>60且站上月線)", "OBV能量潮創60日新高", "短線過熱(高於月線≥20%)"],

]
BT_HOT_STATS = {
    "爆量(≥2倍均量) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 700, "moonshotN": 57, "pct": 8.1, "avg": 57.4}, "20": {"n": 700, "moonshotN": 95, "pct": 13.6, "avg": 59.6}},
    "多方力道≥80 ＋ 量能區間高檔(≥90百分位) ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 139, "moonshotN": 11, "pct": 7.9, "avg": 55.2}, "20": {"n": 139, "moonshotN": 19, "pct": 13.7, "avg": 47.8}},
    "爆量(≥1.5倍均量) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1271, "moonshotN": 96, "pct": 7.6, "avg": 52.5}, "20": {"n": 1271, "moonshotN": 181, "pct": 14.2, "avg": 57.6}},
    "KDJ近3日內死亡交叉 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 521, "moonshotN": 39, "pct": 7.5, "avg": 63.4}, "20": {"n": 521, "moonshotN": 62, "pct": 11.9, "avg": 57.9}},
    "爆量(≥1.5倍均量) ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 857, "moonshotN": 56, "pct": 6.5, "avg": 48.6}, "20": {"n": 857, "moonshotN": 95, "pct": 11.1, "avg": 57.1}},
    "KDJ近3日內死亡交叉 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1089, "moonshotN": 76, "pct": 7.0, "avg": 53.7}, "20": {"n": 1089, "moonshotN": 142, "pct": 13.0, "avg": 54.2}},
    "近1季財報EPS大幅優於預期(≥10%) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2351, "moonshotN": 155, "pct": 6.6, "avg": 45.5}, "20": {"n": 2351, "moonshotN": 325, "pct": 13.8, "avg": 52.8}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1601, "moonshotN": 109, "pct": 6.8, "avg": 50.6}, "20": {"n": 1601, "moonshotN": 192, "pct": 12.0, "avg": 56.2}},
    "中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3113, "moonshotN": 212, "pct": 6.8, "avg": 53.4}, "20": {"n": 3113, "moonshotN": 434, "pct": 13.9, "avg": 55.0}},
    "爆量(≥2倍均量) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1123, "moonshotN": 72, "pct": 6.4, "avg": 63.8}, "20": {"n": 1123, "moonshotN": 115, "pct": 10.2, "avg": 59.4}},
    "多方力道≥80 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 770, "moonshotN": 51, "pct": 6.6, "avg": 63.2}, "20": {"n": 770, "moonshotN": 79, "pct": 10.3, "avg": 64.5}},
    "KDJ近3日內死亡交叉 ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 680, "moonshotN": 51, "pct": 7.5, "avg": 65.5}, "20": {"n": 680, "moonshotN": 78, "pct": 11.5, "avg": 62.0}},
    "中期強勢(12-1月勝大盤≥20%) ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1601, "moonshotN": 111, "pct": 6.9, "avg": 49.2}, "20": {"n": 1601, "moonshotN": 236, "pct": 14.7, "avg": 55.0}},
    "爆量(≥2倍均量) ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1254, "moonshotN": 83, "pct": 6.6, "avg": 57.1}, "20": {"n": 1254, "moonshotN": 136, "pct": 10.8, "avg": 59.0}},
    "KDJ近3日內黃金交叉 ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1270, "moonshotN": 79, "pct": 6.2, "avg": 52.2}, "20": {"n": 1270, "moonshotN": 123, "pct": 9.7, "avg": 56.7}},
    "多方力道≥80 ＋ 短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 76, "moonshotN": 4, "pct": 5.3, "avg": 82.3}, "20": {"n": 76, "moonshotN": 15, "pct": 19.7, "avg": 58.3}},
    "KDJ近3日內死亡交叉 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 293, "moonshotN": 22, "pct": 7.5, "avg": 70.4}, "20": {"n": 293, "moonshotN": 40, "pct": 13.7, "avg": 62.2}},
    "中期強勢(12-1月勝大盤≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1655, "moonshotN": 119, "pct": 7.2, "avg": 53.2}, "20": {"n": 1655, "moonshotN": 215, "pct": 13.0, "avg": 58.6}},
    "爆量(≥1.5倍均量) ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2199, "moonshotN": 140, "pct": 6.4, "avg": 52.2}, "20": {"n": 2199, "moonshotN": 251, "pct": 11.4, "avg": 56.9}},
    "爆量(≥1.5倍均量) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1990, "moonshotN": 127, "pct": 6.4, "avg": 57.5}, "20": {"n": 1990, "moonshotN": 221, "pct": 11.1, "avg": 56.8}},
    "KDJ近3日內黃金交叉 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1069, "moonshotN": 71, "pct": 6.6, "avg": 52.5}, "20": {"n": 1069, "moonshotN": 108, "pct": 10.1, "avg": 57.5}},
    "KDJ近3日內黃金交叉 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1319, "moonshotN": 84, "pct": 6.4, "avg": 51.0}, "20": {"n": 1319, "moonshotN": 152, "pct": 11.5, "avg": 54.8}},
    "KDJ近3日內黃金交叉 ＋ 量能區間高檔(≥90百分位) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1481, "moonshotN": 91, "pct": 6.1, "avg": 51.8}, "20": {"n": 1481, "moonshotN": 147, "pct": 9.9, "avg": 55.8}},
    "大盤站上20日均線 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3522, "moonshotN": 228, "pct": 6.5, "avg": 51.3}, "20": {"n": 3522, "moonshotN": 485, "pct": 13.8, "avg": 54.4}},
    "中期強勢(12-1月勝大盤≥20%) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3449, "moonshotN": 217, "pct": 6.3, "avg": 51.6}, "20": {"n": 3449, "moonshotN": 452, "pct": 13.1, "avg": 54.4}},
    "中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4134, "moonshotN": 254, "pct": 6.1, "avg": 51.3}, "20": {"n": 4134, "moonshotN": 530, "pct": 12.8, "avg": 54.0}},
    "相對強弱為正(強於大盤) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4119, "moonshotN": 253, "pct": 6.1, "avg": 51.3}, "20": {"n": 4119, "moonshotN": 529, "pct": 12.8, "avg": 54.0}},
    "多方力道≥80 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1444, "moonshotN": 87, "pct": 6.0, "avg": 59.4}, "20": {"n": 1444, "moonshotN": 158, "pct": 10.9, "avg": 59.2}},
    "爆量(≥1.5倍均量) ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1832, "moonshotN": 109, "pct": 5.9, "avg": 49.0}, "20": {"n": 1832, "moonshotN": 197, "pct": 10.8, "avg": 55.2}},
    "多方力道≥80 ＋ KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1533, "moonshotN": 93, "pct": 6.1, "avg": 55.1}, "20": {"n": 1533, "moonshotN": 155, "pct": 10.1, "avg": 53.2}},
    "近20日強於大盤≥10% ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4061, "moonshotN": 247, "pct": 6.1, "avg": 51.6}, "20": {"n": 4061, "moonshotN": 525, "pct": 12.9, "avg": 54.1}},
    "KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2494, "moonshotN": 147, "pct": 5.9, "avg": 53.8}, "20": {"n": 2494, "moonshotN": 236, "pct": 9.5, "avg": 56.0}},
    "爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2666, "moonshotN": 156, "pct": 5.9, "avg": 54.6}, "20": {"n": 2666, "moonshotN": 272, "pct": 10.2, "avg": 55.6}},
    "KDJ近3日內黃金交叉 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2488, "moonshotN": 147, "pct": 5.9, "avg": 53.8}, "20": {"n": 2488, "moonshotN": 236, "pct": 9.5, "avg": 56.0}},
    "KDJ近3日內黃金交叉 ＋ 近20日強於大盤≥10% ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2470, "moonshotN": 146, "pct": 5.9, "avg": 53.9}, "20": {"n": 2470, "moonshotN": 234, "pct": 9.5, "avg": 56.2}},
    "相對強弱為正(強於大盤) ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2660, "moonshotN": 156, "pct": 5.9, "avg": 54.6}, "20": {"n": 2660, "moonshotN": 272, "pct": 10.2, "avg": 55.6}},
    "爆量(≥1.5倍均量) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1769, "moonshotN": 105, "pct": 5.9, "avg": 56.2}, "20": {"n": 1769, "moonshotN": 169, "pct": 9.6, "avg": 55.6}},
    "爆量(≥2倍均量) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1173, "moonshotN": 68, "pct": 5.8, "avg": 60.1}, "20": {"n": 1173, "moonshotN": 109, "pct": 9.3, "avg": 60.7}},
    "大盤跌破60日均線 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 母子懷抱(低檔)剛形成": {"10": {"n": 87, "moonshotN": 6, "pct": 6.9, "avg": 45.0}, "20": {"n": 87, "moonshotN": 16, "pct": 18.4, "avg": 57.2}},
    "KDJ近3日內黃金交叉 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2074, "moonshotN": 128, "pct": 6.2, "avg": 51.1}, "20": {"n": 2074, "moonshotN": 213, "pct": 10.3, "avg": 55.8}},
    "近1季財報EPS優於預期 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2804, "moonshotN": 175, "pct": 6.2, "avg": 47.3}, "20": {"n": 2804, "moonshotN": 380, "pct": 13.6, "avg": 53.4}},
    "多方力道≥65 ＋ KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2100, "moonshotN": 125, "pct": 6.0, "avg": 55.2}, "20": {"n": 2100, "moonshotN": 203, "pct": 9.7, "avg": 54.6}},
    "多方力道≥65 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3579, "moonshotN": 214, "pct": 6.0, "avg": 50.5}, "20": {"n": 3579, "moonshotN": 443, "pct": 12.4, "avg": 53.1}},
    "近20日強於大盤≥10% ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2627, "moonshotN": 152, "pct": 5.8, "avg": 55.1}, "20": {"n": 2627, "moonshotN": 268, "pct": 10.2, "avg": 55.9}},
    "回後買上漲全通過 ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 826, "moonshotN": 48, "pct": 5.8, "avg": 51.9}, "20": {"n": 826, "moonshotN": 75, "pct": 9.1, "avg": 60.6}},
    "布林通道高檔(≥80%) ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2642, "moonshotN": 151, "pct": 5.7, "avg": 55.1}, "20": {"n": 2642, "moonshotN": 268, "pct": 10.1, "avg": 55.8}},
    "爆量(≥1.5倍均量) ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1254, "moonshotN": 71, "pct": 5.7, "avg": 46.9}, "20": {"n": 1254, "moonshotN": 143, "pct": 11.4, "avg": 53.3}},
    "爆量(≥2倍均量) ＋ 量能區間高檔(≥90百分位) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1502, "moonshotN": 86, "pct": 5.7, "avg": 58.7}, "20": {"n": 1502, "moonshotN": 140, "pct": 9.3, "avg": 57.8}},
    "KDJ近3日內黃金交叉 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1929, "moonshotN": 117, "pct": 6.1, "avg": 56.3}, "20": {"n": 1929, "moonshotN": 194, "pct": 10.1, "avg": 54.9}},
    "KDJ近3日內死亡交叉 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1701, "moonshotN": 104, "pct": 6.1, "avg": 52.5}, "20": {"n": 1701, "moonshotN": 189, "pct": 11.1, "avg": 53.8}},
    "大盤站上20日均線 ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2881, "moonshotN": 175, "pct": 6.1, "avg": 53.4}, "20": {"n": 2881, "moonshotN": 300, "pct": 10.4, "avg": 57.4}},
    "布林通道高檔(≥80%) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3904, "moonshotN": 235, "pct": 6.0, "avg": 51.8}, "20": {"n": 3904, "moonshotN": 501, "pct": 12.8, "avg": 54.2}},
    "爆量(≥1.5倍均量) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2277, "moonshotN": 136, "pct": 6.0, "avg": 55.3}, "20": {"n": 2277, "moonshotN": 243, "pct": 10.7, "avg": 56.4}},
    "KDJ近3日內黃金交叉 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2103, "moonshotN": 125, "pct": 5.9, "avg": 53.7}, "20": {"n": 2103, "moonshotN": 209, "pct": 9.9, "avg": 55.8}},
    "大盤站上60日均線 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3824, "moonshotN": 224, "pct": 5.9, "avg": 49.4}, "20": {"n": 3824, "moonshotN": 487, "pct": 12.7, "avg": 53.1}},
    "爆量(≥1.5倍均量) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2208, "moonshotN": 127, "pct": 5.8, "avg": 56.5}, "20": {"n": 2208, "moonshotN": 209, "pct": 9.5, "avg": 57.5}},
    "爆量(≥2倍均量) ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1066, "moonshotN": 62, "pct": 5.8, "avg": 52.1}, "20": {"n": 1066, "moonshotN": 107, "pct": 10.0, "avg": 58.3}},
    "爆量(≥2倍均量) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1296, "moonshotN": 75, "pct": 5.8, "avg": 61.4}, "20": {"n": 1296, "moonshotN": 126, "pct": 9.7, "avg": 59.5}},
    "量能區間高檔(≥90百分位) ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3307, "moonshotN": 192, "pct": 5.8, "avg": 53.0}, "20": {"n": 3307, "moonshotN": 362, "pct": 10.9, "avg": 54.6}},
    "多方力道≥65 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1204, "moonshotN": 68, "pct": 5.6, "avg": 59.2}, "20": {"n": 1204, "moonshotN": 108, "pct": 9.0, "avg": 61.0}},
    "KDJ近3日內黃金交叉 ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1198, "moonshotN": 73, "pct": 6.1, "avg": 57.8}, "20": {"n": 1198, "moonshotN": 112, "pct": 9.3, "avg": 59.2}},
    "KDJ近3日內死亡交叉 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1534, "moonshotN": 93, "pct": 6.1, "avg": 58.4}, "20": {"n": 1534, "moonshotN": 174, "pct": 11.3, "avg": 53.6}},
    "布林通道高檔(≥80%) ＋ KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2391, "moonshotN": 141, "pct": 5.9, "avg": 53.6}, "20": {"n": 2391, "moonshotN": 224, "pct": 9.4, "avg": 56.8}},
    "爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1535, "moonshotN": 89, "pct": 5.8, "avg": 59.6}, "20": {"n": 1535, "moonshotN": 143, "pct": 9.3, "avg": 58.3}},
    "相對強弱為正(強於大盤) ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1531, "moonshotN": 89, "pct": 5.8, "avg": 59.6}, "20": {"n": 1531, "moonshotN": 143, "pct": 9.3, "avg": 58.3}},
    "近20日強於大盤≥10% ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1511, "moonshotN": 88, "pct": 5.8, "avg": 59.9}, "20": {"n": 1511, "moonshotN": 141, "pct": 9.3, "avg": 58.6}},
    "強勢突破盤 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2206, "moonshotN": 126, "pct": 5.7, "avg": 51.9}, "20": {"n": 2206, "moonshotN": 275, "pct": 12.5, "avg": 53.9}},
    "KDJ近3日內死亡交叉 ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 560, "moonshotN": 34, "pct": 6.1, "avg": 53.1}, "20": {"n": 560, "moonshotN": 62, "pct": 11.1, "avg": 55.3}},
    "連續放量(近3日均量≥1.5倍前20日均量) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2594, "moonshotN": 152, "pct": 5.9, "avg": 58.2}, "20": {"n": 2594, "moonshotN": 261, "pct": 10.1, "avg": 58.0}},
    "爆量(≥2倍均量) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1425, "moonshotN": 83, "pct": 5.8, "avg": 61.3}, "20": {"n": 1425, "moonshotN": 128, "pct": 9.0, "avg": 59.6}},
    "多方力道≥65 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2175, "moonshotN": 125, "pct": 5.7, "avg": 54.9}, "20": {"n": 2175, "moonshotN": 218, "pct": 10.0, "avg": 56.9}},
    "MACD近3日內黃金交叉 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 467, "moonshotN": 26, "pct": 5.6, "avg": 43.7}, "20": {"n": 467, "moonshotN": 53, "pct": 11.3, "avg": 47.9}},
    "KDJ近3日內死亡交叉 ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1078, "moonshotN": 60, "pct": 5.6, "avg": 53.6}, "20": {"n": 1078, "moonshotN": 115, "pct": 10.7, "avg": 50.2}},
    "爆量(≥1.5倍均量) ＋ 量能區間高檔(≥90百分位) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2419, "moonshotN": 133, "pct": 5.5, "avg": 53.9}, "20": {"n": 2419, "moonshotN": 242, "pct": 10.0, "avg": 55.8}},
    "多方力道≥80 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 348, "moonshotN": 18, "pct": 5.2, "avg": 45.8}, "20": {"n": 348, "moonshotN": 35, "pct": 10.1, "avg": 45.0}},
    "KDJ近3日內死亡交叉 ＋ 量能區間高檔(≥90百分位) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 904, "moonshotN": 56, "pct": 6.2, "avg": 58.0}, "20": {"n": 904, "moonshotN": 104, "pct": 11.5, "avg": 56.0}},
    "爆量(≥2倍均量) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1103, "moonshotN": 64, "pct": 5.8, "avg": 61.1}, "20": {"n": 1103, "moonshotN": 98, "pct": 8.9, "avg": 60.8}},
    "KDJ近3日內黃金交叉 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 670, "moonshotN": 38, "pct": 5.7, "avg": 50.8}, "20": {"n": 670, "moonshotN": 64, "pct": 9.6, "avg": 58.1}},
    "連續放量(近3日均量≥1.5倍前20日均量) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1960, "moonshotN": 112, "pct": 5.7, "avg": 57.0}, "20": {"n": 1960, "moonshotN": 180, "pct": 9.2, "avg": 56.6}},
    "量能區間高檔(≥90百分位) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2978, "moonshotN": 167, "pct": 5.6, "avg": 55.0}, "20": {"n": 2978, "moonshotN": 336, "pct": 11.3, "avg": 54.1}},
    "爆量(≥2倍均量) ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 692, "moonshotN": 36, "pct": 5.2, "avg": 48.1}, "20": {"n": 692, "moonshotN": 70, "pct": 10.1, "avg": 52.2}},
    "漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 85, "moonshotN": 5, "pct": 5.9, "avg": 74.9}, "20": {"n": 85, "moonshotN": 15, "pct": 17.6, "avg": 61.0}},
    "大盤站上60日均線 ＋ 短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 93, "moonshotN": 5, "pct": 5.4, "avg": 77.6}, "20": {"n": 93, "moonshotN": 16, "pct": 17.2, "avg": 58.4}},
    "量能區間高檔(≥90百分位) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1980, "moonshotN": 125, "pct": 6.3, "avg": 50.5}, "20": {"n": 1980, "moonshotN": 259, "pct": 13.1, "avg": 54.7}},
    "多方力道≥80 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2576, "moonshotN": 156, "pct": 6.1, "avg": 52.6}, "20": {"n": 2576, "moonshotN": 327, "pct": 12.7, "avg": 53.7}},
    "布林通道高檔(≥80%) ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1527, "moonshotN": 88, "pct": 5.8, "avg": 59.9}, "20": {"n": 1527, "moonshotN": 143, "pct": 9.4, "avg": 58.3}},
    "中期強勢(12-1月勝大盤≥20%) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2071, "moonshotN": 120, "pct": 5.8, "avg": 52.3}, "20": {"n": 2071, "moonshotN": 261, "pct": 12.6, "avg": 53.6}},
    "爆量(≥2倍均量) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1235, "moonshotN": 70, "pct": 5.7, "avg": 59.8}, "20": {"n": 1235, "moonshotN": 106, "pct": 8.6, "avg": 59.3}},
    "中期強勢(12-1月勝大盤≥20%) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3605, "moonshotN": 204, "pct": 5.7, "avg": 50.4}, "20": {"n": 3605, "moonshotN": 433, "pct": 12.0, "avg": 52.6}},
    "KDJ近3日內黃金交叉 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2285, "moonshotN": 127, "pct": 5.6, "avg": 50.2}, "20": {"n": 2285, "moonshotN": 216, "pct": 9.5, "avg": 55.0}},
    "KDJ近3日內黃金交叉 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2117, "moonshotN": 119, "pct": 5.6, "avg": 53.1}, "20": {"n": 2117, "moonshotN": 198, "pct": 9.4, "avg": 54.8}},
    "連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3496, "moonshotN": 193, "pct": 5.5, "avg": 55.6}, "20": {"n": 3496, "moonshotN": 321, "pct": 9.2, "avg": 56.7}},
    "相對強弱為正(強於大盤) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3489, "moonshotN": 193, "pct": 5.5, "avg": 55.6}, "20": {"n": 3489, "moonshotN": 321, "pct": 9.2, "avg": 56.7}},
    "近20日強於大盤≥10% ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 夜星剛形成": {"10": {"n": 217, "moonshotN": 12, "pct": 5.5, "avg": 45.2}, "20": {"n": 217, "moonshotN": 19, "pct": 8.8, "avg": 52.6}},
    "布林通道高檔(≥80%) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3463, "moonshotN": 188, "pct": 5.4, "avg": 56.0}, "20": {"n": 3463, "moonshotN": 320, "pct": 9.2, "avg": 56.8}},
    "爆量(≥1.5倍均量) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1887, "moonshotN": 101, "pct": 5.4, "avg": 54.8}, "20": {"n": 1887, "moonshotN": 168, "pct": 8.9, "avg": 56.7}},
    "爆量(≥1.5倍均量) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2083, "moonshotN": 113, "pct": 5.4, "avg": 55.5}, "20": {"n": 2083, "moonshotN": 206, "pct": 9.9, "avg": 56.1}},
    "爆量(≥2倍均量) ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1387, "moonshotN": 75, "pct": 5.4, "avg": 51.4}, "20": {"n": 1387, "moonshotN": 129, "pct": 9.3, "avg": 58.7}},
    "強勢突破盤 ＋ KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1477, "moonshotN": 79, "pct": 5.3, "avg": 57.1}, "20": {"n": 1477, "moonshotN": 122, "pct": 8.3, "avg": 56.6}},
    "相對強弱為正(強於大盤) ＋ 量能區間高檔(≥90百分位) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4026, "moonshotN": 212, "pct": 5.3, "avg": 52.8}, "20": {"n": 4026, "moonshotN": 411, "pct": 10.2, "avg": 53.4}},
    "量能區間高檔(≥90百分位) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2338, "moonshotN": 120, "pct": 5.1, "avg": 52.6}, "20": {"n": 2338, "moonshotN": 221, "pct": 9.5, "avg": 53.0}},
    "回後買上漲全通過 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1582, "moonshotN": 79, "pct": 5.0, "avg": 51.8}, "20": {"n": 1582, "moonshotN": 160, "pct": 10.1, "avg": 54.3}},
    "爆量(≥2倍均量) ＋ 近1季財報EPS低於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 382, "moonshotN": 24, "pct": 6.3, "avg": 71.5}, "20": {"n": 382, "moonshotN": 28, "pct": 7.3, "avg": 56.1}},
    "多方力道≥80 ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2038, "moonshotN": 116, "pct": 5.7, "avg": 59.2}, "20": {"n": 2038, "moonshotN": 209, "pct": 10.3, "avg": 58.9}},
    "爆量(≥2倍均量) ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 524, "moonshotN": 30, "pct": 5.7, "avg": 52.6}, "20": {"n": 524, "moonshotN": 48, "pct": 9.2, "avg": 60.4}},
    "多方力道≥65 ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2961, "moonshotN": 166, "pct": 5.6, "avg": 55.5}, "20": {"n": 2961, "moonshotN": 279, "pct": 9.4, "avg": 57.0}},
    "近20日強於大盤≥10% ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3441, "moonshotN": 188, "pct": 5.5, "avg": 56.0}, "20": {"n": 3441, "moonshotN": 318, "pct": 9.2, "avg": 56.9}},
    "近1季財報EPS優於預期 ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2462, "moonshotN": 133, "pct": 5.4, "avg": 47.8}, "20": {"n": 2462, "moonshotN": 232, "pct": 9.4, "avg": 55.1}},
    "量能區間高檔(≥90百分位) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4044, "moonshotN": 213, "pct": 5.3, "avg": 52.7}, "20": {"n": 4044, "moonshotN": 412, "pct": 10.2, "avg": 53.4}},
    "半年強勢(6-1月勝大盤≥10%) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 207, "moonshotN": 11, "pct": 5.3, "avg": 58.3}, "20": {"n": 207, "moonshotN": 29, "pct": 14.0, "avg": 54.3}},
    "近20日強於大盤≥10% ＋ 量能區間高檔(≥90百分位) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3984, "moonshotN": 208, "pct": 5.2, "avg": 53.1}, "20": {"n": 3984, "moonshotN": 408, "pct": 10.2, "avg": 53.5}},
    "量能區間高檔(≥90百分位) ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2160, "moonshotN": 111, "pct": 5.1, "avg": 47.9}, "20": {"n": 2160, "moonshotN": 220, "pct": 10.2, "avg": 55.3}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1559, "moonshotN": 78, "pct": 5.0, "avg": 46.2}, "20": {"n": 1559, "moonshotN": 149, "pct": 9.6, "avg": 52.4}},
    "大盤站上20日均線 ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1566, "moonshotN": 78, "pct": 5.0, "avg": 49.7}, "20": {"n": 1566, "moonshotN": 167, "pct": 10.7, "avg": 54.2}},
    "近1季財報EPS低於預期 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 333, "moonshotN": 29, "pct": 8.7, "avg": 58.9}, "20": {"n": 333, "moonshotN": 32, "pct": 9.6, "avg": 61.0}},
    "量能區間高檔(≥90百分位) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 299, "moonshotN": 22, "pct": 7.4, "avg": 51.3}, "20": {"n": 299, "moonshotN": 29, "pct": 9.7, "avg": 54.8}},
    "KDJ近3日內死亡交叉 ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1373, "moonshotN": 81, "pct": 5.9, "avg": 46.4}, "20": {"n": 1373, "moonshotN": 147, "pct": 10.7, "avg": 50.3}},
    "連續放量(近3日均量≥1.5倍前20日均量) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2974, "moonshotN": 169, "pct": 5.7, "avg": 56.1}, "20": {"n": 2974, "moonshotN": 289, "pct": 9.7, "avg": 57.5}},
    "回後買上漲全通過 ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 949, "moonshotN": 53, "pct": 5.6, "avg": 48.8}, "20": {"n": 949, "moonshotN": 105, "pct": 11.1, "avg": 53.3}},
    "KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2033, "moonshotN": 112, "pct": 5.5, "avg": 55.4}, "20": {"n": 2033, "moonshotN": 207, "pct": 10.2, "avg": 52.7}},
    "KDJ近3日內死亡交叉 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2031, "moonshotN": 112, "pct": 5.5, "avg": 55.4}, "20": {"n": 2031, "moonshotN": 207, "pct": 10.2, "avg": 52.7}},
    "KDJ近3日內死亡交叉 ＋ 近20日強於大盤≥10% ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2025, "moonshotN": 111, "pct": 5.5, "avg": 55.5}, "20": {"n": 2025, "moonshotN": 205, "pct": 10.1, "avg": 52.8}},
    "布林通道高檔(≥80%) ＋ KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1800, "moonshotN": 98, "pct": 5.4, "avg": 56.4}, "20": {"n": 1800, "moonshotN": 174, "pct": 9.7, "avg": 53.5}},
    "量能區間高檔(≥90百分位) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3465, "moonshotN": 187, "pct": 5.4, "avg": 53.6}, "20": {"n": 3465, "moonshotN": 366, "pct": 10.6, "avg": 54.1}},
    "半年強勢(6-1月勝大盤≥10%) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1614, "moonshotN": 84, "pct": 5.2, "avg": 50.2}, "20": {"n": 1614, "moonshotN": 159, "pct": 9.9, "avg": 55.3}},
    "量能區間高檔(≥90百分位) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3359, "moonshotN": 170, "pct": 5.1, "avg": 54.4}, "20": {"n": 3359, "moonshotN": 356, "pct": 10.6, "avg": 53.4}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2899, "moonshotN": 149, "pct": 5.1, "avg": 50.3}, "20": {"n": 2899, "moonshotN": 261, "pct": 9.0, "avg": 56.4}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 804, "moonshotN": 39, "pct": 4.9, "avg": 49.8}, "20": {"n": 804, "moonshotN": 68, "pct": 8.5, "avg": 58.3}},
    "回後買上漲全通過 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1524, "moonshotN": 73, "pct": 4.8, "avg": 53.2}, "20": {"n": 1524, "moonshotN": 156, "pct": 10.2, "avg": 54.6}},
    "均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 99, "moonshotN": 4, "pct": 4.0, "avg": 82.3}, "20": {"n": 99, "moonshotN": 17, "pct": 17.2, "avg": 58.6}},
    "多方力道≥65 ＋ 短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 100, "moonshotN": 6, "pct": 6.0, "avg": 70.4}, "20": {"n": 100, "moonshotN": 17, "pct": 17.0, "avg": 58.6}},
    "大盤站上20日均線 ＋ 短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 91, "moonshotN": 5, "pct": 5.5, "avg": 77.6}, "20": {"n": 91, "moonshotN": 15, "pct": 16.5, "avg": 60.2}},
    "中期強勢(12-1月勝大盤≥20%) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 夜星剛形成": {"10": {"n": 188, "moonshotN": 11, "pct": 5.9, "avg": 43.4}, "20": {"n": 188, "moonshotN": 16, "pct": 8.5, "avg": 51.3}},
    "多方力道≥65 ＋ KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1886, "moonshotN": 105, "pct": 5.6, "avg": 55.1}, "20": {"n": 1886, "moonshotN": 193, "pct": 10.2, "avg": 53.2}},
    "爆量(≥1.5倍均量) ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2422, "moonshotN": 132, "pct": 5.5, "avg": 48.7}, "20": {"n": 2422, "moonshotN": 249, "pct": 10.3, "avg": 56.1}},
    "大盤站上20日均線 ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3382, "moonshotN": 187, "pct": 5.5, "avg": 52.2}, "20": {"n": 3382, "moonshotN": 398, "pct": 11.8, "avg": 52.4}},
    "多方力道≥80 ＋ KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1569, "moonshotN": 85, "pct": 5.4, "avg": 57.7}, "20": {"n": 1569, "moonshotN": 165, "pct": 10.5, "avg": 53.2}},
    "多方力道≥80 ＋ 量能區間高檔(≥90百分位) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2545, "moonshotN": 138, "pct": 5.4, "avg": 55.3}, "20": {"n": 2545, "moonshotN": 293, "pct": 11.5, "avg": 55.0}},
    "量能區間高檔(≥90百分位) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2647, "moonshotN": 142, "pct": 5.4, "avg": 54.8}, "20": {"n": 2647, "moonshotN": 242, "pct": 9.1, "avg": 57.3}},
    "多方力道≥65 ＋ 量能區間高檔(≥90百分位) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3444, "moonshotN": 181, "pct": 5.3, "avg": 53.2}, "20": {"n": 3444, "moonshotN": 357, "pct": 10.4, "avg": 53.8}},
    "大盤站上20日均線 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5294, "moonshotN": 279, "pct": 5.3, "avg": 53.3}, "20": {"n": 5294, "moonshotN": 560, "pct": 10.6, "avg": 54.0}},
    "多方力道≥80 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 411, "moonshotN": 21, "pct": 5.1, "avg": 50.1}, "20": {"n": 411, "moonshotN": 39, "pct": 9.5, "avg": 46.1}},
    "量能區間高檔(≥90百分位) ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1106, "moonshotN": 56, "pct": 5.1, "avg": 49.2}, "20": {"n": 1106, "moonshotN": 113, "pct": 10.2, "avg": 54.9}},
    "多方力道≥80 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4497, "moonshotN": 226, "pct": 5.0, "avg": 53.6}, "20": {"n": 4497, "moonshotN": 469, "pct": 10.4, "avg": 53.1}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2530, "moonshotN": 127, "pct": 5.0, "avg": 56.2}, "20": {"n": 2530, "moonshotN": 221, "pct": 8.7, "avg": 57.7}},
    "量能區間高檔(≥90百分位) ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2752, "moonshotN": 135, "pct": 4.9, "avg": 48.0}, "20": {"n": 2752, "moonshotN": 283, "pct": 10.3, "avg": 52.5}},
    "布林通道高檔(≥80%) ＋ 量能區間高檔(≥90百分位) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3852, "moonshotN": 185, "pct": 4.8, "avg": 52.5}, "20": {"n": 3852, "moonshotN": 374, "pct": 9.7, "avg": 54.3}},
    "回後買上漲全通過 ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1237, "moonshotN": 59, "pct": 4.8, "avg": 50.7}, "20": {"n": 1237, "moonshotN": 116, "pct": 9.4, "avg": 51.9}},
    "半年強勢(6-1月勝大盤≥10%) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 812, "moonshotN": 62, "pct": 7.6, "avg": 51.1}, "20": {"n": 812, "moonshotN": 85, "pct": 10.5, "avg": 53.0}},
    "KDJ近3日內黃金交叉 ＋ KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 833, "moonshotN": 52, "pct": 6.2, "avg": 55.4}, "20": {"n": 833, "moonshotN": 75, "pct": 9.0, "avg": 51.7}},
    "KDJ近3日內死亡交叉 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1780, "moonshotN": 98, "pct": 5.5, "avg": 54.7}, "20": {"n": 1780, "moonshotN": 183, "pct": 10.3, "avg": 53.7}},
    "KDJ近3日內死亡交叉 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1883, "moonshotN": 102, "pct": 5.4, "avg": 51.4}, "20": {"n": 1883, "moonshotN": 193, "pct": 10.2, "avg": 52.7}},
    "KDJ近3日內死亡交叉 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1843, "moonshotN": 99, "pct": 5.4, "avg": 52.5}, "20": {"n": 1843, "moonshotN": 190, "pct": 10.3, "avg": 52.9}},
    "半年強勢(6-1月勝大盤≥10%) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2969, "moonshotN": 161, "pct": 5.4, "avg": 54.6}, "20": {"n": 2969, "moonshotN": 334, "pct": 11.2, "avg": 52.6}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2720, "moonshotN": 143, "pct": 5.3, "avg": 53.4}, "20": {"n": 2720, "moonshotN": 224, "pct": 8.2, "avg": 57.2}},
    "均線多頭排列(5>20>60且站上月線) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2793, "moonshotN": 144, "pct": 5.2, "avg": 55.9}, "20": {"n": 2793, "moonshotN": 254, "pct": 9.1, "avg": 57.1}},
    "爆量(≥2倍均量) ＋ 創52週新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 870, "moonshotN": 44, "pct": 5.1, "avg": 58.6}, "20": {"n": 870, "moonshotN": 78, "pct": 9.0, "avg": 56.6}},
    "大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6937, "moonshotN": 347, "pct": 5.0, "avg": 51.5}, "20": {"n": 6937, "moonshotN": 699, "pct": 10.1, "avg": 53.3}},
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6166, "moonshotN": 310, "pct": 5.0, "avg": 54.9}, "20": {"n": 6166, "moonshotN": 614, "pct": 10.0, "avg": 53.3}},
    "多方力道≥80 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4323, "moonshotN": 217, "pct": 5.0, "avg": 55.4}, "20": {"n": 4323, "moonshotN": 445, "pct": 10.3, "avg": 53.1}},
    "相對強弱為正(強於大盤) ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6916, "moonshotN": 346, "pct": 5.0, "avg": 51.5}, "20": {"n": 6916, "moonshotN": 697, "pct": 10.1, "avg": 53.4}},
    "相對強弱為正(強於大盤) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6162, "moonshotN": 310, "pct": 5.0, "avg": 54.9}, "20": {"n": 6162, "moonshotN": 613, "pct": 9.9, "avg": 53.4}},
    "相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 8204, "moonshotN": 391, "pct": 4.8, "avg": 52.4}, "20": {"n": 8204, "moonshotN": 769, "pct": 9.4, "avg": 52.9}},
    "大盤站上20日均線 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5798, "moonshotN": 281, "pct": 4.8, "avg": 52.6}, "20": {"n": 5798, "moonshotN": 580, "pct": 10.0, "avg": 52.7}},
    "近1季財報EPS低於預期 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 585, "moonshotN": 27, "pct": 4.6, "avg": 46.4}, "20": {"n": 585, "moonshotN": 68, "pct": 11.6, "avg": 49.6}},
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 83, "moonshotN": 3, "pct": 3.6, "avg": 96.9}, "20": {"n": 83, "moonshotN": 13, "pct": 15.7, "avg": 63.7}},
    "KDJ近3日內黃金交叉 ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 919, "moonshotN": 57, "pct": 6.2, "avg": 54.9}, "20": {"n": 919, "moonshotN": 89, "pct": 9.7, "avg": 58.0}},
    "爆量(≥1.5倍均量) ＋ 近1季財報EPS大幅優於預期(≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1457, "moonshotN": 84, "pct": 5.8, "avg": 46.7}, "20": {"n": 1457, "moonshotN": 161, "pct": 11.1, "avg": 53.4}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 近1季財報EPS低於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 820, "moonshotN": 46, "pct": 5.6, "avg": 64.6}, "20": {"n": 820, "moonshotN": 64, "pct": 7.8, "avg": 55.5}},
    "量能區間高檔(≥90百分位) ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3636, "moonshotN": 185, "pct": 5.1, "avg": 50.4}, "20": {"n": 3636, "moonshotN": 370, "pct": 10.2, "avg": 53.9}},
    "多方力道≥65 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6053, "moonshotN": 303, "pct": 5.0, "avg": 52.1}, "20": {"n": 6053, "moonshotN": 600, "pct": 9.9, "avg": 53.0}},
    "近20日強於大盤≥10% ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6839, "moonshotN": 339, "pct": 5.0, "avg": 51.7}, "20": {"n": 6839, "moonshotN": 692, "pct": 10.1, "avg": 53.5}},
    "近20日強於大盤≥10% ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6130, "moonshotN": 305, "pct": 5.0, "avg": 55.2}, "20": {"n": 6130, "moonshotN": 609, "pct": 9.9, "avg": 53.5}},
    "漲時量≥跌時量1.5倍(近20日) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5503, "moonshotN": 273, "pct": 5.0, "avg": 55.2}, "20": {"n": 5503, "moonshotN": 555, "pct": 10.1, "avg": 53.7}},
    "大盤站上20日均線 ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2834, "moonshotN": 140, "pct": 4.9, "avg": 48.9}, "20": {"n": 2834, "moonshotN": 301, "pct": 10.6, "avg": 54.7}},
    "均線多頭排列(5>20>60且站上月線) ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2505, "moonshotN": 123, "pct": 4.9, "avg": 49.2}, "20": {"n": 2505, "moonshotN": 256, "pct": 10.2, "avg": 54.0}},
    "近3日向上跳空缺口 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2375, "moonshotN": 117, "pct": 4.9, "avg": 50.9}, "20": {"n": 2375, "moonshotN": 252, "pct": 10.6, "avg": 55.5}},
    "短線過熱(高於月線≥20%)": {"10": {"n": 8229, "moonshotN": 392, "pct": 4.8, "avg": 52.3}, "20": {"n": 8229, "moonshotN": 771, "pct": 9.4, "avg": 52.9}},
    "布林通道高檔(≥80%) ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6576, "moonshotN": 316, "pct": 4.8, "avg": 51.2}, "20": {"n": 6576, "moonshotN": 642, "pct": 9.8, "avg": 54.1}},
    "KDJ近3日內黃金交叉 ＋ 創52週新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1217, "moonshotN": 58, "pct": 4.8, "avg": 51.1}, "20": {"n": 1217, "moonshotN": 117, "pct": 9.6, "avg": 54.2}},
    "大盤站上20日均線 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6796, "moonshotN": 328, "pct": 4.8, "avg": 49.5}, "20": {"n": 6796, "moonshotN": 678, "pct": 10.0, "avg": 52.5}},
    "大盤站上20日均線 ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4850, "moonshotN": 233, "pct": 4.8, "avg": 46.9}, "20": {"n": 4850, "moonshotN": 496, "pct": 10.2, "avg": 52.6}},
    "近20日強於大盤≥10% ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 8120, "moonshotN": 384, "pct": 4.7, "avg": 52.6}, "20": {"n": 8120, "moonshotN": 764, "pct": 9.4, "avg": 53.0}},
    "近20日強於大盤≥10% ＋ 短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 103, "moonshotN": 6, "pct": 5.8, "avg": 70.4}, "20": {"n": 103, "moonshotN": 17, "pct": 16.5, "avg": 58.6}},
    "相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 104, "moonshotN": 6, "pct": 5.8, "avg": 70.4}, "20": {"n": 104, "moonshotN": 17, "pct": 16.3, "avg": 58.6}},
    "短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 105, "moonshotN": 6, "pct": 5.7, "avg": 70.4}, "20": {"n": 105, "moonshotN": 17, "pct": 16.2, "avg": 58.6}},
    "近20日強於大盤≥10% ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 1287, "moonshotN": 78, "pct": 6.1, "avg": 49.5}, "20": {"n": 1287, "moonshotN": 128, "pct": 9.9, "avg": 51.1}},
    "近1季財報EPS低於預期 ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 854, "moonshotN": 50, "pct": 5.9, "avg": 67.7}, "20": {"n": 854, "moonshotN": 68, "pct": 8.0, "avg": 57.5}},
    "KDJ近3日內黃金交叉 ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1285, "moonshotN": 73, "pct": 5.7, "avg": 46.7}, "20": {"n": 1285, "moonshotN": 125, "pct": 9.7, "avg": 53.8}},
    "多方力道≥80 ＋ 大盤站上60日均線 ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 426, "moonshotN": 22, "pct": 5.2, "avg": 46.7}, "20": {"n": 426, "moonshotN": 38, "pct": 8.9, "avg": 43.8}},
    "KDJ近3日內黃金交叉 ＋ 近1季財報EPS低於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 607, "moonshotN": 31, "pct": 5.1, "avg": 70.5}, "20": {"n": 607, "moonshotN": 59, "pct": 9.7, "avg": 56.1}},
    "近20日強於大盤≥10% ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4002, "moonshotN": 205, "pct": 5.1, "avg": 51.6}, "20": {"n": 4002, "moonshotN": 442, "pct": 11.0, "avg": 52.5}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1835, "moonshotN": 93, "pct": 5.1, "avg": 55.8}, "20": {"n": 1835, "moonshotN": 149, "pct": 8.1, "avg": 56.2}},
    "半年強勢(6-1月勝大盤≥10%) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3382, "moonshotN": 171, "pct": 5.1, "avg": 52.5}, "20": {"n": 3382, "moonshotN": 375, "pct": 11.1, "avg": 52.4}},
    "回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1874, "moonshotN": 94, "pct": 5.0, "avg": 50.3}, "20": {"n": 1874, "moonshotN": 182, "pct": 9.7, "avg": 53.8}},
    "多方力道≥65 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5586, "moonshotN": 278, "pct": 5.0, "avg": 54.2}, "20": {"n": 5586, "moonshotN": 551, "pct": 9.9, "avg": 52.8}},
    "布林通道高檔(≥80%) ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1869, "moonshotN": 93, "pct": 5.0, "avg": 50.5}, "20": {"n": 1869, "moonshotN": 181, "pct": 9.7, "avg": 53.9}},
    "相對強弱為正(強於大盤) ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1871, "moonshotN": 94, "pct": 5.0, "avg": 50.3}, "20": {"n": 1871, "moonshotN": 181, "pct": 9.7, "avg": 54.0}},
    "大盤站上20日均線 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5899, "moonshotN": 294, "pct": 5.0, "avg": 52.7}, "20": {"n": 5899, "moonshotN": 615, "pct": 10.4, "avg": 53.7}},
    "多方力道≥65 ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2615, "moonshotN": 128, "pct": 4.9, "avg": 48.9}, "20": {"n": 2615, "moonshotN": 262, "pct": 10.0, "avg": 54.1}},
    "KDJ近3日內黃金交叉 ＋ 距52週高點≤5% ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1352, "moonshotN": 65, "pct": 4.8, "avg": 53.2}, "20": {"n": 1352, "moonshotN": 120, "pct": 8.9, "avg": 55.7}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3466, "moonshotN": 164, "pct": 4.7, "avg": 53.4}, "20": {"n": 3466, "moonshotN": 278, "pct": 8.0, "avg": 55.9}},
    "多方力道≥80 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5072, "moonshotN": 237, "pct": 4.7, "avg": 53.2}, "20": {"n": 5072, "moonshotN": 499, "pct": 9.8, "avg": 52.1}},
    "布林通道高檔(≥80%) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5840, "moonshotN": 275, "pct": 4.7, "avg": 55.3}, "20": {"n": 5840, "moonshotN": 555, "pct": 9.5, "avg": 54.1}},
    "相對強弱為正(強於大盤) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3453, "moonshotN": 163, "pct": 4.7, "avg": 53.5}, "20": {"n": 3453, "moonshotN": 277, "pct": 8.0, "avg": 56.0}},
    "相對強弱為正(強於大盤) ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3275, "moonshotN": 153, "pct": 4.7, "avg": 48.6}, "20": {"n": 3275, "moonshotN": 324, "pct": 9.9, "avg": 54.2}},
    "近20日強於大盤≥10% ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3229, "moonshotN": 152, "pct": 4.7, "avg": 48.7}, "20": {"n": 3229, "moonshotN": 322, "pct": 10.0, "avg": 54.3}},
    "近1季財報EPS優於預期 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4834, "moonshotN": 228, "pct": 4.7, "avg": 46.3}, "20": {"n": 4834, "moonshotN": 481, "pct": 10.0, "avg": 52.0}},
    "近1季財報EPS優於預期 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4360, "moonshotN": 207, "pct": 4.7, "avg": 46.7}, "20": {"n": 4360, "moonshotN": 440, "pct": 10.1, "avg": 52.5}},
    "均線多頭排列(5>20>60且站上月線) ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5378, "moonshotN": 255, "pct": 4.7, "avg": 54.1}, "20": {"n": 5378, "moonshotN": 532, "pct": 9.9, "avg": 52.3}},
    "CMF資金流買方佔優(近20日≥0.1) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3179, "moonshotN": 150, "pct": 4.7, "avg": 56.2}, "20": {"n": 3179, "moonshotN": 304, "pct": 9.6, "avg": 53.7}},
    "布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 7769, "moonshotN": 354, "pct": 4.6, "avg": 52.5}, "20": {"n": 7769, "moonshotN": 701, "pct": 9.0, "avg": 53.6}},
    "近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3291, "moonshotN": 153, "pct": 4.6, "avg": 48.6}, "20": {"n": 3291, "moonshotN": 324, "pct": 9.8, "avg": 54.2}},
    "多方力道≥65 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6419, "moonshotN": 293, "pct": 4.6, "avg": 52.5}, "20": {"n": 6419, "moonshotN": 601, "pct": 9.4, "avg": 52.1}},
    "多方力道≥80 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4889, "moonshotN": 225, "pct": 4.6, "avg": 54.7}, "20": {"n": 4889, "moonshotN": 480, "pct": 9.8, "avg": 52.6}},
    "布林通道高檔(≥80%) ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 7747, "moonshotN": 354, "pct": 4.6, "avg": 52.5}, "20": {"n": 7747, "moonshotN": 699, "pct": 9.0, "avg": 53.7}},
    "布林通道高檔(≥80%) ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3271, "moonshotN": 150, "pct": 4.6, "avg": 48.9}, "20": {"n": 3271, "moonshotN": 321, "pct": 9.8, "avg": 54.3}},
    "近20日強於大盤≥10% ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5636, "moonshotN": 258, "pct": 4.6, "avg": 45.7}, "20": {"n": 5636, "moonshotN": 532, "pct": 9.4, "avg": 52.2}},
    "均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6873, "moonshotN": 312, "pct": 4.5, "avg": 51.9}, "20": {"n": 6873, "moonshotN": 643, "pct": 9.4, "avg": 52.3}},
    "布林通道高檔(≥80%) ＋ 近20日強於大盤≥10% ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 7665, "moonshotN": 348, "pct": 4.5, "avg": 52.7}, "20": {"n": 7665, "moonshotN": 695, "pct": 9.1, "avg": 53.8}},
    "相對強弱為正(強於大盤) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6868, "moonshotN": 312, "pct": 4.5, "avg": 51.9}, "20": {"n": 6868, "moonshotN": 643, "pct": 9.4, "avg": 52.3}},
    "大盤站上60日均線 ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1708, "moonshotN": 76, "pct": 4.4, "avg": 48.7}, "20": {"n": 1708, "moonshotN": 167, "pct": 9.8, "avg": 53.6}},
    "近1季財報EPS優於預期 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4808, "moonshotN": 208, "pct": 4.3, "avg": 45.4}, "20": {"n": 4808, "moonshotN": 449, "pct": 9.3, "avg": 50.6}},
    "近1季財報EPS大幅優於預期(≥10%) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 夜星剛形成": {"10": {"n": 270, "moonshotN": 15, "pct": 5.6, "avg": 42.4}, "20": {"n": 270, "moonshotN": 29, "pct": 10.7, "avg": 53.6}},
    "爆量(≥2倍均量) ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1155, "moonshotN": 61, "pct": 5.3, "avg": 49.9}, "20": {"n": 1155, "moonshotN": 107, "pct": 9.3, "avg": 56.6}},
    "半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4061, "moonshotN": 210, "pct": 5.2, "avg": 51.3}, "20": {"n": 4061, "moonshotN": 447, "pct": 11.0, "avg": 52.3}},
    "KDJ近3日內死亡交叉 ＋ 創52週新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 633, "moonshotN": 33, "pct": 5.2, "avg": 56.0}, "20": {"n": 633, "moonshotN": 61, "pct": 9.6, "avg": 52.7}},
    "相對強弱為正(強於大盤) ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4052, "moonshotN": 210, "pct": 5.2, "avg": 51.3}, "20": {"n": 4052, "moonshotN": 446, "pct": 11.0, "avg": 52.3}},
    "大盤站上60日均線 ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3187, "moonshotN": 167, "pct": 5.2, "avg": 50.5}, "20": {"n": 3187, "moonshotN": 302, "pct": 9.5, "avg": 57.2}},
    "MACD近3日內黃金交叉 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 626, "moonshotN": 32, "pct": 5.1, "avg": 41.9}, "20": {"n": 626, "moonshotN": 61, "pct": 9.7, "avg": 52.3}},
    "多方力道≥65 ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1576, "moonshotN": 78, "pct": 4.9, "avg": 52.2}, "20": {"n": 1576, "moonshotN": 147, "pct": 9.3, "avg": 53.7}},
    "多方力道≥80 ＋ 近20日強於大盤≥10% ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 460, "moonshotN": 22, "pct": 4.8, "avg": 49.9}, "20": {"n": 460, "moonshotN": 43, "pct": 9.3, "avg": 45.1}},
    "多方力道≥80 ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1124, "moonshotN": 54, "pct": 4.8, "avg": 51.3}, "20": {"n": 1124, "moonshotN": 117, "pct": 10.4, "avg": 52.1}},
    "量能區間高檔(≥90百分位) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2355, "moonshotN": 113, "pct": 4.8, "avg": 53.2}, "20": {"n": 2355, "moonshotN": 200, "pct": 8.5, "avg": 57.2}},
    "大盤站上60日均線 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5668, "moonshotN": 273, "pct": 4.8, "avg": 50.8}, "20": {"n": 5668, "moonshotN": 565, "pct": 10.0, "avg": 52.9}},
    "多方力道≥65 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 7173, "moonshotN": 337, "pct": 4.7, "avg": 52.2}, "20": {"n": 7173, "moonshotN": 663, "pct": 9.2, "avg": 52.4}},
    "漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6986, "moonshotN": 330, "pct": 4.7, "avg": 53.1}, "20": {"n": 6986, "moonshotN": 672, "pct": 9.6, "avg": 53.2}},
    "多方力道≥65 ＋ MACD近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 527, "moonshotN": 25, "pct": 4.7, "avg": 44.9}, "20": {"n": 527, "moonshotN": 46, "pct": 8.7, "avg": 53.4}},
    "多方力道≥65 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 7168, "moonshotN": 336, "pct": 4.7, "avg": 52.3}, "20": {"n": 7168, "moonshotN": 663, "pct": 9.2, "avg": 52.4}},
    "多方力道≥65 ＋ 近20日強於大盤≥10% ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 7135, "moonshotN": 334, "pct": 4.7, "avg": 52.3}, "20": {"n": 7135, "moonshotN": 660, "pct": 9.3, "avg": 52.4}},
    "相對強弱為正(強於大盤) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6983, "moonshotN": 330, "pct": 4.7, "avg": 53.1}, "20": {"n": 6983, "moonshotN": 672, "pct": 9.6, "avg": 53.2}},
    "近20日強於大盤≥10% ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6965, "moonshotN": 327, "pct": 4.7, "avg": 53.2}, "20": {"n": 6965, "moonshotN": 671, "pct": 9.6, "avg": 53.2}},
    "多方力道≥80 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5330, "moonshotN": 246, "pct": 4.6, "avg": 54.0}, "20": {"n": 5330, "moonshotN": 513, "pct": 9.6, "avg": 52.3}},
    "近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5700, "moonshotN": 260, "pct": 4.6, "avg": 45.7}, "20": {"n": 5700, "moonshotN": 533, "pct": 9.4, "avg": 52.2}},
    "多方力道≥65 ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4972, "moonshotN": 230, "pct": 4.6, "avg": 45.4}, "20": {"n": 4972, "moonshotN": 468, "pct": 9.4, "avg": 51.0}},
    "多方力道≥80 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5329, "moonshotN": 246, "pct": 4.6, "avg": 54.0}, "20": {"n": 5329, "moonshotN": 513, "pct": 9.6, "avg": 52.3}},
    "多方力道≥80 ＋ 近20日強於大盤≥10% ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5322, "moonshotN": 246, "pct": 4.6, "avg": 54.0}, "20": {"n": 5322, "moonshotN": 513, "pct": 9.6, "avg": 52.3}},
    "相對強弱為正(強於大盤) ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5689, "moonshotN": 259, "pct": 4.6, "avg": 45.7}, "20": {"n": 5689, "moonshotN": 532, "pct": 9.4, "avg": 52.2}},
    "近20日強於大盤≥10% ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6837, "moonshotN": 310, "pct": 4.5, "avg": 52.0}, "20": {"n": 6837, "moonshotN": 642, "pct": 9.4, "avg": 52.3}},
    "均線多頭排列(5>20>60且站上月線) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6066, "moonshotN": 275, "pct": 4.5, "avg": 53.1}, "20": {"n": 6066, "moonshotN": 577, "pct": 9.5, "avg": 52.7}},
    "多方力道≥80 ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2672, "moonshotN": 118, "pct": 4.4, "avg": 55.6}, "20": {"n": 2672, "moonshotN": 260, "pct": 9.7, "avg": 52.3}},
    "近1季財報EPS低於預期 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 夜星剛形成": {"10": {"n": 123, "moonshotN": 6, "pct": 4.9, "avg": 42.8}, "20": {"n": 123, "moonshotN": 18, "pct": 14.6, "avg": 44.0}},
    "布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 90, "moonshotN": 5, "pct": 5.6, "avg": 77.6}, "20": {"n": 90, "moonshotN": 14, "pct": 15.6, "avg": 60.3}},
    "半年強勢(6-1月勝大盤≥10%) ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1515, "moonshotN": 86, "pct": 5.7, "avg": 48.0}, "20": {"n": 1515, "moonshotN": 187, "pct": 12.3, "avg": 54.5}},
    "KDJ近3日內黃金交叉 ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1748, "moonshotN": 98, "pct": 5.6, "avg": 43.9}, "20": {"n": 1748, "moonshotN": 158, "pct": 9.0, "avg": 54.2}},
    "多方力道≥80 ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1778, "moonshotN": 97, "pct": 5.5, "avg": 51.3}, "20": {"n": 1778, "moonshotN": 199, "pct": 11.2, "avg": 55.5}},
    "MACD近3日內黃金交叉 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 468, "moonshotN": 23, "pct": 4.9, "avg": 49.9}, "20": {"n": 468, "moonshotN": 42, "pct": 9.0, "avg": 50.2}},
    "布林通道高檔(≥80%) ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3805, "moonshotN": 184, "pct": 4.8, "avg": 51.0}, "20": {"n": 3805, "moonshotN": 404, "pct": 10.6, "avg": 53.0}},
    "KDJ近3日內黃金交叉 ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 983, "moonshotN": 47, "pct": 4.8, "avg": 54.7}, "20": {"n": 983, "moonshotN": 80, "pct": 8.1, "avg": 58.1}},
    "近1季財報EPS優於預期 ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2391, "moonshotN": 114, "pct": 4.8, "avg": 47.1}, "20": {"n": 2391, "moonshotN": 236, "pct": 9.9, "avg": 53.4}},
    "多方力道≥65 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6305, "moonshotN": 295, "pct": 4.7, "avg": 53.4}, "20": {"n": 6305, "moonshotN": 596, "pct": 9.5, "avg": 53.1}},
    "大盤站上20日均線 ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3388, "moonshotN": 159, "pct": 4.7, "avg": 53.1}, "20": {"n": 3388, "moonshotN": 340, "pct": 10.0, "avg": 53.7}},
    "回後買上漲全通過 ＋ 近3日向上跳空缺口 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 870, "moonshotN": 41, "pct": 4.7, "avg": 46.0}, "20": {"n": 870, "moonshotN": 94, "pct": 10.8, "avg": 53.5}},
    "近1季財報EPS低於預期 ＋ CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1464, "moonshotN": 69, "pct": 4.7, "avg": 64.5}, "20": {"n": 1464, "moonshotN": 130, "pct": 8.9, "avg": 50.8}},
    "大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 7520, "moonshotN": 344, "pct": 4.6, "avg": 49.1}, "20": {"n": 7520, "moonshotN": 711, "pct": 9.5, "avg": 52.4}},
    "多方力道≥65 ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2892, "moonshotN": 133, "pct": 4.6, "avg": 52.3}, "20": {"n": 2892, "moonshotN": 228, "pct": 7.9, "avg": 56.7}},
    "多方力道≥65 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6624, "moonshotN": 307, "pct": 4.6, "avg": 50.3}, "20": {"n": 6624, "moonshotN": 621, "pct": 9.4, "avg": 52.4}},
    "多方力道≥80 ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1957, "moonshotN": 90, "pct": 4.6, "avg": 55.8}, "20": {"n": 1957, "moonshotN": 168, "pct": 8.6, "avg": 60.0}},
    "多方力道≥80 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4935, "moonshotN": 228, "pct": 4.6, "avg": 51.6}, "20": {"n": 4935, "moonshotN": 485, "pct": 9.8, "avg": 52.6}},
    "相對強弱為正(強於大盤) ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 7498, "moonshotN": 343, "pct": 4.6, "avg": 49.2}, "20": {"n": 7498, "moonshotN": 709, "pct": 9.5, "avg": 52.5}},
    "多方力道≥65 ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6729, "moonshotN": 300, "pct": 4.5, "avg": 52.3}, "20": {"n": 6729, "moonshotN": 594, "pct": 8.8, "avg": 53.1}},
    "布林通道高檔(≥80%) ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5429, "moonshotN": 243, "pct": 4.5, "avg": 46.2}, "20": {"n": 5429, "moonshotN": 497, "pct": 9.2, "avg": 52.6}},
    "布林通道高檔(≥80%) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3966, "moonshotN": 180, "pct": 4.5, "avg": 53.8}, "20": {"n": 3966, "moonshotN": 365, "pct": 9.2, "avg": 53.4}},
    "KDJ近3日內死亡交叉 ＋ 距52週高點≤5% ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 756, "moonshotN": 34, "pct": 4.5, "avg": 51.1}, "20": {"n": 756, "moonshotN": 60, "pct": 7.9, "avg": 50.2}},
    "相對強弱為正(強於大盤) ＋ 近1季財報EPS低於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2080, "moonshotN": 94, "pct": 4.5, "avg": 59.5}, "20": {"n": 2080, "moonshotN": 186, "pct": 8.9, "avg": 50.3}},
    "近20日強於大盤≥10% ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 7420, "moonshotN": 336, "pct": 4.5, "avg": 49.4}, "20": {"n": 7420, "moonshotN": 705, "pct": 9.5, "avg": 52.5}},
    "大盤站上60日均線 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6364, "moonshotN": 287, "pct": 4.5, "avg": 50.4}, "20": {"n": 6364, "moonshotN": 599, "pct": 9.4, "avg": 52.3}},
    "半年強勢(6-1月勝大盤≥10%) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 1271, "moonshotN": 57, "pct": 4.5, "avg": 45.6}, "20": {"n": 1271, "moonshotN": 95, "pct": 7.5, "avg": 50.3}},
    "漲時量≥跌時量1.5倍(近20日) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3799, "moonshotN": 172, "pct": 4.5, "avg": 54.3}, "20": {"n": 3799, "moonshotN": 348, "pct": 9.2, "avg": 53.6}},
    "多方力道≥65 ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3557, "moonshotN": 158, "pct": 4.4, "avg": 53.8}, "20": {"n": 3557, "moonshotN": 321, "pct": 9.0, "avg": 53.1}},
    "大盤站上60日均線 ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 5237, "moonshotN": 231, "pct": 4.4, "avg": 44.7}, "20": {"n": 5237, "moonshotN": 502, "pct": 9.6, "avg": 51.2}},
    "多方力道≥80 ＋ 大盤站上20日均線 ＋ 近5日跌幅≥8%(短線超跌)": {"10": {"n": 328, "moonshotN": 14, "pct": 4.3, "avg": 47.8}, "20": {"n": 328, "moonshotN": 29, "pct": 8.8, "avg": 44.0}},
    "布林通道高檔(≥80%) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6429, "moonshotN": 275, "pct": 4.3, "avg": 52.0}, "20": {"n": 6429, "moonshotN": 574, "pct": 8.9, "avg": 53.1}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2465, "moonshotN": 107, "pct": 4.3, "avg": 47.1}, "20": {"n": 2465, "moonshotN": 193, "pct": 7.8, "avg": 54.7}},
    "大盤跌破60日均線 ＋ 近3日向上跳空缺口 ＋ 母子懷抱(低檔)剛形成": {"10": {"n": 119, "moonshotN": 4, "pct": 3.4, "avg": 45.5}, "20": {"n": 119, "moonshotN": 17, "pct": 14.3, "avg": 43.9}},
    "近20日強於大盤≥10% ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1853, "moonshotN": 92, "pct": 5.0, "avg": 50.6}, "20": {"n": 1853, "moonshotN": 180, "pct": 9.7, "avg": 54.0}},
    "MACD近3日內黃金交叉 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 498, "moonshotN": 24, "pct": 4.8, "avg": 41.1}, "20": {"n": 498, "moonshotN": 37, "pct": 7.4, "avg": 50.4}},
    "布林通道高檔(≥80%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3441, "moonshotN": 159, "pct": 4.6, "avg": 53.8}, "20": {"n": 3441, "moonshotN": 278, "pct": 8.1, "avg": 55.9}},
    "相對強弱為正(強於大盤) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3995, "moonshotN": 182, "pct": 4.6, "avg": 53.8}, "20": {"n": 3995, "moonshotN": 364, "pct": 9.1, "avg": 53.4}},
    "近20日強於大盤≥10% ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3404, "moonshotN": 158, "pct": 4.6, "avg": 53.9}, "20": {"n": 3404, "moonshotN": 273, "pct": 8.0, "avg": 56.2}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2906, "moonshotN": 135, "pct": 4.6, "avg": 54.3}, "20": {"n": 2906, "moonshotN": 243, "pct": 8.4, "avg": 57.3}},
    "近1季財報EPS低於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2084, "moonshotN": 94, "pct": 4.5, "avg": 59.5}, "20": {"n": 2084, "moonshotN": 187, "pct": 9.0, "avg": 50.2}},
    "OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 4001, "moonshotN": 182, "pct": 4.5, "avg": 53.8}, "20": {"n": 4001, "moonshotN": 366, "pct": 9.1, "avg": 53.3}},
    "多方力道≥80 ＋ 近1季財報EPS優於預期 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3696, "moonshotN": 165, "pct": 4.5, "avg": 46.2}, "20": {"n": 3696, "moonshotN": 363, "pct": 9.8, "avg": 50.5}},
    "強勢突破盤 ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2627, "moonshotN": 119, "pct": 4.5, "avg": 58.4}, "20": {"n": 2627, "moonshotN": 212, "pct": 8.1, "avg": 55.6}},
    "布林通道高檔(≥80%) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 6579, "moonshotN": 297, "pct": 4.5, "avg": 53.1}, "20": {"n": 6579, "moonshotN": 607, "pct": 9.2, "avg": 54.0}},
    "布林通道高檔(≥80%) ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 7115, "moonshotN": 309, "pct": 4.3, "avg": 48.8}, "20": {"n": 7115, "moonshotN": 650, "pct": 9.1, "avg": 53.1}},
    "回後買上漲全通過 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 1543, "moonshotN": 67, "pct": 4.3, "avg": 50.9}, "20": {"n": 1543, "moonshotN": 144, "pct": 9.3, "avg": 52.5}},
    "半年強勢(6-1月勝大盤≥10%) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 2043, "moonshotN": 87, "pct": 4.3, "avg": 49.9}, "20": {"n": 2043, "moonshotN": 212, "pct": 10.4, "avg": 50.8}},
    "均線多頭排列(5>20>60且站上月線) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": {"10": {"n": 3563, "moonshotN": 149, "pct": 4.2, "avg": 53.5}, "20": {"n": 3563, "moonshotN": 328, "pct": 9.2, "avg": 52.4}},

}
BT_HIT_SHOW = 3   # 摘要表每類最多顯示幾個編號，其餘以「+N」表示

# ── 大盤狀態分組回測（2026-10-05 三年六段回測，20日超額報酬）：每個清單組合在四種大盤狀態下的 [筆數, 勝率%, 同日超額%, t(依日)]，
#    依序為 ①多頭（SPY站上月線與季線）②多頭回檔（跌破月線、守季線）③空頭反彈（站上月線、季線下）④空頭（跌破月線與季線）；樣本<30筆為 None
REGIME_STATS = {
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 近1季財報EPS大幅優於預期(≥10%)": [[22465, 55.8, 1.36, 7.76], [2674, 52.5, -0.62, 1.02], [741, 65.6, 3.99, 4.79], [3636, 60.6, 2.28, 6.21]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 近20日強於大盤≥10%": [[17525, 54.5, 1.74, 8.32], [2504, 48.2, -1.42, 0.53], [479, 68.9, 6.0, 4.66], [2778, 55.1, 1.74, 4.04]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 布林通道收窄(寬度近半年最低20%) ＋ 近1季財報EPS大幅優於預期(≥10%)": [[14318, 52.5, 0.67, 4.61], [2185, 58.9, 2.08, 4.35], [241, 61.8, 2.28, 0.15], [2152, 63.9, 2.46, 5.36]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上)": [[5724, 54.9, 1.0, 3.7], [539, 59.6, 1.65, 1.46], [131, 58.0, 1.71, 1.88], [1043, 64.3, 2.43, 2.79]],
    "創52週新高 ＋ 大盤站上20日均線 ＋ 近20日強於大盤≥10%": [[17167, 51.7, 1.1, 6.62], None, [299, 60.5, 1.31, -1.51], None],
    "大盤站上20日均線 ＋ 大盤跌破60日均線 ＋ 爆量(≥2倍均量)": [None, None, [541, 74.7, 1.43, 0.68], None],
    "型態剛形成(剛突破) ＋ 夜星剛形成 ＋ 大盤跌破60日均線": [None, None, None, [196, 62.8, 4.82, 1.15]],
    "夜星剛形成 ＋ 大盤跌破60日均線": [None, None, None, [196, 62.8, 4.82, 1.15]],
    "型態成形中 ＋ 夜星剛形成 ＋ 大盤跌破60日均線": [None, None, None, [196, 62.8, 4.82, 1.15]],
    "型態突破確認 ＋ 夜星剛形成 ＋ 大盤跌破60日均線": [None, None, None, [196, 62.8, 4.82, 1.15]],
    "夜星剛形成 ＋ 大盤跌破20日均線 ＋ 大盤跌破60日均線": [None, None, None, [196, 62.8, 4.82, 1.15]],
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成": [None, None, None, [338, 57.1, 2.29, 2.48]],
    "大盤跌破60日均線 ＋ 相對強弱為正(強於大盤) ＋ 突破飆股大量黑K最高點剛形成": [None, None, None, [281, 60.5, 0.98, 1.32]],
    "布林通道低檔(≤20%) ＋ 爆量(≥2倍均量) ＋ 相對強弱為正(強於大盤)": [[50, 36.0, -2.88, -1.89], [107, 62.6, 0.47, -0.48], None, [413, 67.8, -0.41, -0.85]],
    "型態剛形成(剛突破) ＋ 夜星剛形成 ＋ 大盤跌破20日均線": [None, [162, 51.9, -0.64, -1.94], None, [196, 62.8, 4.82, 1.15]],
    "型態突破確認 ＋ 夜星剛形成 ＋ 大盤跌破20日均線": [None, [162, 51.9, -0.64, -1.94], None, [196, 62.8, 4.82, 1.15]],
    "型態成形中 ＋ 夜星剛形成 ＋ 大盤跌破20日均線": [None, [162, 51.9, -0.64, -1.94], None, [196, 62.8, 4.82, 1.15]],
    "夜星剛形成 ＋ 大盤跌破20日均線": [None, [162, 51.9, -0.64, -1.94], None, [196, 62.8, 4.82, 1.15]],
    "大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成 ＋ 相對強弱為正(強於大盤)": [None, None, None, [321, 59.8, 2.25, -0.83]],
    "型態剛形成(剛突破) ＋ 夜星剛形成 ＋ 相對強弱為負(弱於大盤)": [[196, 46.9, -0.62, -0.11], [36, 44.4, -3.21, -1.55], None, [39, 69.2, 9.12, 1.99]],
    "型態成形中 ＋ 夜星剛形成 ＋ 相對強弱為負(弱於大盤)": [[196, 46.9, -0.62, -0.11], [36, 44.4, -3.21, -1.55], None, [39, 69.2, 9.12, 1.99]],
    "型態突破確認 ＋ 夜星剛形成 ＋ 相對強弱為負(弱於大盤)": [[196, 46.9, -0.62, -0.11], [36, 44.4, -3.21, -1.55], None, [39, 69.2, 9.12, 1.99]],
    "夜星剛形成 ＋ 相對強弱為負(弱於大盤)": [[196, 46.9, -0.62, -0.11], [36, 44.4, -3.21, -1.55], None, [39, 69.2, 9.12, 1.99]],
    "大盤跌破20日均線 ＋ 大盤跌破60日均線 ＋ 突破飆股大量黑K最高點剛形成": [None, None, None, [293, 59.4, 0.76, 0.99]],
    "型態突破確認 ＋ 大盤跌破60日均線 ＋ 突破飆股大量黑K最高點剛形成": [None, None, None, [293, 59.4, 0.76, 0.99]],
    "大盤站上20日均線 ＋ 大盤跌破60日均線 ＋ 突破ABC修正下降切線剛形成": [None, None, [346, 65.9, 1.26, 1.33], None],
    "型態剛形成(剛突破) ＋ 夜星剛形成 ＋ 大盤站上60日均線": [[664, 45.6, -1.04, 0.48], [162, 51.9, -0.64, -1.94], None, None],
    "夜星剛形成 ＋ 大盤站上60日均線": [[664, 45.6, -1.04, 0.48], [162, 51.9, -0.64, -1.94], None, None],
    "型態成形中 ＋ 夜星剛形成 ＋ 大盤站上60日均線": [[664, 45.6, -1.04, 0.48], [162, 51.9, -0.64, -1.94], None, None],
    "型態突破確認 ＋ 夜星剛形成 ＋ 大盤站上60日均線": [[664, 45.6, -1.04, 0.48], [162, 51.9, -0.64, -1.94], None, None],
    "KDJ近3日內死亡交叉 ＋ 型態剛形成(剛突破) ＋ 夜星剛形成": [[429, 46.2, -0.94, 0.06], [116, 54.3, -0.89, -1.79], None, [135, 65.9, 5.82, 1.13]],
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成": [[429, 46.2, -0.94, 0.06], [116, 54.3, -0.89, -1.79], None, [135, 65.9, 5.82, 1.13]],
    "KDJ近3日內死亡交叉 ＋ 型態成形中 ＋ 夜星剛形成": [[429, 46.2, -0.94, 0.06], [116, 54.3, -0.89, -1.79], None, [135, 65.9, 5.82, 1.13]],
    "KDJ近3日內死亡交叉 ＋ 型態突破確認 ＋ 夜星剛形成": [[429, 46.2, -0.94, 0.06], [116, 54.3, -0.89, -1.79], None, [135, 65.9, 5.82, 1.13]],
    "K線橫盤的突破剛形成 ＋ 大盤站上20日均線 ＋ 突破飆股大量黑K最高點剛形成": [[412, 55.8, 0.68, 1.55], None, None, None],
    "布林通道低檔(≤20%) ＋ 爆量(≥1.5倍均量) ＋ 相對強弱為正(強於大盤)": [[159, 47.2, -0.32, -0.29], [201, 63.2, 1.23, 1.08], None, [1398, 72.3, 0.01, -0.09]],
    "夜星剛形成 ＋ 大盤站上20日均線": [[664, 45.6, -1.04, 0.48], None, None, None],
    "K線橫盤的突破剛形成 ＋ 大盤跌破60日均線 ＋ 相對強弱為負(弱於大盤)": [None, None, [76, 67.1, -1.52, -1.87], [425, 67.1, 0.57, -2.05]],
    "多方力道≥65 ＋ 強勢突破盤 ＋ 頭肩底剛形成": [[280, 51.8, -0.55, -1.16], [31, 64.5, 0.72, 0.72], None, None],
    "大盤跌破20日均線 ＋ 相對強弱為正(強於大盤) ＋ 複式頭肩底剛形成": [None, [213, 52.1, -1.15, -0.01], None, [156, 56.4, -0.67, 0.06]],
    "大盤跌破20日均線 ＋ 複式頭肩底剛形成": [None, [217, 52.1, -1.22, -0.12], None, [156, 56.4, -0.67, 0.06]],
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": [None, None, [1905, 69.8, 2.03, 0.99], [5027, 62.6, 1.03, -1.59]],
    "型態突破確認 ＋ 夜星剛形成 ＋ 爆量(≥1.5倍均量)": [[144, 40.3, -2.08, -2.65], [45, 46.7, -0.98, -2.51], None, [37, 64.9, 1.97, 0.76]],
    "夜星剛形成 ＋ 爆量(≥1.5倍均量)": [[144, 40.3, -2.08, -2.65], [45, 46.7, -0.98, -2.51], None, [37, 64.9, 1.97, 0.76]],
    "型態剛形成(剛突破) ＋ 夜星剛形成 ＋ 爆量(≥1.5倍均量)": [[144, 40.3, -2.08, -2.65], [45, 46.7, -0.98, -2.51], None, [37, 64.9, 1.97, 0.76]],
    "型態成形中 ＋ 夜星剛形成 ＋ 爆量(≥1.5倍均量)": [[144, 40.3, -2.08, -2.65], [45, 46.7, -0.98, -2.51], None, [37, 64.9, 1.97, 0.76]],
    "夜星剛形成 ＋ 大盤站上20日均線 ＋ 相對強弱為正(強於大盤)": [[468, 45.1, -1.22, 0.36], None, None, None],
    "夜星剛形成 ＋ 大盤跌破60日均線 ＋ 布林通道低檔(≤20%)": [None, None, None, [32, 50.0, -0.25, -0.25]],
    "大盤站上60日均線 ＋ 布林通道低檔(≤20%) ＋ 母子懷抱(高檔)剛形成": [[140, 57.1, 1.63, 2.09], [40, 52.5, -1.4, -0.89], None, None],
    "KDJ近3日內黃金交叉 ＋ 夜星剛形成 ＋ 大盤站上20日均線": [[220, 50.5, -0.45, -0.98], None, None, None],
    "夜星剛形成 ＋ 大盤跌破20日均線 ＋ 布林通道低檔(≤20%)": [None, None, None, [32, 50.0, -0.25, -0.25]],
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成 ＋ 相對強弱為正(強於大盤)": [[300, 45.3, -1.4, -0.22], [86, 58.1, 0.09, -1.43], None, [113, 62.8, 4.66, 0.27]],
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成 ＋ 大盤站上20日均線": [[429, 46.2, -0.94, 0.06], None, None, None],
    "KDJ近3日內黃金交叉 ＋ 布林通道低檔(≤20%) ＋ 母子懷抱(高檔)剛形成": [[72, 52.8, 0.97, 1.86], None, None, [132, 73.5, 3.72, 0.49]],
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成 ＋ 大盤站上60日均線": [[429, 46.2, -0.94, 0.06], [116, 54.3, -0.89, -1.79], None, None],
    "MACD近3日內黃金交叉 ＋ 多方力道≥80 ＋ 突破ABC修正下降切線剛形成": [[107, 57.9, 0.81, 0.56], None, None, None],
    "大盤站上20日均線 ＋ 晨星剛形成 ＋ 爆量(≥1.5倍均量)": [[242, 50.4, 0.92, -0.2], None, None, None],
    "多方力道≥65 ＋ 爆量(≥2倍均量) ＋ 突破ABC修正下降切線剛形成": [[208, 50.0, 0.52, 0.37], [50, 46.0, -2.62, -1.47], None, None],
    "多方力道≥80 ＋ 爆量(≥1.5倍均量) ＋ 突破ABC修正下降切線剛形成": [[186, 53.8, 1.06, 0.93], [34, 52.9, -3.29, -1.5], None, None],
    "大盤站上60日均線 ＋ 晨星剛形成 ＋ 爆量(≥1.5倍均量)": [[242, 50.4, 0.92, -0.2], [95, 55.8, -1.73, 0.31], None, None],
    "夜星剛形成 ＋ 大盤站上60日均線 ＋ 相對強弱為正(強於大盤)": [[468, 45.1, -1.22, 0.36], [126, 54.0, 0.1, -1.62], None, None],
    "強勢突破盤 ＋ 爆量(≥2倍均量) ＋ 突破ABC修正下降切線剛形成": [[296, 51.4, 0.92, 0.97], [51, 41.2, -3.9, -3.82], None, [41, 58.5, -1.59, -0.5]],
    "型態突破確認 ＋ 晨星剛形成 ＋ 爆量(≥1.5倍均量)": [[242, 50.4, 0.92, -0.2], [95, 55.8, -1.73, 0.31], None, [63, 50.8, -2.0, -1.65]],
    "晨星剛形成 ＋ 爆量(≥1.5倍均量)": [[242, 50.4, 0.92, -0.2], [95, 55.8, -1.73, 0.31], None, [63, 50.8, -2.0, -1.65]],
    "型態剛形成(剛突破) ＋ 晨星剛形成 ＋ 爆量(≥1.5倍均量)": [[242, 50.4, 0.92, -0.2], [95, 55.8, -1.73, 0.31], None, [63, 50.8, -2.0, -1.65]],
    "型態成形中 ＋ 晨星剛形成 ＋ 爆量(≥1.5倍均量)": [[242, 50.4, 0.92, -0.2], [95, 55.8, -1.73, 0.31], None, [63, 50.8, -2.0, -1.65]],
    "大盤站上60日均線 ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": [[273, 50.9, -0.42, -1.14], [52, 55.8, 0.07, -0.01], None, None],
    "夜星剛形成 ＋ 大盤站上20日均線 ＋ 大盤站上60日均線": [[664, 45.6, -1.04, 0.48], None, None, None],
    "多方力道≥80 ＋ 爆量(≥2倍均量) ＋ 突破飆股大量黑K最高點剛形成": [[135, 60.0, 2.7, 2.09], None, None, None],
    "多方力道≥65 ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": [[119, 51.3, -0.53, -0.73], None, None, None],
    "相對強弱為正(強於大盤) ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": [[249, 51.4, -0.2, -0.9], [48, 54.2, -0.46, -0.35], None, [39, 76.9, 2.89, 1.9]],
    "KDJ近3日內死亡交叉 ＋ 多方力道≥65 ＋ 夜星剛形成": [[207, 44.0, -1.99, -0.58], [45, 57.8, -1.09, -2.01], None, [38, 52.6, 1.06, -0.33]],
    "型態成形中 ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": [[273, 50.9, -0.42, -1.14], [52, 55.8, 0.07, -0.01], None, [39, 76.9, 2.89, 1.9]],
    "型態剛形成(剛突破) ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": [[273, 50.9, -0.42, -1.14], [52, 55.8, 0.07, -0.01], None, [39, 76.9, 2.89, 1.9]],
    "K線橫盤的突破剛形成 ＋ 大盤跌破20日均線 ＋ 突破ABC修正下降切線剛形成": [None, [107, 60.7, 2.34, -0.65], None, [75, 56.0, -0.4, -0.81]],
    "回後買上漲全通過 ＋ 多方力道≥80 ＋ 突破飆股大量黑K最高點剛形成": [[180, 51.1, 1.04, 0.34], None, None, None],
    "K線橫盤的突破剛形成 ＋ 圓弧底剛形成 ＋ 爆量(≥2倍均量)": [[137, 50.4, 0.58, 0.62], None, None, None],
    "型態突破確認 ＋ 突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": [[273, 50.9, -0.42, -1.14], [52, 55.8, 0.07, -0.01], None, [39, 76.9, 2.89, 1.9]],
    "突破ABC修正下降切線剛形成 ＋ 突破飆股大量黑K最高點剛形成": [[273, 50.9, -0.42, -1.14], [52, 55.8, 0.07, -0.01], None, [39, 76.9, 2.89, 1.9]],
    "KDJ近3日內死亡交叉 ＋ 夜星剛形成 ＋ 大盤跌破60日均線": [None, None, None, [135, 65.9, 5.82, 1.13]],
    "地量(≤0.5倍均量) ＋ 多方力道≥80 ＋ 量能區間高檔(≥90百分位)": [[85, 48.2, 3.47, 1.46], None, None, None],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線": [None, None, [4615, 65.3, 2.17, 4.69], [33732, 60.2, 1.69, 8.69]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": [None, None, [2054, 68.0, 3.72, 6.45], [15105, 61.5, 2.05, 8.13]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線 ＋ 近1季財報EPS優於預期": [None, None, [3364, 67.3, 2.76, 5.32], [24419, 61.0, 1.72, 7.75]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線 ＋ 布林通道低檔(≤20%)": [None, None, [279, 77.1, 3.98, 4.5], [11682, 62.5, 2.02, 8.09]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 近1季財報EPS優於預期": [[34409, 55.5, 1.06, 6.82], [3920, 54.3, -0.34, 1.31], [1093, 65.0, 2.77, 4.28], [5699, 60.2, 1.97, 6.98]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破20日均線 ＋ 大盤跌破60日均線": [None, None, None, [33732, 60.2, 1.69, 8.69]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破20日均線 ＋ 布林通道低檔(≤20%)": [None, [5275, 60.1, 1.82, 4.44], None, [11682, 62.5, 2.02, 8.09]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線 ＋ 相對強弱為負(弱於大盤)": [None, None, [1994, 63.0, 0.17, 0.49], [19126, 60.5, 1.89, 8.24]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%)": [[44888, 54.6, 0.95, 7.4], [5246, 53.1, -0.46, 0.58], [1537, 61.0, 1.72, 3.48], [8076, 58.9, 1.86, 5.77]],
    "大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": [None, None, [7095, 65.1, 1.35, 6.22], [48604, 58.9, 0.44, 5.95]],
    "大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%) ＋ 量能區間高檔(≥90百分位)": [None, None, [962, 71.0, 1.45, 3.53], [8587, 59.6, 1.07, 5.45]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破20日均線 ＋ 相對強弱為負(弱於大盤)": [None, [10084, 57.1, 1.15, 3.2], None, [19126, 60.5, 1.89, 8.24]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線 ＋ 近5日跌幅≥8%(短線超跌)": [None, None, [129, 67.4, 1.65, 0.37], [4881, 60.1, 3.12, 6.86]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線 ＋ 相對強弱為正(強於大盤)": [None, None, [2621, 67.0, 3.69, 5.3], [14606, 59.7, 1.42, 6.59]],
    "大盤跌破60日均線 ＋ 近1季財報EPS優於預期 ＋ 量能區間高檔(≥90百分位)": [None, None, [1868, 70.8, 0.49, 2.14], [18012, 59.1, 0.5, 4.35]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 大盤跌破60日均線": [None, None, [3225, 63.4, 1.69, 3.2], [25107, 60.7, 1.75, 7.01]],
    "大盤跌破20日均線 ＋ 相對強弱為負(弱於大盤) ＋ 近1季財報EPS大幅優於預期(≥10%)": [None, [14693, 58.0, 0.68, 4.12], None, [24193, 60.8, 0.97, 5.24]],
    "大盤跌破20日均線 ＋ 大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": [None, None, None, [48604, 58.9, 0.44, 5.95]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線 ＋ 近1季財報EPS低於預期": [None, None, [1027, 59.2, 0.62, 0.84], [6961, 60.8, 1.82, 8.12]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 量能區間高檔(≥90百分位)": [[5985, 55.8, 1.57, 5.49], [949, 46.5, -2.46, -0.96], [196, 66.3, 0.87, 0.4], [1387, 58.0, 2.53, 2.86]],
    "多方力道≥80 ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[1521, 57.0, 3.81, 6.43], [120, 51.7, -1.2, -0.81], None, [121, 43.0, -1.13, -0.67]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破20日均線 ＋ 近1季財報EPS低於預期": [None, [3867, 53.6, -0.01, 0.37], None, [6961, 60.8, 1.82, 8.12]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線 ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [None, None, [584, 67.5, 1.36, 2.54], [4616, 61.5, 1.38, 4.27]],
    "大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [None, None, [925, 69.2, 1.62, 4.08], [6876, 63.2, 0.5, 3.75]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": [None, None, [2401, 63.5, 1.75, 4.47], [18779, 60.7, 1.17, 5.48]],
    "大盤跌破60日均線 ＋ 相對強弱為負(弱於大盤) ＋ 量能區間高檔(≥90百分位)": [None, None, [1527, 69.7, 0.05, -0.01], [14399, 59.7, 0.63, 2.57]],
    "大盤跌破60日均線 ＋ 近1季財報EPS大幅優於預期(≥10%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [None, None, [685, 64.4, 1.58, 2.86], [5899, 62.8, 0.74, 3.61]],
    "多方力道≥80 ＋ 短線過熱(高於月線≥20%) ＋ 近20日財報跳空上漲": [[1740, 59.7, 3.45, 5.14], [143, 53.1, 0.13, -1.29], None, [161, 54.0, 4.37, 5.19]],
    "KDJ近3日內黃金交叉 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線": [None, None, [1424, 68.5, 3.24, 2.46], [10014, 60.5, 1.52, 4.74]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破20日均線 ＋ 近5日跌幅≥8%(短線超跌)": [None, [2421, 53.5, 0.37, 1.07], None, [4881, 60.1, 3.12, 6.86]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 布林通道低檔(≤20%) ＋ 近1季財報EPS優於預期": [[12933, 51.1, 0.42, 2.31], [3994, 61.3, 2.02, 4.3], [194, 77.8, 4.78, 5.07], [8580, 63.2, 2.03, 7.39]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%) ＋ 近20日財報跳空上漲": [[1945, 58.0, 2.92, 5.5], [146, 52.1, -0.32, -0.96], [33, 100.0, 6.33, 3.1], [187, 52.9, 2.96, 1.61]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 大盤站上20日均線 ＋ 近1季財報EPS大幅優於預期(≥10%)": [[67750, 54.8, 0.55, 7.58], None, [2285, 65.5, 1.81, 2.11], None],
    "半年強勢(6-1月勝大盤≥10%) ＋ 近5日跌幅≥8%(短線超跌) ＋ 量能區間高檔(≥90百分位)": [[2343, 46.8, -0.02, 1.75], [667, 53.8, -0.34, 1.52], [80, 67.5, 1.69, 1.69], [1995, 59.1, 2.58, 3.69]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 布林通道低檔(≤20%) ＋ 相對強弱為負(弱於大盤)": [[17241, 50.7, 0.38, 2.25], [4946, 60.1, 1.81, 4.35], [264, 76.9, 3.82, 4.21], [9990, 61.7, 2.09, 8.25]],
    "多方力道≥80 ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[3111, 57.4, 3.62, 7.23], [334, 45.5, -3.01, -2.29], None, [225, 44.0, -0.28, -1.52]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 大盤跌破20日均線 ＋ 布林通道低檔(≤20%)": [None, [6480, 60.6, 1.33, 3.66], None, [15546, 63.0, 1.27, 4.67]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線 ＋ 近20日強於大盤≥10%": [None, None, [894, 73.4, 7.64, 5.45], [4791, 55.9, 1.49, 3.67]],
    "大盤跌破60日均線 ＋ 相對強弱為負(弱於大盤) ＋ 近1季財報EPS大幅優於預期(≥10%)": [None, None, [3290, 63.4, 0.23, 0.56], [24193, 60.8, 0.97, 5.24]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 布林通道低檔(≤20%)": [[17596, 50.7, 0.38, 2.3], [5275, 60.1, 1.82, 4.44], [279, 77.1, 3.98, 4.5], [11682, 62.5, 2.02, 8.09]],
    "短線過熱(高於月線≥20%) ＋ 距52週高點≤5% ＋ 近20日財報跳空上漲": [[1325, 56.6, 2.33, 4.71], [97, 49.5, 0.41, 0.36], None, [83, 53.0, 4.03, 2.42]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 多方力道≥65": [[22572, 55.9, 1.45, 7.75], [2355, 51.5, -0.55, -0.42], [272, 54.4, 0.54, -0.35], [1911, 50.0, 1.18, 1.66]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 布林通道低檔(≤20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[3836, 50.4, 0.23, 1.77], [1002, 62.6, 1.6, 3.37], [117, 75.2, 2.45, 2.92], [2126, 61.2, 1.59, 4.23]],
    "NR7窄幅日(近7日振幅最小) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線": [None, None, [815, 62.9, 1.52, 0.3], [4750, 59.5, 1.61, 4.63]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破20日均線": [None, [20763, 54.9, 0.21, 1.54], None, [33732, 60.2, 1.69, 8.69]],
    "大盤跌破20日均線 ＋ 近1季財報EPS大幅優於預期(≥10%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [None, [4367, 59.7, 0.22, 0.48], None, [6876, 63.2, 0.5, 3.75]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 布林通道低檔(≤20%)": [[1593, 55.6, 0.98, 2.84], [393, 60.8, 1.13, 2.74], None, [1532, 65.9, 2.93, 5.58]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[6201, 55.6, 1.29, 5.78], [686, 50.7, -1.38, -1.24], [219, 65.3, 1.7, 1.61], [1085, 57.4, 1.22, 0.64]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破60日均線 ＋ 量能區間高檔(≥90百分位)": [None, None, [559, 69.8, 2.47, 2.26], [5635, 57.7, 1.86, 5.62]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤跌破20日均線 ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [None, [2818, 56.7, -0.09, 1.33], None, [4616, 61.5, 1.38, 4.27]],
    "大盤跌破60日均線 ＋ 近1季財報EPS低於預期 ＋ 近5日跌幅≥8%(短線超跌)": [None, None, [180, 60.6, -1.91, -2.25], [3869, 60.9, 1.09, 3.95]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 布林通道低檔(≤20%)": [[12337, 49.4, 0.26, 1.77], [3810, 59.8, 1.82, 3.89], [182, 75.3, 3.5, 3.15], [8802, 62.3, 2.01, 7.04]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[566, 52.5, 3.01, 3.94], [71, 36.6, -5.82, -3.34], None, [49, 51.0, -0.98, -0.78]],
    "多方力道≥80 ＋ 近5日跌幅≥8%(短線超跌) ＋ 量能區間高檔(≥90百分位)": [[98, 48.0, 0.36, 0.22], None, None, None],
    "中期強勢(12-1月勝大盤≥20%) ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[1047, 53.2, 3.07, 4.34], [119, 30.3, -7.37, -3.03], None, [82, 54.9, 2.29, 0.4]],
    "KDJ近3日內死亡交叉 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[427, 54.3, 2.88, 2.84], [54, 24.1, -8.37, -3.22], None, [37, 51.4, 1.51, 0.92]],
    "回後買上漲全通過 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[700, 52.1, 2.54, 2.48], [79, 36.7, -4.42, -2.04], None, [72, 45.8, -0.63, -1.12]],
    "KDJ近3日內死亡交叉 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": [[914, 56.2, 3.79, 5.35], [107, 32.7, -7.63, -1.23], None, [63, 60.3, 2.83, 1.02]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS大幅優於預期(≥10%)": [[1984, 54.2, 3.29, 4.25], [201, 38.8, -6.65, -1.41], [48, 95.8, 13.15, 5.36], [118, 45.8, -0.28, 0.19]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[1342, 52.2, 2.64, 5.24], [149, 37.6, -5.04, -2.21], None, [89, 49.4, -0.02, -0.09]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": [[2675, 54.8, 3.41, 6.88], [223, 41.3, -4.0, -0.23], [49, 87.8, 8.51, 5.16], [166, 53.6, 2.09, -0.27]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[930, 53.1, 2.08, 2.98], [89, 43.8, -2.67, -2.39], None, [82, 43.9, -2.47, -1.48]],
    "多方力道≥80 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[625, 54.9, 2.33, 2.13], [83, 42.2, -4.38, -2.07], None, [54, 40.7, -2.95, -1.0]],
    "KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[554, 54.5, 3.01, 3.4], [81, 29.6, -7.74, -2.72], None, [43, 58.1, 1.78, 0.83]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[1378, 55.2, 4.02, 6.04], [106, 38.7, -5.06, -1.62], [36, 88.9, 10.34, 2.43], [81, 55.6, 2.04, 1.03]],
    "大盤站上20日均線 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[1226, 50.5, 1.24, 2.21], None, None, None],
    "KDJ近3日內黃金交叉 ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": [[1027, 48.3, 0.72, 1.67], [143, 39.9, -4.47, -1.59], None, [84, 52.4, 1.88, 0.06]],
    "多方力道≥80 ＋ 母子懷抱(高檔)剛形成 ＋ 短線過熱(高於月線≥20%)": [[63, 50.8, 3.76, 1.56], None, None, None],
    "KDJ近3日內死亡交叉 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[240, 55.4, 3.8, 2.59], [31, 22.6, -9.89, -5.11], None, None],
    "中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[1363, 52.8, 2.72, 5.87], [166, 34.9, -6.18, -2.92], None, [104, 53.8, 0.88, -0.58]],
    "大盤站上20日均線 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[2155, 51.0, 1.53, 2.76], None, [44, 81.8, 5.78, 1.61], None],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[1664, 54.1, 2.53, 4.06], [156, 41.0, -3.5, -3.0], [35, 80.0, 2.49, 0.1], [135, 48.9, 0.24, -0.14]],
    "KDJ近3日內黃金交叉 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[867, 51.6, 1.6, 1.69], [117, 44.4, -2.31, -1.09], None, [77, 42.9, -1.94, -1.73]],
    "KDJ近3日內黃金交叉 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": [[1118, 52.2, 1.91, 4.29], [113, 36.3, -6.11, -1.0], None, [73, 52.1, 1.18, -0.13]],
    "KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[1186, 51.3, 1.45, 2.7], [164, 45.7, -2.35, -0.85], None, [120, 45.8, -1.09, -1.97]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[3456, 52.5, 2.58, 5.99], None, [66, 89.4, 9.92, 4.31], None],
    "中期強勢(12-1月勝大盤≥20%) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[2895, 54.3, 3.26, 6.91], [310, 39.0, -4.53, -0.6], [51, 88.2, 10.24, 1.96], [193, 49.7, 0.76, -0.47]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": [[3456, 52.5, 2.58, 5.99], [368, 38.0, -5.61, -1.17], [66, 89.4, 9.92, 4.31], [244, 52.0, 1.43, -0.85]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[3444, 52.6, 2.64, 6.09], [366, 38.3, -5.47, -1.09], [66, 89.4, 9.92, 4.31], [243, 52.3, 1.54, -0.83]],
    "多方力道≥80 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[1187, 54.6, 2.48, 3.09], [145, 40.7, -4.55, -1.69], None, [99, 47.5, -0.01, -0.38]],
    "爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[1480, 53.2, 2.45, 3.13], [188, 41.5, -4.14, -2.34], None, [135, 45.9, -0.42, -0.7]],
    "KDJ近3日內黃金交叉 ＋ 多方力道≥80 ＋ 短線過熱(高於月線≥20%)": [[1272, 55.1, 2.75, 5.58], [150, 48.7, -2.23, -1.15], None, [101, 41.6, -2.19, -2.62]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[3389, 52.8, 2.75, 6.38], [364, 38.5, -5.4, -1.04], [66, 89.4, 9.92, 4.31], [242, 52.5, 1.57, -0.81]],
    "KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%)": [[2046, 52.5, 1.58, 3.61], [239, 44.8, -3.12, -1.32], None, [181, 45.9, -1.48, -3.07]],
    "爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[2155, 51.0, 1.53, 2.76], [267, 39.0, -4.42, -2.2], [44, 81.8, 5.78, 1.61], [200, 49.5, 0.19, -0.52]],
    "KDJ近3日內黃金交叉 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[2042, 52.5, 1.6, 3.65], [237, 45.1, -2.89, -1.26], None, [181, 45.9, -1.48, -3.07]],
    "KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[2026, 52.8, 1.67, 3.76], [237, 45.1, -2.89, -1.26], None, [180, 46.1, -1.45, -3.11]],
    "爆量(≥1.5倍均量) ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[2152, 51.1, 1.55, 2.81], [267, 39.0, -4.42, -2.2], [43, 81.4, 5.95, 1.63], [198, 50.0, 0.37, -0.48]],
    "OBV能量潮創60日新高 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[1430, 51.5, 1.29, 1.86], [190, 40.0, -4.19, -1.87], None, [121, 48.8, 0.8, -0.25]],
    "均線多頭排列(5>20>60且站上月線) ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[950, 50.5, 1.1, 1.81], [126, 41.3, -4.94, -3.36], None, [83, 47.0, -1.66, -0.79]],
    "大盤跌破60日均線 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 母子懷抱(低檔)剛形成": [None, None, None, [82, 62.2, 5.62, 2.02]],
    "KDJ近3日內黃金交叉 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[2046, 52.5, 1.58, 3.61], None, None, None],
    "中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[2365, 54.8, 3.51, 5.45], [244, 40.6, -5.69, 0.58], [52, 94.2, 11.69, 4.58], [143, 47.6, 0.3, 0.69]],
    "KDJ近3日內黃金交叉 ＋ 多方力道≥65 ＋ 短線過熱(高於月線≥20%)": [[1744, 52.7, 1.93, 3.65], [205, 45.9, -2.63, -0.66], None, [139, 46.0, -1.27, -2.31]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 多方力道≥65 ＋ 短線過熱(高於月線≥20%)": [[3001, 53.5, 2.96, 6.96], [330, 39.1, -5.15, -0.9], [38, 84.2, 5.44, 0.57], [210, 54.3, 2.24, 0.01]],
    "爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[2125, 51.3, 1.59, 2.84], [266, 39.1, -4.35, -2.15], [42, 81.0, 5.52, 1.44], [194, 51.0, 0.48, -0.42]],
    "回後買上漲全通過 ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[673, 52.6, 1.97, 1.88], [77, 33.8, -6.41, -2.44], None, [69, 50.7, -1.12, -1.71]],
    "布林通道高檔(≥80%) ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[2138, 51.0, 1.55, 2.81], [261, 39.8, -4.04, -1.83], [44, 81.8, 5.78, 1.61], [199, 49.2, 0.03, -0.72]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[1002, 46.9, 1.06, 2.29], [145, 33.1, -6.73, -2.32], None, [84, 59.5, 4.04, 1.63]],
    "爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[1197, 50.5, 1.26, 2.25], [159, 40.9, -4.51, -4.2], None, [118, 44.9, -2.17, -1.8]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%)": [[1611, 54.0, 2.27, 4.83], [171, 43.9, -3.31, -1.89], None, [127, 44.1, -1.15, -2.5]],
    "KDJ近3日內死亡交叉 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[1686, 54.8, 2.86, 4.67], None, None, None],
    "大盤站上20日均線 ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2832, 51.1, 1.4, 3.76], None, [49, 83.7, 4.73, 1.14], None],
    "中期強勢(12-1月勝大盤≥20%) ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[3281, 52.5, 2.63, 6.34], [339, 37.2, -5.87, -1.76], [66, 89.4, 9.92, 4.31], [218, 51.8, 1.0, -0.55]],
    "漲時量≥跌時量1.5倍(近20日) ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[1842, 52.4, 1.95, 2.95], [233, 40.8, -3.69, -2.18], [40, 80.0, 4.93, 0.75], [162, 50.0, 0.96, -0.13]],
    "KDJ近3日內黃金交叉 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[1742, 54.3, 2.23, 3.46], [199, 47.2, -2.11, -1.46], None, [146, 46.6, -1.95, -3.11]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": [[3456, 52.5, 2.58, 5.99], [368, 38.0, -5.61, -1.17], None, None],
    "爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[1781, 51.2, 1.38, 3.05], [229, 39.7, -4.54, -3.07], [31, 80.6, 4.8, 1.35], [167, 47.9, -0.54, -1.41]],
    "爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[860, 52.6, 2.07, 1.92], [110, 43.6, -4.09, -3.64], None, [80, 40.0, -2.6, -1.45]],
    "漲時量≥跌時量1.5倍(近20日) ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[1032, 51.9, 1.51, 2.24], [140, 42.9, -3.75, -3.41], None, [97, 45.4, -1.48, -1.1]],
    "大盤站上20日均線 ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[3238, 50.6, 1.27, 2.62], None, [69, 78.3, 4.76, 1.59], None],
    "多方力道≥65 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[973, 51.1, 1.25, 1.98], [126, 40.5, -4.69, -3.42], None, [87, 47.1, -1.91, -0.88]],
    "KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[972, 52.4, 1.21, 2.5], [126, 46.8, -2.17, -1.2], None, [89, 44.9, -2.66, -2.54]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%)": [[1300, 56.9, 3.7, 5.71], [134, 41.0, -4.64, -1.77], None, [92, 52.2, -0.23, -0.04]],
    "KDJ近3日內黃金交叉 ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[1956, 52.2, 1.42, 2.75], [233, 45.5, -2.84, -1.31], None, [174, 46.0, -1.43, -3.04]],
    "爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[1226, 50.5, 1.24, 2.21], [161, 41.0, -4.52, -4.15], None, [120, 45.0, -2.1, -1.84]],
    "爆量(≥2倍均量) ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[1224, 50.5, 1.25, 2.31], [161, 41.0, -4.52, -4.15], None, [118, 45.8, -1.84, -1.53]],
    "爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[1207, 51.0, 1.33, 2.34], [160, 41.2, -4.41, -4.1], None, [117, 46.2, -1.73, -1.52]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 強勢突破盤 ＋ 短線過熱(高於月線≥20%)": [[1847, 53.1, 2.9, 6.33], [192, 32.3, -6.27, -1.22], [42, 88.1, 7.15, 3.18], [125, 52.0, 0.63, 0.34]],
    "KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[484, 56.4, 4.03, 4.24], [39, 43.6, -2.92, -0.81], None, [32, 34.4, -4.53, -1.5]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2164, 54.3, 2.35, 4.62], [216, 42.6, -3.86, -4.56], [39, 87.2, 3.68, 0.2], [175, 49.7, -0.1, -0.69]],
    "爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[1133, 50.6, 1.18, 2.34], [156, 41.0, -4.56, -4.36], None, [111, 45.0, -2.48, -1.96]],
    "多方力道≥65 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[1776, 51.5, 1.57, 2.71], [219, 39.3, -4.21, -1.67], None, [152, 50.7, 0.53, 0.08]],
    "MACD近3日內黃金交叉 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": [[383, 41.3, -1.68, -1.28], [51, 37.3, -7.3, -2.54], None, None],
    "KDJ近3日內死亡交叉 ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": [[867, 54.1, 3.0, 5.02], [132, 31.8, -8.58, -2.43], None, [72, 62.5, 3.91, 1.1]],
    "爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[1942, 50.4, 1.22, 2.41], [249, 39.8, -4.22, -2.01], [42, 81.0, 5.21, 1.57], [186, 50.0, -0.03, -0.74]],
    "多方力道≥80 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 近5日跌幅≥8%(短線超跌)": [[232, 54.7, 1.67, 2.09], [70, 55.7, -1.31, -0.74], None, [45, 55.6, 1.41, -0.47]],
    "KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[729, 53.2, 2.44, 3.53], [102, 36.3, -5.13, -1.74], None, [70, 52.9, 2.09, 0.44]],
    "OBV能量潮創60日新高 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[880, 52.5, 1.29, 1.93], [121, 42.1, -3.87, -2.13], None, [83, 47.0, -1.24, -1.11]],
    "KDJ近3日內黃金交叉 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[541, 51.0, 1.61, 1.06], [69, 49.3, -2.57, -1.27], None, [54, 38.9, -4.18, -2.62]],
    "OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[1593, 51.7, 1.43, 2.47], [208, 43.3, -3.83, -2.59], None, [135, 48.9, 0.52, -0.23]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[2463, 53.5, 2.26, 4.04], [244, 42.6, -2.89, -3.11], [49, 79.6, 2.53, 0.27], [222, 50.9, 1.75, -0.54]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[541, 44.0, 0.24, 0.33], [82, 35.4, -7.34, -4.41], None, [53, 52.8, 0.28, -0.07]],
    "母子懷抱(高檔)剛形成 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[71, 50.7, 2.79, 1.36], None, None, None],
    "大盤站上60日均線 ＋ 母子懷抱(高檔)剛形成 ＋ 短線過熱(高於月線≥20%)": [[88, 53.4, 2.81, 1.41], None, None, None],
    "中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[1585, 50.7, 1.77, 3.66], [201, 36.8, -4.58, -0.91], [37, 86.5, 8.23, 0.96], [157, 51.0, 1.09, -1.16]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 多方力道≥80 ＋ 短線過熱(高於月線≥20%)": [[2173, 55.5, 3.42, 7.24], [231, 44.2, -3.87, -0.08], None, [154, 53.9, 2.52, -0.19]],
    "布林通道高檔(≥80%) ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[1221, 50.5, 1.28, 2.28], [158, 41.8, -4.12, -3.83], None, [120, 45.0, -2.1, -1.84]],
    "OBV能量潮創60日新高 ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 短線過熱(高於月線≥20%)": [[1740, 52.8, 2.79, 5.11], [188, 37.2, -5.44, -1.48], [42, 90.5, 12.18, 4.3], [101, 53.5, 2.05, 1.47]],
    "爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[985, 51.5, 1.36, 2.36], [136, 44.1, -3.97, -3.64], None, [93, 45.2, -2.32, -1.57]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": [[3015, 52.7, 2.59, 6.2], [334, 38.9, -5.56, -0.77], [42, 83.3, 5.92, 0.1], [214, 53.7, 2.15, -0.26]],
    "KDJ近3日內黃金交叉 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": [[2046, 52.5, 1.58, 3.61], [239, 44.8, -3.12, -1.32], None, None],
    "KDJ近3日內黃金交叉 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": [[1747, 52.0, 1.66, 3.75], [218, 44.0, -3.04, -1.12], None, [141, 45.4, -1.14, -2.54]],
    "短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2832, 51.1, 1.4, 3.76], [355, 40.6, -4.58, -3.89], [49, 83.7, 4.73, 1.14], [260, 47.7, -0.86, -2.29]],
    "相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2827, 51.1, 1.42, 3.8], [355, 40.6, -4.58, -3.89], [49, 83.7, 4.73, 1.14], [258, 48.1, -0.74, -2.2]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 夜星剛形成 ＋ 近20日強於大盤≥10%": [[150, 42.7, -2.5, -0.75], [36, 58.3, 1.12, -0.33], None, None],
    "布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2811, 51.2, 1.44, 4.0], [346, 41.6, -4.16, -3.37], [49, 83.7, 4.73, 1.14], [257, 47.5, -0.98, -2.1]],
    "爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[1531, 51.3, 1.4, 2.52], [190, 44.2, -3.76, -2.59], None, [137, 45.3, -1.36, -1.26]],
    "均線多頭排列(5>20>60且站上月線) ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[1702, 50.9, 1.44, 2.85], [218, 38.1, -5.17, -2.53], None, [139, 51.8, 0.91, -0.0]],
    "大盤站上60日均線 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[1226, 50.5, 1.24, 2.21], [161, 41.0, -4.52, -4.15], None, None],
    "KDJ近3日內黃金交叉 ＋ 強勢突破盤 ＋ 短線過熱(高於月線≥20%)": [[1192, 52.5, 1.18, 1.96], [151, 45.7, -2.74, -0.76], None, [112, 42.0, -1.28, -2.37]],
    "相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[3225, 50.7, 1.31, 2.65], [396, 41.2, -3.72, -2.34], [68, 77.9, 4.85, 1.62], [337, 48.7, 0.34, -2.52]],
    "OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[1890, 51.8, 1.34, 2.02], [239, 40.2, -4.11, -2.01], [42, 73.8, 5.34, 1.5], [167, 48.5, 0.47, -0.05]],
    "回後買上漲全通過 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[1306, 52.5, 2.05, 2.81], [137, 38.0, -4.6, -2.13], None, [122, 47.5, -0.03, -1.66]],
    "爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS低於預期": [[299, 49.5, 0.68, 1.14], [44, 38.6, -5.35, -1.5], None, [30, 50.0, -2.7, -0.96]],
    "多方力道≥80 ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[1673, 54.4, 2.44, 3.29], [211, 46.0, -3.0, -2.17], None, [137, 46.0, -0.59, -0.81]],
    "回後買上漲全通過 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[425, 52.9, 2.22, 1.8], [51, 37.3, -4.73, -2.22], None, [45, 35.6, -4.45, -2.35]],
    "多方力道≥65 ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2423, 51.8, 1.72, 3.98], [300, 42.0, -4.18, -3.47], [31, 80.6, 2.35, 0.56], [207, 49.3, -0.46, -1.4]],
    "短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10% ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2786, 51.4, 1.5, 3.82], [353, 40.8, -4.49, -3.87], [48, 83.3, 4.33, 0.92], [254, 48.8, -0.64, -2.14]],
    "短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期 ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2002, 53.1, 2.23, 3.86], [252, 44.4, -4.29, -3.03], [35, 91.4, 6.82, 1.91], [173, 43.4, -1.68, -2.24]],
    "短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[3238, 50.6, 1.27, 2.62], [398, 41.0, -3.85, -2.4], [69, 78.3, 4.76, 1.59], [339, 48.4, 0.24, -2.6]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 母子懷抱(高檔)剛形成 ＋ 漲時量≥跌時量1.5倍(近20日)": [[157, 54.1, 2.9, 1.84], None, None, None],
    "短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10% ＋ 量能區間高檔(≥90百分位)": [[3190, 50.9, 1.37, 2.74], [395, 41.3, -3.67, -2.28], [67, 77.6, 4.57, 1.41], [332, 49.4, 0.45, -2.46]],
    "短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口 ＋ 量能區間高檔(≥90百分位)": [[1757, 51.6, 1.81, 3.87], [169, 47.9, -1.59, -1.1], [48, 85.4, 9.04, 2.35], [186, 48.9, -0.17, -0.68]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[1288, 47.3, 0.91, 2.2], [156, 35.9, -6.39, -2.4], None, [95, 49.5, -1.16, -0.94]],
    "回後買上漲全通過 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[1541, 51.3, 1.57, 2.4], None, None, None],
    "均線多頭排列(5>20>60且站上月線) ＋ 近1季財報EPS低於預期 ＋ 近5日跌幅≥8%(短線超跌)": [[221, 41.6, -0.73, -1.01], [65, 36.9, -5.21, -1.56], None, [44, 56.8, 3.34, 0.63]],
    "均線多頭排列(5>20>60且站上月線) ＋ 近5日跌幅≥8%(短線超跌) ＋ 量能區間高檔(≥90百分位)": [[193, 47.2, 0.15, 0.58], [52, 48.1, -4.66, -0.83], None, [54, 57.4, 2.56, 1.42]],
    "KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[1146, 57.9, 4.27, 6.16], [133, 39.8, -5.21, -1.57], None, [88, 51.1, -0.86, -0.55]],
    "漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2413, 52.4, 1.86, 3.87], [306, 42.2, -3.56, -2.92], [43, 83.7, 4.96, 0.99], [212, 47.2, -0.63, -1.5]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": [[758, 49.5, 1.41, 0.97], [108, 32.4, -7.28, -1.58], None, [70, 61.4, 4.04, 2.06]],
    "KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%)": [[1686, 54.8, 2.86, 4.67], [197, 36.5, -6.26, -2.12], None, [135, 51.9, 0.13, -0.73]],
    "KDJ近3日內死亡交叉 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[1685, 54.8, 2.88, 4.69], [197, 36.5, -6.26, -2.12], None, [134, 52.2, 0.31, -0.71]],
    "KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[1679, 54.9, 2.86, 4.71], [197, 36.5, -6.26, -2.12], None, [134, 52.2, 0.31, -0.71]],
    "KDJ近3日內死亡交叉 ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[1507, 55.1, 2.94, 5.08], [176, 36.9, -5.67, -1.39], None, [104, 50.0, -0.58, 0.01]],
    "漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[2792, 51.9, 1.75, 3.51], [345, 42.3, -3.07, -2.18], [57, 78.9, 5.5, 0.69], [271, 48.7, 0.27, -2.29]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[1307, 46.8, 0.57, 2.91], [179, 34.1, -7.2, -3.34], None, [106, 54.7, 1.05, -0.33]],
    "均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[2717, 50.6, 1.44, 2.78], [341, 40.2, -4.13, -2.35], [42, 71.4, 1.32, -0.31], [259, 50.2, 1.06, -1.82]],
    "大盤站上20日均線 ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[2841, 50.7, 1.12, 3.19], None, [58, 93.1, 6.21, 2.56], None],
    "回後買上漲全通過 ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[662, 52.4, 2.01, 1.49], [71, 39.4, -5.93, -3.02], None, [64, 43.8, -2.93, -3.02]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": [[1278, 51.7, 1.72, 2.39], [119, 36.1, -6.0, -3.34], None, [110, 49.1, -0.15, -1.4]],
    "均線多頭排列(5>20>60且站上月線) ＋ 母子懷抱(高檔)剛形成 ＋ 短線過熱(高於月線≥20%)": [[84, 52.4, 2.62, 1.24], None, None, None],
    "多方力道≥65 ＋ 母子懷抱(高檔)剛形成 ＋ 短線過熱(高於月線≥20%)": [[83, 55.4, 4.02, 2.03], None, None, None],
    "大盤站上20日均線 ＋ 母子懷抱(高檔)剛形成 ＋ 短線過熱(高於月線≥20%)": [[88, 53.4, 2.81, 1.41], None, None, None],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 中期強勢(12-1月勝大盤≥20%) ＋ 夜星剛形成": [[132, 41.7, -2.45, -1.16], None, None, None],
    "KDJ近3日內死亡交叉 ＋ 多方力道≥65 ＋ 短線過熱(高於月線≥20%)": [[1570, 55.4, 3.06, 5.07], [185, 37.8, -6.05, -1.83], None, [119, 54.6, 0.82, -0.36]],
    "大盤站上60日均線 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[2155, 51.0, 1.53, 2.76], [267, 39.0, -4.42, -2.2], None, None],
    "半年強勢(6-1月勝大盤≥10%) ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[3315, 49.3, 1.45, 3.89], None, [67, 88.1, 8.09, 2.25], None],
    "KDJ近3日內死亡交叉 ＋ 多方力道≥80 ＋ 短線過熱(高於月線≥20%)": [[1298, 56.8, 3.42, 5.29], [157, 38.9, -5.69, -2.12], None, [105, 53.3, 0.42, -0.43]],
    "多方力道≥80 ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[2068, 54.0, 2.44, 3.78], [253, 42.3, -3.2, -1.07], None, [200, 49.0, 1.71, -0.99]],
    "短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 量能區間高檔(≥90百分位)": [[2116, 50.4, 0.98, 2.96], [274, 40.5, -4.28, -3.82], [41, 80.5, 4.97, 1.18], [216, 46.3, -1.29, -2.32]],
    "多方力道≥65 ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[2779, 51.4, 1.55, 2.78], [343, 41.4, -3.46, -2.12], [45, 73.3, 1.29, 0.55], [277, 49.8, 0.95, -1.71]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[5190, 53.6, 2.29, 6.17], None, [104, 81.7, 5.21, 5.55], None],
    "均線多頭排列(5>20>60且站上月線) ＋ 多方力道≥80 ＋ 近5日跌幅≥8%(短線超跌)": [[271, 51.7, 1.4, 1.91], [84, 65.5, 0.93, 0.09], None, [54, 51.9, 1.23, -0.13]],
    "回後買上漲全通過 ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[878, 51.0, 1.44, 1.15], [111, 36.9, -5.12, -2.46], None, [103, 49.5, 0.82, -0.32]],
    "多方力道≥80 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[4454, 54.5, 2.48, 5.88], None, [43, 74.4, 0.96, 0.23], None],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[2123, 53.0, 2.0, 4.31], [205, 44.9, -2.79, -3.55], [48, 95.8, 5.59, 2.26], [154, 46.8, -0.95, -0.92]],
    "短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期 ＋ 量能區間高檔(≥90百分位)": [[2202, 53.4, 2.32, 3.88], [277, 43.3, -3.28, -1.43], [50, 86.0, 6.71, 2.11], [223, 44.4, -0.24, -1.28]],
    "布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[3087, 50.4, 1.14, 2.53], [379, 41.7, -3.64, -2.39], [69, 78.3, 4.76, 1.59], [317, 47.0, -0.43, -2.27]],
    "OBV能量潮創60日新高 ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": [[1011, 51.4, 1.33, 2.66], [114, 37.7, -4.02, -1.59], None, [96, 45.8, -0.08, -0.96]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 近5日跌幅≥8%(短線超跌)": [[515, 49.7, 0.75, 0.52], [156, 60.3, 0.69, 1.26], None, [137, 61.3, 4.21, 0.25]],
    "KDJ近3日內死亡交叉 ＋ KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%)": [[689, 55.9, 2.82, 3.98], [87, 40.2, -4.71, -1.43], None, [50, 44.0, -1.7, -0.55]],
    "KDJ近3日內死亡交叉 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[1484, 56.5, 3.24, 4.62], [173, 34.1, -6.6, -2.46], None, [113, 53.1, -0.52, -1.16]],
    "KDJ近3日內死亡交叉 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": [[1686, 54.8, 2.86, 4.67], [197, 36.5, -6.26, -2.12], None, None],
    "KDJ近3日內死亡交叉 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": [[1531, 55.3, 3.09, 5.1], [190, 36.3, -6.39, -1.93], None, [112, 52.7, 0.45, -0.75]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": [[2491, 51.0, 1.87, 4.6], [242, 36.0, -5.9, -2.67], [47, 87.2, 7.2, 4.2], [189, 54.0, 2.63, 0.27]],
    "短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[2207, 50.7, 1.13, 3.23], [275, 43.6, -3.47, -2.91], [41, 90.2, 6.22, 2.18], [197, 44.2, -1.79, -2.25]],
    "均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2293, 50.8, 1.32, 3.25], [298, 40.6, -4.84, -3.58], None, [175, 49.1, -0.52, -1.54]],
    "創52週新高 ＋ 爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%)": [[708, 50.6, 1.69, 2.35], [86, 43.0, -4.04, -2.56], None, [62, 54.8, -0.22, -0.31]],
    "大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[6796, 51.7, 1.59, 4.39], None, [141, 82.3, 5.67, 1.53], None],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%)": [[5190, 53.6, 2.29, 6.17], [478, 42.9, -3.11, -3.28], [104, 81.7, 5.21, 5.55], [394, 50.0, 0.27, -1.88]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 多方力道≥80 ＋ 短線過熱(高於月線≥20%)": [[3680, 55.5, 2.92, 6.88], [343, 45.8, -2.15, -3.17], [31, 77.4, 1.59, 1.18], [269, 50.6, 1.0, -1.36]],
    "大盤站上20日均線 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[6776, 51.8, 1.62, 4.45], None, [140, 82.1, 5.72, 1.54], None],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[5187, 53.6, 2.29, 6.17], [478, 42.9, -3.11, -3.28], [103, 81.6, 5.28, 5.58], [394, 50.0, 0.27, -1.88]],
    "相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[6776, 51.8, 1.62, 4.45], [722, 41.6, -3.98, -3.06], [140, 82.1, 5.72, 1.54], [566, 48.4, -0.29, -4.02]],
    "均線多頭排列(5>20>60且站上月線) ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[5725, 51.8, 1.75, 4.6], None, [73, 78.1, 3.6, 0.74], None],
    "布林通道收窄(寬度近半年最低20%) ＋ 近1季財報EPS低於預期 ＋ 近5日跌幅≥8%(短線超跌)": [[360, 49.7, 0.55, -0.13], [83, 42.2, -2.92, -1.6], None, [138, 63.8, 3.76, 1.05]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 母子懷抱(高檔)剛形成 ＋ 短線過熱(高於月線≥20%)": [[73, 52.1, 1.5, 0.7], None, None, None],
    "KDJ近3日內黃金交叉 ＋ 回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": [[742, 51.9, 1.51, 1.52], [104, 40.4, -4.71, -1.71], None, [65, 46.2, -1.16, -0.53]],
    "爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS大幅優於預期(≥10%)": [[1169, 53.6, 2.62, 2.99], [152, 43.4, -3.86, -2.22], None, [110, 46.4, -0.28, 0.09]],
    "短線過熱(高於月線≥20%) ＋ 近1季財報EPS低於預期 ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[670, 48.1, 0.19, 1.34], [74, 35.1, -4.51, -2.11], None, [65, 56.9, -1.16, -1.24]],
    "大盤站上60日均線 ＋ 短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位)": [[3238, 50.6, 1.27, 2.62], [398, 41.0, -3.85, -2.4], None, None],
    "多方力道≥65 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[5974, 52.4, 1.86, 4.97], None, [79, 79.7, 3.72, 1.67], None],
    "大盤站上20日均線 ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[6700, 52.0, 1.69, 4.6], None, [139, 82.0, 5.59, 1.43], None],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[5157, 53.7, 2.34, 6.26], [478, 42.9, -3.11, -3.28], [102, 81.4, 5.09, 5.64], [393, 50.1, 0.31, -1.86]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[4659, 54.5, 2.62, 6.49], [423, 43.7, -2.26, -2.81], [82, 85.4, 5.96, 5.62], [339, 48.4, -0.54, -2.31]],
    "大盤站上20日均線 ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[2765, 52.7, 2.19, 4.0], None, [69, 87.0, 7.91, 2.75], None],
    "均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[2130, 52.4, 2.4, 4.19], [180, 47.8, -2.8, -0.89], [32, 81.2, 7.77, 1.07], [163, 47.2, -0.89, -0.34]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[2041, 54.8, 3.07, 5.29], [127, 52.0, -1.3, -1.03], [47, 87.2, 6.0, 2.33], [160, 48.1, -1.09, -0.62]],
    "短線過熱(高於月線≥20%)": [[6796, 51.7, 1.59, 4.39], [724, 41.4, -4.06, -3.11], [141, 82.3, 5.67, 1.53], [568, 48.2, -0.35, -4.05]],
    "大盤站上20日均線 ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[6437, 51.5, 1.47, 4.01], None, [139, 82.7, 5.87, 3.44], None],
    "KDJ近3日內黃金交叉 ＋ 創52週新高 ＋ 短線過熱(高於月線≥20%)": [[1017, 53.8, 2.16, 2.7], [121, 40.5, -4.9, -1.72], None, [75, 53.3, 0.3, -0.27]],
    "大盤站上20日均線 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": [[6796, 51.7, 1.59, 4.39], None, None, None],
    "大盤站上20日均線 ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[4749, 53.9, 2.4, 4.7], None, [101, 91.1, 8.41, 5.49], None],
    "短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[6700, 52.0, 1.69, 4.6], [720, 41.7, -3.94, -3.03], [139, 82.0, 5.59, 1.43], [561, 48.8, -0.23, -3.99]],
    "母子懷抱(高檔)剛形成 ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[86, 54.7, 3.3, 1.63], None, None, None],
    "母子懷抱(高檔)剛形成 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[87, 54.0, 2.93, 1.46], None, None, None],
    "母子懷抱(高檔)剛形成 ＋ 短線過熱(高於月線≥20%)": [[88, 53.4, 2.81, 1.41], None, None, None],
    "半年強勢(6-1月勝大盤≥10%) ＋ 近20日強於大盤≥10% ＋ 近5日跌幅≥8%(短線超跌)": [[719, 44.1, -1.22, -0.42], [228, 57.0, 0.53, 0.9], None, [330, 61.8, 2.88, 1.22]],
    "短線過熱(高於月線≥20%) ＋ 近1季財報EPS低於預期 ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[689, 49.1, 0.25, 1.93], [87, 31.0, -5.2, -2.01], None, [68, 54.4, 0.1, -0.18]],
    "KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[1079, 53.8, 2.09, 3.02], [92, 58.7, 1.09, 1.37], None, [100, 40.0, -3.39, -3.82]],
    "多方力道≥80 ＋ 大盤站上60日均線 ＋ 近5日跌幅≥8%(短線超跌)": [[325, 52.3, 0.87, 1.46], [101, 64.4, 0.78, -0.0], None, None],
    "KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS低於預期": [[504, 51.4, 1.02, 1.43], [55, 43.6, -3.99, -1.33], None, [40, 55.0, 0.84, 0.28]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[3260, 49.7, 1.61, 4.17], [404, 36.6, -6.12, -1.83], [66, 87.9, 7.85, 2.08], [272, 53.7, 1.68, -1.83]],
    "OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[1514, 50.5, 1.01, 1.78], [178, 46.6, -3.28, -1.69], None, [122, 44.3, -0.65, -0.71]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[2767, 51.3, 2.1, 5.26], [343, 37.3, -5.54, -1.72], [47, 87.2, 7.81, 1.55], [225, 51.1, 1.08, -1.73]],
    "回後買上漲全通過 ＋ 短線過熱(高於月線≥20%)": [[1541, 51.3, 1.57, 2.4], [167, 34.7, -6.05, -2.56], None, [141, 48.2, -0.03, -1.47]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 多方力道≥65 ＋ 短線過熱(高於月線≥20%)": [[4750, 53.7, 2.35, 6.19], [440, 43.2, -2.95, -3.3], [58, 79.3, 3.26, 2.08], [338, 51.5, 1.06, -1.03]],
    "回後買上漲全通過 ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[1537, 51.3, 1.57, 2.52], [167, 34.7, -6.05, -2.56], None, [140, 47.9, -0.29, -1.66]],
    "回後買上漲全通過 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[1538, 51.4, 1.57, 2.39], [167, 34.7, -6.05, -2.56], None, [141, 48.2, -0.03, -1.47]],
    "大盤站上20日均線 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[5795, 53.2, 2.13, 5.32], None, [104, 83.7, 6.12, 1.5], None],
    "多方力道≥65 ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[2227, 53.7, 2.64, 4.81], [173, 49.1, -1.58, -0.79], [31, 83.9, 6.39, 2.48], [184, 48.4, -0.72, -0.47]],
    "KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%) ＋ 距52週高點≤5%": [[1130, 53.5, 2.21, 3.2], [132, 43.9, -4.16, -1.2], None, [84, 51.2, 0.84, -0.88]],
    "短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[2841, 50.7, 1.12, 3.19], [328, 43.0, -3.73, -3.51], [58, 93.1, 6.21, 2.56], [239, 44.4, -1.86, -3.33]],
    "均線多頭排列(5>20>60且站上月線) ＋ 多方力道≥80 ＋ 短線過熱(高於月線≥20%)": [[4264, 54.3, 2.52, 6.23], [457, 44.6, -3.25, -2.54], [38, 71.1, -0.15, 0.21], [313, 49.5, 1.04, -2.17]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[4928, 53.2, 2.09, 5.53], [445, 42.5, -3.22, -4.36], [103, 81.6, 5.13, 5.38], [364, 49.7, -0.1, -1.61]],
    "相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[2829, 50.8, 1.16, 3.24], [328, 43.0, -3.73, -3.51], [58, 93.1, 6.21, 2.56], [238, 44.5, -1.77, -3.27]],
    "相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[2752, 52.9, 2.25, 4.04], [223, 47.5, -2.4, -0.89], [68, 86.8, 8.05, 2.81], [232, 47.8, -1.02, -1.23]],
    "短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10% ＋ 近3日向上跳空缺口": [[2712, 53.2, 2.38, 4.09], [222, 47.7, -2.32, -0.83], [67, 86.6, 7.81, 2.61], [228, 48.7, -0.95, -1.23]],
    "漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[4048, 56.0, 3.12, 5.82], [406, 45.3, -2.38, -1.9], [77, 88.3, 7.51, 4.49], [303, 48.5, -0.21, -2.01]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[3708, 55.9, 3.2, 6.63], [324, 45.7, -2.35, -2.11], [75, 92.0, 8.16, 4.83], [253, 47.0, -0.93, -1.84]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": [[4570, 53.2, 2.2, 5.71], [439, 42.1, -3.38, -3.25], [54, 79.6, 3.6, 1.62], [315, 50.8, 0.89, -1.09]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": [[2696, 53.0, 2.07, 4.61], [243, 44.0, -2.73, -1.51], [53, 81.1, 7.36, 4.01], [187, 47.6, -0.1, -0.59]],
    "布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[6437, 51.5, 1.47, 4.01], [678, 41.3, -4.07, -3.02], [139, 82.7, 5.87, 3.44], [515, 47.4, -0.73, -3.62]],
    "短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[2765, 52.7, 2.19, 4.0], [223, 47.5, -2.4, -0.89], [69, 87.0, 7.91, 2.75], [234, 47.4, -1.16, -1.32]],
    "均線多頭排列(5>20>60且站上月線) ＋ 多方力道≥65 ＋ 短線過熱(高於月線≥20%)": [[5368, 52.3, 1.93, 4.96], [594, 41.9, -3.92, -2.87], [59, 74.6, 0.98, 0.29], [398, 50.0, 0.76, -2.44]],
    "多方力道≥80 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[4119, 55.1, 2.75, 6.14], [433, 44.1, -2.9, -2.27], [36, 69.4, -0.08, -0.29], [301, 46.8, -0.12, -2.94]],
    "布林通道高檔(≥80%) ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[6419, 51.6, 1.49, 4.07], [677, 41.4, -4.03, -2.99], [138, 82.6, 5.92, 3.46], [513, 47.6, -0.67, -3.55]],
    "布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[2748, 52.6, 2.16, 4.05], [223, 47.5, -2.4, -0.89], [69, 87.0, 7.91, 2.75], [231, 46.8, -1.43, -1.43]],
    "短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期 ＋ 近20日強於大盤≥10%": [[4694, 54.3, 2.51, 4.81], [486, 45.1, -3.23, -1.35], [100, 91.0, 8.51, 5.52], [356, 46.6, -0.75, -2.99]],
    "均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": [[5725, 51.8, 1.75, 4.6], [639, 41.2, -4.26, -3.14], [73, 78.1, 3.6, 0.74], [436, 50.0, 0.5, -2.99]],
    "布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[6345, 51.8, 1.57, 4.23], [675, 41.5, -3.98, -2.97], [137, 82.5, 5.79, 3.35], [508, 48.0, -0.61, -3.53]],
    "均線多頭排列(5>20>60且站上月線) ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[5720, 51.9, 1.77, 4.64], [639, 41.2, -4.26, -3.14], [73, 78.1, 3.6, 0.74], [436, 50.0, 0.5, -2.99]],
    "回後買上漲全通過 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": [[1541, 51.3, 1.57, 2.4], [167, 34.7, -6.05, -2.56], None, None],
    "均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[4028, 54.4, 2.71, 4.56], [440, 43.9, -3.59, -1.94], [50, 86.0, 6.43, 1.54], [290, 46.9, -0.26, -2.3]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 夜星剛形成 ＋ 近1季財報EPS大幅優於預期(≥10%)": [[179, 43.0, -1.29, -0.49], [46, 60.9, 2.02, -0.68], None, [44, 72.7, 10.11, 1.49]],
    "爆量(≥2倍均量) ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[942, 51.0, 1.48, 2.82], [100, 48.0, -2.42, -2.67], None, [94, 45.7, -2.42, -2.37]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": [[3315, 49.3, 1.45, 3.89], [406, 36.5, -6.19, -1.89], [67, 88.1, 8.09, 2.25], [273, 53.5, 1.58, -1.84]],
    "KDJ近3日內死亡交叉 ＋ 創52週新高 ＋ 短線過熱(高於月線≥20%)": [[532, 57.5, 4.0, 4.7], [68, 35.3, -6.61, -2.16], None, [32, 50.0, 0.12, 0.37]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[3307, 49.4, 1.49, 3.96], [406, 36.5, -6.19, -1.89], [67, 88.1, 8.09, 2.25], [272, 53.7, 1.68, -1.83]],
    "大盤站上60日均線 ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2832, 51.1, 1.4, 3.76], [355, 40.6, -4.58, -3.89], None, None],
    "MACD近3日內黃金交叉 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[616, 40.7, -2.27, -2.35], None, None, None],
    "回後買上漲全通過 ＋ 多方力道≥65 ＋ 短線過熱(高於月線≥20%)": [[1302, 50.6, 1.34, 2.31], [148, 34.5, -5.77, -1.98], None, [112, 50.9, 0.74, -0.98]],
    "多方力道≥80 ＋ 近20日強於大盤≥10% ＋ 近5日跌幅≥8%(短線超跌)": [[294, 54.4, 1.48, 1.89], [97, 63.9, 0.76, -0.37], None, [67, 49.3, 0.09, -1.28]],
    "回後買上漲全通過 ＋ 多方力道≥80 ＋ 短線過熱(高於月線≥20%)": [[931, 53.1, 2.37, 2.77], [98, 36.7, -5.09, -1.95], None, [87, 47.1, -0.14, -1.61]],
    "短線過熱(高於月線≥20%) ＋ 量能區間高檔(≥90百分位) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[1890, 50.3, 0.87, 2.36], [240, 44.2, -3.56, -2.94], [38, 89.5, 7.09, 2.6], [187, 43.3, -2.52, -2.86]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": [[5190, 53.6, 2.29, 6.17], [478, 42.9, -3.11, -3.28], None, None],
    "多方力道≥65 ＋ 短線過熱(高於月線≥20%)": [[5974, 52.4, 1.86, 4.97], [650, 42.0, -3.81, -2.89], [79, 79.7, 3.72, 1.67], [470, 49.6, 0.25, -2.99]],
    "漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[5795, 53.2, 2.13, 5.32], [622, 42.1, -3.27, -2.55], [104, 83.7, 6.12, 1.5], [465, 48.2, -0.55, -3.85]],
    "MACD近3日內黃金交叉 ＋ 多方力道≥65 ＋ 短線過熱(高於月線≥20%)": [[423, 43.5, -1.16, -1.29], [54, 53.7, -0.57, 0.32], None, [45, 44.4, -1.57, -0.56]],
    "多方力道≥65 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[5970, 52.4, 1.87, 4.98], [650, 42.0, -3.81, -2.89], [79, 79.7, 3.72, 1.67], [469, 49.7, 0.27, -2.98]],
    "多方力道≥65 ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[5942, 52.4, 1.87, 4.99], [649, 42.1, -3.79, -2.88], [78, 79.5, 3.46, 1.57], [466, 50.0, 0.31, -2.96]],
    "漲時量≥跌時量1.5倍(近20日) ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[5792, 53.3, 2.14, 5.36], [622, 42.1, -3.27, -2.55], [104, 83.7, 6.12, 1.5], [465, 48.2, -0.55, -3.85]],
    "漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[5775, 53.3, 2.18, 5.45], [622, 42.1, -3.27, -2.55], [103, 83.5, 5.94, 1.39], [465, 48.2, -0.55, -3.85]],
    "多方力道≥80 ＋ 短線過熱(高於月線≥20%)": [[4454, 54.5, 2.48, 5.88], [481, 44.7, -3.23, -2.64], [43, 74.4, 0.96, 0.23], [352, 48.3, 0.54, -2.78]],
    "短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[4749, 53.9, 2.4, 4.7], [488, 44.9, -3.29, -1.42], [101, 91.1, 8.41, 5.49], [362, 45.9, -0.88, -3.07]],
    "多方力道≥65 ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[4173, 54.9, 2.84, 5.69], [440, 44.5, -3.23, -1.36], [52, 84.6, 5.09, 2.4], [307, 46.3, -0.31, -2.17]],
    "多方力道≥80 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[4454, 54.5, 2.48, 5.88], [481, 44.7, -3.23, -2.64], [43, 74.4, 0.96, 0.23], [351, 48.4, 0.57, -2.76]],
    "多方力道≥80 ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[4449, 54.5, 2.48, 5.89], [481, 44.7, -3.23, -2.64], [43, 74.4, 0.96, 0.23], [349, 48.7, 0.57, -2.75]],
    "相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[4741, 54.0, 2.41, 4.7], [488, 44.9, -3.29, -1.42], [100, 91.0, 8.51, 5.52], [360, 46.1, -0.79, -3.02]],
    "均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[5689, 51.9, 1.81, 4.75], [639, 41.2, -4.26, -3.14], [73, 78.1, 3.6, 0.74], [436, 50.0, 0.5, -2.99]],
    "均線多頭排列(5>20>60且站上月線) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[5080, 52.9, 2.12, 5.37], [557, 41.5, -3.58, -2.48], [59, 76.3, 2.95, 0.04], [370, 48.1, -0.19, -3.19]],
    "OBV能量潮創60日新高 ＋ 多方力道≥80 ＋ 短線過熱(高於月線≥20%)": [[2265, 54.3, 2.57, 4.92], [227, 43.6, -3.1, -1.44], None, [154, 44.2, -0.48, -1.74]],
    "中期強勢(12-1月勝大盤≥20%) ＋ 夜星剛形成 ＋ 近1季財報EPS低於預期": [[83, 41.0, -1.56, -0.37], None, None, None],
    "布林通道高檔(≥80%) ＋ 母子懷抱(高檔)剛形成 ＋ 短線過熱(高於月線≥20%)": [[76, 56.6, 3.77, 1.68], None, None, None],
    "半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[1269, 49.8, 2.19, 3.13], [109, 41.3, -4.29, -1.48], [37, 89.2, 10.72, 3.44], [100, 53.0, 1.61, 0.53]],
    "KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[1433, 53.5, 1.95, 2.74], [171, 46.8, -2.55, -0.27], None, [126, 44.4, -1.82, -2.66]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ MACD近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%)": [[375, 43.7, -1.28, -0.99], [53, 50.9, -2.17, -0.36], None, [32, 50.0, -0.07, -0.61]],
    "半年強勢(6-1月勝大盤≥10%) ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[3128, 49.0, 1.29, 3.52], [372, 36.3, -6.16, -1.35], [65, 89.2, 8.6, 4.04], [240, 53.8, 1.25, -1.32]],
    "KDJ近3日內黃金交叉 ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[791, 52.1, 1.33, 1.88], [112, 50.9, -1.67, -1.5], None, [69, 40.6, -3.57, -2.51]],
    "短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期 ＋ 近3日向上跳空缺口": [[2025, 54.2, 2.83, 4.29], [157, 49.7, -2.23, -1.15], [49, 93.9, 9.34, 3.51], [160, 45.0, -2.41, -1.89]],
    "多方力道≥65 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[5271, 53.4, 2.24, 5.51], [570, 41.8, -3.36, -2.53], [65, 76.9, 3.43, 0.55], [399, 48.6, -0.08, -3.16]],
    "OBV能量潮創60日新高 ＋ 大盤站上20日均線 ＋ 短線過熱(高於月線≥20%)": [[3320, 51.8, 1.59, 3.64], None, [68, 79.4, 7.5, 2.26], None],
    "回後買上漲全通過 ＋ 短線過熱(高於月線≥20%) ＋ 近3日向上跳空缺口": [[737, 53.9, 2.74, 3.65], [51, 41.2, -2.61, -1.03], None, [68, 42.6, -2.8, -1.84]],
    "CMF資金流買方佔優(近20日≥0.1) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS低於預期": [[1221, 48.6, -0.05, 1.03], [127, 32.3, -6.46, -4.4], None, [91, 61.5, 4.02, 1.86]],
    "大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": [[6796, 51.7, 1.59, 4.39], [724, 41.4, -4.06, -3.11], None, None],
    "多方力道≥65 ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[2392, 51.5, 1.42, 3.68], [275, 44.0, -3.46, -2.86], [33, 93.9, 5.49, 2.27], [192, 45.8, -1.39, -2.19]],
    "多方力道≥65 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": [[5974, 52.4, 1.86, 4.97], [650, 42.0, -3.81, -2.89], None, None],
    "多方力道≥80 ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[1626, 54.3, 2.12, 3.45], [192, 45.8, -3.04, -2.07], None, [122, 43.4, -1.02, -0.96]],
    "多方力道≥80 ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": [[4454, 54.5, 2.48, 5.88], [481, 44.7, -3.23, -2.64], None, None],
    "大盤站上60日均線 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[6776, 51.8, 1.62, 4.45], [722, 41.6, -3.98, -3.06], None, None],
    "多方力道≥65 ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[5625, 52.1, 1.72, 4.56], [606, 41.7, -3.89, -2.39], [77, 80.5, 4.03, 2.68], [421, 48.9, -0.12, -2.64]],
    "布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[4533, 53.7, 2.29, 3.98], [457, 45.1, -3.23, -1.81], [100, 91.0, 8.36, 5.47], [339, 45.1, -1.36, -3.28]],
    "OBV能量潮創60日新高 ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[3290, 51.9, 1.63, 3.66], [364, 41.8, -3.86, -2.18], [67, 80.6, 8.07, 4.97], [245, 48.6, 0.5, -0.84]],
    "KDJ近3日內死亡交叉 ＋ 短線過熱(高於月線≥20%) ＋ 距52週高點≤5%": [[642, 60.0, 4.05, 5.5], [74, 37.8, -4.6, -1.4], None, [39, 53.8, 0.63, 0.6]],
    "相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS低於預期": [[1707, 48.1, 0.12, 1.32], [191, 33.5, -6.24, -4.03], [35, 57.1, -2.57, -1.21], [147, 56.5, 1.34, -0.79]],
    "大盤站上60日均線 ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[6700, 52.0, 1.69, 4.6], [720, 41.7, -3.94, -3.03], None, None],
    "均線多頭排列(5>20>60且站上月線) ＋ 大盤站上60日均線 ＋ 短線過熱(高於月線≥20%)": [[5725, 51.8, 1.75, 4.6], [639, 41.2, -4.26, -3.14], None, None],
    "半年強勢(6-1月勝大盤≥10%) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 近5日跌幅≥8%(短線超跌)": [[773, 44.0, -1.08, -1.23], [224, 60.3, 1.54, 1.28], None, [267, 61.0, 2.83, 0.54]],
    "OBV能量潮創60日新高 ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[3151, 52.2, 1.73, 3.84], [350, 42.3, -3.75, -2.23], [62, 79.0, 6.82, 1.93], [236, 47.5, 0.03, -1.64]],
    "OBV能量潮創60日新高 ＋ 多方力道≥65 ＋ 短線過熱(高於月線≥20%)": [[2971, 51.8, 1.71, 3.9], [327, 41.6, -3.43, -1.94], [45, 68.9, 2.11, 0.61], [214, 47.2, 0.39, -1.21]],
    "大盤站上60日均線 ＋ 短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期": [[4749, 53.9, 2.4, 4.7], [488, 44.9, -3.29, -1.42], None, None],
    "多方力道≥80 ＋ 大盤站上20日均線 ＋ 近5日跌幅≥8%(短線超跌)": [[325, 52.3, 0.87, 1.46], None, None, None],
    "均線多頭排列(5>20>60且站上月線) ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[5375, 51.5, 1.6, 4.12], [594, 40.9, -4.33, -3.1], [72, 79.2, 4.07, 1.74], [388, 49.7, 0.2, -2.48]],
    "短線過熱(高於月線≥20%) ＋ 近1季財報EPS優於預期 ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[2024, 52.9, 1.84, 3.17], [243, 46.1, -3.44, -2.12], [42, 100.0, 7.76, 3.72], [156, 37.2, -2.96, -3.16]],
    "大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成 ＋ 近3日向上跳空缺口": [None, None, None, [112, 73.2, 7.61, 2.08]],
    "回後買上漲全通過 ＋ 短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10%": [[1522, 51.6, 1.66, 2.52], [167, 34.7, -6.05, -2.56], None, [139, 48.9, 0.03, -1.43]],
    "MACD近3日內黃金交叉 ＋ 爆量(≥1.5倍均量) ＋ 短線過熱(高於月線≥20%)": [[384, 42.7, -1.54, -1.66], [62, 45.2, -4.37, -1.53], None, [43, 55.8, 0.46, -0.26]],
    "布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[2822, 50.9, 1.19, 3.38], [323, 43.3, -3.63, -3.35], [58, 93.1, 6.21, 2.56], [238, 44.5, -1.81, -3.23]],
    "OBV能量潮創60日新高 ＋ 相對強弱為正(強於大盤) ＋ 短線過熱(高於月線≥20%)": [[3314, 51.8, 1.58, 3.67], [366, 41.8, -3.82, -2.22], [68, 79.4, 7.5, 2.26], [247, 48.2, 0.26, -1.28]],
    "短線過熱(高於月線≥20%) ＋ 近20日強於大盤≥10% ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[2786, 51.0, 1.25, 3.35], [326, 43.3, -3.63, -3.44], [57, 93.0, 5.9, 2.22], [235, 45.1, -1.73, -3.25]],
    "漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": [[2385, 51.6, 1.55, 3.92], [276, 44.9, -2.36, -2.29], [51, 94.1, 6.52, 2.64], [194, 43.3, -1.77, -2.64]],
    "短線過熱(高於月線≥20%) ＋ 近1季財報EPS低於預期": [[1711, 48.1, 0.12, 1.34], [191, 33.5, -6.24, -4.03], [35, 57.1, -2.57, -1.21], [147, 56.5, 1.34, -0.79]],
    "OBV能量潮創60日新高 ＋ 短線過熱(高於月線≥20%)": [[3320, 51.8, 1.59, 3.64], [366, 41.8, -3.82, -2.22], [68, 79.4, 7.5, 2.26], [247, 48.2, 0.26, -1.28]],
    "強勢突破盤 ＋ 短線過熱(高於月線≥20%) ＋ 連續放量(近3日均量≥1.5倍前20日均量)": [[2132, 51.2, 1.3, 4.08], [258, 43.0, -3.42, -2.18], [40, 82.5, 2.08, 0.18], [197, 50.8, 0.18, -0.9]],
    "布林通道高檔(≥80%) ＋ 漲時量≥跌時量1.5倍(近20日) ＋ 短線過熱(高於月線≥20%)": [[5468, 52.9, 1.98, 4.69], [584, 42.0, -3.33, -3.08], [103, 84.5, 6.47, 3.42], [424, 46.7, -0.97, -3.86]],
    "大盤站上60日均線 ＋ 布林通道高檔(≥80%) ＋ 短線過熱(高於月線≥20%)": [[6437, 51.5, 1.47, 4.01], [678, 41.3, -4.07, -3.02], None, None],
    "回後買上漲全通過 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": [[1266, 50.3, 1.21, 1.98], [153, 34.0, -6.3, -2.28], None, [110, 50.9, 0.64, -0.92]],
    "OBV能量潮創60日新高 ＋ 半年強勢(6-1月勝大盤≥10%) ＋ 短線過熱(高於月線≥20%)": [[1680, 48.9, 1.26, 2.58], [201, 35.3, -6.2, -1.93], [38, 86.8, 10.04, 2.36], [124, 53.2, 1.9, 0.58]],
    "OBV能量潮創60日新高 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 短線過熱(高於月線≥20%)": [[2980, 51.6, 1.75, 4.09], [332, 41.6, -3.7, -1.74], [44, 70.5, 2.22, 0.48], [207, 48.3, 0.23, -0.99]],
}


def CONSTANTS():
    return dict(SP500_LIST=SP500_LIST, SP500_NAMES=SP500_NAMES, NASDAQ100_LIST=NASDAQ100_LIST, SOX_LIST=SOX_LIST,
                MY_LIST_DEFAULT=MY_LIST_DEFAULT, PINNED_COMBOS=PINNED_COMBOS, PINNED_COMBO_WINRATES=PINNED_COMBO_WINRATES,
                MOONSHOT_COMBOS=MOONSHOT_COMBOS, MOONSHOT_COMBO_STATS=MOONSHOT_COMBO_STATS,
                STOCK_PICK_COMBOS=STOCK_PICK_COMBOS, STOCK_PICK_STATS=STOCK_PICK_STATS, COMBO_REF_STATS=COMBO_REF_STATS,
                BT_WIN_COMBOS=BT_WIN_COMBOS, BT_WIN_STATS=BT_WIN_STATS, BT_HOT_COMBOS=BT_HOT_COMBOS, BT_HOT_STATS=BT_HOT_STATS,
                REGIME_STATS=REGIME_STATS)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'daily':
        sys.exit(run_daily(sys.argv[2:]))
    main()
