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
#   - 指定組合：S＝選股型（2023-10～2026-08 美股三年回測）／#＝高勝率（標⏱為擇時型）／M＝高標股。
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
                 'plusDI', 'minusDI', 'adx', 'adxr', '_zz', '_good', '_filtered', '_fmap')

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
    """52週高點距離／創新高、均線多頭排列、布林寬度在近半年的百分位、近3日向上跳空。資料不足時為 None"""
    out = dict(high52Dist=None, newHigh52=None, maBull=None, bbwRank=None, gapUp3=None)
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
    row.update((extras or {}).get('tech') or tech_extras(b, e))
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
]
# 不放進「多因子複選搜尋／飆股搜尋」的條件（回測貢獻極低或與個別型態重複；指定組合比對與統計仍可使用）
SEARCH_EXCLUDE_FLAGS = {'型態成形中', '型態突破確認', '型態剛形成(剛突破)', '突破上升軌道線剛形成',
                        '分價量表-守穩POC買進', '分價量表-突破POC追價買進', '分價量表-反彈POC遇壓賣出', '分價量表-破位停損賣出'}


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


def fetch_market_universe(token):
    j = fmp_get('/company-screener', dict(exchange='NYSE,NASDAQ', country='US', isEtf='false', isFund='false',
                                          isActivelyTrading='true', volumeMoreThan=50000, limit=3000), token, timeout=60)
    if not isinstance(j, list) or not j:
        raise RuntimeError('company-screener 回傳空清單，請確認 API Key 是否有效')
    return [dict(id=r['symbol'], name=r.get('companyName') or r['symbol']) for r in j if r.get('symbol')]


def fetch_market_snapshot(token, progress=None):
    """全市場（NYSE+NASDAQ，約3000檔）逐檔查 /quote，平行查詢＋限流；回傳 [{id,name,pct,volume}]"""
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
def add_derived(df):
    if df.attrs.get('derived'):
        return df
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
    df = add_derived(df)
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


def bt_memo(ss, name, params, fn):
    """回測分析結果快取（存在 session）：同一份回測資料、同樣參數只算一次，
    改任何一個選單時 Streamlit 會整頁重跑，沒有快取的話每次都要重算幾十秒。"""
    df = ss.get('bt_df')
    sig = (id(df), len(df) if df is not None else 0)
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
    """一次載入一個或多個回測原始紀錄檔（例如三年分三段各一個），合併後去除重複（同日同檔）"""
    parts = []
    for f in files:
        if hasattr(f, 'seek'):
            f.seek(0)
        d = pd.read_csv(f, compression='gzip' if f.name.endswith('.gz') else None, dtype={'stockId': str})
        for c in d.columns:   # 先逐檔轉 float32，合併時記憶體高峰較低
            if c not in ('evalDate', 'stockId', 'name') and (pd.api.types.is_float_dtype(d[c]) or pd.api.types.is_integer_dtype(d[c])):
                d[c] = d[c].astype(np.float32)
        d['evalDate'] = d['evalDate'].astype(str)
        parts.append(d)
    df = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    del parts
    if len(files) > 1:
        df = df.drop_duplicates(['evalDate', 'stockId'], keep='last').sort_values(['evalDate', 'stockId']).reset_index(drop=True)
    return store_bt_df(df)


BT_SEG_OPTIONS = ['自訂月數', '三年分三段・第1段（最近1年）', '三年分三段・第2段（1～2年前）', '三年分三段・第3段（2～3年前）']

WINSOR_OPTIONS = {'截尾 1%／99%（建議）': 0.01, '截尾 0.5%／99.5%': 0.005, '不處理': 0.0}
ANALYSIS_DEFAULTS = dict(min_px=5.0, wq=0.01)


def prep_analysis_df(df, min_px=5.0, wq=0.01):
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
                 stock_timeout=150, stall_timeout=300, end_offset_months=0):
    """回測：多執行緒平行抓資料（所有請求共用限流器），每檔算完就轉成 DataFrame 以節省記憶體。
    universe 可以是全美股約3000檔（fetch_market_universe），時間主要花在 API：3000檔約需 11～15 分鐘。"""
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
                    frames.append(d)
                    saved += len(d)
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
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    frames.clear()
    import gc
    gc.collect()
    if len(df):
        df['evalDate'] = df['evalDate'].astype(str)
        df = df.sort_values(['evalDate', 'stockId']).reset_index(drop=True)
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
        rec['財報日'] = earnings_label(r.get('earnings'))
        rec['_pbPass'] = pb['allPass']
        rec['_pt'] = 'justbreak' if pt['anyJustBroke'] else ('breakout' if pt['anyBreakout'] else ('forming' if pt['anyFormed'] else 'none'))
        recs.append(rec)
    df = pd.DataFrame(recs)
    if not len(df):
        return df
    # 欄位順序：股票、名稱之後依序放 股價、漲跌幅%、選股型命中、指定組合命中、型態確認；不顯示 評等、命中數、+DI/-DI/ADX/ADXR
    df = df.rename(columns={'收盤': '股價'}).drop(columns=['評等', '命中數', '+DI', '-DI', 'ADX', 'ADXR'], errors='ignore')
    front = ['股票', '名稱', '股價', '漲跌幅%', '命中類別數', '選股型命中', '指定組合命中', '回測高勝率命中', '回測飆股命中', '型態確認', '財報日']
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
        bm = fetch_benchmark(token, days)
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
            extras = dict(realtime=rt, peRange=None, peRangeErr=None, revRange=None, priceYoYRange=None,
                          sector=None, sectorBenchmark=None, tech=tech)
            extras['earnings'] = (earn_cal or {}).get(to_fmp_symbol(sid).upper())
            if extras['earnings'] is None:
                try:
                    extras['earnings'] = fetch_next_earnings(sid, token)
                except Exception:  # noqa
                    pass
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
        return df
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
  top100＝全市場約3000檔逐檔查報價後取漲幅前100＋成交量前100（限流280次/分，約需10多分鐘）。
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

    # ── 漲幅／成交量前100（全市場約3000檔逐檔查報價）：在畫出輸入框前先把清單換掉 ──
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
        if cg.button('🔥 漲幅前100', use_container_width=True, help='全市場約3000檔逐檔查報價，約需10多分鐘'):
            ss['pending_top100'] = 'gain'
            st.rerun()
        if cv.button('📊 成交量前100', use_container_width=True, help='全市場約3000檔逐檔查報價，約需10多分鐘'):
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
        st.markdown('**🔬 歷史回測分析（美股，預設全美股約3000檔）**')
        bt_univ = st.selectbox('回測股票池', ['全美股（約3000檔）', 'S&P 500', 'Nasdaq-100', 'SOX半導體', '目前輸入框清單'],
                               key='bt_univ', help='全美股＝FMP company-screener：NYSE＋NASDAQ 可交易個股（排除ETF／基金，日均量>5萬股），約3000檔')
        bt_workers = st.number_input('回測平行抓取數', 1, 32, 8, 1, key='bt_workers', help='同時抓幾檔；總請求數仍受每分鐘上限控制')
        bt_seg = st.selectbox('回測期間', BT_SEG_OPTIONS, key='bt_seg',
                              help='三年一次跑完資料量太大（容易記憶體不足當掉），改成分三次：每次跑一年，各自下載原始紀錄檔，最後三個檔一起載入合併分析')
        if bt_seg == BT_SEG_OPTIONS[0]:
            bt_months = st.number_input('回測天數（月）', 1, 36, 3, 1, key='bt_months')
            bt_off = 0
        else:
            bt_months, bt_off = 12, (BT_SEG_OPTIONS.index(bt_seg) - 1) * 12
            st.caption(f'本次評估區間：{bt_off}～{bt_off + 12} 個月前。每段跑完到「🔬 歷史回測」分頁按「產生回測原始紀錄檔」下載；'
                       '三段都下載後，三個檔一起拖進下方「載入」即可合併分析，多因子／飆股搜尋會自動逐段（第1／2／3段）驗證。')
        bt_div = st.checkbox('📈 近1季YoY乖離度（股價歷史要抓超過1年，明顯拉長時間）', key='bt_div')
        bt_sector = st.checkbox('🏭 類股狀態濾網（每檔多一次 sector 查詢）', key='bt_sector')
        bt_minpx = st.number_input('最低股價（美元，0＝不篩）', 0.0, 1000.0, 5.0, 1.0, key='bt_minpx',
                                   help='評估當天收盤價低於此價的紀錄不收（排除雞蛋水餃股，也省記憶體）')
        bt_liq = st.number_input('最低近20日均成交金額（萬美元，0＝不篩）', 0, 1000000, 500, 100, key='bt_liq',
                                 help='全美股建議 500 萬美元以上，排除成交清淡的小型股')
        bt_delay = st.number_input('回測每檔間隔秒數（已有限流器）', 0.0, 5.0, 0.0, 0.1, key='bt_delay')
        run_bt = st.button('🔬 執行歷史回測', use_container_width=True, key='run_bt')
        st.caption('營收YoY用季報，公告延遲以季末+45天估計（非精確申報日）；美股沒有三大法人資料。')
        ups = st.file_uploader('或載入先前匯出的回測原始紀錄（.csv / .csv.gz，可一次選多個檔合併，例如三段各一個）',
                               type=['csv', 'gz'], accept_multiple_files=True)
        up_sig = tuple(sorted((u.name, u.size) for u in ups)) if ups else None
        if up_sig and ss.get('bt_loaded_name') != up_sig:
            try:
                with st.spinner(f'載入 {len(ups)} 個檔案中…'):
                    ss['bt_df'] = load_bt_files(ups)
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
                                   end_offset_months=bt_off)
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
    sig = (id(df), len(df), mp, wq)
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
        st.caption('🧮 命中組合的 t值／勝率／飆股比例：尚未跑回測，先用回測記錄值（選股型 S 為 2023-10～2026-08 三年回測；沒進榜的組合沒有 t 值）；跑過回測或載入回測紀錄後會改用即時計算。')
    df = summary_frame(results, exf, K, live)

    with st.expander(f"「指定組合命中」編號對照：[選股型] S1~S{len(K['STOCK_PICK_COMBOS'])}　／　[高勝率] #1~#{len(K['PINNED_COMBOS'])}　／　[高標股] M1~M{len(K['MOONSHOT_COMBOS'])}"):
        st.caption('S＝選股型：2023-10～2026-08 美股三年回測分三段，每一段扣掉同一天大盤後都仍有超額報酬（t≥2），最值得看（美股幅度較小，約每10～20日 +0.3～0.8%）。'
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
        st.caption(f"🔵 W＝回測高勝率（{len(K.get('BT_WIN_COMBOS', []))}組，青色）：三年回測任一天期勝率>60% 且 t>2；標 ⏱ 的是擇時型。"
                   f"　🔴 F＝回測飆股（{len(K.get('BT_HOT_COMBOS', []))}組，粉紅色）：10/20日飆股比例>10% 且飆股次數≥10。摘要表每類只顯示最強的{BT_HIT_SHOW}個，其餘以 +N 表示。"
                   "　🔢 命中類別數＝S/#/M/W/F 五類中命中幾類；同類組合常共用同一訊號，類別數比總命中數更能反映強度（回測：≥3類 台股20日超額約+2~4%、美股+0.6~1.8%，但波動也較大）。")
        tw_ = pd.DataFrame([{'編號': f'W{i + 1}', '類型': combo_type_label(c), '條件組合': '＋'.join(c),
                             '三年回測': '　'.join(f"{h}日 勝率{e['win']}% t={e['t']}" for h, e in sorted(K['BT_WIN_STATS'].get(' ＋ '.join(c), {}).items(), key=lambda kv: int(kv[0])))}
                            for i, c in enumerate(K.get('BT_WIN_COMBOS', []))])
        tf_ = pd.DataFrame([{'編號': f'F{i + 1}', '條件組合': '＋'.join(c),
                             '三年回測': format_moonshot_stats(K['BT_HOT_STATS'].get(' ＋ '.join(c)))}
                            for i, c in enumerate(K.get('BT_HOT_COMBOS', []))])
        st.dataframe(tw_, hide_index=True, use_container_width=True, height=240)
        st.dataframe(tf_, hide_index=True, use_container_width=True, height=240)

    c1, c2, c3, c4 = st.columns([1.2, 1.4, 1.4, 1])
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
    view = df[m].drop(columns=['_pbPass', '_pt'])
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
    if ss.get('_bt_csv_sig') == id(ss.get('bt_df')) and ss.get('_bt_csv'):
        st.download_button('💾 下載回測原始紀錄（.csv.gz，之後可直接載入，不用重抓）', ss['_bt_csv'],
                           file_name=f'美股回測原始紀錄{("_" + ss["bt_seg_label"]) if ss.get("bt_seg_label") else ""}_{dt.date.today()}.csv.gz', key='dl_btcsv')
    elif st.button('💾 產生回測原始紀錄檔（之後可直接載入，不用重抓）', key='mk_btcsv'):
        with st.spinner('壓縮中…'):
            ss['_bt_csv'] = bt_csv_gz(df_all)
            ss['_bt_csv_sig'] = id(ss.get('bt_df'))
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
# 選股型指定組合（S編號）：2023-10～2026-08 美股三年回測（全美股、股價≥5美元，分三段），三段的同日調整 t 值都 ≥2 的組合
# （2026-09-30 改版；舊版 S1～S7 夜星類三年都沒有選股效果，夜星改列高標股 M）
STOCK_PICK_COMBOS = [
    ["大盤站上20日均線", "距52週高點≤5%", "近3日向上跳空缺口"],
    ["相對強弱為正(強於大盤)", "量能區間低檔(≤10百分位)", "近3日向上跳空缺口"],
    ["KDJ近3日內死亡交叉", "距52週高點≤5%", "近3日向上跳空缺口"],
    ["爆量(≥1.5倍均量)", "距52週高點≤5%", "近3日向上跳空缺口"],
    ["強勢突破盤", "大盤站上20日均線", "距52週高點≤5%"],
]
STOCK_PICK_STATS = {
    "大盤站上20日均線 ＋ 距52週高點≤5% ＋ 近3日向上跳空缺口": {"10": {"n": 29447, "win": 54.3, "ret": 0.75, "ex": 0.45, "t": 11.5}, "20": {"n": 29447, "win": 53.7, "ret": 1.11, "ex": 0.66, "t": 11.57}},
    "相對強弱為正(強於大盤) ＋ 量能區間低檔(≤10百分位) ＋ 近3日向上跳空缺口": {"10": {"n": 3511, "win": 54.7, "ret": 1.05, "ex": 0.4, "t": 3.45}, "20": {"n": 3511, "win": 55.7, "ret": 1.87, "ex": 0.83, "t": 4.79}},
    "KDJ近3日內死亡交叉 ＋ 距52週高點≤5% ＋ 近3日向上跳空缺口": {"10": {"n": 5100, "win": 52.7, "ret": 0.59, "ex": 0.4, "t": 4.34}, "20": {"n": 5100, "win": 54.2, "ret": 1.24, "ex": 0.77, "t": 5.88}},
    "爆量(≥1.5倍均量) ＋ 距52週高點≤5% ＋ 近3日向上跳空缺口": {"10": {"n": 7526, "win": 54.1, "ret": 0.77, "ex": 0.31, "t": 3.4}, "20": {"n": 7526, "win": 54.7, "ret": 1.46, "ex": 0.67, "t": 5.27}},
    "強勢突破盤 ＋ 大盤站上20日均線 ＋ 距52週高點≤5%": {"10": {"n": 43665, "win": 53.7, "ret": 0.55, "ex": 0.34, "t": 10.29}, "20": {"n": 43665, "win": 52.8, "ret": 0.85, "ex": 0.51, "t": 10.64}},
}
# 2026-09-27 美股回測（勝率榜5/10/20日＋飆股榜5/10/20日）中，指定組合的勝率／t值(同日調整)／飆股比例記錄值
# key＝條件依字元排序後以「 ＋ 」連接
COMBO_REF_STATS = {
    # ── 2026-09-30 三年回測（2023-10～2026-08）新增組合的記錄值 ──
    "大盤站上20日均線 ＋ 距52週高點≤5% ＋ 近3日向上跳空缺口": {"win": {"5": 52.7, "10": 54.3, "20": 53.7}, "t": {"5": 6.44, "10": 11.5, "20": 11.57}, "moon": {"10": 0.5, "20": 1.7}},
    "相對強弱為正(強於大盤) ＋ 近3日向上跳空缺口 ＋ 量能區間低檔(≤10百分位)": {"win": {"5": 54.4, "10": 54.7, "20": 55.7}, "t": {"5": 2.63, "10": 3.45, "20": 4.79}, "moon": {"10": 0.5, "20": 2.0}},
    "KDJ近3日內死亡交叉 ＋ 距52週高點≤5% ＋ 近3日向上跳空缺口": {"win": {"5": 52.6, "10": 52.7, "20": 54.2}, "t": {"5": 1.29, "10": 4.34, "20": 5.88}, "moon": {"10": 0.4, "20": 1.3}},
    "爆量(≥1.5倍均量) ＋ 距52週高點≤5% ＋ 近3日向上跳空缺口": {"win": {"5": 52.5, "10": 54.1, "20": 54.7}, "t": {"5": 0.38, "10": 3.4, "20": 5.27}, "moon": {"10": 1.0, "20": 2.5}},
    "大盤站上20日均線 ＋ 強勢突破盤 ＋ 距52週高點≤5%": {"win": {"5": 50.7, "10": 53.7, "20": 52.8}, "t": {"5": 2.85, "10": 10.29, "20": 10.64}, "moon": {"10": 0.5, "20": 1.7}},
    "夜星剛形成 ＋ 大盤跌破60日均線": {"win": {"5": 59.1, "10": 57.7, "20": 62.6}, "t": {"5": 0.82, "10": 2.53, "20": 4.53}, "moon": {"10": 5.4, "20": 14.6}},
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
    "夜星剛形成 ＋ 大盤跌破60日均線": {"win": {"5": 72.4, "10": 67.2, "20": 67.2}, "t": {"5": 2.54, "10": 2.86, "20": 4.26}, "moon": {"20": 17.2}},
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



# ── 回測自動入選組合（2026-09-30，美股 2023-10～2026-08 三年回測）──
# W＝回測高勝率：5/10/20日任一天期勝率>60% 且 t值(同日調整)>2（樣本數≥100）；依最高 t 值排序
# F＝回測飆股：10/20日飆股(漲幅>30%)比例>10% 且飆股次數≥10；依最高飆股比例排序
# 已在 S／#／M 清單裡的組合不重複列入
BT_WIN_COMBOS = [
    ["布林通道低檔(≤20%)", "相對強弱為負(弱於大盤)", "大盤跌破60日均線"],
    ["相對強弱為負(弱於大盤)", "大盤跌破20日均線", "大盤跌破60日均線"],
    ["相對強弱為負(弱於大盤)", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "大盤跌破20日均線", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "相對強弱為負(弱於大盤)", "大盤跌破20日均線"],
    ["布林通道低檔(≤20%)", "大盤跌破20日均線"],
    ["KDJ近3日內黃金交叉", "相對強弱為負(弱於大盤)", "大盤跌破20日均線"],
    ["KDJ近3日內黃金交叉", "相對強弱為負(弱於大盤)", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "KDJ近3日內黃金交叉", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "量能區間高檔(≥90百分位)", "大盤跌破60日均線"],
    ["相對強弱為負(弱於大盤)", "量能區間高檔(≥90百分位)", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "KDJ近3日內黃金交叉", "大盤跌破20日均線"],
    ["布林通道低檔(≤20%)", "爆量(≥1.5倍均量)", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "量能區間高檔(≥90百分位)", "大盤跌破20日均線"],
    ["布林通道低檔(≤20%)", "KDJ近3日內死亡交叉", "大盤跌破60日均線"],
    ["相對強弱為負(弱於大盤)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線"],
    ["大盤站上20日均線", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["相對強弱為負(弱於大盤)", "爆量(≥1.5倍均量)", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "大盤跌破20日均線", "大盤站上60日均線"],
    ["布林通道低檔(≤20%)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線"],
    ["大盤跌破60日均線", "近3日向上跳空缺口"],
    ["相對強弱為負(弱於大盤)", "大盤跌破20日均線", "近3日向上跳空缺口"],
    ["相對強弱為負(弱於大盤)", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["布林通道低檔(≤20%)", "爆量(≥1.5倍均量)", "大盤跌破20日均線"],
    ["相對強弱為負(弱於大盤)", "量能斜率轉弱(近5日均量<近10日均量20%以上)", "大盤跌破20日均線"],
    ["相對強弱為負(弱於大盤)", "量能斜率轉弱(近5日均量<近10日均量20%以上)", "大盤跌破60日均線"],
    ["大盤跌破60日均線", "布林通道收窄(寬度近半年最低20%)", "近3日向上跳空缺口"],
    ["相對強弱為負(弱於大盤)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破20日均線"],
    ["相對強弱為正(強於大盤)", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["相對強弱為負(弱於大盤)", "爆量(≥1.5倍均量)", "大盤跌破20日均線"],
    ["大盤跌破20日均線", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["布林通道低檔(≤20%)", "爆量(≥2倍均量)", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "大盤跌破60日均線", "布林通道收窄(寬度近半年最低20%)"],
    ["布林通道低檔(≤20%)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破20日均線"],
    ["MACD近3日內黃金交叉", "相對強弱為負(弱於大盤)", "大盤跌破20日均線"],
    ["相對強弱為負(弱於大盤)", "大盤跌破60日均線", "母子懷抱(高檔)剛形成"],
    ["布林通道低檔(≤20%)", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["KDJ近3日內黃金交叉", "量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線"],
    ["KDJ近3日內黃金交叉", "量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破20日均線"],
    ["相對強弱為正(強於大盤)", "大盤站上20日均線", "大盤跌破60日均線"],
    ["爆量(≥1.5倍均量)", "大盤跌破60日均線"],
    ["KDJ近3日內黃金交叉", "大盤跌破60日均線", "母子懷抱(高檔)剛形成"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線"],
    ["KDJ近3日內死亡交叉", "爆量(≥1.5倍均量)", "大盤跌破60日均線"],
    ["MACD近3日內黃金交叉", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["大盤跌破60日均線", "近3日向上跳空缺口", "母子懷抱(低檔)剛形成"],
    ["布林通道低檔(≤20%)", "大盤跌破20日均線", "近3日向上跳空缺口"],
    ["相對強弱為負(弱於大盤)", "爆量(≥2倍均量)", "大盤跌破60日均線"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破20日均線", "大盤跌破60日均線"],
    ["爆量(≥1.5倍均量)", "大盤跌破20日均線", "大盤跌破60日均線"],
    ["布林通道高檔(≥80%)", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["布林通道低檔(≤20%)", "量能區間低檔(≤10百分位)", "大盤跌破20日均線"],
    ["布林通道高檔(≥80%)", "大盤站上20日均線", "大盤跌破60日均線"],
    ["爆量(≥1.5倍均量)", "量能區間高檔(≥90百分位)", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "量能區間低檔(≤10百分位)", "大盤跌破60日均線"],
    ["大盤跌破60日均線", "母子懷抱(高檔)剛形成"],
    ["相對強弱為負(弱於大盤)", "大盤跌破20日均線", "母子懷抱(低檔)剛形成"],
    ["大盤跌破60日均線", "母子懷抱(低檔)剛形成"],
    ["布林通道低檔(≤20%)", "大盤跌破60日均線", "母子懷抱(低檔)剛形成"],
    ["相對強弱為負(弱於大盤)", "大盤跌破60日均線", "母子懷抱(低檔)剛形成"],
    ["大盤跌破20日均線", "大盤跌破60日均線", "母子懷抱(低檔)剛形成"],
    ["KDJ近3日內死亡交叉", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["相對強弱為負(弱於大盤)", "大盤跌破20日均線", "母子懷抱(高檔)剛形成"],
    ["量能區間低檔(≤10百分位)", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["KDJ近3日內死亡交叉", "爆量(≥2倍均量)", "大盤跌破60日均線"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破20日均線", "布林通道收窄(寬度近半年最低20%)"],
    ["爆量(≥1.5倍均量)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線"],
    ["量能斜率轉弱(近5日均量<近10日均量20%以上)", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["大盤跌破20日均線", "大盤跌破60日均線", "母子懷抱(高檔)剛形成"],
    ["布林通道低檔(≤20%)", "大盤跌破60日均線", "母子懷抱(高檔)剛形成"],
    ["相對強弱為負(弱於大盤)", "量能區間低檔(≤10百分位)", "大盤跌破20日均線"],
    ["KDJ近3日內黃金交叉", "大盤站上20日均線", "大盤跌破60日均線"],
    ["大盤跌破60日均線", "布林通道收窄(寬度近半年最低20%)", "母子懷抱(低檔)剛形成"],
    ["KDJ近3日內黃金交叉", "爆量(≥1.5倍均量)", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "量能斜率轉弱(近5日均量<近10日均量20%以上)", "大盤跌破60日均線"],
    ["爆量(≥1.5倍均量)", "大盤站上20日均線", "大盤跌破60日均線"],
    ["KDJ近3日內黃金交叉", "大盤跌破20日均線", "母子懷抱(高檔)剛形成"],
    ["布林通道低檔(≤20%)", "大盤跌破20日均線", "母子懷抱(低檔)剛形成"],
    ["量能區間高檔(≥90百分位)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["量能區間低檔(≤10百分位)", "量能斜率轉弱(近5日均量<近10日均量20%以上)", "大盤跌破20日均線"],
    ["布林通道低檔(≤20%)", "爆量(≥2倍均量)", "大盤跌破20日均線"],
    ["MACD近3日內黃金交叉", "相對強弱為負(弱於大盤)", "大盤跌破60日均線"],
    ["相對強弱為負(弱於大盤)", "量能區間低檔(≤10百分位)", "大盤跌破60日均線"],
    ["強勢突破盤", "N字底剛形成", "一字底(均線糾結)剛形成"],
    ["跌深反彈盤", "相對強弱為負(弱於大盤)", "大盤跌破60日均線"],
    ["跌深反彈盤", "KDJ近3日內死亡交叉", "大盤跌破60日均線"],
    ["量能斜率轉弱(近5日均量<近10日均量20%以上)", "大盤跌破20日均線", "近3日向上跳空缺口"],
    ["跌深反彈盤", "大盤跌破20日均線", "大盤跌破60日均線"],
    ["相對強弱為正(強於大盤)", "大盤跌破60日均線", "夜星剛形成"],
    ["大盤跌破20日均線", "三重底剛形成", "K線橫盤的突破剛形成"],
    ["布林通道低檔(≤20%)", "量能斜率轉弱(近5日均量<近10日均量20%以上)", "大盤跌破20日均線"],
    ["跌深反彈盤", "大盤跌破60日均線"],
    ["MACD近3日內黃金交叉", "KDJ近3日內死亡交叉", "大盤跌破60日均線"],
    ["布林通道低檔(≤20%)", "大盤跌破20日均線", "母子懷抱(高檔)剛形成"],
    ["大盤跌破60日均線", "布林通道收窄(寬度近半年最低20%)", "母子懷抱(高檔)剛形成"],
    ["MACD近3日內黃金交叉", "KDJ近3日內死亡交叉", "大盤跌破20日均線"],
    ["大盤站上20日均線", "大盤跌破60日均線", "母子懷抱(高檔)剛形成"],
    ["KDJ近3日內黃金交叉", "量能區間低檔(≤10百分位)", "大盤跌破60日均線"],
    ["爆量(≥2倍均量)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線"],
    ["相對強弱為負(弱於大盤)", "大盤跌破20日均線", "K線橫盤的突破剛形成"],
    ["大盤跌破20日均線", "布林通道收窄(寬度近半年最低20%)", "母子懷抱(高檔)剛形成"],
    ["均線多頭排列(5>20>60且站上月線)", "N字底剛形成", "突破飆股大量黑K最高點剛形成"],
    ["近3日向上跳空缺口", "N字底剛形成", "圓弧底剛形成"],
    ["KDJ近3日內黃金交叉", "大盤跌破60日均線"],
    ["大盤跌破20日均線", "近3日向上跳空缺口", "母子懷抱(低檔)剛形成"],
    ["布林通道低檔(≤20%)", "量能區間低檔(≤10百分位)", "量能斜率轉弱(近5日均量<近10日均量20%以上)"],
    ["KDJ近3日內黃金交叉", "大盤跌破20日均線", "大盤跌破60日均線"],
    ["KDJ近3日內黃金交叉", "大盤跌破20日均線", "K線橫盤的突破剛形成"],
    ["大盤跌破60日均線", "均線多頭排列(5>20>60且站上月線)", "夜星剛形成"],
    ["量能區間低檔(≤10百分位)", "量能斜率轉弱(近5日均量<近10日均量20%以上)", "三重底剛形成"],
    ["量能區間高檔(≥90百分位)", "大盤跌破60日均線", "夜星剛形成"],
    ["近3日向上跳空缺口", "N字底剛形成", "K線橫盤的突破剛形成"],
    ["量能區間低檔(≤10百分位)", "大盤跌破20日均線", "母子懷抱(低檔)剛形成"],
    ["均線多頭排列(5>20>60且站上月線)", "頭肩底剛形成", "圓弧底剛形成"],
    ["多方力道≥65", "創52週新高", "一字底(均線糾結)剛形成"],
    ["跌深反彈盤", "量能區間高檔(≥90百分位)", "大盤跌破60日均線"],
    ["相對強弱為負(弱於大盤)", "爆量(≥2倍均量)", "大盤跌破20日均線"],
    ["MACD近3日內黃金交叉", "大盤跌破60日均線", "母子懷抱(低檔)剛形成"],
    ["布林通道低檔(≤20%)", "地量(≤0.5倍均量)", "量能區間低檔(≤10百分位)"],
    ["爆量(≥1.5倍均量)", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["KDJ近3日內黃金交叉", "爆量(≥2倍均量)", "大盤跌破60日均線"],
    ["KDJ近3日內黃金交叉", "大盤跌破20日均線", "母子懷抱(低檔)剛形成"],
    ["跌深反彈盤", "相對強弱為負(弱於大盤)", "大盤跌破20日均線"],
    ["多方力道≥65", "布林通道低檔(≤20%)", "大盤跌破60日均線"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線", "K線橫盤的突破剛形成"],
    ["量能區間高檔(≥90百分位)", "大盤跌破60日均線", "近3日向上跳空缺口"],
    ["多方力道≥80", "大盤站上20日均線", "複式頭肩底剛形成"],
    ["強勢突破盤", "布林通道收窄(寬度近半年最低20%)", "K線橫盤的突破剛形成"],
    ["量能區間低檔(≤10百分位)", "量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線"],
    ["多方力道≥80", "KDJ近3日內黃金交叉", "複式頭肩底剛形成"],
    ["相對強弱為正(強於大盤)", "頭肩底剛形成", "圓弧底剛形成"],
    ["布林通道低檔(≤20%)", "爆量(≥1.5倍均量)", "母子懷抱(低檔)剛形成"],
    ["相對強弱為正(強於大盤)", "量能區間低檔(≤10百分位)", "母子懷抱(低檔)剛形成"],
    ["大盤跌破60日均線", "近3日向上跳空缺口", "突破ABC修正下降切線剛形成"],
    ["布林通道低檔(≤20%)", "爆量(≥1.5倍均量)", "距52週高點≤5%"],
    ["跌深反彈盤", "KDJ近3日內死亡交叉", "量能斜率轉強(近5日均量>近10日均量20%以上)"],
    ["量能區間低檔(≤10百分位)", "大盤跌破60日均線"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "頭肩底剛形成", "三重底剛形成"],
    ["跌深反彈盤", "大盤跌破20日均線"],
    ["KDJ近3日內死亡交叉", "量能斜率轉弱(近5日均量<近10日均量20%以上)", "大盤跌破20日均線"],
    ["量能區間低檔(≤10百分位)", "回後買上漲全通過", "近3日向上跳空缺口"],
    ["多方力道≥65", "布林通道低檔(≤20%)", "相對強弱為正(強於大盤)"],
    ["多方力道≥65", "布林通道低檔(≤20%)", "大盤跌破20日均線"],
    ["布林通道低檔(≤20%)", "地量(≤0.5倍均量)", "大盤跌破20日均線"],
    ["量能區間高檔(≥90百分位)", "大盤跌破20日均線", "夜星剛形成"],
    ["MACD近3日內黃金交叉", "大盤跌破20日均線", "近3日向上跳空缺口"],
    ["KDJ近3日內黃金交叉", "大盤跌破60日均線", "突破飆股大量黑K最高點剛形成"],
    ["KDJ近3日內黃金交叉", "爆量(≥2倍均量)", "大盤跌破20日均線"],
]
BT_WIN_STATS = {
    "大盤跌破20日均線 ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 13812, "win": 60.2, "ex": 0.33, "t": 6.89}, "10": {"n": 13812, "win": 61.0, "ex": 0.39, "t": 5.56}, "20": {"n": 13812, "win": 58.3, "ex": 0.42, "t": 4.22}},
    "布林通道低檔(≤20%) ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 868, "win": 60.7, "ex": 1.18, "t": 5.78}, "10": {"n": 868, "win": 63.5, "ex": 1.54, "t": 5.51}, "20": {"n": 868, "win": 60.5, "ex": 1.42, "t": 3.81}},
    "大盤跌破60日均線 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 近3日向上跳空缺口": {"5": {"n": 2308, "win": 61.1, "ex": 0.59, "t": 5.01}, "10": {"n": 2308, "win": 62.0, "ex": 1.18, "t": 6.32}, "20": {"n": 2308, "win": 59.4, "ex": 2.19, "t": 8.17}},
    "布林通道低檔(≤20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線": {"5": {"n": 14585, "win": 62.3, "ex": 0.22, "t": 4.92}, "10": {"n": 14585, "win": 67.4, "ex": 0.55, "t": 9.09}, "20": {"n": 14585, "win": 65.5, "ex": 0.34, "t": 3.94}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 1563, "win": 60.8, "ex": 0.7, "t": 4.68}, "10": {"n": 1563, "win": 59.2, "ex": 0.97, "t": 4.43}, "20": {"n": 1563, "win": 56.7, "ex": 1.08, "t": 3.27}},
    "量能區間低檔(≤10百分位) ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 771, "win": 64.2, "ex": 0.84, "t": 4.42}, "10": {"n": 771, "win": 64.3, "ex": 0.74, "t": 2.54}, "20": {"n": 771, "win": 61.1, "ex": 1.37, "t": 3.26}},
    "KDJ近3日內死亡交叉 ＋ 爆量(≥2倍均量) ＋ 大盤跌破60日均線": {"5": {"n": 3277, "win": 60.1, "ex": 0.48, "t": 4.3}, "10": {"n": 3277, "win": 60.9, "ex": 0.55, "t": 3.85}, "20": {"n": 3277, "win": 59.7, "ex": 0.2, "t": 1.01}},
    "量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 1767, "win": 65.8, "ex": 0.54, "t": 4.26}, "10": {"n": 1767, "win": 65.8, "ex": 0.41, "t": 2.12}, "20": {"n": 1767, "win": 65.7, "ex": 0.64, "t": 2.26}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 958, "win": 61.6, "ex": 0.76, "t": 3.95}, "10": {"n": 958, "win": 60.3, "ex": 1.23, "t": 4.55}, "20": {"n": 958, "win": 55.8, "ex": 1.52, "t": 3.72}},
    "大盤跌破60日均線 ＋ 近3日向上跳空缺口 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 174, "win": 68.4, "ex": 2.04, "t": 3.8}, "10": {"n": 174, "win": 69.5, "ex": 3.55, "t": 4.82}, "20": {"n": 174, "win": 69.5, "ex": 5.85, "t": 5.11}},
    "大盤跌破20日均線 ＋ 大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 1322, "win": 62.6, "ex": 0.58, "t": 3.32}, "10": {"n": 1322, "win": 62.1, "ex": 1.08, "t": 4.34}, "20": {"n": 1322, "win": 57.0, "ex": 1.64, "t": 4.49}},
    "強勢突破盤 ＋ N字底剛形成 ＋ 一字底(均線糾結)剛形成": {"5": {"n": 225, "win": 63.1, "ex": 0.76, "t": 3.31}, "10": {"n": 225, "win": 61.3, "ex": 0.86, "t": 2.56}, "20": {"n": 225, "win": 56.4, "ex": 1.0, "t": 2.11}},
    "量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 大盤跌破20日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 1633, "win": 64.8, "ex": 0.44, "t": 3.23}, "10": {"n": 1633, "win": 64.4, "ex": 0.12, "t": 0.58}, "20": {"n": 1633, "win": 62.9, "ex": 0.23, "t": 0.76}},
    "MACD近3日內黃金交叉 ＋ KDJ近3日內死亡交叉 ＋ 大盤跌破60日均線": {"5": {"n": 4815, "win": 60.5, "ex": 0.23, "t": 3.1}, "10": {"n": 4815, "win": 61.8, "ex": 0.25, "t": 2.24}, "20": {"n": 4815, "win": 61.1, "ex": 0.39, "t": 2.43}},
    "KDJ近3日內死亡交叉 ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 3229, "win": 60.6, "ex": 0.27, "t": 2.78}, "10": {"n": 3229, "win": 59.4, "ex": 0.37, "t": 2.7}, "20": {"n": 3229, "win": 59.8, "ex": 0.9, "t": 4.49}},
    "量能區間低檔(≤10百分位) ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 三重底剛形成": {"5": {"n": 259, "win": 61.8, "ex": 0.65, "t": 2.68}, "10": {"n": 259, "win": 60.6, "ex": 0.76, "t": 2.04}, "20": {"n": 259, "win": 61.0, "ex": 0.94, "t": 1.72}},
    "量能區間低檔(≤10百分位) ＋ 大盤跌破20日均線 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 126, "win": 67.5, "ex": 1.38, "t": 2.52}, "10": {"n": 126, "win": 68.3, "ex": 1.03, "t": 1.23}, "20": {"n": 126, "win": 57.1, "ex": 1.55, "t": 1.3}},
    "均線多頭排列(5>20>60且站上月線) ＋ 頭肩底剛形成 ＋ 圓弧底剛形成": {"5": {"n": 117, "win": 60.7, "ex": 0.92, "t": 2.52}, "10": {"n": 117, "win": 62.4, "ex": 0.76, "t": 1.32}, "20": {"n": 117, "win": 54.7, "ex": 0.9, "t": 1.02}},
    "跌深反彈盤 ＋ 量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線": {"5": {"n": 128, "win": 63.3, "ex": 1.37, "t": 2.46}, "10": {"n": 128, "win": 63.3, "ex": 1.61, "t": 2.01}, "20": {"n": 128, "win": 56.2, "ex": 0.09, "t": 0.08}},
    "大盤站上20日均線 ＋ 大盤跌破60日均線 ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 129, "win": 73.6, "ex": 1.33, "t": 2.24}, "10": {"n": 129, "win": 77.5, "ex": 2.58, "t": 2.96}, "20": {"n": 129, "win": 76.7, "ex": 3.36, "t": 2.78}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破20日均線 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 1055, "win": 60.7, "ex": 0.45, "t": 2.21}, "10": {"n": 1055, "win": 58.9, "ex": 0.65, "t": 2.3}, "20": {"n": 1055, "win": 53.5, "ex": 0.2, "t": 0.46}},
    "量能區間低檔(≤10百分位) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線": {"5": {"n": 182, "win": 63.7, "ex": 0.72, "t": 2.2}, "10": {"n": 182, "win": 64.8, "ex": 0.57, "t": 1.21}, "20": {"n": 182, "win": 59.3, "ex": 0.57, "t": 0.7}},
    "相對強弱為正(強於大盤) ＋ 頭肩底剛形成 ＋ 圓弧底剛形成": {"5": {"n": 218, "win": 60.1, "ex": 0.6, "t": 2.17}, "10": {"n": 218, "win": 59.6, "ex": 0.61, "t": 1.43}, "20": {"n": 218, "win": 59.2, "ex": 1.13, "t": 1.82}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 頭肩底剛形成 ＋ 三重底剛形成": {"5": {"n": 101, "win": 63.4, "ex": 0.96, "t": 2.14}, "10": {"n": 101, "win": 55.4, "ex": 0.49, "t": 0.78}, "20": {"n": 101, "win": 52.5, "ex": 0.14, "t": 0.15}},
    "大盤跌破20日均線 ＋ 近3日向上跳空缺口 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 204, "win": 63.7, "ex": 1.07, "t": 2.07}, "10": {"n": 204, "win": 61.8, "ex": 1.81, "t": 2.41}, "20": {"n": 204, "win": 61.8, "ex": 3.12, "t": 2.77}},
    "量能區間高檔(≥90百分位) ＋ 大盤跌破20日均線 ＋ 夜星剛形成": {"5": {"n": 181, "win": 60.2, "ex": 1.33, "t": 2.06}, "10": {"n": 181, "win": 51.9, "ex": -0.29, "t": -0.32}, "20": {"n": 181, "win": 56.9, "ex": 1.51, "t": 1.19}},
    "布林通道低檔(≤20%) ＋ 相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線": {"5": {"n": 60126, "win": 56.7, "ex": 0.26, "t": 11.76}, "10": {"n": 60126, "win": 61.6, "ex": 0.62, "t": 19.69}, "20": {"n": 60126, "win": 61.4, "ex": 0.58, "t": 13.2}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線 ＋ 大盤跌破60日均線": {"5": {"n": 107626, "win": 57.2, "ex": 0.21, "t": 13.01}, "10": {"n": 107626, "win": 61.2, "ex": 0.46, "t": 19.52}, "20": {"n": 107626, "win": 59.8, "ex": 0.43, "t": 12.94}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線": {"5": {"n": 125764, "win": 56.9, "ex": 0.17, "t": 10.86}, "10": {"n": 125764, "win": 61.2, "ex": 0.4, "t": 18.17}, "20": {"n": 125764, "win": 60.1, "ex": 0.3, "t": 9.89}},
    "布林通道低檔(≤20%) ＋ 大盤跌破20日均線 ＋ 大盤跌破60日均線": {"5": {"n": 69563, "win": 58.0, "ex": 0.2, "t": 9.85}, "10": {"n": 69563, "win": 62.4, "ex": 0.49, "t": 17.15}, "20": {"n": 69563, "win": 61.7, "ex": 0.48, "t": 12.02}},
    "布林通道低檔(≤20%) ＋ 大盤跌破60日均線": {"5": {"n": 72335, "win": 57.7, "ex": 0.2, "t": 10.1}, "10": {"n": 72335, "win": 62.0, "ex": 0.48, "t": 17.03}, "20": {"n": 72335, "win": 61.7, "ex": 0.47, "t": 11.75}},
    "布林通道低檔(≤20%) ＋ 大盤跌破20日均線": {"5": {"n": 99498, "win": 57.1, "ex": 0.15, "t": 8.62}, "10": {"n": 99498, "win": 60.2, "ex": 0.36, "t": 15.06}, "20": {"n": 99498, "win": 61.3, "ex": 0.52, "t": 15.09}},
    "KDJ近3日內黃金交叉 ＋ 相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線": {"5": {"n": 48286, "win": 56.0, "ex": 0.16, "t": 6.53}, "10": {"n": 48286, "win": 60.3, "ex": 0.49, "t": 14.09}, "20": {"n": 48286, "win": 59.5, "ex": 0.4, "t": 7.98}},
    "KDJ近3日內黃金交叉 ＋ 相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線": {"5": {"n": 36344, "win": 56.7, "ex": 0.16, "t": 5.79}, "10": {"n": 36344, "win": 62.5, "ex": 0.49, "t": 12.24}, "20": {"n": 36344, "win": 60.4, "ex": 0.35, "t": 6.1}},
    "布林通道低檔(≤20%) ＋ KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線": {"5": {"n": 16103, "win": 56.1, "ex": 0.22, "t": 5.2}, "10": {"n": 16103, "win": 62.2, "ex": 0.71, "t": 12.1}, "20": {"n": 16103, "win": 60.0, "ex": 0.55, "t": 6.47}},
    "布林通道低檔(≤20%) ＋ 量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線": {"5": {"n": 17807, "win": 59.6, "ex": 0.33, "t": 7.74}, "10": {"n": 17807, "win": 64.5, "ex": 0.68, "t": 12.06}, "20": {"n": 17807, "win": 62.2, "ex": 0.47, "t": 6.05}},
    "相對強弱為負(弱於大盤) ＋ 量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線": {"5": {"n": 23820, "win": 56.5, "ex": 0.29, "t": 7.73}, "10": {"n": 23820, "win": 61.9, "ex": 0.6, "t": 11.92}, "20": {"n": 23820, "win": 60.1, "ex": 0.4, "t": 5.82}},
    "布林通道低檔(≤20%) ＋ 爆量(≥1.5倍均量) ＋ 大盤跌破60日均線": {"5": {"n": 12928, "win": 59.8, "ex": 0.37, "t": 7.21}, "10": {"n": 12928, "win": 64.5, "ex": 0.73, "t": 10.76}, "20": {"n": 12928, "win": 64.5, "ex": 0.77, "t": 8.1}},
    "布林通道低檔(≤20%) ＋ 量能區間高檔(≥90百分位) ＋ 大盤跌破20日均線": {"5": {"n": 23381, "win": 58.1, "ex": 0.22, "t": 5.88}, "10": {"n": 23381, "win": 61.6, "ex": 0.5, "t": 10.0}, "20": {"n": 23381, "win": 62.0, "ex": 0.54, "t": 7.82}},
    "布林通道低檔(≤20%) ＋ KDJ近3日內死亡交叉 ＋ 大盤跌破60日均線": {"5": {"n": 24041, "win": 56.2, "ex": 0.29, "t": 8.32}, "10": {"n": 24041, "win": 60.3, "ex": 0.49, "t": 9.93}, "20": {"n": 24041, "win": 59.1, "ex": 0.37, "t": 5.33}},
    "相對強弱為負(弱於大盤) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線": {"5": {"n": 18724, "win": 58.7, "ex": 0.17, "t": 4.12}, "10": {"n": 18724, "win": 65.4, "ex": 0.54, "t": 9.75}, "20": {"n": 18724, "win": 63.3, "ex": 0.25, "t": 3.25}},
    "相對強弱為負(弱於大盤) ＋ 爆量(≥1.5倍均量) ＋ 大盤跌破60日均線": {"5": {"n": 15606, "win": 56.7, "ex": 0.23, "t": 4.78}, "10": {"n": 15606, "win": 62.5, "ex": 0.6, "t": 9.45}, "20": {"n": 15606, "win": 62.4, "ex": 0.62, "t": 7.04}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 8846, "win": 59.1, "ex": 0.42, "t": 7.42}, "10": {"n": 8846, "win": 62.1, "ex": 0.69, "t": 8.49}, "20": {"n": 8846, "win": 60.3, "ex": 0.5, "t": 4.31}},
    "大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 21249, "win": 58.3, "ex": 0.26, "t": 6.94}, "10": {"n": 21249, "win": 61.4, "ex": 0.47, "t": 8.48}, "20": {"n": 21249, "win": 60.3, "ex": 0.7, "t": 8.84}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 7363, "win": 59.9, "ex": 0.56, "t": 8.7}, "10": {"n": 7363, "win": 61.4, "ex": 0.78, "t": 8.43}, "20": {"n": 7363, "win": 59.5, "ex": 0.5, "t": 3.78}},
    "相對強弱為負(弱於大盤) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破20日均線": {"5": {"n": 25057, "win": 57.4, "ex": 0.09, "t": 2.46}, "10": {"n": 25057, "win": 61.5, "ex": 0.38, "t": 7.85}, "20": {"n": 25057, "win": 62.9, "ex": 0.36, "t": 5.2}},
    "相對強弱為負(弱於大盤) ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 大盤跌破60日均線": {"5": {"n": 16222, "win": 59.0, "ex": 0.34, "t": 8.21}, "10": {"n": 16222, "win": 63.8, "ex": 0.47, "t": 7.6}, "20": {"n": 16222, "win": 62.6, "ex": 0.61, "t": 6.91}},
    "相對強弱為負(弱於大盤) ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 大盤跌破20日均線": {"5": {"n": 21070, "win": 57.9, "ex": 0.31, "t": 8.35}, "10": {"n": 21070, "win": 63.2, "ex": 0.38, "t": 7.09}, "20": {"n": 21070, "win": 61.5, "ex": 0.56, "t": 7.19}},
    "布林通道低檔(≤20%) ＋ 爆量(≥1.5倍均量) ＋ 大盤跌破20日均線": {"5": {"n": 18047, "win": 57.9, "ex": 0.2, "t": 4.52}, "10": {"n": 18047, "win": 60.6, "ex": 0.4, "t": 6.92}, "20": {"n": 18047, "win": 63.6, "ex": 0.68, "t": 8.44}},
    "大盤站上20日均線 ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 7437, "win": 54.6, "ex": 0.14, "t": 2.23}, "10": {"n": 7437, "win": 62.0, "ex": 0.61, "t": 6.92}, "20": {"n": 7437, "win": 64.1, "ex": 1.21, "t": 9.48}},
    "布林通道低檔(≤20%) ＋ 爆量(≥2倍均量) ＋ 大盤跌破60日均線": {"5": {"n": 4584, "win": 59.3, "ex": 0.4, "t": 4.38}, "10": {"n": 4584, "win": 64.1, "ex": 0.75, "t": 6.43}, "20": {"n": 4584, "win": 63.9, "ex": 0.59, "t": 3.63}},
    "布林通道低檔(≤20%) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破20日均線": {"5": {"n": 19815, "win": 59.7, "ex": 0.1, "t": 2.48}, "10": {"n": 19815, "win": 62.8, "ex": 0.33, "t": 6.21}, "20": {"n": 19815, "win": 64.8, "ex": 0.39, "t": 5.23}},
    "MACD近3日內黃金交叉 ＋ 相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線": {"5": {"n": 17152, "win": 59.0, "ex": 0.24, "t": 5.74}, "10": {"n": 17152, "win": 60.4, "ex": 0.37, "t": 6.16}, "20": {"n": 17152, "win": 57.2, "ex": 0.17, "t": 2.01}},
    "KDJ近3日內黃金交叉 ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線": {"5": {"n": 10247, "win": 55.1, "ex": 0.03, "t": 0.54}, "10": {"n": 10247, "win": 65.2, "ex": 0.43, "t": 5.76}, "20": {"n": 10247, "win": 64.4, "ex": 0.36, "t": 3.31}},
    "KDJ近3日內黃金交叉 ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破20日均線": {"5": {"n": 14512, "win": 55.4, "ex": 0.04, "t": 0.87}, "10": {"n": 14512, "win": 61.4, "ex": 0.37, "t": 5.72}, "20": {"n": 14512, "win": 63.1, "ex": 0.34, "t": 3.69}},
    "爆量(≥1.5倍均量) ＋ 大盤跌破60日均線": {"5": {"n": 27260, "win": 56.5, "ex": 0.09, "t": 2.63}, "10": {"n": 27260, "win": 60.6, "ex": 0.27, "t": 5.65}, "20": {"n": 27260, "win": 60.7, "ex": 0.34, "t": 5.0}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線": {"5": {"n": 33136, "win": 58.1, "ex": 0.13, "t": 4.18}, "10": {"n": 33136, "win": 63.0, "ex": 0.23, "t": 5.46}, "20": {"n": 33136, "win": 61.8, "ex": 0.02, "t": 0.28}},
    "KDJ近3日內死亡交叉 ＋ 爆量(≥1.5倍均量) ＋ 大盤跌破60日均線": {"5": {"n": 9009, "win": 59.6, "ex": 0.32, "t": 5.08}, "10": {"n": 9009, "win": 60.9, "ex": 0.44, "t": 5.18}, "20": {"n": 9009, "win": 60.2, "ex": 0.28, "t": 2.36}},
    "MACD近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 6737, "win": 58.1, "ex": 0.19, "t": 2.93}, "10": {"n": 6737, "win": 63.3, "ex": 0.48, "t": 5.12}, "20": {"n": 6737, "win": 60.8, "ex": 0.4, "t": 2.95}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線 ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 718, "win": 50.1, "ex": 0.48, "t": 2.0}, "10": {"n": 718, "win": 67.3, "ex": 1.72, "t": 5.03}, "20": {"n": 718, "win": 65.0, "ex": 2.9, "t": 5.92}},
    "相對強弱為負(弱於大盤) ＋ 爆量(≥2倍均量) ＋ 大盤跌破60日均線": {"5": {"n": 5520, "win": 56.2, "ex": 0.2, "t": 2.4}, "10": {"n": 5520, "win": 61.6, "ex": 0.55, "t": 4.93}, "20": {"n": 5520, "win": 61.3, "ex": 0.46, "t": 3.06}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破20日均線 ＋ 大盤跌破60日均線": {"5": {"n": 29073, "win": 58.4, "ex": 0.11, "t": 3.28}, "10": {"n": 29073, "win": 63.4, "ex": 0.22, "t": 4.92}, "20": {"n": 29073, "win": 61.6, "ex": 0.0, "t": 0.02}},
    "爆量(≥1.5倍均量) ＋ 大盤跌破20日均線 ＋ 大盤跌破60日均線": {"5": {"n": 24595, "win": 56.7, "ex": 0.09, "t": 2.4}, "10": {"n": 24595, "win": 60.1, "ex": 0.25, "t": 4.9}, "20": {"n": 24595, "win": 59.8, "ex": 0.28, "t": 3.97}},
    "爆量(≥1.5倍均量) ＋ 量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線": {"5": {"n": 22023, "win": 56.3, "ex": 0.1, "t": 2.4}, "10": {"n": 22023, "win": 60.4, "ex": 0.25, "t": 4.73}, "20": {"n": 22023, "win": 60.1, "ex": 0.17, "t": 2.29}},
    "布林通道低檔(≤20%) ＋ 大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 429, "win": 61.3, "ex": 0.51, "t": 1.8}, "10": {"n": 429, "win": 64.1, "ex": 1.79, "t": 4.58}, "20": {"n": 429, "win": 58.0, "ex": 1.83, "t": 3.2}},
    "爆量(≥1.5倍均量) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線": {"5": {"n": 12473, "win": 57.5, "ex": 0.04, "t": 0.76}, "10": {"n": 12473, "win": 63.2, "ex": 0.3, "t": 4.27}, "20": {"n": 12473, "win": 63.2, "ex": 0.2, "t": 2.0}},
    "相對強弱為正(強於大盤) ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 12403, "win": 57.6, "ex": 0.15, "t": 2.99}, "10": {"n": 12403, "win": 60.8, "ex": 0.31, "t": 4.12}, "20": {"n": 12403, "win": 60.3, "ex": 0.84, "t": 7.84}},
    "大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 1502, "win": 59.7, "ex": 0.46, "t": 2.81}, "10": {"n": 1502, "win": 60.3, "ex": 0.93, "t": 4.04}, "20": {"n": 1502, "win": 57.5, "ex": 1.57, "t": 4.62}},
    "布林通道低檔(≤20%) ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 大盤跌破60日均線": {"5": {"n": 6144, "win": 54.1, "ex": 0.16, "t": 2.38}, "10": {"n": 6144, "win": 62.4, "ex": 0.36, "t": 3.74}, "20": {"n": 6144, "win": 61.9, "ex": 0.29, "t": 2.08}},
    "布林通道低檔(≤20%) ＋ 大盤跌破60日均線 ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 416, "win": 49.0, "ex": 0.34, "t": 1.12}, "10": {"n": 416, "win": 72.1, "ex": 1.66, "t": 3.71}, "20": {"n": 416, "win": 67.8, "ex": 2.54, "t": 4.01}},
    "布林通道低檔(≤20%) ＋ 大盤跌破20日均線 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 695, "win": 59.6, "ex": 0.47, "t": 2.2}, "10": {"n": 695, "win": 60.1, "ex": 1.12, "t": 3.68}, "20": {"n": 695, "win": 59.9, "ex": 1.54, "t": 3.38}},
    "量能區間高檔(≥90百分位) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線": {"5": {"n": 15521, "win": 57.8, "ex": 0.04, "t": 0.87}, "10": {"n": 15521, "win": 63.6, "ex": 0.23, "t": 3.68}, "20": {"n": 15521, "win": 62.8, "ex": -0.07, "t": -0.76}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 4180, "win": 56.9, "ex": 0.23, "t": 2.46}, "10": {"n": 4180, "win": 61.1, "ex": 0.45, "t": 3.52}, "20": {"n": 4180, "win": 58.5, "ex": 0.1, "t": 0.54}},
    "布林通道低檔(≤20%) ＋ 量能區間低檔(≤10百分位) ＋ 大盤跌破60日均線": {"5": {"n": 2252, "win": 58.5, "ex": 0.09, "t": 0.96}, "10": {"n": 2252, "win": 65.0, "ex": 0.52, "t": 3.39}, "20": {"n": 2252, "win": 70.2, "ex": 1.04, "t": 4.7}},
    "MACD近3日內黃金交叉 ＋ 相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線": {"5": {"n": 15615, "win": 57.6, "ex": 0.08, "t": 1.96}, "10": {"n": 15615, "win": 60.8, "ex": 0.21, "t": 3.39}, "20": {"n": 15615, "win": 58.4, "ex": 0.05, "t": 0.59}},
    "跌深反彈盤 ＋ 相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線": {"5": {"n": 390, "win": 54.9, "ex": 0.87, "t": 2.94}, "10": {"n": 390, "win": 60.5, "ex": 1.42, "t": 3.28}, "20": {"n": 390, "win": 60.8, "ex": 1.35, "t": 2.22}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線 ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 820, "win": 50.9, "ex": 0.48, "t": 2.11}, "10": {"n": 820, "win": 62.4, "ex": 1.07, "t": 3.27}, "20": {"n": 820, "win": 60.5, "ex": 2.13, "t": 4.49}},
    "跌深反彈盤 ＋ 大盤跌破20日均線 ＋ 大盤跌破60日均線": {"5": {"n": 368, "win": 57.9, "ex": 0.76, "t": 2.48}, "10": {"n": 368, "win": 63.6, "ex": 1.43, "t": 3.22}, "20": {"n": 368, "win": 59.8, "ex": 1.34, "t": 2.16}},
    "大盤跌破60日均線 ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 1283, "win": 51.0, "ex": 0.31, "t": 1.67}, "10": {"n": 1283, "win": 62.3, "ex": 0.84, "t": 3.16}, "20": {"n": 1283, "win": 61.9, "ex": 1.83, "t": 4.69}},
    "大盤跌破20日均線 ＋ 三重底剛形成 ＋ K線橫盤的突破剛形成": {"5": {"n": 392, "win": 53.8, "ex": 0.22, "t": 0.8}, "10": {"n": 392, "win": 62.2, "ex": 1.22, "t": 3.16}, "20": {"n": 392, "win": 60.5, "ex": 1.17, "t": 2.08}},
    "布林通道低檔(≤20%) ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 大盤跌破20日均線": {"5": {"n": 8610, "win": 55.5, "ex": 0.15, "t": 2.62}, "10": {"n": 8610, "win": 62.0, "ex": 0.25, "t": 3.13}, "20": {"n": 8610, "win": 61.7, "ex": 0.28, "t": 2.33}},
    "跌深反彈盤 ＋ 大盤跌破60日均線": {"5": {"n": 415, "win": 54.9, "ex": 0.76, "t": 2.69}, "10": {"n": 415, "win": 60.7, "ex": 1.28, "t": 3.12}, "20": {"n": 415, "win": 60.0, "ex": 1.18, "t": 2.04}},
    "爆量(≥1.5倍均量) ＋ 大盤站上20日均線 ＋ 大盤跌破60日均線": {"5": {"n": 2665, "win": 54.7, "ex": 0.13, "t": 1.09}, "10": {"n": 2665, "win": 64.7, "ex": 0.51, "t": 3.08}, "20": {"n": 2665, "win": 69.2, "ex": 0.88, "t": 3.74}},
    "跌深反彈盤 ＋ KDJ近3日內死亡交叉 ＋ 大盤跌破60日均線": {"5": {"n": 275, "win": 54.9, "ex": 1.13, "t": 3.24}, "10": {"n": 275, "win": 62.2, "ex": 1.56, "t": 3.05}, "20": {"n": 275, "win": 58.2, "ex": 1.27, "t": 1.76}},
    "布林通道低檔(≤20%) ＋ 大盤跌破20日均線 ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 478, "win": 51.3, "ex": 0.41, "t": 1.43}, "10": {"n": 478, "win": 69.0, "ex": 1.28, "t": 3.01}, "20": {"n": 478, "win": 64.9, "ex": 1.88, "t": 3.09}},
    "爆量(≥2倍均量) ＋ 量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線": {"5": {"n": 5906, "win": 56.6, "ex": 0.04, "t": 0.45}, "10": {"n": 5906, "win": 61.3, "ex": 0.31, "t": 2.86}, "20": {"n": 5906, "win": 61.0, "ex": 0.1, "t": 0.65}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線": {"5": {"n": 68661, "win": 55.8, "ex": -0.01, "t": -0.6}, "10": {"n": 68661, "win": 60.3, "ex": 0.08, "t": 2.78}, "20": {"n": 68661, "win": 58.7, "ex": 0.08, "t": 1.95}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破20日均線 ＋ 大盤跌破60日均線": {"5": {"n": 59078, "win": 56.1, "ex": -0.04, "t": -1.67}, "10": {"n": 59078, "win": 60.7, "ex": 0.08, "t": 2.72}, "20": {"n": 59078, "win": 58.0, "ex": 0.02, "t": 0.47}},
    "相對強弱為負(弱於大盤) ＋ 量能區間低檔(≤10百分位) ＋ 大盤跌破60日均線": {"5": {"n": 5247, "win": 57.8, "ex": 0.07, "t": 1.0}, "10": {"n": 5247, "win": 63.9, "ex": 0.26, "t": 2.48}, "20": {"n": 5247, "win": 64.3, "ex": 0.52, "t": 3.39}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 580, "win": 47.9, "ex": -0.04, "t": -0.17}, "10": {"n": 580, "win": 68.8, "ex": 0.92, "t": 2.47}, "20": {"n": 580, "win": 69.5, "ex": 3.2, "t": 5.61}},
    "MACD近3日內黃金交叉 ＋ KDJ近3日內死亡交叉 ＋ 大盤跌破20日均線": {"5": {"n": 6563, "win": 59.9, "ex": 0.2, "t": 3.04}, "10": {"n": 6563, "win": 61.8, "ex": 0.24, "t": 2.4}, "20": {"n": 6563, "win": 59.3, "ex": 0.17, "t": 1.26}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線 ＋ K線橫盤的突破剛形成": {"5": {"n": 935, "win": 45.7, "ex": -0.21, "t": -1.1}, "10": {"n": 935, "win": 61.1, "ex": 0.64, "t": 2.38}, "20": {"n": 935, "win": 63.6, "ex": 1.03, "t": 2.86}},
    "爆量(≥1.5倍均量) ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 3287, "win": 57.8, "ex": 0.01, "t": 0.1}, "10": {"n": 3287, "win": 62.8, "ex": 0.36, "t": 2.37}, "20": {"n": 3287, "win": 61.0, "ex": 0.41, "t": 1.93}},
    "KDJ近3日內黃金交叉 ＋ 爆量(≥2倍均量) ＋ 大盤跌破60日均線": {"5": {"n": 3097, "win": 52.9, "ex": -0.16, "t": -1.36}, "10": {"n": 3097, "win": 60.3, "ex": 0.35, "t": 2.32}, "20": {"n": 3097, "win": 62.4, "ex": 0.47, "t": 2.2}},
    "大盤跌破20日均線 ＋ 大盤跌破60日均線 ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 1154, "win": 48.4, "ex": 0.2, "t": 1.0}, "10": {"n": 1154, "win": 60.6, "ex": 0.65, "t": 2.32}, "20": {"n": 1154, "win": 60.2, "ex": 1.66, "t": 4.03}},
    "跌深反彈盤 ＋ 相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線": {"5": {"n": 472, "win": 56.1, "ex": 0.47, "t": 1.77}, "10": {"n": 472, "win": 62.1, "ex": 0.91, "t": 2.29}, "20": {"n": 472, "win": 57.4, "ex": 1.13, "t": 2.04}},
    "量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 4690, "win": 58.3, "ex": 0.16, "t": 1.71}, "10": {"n": 4690, "win": 61.6, "ex": 0.28, "t": 2.23}, "20": {"n": 4690, "win": 59.4, "ex": 0.01, "t": 0.06}},
    "多方力道≥80 ＋ 大盤站上20日均線 ＋ 複式頭肩底剛形成": {"5": {"n": 183, "win": 59.0, "ex": 0.55, "t": 1.95}, "10": {"n": 183, "win": 60.1, "ex": 0.93, "t": 2.22}, "20": {"n": 183, "win": 55.2, "ex": 0.41, "t": 0.69}},
    "多方力道≥80 ＋ KDJ近3日內黃金交叉 ＋ 複式頭肩底剛形成": {"5": {"n": 115, "win": 60.0, "ex": 0.82, "t": 2.07}, "10": {"n": 115, "win": 60.9, "ex": 1.26, "t": 2.2}, "20": {"n": 115, "win": 55.7, "ex": 1.49, "t": 2.05}},
    "布林通道低檔(≤20%) ＋ 爆量(≥1.5倍均量) ＋ 距52週高點≤5%": {"5": {"n": 270, "win": 56.3, "ex": 0.38, "t": 1.87}, "10": {"n": 270, "win": 65.6, "ex": 0.58, "t": 2.15}, "20": {"n": 270, "win": 60.7, "ex": 0.25, "t": 0.59}},
    "布林通道低檔(≤20%) ＋ 量能區間低檔(≤10百分位) ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上)": {"5": {"n": 1891, "win": 56.4, "ex": 0.08, "t": 0.76}, "10": {"n": 1891, "win": 62.3, "ex": 0.35, "t": 2.15}, "20": {"n": 1891, "win": 60.9, "ex": 0.64, "t": 2.77}},
    "跌深反彈盤 ＋ 大盤跌破20日均線": {"5": {"n": 499, "win": 56.3, "ex": 0.4, "t": 1.57}, "10": {"n": 499, "win": 62.3, "ex": 0.8, "t": 2.13}, "20": {"n": 499, "win": 57.1, "ex": 0.99, "t": 1.87}},
    "跌深反彈盤 ＋ KDJ近3日內死亡交叉 ＋ 量能斜率轉強(近5日均量>近10日均量20%以上)": {"5": {"n": 136, "win": 59.6, "ex": 1.13, "t": 2.15}, "10": {"n": 136, "win": 62.5, "ex": 1.52, "t": 2.11}, "20": {"n": 136, "win": 61.0, "ex": 2.0, "t": 1.84}},
    "MACD近3日內黃金交叉 ＋ 大盤跌破20日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 6503, "win": 59.1, "ex": 0.02, "t": 0.36}, "10": {"n": 6503, "win": 60.5, "ex": 0.21, "t": 2.05}, "20": {"n": 6503, "win": 58.6, "ex": -0.25, "t": -1.79}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 239, "win": 61.5, "ex": 0.79, "t": 1.93}, "10": {"n": 239, "win": 65.7, "ex": 1.17, "t": 2.05}, "20": {"n": 239, "win": 60.3, "ex": 0.91, "t": 1.23}},
    "布林通道低檔(≤20%) ＋ 相對強弱為負(弱於大盤) ＋ 大盤跌破20日均線": {"5": {"n": 85751, "win": 56.4, "ex": 0.18, "t": 9.67}, "10": {"n": 85751, "win": 59.6, "ex": 0.45, "t": 16.94}, "20": {"n": 85751, "win": 60.9, "ex": 0.59, "t": 15.79}},
    "布林通道低檔(≤20%) ＋ 大盤跌破20日均線 ＋ 大盤站上60日均線": {"5": {"n": 29935, "win": 55.0, "ex": 0.03, "t": 0.87}, "10": {"n": 29935, "win": 54.9, "ex": 0.08, "t": 1.66}, "20": {"n": 29935, "win": 60.1, "ex": 0.6, "t": 9.14}},
    "布林通道低檔(≤20%) ＋ KDJ近3日內黃金交叉 ＋ 大盤跌破20日均線": {"5": {"n": 21897, "win": 55.1, "ex": 0.16, "t": 4.49}, "10": {"n": 21897, "win": 59.9, "ex": 0.57, "t": 11.38}, "20": {"n": 21897, "win": 60.3, "ex": 0.59, "t": 8.06}},
    "相對強弱為負(弱於大盤) ＋ 爆量(≥1.5倍均量) ＋ 大盤跌破20日均線": {"5": {"n": 22222, "win": 56.4, "ex": 0.11, "t": 2.87}, "10": {"n": 22222, "win": 59.4, "ex": 0.34, "t": 6.24}, "20": {"n": 22222, "win": 61.5, "ex": 0.51, "t": 6.92}},
    "布林通道低檔(≤20%) ＋ 大盤跌破60日均線 ＋ 布林通道收窄(寬度近半年最低20%)": {"5": {"n": 7048, "win": 52.8, "ex": 0.18, "t": 2.7}, "10": {"n": 7048, "win": 56.2, "ex": 0.21, "t": 2.17}, "20": {"n": 7048, "win": 60.2, "ex": 0.88, "t": 6.26}},
    "相對強弱為正(強於大盤) ＋ 大盤站上20日均線 ＋ 大盤跌破60日均線": {"5": {"n": 16053, "win": 55.0, "ex": 0.14, "t": 3.05}, "10": {"n": 16053, "win": 56.4, "ex": -0.02, "t": -0.31}, "20": {"n": 16053, "win": 62.2, "ex": 0.52, "t": 5.72}},
    "布林通道高檔(≥80%) ＋ 大盤跌破60日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 8246, "win": 57.3, "ex": 0.02, "t": 0.26}, "10": {"n": 8246, "win": 61.4, "ex": 0.1, "t": 1.11}, "20": {"n": 8246, "win": 60.9, "ex": 0.65, "t": 4.89}},
    "布林通道低檔(≤20%) ＋ 量能區間低檔(≤10百分位) ＋ 大盤跌破20日均線": {"5": {"n": 3588, "win": 56.8, "ex": 0.12, "t": 1.54}, "10": {"n": 3588, "win": 59.7, "ex": 0.34, "t": 2.93}, "20": {"n": 3588, "win": 66.2, "ex": 0.84, "t": 4.88}},
    "布林通道高檔(≥80%) ＋ 大盤站上20日均線 ＋ 大盤跌破60日均線": {"5": {"n": 10301, "win": 59.0, "ex": 0.05, "t": 0.93}, "10": {"n": 10301, "win": 64.2, "ex": 0.15, "t": 1.85}, "20": {"n": 10301, "win": 67.0, "ex": 0.55, "t": 4.85}},
    "布林通道低檔(≤20%) ＋ 大盤跌破20日均線 ＋ 近3日向上跳空缺口": {"5": {"n": 1097, "win": 56.7, "ex": 0.79, "t": 4.56}, "10": {"n": 1097, "win": 59.7, "ex": 1.21, "t": 4.94}, "20": {"n": 1097, "win": 60.9, "ex": 1.48, "t": 4.46}},
    "相對強弱為負(弱於大盤) ＋ 量能區間低檔(≤10百分位) ＋ 大盤跌破20日均線": {"5": {"n": 8249, "win": 54.9, "ex": 0.03, "t": 0.49}, "10": {"n": 8249, "win": 57.8, "ex": 0.1, "t": 1.21}, "20": {"n": 8249, "win": 60.4, "ex": 0.45, "t": 3.87}},
    "KDJ近3日內黃金交叉 ＋ 大盤站上20日均線 ＋ 大盤跌破60日均線": {"5": {"n": 9583, "win": 53.7, "ex": 0.14, "t": 2.34}, "10": {"n": 9583, "win": 58.0, "ex": 0.06, "t": 0.73}, "20": {"n": 9583, "win": 62.5, "ex": 0.46, "t": 3.86}},
    "大盤跌破60日均線 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 160, "win": 51.2, "ex": 0.22, "t": 0.39}, "10": {"n": 160, "win": 55.0, "ex": 0.91, "t": 1.01}, "20": {"n": 160, "win": 63.7, "ex": 4.9, "t": 3.82}},
    "KDJ近3日內黃金交叉 ＋ 爆量(≥1.5倍均量) ＋ 大盤跌破60日均線": {"5": {"n": 8495, "win": 52.8, "ex": -0.13, "t": -1.94}, "10": {"n": 8495, "win": 59.7, "ex": 0.23, "t": 2.61}, "20": {"n": 8495, "win": 61.4, "ex": 0.46, "t": 3.75}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破20日均線 ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 696, "win": 47.1, "ex": -0.27, "t": -1.13}, "10": {"n": 696, "win": 63.2, "ex": 0.21, "t": 0.62}, "20": {"n": 696, "win": 64.4, "ex": 1.95, "t": 3.71}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破20日均線 ＋ 布林通道收窄(寬度近半年最低20%)": {"5": {"n": 5376, "win": 58.0, "ex": 0.28, "t": 3.39}, "10": {"n": 5376, "win": 58.9, "ex": 0.48, "t": 4.3}, "20": {"n": 5376, "win": 60.2, "ex": 0.56, "t": 3.63}},
    "量能區間低檔(≤10百分位) ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 大盤跌破20日均線": {"5": {"n": 4033, "win": 60.0, "ex": 0.09, "t": 1.16}, "10": {"n": 4033, "win": 64.2, "ex": -0.03, "t": -0.23}, "20": {"n": 4033, "win": 64.0, "ex": 0.55, "t": 3.5}},
    "布林通道低檔(≤20%) ＋ 爆量(≥2倍均量) ＋ 大盤跌破20日均線": {"5": {"n": 7037, "win": 55.6, "ex": 0.13, "t": 1.88}, "10": {"n": 7037, "win": 58.2, "ex": 0.26, "t": 2.73}, "20": {"n": 7037, "win": 63.2, "ex": 0.45, "t": 3.47}},
    "相對強弱為正(強於大盤) ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"5": {"n": 278, "win": 57.9, "ex": 0.27, "t": 0.56}, "10": {"n": 278, "win": 55.4, "ex": 1.0, "t": 1.44}, "20": {"n": 278, "win": 62.2, "ex": 3.22, "t": 3.18}},
    "大盤跌破60日均線 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 131, "win": 55.0, "ex": 0.8, "t": 1.47}, "10": {"n": 131, "win": 58.0, "ex": 1.86, "t": 2.23}, "20": {"n": 131, "win": 63.4, "ex": 4.0, "t": 3.08}},
    "KDJ近3日內黃金交叉 ＋ 量能區間低檔(≤10百分位) ＋ 大盤跌破60日均線": {"5": {"n": 2870, "win": 62.2, "ex": 0.08, "t": 0.86}, "10": {"n": 2870, "win": 66.0, "ex": 0.26, "t": 1.77}, "20": {"n": 2870, "win": 63.4, "ex": 0.62, "t": 2.92}},
    "大盤跌破20日均線 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 母子懷抱(高檔)剛形成": {"5": {"n": 164, "win": 56.7, "ex": 1.15, "t": 2.45}, "10": {"n": 164, "win": 56.7, "ex": 1.64, "t": 2.18}, "20": {"n": 164, "win": 61.6, "ex": 3.3, "t": 2.85}},
    "近3日向上跳空缺口 ＋ N字底剛形成 ＋ 圓弧底剛形成": {"5": {"n": 268, "win": 51.1, "ex": 0.05, "t": 0.17}, "10": {"n": 268, "win": 54.9, "ex": 0.55, "t": 1.47}, "20": {"n": 268, "win": 62.7, "ex": 1.55, "t": 2.79}},
    "大盤跌破60日均線 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 夜星剛形成": {"5": {"n": 124, "win": 55.6, "ex": -0.24, "t": -0.35}, "10": {"n": 124, "win": 56.5, "ex": 1.44, "t": 1.25}, "20": {"n": 124, "win": 65.3, "ex": 4.41, "t": 2.69}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破20日均線 ＋ K線橫盤的突破剛形成": {"5": {"n": 1972, "win": 49.1, "ex": -0.04, "t": -0.32}, "10": {"n": 1972, "win": 58.7, "ex": 0.5, "t": 2.7}, "20": {"n": 1972, "win": 61.0, "ex": 0.69, "t": 2.67}},
    "量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"5": {"n": 107, "win": 60.7, "ex": 1.05, "t": 1.42}, "10": {"n": 107, "win": 53.3, "ex": 0.65, "t": 0.56}, "20": {"n": 107, "win": 67.3, "ex": 4.25, "t": 2.67}},
    "近3日向上跳空缺口 ＋ N字底剛形成 ＋ K線橫盤的突破剛形成": {"5": {"n": 220, "win": 51.8, "ex": 0.2, "t": 0.47}, "10": {"n": 220, "win": 58.2, "ex": 1.11, "t": 1.9}, "20": {"n": 220, "win": 61.4, "ex": 2.3, "t": 2.56}},
    "多方力道≥65 ＋ 創52週新高 ＋ 一字底(均線糾結)剛形成": {"5": {"n": 477, "win": 51.8, "ex": 0.32, "t": 2.0}, "10": {"n": 477, "win": 55.8, "ex": 0.39, "t": 1.79}, "20": {"n": 477, "win": 60.2, "ex": 0.71, "t": 2.47}},
    "相對強弱為負(弱於大盤) ＋ 爆量(≥2倍均量) ＋ 大盤跌破20日均線": {"5": {"n": 8613, "win": 54.7, "ex": 0.04, "t": 0.68}, "10": {"n": 8613, "win": 57.0, "ex": 0.14, "t": 1.65}, "20": {"n": 8613, "win": 61.2, "ex": 0.29, "t": 2.46}},
    "MACD近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 102, "win": 58.8, "ex": -0.09, "t": -0.14}, "10": {"n": 102, "win": 63.7, "ex": 0.89, "t": 1.06}, "20": {"n": 102, "win": 64.7, "ex": 2.93, "t": 2.42}},
    "布林通道低檔(≤20%) ＋ 地量(≤0.5倍均量) ＋ 量能區間低檔(≤10百分位)": {"5": {"n": 2146, "win": 44.6, "ex": -0.21, "t": -2.37}, "10": {"n": 2146, "win": 50.5, "ex": -0.01, "t": -0.08}, "20": {"n": 2146, "win": 62.1, "ex": 0.5, "t": 2.4}},
    "多方力道≥65 ＋ 布林通道低檔(≤20%) ＋ 大盤跌破60日均線": {"5": {"n": 911, "win": 58.6, "ex": -0.25, "t": -1.51}, "10": {"n": 911, "win": 67.3, "ex": -0.02, "t": -0.11}, "20": {"n": 911, "win": 68.8, "ex": 0.79, "t": 2.26}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線 ＋ K線橫盤的突破剛形成": {"5": {"n": 796, "win": 41.0, "ex": -0.46, "t": -2.2}, "10": {"n": 796, "win": 62.4, "ex": 0.34, "t": 1.26}, "20": {"n": 796, "win": 69.0, "ex": 0.89, "t": 2.26}},
    "強勢突破盤 ＋ 布林通道收窄(寬度近半年最低20%) ＋ K線橫盤的突破剛形成": {"5": {"n": 283, "win": 56.5, "ex": 0.66, "t": 1.77}, "10": {"n": 283, "win": 57.6, "ex": 0.73, "t": 1.36}, "20": {"n": 283, "win": 60.4, "ex": 1.67, "t": 2.22}},
    "布林通道低檔(≤20%) ＋ 爆量(≥1.5倍均量) ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 296, "win": 49.3, "ex": 0.28, "t": 0.84}, "10": {"n": 296, "win": 48.3, "ex": 0.07, "t": 0.16}, "20": {"n": 296, "win": 61.5, "ex": 1.4, "t": 2.17}},
    "相對強弱為正(強於大盤) ＋ 量能區間低檔(≤10百分位) ＋ 母子懷抱(低檔)剛形成": {"5": {"n": 159, "win": 56.0, "ex": 0.25, "t": 0.58}, "10": {"n": 159, "win": 60.4, "ex": 0.44, "t": 0.63}, "20": {"n": 159, "win": 62.3, "ex": 2.33, "t": 2.16}},
    "大盤跌破60日均線 ＋ 近3日向上跳空缺口 ＋ 突破ABC修正下降切線剛形成": {"5": {"n": 426, "win": 55.6, "ex": -0.32, "t": -1.08}, "10": {"n": 426, "win": 61.0, "ex": 0.18, "t": 0.45}, "20": {"n": 426, "win": 61.3, "ex": 1.35, "t": 2.16}},
    "量能區間低檔(≤10百分位) ＋ 大盤跌破60日均線": {"5": {"n": 10125, "win": 58.8, "ex": -0.07, "t": -1.4}, "10": {"n": 10125, "win": 62.5, "ex": -0.15, "t": -2.01}, "20": {"n": 10125, "win": 62.8, "ex": 0.23, "t": 2.15}},
    "KDJ近3日內死亡交叉 ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 大盤跌破20日均線": {"5": {"n": 13833, "win": 56.5, "ex": 0.06, "t": 1.21}, "10": {"n": 13833, "win": 61.4, "ex": 0.03, "t": 0.41}, "20": {"n": 13833, "win": 61.0, "ex": 0.21, "t": 2.13}},
    "量能區間低檔(≤10百分位) ＋ 回後買上漲全通過 ＋ 近3日向上跳空缺口": {"5": {"n": 101, "win": 61.4, "ex": 0.5, "t": 1.0}, "10": {"n": 101, "win": 62.4, "ex": 0.99, "t": 1.29}, "20": {"n": 101, "win": 61.4, "ex": 2.56, "t": 2.12}},
    "多方力道≥65 ＋ 布林通道低檔(≤20%) ＋ 相對強弱為正(強於大盤)": {"5": {"n": 836, "win": 58.4, "ex": 0.01, "t": 0.05}, "10": {"n": 836, "win": 62.9, "ex": -0.07, "t": -0.27}, "20": {"n": 836, "win": 63.8, "ex": 0.8, "t": 2.11}},
    "多方力道≥65 ＋ 布林通道低檔(≤20%) ＋ 大盤跌破20日均線": {"5": {"n": 1264, "win": 59.1, "ex": -0.03, "t": -0.19}, "10": {"n": 1264, "win": 64.6, "ex": -0.04, "t": -0.2}, "20": {"n": 1264, "win": 67.0, "ex": 0.63, "t": 2.08}},
    "布林通道低檔(≤20%) ＋ 地量(≤0.5倍均量) ＋ 大盤跌破20日均線": {"5": {"n": 1899, "win": 55.5, "ex": 0.08, "t": 0.68}, "10": {"n": 1899, "win": 60.2, "ex": 0.22, "t": 1.21}, "20": {"n": 1899, "win": 61.3, "ex": 0.58, "t": 2.08}},
    "均線多頭排列(5>20>60且站上月線) ＋ N字底剛形成 ＋ 突破飆股大量黑K最高點剛形成": {"5": {"n": 128, "win": 58.6, "ex": 1.21, "t": 2.24}, "10": {"n": 128, "win": 59.4, "ex": 2.28, "t": 2.81}, "20": {"n": 128, "win": 63.3, "ex": 2.44, "t": 2.08}},
    "KDJ近3日內黃金交叉 ＋ 爆量(≥2倍均量) ＋ 大盤跌破20日均線": {"5": {"n": 5224, "win": 52.2, "ex": -0.17, "t": -1.91}, "10": {"n": 5224, "win": 56.1, "ex": 0.21, "t": 1.77}, "20": {"n": 5224, "win": 60.4, "ex": 0.32, "t": 2.01}},
}
BT_HOT_COMBOS = [
    ["KDJ近3日內死亡交叉", "K線橫盤的突破剛形成", "晨星剛形成"],
    ["MACD近3日內黃金交叉", "大盤跌破60日均線", "夜星剛形成"],
    ["大盤跌破60日均線", "均線多頭排列(5>20>60且站上月線)", "夜星剛形成"],
    ["回後買上漲全通過", "K線橫盤的突破剛形成", "晨星剛形成"],
    ["大盤跌破60日均線", "近3日向上跳空缺口", "夜星剛形成"],
    ["大盤跌破60日均線", "布林通道收窄(寬度近半年最低20%)", "晨星剛形成"],
    ["爆量(≥1.5倍均量)", "大盤跌破60日均線", "夜星剛形成"],
    ["地量(≤0.5倍均量)", "量能斜率轉弱(近5日均量<近10日均量20%以上)", "晨星剛形成"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "創52週新高", "晨星剛形成"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破60日均線", "夜星剛形成"],
    ["多方力道≥80", "KDJ近3日內死亡交叉", "晨星剛形成"],
    ["量能區間高檔(≥90百分位)", "大盤跌破60日均線", "夜星剛形成"],
    ["MACD近3日內黃金交叉", "大盤跌破20日均線", "夜星剛形成"],
    ["爆量(≥1.5倍均量)", "距52週高點≤5%", "晨星剛形成"],
    ["相對強弱為正(強於大盤)", "大盤跌破60日均線", "夜星剛形成"],
    ["爆量(≥1.5倍均量)", "創52週新高", "晨星剛形成"],
    ["地量(≤0.5倍均量)", "大盤站上20日均線", "晨星剛形成"],
    ["KDJ近3日內黃金交叉", "K線橫盤的突破剛形成", "晨星剛形成"],
    ["多方力道≥80", "爆量(≥1.5倍均量)", "晨星剛形成"],
    ["MACD近3日內黃金交叉", "相對強弱為正(強於大盤)", "夜星剛形成"],
    ["相對強弱為負(弱於大盤)", "大盤跌破60日均線", "夜星剛形成"],
    ["爆量(≥2倍均量)", "創52週新高", "晨星剛形成"],
    ["爆量(≥2倍均量)", "距52週高點≤5%", "晨星剛形成"],
    ["大盤跌破60日均線", "布林通道收窄(寬度近半年最低20%)", "母子懷抱(低檔)剛形成"],
    ["相對強弱為正(強於大盤)", "創52週新高", "母子懷抱(高檔)剛形成"],
    ["大盤跌破20日均線", "均線多頭排列(5>20>60且站上月線)", "夜星剛形成"],
    ["地量(≤0.5倍均量)", "大盤站上60日均線", "晨星剛形成"],
    ["量能區間高檔(≥90百分位)", "創52週新高", "晨星剛形成"],
    ["大盤跌破20日均線", "近3日向上跳空缺口", "夜星剛形成"],
    ["創52週新高", "母子懷抱(高檔)剛形成"],
    ["回後買上漲全通過", "創52週新高", "晨星剛形成"],
    ["MACD近3日內黃金交叉", "均線多頭排列(5>20>60且站上月線)", "夜星剛形成"],
    ["大盤跌破60日均線", "近3日向上跳空缺口", "母子懷抱(低檔)剛形成"],
    ["回後買上漲全通過", "距52週高點≤5%", "晨星剛形成"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "距52週高點≤5%", "晨星剛形成"],
    ["地量(≤0.5倍均量)", "晨星剛形成"],
    ["地量(≤0.5倍均量)", "量能區間高檔(≥90百分位)", "均線多頭排列(5>20>60且站上月線)"],
    ["爆量(≥1.5倍均量)", "回後買上漲全通過", "晨星剛形成"],
    ["多方力道≥80", "創52週新高", "晨星剛形成"],
    ["KDJ近3日內死亡交叉", "創52週新高", "晨星剛形成"],
    ["量能區間高檔(≥90百分位)", "距52週高點≤5%", "晨星剛形成"],
    ["量能斜率轉強(近5日均量>近10日均量20%以上)", "大盤跌破20日均線", "夜星剛形成"],
    ["大盤跌破60日均線", "母子懷抱(高檔)剛形成", "夜星剛形成"],
    ["爆量(≥1.5倍均量)", "K線橫盤的突破剛形成", "晨星剛形成"],
    ["大盤跌破20日均線", "母子懷抱(高檔)剛形成", "夜星剛形成"],
    ["布林通道高檔(≥80%)", "K線橫盤的突破剛形成", "晨星剛形成"],
    ["相對強弱為正(強於大盤)", "大盤跌破20日均線", "夜星剛形成"],
    ["多方力道≥80", "距52週高點≤5%", "晨星剛形成"],
    ["創52週新高", "距52週高點≤5%", "晨星剛形成"],
    ["MACD近3日內黃金交叉", "KDJ近3日內死亡交叉", "夜星剛形成"],
    ["KDJ近3日內黃金交叉", "大盤跌破60日均線", "夜星剛形成"],
    ["相對強弱為負(弱於大盤)", "回後買上漲全通過", "晨星剛形成"],
    ["距52週高點≤5%", "近3日向上跳空缺口", "晨星剛形成"],
    ["多方力道≥80", "量能區間高檔(≥90百分位)", "母子懷抱(高檔)剛形成"],
    ["多方力道≥80", "母子懷抱(低檔)剛形成", "晨星剛形成"],
    ["KDJ近3日內黃金交叉", "大盤跌破60日均線", "母子懷抱(高檔)剛形成"],
    ["多方力道≥80", "量能區間高檔(≥90百分位)", "晨星剛形成"],
    ["KDJ近3日內死亡交叉", "回後買上漲全通過", "晨星剛形成"],
    ["多方力道≥80", "母子懷抱(高檔)剛形成", "夜星剛形成"],
    ["布林通道高檔(≥80%)", "創52週新高", "晨星剛形成"],
    ["多方力道≥80", "量能區間高檔(≥90百分位)", "母子懷抱(低檔)剛形成"],
    ["強勢突破盤", "創52週新高", "晨星剛形成"],
    ["相對強弱為正(強於大盤)", "創52週新高", "晨星剛形成"],
    ["創52週新高", "晨星剛形成"],
    ["多方力道≥65", "地量(≤0.5倍均量)", "量能區間高檔(≥90百分位)"],
    ["布林通道高檔(≥80%)", "地量(≤0.5倍均量)", "量能區間高檔(≥90百分位)"],
    ["爆量(≥2倍均量)", "回後買上漲全通過", "晨星剛形成"],
    ["大盤站上20日均線", "距52週高點≤5%", "晨星剛形成"],
    ["回後買上漲全通過", "母子懷抱(低檔)剛形成", "晨星剛形成"],
    ["創52週新高", "均線多頭排列(5>20>60且站上月線)", "晨星剛形成"],
    ["多方力道≥65", "爆量(≥1.5倍均量)", "晨星剛形成"],
    ["KDJ近3日內黃金交叉", "均線多頭排列(5>20>60且站上月線)", "夜星剛形成"],
    ["相對強弱為正(強於大盤)", "母子懷抱(高檔)剛形成", "夜星剛形成"],
    ["大盤站上20日均線", "創52週新高", "晨星剛形成"],
    ["布林通道高檔(≥80%)", "爆量(≥2倍均量)", "母子懷抱(低檔)剛形成"],
    ["MACD近3日內黃金交叉", "距52週高點≤5%", "晨星剛形成"],
    ["KDJ近3日內死亡交叉", "大盤跌破20日均線", "夜星剛形成"],
    ["多方力道≥80", "相對強弱為正(強於大盤)", "晨星剛形成"],
    ["多方力道≥80", "均線多頭排列(5>20>60且站上月線)", "晨星剛形成"],
    ["MACD近3日內黃金交叉", "爆量(≥2倍均量)", "晨星剛形成"],
    ["KDJ近3日內黃金交叉", "母子懷抱(高檔)剛形成", "夜星剛形成"],
    ["量能區間高檔(≥90百分位)", "大盤跌破20日均線", "夜星剛形成"],
    ["量能區間高檔(≥90百分位)", "K線橫盤的突破剛形成", "晨星剛形成"],
    ["多方力道≥80", "晨星剛形成"],
    ["多方力道≥80", "大盤站上60日均線", "晨星剛形成"],
    ["KDJ近3日內死亡交叉", "回後買上漲全通過", "母子懷抱(低檔)剛形成"],
    ["相對強弱為正(強於大盤)", "K線橫盤的突破剛形成", "晨星剛形成"],
    ["K線橫盤的突破剛形成", "晨星剛形成"],
    ["多方力道≥65", "KDJ近3日內黃金交叉", "夜星剛形成"],
    ["多方力道≥80", "大盤站上20日均線", "晨星剛形成"],
    ["相對強弱為正(強於大盤)", "地量(≤0.5倍均量)", "量能區間高檔(≥90百分位)"],
    ["大盤跌破20日均線", "近3日向上跳空缺口", "母子懷抱(低檔)剛形成"],
    ["相對強弱為正(強於大盤)", "布林通道收窄(寬度近半年最低20%)", "夜星剛形成"],
    ["爆量(≥1.5倍均量)", "母子懷抱(低檔)剛形成", "晨星剛形成"],
    ["大盤站上60日均線", "創52週新高", "晨星剛形成"],
    ["布林通道高檔(≥80%)", "爆量(≥1.5倍均量)", "母子懷抱(低檔)剛形成"],
    ["KDJ近3日內死亡交叉", "均線多頭排列(5>20>60且站上月線)", "夜星剛形成"],
    ["相對強弱為正(強於大盤)", "近3日向上跳空缺口", "夜星剛形成"],
    ["爆量(≥1.5倍均量)", "大盤跌破20日均線", "夜星剛形成"],
    ["大盤跌破20日均線", "布林通道收窄(寬度近半年最低20%)", "晨星剛形成"],
]
BT_HOT_STATS = {
    "KDJ近3日內死亡交叉 ＋ K線橫盤的突破剛形成 ＋ 晨星剛形成": {"10": {"n": 58, "moonshotN": 5, "pct": 8.6, "avg": 35.8}, "20": {"n": 58, "moonshotN": 11, "pct": 19.0, "avg": 44.0}},
    "MACD近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"10": {"n": 75, "moonshotN": 7, "pct": 9.3, "avg": 36.5}, "20": {"n": 75, "moonshotN": 14, "pct": 18.7, "avg": 43.8}},
    "大盤跌破60日均線 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 夜星剛形成": {"10": {"n": 124, "moonshotN": 9, "pct": 7.3, "avg": 44.7}, "20": {"n": 124, "moonshotN": 22, "pct": 17.7, "avg": 51.4}},
    "回後買上漲全通過 ＋ K線橫盤的突破剛形成 ＋ 晨星剛形成": {"10": {"n": 75, "moonshotN": 4, "pct": 5.3, "avg": 36.8}, "20": {"n": 75, "moonshotN": 13, "pct": 17.3, "avg": 45.0}},
    "大盤跌破60日均線 ＋ 近3日向上跳空缺口 ＋ 夜星剛形成": {"10": {"n": 59, "moonshotN": 6, "pct": 10.2, "avg": 40.9}, "20": {"n": 59, "moonshotN": 10, "pct": 16.9, "avg": 44.3}},
    "大盤跌破60日均線 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 晨星剛形成": {"10": {"n": 97, "moonshotN": 6, "pct": 6.2, "avg": 50.6}, "20": {"n": 97, "moonshotN": 16, "pct": 16.5, "avg": 61.2}},
    "爆量(≥1.5倍均量) ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"10": {"n": 67, "moonshotN": 4, "pct": 6.0, "avg": 40.3}, "20": {"n": 67, "moonshotN": 11, "pct": 16.4, "avg": 47.8}},
    "地量(≤0.5倍均量) ＋ 量能斜率轉弱(近5日均量<近10日均量20%以上) ＋ 晨星剛形成": {"10": {"n": 62, "moonshotN": 4, "pct": 6.5, "avg": 39.7}, "20": {"n": 62, "moonshotN": 10, "pct": 16.1, "avg": 50.8}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 84, "moonshotN": 3, "pct": 3.6, "avg": 32.9}, "20": {"n": 84, "moonshotN": 13, "pct": 15.5, "avg": 41.4}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"10": {"n": 99, "moonshotN": 6, "pct": 6.1, "avg": 43.4}, "20": {"n": 99, "moonshotN": 15, "pct": 15.2, "avg": 53.5}},
    "多方力道≥80 ＋ KDJ近3日內死亡交叉 ＋ 晨星剛形成": {"10": {"n": 180, "moonshotN": 18, "pct": 10.0, "avg": 59.5}, "20": {"n": 180, "moonshotN": 27, "pct": 15.0, "avg": 50.3}},
    "量能區間高檔(≥90百分位) ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"10": {"n": 107, "moonshotN": 7, "pct": 6.5, "avg": 40.9}, "20": {"n": 107, "moonshotN": 16, "pct": 15.0, "avg": 45.0}},
    "MACD近3日內黃金交叉 ＋ 大盤跌破20日均線 ＋ 夜星剛形成": {"10": {"n": 114, "moonshotN": 8, "pct": 7.0, "avg": 36.1}, "20": {"n": 114, "moonshotN": 17, "pct": 14.9, "avg": 43.6}},
    "爆量(≥1.5倍均量) ＋ 距52週高點≤5% ＋ 晨星剛形成": {"10": {"n": 141, "moonshotN": 6, "pct": 4.3, "avg": 35.0}, "20": {"n": 141, "moonshotN": 21, "pct": 14.9, "avg": 46.1}},
    "相對強弱為正(強於大盤) ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"10": {"n": 278, "moonshotN": 15, "pct": 5.4, "avg": 42.1}, "20": {"n": 278, "moonshotN": 41, "pct": 14.7, "avg": 48.1}},
    "爆量(≥1.5倍均量) ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 109, "moonshotN": 4, "pct": 3.7, "avg": 36.2}, "20": {"n": 109, "moonshotN": 16, "pct": 14.7, "avg": 47.1}},
    "地量(≤0.5倍均量) ＋ 大盤站上20日均線 ＋ 晨星剛形成": {"10": {"n": 89, "moonshotN": 5, "pct": 5.6, "avg": 79.9}, "20": {"n": 89, "moonshotN": 13, "pct": 14.6, "avg": 50.6}},
    "KDJ近3日內黃金交叉 ＋ K線橫盤的突破剛形成 ＋ 晨星剛形成": {"10": {"n": 90, "moonshotN": 6, "pct": 6.7, "avg": 35.4}, "20": {"n": 90, "moonshotN": 13, "pct": 14.4, "avg": 44.2}},
    "多方力道≥80 ＋ 爆量(≥1.5倍均量) ＋ 晨星剛形成": {"10": {"n": 91, "moonshotN": 6, "pct": 6.6, "avg": 52.5}, "20": {"n": 91, "moonshotN": 13, "pct": 14.3, "avg": 48.5}},
    "MACD近3日內黃金交叉 ＋ 相對強弱為正(強於大盤) ＋ 夜星剛形成": {"10": {"n": 180, "moonshotN": 7, "pct": 3.9, "avg": 35.5}, "20": {"n": 180, "moonshotN": 25, "pct": 13.9, "avg": 47.7}},
    "相對強弱為負(弱於大盤) ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"10": {"n": 72, "moonshotN": 4, "pct": 5.6, "avg": 44.8}, "20": {"n": 72, "moonshotN": 10, "pct": 13.9, "avg": 53.9}},
    "爆量(≥2倍均量) ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 75, "moonshotN": 3, "pct": 4.0, "avg": 37.5}, "20": {"n": 75, "moonshotN": 10, "pct": 13.3, "avg": 51.1}},
    "爆量(≥2倍均量) ＋ 距52週高點≤5% ＋ 晨星剛形成": {"10": {"n": 83, "moonshotN": 3, "pct": 3.6, "avg": 36.8}, "20": {"n": 83, "moonshotN": 11, "pct": 13.3, "avg": 51.2}},
    "大盤跌破60日均線 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 母子懷抱(低檔)剛形成": {"10": {"n": 160, "moonshotN": 10, "pct": 6.2, "avg": 47.3}, "20": {"n": 160, "moonshotN": 21, "pct": 13.1, "avg": 55.4}},
    "相對強弱為正(強於大盤) ＋ 創52週新高 ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 77, "moonshotN": 1, "pct": 1.3, "avg": 34.1}, "20": {"n": 77, "moonshotN": 10, "pct": 13.0, "avg": 52.7}},
    "大盤跌破20日均線 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 夜星剛形成": {"10": {"n": 254, "moonshotN": 14, "pct": 5.5, "avg": 43.6}, "20": {"n": 254, "moonshotN": 33, "pct": 13.0, "avg": 51.7}},
    "地量(≤0.5倍均量) ＋ 大盤站上60日均線 ＋ 晨星剛形成": {"10": {"n": 101, "moonshotN": 4, "pct": 4.0, "avg": 89.9}, "20": {"n": 101, "moonshotN": 13, "pct": 12.9, "avg": 50.7}},
    "量能區間高檔(≥90百分位) ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 155, "moonshotN": 6, "pct": 3.9, "avg": 45.4}, "20": {"n": 155, "moonshotN": 20, "pct": 12.9, "avg": 45.4}},
    "大盤跌破20日均線 ＋ 近3日向上跳空缺口 ＋ 夜星剛形成": {"10": {"n": 78, "moonshotN": 5, "pct": 6.4, "avg": 42.9}, "20": {"n": 78, "moonshotN": 10, "pct": 12.8, "avg": 46.0}},
    "創52週新高 ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 79, "moonshotN": 1, "pct": 1.3, "avg": 34.1}, "20": {"n": 79, "moonshotN": 10, "pct": 12.7, "avg": 52.7}},
    "回後買上漲全通過 ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 142, "moonshotN": 5, "pct": 3.5, "avg": 55.6}, "20": {"n": 142, "moonshotN": 18, "pct": 12.7, "avg": 46.9}},
    "MACD近3日內黃金交叉 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 夜星剛形成": {"10": {"n": 159, "moonshotN": 4, "pct": 2.5, "avg": 34.9}, "20": {"n": 159, "moonshotN": 20, "pct": 12.6, "avg": 47.4}},
    "大盤跌破60日均線 ＋ 近3日向上跳空缺口 ＋ 母子懷抱(低檔)剛形成": {"10": {"n": 174, "moonshotN": 5, "pct": 2.9, "avg": 46.0}, "20": {"n": 174, "moonshotN": 22, "pct": 12.6, "avg": 45.9}},
    "回後買上漲全通過 ＋ 距52週高點≤5% ＋ 晨星剛形成": {"10": {"n": 175, "moonshotN": 8, "pct": 4.6, "avg": 47.4}, "20": {"n": 175, "moonshotN": 22, "pct": 12.6, "avg": 51.3}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 距52週高點≤5% ＋ 晨星剛形成": {"10": {"n": 120, "moonshotN": 4, "pct": 3.3, "avg": 32.4}, "20": {"n": 120, "moonshotN": 15, "pct": 12.5, "avg": 41.4}},
    "地量(≤0.5倍均量) ＋ 晨星剛形成": {"10": {"n": 121, "moonshotN": 7, "pct": 5.8, "avg": 69.3}, "20": {"n": 121, "moonshotN": 15, "pct": 12.4, "avg": 50.9}},
    "地量(≤0.5倍均量) ＋ 量能區間高檔(≥90百分位) ＋ 均線多頭排列(5>20>60且站上月線)": {"10": {"n": 529, "moonshotN": 35, "pct": 6.6, "avg": 97.8}, "20": {"n": 529, "moonshotN": 65, "pct": 12.3, "avg": 69.6}},
    "爆量(≥1.5倍均量) ＋ 回後買上漲全通過 ＋ 晨星剛形成": {"10": {"n": 188, "moonshotN": 8, "pct": 4.3, "avg": 45.5}, "20": {"n": 188, "moonshotN": 23, "pct": 12.2, "avg": 49.5}},
    "多方力道≥80 ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 100, "moonshotN": 4, "pct": 4.0, "avg": 44.6}, "20": {"n": 100, "moonshotN": 12, "pct": 12.0, "avg": 46.6}},
    "KDJ近3日內死亡交叉 ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 117, "moonshotN": 4, "pct": 3.4, "avg": 55.1}, "20": {"n": 117, "moonshotN": 14, "pct": 12.0, "avg": 46.8}},
    "量能區間高檔(≥90百分位) ＋ 距52週高點≤5% ＋ 晨星剛形成": {"10": {"n": 208, "moonshotN": 9, "pct": 4.3, "avg": 46.6}, "20": {"n": 208, "moonshotN": 25, "pct": 12.0, "avg": 44.8}},
    "量能斜率轉強(近5日均量>近10日均量20%以上) ＋ 大盤跌破20日均線 ＋ 夜星剛形成": {"10": {"n": 150, "moonshotN": 6, "pct": 4.0, "avg": 44.8}, "20": {"n": 150, "moonshotN": 18, "pct": 12.0, "avg": 62.8}},
    "大盤跌破60日均線 ＋ 母子懷抱(高檔)剛形成 ＋ 夜星剛形成": {"10": {"n": 109, "moonshotN": 6, "pct": 5.5, "avg": 49.0}, "20": {"n": 109, "moonshotN": 13, "pct": 11.9, "avg": 53.4}},
    "爆量(≥1.5倍均量) ＋ K線橫盤的突破剛形成 ＋ 晨星剛形成": {"10": {"n": 110, "moonshotN": 4, "pct": 3.6, "avg": 33.5}, "20": {"n": 110, "moonshotN": 13, "pct": 11.8, "avg": 44.3}},
    "大盤跌破20日均線 ＋ 母子懷抱(高檔)剛形成 ＋ 夜星剛形成": {"10": {"n": 170, "moonshotN": 7, "pct": 4.1, "avg": 51.4}, "20": {"n": 170, "moonshotN": 20, "pct": 11.8, "avg": 59.0}},
    "布林通道高檔(≥80%) ＋ K線橫盤的突破剛形成 ＋ 晨星剛形成": {"10": {"n": 120, "moonshotN": 6, "pct": 5.0, "avg": 36.1}, "20": {"n": 120, "moonshotN": 14, "pct": 11.7, "avg": 42.9}},
    "相對強弱為正(強於大盤) ＋ 大盤跌破20日均線 ＋ 夜星剛形成": {"10": {"n": 460, "moonshotN": 21, "pct": 4.6, "avg": 43.0}, "20": {"n": 460, "moonshotN": 54, "pct": 11.7, "avg": 48.8}},
    "多方力道≥80 ＋ 距52週高點≤5% ＋ 晨星剛形成": {"10": {"n": 158, "moonshotN": 9, "pct": 5.7, "avg": 48.5}, "20": {"n": 158, "moonshotN": 18, "pct": 11.4, "avg": 45.8}},
    "創52週新高 ＋ 距52週高點≤5% ＋ 晨星剛形成": {"10": {"n": 211, "moonshotN": 6, "pct": 2.8, "avg": 51.5}, "20": {"n": 211, "moonshotN": 24, "pct": 11.4, "avg": 46.6}},
    "MACD近3日內黃金交叉 ＋ KDJ近3日內死亡交叉 ＋ 夜星剛形成": {"10": {"n": 159, "moonshotN": 5, "pct": 3.1, "avg": 31.8}, "20": {"n": 159, "moonshotN": 18, "pct": 11.3, "avg": 45.0}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 夜星剛形成": {"10": {"n": 106, "moonshotN": 4, "pct": 3.8, "avg": 46.5}, "20": {"n": 106, "moonshotN": 12, "pct": 11.3, "avg": 52.0}},
    "相對強弱為負(弱於大盤) ＋ 回後買上漲全通過 ＋ 晨星剛形成": {"10": {"n": 97, "moonshotN": 5, "pct": 5.2, "avg": 48.3}, "20": {"n": 97, "moonshotN": 11, "pct": 11.3, "avg": 49.7}},
    "距52週高點≤5% ＋ 近3日向上跳空缺口 ＋ 晨星剛形成": {"10": {"n": 97, "moonshotN": 5, "pct": 5.2, "avg": 41.7}, "20": {"n": 97, "moonshotN": 11, "pct": 11.3, "avg": 56.3}},
    "多方力道≥80 ＋ 量能區間高檔(≥90百分位) ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 179, "moonshotN": 10, "pct": 5.6, "avg": 53.9}, "20": {"n": 179, "moonshotN": 20, "pct": 11.2, "avg": 55.2}},
    "多方力道≥80 ＋ 母子懷抱(低檔)剛形成 ＋ 晨星剛形成": {"10": {"n": 98, "moonshotN": 8, "pct": 8.2, "avg": 70.3}, "20": {"n": 98, "moonshotN": 11, "pct": 11.2, "avg": 53.0}},
    "KDJ近3日內黃金交叉 ＋ 大盤跌破60日均線 ＋ 母子懷抱(高檔)剛形成": {"10": {"n": 580, "moonshotN": 12, "pct": 2.1, "avg": 42.9}, "20": {"n": 580, "moonshotN": 65, "pct": 11.2, "avg": 44.5}},
    "多方力道≥80 ＋ 量能區間高檔(≥90百分位) ＋ 晨星剛形成": {"10": {"n": 180, "moonshotN": 12, "pct": 6.7, "avg": 64.1}, "20": {"n": 180, "moonshotN": 20, "pct": 11.1, "avg": 48.1}},
    "KDJ近3日內死亡交叉 ＋ 回後買上漲全通過 ＋ 晨星剛形成": {"10": {"n": 271, "moonshotN": 13, "pct": 4.8, "avg": 45.5}, "20": {"n": 271, "moonshotN": 30, "pct": 11.1, "avg": 46.4}},
    "多方力道≥80 ＋ 母子懷抱(高檔)剛形成 ＋ 夜星剛形成": {"10": {"n": 118, "moonshotN": 4, "pct": 3.4, "avg": 41.6}, "20": {"n": 118, "moonshotN": 13, "pct": 11.0, "avg": 46.2}},
    "布林通道高檔(≥80%) ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 228, "moonshotN": 7, "pct": 3.1, "avg": 49.2}, "20": {"n": 228, "moonshotN": 25, "pct": 11.0, "avg": 46.3}},
    "多方力道≥80 ＋ 量能區間高檔(≥90百分位) ＋ 母子懷抱(低檔)剛形成": {"10": {"n": 128, "moonshotN": 10, "pct": 7.8, "avg": 75.2}, "20": {"n": 128, "moonshotN": 14, "pct": 10.9, "avg": 47.4}},
    "強勢突破盤 ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 129, "moonshotN": 3, "pct": 2.3, "avg": 50.3}, "20": {"n": 129, "moonshotN": 14, "pct": 10.9, "avg": 44.4}},
    "相對強弱為正(強於大盤) ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 229, "moonshotN": 7, "pct": 3.1, "avg": 49.2}, "20": {"n": 229, "moonshotN": 25, "pct": 10.9, "avg": 46.3}},
    "創52週新高 ＋ 晨星剛形成": {"10": {"n": 231, "moonshotN": 7, "pct": 3.0, "avg": 49.2}, "20": {"n": 231, "moonshotN": 25, "pct": 10.8, "avg": 46.3}},
    "多方力道≥65 ＋ 地量(≤0.5倍均量) ＋ 量能區間高檔(≥90百分位)": {"10": {"n": 648, "moonshotN": 40, "pct": 6.2, "avg": 91.8}, "20": {"n": 648, "moonshotN": 70, "pct": 10.8, "avg": 67.4}},
    "布林通道高檔(≥80%) ＋ 地量(≤0.5倍均量) ＋ 量能區間高檔(≥90百分位)": {"10": {"n": 195, "moonshotN": 5, "pct": 2.6, "avg": 68.8}, "20": {"n": 195, "moonshotN": 21, "pct": 10.8, "avg": 66.0}},
    "爆量(≥2倍均量) ＋ 回後買上漲全通過 ＋ 晨星剛形成": {"10": {"n": 102, "moonshotN": 2, "pct": 2.0, "avg": 39.8}, "20": {"n": 102, "moonshotN": 11, "pct": 10.8, "avg": 50.6}},
    "大盤站上20日均線 ＋ 距52週高點≤5% ＋ 晨星剛形成": {"10": {"n": 297, "moonshotN": 15, "pct": 5.1, "avg": 47.1}, "20": {"n": 297, "moonshotN": 32, "pct": 10.8, "avg": 49.7}},
    "回後買上漲全通過 ＋ 母子懷抱(低檔)剛形成 ＋ 晨星剛形成": {"10": {"n": 157, "moonshotN": 5, "pct": 3.2, "avg": 55.0}, "20": {"n": 157, "moonshotN": 17, "pct": 10.8, "avg": 44.6}},
    "創52週新高 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 晨星剛形成": {"10": {"n": 213, "moonshotN": 7, "pct": 3.3, "avg": 49.2}, "20": {"n": 213, "moonshotN": 23, "pct": 10.8, "avg": 45.8}},
    "多方力道≥65 ＋ 爆量(≥1.5倍均量) ＋ 晨星剛形成": {"10": {"n": 270, "moonshotN": 16, "pct": 5.9, "avg": 54.1}, "20": {"n": 270, "moonshotN": 29, "pct": 10.7, "avg": 49.8}},
    "KDJ近3日內黃金交叉 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 夜星剛形成": {"10": {"n": 280, "moonshotN": 10, "pct": 3.6, "avg": 45.0}, "20": {"n": 280, "moonshotN": 30, "pct": 10.7, "avg": 49.0}},
    "相對強弱為正(強於大盤) ＋ 母子懷抱(高檔)剛形成 ＋ 夜星剛形成": {"10": {"n": 394, "moonshotN": 16, "pct": 4.1, "avg": 44.3}, "20": {"n": 394, "moonshotN": 42, "pct": 10.7, "avg": 52.6}},
    "大盤站上20日均線 ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 169, "moonshotN": 6, "pct": 3.6, "avg": 52.0}, "20": {"n": 169, "moonshotN": 18, "pct": 10.7, "avg": 48.7}},
    "布林通道高檔(≥80%) ＋ 爆量(≥2倍均量) ＋ 母子懷抱(低檔)剛形成": {"10": {"n": 113, "moonshotN": 7, "pct": 6.2, "avg": 51.3}, "20": {"n": 113, "moonshotN": 12, "pct": 10.6, "avg": 40.9}},
    "MACD近3日內黃金交叉 ＋ 距52週高點≤5% ＋ 晨星剛形成": {"10": {"n": 94, "moonshotN": 2, "pct": 2.1, "avg": 32.8}, "20": {"n": 94, "moonshotN": 10, "pct": 10.6, "avg": 43.2}},
    "KDJ近3日內死亡交叉 ＋ 大盤跌破20日均線 ＋ 夜星剛形成": {"10": {"n": 386, "moonshotN": 13, "pct": 3.4, "avg": 43.3}, "20": {"n": 386, "moonshotN": 41, "pct": 10.6, "avg": 52.1}},
    "多方力道≥80 ＋ 相對強弱為正(強於大盤) ＋ 晨星剛形成": {"10": {"n": 372, "moonshotN": 23, "pct": 6.2, "avg": 58.0}, "20": {"n": 372, "moonshotN": 39, "pct": 10.5, "avg": 52.4}},
    "多方力道≥80 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 晨星剛形成": {"10": {"n": 362, "moonshotN": 24, "pct": 6.6, "avg": 57.0}, "20": {"n": 362, "moonshotN": 38, "pct": 10.5, "avg": 52.9}},
    "MACD近3日內黃金交叉 ＋ 爆量(≥2倍均量) ＋ 晨星剛形成": {"10": {"n": 95, "moonshotN": 4, "pct": 4.2, "avg": 47.7}, "20": {"n": 95, "moonshotN": 10, "pct": 10.5, "avg": 57.1}},
    "KDJ近3日內黃金交叉 ＋ 母子懷抱(高檔)剛形成 ＋ 夜星剛形成": {"10": {"n": 152, "moonshotN": 5, "pct": 3.3, "avg": 43.6}, "20": {"n": 152, "moonshotN": 16, "pct": 10.5, "avg": 46.9}},
    "量能區間高檔(≥90百分位) ＋ 大盤跌破20日均線 ＋ 夜星剛形成": {"10": {"n": 181, "moonshotN": 7, "pct": 3.9, "avg": 44.3}, "20": {"n": 181, "moonshotN": 19, "pct": 10.5, "avg": 47.7}},
    "量能區間高檔(≥90百分位) ＋ K線橫盤的突破剛形成 ＋ 晨星剛形成": {"10": {"n": 105, "moonshotN": 5, "pct": 4.8, "avg": 36.7}, "20": {"n": 105, "moonshotN": 11, "pct": 10.5, "avg": 42.7}},
    "多方力道≥80 ＋ 晨星剛形成": {"10": {"n": 375, "moonshotN": 24, "pct": 6.4, "avg": 57.0}, "20": {"n": 375, "moonshotN": 39, "pct": 10.4, "avg": 52.4}},
    "多方力道≥80 ＋ 大盤站上60日均線 ＋ 晨星剛形成": {"10": {"n": 346, "moonshotN": 24, "pct": 6.9, "avg": 57.0}, "20": {"n": 346, "moonshotN": 36, "pct": 10.4, "avg": 53.3}},
    "KDJ近3日內死亡交叉 ＋ 回後買上漲全通過 ＋ 母子懷抱(低檔)剛形成": {"10": {"n": 115, "moonshotN": 4, "pct": 3.5, "avg": 54.5}, "20": {"n": 115, "moonshotN": 12, "pct": 10.4, "avg": 45.7}},
    "相對強弱為正(強於大盤) ＋ K線橫盤的突破剛形成 ＋ 晨星剛形成": {"10": {"n": 125, "moonshotN": 7, "pct": 5.6, "avg": 36.2}, "20": {"n": 125, "moonshotN": 13, "pct": 10.4, "avg": 43.4}},
    "K線橫盤的突破剛形成 ＋ 晨星剛形成": {"10": {"n": 156, "moonshotN": 9, "pct": 5.8, "avg": 35.8}, "20": {"n": 156, "moonshotN": 16, "pct": 10.3, "avg": 43.0}},
    "多方力道≥65 ＋ KDJ近3日內黃金交叉 ＋ 夜星剛形成": {"10": {"n": 242, "moonshotN": 8, "pct": 3.3, "avg": 43.4}, "20": {"n": 242, "moonshotN": 25, "pct": 10.3, "avg": 48.4}},
    "多方力道≥80 ＋ 大盤站上20日均線 ＋ 晨星剛形成": {"10": {"n": 290, "moonshotN": 22, "pct": 7.6, "avg": 57.7}, "20": {"n": 290, "moonshotN": 30, "pct": 10.3, "avg": 53.4}},
    "相對強弱為正(強於大盤) ＋ 地量(≤0.5倍均量) ＋ 量能區間高檔(≥90百分位)": {"10": {"n": 690, "moonshotN": 42, "pct": 6.1, "avg": 89.8}, "20": {"n": 690, "moonshotN": 71, "pct": 10.3, "avg": 67.5}},
    "大盤跌破20日均線 ＋ 近3日向上跳空缺口 ＋ 母子懷抱(低檔)剛形成": {"10": {"n": 204, "moonshotN": 4, "pct": 2.0, "avg": 41.6}, "20": {"n": 204, "moonshotN": 21, "pct": 10.3, "avg": 43.5}},
    "相對強弱為正(強於大盤) ＋ 布林通道收窄(寬度近半年最低20%) ＋ 夜星剛形成": {"10": {"n": 127, "moonshotN": 3, "pct": 2.4, "avg": 43.5}, "20": {"n": 127, "moonshotN": 13, "pct": 10.2, "avg": 42.7}},
    "爆量(≥1.5倍均量) ＋ 母子懷抱(低檔)剛形成 ＋ 晨星剛形成": {"10": {"n": 236, "moonshotN": 9, "pct": 3.8, "avg": 60.0}, "20": {"n": 236, "moonshotN": 24, "pct": 10.2, "avg": 47.0}},
    "大盤站上60日均線 ＋ 創52週新高 ＋ 晨星剛形成": {"10": {"n": 205, "moonshotN": 6, "pct": 2.9, "avg": 52.0}, "20": {"n": 205, "moonshotN": 21, "pct": 10.2, "avg": 47.4}},
    "布林通道高檔(≥80%) ＋ 爆量(≥1.5倍均量) ＋ 母子懷抱(低檔)剛形成": {"10": {"n": 228, "moonshotN": 11, "pct": 4.8, "avg": 66.2}, "20": {"n": 228, "moonshotN": 23, "pct": 10.1, "avg": 47.1}},
    "KDJ近3日內死亡交叉 ＋ 均線多頭排列(5>20>60且站上月線) ＋ 夜星剛形成": {"10": {"n": 632, "moonshotN": 27, "pct": 4.3, "avg": 45.2}, "20": {"n": 632, "moonshotN": 64, "pct": 10.1, "avg": 51.4}},
    "相對強弱為正(強於大盤) ＋ 近3日向上跳空缺口 ＋ 夜星剛形成": {"10": {"n": 248, "moonshotN": 12, "pct": 4.8, "avg": 44.4}, "20": {"n": 248, "moonshotN": 25, "pct": 10.1, "avg": 54.8}},
    "爆量(≥1.5倍均量) ＋ 大盤跌破20日均線 ＋ 夜星剛形成": {"10": {"n": 109, "moonshotN": 4, "pct": 3.7, "avg": 37.6}, "20": {"n": 109, "moonshotN": 11, "pct": 10.1, "avg": 52.5}},
    "大盤跌破20日均線 ＋ 布林通道收窄(寬度近半年最低20%) ＋ 晨星剛形成": {"10": {"n": 208, "moonshotN": 8, "pct": 3.8, "avg": 51.7}, "20": {"n": 208, "moonshotN": 21, "pct": 10.1, "avg": 63.5}},
}
BT_HIT_SHOW = 3   # 摘要表每類最多顯示幾個編號，其餘以「+N」表示


def CONSTANTS():
    return dict(SP500_LIST=SP500_LIST, SP500_NAMES=SP500_NAMES, NASDAQ100_LIST=NASDAQ100_LIST, SOX_LIST=SOX_LIST,
                MY_LIST_DEFAULT=MY_LIST_DEFAULT, PINNED_COMBOS=PINNED_COMBOS, PINNED_COMBO_WINRATES=PINNED_COMBO_WINRATES,
                MOONSHOT_COMBOS=MOONSHOT_COMBOS, MOONSHOT_COMBO_STATS=MOONSHOT_COMBO_STATS,
                STOCK_PICK_COMBOS=STOCK_PICK_COMBOS, STOCK_PICK_STATS=STOCK_PICK_STATS, COMBO_REF_STATS=COMBO_REF_STATS,
                BT_WIN_COMBOS=BT_WIN_COMBOS, BT_WIN_STATS=BT_WIN_STATS, BT_HOT_COMBOS=BT_HOT_COMBOS, BT_HOT_STATS=BT_HOT_STATS)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'daily':
        sys.exit(run_daily(sys.argv[2:]))
    main()
