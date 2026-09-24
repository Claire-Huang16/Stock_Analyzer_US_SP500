# -*- coding: utf-8 -*-
"""
US技術分析全攻略 · 美股評分分析系統 (Streamlit 版)
由 stock_analyzer_us.html 轉換而成，邏輯與原 HTML/JS 版本一致：
- 朱家泓四維度評分（趨勢／K線／均線／成交量，各25分，共100分），套用於美股個股分析
- 回後買上漲 8 條件核對
- 15 種進場型態確認（含「剛突破」：與前一交易日比較的新鮮突破訊號）
- 批次分析摘要表（可依進場條件／評分／型態確認篩選，關鍵字搜尋）
- Plotly K線＋均線＋布林通道＋成交量＋MACD 圖表
- OpenAI API「AI 智能綜合分析」
資料來源：Financial Modeling Prep（FMP）API /stable/ 端點
"""

import time
import json
import os
import io
import sqlite3
import itertools
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import streamlit as st
from plotly.subplots import make_subplots
import plotly.graph_objects as go

st.set_page_config(page_title="US技術分析全攻略 · 美股評分分析系統", page_icon="📊", layout="wide")

FMP_BASE = "https://financialmodelingprep.com/stable"

# ────────────────────────────────────────────────────────────────
# 預設股票清單（S&P 500 / 我的清單）
# ────────────────────────────────────────────────────────────────
SP500_LIST = ['MMM', 'AOS', 'ABT', 'ABBV', 'ACN', 'ADBE', 'AMD', 'AES', 'AFL', 'A', 'APD', 'ABNB', 'AKAM', 'ALB', 'ARE', 'ALGN', 'ALLE', 'LNT', 'ALL', 'GOOGL', 'GOOG', 'MO', 'AMZN', 'AMCR', 'AEE', 'AEP', 'AXP', 'AIG', 'AMT', 'AWK', 'AMP', 'AME', 'AMGN', 'APH', 'ADI', 'AON', 'APA', 'APO', 'AAPL', 'AMAT', 'APP', 'APTV', 'ACGL', 'ADM', 'ARES', 'ANET', 'AJG', 'AIZ', 'T', 'ATO', 'ADSK', 'ADP', 'AZO', 'AVB', 'AVY', 'AXON', 'BKR', 'BALL', 'BAC', 'BAX', 'BDX', 'BRK.B', 'BBY', 'TECH', 'BIIB', 'BLK', 'BX', 'XYZ', 'BNY', 'BA', 'BKNG', 'BSX', 'BMY', 'AVGO', 'BR', 'BRO', 'BF.B', 'BLDR', 'BG', 'BXP', 'CHRW', 'CDNS', 'CPT', 'COF', 'CAH', 'CCL', 'CARR', 'CVNA', 'CASY', 'CAT', 'CBOE', 'CBRE', 'CDW', 'COR', 'CNC', 'CNP', 'CF', 'CRL', 'SCHW', 'CHTR', 'CVX', 'CMG', 'CB', 'CHD', 'CIEN', 'CI', 'CINF', 'CTAS', 'CSCO', 'C', 'CFG', 'CLX', 'CME', 'CMS', 'KO', 'CTSH', 'COHR', 'COIN', 'CL', 'CMCSA', 'FIX', 'COP', 'ED', 'STZ', 'CEG', 'COO', 'CPRT', 'GLW', 'CPAY', 'CTVA', 'CSGP', 'COST', 'CRH', 'CRWD', 'CCI', 'CSX', 'CMI', 'CVS', 'DHR', 'DRI', 'DDOG', 'DVA', 'DECK', 'DE', 'DELL', 'DAL', 'DVN', 'DXCM', 'FANG', 'DLR', 'DG', 'DLTR', 'D', 'DPZ', 'DASH', 'DOV', 'DOW', 'DHI', 'DTE', 'DUK', 'DD', 'ETN', 'EBAY', 'ECHO', 'ECL', 'EIX', 'EW', 'EA', 'ELV', 'EME', 'EMR', 'ETR', 'EOG', 'EQT', 'EFX', 'EQIX', 'EQR', 'ERIE', 'ESS', 'EL', 'EG', 'EVRG', 'ES', 'EXC', 'EXE', 'EXPE', 'EXPD', 'EXR', 'XOM', 'FFIV', 'FDS', 'FICO', 'FAST', 'FRT', 'FDX', 'FDXF', 'FIS', 'FITB', 'FSLR', 'FE', 'FISV', 'FLEX', 'F', 'FTNT', 'FTV', 'FOXA', 'FOX', 'BEN', 'FCX', 'GRMN', 'IT', 'GE', 'GEHC', 'GEV', 'GEN', 'GNRC', 'GD', 'GIS', 'GM', 'GPC', 'GILD', 'GPN', 'GL', 'GDDY', 'GS', 'HAL', 'HIG', 'HAS', 'HCA', 'DOC', 'HSIC', 'HSY', 'HPE', 'HLT', 'HD', 'HONA', 'HON', 'HRL', 'HST', 'HWM', 'HPQ', 'HUBB', 'HUM', 'HBAN', 'HII', 'IBM', 'IEX', 'IDXX', 'ITW', 'INCY', 'IR', 'PODD', 'INTC', 'IBKR', 'ICE', 'IFF', 'IP', 'INTU', 'ISRG', 'IVZ', 'INVH', 'IQV', 'IRM', 'JBHT', 'JBL', 'JKHY', 'J', 'JNJ', 'JCI', 'JPM', 'KVUE', 'KDP', 'KEY', 'KEYS', 'KMB', 'KIM', 'KMI', 'KKR', 'KLAC', 'KHC', 'KR', 'LHX', 'LH', 'LRCX', 'LVS', 'LDOS', 'LEN', 'LII', 'LLY', 'LIN', 'LYV', 'LMT', 'L', 'LOW', 'LULU', 'LITE', 'LYB', 'MTB', 'MPC', 'MAR', 'MRSH', 'MLM', 'MRVL', 'MAS', 'MA', 'MKC', 'MCD', 'MCK', 'MDT', 'MRK', 'META', 'MET', 'MTD', 'MGM', 'MCHP', 'MU', 'MSFT', 'MAA', 'MRNA', 'TAP', 'MDLZ', 'MPWR', 'MNST', 'MCO', 'MS', 'MOS', 'MSI', 'MSCI', 'NDAQ', 'NTAP', 'NFLX', 'NEM', 'NWSA', 'NWS', 'NEE', 'NKE', 'NI', 'NDSN', 'NSC', 'NTRS', 'NOC', 'NCLH', 'NRG', 'NUE', 'NVDA', 'NVR', 'NXPI', 'ORLY', 'OXY', 'ODFL', 'OMC', 'ON', 'OKE', 'ORCL', 'OTIS', 'PCAR', 'PKG', 'PLTR', 'PANW', 'PSKY', 'PH', 'PAYX', 'PYPL', 'PNR', 'PEP', 'PFE', 'PCG', 'PM', 'PSX', 'PNW', 'PNC', 'PPG', 'PPL', 'PFG', 'PG', 'PGR', 'PLD', 'PRU', 'PEG', 'PTC', 'PSA', 'PHM', 'PWR', 'QCOM', 'DGX', 'Q', 'RL', 'RJF', 'RTX', 'O', 'REG', 'REGN', 'RF', 'RSG', 'RMD', 'RVTY', 'HOOD', 'ROK', 'ROL', 'ROP', 'ROST', 'RCL', 'SPGI', 'CRM', 'SNDK', 'SBAC', 'SLB', 'STX', 'SRE', 'NOW', 'SHW', 'SPG', 'SWKS', 'SJM', 'SW', 'SNA', 'SOLV', 'SO', 'LUV', 'SWK', 'SBUX', 'STT', 'STLD', 'STE', 'SYK', 'SMCI', 'SYF', 'SNPS', 'SYY', 'TMUS', 'TROW', 'TTWO', 'TPR', 'TRGP', 'TGT', 'TEL', 'TDY', 'TER', 'TSLA', 'TXN', 'TPL', 'TXT', 'TMO', 'TJX', 'TKO', 'TTD', 'TSCO', 'TT', 'TDG', 'TRV', 'TRMB', 'TFC', 'TYL', 'TSN', 'USB', 'UBER', 'UDR', 'ULTA', 'UNP', 'UAL', 'UPS', 'URI', 'UNH', 'UHS', 'VLO', 'VEEV', 'VTR', 'VLTO', 'VRSN', 'VRSK', 'VZ', 'VRTX', 'VRT', 'VTRS', 'VICI', 'V', 'VST', 'VMC', 'WRB', 'GWW', 'WAB', 'WMT', 'DIS', 'WBD', 'WM', 'WAT', 'WEC', 'WFC', 'WELL', 'WST', 'WDC', 'WY', 'WSM', 'WMB', 'WTW', 'WDAY', 'WYNN', 'XEL', 'XYL', 'YUM', 'ZBRA', 'ZBH', 'ZTS']

SP500_NAMES = {
    'MMM': '3M',
    'AOS': 'A. O. Smith',
    'ABT': 'Abbott Laboratories',
    'ABBV': 'AbbVie',
    'ACN': 'Accenture',
    'ADBE': 'Adobe Inc.',
    'AMD': 'Advanced Micro Devices',
    'AES': 'AES Corporation',
    'AFL': 'Aflac',
    'A': 'Agilent Technologies',
    'APD': 'Air Products',
    'ABNB': 'Airbnb',
    'AKAM': 'Akamai Technologies',
    'ALB': 'Albemarle Corporation',
    'ARE': 'Alexandria Real Estate Equities',
    'ALGN': 'Align Technology',
    'ALLE': 'Allegion',
    'LNT': 'Alliant Energy',
    'ALL': 'Allstate',
    'GOOGL': 'Alphabet Inc. (Class A)',
    'GOOG': 'Alphabet Inc. (Class C)',
    'MO': 'Altria',
    'AMZN': 'Amazon',
    'AMCR': 'Amcor',
    'AEE': 'Ameren',
    'AEP': 'American Electric Power',
    'AXP': 'American Express',
    'AIG': 'American International Group',
    'AMT': 'American Tower',
    'AWK': 'American Water Works',
    'AMP': 'Ameriprise Financial',
    'AME': 'Ametek',
    'AMGN': 'Amgen',
    'APH': 'Amphenol',
    'ADI': 'Analog Devices',
    'AON': 'Aon plc',
    'APA': 'APA Corporation',
    'APO': 'Apollo Global Management',
    'AAPL': 'Apple Inc.',
    'AMAT': 'Applied Materials',
    'APP': 'AppLovin',
    'APTV': 'Aptiv',
    'ACGL': 'Arch Capital Group',
    'ADM': 'Archer Daniels Midland',
    'ARES': 'Ares Management',
    'ANET': 'Arista Networks',
    'AJG': 'Arthur J. Gallagher & Co.',
    'AIZ': 'Assurant',
    'T': 'AT&T',
    'ATO': 'Atmos Energy',
    'ADSK': 'Autodesk',
    'ADP': 'Automatic Data Processing',
    'AZO': 'AutoZone',
    'AVB': 'AvalonBay Communities',
    'AVY': 'Avery Dennison',
    'AXON': 'Axon Enterprise',
    'BKR': 'Baker Hughes',
    'BALL': 'Ball Corporation',
    'BAC': 'Bank of America',
    'BAX': 'Baxter International',
    'BDX': 'Becton Dickinson',
    'BRK.B': 'Berkshire Hathaway',
    'BBY': 'Best Buy',
    'TECH': 'Bio-Techne',
    'BIIB': 'Biogen',
    'BLK': 'BlackRock',
    'BX': 'Blackstone Inc.',
    'XYZ': 'Block Inc.',
    'BNY': 'BNY Mellon',
    'BA': 'Boeing',
    'BKNG': 'Booking Holdings',
    'BSX': 'Boston Scientific',
    'BMY': 'Bristol Myers Squibb',
    'AVGO': 'Broadcom',
    'BR': 'Broadridge Financial Solutions',
    'BRO': 'Brown & Brown',
    'BF.B': 'Brown-Forman',
    'BLDR': 'Builders FirstSource',
    'BG': 'Bunge Global',
    'BXP': 'BXP Inc.',
    'CHRW': 'C.H. Robinson',
    'CDNS': 'Cadence Design Systems',
    'CPT': 'Camden Property Trust',
    'COF': 'Capital One',
    'CAH': 'Cardinal Health',
    'CCL': 'Carnival Corporation',
    'CARR': 'Carrier Global',
    'CVNA': 'Carvana',
    'CASY': 'Casey\'s',
    'CAT': 'Caterpillar Inc.',
    'CBOE': 'Cboe Global Markets',
    'CBRE': 'CBRE Group',
    'CDW': 'CDW Corporation',
    'COR': 'Cencora',
    'CNC': 'Centene Corporation',
    'CNP': 'CenterPoint Energy',
    'CF': 'CF Industries',
    'CRL': 'Charles River Laboratories',
    'SCHW': 'Charles Schwab Corporation',
    'CHTR': 'Charter Communications',
    'CVX': 'Chevron Corporation',
    'CMG': 'Chipotle Mexican Grill',
    'CB': 'Chubb Limited',
    'CHD': 'Church & Dwight',
    'CIEN': 'Ciena',
    'CI': 'Cigna',
    'CINF': 'Cincinnati Financial',
    'CTAS': 'Cintas',
    'CSCO': 'Cisco',
    'C': 'Citigroup',
    'CFG': 'Citizens Financial Group',
    'CLX': 'Clorox',
    'CME': 'CME Group',
    'CMS': 'CMS Energy',
    'KO': 'Coca-Cola Company',
    'CTSH': 'Cognizant',
    'COHR': 'Coherent Corp.',
    'COIN': 'Coinbase',
    'CL': 'Colgate-Palmolive',
    'CMCSA': 'Comcast',
    'FIX': 'Comfort Systems USA',
    'COP': 'ConocoPhillips',
    'ED': 'Consolidated Edison',
    'STZ': 'Constellation Brands',
    'CEG': 'Constellation Energy',
    'COO': 'Cooper Companies',
    'CPRT': 'Copart',
    'GLW': 'Corning Inc.',
    'CPAY': 'Corpay',
    'CTVA': 'Corteva',
    'CSGP': 'CoStar Group',
    'COST': 'Costco',
    'CRH': 'CRH plc',
    'CRWD': 'CrowdStrike',
    'CCI': 'Crown Castle',
    'CSX': 'CSX Corporation',
    'CMI': 'Cummins',
    'CVS': 'CVS Health',
    'DHR': 'Danaher Corporation',
    'DRI': 'Darden Restaurants',
    'DDOG': 'Datadog',
    'DVA': 'DaVita',
    'DECK': 'Deckers Brands',
    'DE': 'Deere & Company',
    'DELL': 'Dell Technologies',
    'DAL': 'Delta Air Lines',
    'DVN': 'Devon Energy',
    'DXCM': 'Dexcom',
    'FANG': 'Diamondback Energy',
    'DLR': 'Digital Realty',
    'DG': 'Dollar General',
    'DLTR': 'Dollar Tree',
    'D': 'Dominion Energy',
    'DPZ': 'Domino\'s',
    'DASH': 'DoorDash',
    'DOV': 'Dover Corporation',
    'DOW': 'Dow Inc.',
    'DHI': 'D.R. Horton',
    'DTE': 'DTE Energy',
    'DUK': 'Duke Energy',
    'DD': 'DuPont',
    'ETN': 'Eaton Corporation',
    'EBAY': 'eBay Inc.',
    'ECHO': 'EchoStar',
    'ECL': 'Ecolab',
    'EIX': 'Edison International',
    'EW': 'Edwards Lifesciences',
    'EA': 'Electronic Arts',
    'ELV': 'Elevance Health',
    'EME': 'Emcor',
    'EMR': 'Emerson Electric',
    'ETR': 'Entergy',
    'EOG': 'EOG Resources',
    'EQT': 'EQT Corporation',
    'EFX': 'Equifax',
    'EQIX': 'Equinix',
    'EQR': 'Equity Residential',
    'ERIE': 'Erie Indemnity',
    'ESS': 'Essex Property Trust',
    'EL': 'Estee Lauder Companies',
    'EG': 'Everest Group',
    'EVRG': 'Evergy',
    'ES': 'Eversource Energy',
    'EXC': 'Exelon',
    'EXE': 'Expand Energy',
    'EXPE': 'Expedia Group',
    'EXPD': 'Expeditors International',
    'EXR': 'Extra Space Storage',
    'XOM': 'ExxonMobil',
    'FFIV': 'F5 Inc.',
    'FDS': 'FactSet',
    'FICO': 'Fair Isaac',
    'FAST': 'Fastenal',
    'FRT': 'Federal Realty Investment Trust',
    'FDX': 'FedEx',
    'FDXF': 'FedEx Freight',
    'FIS': 'Fidelity National Information Services',
    'FITB': 'Fifth Third Bancorp',
    'FSLR': 'First Solar',
    'FE': 'FirstEnergy',
    'FISV': 'Fiserv',
    'FLEX': 'Flex Ltd.',
    'F': 'Ford Motor Company',
    'FTNT': 'Fortinet',
    'FTV': 'Fortive',
    'FOXA': 'Fox Corporation (Class A)',
    'FOX': 'Fox Corporation (Class B)',
    'BEN': 'Franklin Resources',
    'FCX': 'Freeport-McMoRan',
    'GRMN': 'Garmin',
    'IT': 'Gartner',
    'GE': 'GE Aerospace',
    'GEHC': 'GE HealthCare',
    'GEV': 'GE Vernova',
    'GEN': 'Gen Digital',
    'GNRC': 'Generac',
    'GD': 'General Dynamics',
    'GIS': 'General Mills',
    'GM': 'General Motors',
    'GPC': 'Genuine Parts Company',
    'GILD': 'Gilead Sciences',
    'GPN': 'Global Payments',
    'GL': 'Globe Life',
    'GDDY': 'GoDaddy',
    'GS': 'Goldman Sachs',
    'HAL': 'Halliburton',
    'HIG': 'Hartford',
    'HAS': 'Hasbro',
    'HCA': 'HCA Healthcare',
    'DOC': 'Healthpeak Properties',
    'HSIC': 'Henry Schein',
    'HSY': 'Hershey Company',
    'HPE': 'Hewlett Packard Enterprise',
    'HLT': 'Hilton Worldwide',
    'HD': 'Home Depot',
    'HONA': 'Honeywell Aerospace',
    'HON': 'Honeywell Technologies',
    'HRL': 'Hormel Foods',
    'HST': 'Host Hotels & Resorts',
    'HWM': 'Howmet Aerospace',
    'HPQ': 'HP Inc.',
    'HUBB': 'Hubbell Incorporated',
    'HUM': 'Humana',
    'HBAN': 'Huntington Bancshares',
    'HII': 'Huntington Ingalls Industries',
    'IBM': 'IBM',
    'IEX': 'IDEX Corporation',
    'IDXX': 'Idexx Laboratories',
    'ITW': 'Illinois Tool Works',
    'INCY': 'Incyte',
    'IR': 'Ingersoll Rand',
    'PODD': 'Insulet Corporation',
    'INTC': 'Intel',
    'IBKR': 'Interactive Brokers',
    'ICE': 'Intercontinental Exchange',
    'IFF': 'International Flavors & Fragrances',
    'IP': 'International Paper',
    'INTU': 'Intuit',
    'ISRG': 'Intuitive Surgical',
    'IVZ': 'Invesco',
    'INVH': 'Invitation Homes',
    'IQV': 'IQVIA',
    'IRM': 'Iron Mountain',
    'JBHT': 'J.B. Hunt',
    'JBL': 'Jabil',
    'JKHY': 'Jack Henry & Associates',
    'J': 'Jacobs Solutions',
    'JNJ': 'Johnson & Johnson',
    'JCI': 'Johnson Controls',
    'JPM': 'JPMorgan Chase',
    'KVUE': 'Kenvue',
    'KDP': 'Keurig Dr Pepper',
    'KEY': 'KeyCorp',
    'KEYS': 'Keysight Technologies',
    'KMB': 'Kimberly-Clark',
    'KIM': 'Kimco Realty',
    'KMI': 'Kinder Morgan',
    'KKR': 'KKR & Co.',
    'KLAC': 'KLA Corporation',
    'KHC': 'Kraft Heinz',
    'KR': 'Kroger',
    'LHX': 'L3Harris',
    'LH': 'Labcorp',
    'LRCX': 'Lam Research',
    'LVS': 'Las Vegas Sands',
    'LDOS': 'Leidos',
    'LEN': 'Lennar',
    'LII': 'Lennox International',
    'LLY': 'Lilly (Eli)',
    'LIN': 'Linde plc',
    'LYV': 'Live Nation Entertainment',
    'LMT': 'Lockheed Martin',
    'L': 'Loews Corporation',
    'LOW': 'Lowe\'s',
    'LULU': 'Lululemon Athletica',
    'LITE': 'Lumentum',
    'LYB': 'LyondellBasell',
    'MTB': 'M&T Bank',
    'MPC': 'Marathon Petroleum',
    'MAR': 'Marriott International',
    'MRSH': 'Marsh McLennan',
    'MLM': 'Martin Marietta Materials',
    'MRVL': 'Marvell Technology',
    'MAS': 'Masco',
    'MA': 'Mastercard',
    'MKC': 'McCormick & Company',
    'MCD': 'McDonald\'s',
    'MCK': 'McKesson Corporation',
    'MDT': 'Medtronic',
    'MRK': 'Merck & Co.',
    'META': 'Meta Platforms',
    'MET': 'MetLife',
    'MTD': 'Mettler Toledo',
    'MGM': 'MGM Resorts',
    'MCHP': 'Microchip Technology',
    'MU': 'Micron Technology',
    'MSFT': 'Microsoft',
    'MAA': 'Mid-America Apartment Communities',
    'MRNA': 'Moderna',
    'TAP': 'Molson Coors Beverage Company',
    'MDLZ': 'Mondelez International',
    'MPWR': 'Monolithic Power Systems',
    'MNST': 'Monster Beverage',
    'MCO': 'Moody\'s Corporation',
    'MS': 'Morgan Stanley',
    'MOS': 'Mosaic Company',
    'MSI': 'Motorola Solutions',
    'MSCI': 'MSCI Inc.',
    'NDAQ': 'Nasdaq Inc.',
    'NTAP': 'NetApp',
    'NFLX': 'Netflix',
    'NEM': 'Newmont',
    'NWSA': 'News Corp (Class A)',
    'NWS': 'News Corp (Class B)',
    'NEE': 'NextEra Energy',
    'NKE': 'Nike Inc.',
    'NI': 'NiSource',
    'NDSN': 'Nordson Corporation',
    'NSC': 'Norfolk Southern',
    'NTRS': 'Northern Trust',
    'NOC': 'Northrop Grumman',
    'NCLH': 'Norwegian Cruise Line Holdings',
    'NRG': 'NRG Energy',
    'NUE': 'Nucor',
    'NVDA': 'Nvidia',
    'NVR': 'NVR Inc.',
    'NXPI': 'NXP Semiconductors',
    'ORLY': 'O\'Reilly Automotive',
    'OXY': 'Occidental Petroleum',
    'ODFL': 'Old Dominion',
    'OMC': 'Omnicom Group',
    'ON': 'ON Semiconductor',
    'OKE': 'Oneok',
    'ORCL': 'Oracle Corporation',
    'OTIS': 'Otis Worldwide',
    'PCAR': 'Paccar',
    'PKG': 'Packaging Corporation of America',
    'PLTR': 'Palantir Technologies',
    'PANW': 'Palo Alto Networks',
    'PSKY': 'Paramount Skydance Corporation',
    'PH': 'Parker Hannifin',
    'PAYX': 'Paychex',
    'PYPL': 'PayPal',
    'PNR': 'Pentair',
    'PEP': 'PepsiCo',
    'PFE': 'Pfizer',
    'PCG': 'PG&E Corporation',
    'PM': 'Philip Morris International',
    'PSX': 'Phillips 66',
    'PNW': 'Pinnacle West Capital',
    'PNC': 'PNC Financial Services',
    'PPG': 'PPG Industries',
    'PPL': 'PPL Corporation',
    'PFG': 'Principal Financial Group',
    'PG': 'Procter & Gamble',
    'PGR': 'Progressive Corporation',
    'PLD': 'Prologis',
    'PRU': 'Prudential Financial',
    'PEG': 'Public Service Enterprise Group',
    'PTC': 'PTC Inc.',
    'PSA': 'Public Storage',
    'PHM': 'PulteGroup',
    'PWR': 'Quanta Services',
    'QCOM': 'Qualcomm',
    'DGX': 'Quest Diagnostics',
    'Q': 'Qnity Electronics',
    'RL': 'Ralph Lauren Corporation',
    'RJF': 'Raymond James Financial',
    'RTX': 'RTX Corporation',
    'O': 'Realty Income',
    'REG': 'Regency Centers',
    'REGN': 'Regeneron Pharmaceuticals',
    'RF': 'Regions Financial Corporation',
    'RSG': 'Republic Services',
    'RMD': 'ResMed',
    'RVTY': 'Revvity',
    'HOOD': 'Robinhood Markets',
    'ROK': 'Rockwell Automation',
    'ROL': 'Rollins Inc.',
    'ROP': 'Roper Technologies',
    'ROST': 'Ross Stores',
    'RCL': 'Royal Caribbean Group',
    'SPGI': 'S&P Global',
    'CRM': 'Salesforce',
    'SNDK': 'Sandisk',
    'SBAC': 'SBA Communications',
    'SLB': 'Schlumberger',
    'STX': 'Seagate Technology',
    'SRE': 'Sempra',
    'NOW': 'ServiceNow',
    'SHW': 'Sherwin-Williams',
    'SPG': 'Simon Property Group',
    'SWKS': 'Skyworks Solutions',
    'SJM': 'J.M. Smucker Company',
    'SW': 'Smurfit Westrock',
    'SNA': 'Snap-on',
    'SOLV': 'Solventum',
    'SO': 'Southern Company',
    'LUV': 'Southwest Airlines',
    'SWK': 'Stanley Black & Decker',
    'SBUX': 'Starbucks',
    'STT': 'State Street Corporation',
    'STLD': 'Steel Dynamics',
    'STE': 'Steris',
    'SYK': 'Stryker Corporation',
    'SMCI': 'Supermicro',
    'SYF': 'Synchrony Financial',
    'SNPS': 'Synopsys',
    'SYY': 'Sysco',
    'TMUS': 'T-Mobile US',
    'TROW': 'T. Rowe Price',
    'TTWO': 'Take-Two Interactive',
    'TPR': 'Tapestry Inc.',
    'TRGP': 'Targa Resources',
    'TGT': 'Target Corporation',
    'TEL': 'TE Connectivity',
    'TDY': 'Teledyne Technologies',
    'TER': 'Teradyne',
    'TSLA': 'Tesla Inc.',
    'TXN': 'Texas Instruments',
    'TPL': 'Texas Pacific Land Corporation',
    'TXT': 'Textron',
    'TMO': 'Thermo Fisher Scientific',
    'TJX': 'TJX Companies',
    'TKO': 'TKO Group Holdings',
    'TTD': 'Trade Desk',
    'TSCO': 'Tractor Supply',
    'TT': 'Trane Technologies',
    'TDG': 'TransDigm Group',
    'TRV': 'Travelers Companies',
    'TRMB': 'Trimble Inc.',
    'TFC': 'Truist Financial',
    'TYL': 'Tyler Technologies',
    'TSN': 'Tyson Foods',
    'USB': 'U.S. Bancorp',
    'UBER': 'Uber',
    'UDR': 'UDR Inc.',
    'ULTA': 'Ulta Beauty',
    'UNP': 'Union Pacific Corporation',
    'UAL': 'United Airlines Holdings',
    'UPS': 'United Parcel Service',
    'URI': 'United Rentals',
    'UNH': 'UnitedHealth Group',
    'UHS': 'Universal Health Services',
    'VLO': 'Valero Energy',
    'VEEV': 'Veeva Systems',
    'VTR': 'Ventas',
    'VLTO': 'Veralto',
    'VRSN': 'Verisign',
    'VRSK': 'Verisk Analytics',
    'VZ': 'Verizon',
    'VRTX': 'Vertex Pharmaceuticals',
    'VRT': 'Vertiv',
    'VTRS': 'Viatris',
    'VICI': 'Vici Properties',
    'V': 'Visa Inc.',
    'VST': 'Vistra Corp.',
    'VMC': 'Vulcan Materials Company',
    'WRB': 'W.R. Berkley Corporation',
    'GWW': 'W.W. Grainger',
    'WAB': 'Wabtec',
    'WMT': 'Walmart',
    'DIS': 'Walt Disney Company',
    'WBD': 'Warner Bros. Discovery',
    'WM': 'Waste Management',
    'WAT': 'Waters Corporation',
    'WEC': 'WEC Energy Group',
    'WFC': 'Wells Fargo',
    'WELL': 'Welltower',
    'WST': 'West Pharmaceutical Services',
    'WDC': 'Western Digital',
    'WY': 'Weyerhaeuser',
    'WSM': 'Williams-Sonoma Inc.',
    'WMB': 'Williams Companies',
    'WTW': 'Willis Towers Watson',
    'WDAY': 'Workday Inc.',
    'WYNN': 'Wynn Resorts',
    'XEL': 'Xcel Energy',
    'XYL': 'Xylem Inc.',
    'YUM': 'Yum! Brands',
    'ZBRA': 'Zebra Technologies',
    'ZBH': 'Zimmer Biomet',
    'ZTS': 'Zoetis'
}

MY_LIST_DEFAULT = ['AXTI', 'LITE', 'COHR', 'RCAT', 'LWLG', 'UMC', 'AMKR', 'AEHR', 'ON', 'SMR', 'IREN', 'HIMX', 'TSEM', 'CRDO', 'PLTR', 'BABA', 'HOOD', 'ALAB', 'NVDA', 'MU', 'FTNT', 'AEX', 'OXY', 'MRVL', 'QCOM', 'XYZ', 'RKLB', 'FN', 'ORCL', 'AVGO', 'BE', 'CRWV', 'AMD', 'SHOP', 'VZ', 'OWL', 'TER', 'GOOGL', 'SMCI', 'QBTS', 'VRT', 'TSM', 'SNDK', 'ONDS', 'RCAT', 'NBIS', 'POET', 'TSEM', 'GLW', 'DELL', 'SPCX', 'ON', 'SMTC']
# SOX（費城半導體指數）30檔成分股，這是相對穩定的產業型指數，直接內建清單即可，不需要額外呼叫API
SOX_LIST = ['AMD', 'ADI', 'AMAT', 'ARM', 'ASML', 'ALAB', 'AVGO', 'COHR', 'CRDO', 'ENTG', 'GFS', 'INTC', 'KLAC', 'LRCX', 'MTSI', 'MRVL', 'MCHP', 'MU', 'MPWR', 'NVDA', 'NXPI', 'ON', 'QCOM', 'QRVO', 'SWKS', 'TSM', 'TER', 'LSCC', 'RMBS', 'ACLS']


# ────────────────────────────────────────────────────────────────
# 自訂清單（我的清單／清單1／清單2／清單3）：存成本機JSON檔，跨次啟動App都會保留
# （對應 HTML 版用 localStorage 的效果，只是這裡改用本機檔案，因為 Streamlit
# 的 session_state 每次重新啟動就會清空，沒有等同 localStorage 的內建機制）。
# 「我的清單」預設帶入原本內建的 MY_LIST_DEFAULT 觀察名單，清單1/2/3預設空白。
# ────────────────────────────────────────────────────────────────
CUSTOM_LISTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_lists_us.json")
CUSTOM_LIST_KEYS = ["my", "my1", "my2", "my3"]
CUSTOM_LIST_LABELS = {"my": "我的清單", "my1": "我的清單1", "my2": "我的清單2", "my3": "我的清單3"}


def load_custom_lists():
    defaults = {"my": list(MY_LIST_DEFAULT), "my1": [], "my2": [], "my3": []}
    if os.path.exists(CUSTOM_LISTS_FILE):
        try:
            with open(CUSTOM_LISTS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k in CUSTOM_LIST_KEYS:
                if k in saved and isinstance(saved[k], list):
                    defaults[k] = saved[k]
        except Exception:
            pass
    return defaults


def save_custom_lists(lists: dict):
    try:
        with open(CUSTOM_LISTS_FILE, "w", encoding="utf-8") as f:
            json.dump(lists, f, ensure_ascii=False)
    except Exception:
        pass


def parse_stock_tokens(text: str):
    stocks, seen = [], set()
    for tok in text.replace("，", ",").replace("、", ",").split():
        for s in tok.split(","):
            s = s.strip()
            if s and s not in seen:
                seen.add(s)
                stocks.append(s)
    return stocks


# ────────────────────────────────────────────────────────────────
# Financial Modeling Prep（FMP）API 存取
# ────────────────────────────────────────────────────────────────

def get_date_range(days_back: int):
    end = datetime.today()
    start = datetime.today() - timedelta(days=days_back)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def to_fmp_symbol(sid: str) -> str:
    """S&P500 清單內的 BRK.B / BF.B 等股票，FMP 慣例用「-」而非「.」"""
    return sid.replace(".", "-")


def api_fetch(url: str):
    try:
        r = requests.get(url, timeout=15)
    except Exception as e:
        raise RuntimeError(str(e))
    try:
        j = r.json()
    except Exception:
        j = None
    if not r.ok:
        msg = None
        if isinstance(j, dict):
            msg = j.get("Error Message") or j.get("error") or j.get("message")
        raise RuntimeError(msg or f"HTTP {r.status_code}")
    if isinstance(j, dict) and j.get("Error Message"):
        raise RuntimeError(j["Error Message"])
    return j


def fetch_price_data(stock_id: str, token: str, days: int):
    start, end = get_date_range(days)
    sym = to_fmp_symbol(stock_id)
    url = f"{FMP_BASE}/historical-price-eod/full?symbol={sym}&from={start}&to={end}&apikey={token}"
    j = api_fetch(url)
    # /stable/ 端點可能直接回傳陣列，也可能回傳 {symbol, historical:[...]}，兩者都相容
    rows = j if isinstance(j, list) else (j.get("historical") if isinstance(j, dict) else None)
    if not rows:
        raise RuntimeError("無資料（可能代號錯誤，或 API 額度已用完）")
    return rows


# ── 相對強弱（vs 大盤）：用SPY（S&P500 ETF）當大盤代理，不用另外接指數API，
# 直接當一般股票代號抓，同一套 fetch_price_data 就能用。只抓一次，所有股票
# 共用同一份大盤資料，不是每檔股票都各抓一次。──
BENCHMARK_ID = "SPY"
current_benchmark = None  # 這次批次分析用的大盤(SPY)資料，跑之前先抓一次，全部股票共用


def fetch_benchmark_series(token: str, days: int):
    rows = fetch_price_data(BENCHMARK_ID, token, days)
    arr = sorted(
        [{"date": d["date"], "close": float(d["close"])} for d in rows],
        key=lambda x: x["date"],
    )
    # 順便把大盤自己的20日/60日均線也算好，市場狀態濾網（大盤站上/跌破均線）要用，
    # 這裡一次算完整條序列，比逐個評估點現算快很多。
    for i in range(len(arr)):
        if i >= 19:
            arr[i]["ma20"] = sum(arr[j]["close"] for j in range(i - 19, i + 1)) / 20
        if i >= 59:
            arr[i]["ma60"] = sum(arr[j]["close"] for j in range(i - 59, i + 1)) / 60
    date_idx = {bar["date"]: i for i, bar in enumerate(arr)}
    return {"arr": arr, "date_idx": date_idx}


def find_benchmark_idx(benchmark: dict, date_str: str):
    if date_str in benchmark["date_idx"]:
        return benchmark["date_idx"][date_str]
    for i in range(len(benchmark["arr"]) - 1, -1, -1):
        if benchmark["arr"][i]["date"] <= date_str:
            return i
    return -1


def compute_benchmark_regime(benchmark: dict, date_str: str):
    """市場狀態濾網：評估當下大盤自己站在20日/60日均線的上面還下面。跟相對強弱
    不同——相對強弱看的是「這檔股票 vs 大盤」，這個看的是「大盤自己的多空位置」，
    用同一份benchmark資料算，不用多打API。回傳 (above20, above60)，可能是
    True/False/None。"""
    if not benchmark:
        return None, None
    idx = find_benchmark_idx(benchmark, date_str)
    if idx < 0:
        return None, None
    bar = benchmark["arr"][idx]
    above20 = (bar["close"] > bar["ma20"]) if "ma20" in bar else None
    above60 = (bar["close"] > bar["ma60"]) if "ma60" in bar else None
    return above20, above60


def compute_relative_strength(data, benchmark, lookback: int = 20):
    """算「股票N日報酬 - 大盤N日報酬」的相對強弱，正值代表跑贏大盤。用20日窗口。"""
    if not benchmark or not benchmark["arr"] or len(data) <= lookback:
        return None
    last = data[-1]
    prev_bar = data[-1 - lookback]
    if not prev_bar or not prev_bar.get("close"):
        return None
    stock_ret = (last["close"] - prev_bar["close"]) / prev_bar["close"] * 100

    bench_idx = find_benchmark_idx(benchmark, last["date"])
    if bench_idx < lookback:
        return None
    bench_now = benchmark["arr"][bench_idx]
    bench_prev = benchmark["arr"][bench_idx - lookback]
    if not bench_prev or not bench_prev.get("close"):
        return None
    bench_ret = (bench_now["close"] - bench_prev["close"]) / bench_prev["close"] * 100

    return stock_ret - bench_ret


def compute_vol_ratio(data):
    """成交量確認：當天成交量 ÷ 近20日均量（vm20已經在enrich()裡算好）。"""
    last = data[-1]
    if not last.get("vm20"):
        return None
    return last["volume"] / last["vm20"]


# ── 近3年 P/E 區間（/stable/ratios，季頻資料，每股票每次批次分析都會多打一次API）──
# FMP的季度P/E欄位名稱在不同版本文件間出現過 priceToEarningsRatio／priceEarningsRatio
# 兩種說法，這裡兩個都相容處理。過濾掉0/負值/非數字（虧損股常見），取這3年內的最大
# 最小值作為區間，最新一筆視為目前P/E。
# ── 近3年 P/E 區間（/stable/ratios，年頻資料，每股票每次批次分析都會多打一次API）──
# 原本用 period=quarter（季頻）想要更細的區間，但這帳號的FMP方案回傳 HTTP 402
# 「Premium Query Parameter: period」——quarter顆粒度被鎖在更高階方案，改用
# period=annual（年頻，通常基本方案就有）。代價是資料點變少（3年=3筆，不是quarter
# 版的約12+筆），P/E區間會抓得比較粗略，但至少能動。
# 欄位名稱是 priceToEarningsRatio（已跟FMP官方範例JSON核對過），仍相容
# priceEarningsRatio／peRatio 這兩個備用寫法以防萬一。
# 重點：以前抓不到資料時是直接吞掉錯誤回傳None，看不出是API被擋、帳號方案沒開通、
# 還是該股票真的沒有這項資料。現在全部改丟出明確錯誤訊息（RuntimeError），讓呼叫端
# 能把實際原因記下來顯示給使用者看。
def fetch_pe_range_us(token: str, symbol: str, years: int = 3):
    sym = to_fmp_symbol(symbol)
    limit = years + 1
    url = f"{FMP_BASE}/ratios?symbol={sym}&period=annual&limit={limit}&apikey={token}"
    resp = requests.get(url, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}：{resp.text[:200]}")
    rows = resp.json()
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("回傳空陣列（可能此帳號方案未開通 /ratios 端點，或該股票無財務比率資料）")
    rows = sorted(rows, key=lambda r: str(r.get("date", "")))
    sample_keys = ",".join(rows[0].keys())
    values = []
    for r in rows:
        v = r.get("priceToEarningsRatio")
        if v is None:
            v = r.get("priceEarningsRatio")
        if v is None:
            v = r.get("peRatio")
        try:
            v = float(v)
            if v > 0:
                values.append(v)
        except (TypeError, ValueError):
            continue
    if not values:
        raise RuntimeError(f"取得 {len(rows)} 筆資料，但解析不出P/E欄位（欄位範例：{sample_keys}）")
    return {"min": min(values), "max": max(values), "current": values[-1]}


# ── 近3季營收 YoY／QoQ（/stable/income-statement，每股票每次批次分析都會多打一次API）──
# 美股是季報，不是台股那種月營收，所以這裡對應的是「季」而非「月」。季度歸屬用財報
# 的 date（期末日）欄位的月份自行推算日曆季（Q1=1-3月...），不依賴period欄位的字串
# 格式，這樣算法跟下面的均價YoY可以共用同一組季度定義。
def fetch_revenue_yoy_qoq_us(token: str, symbol: str):
    sym = to_fmp_symbol(symbol)
    url = f"{FMP_BASE}/income-statement?symbol={sym}&period=quarter&limit=20&apikey={token}"
    rows = api_fetch(url)
    if not isinstance(rows, list) or not rows:
        return None
    items = []
    for r in rows:
        d = str(r.get("date", ""))[:10]
        if len(d) < 7 or r.get("revenue") is None:
            continue
        y, mo = int(d[:4]), int(d[5:7])
        items.append({"year": y, "quarter": (mo - 1) // 3 + 1, "date": d, "revenue": r["revenue"]})
    if not items:
        return None
    items.sort(key=lambda x: x["date"])
    rev_map = {f"{x['year']}-Q{x['quarter']}": x["revenue"] for x in items}
    last3 = items[-3:]
    result = []
    for x in last3:
        prev_key = f"{x['year']-1}-Q4" if x["quarter"] == 1 else f"{x['year']}-Q{x['quarter']-1}"
        yoy_key = f"{x['year']-1}-Q{x['quarter']}"
        qoq = ((x["revenue"] - rev_map[prev_key]) / rev_map[prev_key] * 100) if rev_map.get(prev_key) else None
        yoy = ((x["revenue"] - rev_map[yoy_key]) / rev_map[yoy_key] * 100) if rev_map.get(yoy_key) else None
        result.append({"year": x["year"], "quarter": x["quarter"], "revenue": x["revenue"], "qoq": qoq, "yoy": yoy})
    return result


def _last_n_calendar_quarters(n: int):
    out = []
    y, q = datetime.today().year, (datetime.today().month - 1) // 3 + 1
    for _ in range(n):
        out.insert(0, {"year": y, "quarter": q})
        q -= 1
        if q < 1:
            q = 4
            y -= 1
    return out


# ── 近3季「當季均價」YoY（跟營收YoY用同一組季度，方便橫向比較；若沒給季度清單則
# 自己算「最近3個日曆季」，讓這個功能可以獨立於營收YoY單獨使用）──
# 自己抓一段股價區間、按日曆季分組算平均收盤價，再跟去年同季的均價比。這是獨立於
# 「分析天數」的另一次歷史股價查詢，每股票每次批次分析又會多打一次API。
def fetch_quarterly_avg_price_yoy_us(token: str, symbol: str, quarters_list=None):
    if not quarters_list:
        quarters_list = _last_n_calendar_quarters(3)
    oldest = quarters_list[0]
    start = f"{oldest['year'] - 1}-01-01"
    end = datetime.today().strftime("%Y-%m-%d")
    sym = to_fmp_symbol(symbol)
    url = f"{FMP_BASE}/historical-price-eod/full?symbol={sym}&from={start}&to={end}&apikey={token}"
    j = api_fetch(url)
    rows = j if isinstance(j, list) else (j.get("historical") if isinstance(j, dict) else None)
    if not rows:
        return None
    sums, counts = {}, {}
    for r in rows:
        d = str(r.get("date", ""))[:10]
        if len(d) < 7:
            continue
        y, mo = int(d[:4]), int(d[5:7])
        key = f"{y}-Q{(mo - 1) // 3 + 1}"
        try:
            c = float(r["close"])
        except (TypeError, ValueError, KeyError):
            continue
        if c <= 0:
            continue
        sums[key] = sums.get(key, 0) + c
        counts[key] = counts.get(key, 0) + 1
    avg_map = {k: sums[k] / counts[k] for k in sums}
    result = []
    for qtr in quarters_list:
        key = f"{qtr['year']}-Q{qtr['quarter']}"
        yoy_key = f"{qtr['year'] - 1}-Q{qtr['quarter']}"
        this_avg, last_avg = avg_map.get(key), avg_map.get(yoy_key)
        yoy = ((this_avg - last_avg) / last_avg * 100) if (this_avg is not None and last_avg) else None
        result.append({"year": qtr["year"], "quarter": qtr["quarter"], "avg": this_avg, "avgLastYear": last_avg, "yoy": yoy})
    return result


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_company_name(token: str, stock_id: str) -> str:
    if stock_id in SP500_NAMES:
        return SP500_NAMES[stock_id]
    try:
        sym = to_fmp_symbol(stock_id)
        url = f"{FMP_BASE}/profile?symbol={sym}&apikey={token}"
        j = api_fetch(url)
        if isinstance(j, list) and j and j[0].get("companyName"):
            return j[0]["companyName"]
    except Exception:
        pass
    return stock_id


# ────────────────────────────────────────────────────────────────
# 分析師評等升降／目標價調整／內部人買賣（美股專屬，台股FinMind無對應資料）
# 注意：FMP官方FAQ明確表示目前不提供「空單比例／short interest」資料，
# 所以這裡沒有做空單比例——不是漏做，是FMP資料源本身沒有這項資料。
# ────────────────────────────────────────────────────────────────
def fetch_grades_consensus(token: str, symbol: str):
    sym = to_fmp_symbol(symbol)
    try:
        j = api_fetch(f"{FMP_BASE}/grades-consensus?symbol={sym}&apikey={token}")
        return j[0] if isinstance(j, list) and j else None
    except Exception as e:
        return {"__error__": str(e)}


def fetch_grades_history(token: str, symbol: str):
    sym = to_fmp_symbol(symbol)
    try:
        j = api_fetch(f"{FMP_BASE}/grades?symbol={sym}&apikey={token}")
        return j if isinstance(j, list) else []
    except Exception as e:
        return {"__error__": str(e)}


def fetch_price_target_consensus(token: str, symbol: str):
    sym = to_fmp_symbol(symbol)
    try:
        j = api_fetch(f"{FMP_BASE}/price-target-consensus?symbol={sym}&apikey={token}")
        return j[0] if isinstance(j, list) and j else None
    except Exception as e:
        return {"__error__": str(e)}


def fetch_price_target_summary(token: str, symbol: str):
    sym = to_fmp_symbol(symbol)
    try:
        j = api_fetch(f"{FMP_BASE}/price-target-summary?symbol={sym}&apikey={token}")
        return j[0] if isinstance(j, list) and j else None
    except Exception as e:
        return {"__error__": str(e)}


def fetch_insider_trading(token: str, symbol: str):
    sym = to_fmp_symbol(symbol)
    try:
        j = api_fetch(f"{FMP_BASE}/insider-trading/search?symbol={sym}&page=0&limit=20&apikey={token}")
        return j if isinstance(j, list) else []
    except Exception as e:
        return {"__error__": str(e)}


def render_analyst_fundamentals(token: str, symbol: str):
    """抓取並渲染分析師評等總覽／評等異動／目標價／內部人交易，四塊資料各自獨立
    抓取、獨立處理失敗（一項失敗不影響其他三項），用 __error__ 標記傳遞錯誤訊息。"""
    errs = []

    consensus = fetch_grades_consensus(token, symbol)
    if isinstance(consensus, dict) and consensus.get("__error__"):
        errs.append(f"評等總覽：{consensus['__error__']}")
        consensus = None

    grades_hist = fetch_grades_history(token, symbol)
    if isinstance(grades_hist, dict) and grades_hist.get("__error__"):
        errs.append(f"評等異動紀錄：{grades_hist['__error__']}")
        grades_hist = []

    pt_consensus = fetch_price_target_consensus(token, symbol)
    if isinstance(pt_consensus, dict) and pt_consensus.get("__error__"):
        errs.append(f"目標價共識：{pt_consensus['__error__']}")
        pt_consensus = None

    pt_summary = fetch_price_target_summary(token, symbol)
    if isinstance(pt_summary, dict) and pt_summary.get("__error__"):
        errs.append(f"目標價趨勢：{pt_summary['__error__']}")
        pt_summary = None

    insider = fetch_insider_trading(token, symbol)
    if isinstance(insider, dict) and insider.get("__error__"):
        errs.append(f"內部人交易：{insider['__error__']}")
        insider = []

    if consensus:
        st.markdown("#### 🏦 分析師評等總覽")
        sb, b = consensus.get("strongBuy", 0) or 0, consensus.get("buy", 0) or 0
        h = consensus.get("hold", 0) or 0
        s, ss = consensus.get("sell", 0) or 0, consensus.get("strongSell", 0) or 0
        total_n = sb + b + h + s + ss
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("強力買進", sb)
        c2.metric("買進", b)
        c3.metric("持有", h)
        c4.metric("賣出", s)
        c5.metric("強力賣出", ss)
        st.caption(f"共 {total_n} 位分析師覆蓋")

    if grades_hist:
        st.markdown("#### 🔔 最近評等異動")
        rows = []
        for g in grades_hist[:8]:
            rows.append({
                "日期": g.get("date", "--"), "機構": g.get("gradingCompany", "--"),
                "原評等": g.get("previousGrade", "--"), "新評等": g.get("newGrade", "--"),
                "動作": g.get("action", "--"),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    if pt_consensus or pt_summary:
        st.markdown("#### 🎯 分析師目標價")
        tc1, tc2, tc3 = st.columns(3)
        if pt_consensus:
            tgt = pt_consensus.get("targetConsensus")
            lo, hi = pt_consensus.get("targetLow"), pt_consensus.get("targetHigh")
            tc1.metric("共識目標價", f"${tgt:.2f}" if tgt is not None else "--")
            tc2.metric("目標價區間", f"${lo:.2f} ~ ${hi:.2f}" if lo is not None and hi is not None else "--")
        if pt_summary:
            q, y = pt_summary.get("lastQuarterAvgPriceTarget"), pt_summary.get("lastYearAvgPriceTarget")
            try:
                q_f, y_f = float(q), float(y)
                if y_f:
                    trend = (q_f - y_f) / y_f * 100
                    tc3.metric("近一季 vs 去年平均目標價", f"{trend:+.1f}%", delta=f"${y_f:.2f} → ${q_f:.2f}")
            except (TypeError, ValueError):
                pass

    if insider:
        st.markdown("#### 👤 最近內部人交易")
        rows = []
        for t in insider[:8]:
            tx_type = t.get("transactionType", "--")
            shares = t.get("securitiesTransacted")
            price = t.get("price")
            rows.append({
                "日期": t.get("transactionDate", t.get("filingDate", "--")),
                "姓名/職稱": f"{t.get('reportingName', '--')}（{t.get('typeOfOwner', '')}）",
                "類型": tx_type,
                "股數": f"{shares:,.0f}" if shares is not None else "--",
                "價格": f"${price:.2f}" if price is not None else "--",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    if not (consensus or grades_hist or pt_consensus or pt_summary or insider):
        st.caption("目前查無分析師評等/目標價/內部人交易資料（可能是這檔股票沒有分析師覆蓋，或FMP無該類資料）。")

    st.caption("ⓘ FMP目前不提供空單比例（short interest）資料，故無此項。")
    if errs:
        st.warning("部分資料讀取失敗：" + "　".join(errs))


# ────────────────────────────────────────────────────────────────
# 技術指標計算、朱家泓四維度評分、回後買上漲、15種進場型態、Plotly 圖表
# （與台股版邏輯完全一致，方法論本身與市場無關）
# ────────────────────────────────────────────────────────────────
def calc_ma(closes, p):
    out = []
    for i in range(len(closes)):
        if i < p - 1:
            out.append(None)
        else:
            out.append(sum(closes[i - p + 1:i + 1]) / p)
    return out


def calc_ema_series(closes, p):
    k = 2 / (p + 1)
    e = closes[0]
    out = [e]
    for i in range(1, len(closes)):
        e = closes[i] * k + e * (1 - k)
        out.append(e)
    return out


def calc_macd_series(closes):
    e12 = calc_ema_series(closes, 12)
    e26 = calc_ema_series(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    k = 2 / 10
    s = dif[0]
    sig = [s]
    for i in range(1, len(dif)):
        s = dif[i] * k + s * (1 - k)
        sig.append(s)
    hist = [d - sgl for d, sgl in zip(dif, sig)]
    return {"dif": dif, "sig": sig, "hist": hist}


def calc_rsi_series(closes, p=14):
    n = len(closes)
    res = [None] * n
    ag = 0.0
    al = 0.0
    for i in range(1, p + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            ag += d
        else:
            al += abs(d)
    ag /= p
    al /= p
    res[p] = 100 - 100 / (1 + ag / (al or 0.001))
    for i in range(p + 1, n):
        d = closes[i] - closes[i - 1]
        ag = (ag * (p - 1) + (d if d > 0 else 0)) / p
        al = (al * (p - 1) + (abs(d) if d < 0 else 0)) / p
        res[i] = 100 - 100 / (1 + ag / (al or 0.001))
    return res


def calc_bb_series(closes, p=20, std=2):
    mid = calc_ma(closes, p)
    out = []
    for i in range(len(closes)):
        if i < p - 1:
            out.append({"u": None, "l": None})
            continue
        m = mid[i]
        window = closes[i - p + 1:i + 1]
        s = sum((c - m) ** 2 for c in window)
        sigma = (s / p) ** 0.5
        out.append({"u": m + std * sigma, "l": m - std * sigma})
    return out


def calc_volma(volumes, p):
    out = []
    for i in range(len(volumes)):
        if i < p - 1:
            out.append(None)
        else:
            out.append(sum(volumes[i - p + 1:i + 1]) / p)
    return out


def pivots(data, w=5):
    highs, lows = [], []
    n = len(data)
    for i in range(w, n - w):
        h, l = data[i]["high"], data[i]["low"]
        is_h, is_l = True, True
        for j in range(i - w, i + w + 1):
            if data[j]["high"] > h:
                is_h = False
            if data[j]["low"] < l:
                is_l = False
        if is_h:
            highs.append(i)
        if is_l:
            lows.append(i)
    return {"highs": highs, "lows": lows}


def calc_kd(data, period=9):
    k_arr, d_arr = [], []
    prev_k, prev_d = 50.0, 50.0
    for i in range(len(data)):
        if i < period - 1:
            k_arr.append(None)
            d_arr.append(None)
            continue
        window = data[i - period + 1:i + 1]
        lowest = min(d["low"] for d in window)
        highest = max(d["high"] for d in window)
        rsv = 50 if highest == lowest else (data[i]["close"] - lowest) / (highest - lowest) * 100
        k = prev_k * 2 / 3 + rsv * 1 / 3
        d = prev_d * 2 / 3 + k * 1 / 3
        k_arr.append(k)
        d_arr.append(d)
        prev_k, prev_d = k, d
    return {"k": k_arr, "d": d_arr}


# ── KDJ(6,3,3)：跟上面的calc_kd（9,3,3，給K線圖用）是兩套獨立參數，不要共用──
def calc_kdj(data, period=6, k_n=3, d_n=3):
    k_arr, d_arr, j_arr = [], [], []
    prev_k, prev_d = 50.0, 50.0
    for i in range(len(data)):
        if i < period - 1:
            k_arr.append(None)
            d_arr.append(None)
            j_arr.append(None)
            continue
        window = data[i - period + 1:i + 1]
        lowest = min(d["low"] for d in window)
        highest = max(d["high"] for d in window)
        rsv = 50 if highest == lowest else (data[i]["close"] - lowest) / (highest - lowest) * 100
        k = prev_k * (k_n - 1) / k_n + rsv * 1 / k_n
        d = prev_d * (d_n - 1) / d_n + k * 1 / d_n
        j = 3 * k - 2 * d
        k_arr.append(k)
        d_arr.append(d)
        j_arr.append(j)
        prev_k, prev_d = k, d
    return {"k": k_arr, "d": d_arr, "j": j_arr}


def enrich(data):
    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]
    m5, m10, m20, m60 = calc_ma(closes, 5), calc_ma(closes, 10), calc_ma(closes, 20), calc_ma(closes, 60)
    md = calc_macd_series(closes)
    rs = calc_rsi_series(closes)
    bbs = calc_bb_series(closes)
    vm5, vm20 = calc_volma(volumes, 5), calc_volma(volumes, 20)
    kd = calc_kd(data, 9)
    kdj = calc_kdj(data, 6, 3, 3)
    out = []
    for i, d in enumerate(data):
        out.append({
            "date": d["date"], "open": d["open"], "high": d["high"], "low": d["low"],
            "close": d["close"], "volume": d["volume"],
            "ma5": m5[i], "ma10": m10[i], "ma20": m20[i], "ma60": m60[i],
            "macd": md["dif"][i], "macdSig": md["sig"][i], "macdHist": md["hist"][i],
            "rsi": rs[i], "bbU": bbs[i]["u"], "bbL": bbs[i]["l"],
            "vm5": vm5[i], "vm20": vm20[i],
            "kdK": kd["k"][i], "kdD": kd["d"][i],
            "kdjK": kdj["k"][i], "kdjD": kdj["d"][i], "kdjJ": kdj["j"][i],
        })
    return out


# ────────────────────────────────────────────────────────────────
# 朱家泓四維度評分（趨勢／K線／均線／成交量，各25分）
# ────────────────────────────────────────────────────────────────

def score_trend(data):
    score, sigs = 0, []
    last = data[-1]
    tdir = "盤整"
    pv = pivots(data)
    if len(pv["highs"]) >= 2 and len(pv["lows"]) >= 2:
        rh = [data[pv["highs"][-2]]["high"], data[pv["highs"][-1]]["high"]]
        rl = [data[pv["lows"][-2]]["low"], data[pv["lows"][-1]]["low"]]
        if rh[1] > rh[0] and rl[1] > rl[0]:
            tdir = "多頭"; score += 10; sigs.append(("多頭趨勢確立（高高低低）", "bull"))
        elif rh[1] < rh[0] and rl[1] < rl[0]:
            tdir = "空頭"; sigs.append(("空頭趨勢確立（低高低低）", "bear"))
        else:
            score += 2; sigs.append(("盤整區間", "neu"))
    else:
        score += 2
    if last["ma20"] is not None:
        if last["close"] > last["ma20"]:
            score += 5; sigs.append(("收盤站上20MA", "bull"))
        else:
            sigs.append(("收盤跌破20MA", "bear"))
    if last["ma60"] is not None:
        if last["close"] > last["ma60"]:
            score += 5; sigs.append(("收盤站上60MA", "bull"))
        else:
            sigs.append(("收盤跌破60MA", "bear"))
    p10 = data[-10] if len(data) >= 10 else None
    if last["ma60"] is not None and p10 and p10["ma60"] is not None:
        sl = (last["ma60"] - p10["ma60"]) / p10["ma60"] * 100
        if sl > 0.5:
            score += 5; sigs.append((f"季線向上 +{sl:.1f}%", "bull"))
        elif sl < -0.5:
            sigs.append((f"季線向下 {sl:.1f}%", "bear"))
        else:
            score += 2; sigs.append(("季線走平", "neu"))
    return {"score": min(score, 25), "max": 25, "sigs": sigs, "tdir": tdir}


def score_kline(data):
    score, sigs = 0, []
    c = data[-1]
    p1 = data[-2] if len(data) >= 2 else c
    p2 = data[-3] if len(data) >= 3 else c
    body = abs(c["close"] - c["open"])
    total = (c["high"] - c["low"]) or 0.01
    up_sh = c["high"] - max(c["close"], c["open"])
    dn_sh = min(c["close"], c["open"]) - c["low"]
    is_bull = c["close"] > c["open"]
    if is_bull:
        score += 5
        if body / total > 0.7:
            score += 3; sigs.append(("實體長紅棒", "bull"))
        else:
            sigs.append(("紅K棒", "bull"))
    else:
        if body / total > 0.7:
            sigs.append(("實體長黑棒", "bear"))
        else:
            sigs.append(("黑K棒", "bear"))
    slice20 = data[-20:]
    r_low = min(d["close"] for d in slice20)
    r_high = max(d["close"] for d in slice20)
    pos = (c["close"] - r_low) / ((r_high - r_low) or 0.01)
    if pos < 0.3:
        if dn_sh > body * 1.5:
            score += 8; sigs.append(("低檔長下影線（變盤訊號）", "bull"))
        if is_bull and c["close"] > p1["high"]:
            score += 5; sigs.append(("低檔紅K突破前高", "bull"))
    elif pos > 0.7:
        if up_sh > body * 1.5:
            sigs.append(("高檔長上影線（變盤訊號）", "bear"))
    three_bull = p2["close"] < p2["open"] and p1["close"] < p1["open"] and is_bull and c["close"] > p1["high"]
    if three_bull and pos < 0.4:
        score += 6; sigs.append(("三K底部反轉組合", "bull"))
    three_bear = p2["close"] > p2["open"] and p1["close"] > p1["open"] and (not is_bull) and c["close"] < p1["low"]
    if three_bear and pos > 0.6:
        sigs.append(("三K頂部反轉組合", "bear"))
    half = (c["high"] + c["low"]) / 2
    if is_bull and c["close"] > half:
        score += 3; sigs.append((f"收盤超過1/2價位 {half:.1f}", "bull"))
    elif (not is_bull) and c["close"] < half:
        sigs.append((f"收盤低於1/2價位 {half:.1f}", "bear"))
    return {"score": min(score, 25), "max": 25, "sigs": sigs}


def score_ma(data):
    score, sigs = 0, []
    last = data[-1]
    prev = data[-2] if len(data) >= 2 else last
    if all(last[k] is not None for k in ("ma5", "ma10", "ma20", "ma60")):
        if last["ma5"] > last["ma10"] > last["ma20"] > last["ma60"]:
            score += 10; sigs.append(("均線多頭排列", "bull"))
        elif last["ma5"] < last["ma10"] < last["ma20"] < last["ma60"]:
            sigs.append(("均線空頭排列", "bear"))
        else:
            score += 2; sigs.append(("均線糾結", "neu"))
    if len(data) >= 3:
        p1 = data[-2]
        if p1["ma5"] is not None and p1["ma5"] < p1["ma20"] and last["ma5"] > last["ma20"]:
            score += 8; sigs.append(("MA5 黃金交叉 MA20", "bull"))
        elif p1["ma5"] is not None and p1["ma5"] > p1["ma20"] and last["ma5"] < last["ma20"]:
            sigs.append(("MA5 死亡交叉 MA20", "bear"))
    if last["ma20"] is not None:
        sl20 = [d for d in data[-10:] if d["ma20"] is not None]
        slope = (sl20[-1]["ma20"] - sl20[0]["ma20"]) / sl20[0]["ma20"] * 100 if len(sl20) >= 2 else 0
        if slope > 0 and last["close"] > last["ma20"] and prev["close"] < prev["ma20"]:
            score += 5; sigs.append(("葛蘭畢買點1（突破均線）", "bull"))
        elif slope > 0 and last["close"] > last["ma20"]:
            diff = (last["close"] - last["ma20"]) / last["ma20"] * 100
            if 0 < diff < 3:
                score += 4; sigs.append(("葛蘭畢買點2（均線支撐）", "bull"))
            elif diff >= 3:
                score += 2; sigs.append(("均線上揚股價強勢", "bull"))
    if last["ma60"] is not None and last["close"] > last["ma60"]:
        score += 2; sigs.append(("股價位於季線上方", "bull"))
    return {"score": min(score, 25), "max": 25, "sigs": sigs}


def score_vol(data):
    score, sigs = 0, []
    last = data[-1]
    p1 = data[-2] if len(data) >= 2 else last
    vr = (last["volume"] / last["vm20"]) if last["vm20"] else 1
    v5r = (last["vm5"] / last["vm20"]) if (last["vm5"] and last["vm20"]) else 1
    is_up = last["close"] > p1["close"]
    if is_up:
        if vr >= 1.5:
            score += 10; sigs.append((f"上漲爆量 {vr:.1f}倍（多頭確認）", "bull"))
        elif vr >= 1.0:
            score += 6; sigs.append((f"上漲放量 {vr:.1f}倍", "bull"))
        else:
            score += 2; sigs.append(("上漲縮量（動能不足）", "neu"))
    else:
        if vr >= 1.5:
            sigs.append((f"下跌爆量 {vr:.1f}倍（賣壓沉重）", "bear"))
        elif vr >= 1.0:
            sigs.append(("下跌放量", "bear"))
        else:
            score += 5; sigs.append(("下跌縮量（賣壓減輕）", "neu"))
    low20 = min(d["low"] for d in data[-20:])
    if last["close"] < low20 * 1.15 and is_up and vr >= 1.3:
        score += 8; sigs.append(("底部放量起漲訊號", "bull"))
    if v5r > 1.2:
        score += 5; sigs.append((f"近5日均量擴增 {v5r:.1f}x", "bull"))
    elif v5r < 0.8:
        score += 1; sigs.append(("近5日均量萎縮", "neu"))
    score += 2
    return {"score": min(score, 25), "max": 25, "sigs": sigs}


# ────────────────────────────────────────────────────────────────
# DMI（趨向指標，Wilder 原始方法）＋多方力道評分（0-100，取代朱家泓四維度總分）
# ────────────────────────────────────────────────────────────────

def calc_dmi(data, period=14):
    """+DI／-DI衡量上升與下降方向的動能強弱，ADX衡量趨勢強度（不分方向），
    ADXR是ADX跟N天前ADX的平均，用來看趨勢是在增強還是減弱。用Wilder's smoothing
    對TR／+DM／-DM做平滑化（是對累計總和做平滑，不是簡單移動平均——這是DMI的
    標準算法，跟MACD用的EMA不同）。"""
    n = len(data)
    if n < period + 1:
        return None

    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, n):
        up_move = data[i]["high"] - data[i - 1]["high"]
        down_move = data[i - 1]["low"] - data[i]["low"]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)
        tr.append(max(
            data[i]["high"] - data[i]["low"],
            abs(data[i]["high"] - data[i - 1]["close"]),
            abs(data[i]["low"] - data[i - 1]["close"]),
        ))
    if len(tr) < period:
        return None

    def wilder_sum(arr, p):
        out = []
        s = sum(arr[:p])
        out.append(s)
        for j in range(p, len(arr)):
            s = s - s / p + arr[j]
            out.append(s)
        return out

    sm_tr = wilder_sum(tr, period)
    sm_plus_dm = wilder_sum(plus_dm, period)
    sm_minus_dm = wilder_sum(minus_dm, period)

    plus_di = [(v / sm_tr[i] * 100) if sm_tr[i] else 0 for i, v in enumerate(sm_plus_dm)]
    minus_di = [(v / sm_tr[i] * 100) if sm_tr[i] else 0 for i, v in enumerate(sm_minus_dm)]
    dx = []
    for i, v in enumerate(plus_di):
        s = v + minus_di[i]
        dx.append(abs(v - minus_di[i]) / s * 100 if s else 0)

    adx = None
    if len(dx) >= period:
        adx = []
        avg = sum(dx[:period]) / period
        adx.append(avg)
        for k in range(period, len(dx)):
            avg = (avg * (period - 1) + dx[k]) / period
            adx.append(avg)

    adxr = None
    if adx and len(adx) > period:
        adxr = [(adx[m] + adx[m - period]) / 2 for m in range(period, len(adx))]

    return {
        "plusDI": plus_di[-1],
        "minusDI": minus_di[-1],
        "adx": adx[-1] if adx else None,
        "adxr": adxr[-1] if adxr else None,
    }


def score_dmi(dmi):
    """多方力道評分（0-100）：①方向性（+DI相對-DI的優勢程度，最高50分）
    ②趨勢強度（ADX，最高30分）③趨勢轉強加分（ADX>ADXR，20分）。
    DMI資料不足（新股/資料太短）時各項給0分，不是「無資料」而是保守給0分，
    因為總分要能排序／篩選，不能是非數值。"""
    if not dmi or dmi.get("plusDI") is None or dmi.get("minusDI") is None:
        return {"score": 0, "max": 100, "plusDI": None, "minusDI": None, "adx": None, "adxr": None,
                "diPts": 0, "adxPts": 0, "adxrPts": 0, "tdir": "資料不足",
                "sigs": [("DMI資料不足（可能資料天數太短）", "neu")]}

    plus_di, minus_di = dmi["plusDI"], dmi["minusDI"]
    adx, adxr = dmi.get("adx"), dmi.get("adxr")

    di_sum = plus_di + minus_di
    di_dominance_pct = (plus_di / di_sum * 100) if di_sum else 50  # 50%=中性，100%=完全多方主導
    di_pts = di_dominance_pct * 0.5  # 0-50分
    adx_pts = (min(adx, 40) / 40 * 30) if adx is not None else 0  # 0-30分，ADX≥40視為滿分
    adxr_pts = 20 if (adx is not None and adxr is not None and adx > adxr) else 0  # 0或20分
    score = round(max(0, min(100, di_pts + adx_pts + adxr_pts)))

    bullish = plus_di > minus_di
    tdir = "多頭" if bullish else ("空頭" if plus_di < minus_di else "盤整")

    sigs = [(f"+DI {plus_di:.1f}{'>' if bullish else '<'}-DI {minus_di:.1f}（多方力道{'較強' if bullish else '較弱'}）",
             "bull" if bullish else "bear")]
    if adx is not None:
        adx_lbl = "趨勢明確" if adx >= 25 else ("趨勢成形中" if adx >= 20 else "盤整")
        sigs.append((f"ADX {adx:.1f}（{adx_lbl}）", "bull" if adx >= 25 else "neu"))
    if adx is not None and adxr is not None:
        strengthening = adx > adxr
        sigs.append((f"ADX{'>' if strengthening else '<'}ADXR，趨勢{'轉強' if strengthening else '轉弱'}",
                     "bull" if strengthening else "bear"))

    return {"score": score, "max": 100, "plusDI": plus_di, "minusDI": minus_di, "adx": adx, "adxr": adxr,
            "diPts": di_pts, "adxPts": adx_pts, "adxrPts": adxr_pts, "tdir": tdir, "sigs": sigs}


# ────────────────────────────────────────────────────────────────
# 回後買上漲 8 條件核對
# ────────────────────────────────────────────────────────────────

def check_pullback_buy(data):
    last = data[-1]
    prev = data[-2] if len(data) >= 2 else last

    results = []
    all_pass = True

    # 轉折波（跟型態辨識、圖表轉折波用同一套 build_zigzag，不再用另一套獨立的分形判斷法）
    zz = build_zigzag(data)
    zz_highs = [p for p in zz if p["type"] == "H"]
    zz_lows = [p for p in zz if p["type"] == "L"]

    # ── 條件1：趨勢多頭（轉折波 高高低低）──
    c1 = False
    if len(zz_highs) >= 2 and len(zz_lows) >= 2:
        rh = [zz_highs[-2]["price"], zz_highs[-1]["price"]]
        rl = [zz_lows[-2]["price"], zz_lows[-1]["price"]]
        c1 = rh[1] > rh[0] and rl[1] > rl[0]
    results.append({"label": "①趨勢多頭（高高低低）", "pass": c1, "required": True, "detail": ""})
    if not c1:
        all_pass = False

    # ── 條件2：位置回後上漲（前1~5日曾出現縮量回檔，今日反彈）──
    # 依課程講義：回檔幅度用費波那契回檔比例分級，回檔愈淺（愈接近0.382）代表股票愈強，
    # 愈要把握機會進場；回檔愈深（接近或超過0.618）力道愈弱。
    pullback_days = data[-6:-1]
    had_pullback = any(
        (d["close"] < (pullback_days[i - 1]["close"] if i > 0 else d["close"])) or (d["close"] < d["open"])
        for i, d in enumerate(pullback_days)
    )
    c2 = had_pullback and last["close"] > prev["close"]
    pullback_detail = ""
    if zz_highs:
        peak_point = zz_highs[-1]
        prior_low_cands = [p for p in zz_lows if p["idx"] < peak_point["idx"]]
        if prior_low_cands:
            prior_low = prior_low_cands[-1]["price"]
            peak = peak_point["price"]
            if peak > prior_low:
                pullback_segment = data[peak_point["idx"]:]
                pullback_low = min(d["low"] for d in pullback_segment)
                retrace = (peak - pullback_low) / (peak - prior_low)
                grade = "最強" if retrace <= 0.382 else "強" if retrace <= 0.5 else "弱" if retrace <= 0.618 else "回檔過深"
                pullback_detail = f"回檔幅度{retrace * 100:.1f}%（{grade}，費波0.382/0.5/0.618分級）"
    results.append({"label": "②位置回後上漲（近期有回檔，今轉上）", "pass": c2, "required": True, "detail": pullback_detail})
    if not c2:
        all_pass = False

    c3 = last["ma5"] is not None and last["close"] > last["ma5"]
    results.append({"label": "③收盤站上5MA（平價不算）", "pass": c3, "required": True,
                     "detail": f"5MA={last['ma5']:.2f}  收盤={last['close']:.2f}" if last["ma5"] is not None else ""})
    if not c3:
        all_pass = False

    c4 = last["high"] > prev["high"]
    results.append({"label": "④突破前一日高點（含上影線）", "pass": c4, "required": True,
                     "detail": f"今高={last['high']:.2f}  昨高={prev['high']:.2f}"})
    if not c4:
        all_pass = False

    chg_pct = (last["close"] - prev["close"]) / prev["close"] * 100 if prev["close"] > 0 else 0
    c5 = chg_pct >= 2.0
    results.append({"label": "⑤漲幅2%以上", "pass": c5, "required": True, "detail": f"漲幅={chg_pct:.2f}%"})
    if not c5:
        all_pass = False

    body = last["close"] - last["open"]
    up_sh = last["high"] - last["close"]
    dn_sh = last["open"] - last["low"]
    max_sh = max(up_sh, dn_sh)
    c6 = body > 0 and max_sh <= body
    results.append({"label": "⑥實體紅K，影線不大於實體", "pass": c6, "required": True,
                     "detail": f"實體={body:.2f}  最大影線={max_sh:.2f}"})
    if not c6:
        all_pass = False

    vol_ratio = (last["volume"] / last["vm20"]) if last["vm20"] else 1
    c7 = vol_ratio >= 1.0
    results.append({"label": "⑦成交量增（加分項）", "pass": c7, "required": False, "detail": f"量比MA20={vol_ratio:.2f}x"})

    prev_kd = data[-2] if len(data) >= 2 else None
    c8 = False
    if last["kdK"] is not None and prev_kd is not None and prev_kd["kdK"] is not None:
        c8 = last["kdK"] > prev_kd["kdK"]
    kd_detail = (f"K={last['kdK']:.1f}  昨K={prev_kd['kdK']:.1f}" if (last["kdK"] is not None and prev_kd and prev_kd["kdK"] is not None) else "KD資料不足")
    results.append({"label": "⑧指標確認（K值向上）", "pass": c8, "required": True, "detail": kd_detail})
    if not c8:
        all_pass = False

    required_total = sum(1 for r in results if r["required"])
    required_passed = sum(1 for r in results if r["required"] and r["pass"])
    bonus_passed = sum(1 for r in results if not r["required"] and r["pass"])

    return {"results": results, "allPass": all_pass, "requiredPassed": required_passed,
            "requiredTotal": required_total, "bonusPassed": bonus_passed}


# ────────────────────────────────────────────────────────────────
# 型態確認：朱家泓 進場型態（6種底部型態＋ABC切線＋上升軌道＋大量黑K＋回後買上漲）
# (1)頭肩底 (2)複式頭肩底 (3)N字底 (4)三重底 (5)圓弧底 (6)一字底(均線糾結)
# (7)突破ABC修正下降切線 (8)突破上升軌道線 (9)突破飆股大量黑K最高點 (10)回後買上漲
# ────────────────────────────────────────────────────────────────

def tolerant(a, b, pct):
    base = max(abs(a), abs(b), 1e-6)
    return abs(a - b) / base <= pct


def _ma_slope_up(data, key, n, last_idx):
    p_idx = max(0, last_idx - n)
    p, c = data[p_idx], data[last_idx]
    if c[key] is None or p[key] is None:
        return False
    return c[key] > p[key]


def compute_core_signals(data, dm):
    """算出布林通道位置、MACD狀態、強勢突破盤／跌深反彈盤——跟 build_summary_row
    裡顯示用的邏輯完全一致，這裡抽成獨立函式回傳「原始數值」（不是格式化文字），
    給快照資料庫寫入用。刻意跟顯示邏輯分開寫（有點重複），避免改動已經在跑的
    總表顯示邏輯。"""
    last = data[-1]
    prev = data[-2] if len(data) >= 2 else last

    bb_pos = None
    if last.get("bbU") is not None and last.get("bbL") is not None:
        bb_width = last["bbU"] - last["bbL"]
        bb_pos = ((last["close"] - last["bbL"]) / bb_width * 100) if bb_width else 50

    macd_state = None
    is_breakout = False
    is_pullback_rebound = False
    golden_cross_recent = False
    if (last.get("macd") is not None and last.get("macdSig") is not None
            and last.get("macdHist") is not None and prev.get("macdHist") is not None):
        above_zero = last["macd"] > 0
        hist_growing = last["macdHist"] > prev["macdHist"]
        if above_zero and last["macdHist"] > 0:
            macd_state = "零軸上・紅柱增長" if hist_growing else "零軸上・紅柱縮短"
        elif not above_zero and last["macdHist"] < 0:
            macd_state = "零軸下・綠柱縮短" if hist_growing else "零軸下・綠柱增長"
        else:
            macd_state = "交叉轉換中"

        for gci in range(max(1, len(data) - 3), len(data)):
            gc_cur, gc_prev = data[gci], data[gci - 1]
            if (gc_cur.get("macd") is not None and gc_cur.get("macdSig") is not None
                    and gc_prev.get("macd") is not None and gc_prev.get("macdSig") is not None
                    and gc_prev["macd"] <= gc_prev["macdSig"] and gc_cur["macd"] > gc_cur["macdSig"]):
                golden_cross_recent = True
                break

        width_expanding = False
        if last.get("bbU") is not None and last.get("bbL") is not None:
            width_now_pct = (last["bbU"] - last["bbL"]) / last["close"] * 100 if last["close"] else 0
            ref_idx = len(data) - 6
            ref_bar = data[ref_idx] if ref_idx >= 0 else None
            if ref_bar and ref_bar.get("bbU") is not None and ref_bar.get("bbL") is not None and ref_bar["close"]:
                width_ref_pct = (ref_bar["bbU"] - ref_bar["bbL"]) / ref_bar["close"] * 100
                width_expanding = width_now_pct > width_ref_pct

        is_breakout = (bb_pos is not None and bb_pos >= 80 and width_expanding
                       and above_zero and last["macdHist"] > 0 and hist_growing)

        divergence_detected = False
        zz_lows = [p for p in build_zigzag(data) if p["type"] == "L"]
        if len(zz_lows) >= 2:
            recent_low, prior_low = zz_lows[-1], zz_lows[-2]
            within_lookback = recent_low["idx"] >= len(data) - 1 - 60
            macd_at_recent = data[recent_low["idx"]].get("macd") if recent_low["idx"] < len(data) else None
            macd_at_prior = data[prior_low["idx"]].get("macd") if prior_low["idx"] < len(data) else None
            if within_lookback and macd_at_recent is not None and macd_at_prior is not None:
                divergence_detected = (recent_low["price"] < prior_low["price"]) and (macd_at_recent > macd_at_prior)

        is_pullback_rebound = bb_pos is not None and bb_pos <= 20 and divergence_detected and golden_cross_recent

    # ── KDJ(6,3,3) 黃金交叉／死亡交叉：K由下往上穿越D＝黃金交叉（多方），
    # K由上往下穿越D＝死亡交叉（空方）。跟MACD黃金交叉用同一套「近3天內」判斷法。
    kdj_state = None
    kdj_golden_cross_recent = False
    kdj_death_cross_recent = False
    if last.get("kdjK") is not None and last.get("kdjD") is not None:
        kdj_state = "K>D（多方）" if last["kdjK"] > last["kdjD"] else ("K<D（空方）" if last["kdjK"] < last["kdjD"] else "K=D")
        for kci in range(max(1, len(data) - 3), len(data)):
            kc_cur, kc_prev = data[kci], data[kci - 1]
            if (kc_cur.get("kdjK") is not None and kc_cur.get("kdjD") is not None
                    and kc_prev.get("kdjK") is not None and kc_prev.get("kdjD") is not None):
                if kc_prev["kdjK"] <= kc_prev["kdjD"] and kc_cur["kdjK"] > kc_cur["kdjD"]:
                    kdj_golden_cross_recent = True
                if kc_prev["kdjK"] >= kc_prev["kdjD"] and kc_cur["kdjK"] < kc_cur["kdjD"]:
                    kdj_death_cross_recent = True
        if kdj_golden_cross_recent:
            kdj_state += "　⚡近3日黃金交叉"
        if kdj_death_cross_recent:
            kdj_state += "　💀近3日死亡交叉"

    return {"bb_pos": bb_pos, "macd_state": macd_state,
            "is_breakout": is_breakout, "is_pullback_rebound": is_pullback_rebound,
            "golden_cross_recent": golden_cross_recent,
            "kdj_state": kdj_state, "kdj_golden_cross_recent": kdj_golden_cross_recent,
            "kdj_death_cross_recent": kdj_death_cross_recent}


def detect_patterns(data, pb, skip_just_broke=False):
    # 型態辨識固定看跟圖表一致的完整資料範圍（也就是「分析天數」實際抓到的全部K棒），
    # 並套用同一套壞資料過濾規則，確保型態辨識用的轉折波，跟圖表上實際畫出來的
    # 轉折波／輔助線是同一組資料算出來的結果——避免用不同範圍算出對不起來的轉折點。
    data = [d for d in data if d.get("open", 0) > 0 and d.get("high", 0) > 0
            and d.get("low", 0) > 0 and d.get("close", 0) > 0
            and all(_is_finite(d[k]) for k in ("open", "high", "low", "close"))]
    last = len(data) - 1
    last_close = data[last]["close"]
    last_vol = data[last]["volume"]
    vm20 = data[last]["vm20"]
    vol_confirm = (last_vol / vm20 >= 1.3) if vm20 else False

    # 型態辨識所用的高低點，改成直接沿用「轉折波」(build_zigzag) 算出來的同一組轉折點，
    # 不再用另一套獨立的分形視窗判斷法——這樣圖表上畫出來的轉折波，就是型態辨識實際依據的高低點，
    # 兩者完全一致，不會有「圖上看到的轉折」跟「型態判斷用的轉折」對不起來的狀況。
    zz = build_zigzag(data)
    # 型態辨識用的轉折點範圍，直接沿用整個(已限制在120根K棒內的)資料範圍，跟圖表顯示範圍完全一致，
    # 不再另外疊加一層90天子視窗限制——避免「圖表上看得到的高低點」卻被排除在型態判斷之外，
    # 導致畫出來的頸線/壓力線跟圖上真正的高低點對不起來。
    floor = 0
    lows = [p["idx"] for p in zz if p["type"] == "L" and floor <= p["idx"] < last]
    highs = [p["idx"] for p in zz if p["type"] == "H" and floor <= p["idx"] < last]

    def breakout_check(resistance):
        if resistance is None:
            return {"confirmed": False, "detail": ""}
        return {
            "confirmed": last_close > resistance,
            "detail": f"頸線/壓力＝{resistance:.2f}　現價＝{last_close:.2f}" + ("　(帶量突破)" if vol_confirm else ""),
        }

    results = []

    # (1) 頭肩底：右肩不破 頭→頸線 1/2（依課程講義）
    id_, name = "hs", "頭肩底"
    added = False
    if len(lows) >= 3:
        l3 = lows[-3:]
        L1, L2, L3v = data[l3[0]]["low"], data[l3[1]]["low"], data[l3[2]]["low"]
        shoulders_similar = tolerant(L1, L3v, 0.06)
        head_lower = L2 < L1 * 0.985 and L2 < L3v * 0.985
        if shoulders_similar and head_lower:
            h_between = [i for i in highs if l3[0] < i < l3[2]]
            neck = max((data[i]["high"] for i in h_between), default=None)
            # 依課程講義：右肩(L3)不能跌破 頭(L2)→頸線 漲幅的 1/2，才是有效的右肩（不是單純比頭高就好）
            valid_right_shoulder = neck is not None and L3v > (L2 + neck) / 2
            if valid_right_shoulder:
                bo = breakout_check(neck)
                results.append({"id": id_, "name": name, "formed": True, "breakout": bo["confirmed"],
                                 "detail": bo["detail"] or "型態成形，等待突破頸線", "desc": "左右肩低點相近，頭部最低，右肩不破1/2，突破頸線為買點",
                                 "line": {"i1": l3[0], "p1": neck, "slope": 0}})
                added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "左右肩低點相近，頭部最低，右肩不破1/2，突破頸線為買點"})

    # (2) 複式頭肩底：最近肩部不破 頭→頸線 1/2（依課程講義）
    id_, name = "chs", "複式頭肩底"
    added = False
    if len(lows) >= 4:
        last_lows = lows[-5:]
        low_vals = [data[i]["low"] for i in last_lows]
        min_val = min(low_vals)
        head_pos = low_vals.index(min_val)
        has_left = head_pos > 0
        has_right = head_pos < len(last_lows) - 1
        shoulder_vals = [v for idx, v in enumerate(low_vals) if idx != head_pos]
        shoulders_ok = has_left and has_right and shoulder_vals and all(
            tolerant(v, shoulder_vals[0], 0.08) and v > min_val * 1.02 for v in shoulder_vals
        )
        if shoulders_ok:
            h_between = [i for i in highs if last_lows[0] < i < last_lows[-1]]
            neck = max((data[i]["high"] for i in h_between), default=None)
            # 依課程講義：最近（最右）一個肩部不能跌破 頭→頸線 漲幅的 1/2
            last_shoulder_val = low_vals[-1]
            valid_last_shoulder = neck is not None and last_shoulder_val > (min_val + neck) / 2
            if valid_last_shoulder:
                bo = breakout_check(neck)
                results.append({"id": id_, "name": name, "formed": True, "breakout": bo["confirmed"],
                                 "detail": bo["detail"] or "型態成形，等待突破頸線", "desc": "多重肩部低點環繞單一最低頭部，最近肩部不破1/2，突破頸線為買點",
                                 "line": {"i1": last_lows[0], "p1": neck, "slope": 0}})
                added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "多重肩部低點環繞單一最低頭部，最近肩部不破1/2，突破頸線為買點"})

    # (3) N字底：依課程講義，拉回不破 A→B 漲幅的 1/2 才是有效的淺拉回
    id_, name = "nb", "N字底"
    added = False
    if len(lows) >= 2 and len(highs) >= 1:
        A, C = lows[-2], lows[-1]
        b_cands = [i for i in highs if A < i < C]
        if b_cands:
            # 取A、C之間「最高」的確認高點，而不是「最近」的一個——
            # 若下跌過程中出現次要反彈小高點，會比真正的主高點更晚被確認，
            # 用「最近」選到的話會抓到錯誤（偏低）的壓力位置。
            B = max(b_cands, key=lambda i: data[i]["high"])
            low_a, low_c, high_b = data[A]["low"], data[C]["low"], data[B]["high"]
            # 依課程講義：C（拉回低點）不能跌破 A→B 漲幅的 1/2，才是有效的淺拉回（不是單純比A高就好）
            half_point = (low_a + high_b) / 2
            shallow_pullback = low_c > half_point
            if shallow_pullback:
                bo = breakout_check(high_b)
                results.append({"id": id_, "name": name, "formed": True, "breakout": bo["confirmed"],
                                 "detail": bo["detail"] or f"拉回未破1/2（{half_point:.2f}），等待突破反彈高點", "desc": "低點反彈後拉回不破1/2，再突破反彈高點為買點",
                                 "line": {"i1": B, "p1": high_b, "slope": 0}})
                added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "低點反彈後拉回不破1/2，再突破反彈高點為買點"})

    # (4) 三重底：三個相近低點，兩個中間高點也要相近（形成真正的水平頸線）
    id_, name = "tb", "三重底"
    added = False
    if len(lows) >= 3:
        l3 = lows[-3:]
        vals = [data[i]["low"] for i in l3]
        all_similar = tolerant(vals[0], vals[1], 0.08) and tolerant(vals[1], vals[2], 0.08) and tolerant(vals[0], vals[2], 0.08)
        if all_similar:
            h_between1 = [i for i in highs if l3[0] < i < l3[1]]
            h_between2 = [i for i in highs if l3[1] < i < l3[2]]
            peak1 = max((data[i]["high"] for i in h_between1), default=None)
            peak2 = max((data[i]["high"] for i in h_between2), default=None)
            # 依課程講義：兩個中間高點要相近，才是真正的水平頸線（不是隨便夾兩個高低不一的高點）
            neckline_ok = peak1 is not None and peak2 is not None and tolerant(peak1, peak2, 0.05)
            if neckline_ok:
                res = max(peak1, peak2)
                bo = breakout_check(res)
                results.append({"id": id_, "name": name, "formed": True, "breakout": bo["confirmed"],
                                 "detail": bo["detail"] or "型態成形，等待突破壓力", "desc": "三個低點高度相近，中間兩高點形成水平頸線，突破頸線為買點",
                                 "line": {"i1": l3[0], "p1": res, "slope": 0}})
                added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "三個低點高度相近，中間兩高點形成水平頸線，突破頸線為買點"})

    # (5) 圓弧底
    id_, name = "rb", "圓弧底"
    added = False
    win = data[-40:]
    if len(win) >= 30:
        seg = len(win) // 3
        first, mid, tail = win[:seg], win[seg:len(win) - seg], win[len(win) - seg:]

        def avg(arr, key):
            return sum(d[key] for d in arr) / len(arr)

        def slope(arr):
            n = len(arr)
            sx = sy = sxy = sxx = 0
            for i, d in enumerate(arr):
                sx += i; sy += d["close"]; sxy += i * d["close"]; sxx += i * i
            denom = (n * sxx - sx * sx) or 1
            return (n * sxy - sx * sy) / denom

        slope_first, slope_tail = slope(first), slope(tail)
        mid_low = min(d["low"] for d in mid)
        is_convex = avg(first, "close") > mid_low * 1.01 and avg(tail, "close") > mid_low * 1.01
        shape_ok = slope_first < 0 and slope_tail > 0 and is_convex
        avg_range = sum((d["high"] - d["low"]) / d["close"] for d in win) / len(win)
        low_vol = avg_range < 0.05
        if shape_ok and low_vol:
            resistance = max(d["high"] for d in first)
            bo = breakout_check(resistance)
            win_start_idx = len(data) - len(win)
            # 依課程講義：目標價 = 突破點 + 型態高度（起跌點高點 - 最低點），即測量移動法
            target = resistance + (resistance - mid_low)
            results.append({"id": id_, "name": name, "formed": True, "breakout": bo["confirmed"],
                             "detail": (bo["detail"] or "弧形築底中，等待突破起跌壓力") + f"　目標價≈{target:.2f}",
                             "desc": "價格緩跌後緩升成U型，突破起跌點高點為買點",
                             "line": {"i1": win_start_idx, "p1": resistance, "slope": 0}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "價格緩跌後緩升成U型，突破起跌點高點為買點"})

    # (6) 一字底（均線糾結）：整理區間範圍約10%內（依課程講義定義）
    id_, name = "fb", "一字底(均線糾結)"
    added = False
    N = 10
    win = data[-N - 1:-1]
    ok = len(win) == N and all(d["ma5"] is not None and d["ma10"] is not None and d["ma20"] is not None and d["ma60"] is not None for d in win)
    if ok:
        tangled = all(
            (max(d["ma5"], d["ma10"], d["ma20"], d["ma60"]) - min(d["ma5"], d["ma10"], d["ma20"], d["ma60"]))
            / min(d["ma5"], d["ma10"], d["ma20"], d["ma60"]) <= 0.05
            for d in win
        )
        # 課程講義定義：整理區間範圍（整段最高與最低價的差距）約在 10% 以內，才算「一字底」
        win_high = max(d["high"] for d in win)
        win_low = min(d["low"] for d in win)
        narrow_range = (win_high - win_low) / win_low <= 0.10
        if tangled and narrow_range:
            resistance = win_high
            last4 = [data[last]["ma5"], data[last]["ma10"], data[last]["ma20"], data[last]["ma60"]]
            above_all_ma = all(v is not None and last_close > v for v in last4)
            breakout = last_close > resistance and above_all_ma and vol_confirm
            win_start_idx6 = len(data) - N - 1
            # 依課程講義：目標價 = 突破點 + 型態高度（整理區間高點 - 整理區間低點）
            target6 = resistance + (resistance - win_low)
            results.append({"id": id_, "name": name, "formed": True, "breakout": breakout,
                             "detail": f"整理區間高點＝{resistance:.2f}　現價＝{last_close:.2f}" + ("　(帶量突破)" if vol_confirm else "　(尚未帶量)") + f"　目標價≈{target6:.2f}",
                             "desc": "均線糾結、價格窄幅整理（區間範圍約10%內），帶量突破整理區間為買點",
                             "line": {"i1": win_start_idx6, "p1": resistance, "slope": 0}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "均線糾結、價格窄幅整理（區間範圍約10%內），帶量突破整理區間為買點"})

    # (7) 突破ABC修正下降切線：A、B兩高點畫下降切線，B之後要有C段拉回低點
    id_, name = "abc", "突破ABC修正下降切線"
    added = False
    # 只看「最近」的轉折高點，取最後兩個（A、B），避免抓到太久遠、已經沒有參考意義的舊高點
    recent_highs = [i for i in highs if i >= last - 40]
    if len(recent_highs) >= 2:
        h1, h2 = recent_highs[-2], recent_highs[-1]  # A：較早較高；B：較近較低
        y1, y2 = data[h1]["high"], data[h2]["high"]
        # A、B之間要有拉回的低點（確認A→低點→B是有效的一次反彈，不是隨便兩個高點連線）
        has_low_between = any(h1 < li < h2 for li in lows)
        # 依課程講義：B之後還要有一段「C」低點（真正的拉回），突破才是站在C低點之上完成的，
        # 不能B之後價格根本沒拉回就直接算突破——那樣就只是A、B兩個高點連線，不是完整的ABC三段修正。
        c_low = None
        for ci in range(h2 + 1, last):
            if c_low is None or data[ci]["low"] < c_low:
                c_low = data[ci]["low"]
        # 至少要有2%以上的拉回幅度，才算真正的C段（避免單純的價格雜訊被誤判成有效拉回）
        has_c_leg = c_low is not None and c_low < data[h2]["close"] * 0.98
        if y2 < y1 and h2 > h1 and has_low_between and has_c_leg and (h2 - h1) <= 20 and (last - h2) <= 20:
            slope_ = (y2 - y1) / (h2 - h1)
            line_at_last = y1 + slope_ * (last - h1)
            ma20up = _ma_slope_up(data, "ma20", 10, last)
            is_red = data[last]["close"] > data[last]["open"]
            breakout = last_close > line_at_last and ma20up and is_red
            results.append({"id": id_, "name": name, "formed": True, "breakout": breakout,
                             "detail": f"下降切線位置≈{line_at_last:.2f}　C低點＝{c_low:.2f}　現價＝{last_close:.2f}" + ("　MA20上揚" if ma20up else "　MA20未上揚"),
                             "desc": "多頭回檔呈ABC三段式下跌，A、B高點畫下降切線，C段拉回後帶量紅K突破切線為買點",
                             "line": {"i1": h1, "p1": y1, "i2": h2, "p2": y2, "slope": slope_}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "多頭回檔呈ABC三段式下跌，A、B高點畫下降切線，C段拉回後帶量紅K突破切線為買點"})

    # (8) 突破上升軌道線：軌道線要有至少2個高點貼著同一條平行線才算數（依課程講義）
    id_, name = "channel", "突破上升軌道線"
    added = False
    floor2 = max(0, len(data) - 60)
    lows_in = [i for i in lows if i >= floor2]
    highs_in = [i for i in highs if i >= floor2]
    if len(lows_in) >= 2:
        l1, l2 = lows_in[-2], lows_in[-1]
        ly1, ly2 = data[l1]["low"], data[l2]["low"]
        if ly2 > ly1 and l2 > l1:
            slope2 = (ly2 - ly1) / (l2 - l1)
            offset_cands = [data[i]["high"] - (ly1 + slope2 * (i - l1)) for i in highs_in if i > l1]
            # 依課程講義：上升軌道線要有「至少2個高點」貼著同一條平行線，才是真正的軌道線
            # （不能只是單一個高點恰好離支撐線最遠，那只是巧合，不是真的通道）
            # 容忍度用股價的3%來算（而不是用offset自身的百分比），避免offset數值太小時誤判過嚴
            offset = None
            if len(offset_cands) >= 2:
                tol = last_close * 0.03
                best_offset, best_count = None, 0
                for o in offset_cands:
                    count = sum(1 for o2 in offset_cands if abs(o - o2) <= tol)
                    if count > best_count or (count == best_count and (best_offset is None or o > best_offset)):
                        best_count, best_offset = count, o
                if best_count >= 2:
                    offset = best_offset
            if offset is not None and offset > 0:
                upper_at_last = ly1 + slope2 * (last - l1) + offset
                ma20up2 = _ma_slope_up(data, "ma20", 10, last)
                is_red2 = data[last]["close"] > data[last]["open"]
                breakout2 = last_close > upper_at_last and ma20up2 and is_red2 and vol_confirm
                results.append({"id": id_, "name": name, "formed": True, "breakout": breakout2,
                                 "detail": f"軌道上緣≈{upper_at_last:.2f}　現價＝{last_close:.2f}" + ("　帶量" if vol_confirm else "　量未放大"),
                                 "desc": "股價沿上升軌道緩步上漲，MA20上揚下帶量長紅收盤突破軌道上緣為買點",
                                 "line": {"i1": l1, "p1": ly1 + offset, "slope": slope2},
                                 "line2": {"i1": l1, "p1": ly1, "slope": slope2}})
                added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "股價沿上升軌道緩步上漲，MA20上揚下帶量長紅收盤突破軌道上緣為買點"})

    # (9) 突破飆股大量黑K最高點
    id_, name = "blackk", "突破飆股大量黑K最高點"
    added = False
    lookback, confirm_window = 10, 3
    floor3 = max(0, len(data) - 1 - lookback)
    candidates = [i for i in range(floor3, last)
                  if data[i]["close"] < data[i]["open"] and data[i]["vm20"] and data[i]["volume"] / data[i]["vm20"] >= 1.6]
    if candidates:
        bk_idx = candidates[-1]
        bk_high = data[bk_idx]["high"]
        within_window = 0 <= (last - bk_idx) <= confirm_window
        if within_window:
            is_red3 = data[last]["close"] > data[last]["open"]
            ma20up3 = _ma_slope_up(data, "ma20", 10, last)
            breakout3 = last_close > bk_high and is_red3 and vol_confirm and ma20up3
            results.append({"id": id_, "name": name, "formed": True, "breakout": breakout3,
                             "detail": f"大量黑K高點＝{bk_high:.2f}　現價＝{last_close:.2f}" + ("　帶量" if vol_confirm else "　量未放大"),
                             "desc": "飆股急漲後出現大量黑K回檔，3日內帶量長紅突破其最高點為買點",
                             "line": {"i1": bk_idx, "p1": bk_high, "slope": 0}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "飆股急漲後出現大量黑K回檔，3日內帶量長紅突破其最高點為買點"})

    # (10) K線橫盤的突破：三天以上(含首日)收盤未突破/跌破首日K線高低點，帶量突破首日高點為買點
    id_, name = "kbp", "K線橫盤的突破"
    added = False
    if len(data) >= 4:
        anchor_idx = last - 3  # 最小需求：3天(含首日)
        anchor_bar = data[anchor_idx]
        min_ok = all(anchor_bar["low"] <= data[j]["close"] <= anchor_bar["high"] for j in range(anchor_idx + 1, last))
        if min_ok:
            # 課本圖上的橫盤區間常常不只3天——只要收盤價持續守在「首日K線」的高低範圍內，
            # 就往前延伸找到最早、仍然成立的起點，涵蓋較長的橫盤整理段。
            max_lookback = 20
            candidate = anchor_idx - 1
            while candidate >= 0 and (last - candidate) <= max_lookback:
                in_range = all(data[candidate]["low"] <= data[k]["close"] <= data[candidate]["high"]
                                for k in range(candidate + 1, last))
                if not in_range:
                    break
                anchor_idx = candidate
                candidate -= 1
            anchor = data[anchor_idx]
            is_red_k = data[last]["close"] > data[last]["open"]
            breakout_k = last_close > anchor["high"] and is_red_k and vol_confirm
            results.append({"id": id_, "name": name, "formed": True, "breakout": breakout_k,
                             "detail": f"首日K線高點＝{anchor['high']:.2f}　整理天數＝{last - anchor_idx}天　現價＝{last_close:.2f}" + ("　(帶量)" if vol_confirm else "　(量未放大)"),
                             "desc": "三天以上(含首日)收盤未突破首日K線高低點，帶量紅K突破首日高點為買點",
                             "line": {"i1": anchor_idx, "p1": anchor["high"], "slope": 0}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "三天以上(含首日)收盤未突破首日K線高低點，帶量紅K突破首日高點為買點"})

    # (11) 高檔母子懷抱：中長紅K(母)＋隔日不過高不破低的黑K/變盤線(子)，次日確認轉折向下
    # 依課程講義：屬於2根K線構成，上漲高檔出現中長紅，次日出現不過高也不破低的黑K線，
    # 中長紅K線稱為母線，次日K線稱為子線；代表多空力量突然拉鋸，多頭上漲力道減弱。
    id_, name = "harami_bear", "母子懷抱(高檔)"
    added = False
    if len(data) >= 3:
        mother, child, confirm_day = data[last - 2], data[last - 1], data[last]
        mother_body_pct = abs(mother["close"] - mother["open"]) / mother["close"]
        mother_is_red = mother["close"] > mother["open"]
        mother_is_med_long = mother_body_pct >= 0.035
        child_contained = child["high"] <= mother["high"] and child["low"] >= mother["low"]
        child_smaller = abs(child["close"] - child["open"]) < abs(mother["close"] - mother["open"]) * 0.6
        at_high = mother["close"] >= data[last - 3]["close"] if last - 3 >= 0 else True
        if mother_is_red and mother_is_med_long and child_contained and child_smaller and at_high:
            breakout_h = confirm_day["close"] < child["close"]
            results.append({"id": id_, "name": name, "formed": True, "breakout": breakout_h,
                             "detail": f"母K(中長紅)高點＝{mother['high']:.2f}　子K收於母K範圍內　" + ("次日已確認轉折向下" if breakout_h else "等待次日確認轉折向下"),
                             "desc": "上漲高檔出現中長紅K，次日不過高不破低的黑K線為母子懷抱，多頭上漲力道轉弱",
                             "marker": {"idx": last - 1, "price": mother["high"], "dir": "up", "label": "母子懷抱"}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "上漲高檔出現中長紅K，次日不過高不破低的黑K線為母子懷抱，多頭上漲力道轉弱"})

    # (12) 低檔母子懷抱：中長黑K(母)＋隔日不過高不破低的紅K/變盤線(子)，次日確認轉折向上
    id_, name = "harami_bull", "母子懷抱(低檔)"
    added = False
    if len(data) >= 3:
        mother, child, confirm_day = data[last - 2], data[last - 1], data[last]
        mother_body_pct = abs(mother["close"] - mother["open"]) / mother["close"]
        mother_is_black = mother["close"] < mother["open"]
        mother_is_med_long = mother_body_pct >= 0.035
        child_contained = child["high"] <= mother["high"] and child["low"] >= mother["low"]
        child_smaller = abs(child["close"] - child["open"]) < abs(mother["close"] - mother["open"]) * 0.6
        at_low = mother["close"] <= data[last - 3]["close"] if last - 3 >= 0 else True
        if mother_is_black and mother_is_med_long and child_contained and child_smaller and at_low:
            breakout_h2 = confirm_day["close"] > child["close"]
            results.append({"id": id_, "name": name, "formed": True, "breakout": breakout_h2,
                             "detail": f"母K(中長黑)低點＝{mother['low']:.2f}　子K收於母K範圍內　" + ("次日已確認轉折向上" if breakout_h2 else "等待次日確認轉折向上"),
                             "desc": "下跌低檔出現中長黑K，次日不過高不破低的紅K線為母子懷抱，空頭下跌力道轉弱",
                             "marker": {"idx": last - 1, "price": mother["low"], "dir": "down", "label": "母子懷抱"}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "下跌低檔出現中長黑K，次日不過高不破低的紅K線為母子懷抱，空頭下跌力道轉弱"})

    # (13) 晨星：下跌中長黑K＋變盤線＋中長紅K，收盤站上首日黑K實體中點為低檔轉折向上
    # 依課程講義：下跌低檔出現左邊中長黑K，右邊中長紅K，中間夾一根「變盤線」，是低檔轉折向上確認的K線組合。
    id_, name = "morning_star", "晨星"
    added = False
    if len(data) >= 3:
        s1, s2, s3 = data[last - 2], data[last - 1], data[last]
        s1_body_pct = abs(s1["close"] - s1["open"]) / s1["close"]
        s1_is_black = s1["close"] < s1["open"]
        s1_med_long = s1_body_pct >= 0.035
        s2_body_pct = abs(s2["close"] - s2["open"]) / s2["close"]
        is_star1 = s2_body_pct < 0.035
        s3_body_pct = abs(s3["close"] - s3["open"]) / s3["close"]
        s3_is_red = s3["close"] > s3["open"]
        s3_med_long = s3_body_pct >= 0.035
        s1_mid = (s1["open"] + s1["close"]) / 2
        closes_above_mid = s3["close"] > s1_mid
        if s1_is_black and s1_med_long and is_star1 and s3_is_red and s3_med_long and closes_above_mid:
            results.append({"id": id_, "name": name, "formed": True, "breakout": True,
                             "detail": f"首日黑K實體中點＝{s1_mid:.2f}　收盤＝{s3['close']:.2f}（已站上中點，轉折確認）",
                             "desc": "下跌出現中長黑K+變盤線+中長紅K，收盤站上首日實體中點為低檔轉折向上訊號",
                             "marker": {"idx": last - 1, "price": s2["low"], "dir": "down", "label": "晨星"}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "下跌出現中長黑K+變盤線+中長紅K，收盤站上首日實體中點為低檔轉折向上訊號"})

    # (14) 夜星：上漲中長紅K＋變盤線＋中長黑K，收盤跌破首日紅K實體中點為高檔轉折向下
    # 晨星的鏡像型態（課程講義未獨立列出，依同一套邏輯對稱推導）
    id_, name = "evening_star", "夜星"
    added = False
    if len(data) >= 3:
        e1, e2, e3 = data[last - 2], data[last - 1], data[last]
        e1_body_pct = abs(e1["close"] - e1["open"]) / e1["close"]
        e1_is_red = e1["close"] > e1["open"]
        e1_med_long = e1_body_pct >= 0.035
        e2_body_pct = abs(e2["close"] - e2["open"]) / e2["close"]
        is_star2 = e2_body_pct < 0.035
        e3_body_pct = abs(e3["close"] - e3["open"]) / e3["close"]
        e3_is_black = e3["close"] < e3["open"]
        e3_med_long = e3_body_pct >= 0.035
        e1_mid = (e1["open"] + e1["close"]) / 2
        closes_below_mid = e3["close"] < e1_mid
        if e1_is_red and e1_med_long and is_star2 and e3_is_black and e3_med_long and closes_below_mid:
            results.append({"id": id_, "name": name, "formed": True, "breakout": True,
                             "detail": f"首日紅K實體中點＝{e1_mid:.2f}　收盤＝{e3['close']:.2f}（已跌破中點，轉折確認）",
                             "desc": "上漲出現中長紅K+變盤線+中長黑K，收盤跌破首日實體中點為高檔轉折向下訊號",
                             "marker": {"idx": last - 1, "price": e2["high"], "dir": "up", "label": "夜星"}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "上漲出現中長紅K+變盤線+中長黑K，收盤跌破首日實體中點為高檔轉折向下訊號"})

    # (15) 回後買上漲：沿用 checkPullbackBuy() 判斷結果
    if pb:
        pb_formed = pb["allPass"] or pb["requiredPassed"] >= pb["requiredTotal"] - 1
        results.append({
            "id": "pbup", "name": "回後買上漲",
            "formed": pb_formed, "breakout": pb["allPass"],
            "detail": f"必要條件 {pb['requiredPassed']}/{pb['requiredTotal']} 通過" + ("　+成交量增加分" if pb["bonusPassed"] else ""),
            "desc": "趨勢多頭，回檔量縮價穩後，今日紅K放量突破前高為買點",
        })

    # ── 剛突破：與前一交易日比較，突破訊號是「今天才發生」──
    if not skip_just_broke and len(data) > 1:
        prev_data = data[:-1]
        prev_pb = check_pullback_buy(prev_data)
        prev_pt = detect_patterns(prev_data, prev_pb, skip_just_broke=True)
        for i, r in enumerate(results):
            pr = prev_pt["results"][i] if i < len(prev_pt["results"]) else None
            r["justBroke"] = bool(r["breakout"] and (not pr or not pr["breakout"]))
    else:
        for r in results:
            r["justBroke"] = False

    any_breakout = any(r["breakout"] for r in results)
    any_formed = any(r["formed"] for r in results)
    any_just_broke = any(r["justBroke"] for r in results)

    return {"results": results, "anyBreakout": any_breakout, "anyFormed": any_formed, "anyJustBroke": any_just_broke}


# ────────────────────────────────────────────────────────────────
# Plotly 圖表：K線＋均線＋布林通道＋成交量＋MACD＋轉折波
# ────────────────────────────────────────────────────────────────

def _is_finite(x):
    try:
        return x == x and x not in (float("inf"), float("-inf"))
    except TypeError:
        return False


def _is_sane_bar(d):
    """過濾掉資料來源偶爾出現的異常值（例如某天 low 被錯誤回傳為 0 或極小值），
    避免單一根爛資料把轉折波拉出一條不合理的長長尖刺"""
    if not d:
        return False
    high, low, close = d.get("high"), d.get("low"), d.get("close")
    if not (high is not None and high > 0 and low is not None and low > 0 and close is not None and close > 0):
        return False
    import math
    if not (math.isfinite(high) and math.isfinite(low) and math.isfinite(close)):
        return False
    ma5 = d.get("ma5")
    if ma5 is not None and ma5 > 0:
        if low < ma5 * 0.4 or high > ma5 * 2.5:
            return False
    return True


def build_zigzag(data):
    """朱家泓「短線轉折波」畫法（依課程圖表2-1-3／2-1-4）：
    用收盤價與5日均線的穿越關係取高低點——
      收盤價「跌破」5日均線 → 取這段上漲過程的「高點」為一個轉折點
      收盤價「突破」5日均線 → 取這段下跌過程的「低點」為一個轉折點
    再把這些高低點依序連接成鋸齒狀的轉折波。"""
    n = len(data)
    # 找到第一個 MA5 已經有值、且資料正常的位置（前4根K棒沒有5日均線可比較）
    start = 0
    while start < n and (data[start]["ma5"] is None or not _is_sane_bar(data[start])):
        start += 1
    if start >= n - 1:
        return []

    state = "above" if data[start]["close"] >= data[start]["ma5"] else "below"  # 目前收盤在5日均線上方或下方
    points = [{
        "idx": start,
        "price": data[start]["low"] if state == "above" else data[start]["high"],
        "type": "L" if state == "above" else "H",
    }]
    extreme_idx = start
    extreme_price = data[start]["high"] if state == "above" else data[start]["low"]

    for i in range(start + 1, n):
        d = data[i]
        if d["ma5"] is None or not _is_sane_bar(d):
            continue
        if state == "above":
            # 收盤在5日均線之上，持續追蹤這段上漲的最高點
            if d["high"] > extreme_price:
                extreme_price, extreme_idx = d["high"], i
            if d["close"] < d["ma5"]:
                # 收盤跌破5日均線 → 確認剛才追蹤到的高點為一個轉折高點
                points.append({"idx": extreme_idx, "price": extreme_price, "type": "H"})
                state = "below"
                extreme_price, extreme_idx = d["low"], i
        else:
            # 收盤在5日均線之下，持續追蹤這段下跌的最低點
            if d["low"] < extreme_price:
                extreme_price, extreme_idx = d["low"], i
            if d["close"] > d["ma5"]:
                # 收盤突破5日均線 → 確認剛才追蹤到的低點為一個轉折低點
                points.append({"idx": extreme_idx, "price": extreme_price, "type": "L"})
                state = "above"
                extreme_price, extreme_idx = d["high"], i

    # 收尾：把目前仍在追蹤中的高/低點畫出來，再接到最新一根K棒的收盤價，確保線一定連到最新資料
    last_idx = n - 1
    if extreme_idx != last_idx:
        points.append({"idx": extreme_idx, "price": extreme_price, "type": "H" if state == "above" else "L"})
        points.append({"idx": last_idx, "price": data[last_idx]["close"], "type": "L" if state == "above" else "H"})
    else:
        points.append({"idx": extreme_idx, "price": data[last_idx]["close"], "type": "H" if state == "above" else "L"})
    return points


def draw_chart(data, name, pt=None):
    # 圖表顯示範圍改用完整的 data（已由「分析天數」在抓資料時決定範圍），
    # 不再寫死只看最近120根K棒，讓滑桿調整能真正反映在圖表上。
    raw_tail = data
    # 過濾掉資料異常的K棒（開高低收有任一項是 0、負值或非數字），
    # 避免圖表出現「沒有K棒的空白位置」卻仍有轉折波或均線的線硬穿過去
    tail = [d for d in raw_tail if d.get("open", 0) > 0 and d.get("high", 0) > 0
            and d.get("low", 0) > 0 and d.get("close", 0) > 0
            and all(_is_finite(d[k]) for k in ("open", "high", "low", "close"))]
    dates = [d["date"] for d in tail]
    vol_colors = ["#ef5350" if d["close"] >= d["open"] else "#26a69a" for d in tail]
    hist_colors = ["#ef5350" if (d["macdHist"] or 0) >= 0 else "#26a69a" for d in tail]

    zz = build_zigzag(tail)
    zz_x = [tail[p["idx"]]["date"] for p in zz]
    zz_y = [p["price"] for p in zz]

    # 每個「型態確認」如果已成形，就把輔助線（頸線/壓力線/切線/軌道線）畫在圖上，
    # 型態成形但未突破 → 虛線；已經突破 → 實線＋★標註，方便直接在圖上對照型態辨識依據
    PATTERN_LINE_STYLE = {
        "hs": {"color": "#ffd54f", "label": "頭肩底頸線"},
        "chs": {"color": "#ff8a65", "label": "複式頭肩底頸線"},
        "nb": {"color": "#81c784", "label": "N字底壓力"},
        "tb": {"color": "#4dd0e1", "label": "三重底壓力"},
        "rb": {"color": "#64b5f6", "label": "圓弧底壓力"},
        "fb": {"color": "#ba68c8", "label": "一字底整理區間高點"},
        "abc": {"color": "#ff2ecc", "label": "ABC下降切線起點"},
        "channel": {"color": "#ffa726", "label": "上升軌道線"},
        "blackk": {"color": "#e57373", "label": "大量黑K高點"},
        "kbp": {"color": "rgba(255,255,255,.85)", "label": "K線橫盤首日高點"},
    }
    # 母子懷抱／晨星／夜星屬於2~3根K線的短線反轉訊號，沒有持續延伸的支撐/壓力線可畫，
    # 改用箭頭標註直接指到型態發生的那根（或那兩根）K棒位置，方便在圖上直接辨識。
    PATTERN_MARKER_STYLE = {
        "harami_bear": {"color": "#ff8a65"},
        "harami_bull": {"color": "#81c784"},
        "morning_star": {"color": "#4dd0e1"},
        "evening_star": {"color": "#ff5252"},
    }
    shapes, annotations = [], []
    last_idx = len(tail) - 1
    if pt and pt.get("results"):
        for p in pt["results"]:
            if not p.get("formed"):
                continue
            style = PATTERN_LINE_STYLE.get(p["id"])
            if style:
                # 上升軌道線另外多畫一條下緣支撐線（line2），其餘型態只有一條輔助線（line）
                for idx, ln in enumerate((p.get("line"), p.get("line2"))):
                    if not ln or ln["i1"] < 0 or ln["i1"] >= len(tail):
                        continue
                    line_end_price = ln["p1"] + ln["slope"] * (last_idx - ln["i1"])
                    shapes.append(dict(
                        type="line", xref="x", yref="y",
                        x0=tail[ln["i1"]]["date"], y0=ln["p1"],
                        x1=tail[last_idx]["date"], y1=line_end_price,
                        line=dict(color=style["color"], width=2 if idx == 0 else 1.3,
                                   dash="solid" if p["breakout"] else "dash"),
                    ))
                    if idx == 0:
                        annotations.append(dict(
                            x=tail[ln["i1"]]["date"], y=ln["p1"], xref="x", yref="y",
                            text=style["label"], showarrow=True, arrowhead=2, arrowcolor=style["color"],
                            font=dict(color=style["color"], size=10), ax=-10, ay=-30,
                        ))
                        if p["breakout"]:
                            annotations.append(dict(
                                x=tail[last_idx]["date"], y=tail[last_idx]["close"], xref="x", yref="y",
                                text="★ 突破" + p["name"], showarrow=True, arrowhead=2, arrowcolor=style["color"],
                                font=dict(color=style["color"], size=11), ax=10, ay=-35,
                            ))
                continue
            m_style = PATTERN_MARKER_STYLE.get(p["id"])
            marker = p.get("marker")
            if m_style and marker and 0 <= marker["idx"] < len(tail):
                annotations.append(dict(
                    x=tail[marker["idx"]]["date"], y=marker["price"], xref="x", yref="y",
                    text=("★ " if p["breakout"] else "") + marker["label"],
                    showarrow=True, arrowhead=2, arrowcolor=m_style["color"],
                    font=dict(color=m_style["color"], size=11),
                    ax=0, ay=-32 if marker["dir"] == "up" else 32,
                ))

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.56, 0.22, 0.22], vertical_spacing=0.03)

    fig.add_trace(go.Candlestick(
        x=dates, open=[d["open"] for d in tail], high=[d["high"] for d in tail],
        low=[d["low"] for d in tail], close=[d["close"] for d in tail], name="K線",
        increasing=dict(line=dict(color="#ef5350"), fillcolor="#ef5350"),
        decreasing=dict(line=dict(color="#26a69a"), fillcolor="#26a69a"),
    ), row=1, col=1)

    for key, color, label in [("ma5", "#ffeb3b", "MA5"), ("ma10", "#ff9800", "MA10"),
                               ("ma20", "#2196f3", "MA20"), ("ma60", "#9c27b0", "MA60")]:
        fig.add_trace(go.Scatter(x=dates, y=[d[key] for d in tail], name=label,
                                  line=dict(color=color, width=1.2)), row=1, col=1)

    fig.add_trace(go.Scatter(x=zz_x, y=zz_y, name="轉折波", mode="lines+markers",
                              line=dict(color="#00e5ff", width=1.8),
                              marker=dict(size=5, color="#00e5ff")), row=1, col=1)

    fig.add_trace(go.Scatter(x=dates, y=[d["bbU"] for d in tail], name="BB上軌",
                              line=dict(color="rgba(100,200,255,.4)", width=1, dash="dot"),
                              showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=[d["bbL"] for d in tail], name="BB下軌",
                              line=dict(color="rgba(100,200,255,.4)", width=1, dash="dot"),
                              fill="tonexty", fillcolor="rgba(100,200,255,.04)", showlegend=False), row=1, col=1)

    fig.add_trace(go.Bar(x=dates, y=[d["volume"] for d in tail], marker_color=vol_colors,
                          name="成交量", opacity=0.7), row=2, col=1)
    fig.add_trace(go.Scatter(x=dates, y=[d["vm20"] for d in tail], name="量MA20",
                              line=dict(color="#ff9800", width=1.5)), row=2, col=1)

    fig.add_trace(go.Bar(x=dates, y=[d["macdHist"] for d in tail], marker_color=hist_colors,
                          name="MACD柱", opacity=0.8), row=3, col=1)
    fig.add_trace(go.Scatter(x=dates, y=[d["macd"] for d in tail], name="MACD",
                              line=dict(color="#2196f3", width=1.5)), row=3, col=1)
    fig.add_trace(go.Scatter(x=dates, y=[d["macdSig"] for d in tail], name="Signal",
                              line=dict(color="#ff9800", width=1.5)), row=3, col=1)

    fig.update_layout(
        title=dict(text=f"{name} 技術分析圖", font=dict(color="#fff", size=14)),
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(15,20,35,1)",
        height=680, margin=dict(l=50, r=15, t=45, b=25),
        shapes=shapes, annotations=annotations,
        xaxis=dict(rangeslider=dict(visible=False), gridcolor="rgba(255,255,255,.04)"),
        legend=dict(orientation="h", y=1.06, bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
        showlegend=True,
    )
    fig.update_yaxes(gridcolor="rgba(255,255,255,.04)")
    return fig



# ────────────────────────────────────────────────────────────────
# AI 智能綜合分析（呼叫 OpenAI API）
# ────────────────────────────────────────────────────────────────

def build_analysis_prompt(r):
    last = r["data"][-1]
    prev = r["data"][-2] if len(r["data"]) >= 2 else last
    chg = last["close"] - prev["close"]
    chgp = (chg / prev["close"] * 100) if prev["close"] else 0

    lines = []
    lines.append(f"股票：{r['stockId']} {r['name']}")
    lines.append(f"資料日期：{last['date']}　收盤：${last['close']}　漲跌：{'+' if chg >= 0 else ''}{chg:.2f} ({'+' if chgp >= 0 else ''}{chgp:.2f}%)")
    vol_ratio_txt = f"{(last['volume'] / last['vm20']):.2f}" if last["vm20"] else "N/A"
    lines.append(f"成交量：{last['volume']:,}　量比(vs MA20量)：{vol_ratio_txt}x")
    if last.get("macd") is not None and last.get("macdSig") is not None:
        bias = "（偏多）" if last["macd"] > last["macdSig"] else "（偏空）"
        lines.append(f"MACD：DIF={last['macd']:.2f}　Signal={last['macdSig']:.2f}　柱狀={last['macdHist']:.2f}{bias}")
    if last.get("bbU") is not None and last.get("bbL") is not None:
        bb_w = last["bbU"] - last["bbL"]
        bb_p = ((last["close"] - last["bbL"]) / bb_w * 100) if bb_w else 50
        lines.append(f"布林通道：上軌={last['bbU']:.2f}　下軌={last['bbL']:.2f}　股價位置={bb_p:.0f}%")
    lines.append("")
    lines.append(f"【DMI多方力道評分】總分 {r['total']}/100")
    dm = r["dm"]
    lines.append(f"+DI={dm['plusDI']:.1f}　-DI={dm['minusDI']:.1f}　ADX={dm['adx']:.1f}　ADXR={dm['adxr']:.1f}"
                 if dm["plusDI"] is not None and dm["adx"] is not None and dm["adxr"] is not None
                 else "DMI資料不足")
    lines.append(f"方向性 {dm['diPts']:.1f}/50、趨勢強度 {dm['adxPts']:.1f}/30、趨勢動能 {dm['adxrPts']:.1f}/20")
    all_sigs = dm["sigs"]
    bull_sigs = [s[0] for s in all_sigs if s[1] == "bull"]
    bear_sigs = [s[0] for s in all_sigs if s[1] == "bear"]
    if bull_sigs:
        lines.append("多頭訊號：" + "、".join(bull_sigs))
    if bear_sigs:
        lines.append("空頭訊號：" + "、".join(bear_sigs))
    lines.append("")
    lines.append(f"【回後買上漲 8條件核對】必要條件通過 {r['pb']['requiredPassed']}/{r['pb']['requiredTotal']}" + ("（全數通過）" if r["pb"]["allPass"] else ""))
    lines.append("")
    lines.append("【型態確認，15種進場型態】")
    for p in r["pt"]["results"]:
        status = "🔥剛突破（較前一交易日新增）" if p["justBroke"] else ("✅已突破" if p["breakout"] else ("🕒成形中未突破" if p["formed"] else "－未偵測到"))
        lines.append(f"・{p['name']}：{status}" + (f"（{p['detail']}）" if p.get("detail") else ""))
    return "\n".join(lines)


def run_ai_analysis(r, api_key, model):
    prompt = build_analysis_prompt(r)
    try:
        res = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0.4,
                "max_tokens": 900,
                "messages": [
                    {"role": "system", "content": "你是一位精通美股技術分析的資深操盤手，熟悉DMI趨向指標（+DI／-DI／ADX／ADXR）多方力道評分方法論，以及朱家泓《技術分析全攻略》的回後買上漲、頭肩底等進場型態判斷，並將此方法論套用於美國股市個股分析。請根據使用者提供的個股技術數據摘要，用繁體中文給出：1) 整體技術面研判（3-4句） 2) 進場時機與風險提示 3) 綜合建議（積極做多／可考慮／觀望／不建議）。語氣專業、精簡、避免空泛用詞，並提醒這僅為技術面參考，非投資建議，且未考慮美股盤前盤後交易、財報公布時程等因素。"},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=60,
        )
        j = res.json()
        if not res.ok:
            msg = (j.get("error") or {}).get("message") or f"HTTP {res.status_code}"
            raise RuntimeError(msg)
        content = ((j.get("choices") or [{}])[0].get("message") or {}).get("content")
        return content or "（AI 未回傳有效內容）"
    except Exception as e:
        return f"❌ AI 分析失敗：{e}\n請確認 API Key 是否正確、額度是否足夠，或稍後再試。"


# ────────────────────────────────────────────────────────────────
# 歷史回測系統（美股／S&P500）
# ────────────────────────────────────────────────────────────────

SNAPSHOT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots_us.db")

# ── 回測專用：長時間歷史版本的季度營收抓取，以及「當時已公告」的判斷邏輯──
# 美股是季報，用「季末+45天」當保守的已公告門檻（美股法規要求大型企業10-Q在
# 季末後40天內申報、10-K在季末後60-90天內申報，45天是介於中間的保守估計，
# 不是精確的實際申報日——要抓每檔股票精確的申報日期需要額外一次API查詢，
# 這裡為了控制API呼叫量選擇用估計值，可能跟實際公告日有一兩週的落差）。
def fetch_revenue_history_for_backtest_us(stock_id: str, token: str, quarters_back: int):
    sym = to_fmp_symbol(stock_id)
    limit = min(40, quarters_back + 4)
    url = f"{FMP_BASE}/income-statement?symbol={sym}&period=quarter&limit={limit}&apikey={token}"
    rows = api_fetch(url)
    if not isinstance(rows, list) or not rows:
        return {}
    rev_map = {}
    for r in rows:
        d = str(r.get("date", ""))[:10]
        if len(d) < 7 or r.get("revenue") is None:
            continue
        y, mo = int(d[:4]), int(d[5:7])
        q = (mo - 1) // 3 + 1
        rev_map[f"{y}-Q{q}"] = r["revenue"]
    return rev_map


def quarter_end_date(year: int, quarter: int) -> datetime:
    end_month = quarter * 3  # Q1→3月, Q2→6月, Q3→9月, Q4→12月
    if end_month == 12:
        return datetime(year, 12, 31)
    return datetime(year, end_month + 1, 1) - timedelta(days=1)


def revenue_known_by_us(year: int, quarter: int, as_of_date_str: str) -> bool:
    q_end = quarter_end_date(year, quarter)
    disclose_date = q_end + timedelta(days=45)
    as_of = datetime.strptime(as_of_date_str, "%Y-%m-%d")
    return as_of >= disclose_date


def last_n_known_quarters_us(as_of_date_str: str, n: int = 1):
    as_of = datetime.strptime(as_of_date_str, "%Y-%m-%d")
    y, q = as_of.year, (as_of.month - 1) // 3 + 1
    result = []
    for _ in range(8):
        if revenue_known_by_us(y, q, as_of_date_str):
            result.append((y, q))
            if len(result) >= n:
                break
        q -= 1
        if q < 1:
            q, y = 4, y - 1
    result.reverse()
    return result


def build_price_quarter_avg_map_us(data):
    sums, counts = {}, {}
    for d in data:
        y, mo = int(d["date"][:4]), int(d["date"][5:7])
        key = f"{y}-Q{(mo - 1) // 3 + 1}"
        sums[key] = sums.get(key, 0) + d["close"]
        counts[key] = counts.get(key, 0) + 1
    return {k: sums[k] / counts[k] for k in sums}


def compute_divergence_asof_us(rev_map: dict, price_quarter_avg_map: dict, eval_date_str: str):
    """算某個評估日『當下』能看到的近1季營收YoY vs 均價YoY乖離度，以及單獨的均價
    YoY（美股季報，只用最近1個已公告季度，不像台股月營收可以累加3個月）。
    回傳 (div_total, price_yoy_total)，都可能是 None。"""
    quarters = last_n_known_quarters_us(eval_date_str, 1)
    if not quarters:
        return None, None
    y, q = quarters[0]
    rev_this, rev_last = rev_map.get(f"{y}-Q{q}"), rev_map.get(f"{y-1}-Q{q}")
    px_this, px_last = price_quarter_avg_map.get(f"{y}-Q{q}"), price_quarter_avg_map.get(f"{y-1}-Q{q}")
    price_yoy_total = ((px_this - px_last) / px_last * 100) if (px_last and px_this is not None) else None
    div_total = None
    if rev_last and rev_this is not None and px_last and px_this is not None:
        rev_yoy = (rev_this - rev_last) / rev_last * 100
        div_total = rev_yoy - price_yoy_total
    return div_total, price_yoy_total


BACKTEST_HORIZONS = [5, 10, 20]  # 事後驗證用的天數（皆為交易日）
# detect_patterns()裡14種型態的id/name對照（回後買上漲是獨立算的pb_all_pass，
# 不在這14種裡——外面常講的「15種進場型態」是這14種圖形型態+回後買上漲）。
PATTERN_DEFS = [
    {"id": "hs", "name": "頭肩底"}, {"id": "chs", "name": "複式頭肩底"}, {"id": "nb", "name": "N字底"},
    {"id": "tb", "name": "三重底"}, {"id": "rb", "name": "圓弧底"}, {"id": "fb", "name": "一字底(均線糾結)"},
    {"id": "abc", "name": "突破ABC修正下降切線"}, {"id": "channel", "name": "突破上升軌道線"},
    {"id": "blackk", "name": "突破飆股大量黑K最高點"}, {"id": "kbp", "name": "K線橫盤的突破"},
    {"id": "harami_bear", "name": "母子懷抱(高檔)"}, {"id": "harami_bull", "name": "母子懷抱(低檔)"},
    {"id": "morning_star", "name": "晨星"}, {"id": "evening_star", "name": "夜星"},
]


def init_backtest_table():
    conn = sqlite3.connect(SNAPSHOT_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_evals (
            eval_date TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            name TEXT,
            score INTEGER,
            plus_di REAL,
            minus_di REAL,
            adx REAL,
            adxr REAL,
            bb_pos REAL,
            is_breakout INTEGER,
            is_pullback_rebound INTEGER,
            entry_close REAL,
            ret_5d REAL,
            ret_10d REAL,
            ret_20d REAL,
            created_at TEXT,
            PRIMARY KEY (eval_date, stock_id)
        )
    """)
    # 相容舊資料庫：用 ALTER TABLE 補新欄位，已存在就吃掉錯誤跳過
    # （SQLite沒有 ADD COLUMN IF NOT EXISTS，只能用這種方式相容舊表）
    for col_def in [
        "pattern_formed INTEGER",
        "pattern_breakout INTEGER",
        "pattern_just_broke INTEGER",
        "pb_all_pass INTEGER",
        "div_total REAL",
        "price_yoy_1q REAL",
        "golden_cross_recent INTEGER",
        "kdj_golden_cross_recent INTEGER",
        "kdj_death_cross_recent INTEGER",
        "pattern_hits_json TEXT",
        "rel_strength_20 REAL",
        "vol_ratio REAL",
        "benchmark_above20 INTEGER",
        "benchmark_above60 INTEGER",
    ]:
        try:
            conn.execute(f"ALTER TABLE backtest_evals ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # 欄位已存在
    conn.commit()
    return conn


def run_historical_backtest(token: str, months_back: int = 3, include_pattern: bool = True,
                             include_div: bool = False,
                             min_liquidity: float = 0):
    """回溯過去 months_back 個月，對每個交易日重新計算「當時」的DMI/評分/訊號
    （只用當天以前的資料切片再丟進既有的 calc_dmi／score_dmi／compute_core_signals／
    check_pullback_buy／detect_patterns，保證沒有偷看未來——這幾個函式本來就是
    「給一段資料、算出最後一天的訊號」，直接重複利用，不用另外寫一套向量化版本
    冒index算錯的風險），然後對照5/10/20天後的實際收盤價算出報酬率，存進
    backtest_evals 表。

    型態確認（型態辨識、回後買上漲）不需要額外API，直接免費算；近1季YoY乖離度
    需要多抓一組歷史資料（季度營收），而且要算YoY需要抓到超過一年的股價歷史，
    勾選這項時，每檔股票的抓取天數／API呼叫次數都會明顯增加，整體時間可能拉長
    到原本的1.5-2倍。美股沒有三大法人買賣超這種資料，這裡跟台股版不同，沒有
    對應的選項。

    美股是季報，公告有『時間差』（用季末+45天估計，非精確申報日），這裡用
    revenue_known_by_us 嚴格只採用『評估當天已經公告』的季度，避免回測偷看
    『當下還沒公告』的營收資料。

    每檔股票固定用S&P500清單當樣本池。運算量本身很小（純迴圈，沒有額外API呼叫），
    真正花時間的還是500多檔的資料抓取次數。
    """
    conn = init_backtest_table()
    name_map = SP500_NAMES
    lookback_buffer = 60
    max_horizon = max(BACKTEST_HORIZONS)
    # 乖離度需要YoY比較（去年同季），所以價格歷史要抓到超過12個月，才能覆蓋
    # 整個評估窗內每一天回頭看「去年同季」的均價
    price_fetch_days = (months_back * 30 + lookback_buffer + max_horizon + 10 + 400) if include_div \
        else (months_back * 30 + lookback_buffer + max_horizon + 10)
    quarters_back = -(-months_back // 3) + 5  # 涵蓋YoY所需的前一年同期，math.ceil的整數版寫法

    # 相對強弱要用的大盤(SPY)資料，整個回測只抓一次，不是每檔股票各抓一次
    try:
        benchmark = fetch_benchmark_series(token, price_fetch_days)
    except Exception as e:
        benchmark = None
        st.warning(
            f"⚠️ 這次回測抓不到大盤(SPY)資料，「相對強弱」相關的欄位/旗標/勝率表這次"
            f"不會出現（不影響其他分析）。錯誤訊息：{e}　常見原因：FMP方案不支援ETF"
            f"資料、API額度用完、或SPY這個代號被擋。"
        )

    total = len(SP500_LIST)
    progress_bar = st.progress(0)
    status = st.empty()
    log_box = st.expander("🔬 回測進度紀錄（展開查看逐檔進度）", expanded=False)
    saved_rows, failed = 0, 0

    for i, sid in enumerate(SP500_LIST):
        status.text(f"🔬 回測中：{sid}… ({i + 1}/{total})　已存 {saved_rows:,} 筆　失敗 {failed} 檔")
        try:
            rows = fetch_price_data(sid, token, price_fetch_days)
            raw_data = sorted(
                [{"date": d["date"], "open": float(d["open"]), "high": float(d["high"]),
                  "low": float(d["low"]), "close": float(d["close"]), "volume": float(d.get("volume") or 0)}
                 for d in rows],
                key=lambda x: x["date"],
            )
            data = enrich(raw_data)
            n = len(data)
            eval_start = lookback_buffer
            eval_end = n - max_horizon  # 不含此index，確保每個評估點後面都還有滿20天可以驗證
            if eval_end <= eval_start:
                failed += 1
                continue

            rev_map, price_month_avg_map = {}, {}
            if include_div:
                try:
                    rev_map = fetch_revenue_history_for_backtest_us(sid, token, quarters_back)
                    price_month_avg_map = build_price_quarter_avg_map_us(data)
                except Exception:
                    rev_map = {}  # 抓不到就這個標的的乖離度全部是None，不影響其他欄位

            name = name_map.get(sid, sid)
            # 限定只評估「最近 months_back 個月」範圍內的交易日。用「這次實際抓到
            # 的資料裡最新一天」當基準往回推，不要用「今天」當基準——FMP有時候
            # 回傳的資料不是精準到今天（例如資料本身有落後），用「今天」當基準
            # 會導致整批資料被誤判成太舊而濾空。
            latest_data_date = data[-1]["date"]
            eval_window_start_date = (
                datetime.strptime(latest_data_date, "%Y-%m-%d") - timedelta(days=months_back * 30)
            ).strftime("%Y-%m-%d")

            for idx in range(eval_start, eval_end):
                eval_date = data[idx]["date"]
                if eval_date < eval_window_start_date:
                    continue
                if min_liquidity > 0:
                    liq_start = max(0, idx - 19)
                    liq_bars = data[liq_start:idx + 1]
                    avg_liquidity = sum(b["close"] * b["volume"] for b in liq_bars) / len(liq_bars)
                    if avg_liquidity < min_liquidity:
                        continue  # 當時的流動性不夠，跳過這個評估點
                data_slice = data[:idx + 1]  # 只給「當時」以前的資料，不含未來
                dmi = calc_dmi(data_slice, 14)
                dm = score_dmi(dmi)
                sig = compute_core_signals(data_slice, dm)
                rel_strength_20 = compute_relative_strength(data_slice, benchmark, 20) if benchmark else None
                vol_ratio = compute_vol_ratio(data_slice)
                above20, above60 = compute_benchmark_regime(benchmark, eval_date) if benchmark else (None, None)

                pattern_formed, pattern_breakout, pattern_just_broke, pb_all_pass = None, None, None, None
                pattern_hits_json = None
                if include_pattern:
                    pb = check_pullback_buy(data_slice)
                    pt = detect_patterns(data_slice, pb)
                    pattern_formed = int(pt["anyFormed"])
                    pattern_breakout = int(pt["anyBreakout"])
                    pattern_just_broke = int(pt["anyJustBroke"])
                    pb_all_pass = int(pb["allPass"])
                    # 15種型態各自的「剛形成」（justBroke）：跟aggregate的anyJustBroke
                    # 同一套判斷邏輯，只是拆成每個型態各自記錄。存成JSON字串（SQLite
                    # 沒有原生的dict欄位），要用時再解回來。
                    pattern_hits = {pr["id"]: (1 if pr["justBroke"] else 0) for pr in pt["results"]}
                    pattern_hits_json = json.dumps(pattern_hits)

                div_total, price_yoy_1q = compute_divergence_asof_us(rev_map, price_month_avg_map, eval_date) if include_div else (None, None)

                entry_close = data[idx]["close"]
                rets = {}
                for h in BACKTEST_HORIZONS:
                    if idx + h < n and entry_close:
                        rets[h] = (data[idx + h]["close"] - entry_close) / entry_close * 100
                    else:
                        rets[h] = None
                conn.execute("""
                    INSERT OR REPLACE INTO backtest_evals
                    (eval_date, stock_id, name, score, plus_di, minus_di, adx, adxr, bb_pos,
                     is_breakout, is_pullback_rebound, entry_close, ret_5d, ret_10d, ret_20d,
                     pattern_formed, pattern_breakout, pattern_just_broke, pb_all_pass,
                     div_total, price_yoy_1q, golden_cross_recent,
                     kdj_golden_cross_recent, kdj_death_cross_recent, pattern_hits_json,
                     rel_strength_20, vol_ratio, benchmark_above20, benchmark_above60, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    eval_date, sid, name, dm["score"], dm["plusDI"], dm["minusDI"], dm["adx"], dm["adxr"],
                    sig["bb_pos"], int(sig["is_breakout"]), int(sig["is_pullback_rebound"]), entry_close,
                    rets[5], rets[10], rets[20],
                    pattern_formed, pattern_breakout, pattern_just_broke, pb_all_pass,
                    div_total, price_yoy_1q, int(sig["golden_cross_recent"]),
                    int(sig["kdj_golden_cross_recent"]), int(sig["kdj_death_cross_recent"]), pattern_hits_json,
                    rel_strength_20, vol_ratio,
                    None if above20 is None else int(above20), None if above60 is None else int(above60),
                    datetime.now().isoformat(timespec="seconds"),
                ))
                saved_rows += 1
            if (i + 1) % 20 == 0:
                conn.commit()
        except Exception as ex:
            failed += 1
            with log_box:
                st.caption(f"❌ {sid} 失敗：{ex}")
        progress_bar.progress((i + 1) / total)
        if i < total - 1:
            time.sleep(0.3)

    conn.commit()
    conn.close()
    progress_bar.empty()
    status.empty()
    st.success(f"✅ 歷史回測完成：{saved_rows:,} 筆評估紀錄（{total - failed}/{total} 檔成功），可以往下看分析結果")


def load_backtest_df():
    if not os.path.exists(SNAPSHOT_DB_PATH):
        return None
    conn = sqlite3.connect(SNAPSHOT_DB_PATH)
    try:
        df = pd.read_sql_query("SELECT * FROM backtest_evals", conn)
    except Exception:
        df = None
    conn.close()
    if df is None or df.empty:
        return None
    if "pattern_hits_json" in df.columns:
        df["pattern_hits"] = df["pattern_hits_json"].apply(
            lambda s: json.loads(s) if isinstance(s, str) else None
        )
    return df


def analyze_score_buckets(df: pd.DataFrame) -> pd.DataFrame:
    def bucket(s):
        if s >= 80:
            return "80-100（積極做多）"
        if s >= 65:
            return "65-79（可考慮進場）"
        if s >= 50:
            return "50-64（觀望）"
        return "0-49（不建議）"

    d = df.copy()
    d["bucket"] = d["score"].apply(bucket)
    order = ["0-49（不建議）", "50-64（觀望）", "65-79（可考慮進場）", "80-100（積極做多）"]
    rows = []
    for b, g in d.groupby("bucket"):
        row = {"評分區間": b, "樣本數": len(g)}
        for h in BACKTEST_HORIZONS:
            valid = g[f"ret_{h}d"].dropna()
            row[f"{h}日平均報酬%"] = round(valid.mean(), 2) if len(valid) else None
            row[f"{h}日勝率%"] = round((valid > 0).mean() * 100, 1) if len(valid) else None
        rows.append(row)
    result = pd.DataFrame(rows)
    result["_order"] = result["評分區間"].apply(lambda x: order.index(x) if x in order else 99)
    return result.sort_values("_order").drop(columns="_order").reset_index(drop=True)


def analyze_tag_hitrate(df: pd.DataFrame, tag_col: str, tag_label: str) -> pd.DataFrame:
    rows = []
    for val, g in df.groupby(tag_col):
        label = f"{tag_label}＝是" if val == 1 else f"{tag_label}＝否"
        row = {"標記": label, "樣本數": len(g)}
        for h in BACKTEST_HORIZONS:
            valid = g[f"ret_{h}d"].dropna()
            row[f"{h}日平均報酬%"] = round(valid.mean(), 2) if len(valid) else None
            row[f"{h}日勝率%"] = round((valid > 0).mean() * 100, 1) if len(valid) else None
        rows.append(row)
    return pd.DataFrame(rows)


def analyze_pattern_hits(df: pd.DataFrame) -> pd.DataFrame:
    """15種型態各自「剛形成」單獨列一列（只看「是」的情況，橫向比較15種型態彼此
    的表現，不是看單一型態內部有沒有效）。依樣本數由多到少排序，樣本數<10筆的
    型態不列出，避免單一兩筆資料的100%勝率造成誤導。"""
    if "pattern_hits" not in df.columns:
        return pd.DataFrame()
    rows = []
    for pdef in PATTERN_DEFS:
        pid = pdef["id"]
        mask = df["pattern_hits"].apply(lambda h: bool(h) and h.get(pid) == 1)
        g = df[mask]
        if len(g) < 10:
            continue
        row = {"型態": pdef["name"] + "剛形成", "樣本數": len(g)}
        for h in BACKTEST_HORIZONS:
            valid = g[f"ret_{h}d"].dropna()
            row[f"{h}日平均報酬%"] = round(valid.mean(), 2) if len(valid) else None
            row[f"{h}日勝率%"] = round((valid > 0).mean() * 100, 1) if len(valid) else None
        rows.append(row)
    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values("樣本數", ascending=False).reset_index(drop=True)
    return result


def grid_search_params(df: pd.DataFrame, target_horizon: int = 10):
    """在已收集的原始指標值（+DI/-DI/ADX/ADXR）上，重新代入不同的評分公式參數
    （方向性權重、ADX滿分上限、ADXR加分值）試算，看哪組參數對N天後報酬的判斷力
    比較好——不需要重新抓資料，純粹是在同一份歷史資料上換公式重算。
    這是簡化版網格搜尋，樣本數有限時容易過度適配歷史資料，結果僅供參考方向，
    不建議未經檢視就直接套用到正式評分公式。"""
    ret_col = f"ret_{target_horizon}d"
    valid_df = df.dropna(subset=[ret_col, "plus_di", "minus_di", "adx"]).copy()
    if valid_df.empty:
        return pd.DataFrame()

    di_sum = valid_df["plus_di"] + valid_df["minus_di"]
    di_dom = np.where(di_sum > 0, valid_df["plus_di"] / di_sum * 100, 50)
    has_adxr = valid_df["adxr"].notna()
    adx_gt_adxr = valid_df["adx"] > valid_df["adxr"].fillna(-1)

    results = []
    for adx_cap in [30, 40, 50]:
        for adxr_bonus in [15, 20, 25]:
            for di_weight in [0.4, 0.5, 0.6]:
                di_pts = di_dom * di_weight
                adx_max_pts = max(0, 100 - 100 * di_weight - adxr_bonus)  # 剩餘配分給ADX，確保三項頂多加到100
                adx_pts = np.minimum(valid_df["adx"], adx_cap) / adx_cap * adx_max_pts
                adxr_pts = np.where(has_adxr & adx_gt_adxr, adxr_bonus, 0)
                new_score = np.clip(di_pts + adx_pts + adxr_pts, 0, 100)

                for buy_thr in [50, 65, 80]:
                    buy_mask = new_score >= buy_thr
                    if buy_mask.sum() < 20:
                        continue
                    rets = valid_df.loc[buy_mask, ret_col]
                    results.append({
                        "方向性權重": di_weight, "ADX滿分上限": adx_cap, "ADXR加分": adxr_bonus,
                        "買進門檻": buy_thr, "訊號數": int(buy_mask.sum()),
                        f"{target_horizon}日平均報酬%": round(rets.mean(), 2),
                        f"{target_horizon}日勝率%": round((rets > 0).mean() * 100, 1),
                    })
    result_df = pd.DataFrame(results)
    if result_df.empty:
        return result_df
    return result_df.sort_values(f"{target_horizon}日平均報酬%", ascending=False).head(15).reset_index(drop=True)


# ────────────────────────────────────────────────────────────────
# 多因子複選搜尋：把每一列資料轉成一組「條件旗標」（分數夠高、有突破盤標記、
# 型態確認、布林通道位置、乖離度、法人買賣超…），然後窮舉1~3個條件的所有組合，
# 看哪個組合的勝率/平均報酬最好。
# ────────────────────────────────────────────────────────────────
def build_condition_flags(df: pd.DataFrame) -> pd.DataFrame:
    """把原始欄位轉成一組True/False條件旗標，供複選搜尋窮舉組合用。哪些旗標會
    出現取決於這次回測有沒有收集對應欄位（乖離度／法人買賣超是可選的，型態確認
    理論上一定有，但相容舊資料庫可能是空值，一併防呆）。"""
    d = df.copy()
    flags = {}
    flags["多方力道≥65"] = d["score"] >= 65
    flags["多方力道≥80"] = d["score"] >= 80
    flags["強勢突破盤"] = d["is_breakout"] == 1
    flags["跌深反彈盤"] = d["is_pullback_rebound"] == 1
    flags["布林通道低檔(≤20%)"] = d["bb_pos"] <= 20
    flags["布林通道高檔(≥80%)"] = d["bb_pos"] >= 80
    if "golden_cross_recent" in d.columns and d["golden_cross_recent"].notna().any():
        flags["MACD近3日內黃金交叉"] = d["golden_cross_recent"] == 1
    if "kdj_golden_cross_recent" in d.columns and d["kdj_golden_cross_recent"].notna().any():
        flags["KDJ近3日內黃金交叉"] = d["kdj_golden_cross_recent"] == 1
    if "kdj_death_cross_recent" in d.columns and d["kdj_death_cross_recent"].notna().any():
        flags["KDJ近3日內死亡交叉"] = d["kdj_death_cross_recent"] == 1
    if "rel_strength_20" in d.columns and d["rel_strength_20"].notna().any():
        flags["相對強弱為正(強於大盤)"] = d["rel_strength_20"] > 0
        flags["相對強弱為負(弱於大盤)"] = d["rel_strength_20"] < 0
    if "vol_ratio" in d.columns and d["vol_ratio"].notna().any():
        flags["爆量(≥1.5倍均量)"] = d["vol_ratio"] >= 1.5
        flags["爆量(≥2倍均量)"] = d["vol_ratio"] >= 2
    if "benchmark_above20" in d.columns and d["benchmark_above20"].notna().any():
        flags["大盤站上20日均線"] = d["benchmark_above20"] == 1
        flags["大盤跌破20日均線"] = d["benchmark_above20"] == 0
    if "benchmark_above60" in d.columns and d["benchmark_above60"].notna().any():
        flags["大盤站上60日均線"] = d["benchmark_above60"] == 1
        flags["大盤跌破60日均線"] = d["benchmark_above60"] == 0

    if "pattern_formed" in d.columns and d["pattern_formed"].notna().any():
        flags["型態成形中"] = d["pattern_formed"] == 1
    if "pattern_breakout" in d.columns and d["pattern_breakout"].notna().any():
        flags["型態突破確認"] = d["pattern_breakout"] == 1
    if "pattern_just_broke" in d.columns and d["pattern_just_broke"].notna().any():
        flags["型態剛形成(剛突破)"] = d["pattern_just_broke"] == 1
    if "pb_all_pass" in d.columns and d["pb_all_pass"].notna().any():
        flags["回後買上漲全通過"] = d["pb_all_pass"] == 1
    if "div_total" in d.columns and d["div_total"].notna().any():
        flags["近1季乖離度為正(營收優於股價)"] = d["div_total"] > 0
        flags["近1季乖離度為負(股價超前營收)"] = d["div_total"] < 0
    if "price_yoy_1q" in d.columns and d["price_yoy_1q"].notna().any():
        flags["近1季均價YoY為正"] = d["price_yoy_1q"] > 0
        flags["近1季均價YoY為負"] = d["price_yoy_1q"] < 0

    # 15種進場型態裡的14種圖形型態，各自的「剛形成」單獨當一個條件旗標
    # （回後買上漲已經是獨立的「回後買上漲全通過」旗標，這裡不重複）。
    if "pattern_hits" in d.columns and d["pattern_hits"].notna().any():
        for pdef in PATTERN_DEFS:
            pid = pdef["id"]
            flags[pdef["name"] + "剛形成"] = d["pattern_hits"].apply(
                lambda h: bool(h) and h.get(pid) == 1
            )

    flag_df = pd.DataFrame(flags, index=d.index)
    return pd.concat([d, flag_df], axis=1), list(flags.keys())


def combo_search(df: pd.DataFrame, target_horizon: int = 10, max_combo_size: int = 3,
                  min_samples: int = 50, top_n: int = 20):
    """窮舉1~max_combo_size個條件旗標的AND組合，看哪個組合對N天後報酬的判斷力
    最好。組合數會隨旗標數量跟max_combo_size快速增加（這是多重比較，測試的組合
    越多，純粹運氣好而表現突出的組合也會越多，不是每個排前面的組合都代表真的
    有效——樣本數門檻（min_samples）是用來過濾掉「條件太嚴苛、樣本太少」的組合，
    但無法完全排除多重比較造成的偽陽性，結果僅供參考方向）。"""
    d, flag_names = build_condition_flags(df)
    ret_col = f"ret_{target_horizon}d"
    valid = d.dropna(subset=[ret_col])
    if valid.empty or not flag_names:
        return pd.DataFrame(), 0

    results = []
    combos_tested = 0
    for size in range(1, max_combo_size + 1):
        for combo in itertools.combinations(flag_names, size):
            mask = valid[list(combo)].all(axis=1)
            combos_tested += 1
            n_match = int(mask.sum())
            if n_match < min_samples:
                continue
            rets = valid.loc[mask, ret_col]
            results.append({
                "條件組合": " ＋ ".join(combo),
                "條件數": size,
                "樣本數": n_match,
                f"{target_horizon}日平均報酬%": round(rets.mean(), 2),
                f"{target_horizon}日勝率%": round((rets > 0).mean() * 100, 1),
            })
    result_df = pd.DataFrame(results)
    if result_df.empty:
        return result_df, combos_tested
    result_df = result_df.sort_values(f"{target_horizon}日勝率%", ascending=False).head(top_n).reset_index(drop=True)
    return result_df, combos_tested


def find_moonshot_combos(df: pd.DataFrame, target_horizon: int = 10, threshold: float = 30,
                          max_combo_size: int = 3, min_samples: int = 20, top_n: int = 20):
    """飆股搜尋：找哪些條件組合最容易在N天內出現「漲幅超過threshold%」的大行情，
    跟combo_search看的東西不一樣——那邊看的是『平均表現/整體勝率』，這裡只看
    『命中大行情的比例』，一個組合平均報酬普通、但只要常常出現飆股也會被排到
    前面。從沒出現過飆股的組合直接不列出來（沒意義）。飆股本來就是稀有事件，
    樣本數門檻預設調低到20，但仍要搭配『飆股次數』一起看，次數只有個位數的
    不建議當真——比例再高，靠幾次極端值撐出來的數字沒有統計意義。"""
    d, flag_names = build_condition_flags(df)
    ret_col = f"ret_{target_horizon}d"
    valid = d.dropna(subset=[ret_col])
    if valid.empty or not flag_names:
        return pd.DataFrame(), 0

    results = []
    combos_tested = 0
    for size in range(1, max_combo_size + 1):
        for combo in itertools.combinations(flag_names, size):
            mask = valid[list(combo)].all(axis=1)
            combos_tested += 1
            n_match = int(mask.sum())
            if n_match < min_samples:
                continue
            rets = valid.loc[mask, ret_col]
            moonshots = rets[rets > threshold]
            if moonshots.empty:
                continue
            results.append({
                "條件組合": " ＋ ".join(combo),
                "條件數": size,
                "樣本數": n_match,
                "飆股次數": len(moonshots),
                "飆股比例%": round(len(moonshots) / n_match * 100, 1),
                "飆股平均漲幅%": round(moonshots.mean(), 1),
            })
    result_df = pd.DataFrame(results)
    if result_df.empty:
        return result_df, combos_tested
    result_df = result_df.sort_values("飆股比例%", ascending=False).head(top_n).reset_index(drop=True)
    return result_df, combos_tested


# 使用者指定要追蹤的特定條件組合——跟上面窮舉搜尋不一樣的地方是：這些組合不管
# 樣本數多少、排名有沒有進前幾名，都一定會顯示出來，方便針對特定假設做比較
# （例如「型態突破確認+均價YoY轉正」這種有明確邏輯的假設，即使沒有進入排行榜，
# 使用者可能還是想知道它實際表現如何）。
# ✅ 2026-09-21 美股（S&P500）自己的歷史回測結果，這份是真正驗證過的資料。
# 22組去重後的高勝率組合（同一組合出現在多個天數的榜單就合併成一列，
# PINNED_COMBO_WINRATES記錄各天數的勝率）。
# ✅ 2026-09-22 美股（S&P500）自己的回測結果（含相對強弱vs SPY、成交量確認），
# 篩選條件：勝率>60%（5/10/20日任一天數達標即列入）。50組——完整計算不排除
# 聚合旗標變體（型態成形中/型態突破確認/型態剛形成(剛突破)這類跟特定型態同時
# 成立時數字會一樣的組合），每個文字上不同的組合都各自列出。
PINNED_COMBOS = [
    ["KDJ近3日內死亡交叉", "N字底剛形成", "一字底(均線糾結)剛形成"],  # 10日80.4%
    ["布林通道低檔(≤20%)", "KDJ近3日內黃金交叉", "母子懷抱(高檔)剛形成"],  # 10日76.9%，20日75.4%
    ["相對強弱為正(強於大盤)", "頭肩底剛形成", "圓弧底剛形成"],  # 5日75.6%，10日70.5%，20日70.5%
    ["頭肩底剛形成", "圓弧底剛形成"],  # 5日74.7%
    ["型態成形中", "頭肩底剛形成", "圓弧底剛形成"],  # 5日74.7%
    ["型態突破確認", "頭肩底剛形成", "圓弧底剛形成"],  # 5日74.7%
    ["型態剛形成(剛突破)", "頭肩底剛形成", "圓弧底剛形成"],  # 5日74.7%
    ["KDJ近3日內黃金交叉", "N字底剛形成", "一字底(均線糾結)剛形成"],  # 10日74.4%
    ["多方力道≥80", "MACD近3日內黃金交叉", "突破ABC修正下降切線剛形成"],  # 20日74.1%
    ["布林通道高檔(≥80%)", "頭肩底剛形成", "圓弧底剛形成"],  # 5日74.0%
    ["爆量(≥1.5倍均量)", "複式頭肩底剛形成", "N字底剛形成"],  # 10日70.2%，20日73.7%
    ["多方力道≥80", "爆量(≥1.5倍均量)", "一字底(均線糾結)剛形成"],  # 20日73.7%
    ["多方力道≥80", "強勢突破盤", "一字底(均線糾結)剛形成"],  # 20日73.4%
    ["多方力道≥80", "相對強弱為正(強於大盤)", "一字底(均線糾結)剛形成"],  # 20日73.3%
    ["KDJ近3日內死亡交叉", "N字底剛形成", "K線橫盤的突破剛形成"],  # 20日73.0%
    ["布林通道低檔(≤20%)", "相對強弱為負(弱於大盤)", "母子懷抱(高檔)剛形成"],  # 10日72.9%
    ["多方力道≥80", "一字底(均線糾結)剛形成"],  # 20日72.7%
    ["多方力道≥65", "多方力道≥80", "一字底(均線糾結)剛形成"],  # 20日72.7%
    ["多方力道≥80", "布林通道高檔(≥80%)", "一字底(均線糾結)剛形成"],  # 20日72.7%
    ["多方力道≥80", "型態成形中", "一字底(均線糾結)剛形成"],  # 20日72.7%
    ["多方力道≥80", "型態突破確認", "一字底(均線糾結)剛形成"],  # 20日72.7%
    ["多方力道≥80", "型態剛形成(剛突破)", "一字底(均線糾結)剛形成"],  # 20日72.7%
    ["多方力道≥65", "圓弧底剛形成", "一字底(均線糾結)剛形成"],  # 20日72.3%
    ["N字底剛形成", "三重底剛形成", "一字底(均線糾結)剛形成"],  # 5日68.9%，10日72.2%
    ["頭肩底剛形成", "N字底剛形成", "圓弧底剛形成"],  # 5日72.0%
    ["爆量(≥2倍均量)", "頭肩底剛形成"],  # 10日72.0%，20日70.0%
    ["爆量(≥1.5倍均量)", "爆量(≥2倍均量)", "頭肩底剛形成"],  # 10日72.0%，20日70.0%
    ["爆量(≥2倍均量)", "型態成形中", "頭肩底剛形成"],  # 10日72.0%，20日70.0%
    ["爆量(≥2倍均量)", "型態突破確認", "頭肩底剛形成"],  # 10日72.0%，20日70.0%
    ["爆量(≥2倍均量)", "型態剛形成(剛突破)", "頭肩底剛形成"],  # 10日72.0%
    ["布林通道低檔(≤20%)", "母子懷抱(高檔)剛形成"],  # 10日71.8%
    ["布林通道低檔(≤20%)", "型態成形中", "母子懷抱(高檔)剛形成"],  # 10日71.8%
    ["布林通道低檔(≤20%)", "型態突破確認", "母子懷抱(高檔)剛形成"],  # 10日71.8%
    ["布林通道低檔(≤20%)", "型態剛形成(剛突破)", "母子懷抱(高檔)剛形成"],  # 10日71.8%
    ["多方力道≥80", "相對強弱為負(弱於大盤)", "爆量(≥1.5倍均量)"],  # 10日71.6%
    ["多方力道≥65", "N字底剛形成", "一字底(均線糾結)剛形成"],  # 10日71.4%
    ["強勢突破盤", "頭肩底剛形成", "圓弧底剛形成"],  # 5日70.7%
    ["KDJ近3日內黃金交叉", "爆量(≥1.5倍均量)", "頭肩底剛形成"],  # 20日70.7%
    ["強勢突破盤", "N字底剛形成", "一字底(均線糾結)剛形成"],  # 10日70.5%
    ["布林通道低檔(≤20%)", "夜星剛形成"],  # 5日69.1%，10日70.1%
    ["布林通道低檔(≤20%)", "相對強弱為負(弱於大盤)", "夜星剛形成"],  # 5日69.8%
    ["KDJ近3日內死亡交叉", "爆量(≥2倍均量)", "圓弧底剛形成"],  # 5日69.7%
    ["布林通道低檔(≤20%)", "型態成形中", "夜星剛形成"],  # 5日69.1%
    ["布林通道低檔(≤20%)", "型態突破確認", "夜星剛形成"],  # 5日69.1%
    ["布林通道低檔(≤20%)", "型態剛形成(剛突破)", "夜星剛形成"],  # 5日69.1%
    ["爆量(≥1.5倍均量)", "夜星剛形成"],  # 5日68.9%
    ["爆量(≥1.5倍均量)", "型態成形中", "夜星剛形成"],  # 5日68.9%
    ["爆量(≥1.5倍均量)", "型態突破確認", "夜星剛形成"],  # 5日68.9%
    ["爆量(≥1.5倍均量)", "型態剛形成(剛突破)", "夜星剛形成"],  # 5日68.9%
    ["布林通道低檔(≤20%)", "相對強弱為正(強於大盤)", "爆量(≥1.5倍均量)"],  # 5日68.8%
]


PINNED_COMBO_WINRATES = {
    "KDJ近3日內死亡交叉 ＋ N字底剛形成 ＋ 一字底(均線糾結)剛形成": { 10: 80.4 },
    "布林通道低檔(≤20%) ＋ KDJ近3日內黃金交叉 ＋ 母子懷抱(高檔)剛形成": { 10: 76.9, 20: 75.4 },
    "相對強弱為正(強於大盤) ＋ 頭肩底剛形成 ＋ 圓弧底剛形成": { 5: 75.6, 10: 70.5, 20: 70.5 },
    "頭肩底剛形成 ＋ 圓弧底剛形成": { 5: 74.7 },
    "型態成形中 ＋ 頭肩底剛形成 ＋ 圓弧底剛形成": { 5: 74.7 },
    "型態突破確認 ＋ 頭肩底剛形成 ＋ 圓弧底剛形成": { 5: 74.7 },
    "型態剛形成(剛突破) ＋ 頭肩底剛形成 ＋ 圓弧底剛形成": { 5: 74.7 },
    "KDJ近3日內黃金交叉 ＋ N字底剛形成 ＋ 一字底(均線糾結)剛形成": { 10: 74.4 },
    "多方力道≥80 ＋ MACD近3日內黃金交叉 ＋ 突破ABC修正下降切線剛形成": { 20: 74.1 },
    "布林通道高檔(≥80%) ＋ 頭肩底剛形成 ＋ 圓弧底剛形成": { 5: 74.0 },
    "爆量(≥1.5倍均量) ＋ 複式頭肩底剛形成 ＋ N字底剛形成": { 10: 70.2, 20: 73.7 },
    "多方力道≥80 ＋ 爆量(≥1.5倍均量) ＋ 一字底(均線糾結)剛形成": { 20: 73.7 },
    "多方力道≥80 ＋ 強勢突破盤 ＋ 一字底(均線糾結)剛形成": { 20: 73.4 },
    "多方力道≥80 ＋ 相對強弱為正(強於大盤) ＋ 一字底(均線糾結)剛形成": { 20: 73.3 },
    "KDJ近3日內死亡交叉 ＋ N字底剛形成 ＋ K線橫盤的突破剛形成": { 20: 73.0 },
    "布林通道低檔(≤20%) ＋ 相對強弱為負(弱於大盤) ＋ 母子懷抱(高檔)剛形成": { 10: 72.9 },
    "多方力道≥80 ＋ 一字底(均線糾結)剛形成": { 20: 72.7 },
    "多方力道≥65 ＋ 多方力道≥80 ＋ 一字底(均線糾結)剛形成": { 20: 72.7 },
    "多方力道≥80 ＋ 布林通道高檔(≥80%) ＋ 一字底(均線糾結)剛形成": { 20: 72.7 },
    "多方力道≥80 ＋ 型態成形中 ＋ 一字底(均線糾結)剛形成": { 20: 72.7 },
    "多方力道≥80 ＋ 型態突破確認 ＋ 一字底(均線糾結)剛形成": { 20: 72.7 },
    "多方力道≥80 ＋ 型態剛形成(剛突破) ＋ 一字底(均線糾結)剛形成": { 20: 72.7 },
    "多方力道≥65 ＋ 圓弧底剛形成 ＋ 一字底(均線糾結)剛形成": { 20: 72.3 },
    "N字底剛形成 ＋ 三重底剛形成 ＋ 一字底(均線糾結)剛形成": { 5: 68.9, 10: 72.2 },
    "頭肩底剛形成 ＋ N字底剛形成 ＋ 圓弧底剛形成": { 5: 72.0 },
    "爆量(≥2倍均量) ＋ 頭肩底剛形成": { 10: 72.0, 20: 70.0 },
    "爆量(≥1.5倍均量) ＋ 爆量(≥2倍均量) ＋ 頭肩底剛形成": { 10: 72.0, 20: 70.0 },
    "爆量(≥2倍均量) ＋ 型態成形中 ＋ 頭肩底剛形成": { 10: 72.0, 20: 70.0 },
    "爆量(≥2倍均量) ＋ 型態突破確認 ＋ 頭肩底剛形成": { 10: 72.0, 20: 70.0 },
    "爆量(≥2倍均量) ＋ 型態剛形成(剛突破) ＋ 頭肩底剛形成": { 10: 72.0 },
    "布林通道低檔(≤20%) ＋ 母子懷抱(高檔)剛形成": { 10: 71.8 },
    "布林通道低檔(≤20%) ＋ 型態成形中 ＋ 母子懷抱(高檔)剛形成": { 10: 71.8 },
    "布林通道低檔(≤20%) ＋ 型態突破確認 ＋ 母子懷抱(高檔)剛形成": { 10: 71.8 },
    "布林通道低檔(≤20%) ＋ 型態剛形成(剛突破) ＋ 母子懷抱(高檔)剛形成": { 10: 71.8 },
    "多方力道≥80 ＋ 相對強弱為負(弱於大盤) ＋ 爆量(≥1.5倍均量)": { 10: 71.6 },
    "多方力道≥65 ＋ N字底剛形成 ＋ 一字底(均線糾結)剛形成": { 10: 71.4 },
    "強勢突破盤 ＋ 頭肩底剛形成 ＋ 圓弧底剛形成": { 5: 70.7 },
    "KDJ近3日內黃金交叉 ＋ 爆量(≥1.5倍均量) ＋ 頭肩底剛形成": { 20: 70.7 },
    "強勢突破盤 ＋ N字底剛形成 ＋ 一字底(均線糾結)剛形成": { 10: 70.5 },
    "布林通道低檔(≤20%) ＋ 夜星剛形成": { 5: 69.1, 10: 70.1 },
    "布林通道低檔(≤20%) ＋ 相對強弱為負(弱於大盤) ＋ 夜星剛形成": { 5: 69.8 },
    "KDJ近3日內死亡交叉 ＋ 爆量(≥2倍均量) ＋ 圓弧底剛形成": { 5: 69.7 },
    "布林通道低檔(≤20%) ＋ 型態成形中 ＋ 夜星剛形成": { 5: 69.1 },
    "布林通道低檔(≤20%) ＋ 型態突破確認 ＋ 夜星剛形成": { 5: 69.1 },
    "布林通道低檔(≤20%) ＋ 型態剛形成(剛突破) ＋ 夜星剛形成": { 5: 69.1 },
    "爆量(≥1.5倍均量) ＋ 夜星剛形成": { 5: 68.9 },
    "爆量(≥1.5倍均量) ＋ 型態成形中 ＋ 夜星剛形成": { 5: 68.9 },
    "爆量(≥1.5倍均量) ＋ 型態突破確認 ＋ 夜星剛形成": { 5: 68.9 },
    "爆量(≥1.5倍均量) ＋ 型態剛形成(剛突破) ＋ 夜星剛形成": { 5: 68.9 },
    "布林通道低檔(≤20%) ＋ 相對強弱為正(強於大盤) ＋ 爆量(≥1.5倍均量)": { 5: 68.8 },
}


def format_winrates(wr_dict):
    """把 {5:64.2, 10:67.4} 這種dict轉成 '5日64.2% / 10日67.4%' 的顯示字串"""
    if not wr_dict:
        return None
    parts = [f"{h}日{wr_dict[h]}%" for h in (5, 10, 20) if h in wr_dict]
    return " / ".join(parts) if parts else None


# ✅ 同一次2026-09-21回測的飆股搜尋結果（10日/20日，漲幅門檻30%），23組去重後
# 的高標股組合。這是「命中大行情的比例」，不是整體勝率，跟上面PINNED_COMBOS
# 是不同分類，飆股比例高不代表整體勝率高。
MOONSHOT_COMBOS = [
    ["強勢突破盤", "回後買上漲全通過", "晨星剛形成"],  # 10日15.0%
    ["多方力道≥65", "強勢突破盤", "晨星剛形成"],  # 10日10.7%
    ["布林通道低檔(≤20%)", "爆量(≥1.5倍均量)", "夜星剛形成"],  # 20日20.7%
    ["多方力道≥80", "KDJ近3日內死亡交叉", "夜星剛形成"],  # 5日7.4%，20日18.5%
    ["相對強弱為負(弱於大盤)", "爆量(≥1.5倍均量)", "夜星剛形成"],  # 20日17.1%
    ["布林通道低檔(≤20%)", "KDJ近3日內黃金交叉", "母子懷抱(高檔)剛形成"],  # 20日16.9%
    ["布林通道高檔(≥80%)", "KDJ近3日內死亡交叉", "夜星剛形成"],  # 20日16.0%
    ["相對強弱為正(強於大盤)", "夜星剛形成"],  # 20日15.8%
    ["KDJ近3日內死亡交叉", "相對強弱為正(強於大盤)", "夜星剛形成"],  # 20日15.8%
    ["KDJ近3日內死亡交叉", "母子懷抱(高檔)剛形成", "夜星剛形成"],  # 20日15.6%
    ["相對強弱為正(強於大盤)", "母子懷抱(高檔)剛形成", "夜星剛形成"],  # 20日15.4%
    ["多方力道≥80", "夜星剛形成"],  # 5日6.1%，20日15.2%
    ["KDJ近3日內黃金交叉", "母子懷抱(高檔)剛形成", "夜星剛形成"],  # 20日15.0%
    ["爆量(≥1.5倍均量)", "夜星剛形成"],  # 20日14.8%
]


MOONSHOT_COMBO_STATS = {
    "強勢突破盤 ＋ 回後買上漲全通過 ＋ 晨星剛形成": { 10: {"n": 20, "moonshot_n": 3, "pct": 15.0, "avg": 35.6} },
    "多方力道≥65 ＋ 強勢突破盤 ＋ 晨星剛形成": { 10: {"n": 28, "moonshot_n": 3, "pct": 10.7, "avg": 35.6} },
    "布林通道低檔(≤20%) ＋ 爆量(≥1.5倍均量) ＋ 夜星剛形成": { 20: {"n": 29, "moonshot_n": 6, "pct": 20.7, "avg": 50.8} },
    "多方力道≥80 ＋ KDJ近3日內死亡交叉 ＋ 夜星剛形成": { 5: {"n": 27, "moonshot_n": 2, "pct": 7.4, "avg": 37.7}, 20: {"n": 27, "moonshot_n": 5, "pct": 18.5, "avg": 46.4} },
    "相對強弱為負(弱於大盤) ＋ 爆量(≥1.5倍均量) ＋ 夜星剛形成": { 20: {"n": 35, "moonshot_n": 6, "pct": 17.1, "avg": 50.9} },
    "布林通道低檔(≤20%) ＋ KDJ近3日內黃金交叉 ＋ 母子懷抱(高檔)剛形成": { 20: {"n": 65, "moonshot_n": 11, "pct": 16.9, "avg": 42.8} },
    "布林通道高檔(≥80%) ＋ KDJ近3日內死亡交叉 ＋ 夜星剛形成": { 20: {"n": 25, "moonshot_n": 4, "pct": 16.0, "avg": 44.4} },
    "相對強弱為正(強於大盤) ＋ 夜星剛形成": { 20: {"n": 152, "moonshot_n": 24, "pct": 15.8, "avg": 44.3} },
    "KDJ近3日內死亡交叉 ＋ 相對強弱為正(強於大盤) ＋ 夜星剛形成": { 20: {"n": 101, "moonshot_n": 16, "pct": 15.8, "avg": 42.7} },
    "KDJ近3日內死亡交叉 ＋ 母子懷抱(高檔)剛形成 ＋ 夜星剛形成": { 20: {"n": 45, "moonshot_n": 7, "pct": 15.6, "avg": 51.4} },
    "相對強弱為正(強於大盤) ＋ 母子懷抱(高檔)剛形成 ＋ 夜星剛形成": { 20: {"n": 39, "moonshot_n": 6, "pct": 15.4, "avg": 48.1} },
    "多方力道≥80 ＋ 夜星剛形成": { 5: {"n": 33, "moonshot_n": 2, "pct": 6.1, "avg": 37.7}, 20: {"n": 33, "moonshot_n": 5, "pct": 15.2, "avg": 46.4} },
    "KDJ近3日內黃金交叉 ＋ 母子懷抱(高檔)剛形成 ＋ 夜星剛形成": { 20: {"n": 20, "moonshot_n": 3, "pct": 15.0, "avg": 32.0} },
    "爆量(≥1.5倍均量) ＋ 夜星剛形成": { 5: {"n": 61, "moonshot_n": 9, "pct": 14.8, "avg": 46.2} },
}


def format_moonshot_entry(e):
    """把 {"n":28,"moonshot_n":4,"pct":14.3,"avg":42.3} 轉成 '28筆中4次飆股(14.3%)，平均漲幅+42.3%'"""
    return f'{e["n"]}筆中{e["moonshot_n"]}次飆股({e["pct"]}%)，平均漲幅+{e["avg"]}%'


def format_moonshot_stats(stats_dict):
    if not stats_dict:
        return None
    parts = [f"{h}日：{format_moonshot_entry(stats_dict[h])}" for h in (10, 20) if h in stats_dict]
    return "　｜　".join(parts) if parts else None


def moonshot_combo_static_stats(combos, stats_map) -> pd.DataFrame:
    """把MOONSHOT_COMBOS固定的23組轉成表格——直接用「2026-09-21美股飆股搜尋」
    記錄下來的靜態資料，不隨目前bt_df重算（跟pinned_combo_stats不同，那個是每次
    都用目前資料現算）。美股這次飆股搜尋只跑了10日/20日，沒有5日資料。"""
    rows = []
    for combo in combos:
        key = " ＋ ".join(combo)
        stats = stats_map.get(key, {})
        row = {"條件組合": key, "條件數": len(combo)}
        for h in (10, 20):
            e = stats.get(h)
            row[f"{h}日樣本數"] = e["n"] if e else None
            row[f"{h}日飆股次數"] = e["moonshot_n"] if e else None
            row[f"{h}日飆股比例%"] = e["pct"] if e else None
            row[f"{h}日飆股平均漲幅%"] = f'+{e["avg"]}' if e else None
        rows.append(row)
    return pd.DataFrame(rows)


def compute_live_flags_row(r):
    """把批次分析結果的一列轉成「旗標列」，重複用同一套build_condition_flags判斷
    邏輯，用來做即時掃描的指定組合命中。近1季乖離度/均價YoY只用最新一季（不是像
    台股月營收那樣3個月加總）——跟回測的compute_divergence_asof_us用同一種
    「近1季」語意，這樣即時掃描的旗標判斷才會跟回測驗證出來的組合定義一致。
    美股沒有三大法人買賣超，沒有對應欄位。"""
    dm = r["dm"]
    sig = compute_core_signals(r["data"], dm)
    rel_strength_20 = compute_relative_strength(r["data"], current_benchmark, 20) if current_benchmark else None
    vol_ratio = compute_vol_ratio(r["data"])
    last_date = r["data"][-1]["date"]
    above20, above60 = compute_benchmark_regime(current_benchmark, last_date) if current_benchmark else (None, None)

    rev = r.get("revRange")
    px_range = r.get("priceYoYRange")
    div_total, price_yoy_1q = None, None
    if rev and px_range:
        latest_rev = rev[-1]
        if latest_rev.get("yoy") is not None:
            px_by_key = {(p["year"], p["quarter"]): p for p in px_range}
            px_e = px_by_key.get((latest_rev["year"], latest_rev["quarter"]))
            if px_e and px_e.get("yoy") is not None:
                price_yoy_1q = px_e["yoy"]
                div_total = latest_rev["yoy"] - price_yoy_1q

    pattern_hits = {}
    if r.get("pt") and r["pt"].get("results"):
        pattern_hits = {pr["id"]: (1 if pr.get("justBroke") else 0) for pr in r["pt"]["results"]}

    return {
        "score": dm["score"],
        "is_breakout": 1 if sig["is_breakout"] else 0,
        "is_pullback_rebound": 1 if sig["is_pullback_rebound"] else 0,
        "bb_pos": sig["bb_pos"],
        "golden_cross_recent": 1 if sig["golden_cross_recent"] else 0,
        "kdj_golden_cross_recent": 1 if sig["kdj_golden_cross_recent"] else 0,
        "kdj_death_cross_recent": 1 if sig["kdj_death_cross_recent"] else 0,
        "rel_strength_20": rel_strength_20,
        "vol_ratio": vol_ratio,
        "benchmark_above20": None if above20 is None else (1 if above20 else 0),
        "benchmark_above60": None if above60 is None else (1 if above60 else 0),
        "pattern_formed": 1 if r["pt"]["anyFormed"] else 0,
        "pattern_breakout": 1 if r["pt"]["anyBreakout"] else 0,
        "pattern_just_broke": 1 if r["pt"]["anyJustBroke"] else 0,
        "pattern_hits": pattern_hits,
        "pb_all_pass": 1 if r["pb"]["allPass"] else 0,
        "div_total": div_total, "price_yoy_1q": price_yoy_1q,
    }


def matched_pinned_combos(r):
    flag_row = compute_live_flags_row(r)
    d, flag_names = build_condition_flags(pd.DataFrame([flag_row]))
    matched = []
    for idx, combo in enumerate(PINNED_COMBOS):
        if all(name in flag_names and bool(d.iloc[0][name]) for name in combo):
            matched.append(idx)
    return matched


def matched_moonshot_combos(r):
    flag_row = compute_live_flags_row(r)
    d, flag_names = build_condition_flags(pd.DataFrame([flag_row]))
    matched = []
    for idx, combo in enumerate(MOONSHOT_COMBOS):
        if all(name in flag_names and bool(d.iloc[0][name]) for name in combo):
            matched.append(idx)
    return matched


def format_matched_combo_badges(r):
    """回傳純文字版的指定組合命中徽章（Streamlit表格欄位用），格式例如
    '#3 #7 M2'——#開頭是高勝率，M開頭是高標股。"""
    try:
        pinned_idx = matched_pinned_combos(r)
    except Exception:
        pinned_idx = []
    try:
        moonshot_idx = matched_moonshot_combos(r)
    except Exception:
        moonshot_idx = []
    if not pinned_idx and not moonshot_idx:
        return "--"
    parts = [f"#{i+1}" for i in pinned_idx] + [f"M{i+1}" for i in moonshot_idx]
    return " ".join(parts)


def pinned_combo_stats(df: pd.DataFrame, combos, target_horizon: int = 10) -> pd.DataFrame:
    """算指定條件組合的表現，不受樣本數門檻或排名影響，全部顯示。缺欄位（例如
    沒勾選對應的可選資料）或沒有符合樣本的組合，也會列出來並註明原因，而不是
    悄悄跳過讓使用者以為系統忘了測。"""
    d, flag_names = build_condition_flags(df)
    ret_col = f"ret_{target_horizon}d"
    valid = d.dropna(subset=[ret_col]) if ret_col in d.columns else d.iloc[0:0]

    rows = []
    for combo in combos:
        is_kdj_morning_star = "KDJ近3日內黃金交叉" in combo and "晨星剛形成" in combo
        label = ("⭐ " if is_kdj_morning_star else "") + " ＋ ".join(combo)
        recorded_wr = format_winrates(PINNED_COMBO_WINRATES.get(" ＋ ".join(combo)))
        missing = [c for c in combo if c not in flag_names]
        if missing:
            rows.append({"條件組合": label, "樣本數": 0, "歷史勝率(記錄)": recorded_wr,
                         f"{target_horizon}日平均報酬%": None, f"{target_horizon}日勝率%": None,
                         "備註": f"缺少欄位（可能沒勾選對應的可選資料）：{'、'.join(missing)}"})
            continue
        mask = valid[combo].all(axis=1)
        n_match = int(mask.sum())
        if n_match == 0:
            rows.append({"條件組合": label, "樣本數": 0, "歷史勝率(記錄)": recorded_wr,
                         f"{target_horizon}日平均報酬%": None, f"{target_horizon}日勝率%": None,
                         "備註": "目前回測資料裡沒有符合這個組合的樣本"})
            continue
        rets = valid.loc[mask, ret_col]
        rows.append({
            "條件組合": label, "樣本數": n_match, "歷史勝率(記錄)": recorded_wr,
            f"{target_horizon}日平均報酬%": round(rets.mean(), 2),
            f"{target_horizon}日勝率%": round((rets > 0).mean() * 100, 1),
            "備註": "" if n_match >= 50 else "⚠️樣本數偏少，僅供參考",
        })
    return pd.DataFrame(rows)


# ────────────────────────────────────────────────────────────────
# 回後買上漲 8 條件核對
# ────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────
# Streamlit UI
# ────────────────────────────────────────────────────────────────

if "stock_text_input" not in st.session_state:
    st.session_state["stock_text_input"] = "\n".join(SP500_LIST)
if "active_list" not in st.session_state:
    st.session_state.active_list = "sp500"
if "custom_lists" not in st.session_state:
    st.session_state.custom_lists = load_custom_lists()

st.title("📊 US技術分析全攻略 · 美股評分分析系統")
st.caption("個股評分改為DMI趨向指標（+DI／-DI／ADX／ADXR）多方力道評分；回後買上漲、15種進場型態辨識仍沿用朱家泓《技術分析全攻略》方法論（美股版，資料來源：Financial Modeling Prep）")


def fetch_etf_set(token):
    """抓 FMP 的 ETF 清單，回傳 symbol 的 set，抓不到就回傳空 set（不影響主要功能）。"""
    try:
        etf_resp = requests.get(f"{FMP_BASE}/etf-list", params={"apikey": token}, timeout=20)
        if etf_resp.ok:
            etf_rows = etf_resp.json()
            if isinstance(etf_rows, list):
                return {e.get("symbol") for e in etf_rows if isinstance(e, dict) and e.get("symbol")}
    except Exception:
        pass
    return set()


def fetch_top100_gainers(token):
    """今日漲幅前100：用 FMP 的 biggest-gainers 端點（Free方案可用），直接取得今日美股漲幅排行，
    並排除ETF（另查一次 FMP 的 ETF 清單，只保留不在此清單中的個股）。"""
    try:
        resp = requests.get(f"{FMP_BASE}/biggest-gainers", params={"apikey": token}, timeout=20)
        if not resp.ok:
            return []
        rows = resp.json()
        if not isinstance(rows, list):
            return []

        etf_set = fetch_etf_set(token)

        results = []
        for r in rows:
            sym = r.get("symbol") or r.get("ticker")
            if not sym or sym in etf_set:
                continue
            pct_raw = r.get("changesPercentage")
            if pct_raw is None:
                pct_raw = r.get("changePercentage")
            try:
                pct = float(pct_raw) if pct_raw is not None else None
            except (TypeError, ValueError):
                pct = None
            if pct is not None:
                results.append({"id": sym, "name": r.get("name") or sym, "pct": pct})
        results.sort(key=lambda r: r["pct"], reverse=True)
        return results[:100]
    except Exception:
        return []


def fetch_top100_volume(token):
    """今日成交量前100：用 FMP 的 most-actives 端點（Free方案可用），直接取得今日美股成交量排行，
    並排除ETF（另查一次 FMP 的 ETF 清單，只保留不在此清單中的個股）。"""
    try:
        resp = requests.get(f"{FMP_BASE}/most-actives", params={"apikey": token}, timeout=20)
        if not resp.ok:
            return []
        rows = resp.json()
        if not isinstance(rows, list):
            return []

        etf_set = fetch_etf_set(token)

        results = []
        for r in rows:
            sym = r.get("symbol") or r.get("ticker")
            if not sym or sym in etf_set:
                continue
            vol_raw = r.get("volume")
            if vol_raw is None:
                vol_raw = r.get("totalVolume")
            if vol_raw is None:
                vol_raw = r.get("avgVolume")
            try:
                vol = float(vol_raw) if vol_raw is not None else None
            except (TypeError, ValueError):
                vol = None
            results.append({"id": sym, "name": r.get("name") or sym, "volume": vol})
        # FMP 本身已依成交量排序回傳；若抓得到明確的成交量欄位就再排序一次確保正確，抓不到就信任原始順序
        if any(r["volume"] is not None for r in results):
            results.sort(key=lambda r: r["volume"] or 0, reverse=True)
        return results[:100]
    except Exception:
        return []


def fetch_nasdaq100_symbols(token):
    """Nasdaq-100 成分股：FMP 官方的 nasdaq-constituent 端點實際涵蓋的就是 Nasdaq-100
    （100檔大型非金融股），不是包含全部Nasdaq上市公司（3000+檔）的 Nasdaq Composite——
    那份清單太龐大，直接內建或批次分析都不切實際。用動態抓取（而非寫死清單），可以避免
    這種常態調整成分股的指數清單過時。回傳 (symbols, error_msg)。"""
    try:
        resp = requests.get(f"{FMP_BASE}/nasdaq-constituent", params={"apikey": token}, timeout=20)
        if not resp.ok:
            return [], f"❌ 無法取得 Nasdaq-100 清單：HTTP {resp.status_code}"
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            return [], "❌ 未取得任何資料，請確認 API Key 是否有效"
        symbols = [r.get("symbol") or r.get("ticker") for r in rows if isinstance(r, dict)]
        symbols = [s for s in symbols if s]
        if not symbols:
            return [], "❌ 資料格式無法解析，請稍後再試"
        return symbols, None
    except Exception as e:
        return [], f"❌ 抓取 Nasdaq-100 成分股清單失敗：{e}"


with st.sidebar:
    st.header("📊 US技術分析全攻略")
    st.caption("朱家泓方法論 · 美股評分系統")

    api_token = st.text_input("Financial Modeling Prep API Key", type="password", placeholder="輸入您的 FMP API Key")
    st.caption("還沒有 Key？前往 [financialmodelingprep.com](https://site.financialmodelingprep.com/developer/docs) 免費註冊（Free 方案，250 次/天）")

    st.subheader("批次股票代號")
    c1, c2, c3 = st.columns(3)
    if c1.button("S&P 500", use_container_width=True,
                  type="primary" if st.session_state.active_list == "sp500" else "secondary"):
        st.session_state["stock_text_input"] = "\n".join(SP500_LIST)
        st.session_state.active_list = "sp500"
        st.rerun()
    if c2.button("SOX半導體", use_container_width=True,
                  type="primary" if st.session_state.active_list == "sox" else "secondary"):
        st.session_state["stock_text_input"] = "\n".join(SOX_LIST)
        st.session_state.active_list = "sox"
        st.rerun()
    if c3.button("Nasdaq-100", use_container_width=True,
                  type="primary" if st.session_state.active_list == "nasdaq100" else "secondary"):
        if not api_token:
            st.session_state["top100_info"] = "❌ 請先輸入 FMP API Key 才能抓取 Nasdaq-100 成分股清單"
        else:
            with st.spinner("抓取 Nasdaq-100 成分股清單中…"):
                nasdaq100_symbols, err_msg = fetch_nasdaq100_symbols(api_token)
            if nasdaq100_symbols:
                st.session_state["stock_text_input"] = "\n".join(nasdaq100_symbols)
                st.session_state.active_list = "nasdaq100"
                st.session_state["top100_info"] = f"✅ 已套入 Nasdaq-100 成分股清單，共 {len(nasdaq100_symbols)} 檔"
            else:
                st.session_state["top100_info"] = err_msg or "❌ 未能取得 Nasdaq-100 清單，請稍後再試"
        st.rerun()

    # 我的清單／清單1／清單2／清單3：每列一個選取按鈕＋一個 × 清除按鈕
    for key in CUSTOM_LIST_KEYS:
        col_sel, col_clear = st.columns([5, 1])
        items = st.session_state.custom_lists.get(key, [])
        label = CUSTOM_LIST_LABELS[key] + (f"（{len(items)}）" if items else "")
        if col_sel.button(label, use_container_width=True, key=f"sel_{key}",
                           type="primary" if st.session_state.active_list == key else "secondary"):
            st.session_state["stock_text_input"] = "\n".join(items)
            st.session_state.active_list = key
            st.rerun()
        if col_clear.button("×", use_container_width=True, key=f"clear_{key}",
                             help=f"清除「{CUSTOM_LIST_LABELS[key]}」"):
            st.session_state.custom_lists[key] = []
            save_custom_lists(st.session_state.custom_lists)
            if st.session_state.active_list == key:
                st.session_state["stock_text_input"] = ""
            st.rerun()

    if st.session_state.get("top100_info"):
        (st.success if st.session_state["top100_info"].startswith("✅") else st.error)(
            st.session_state["top100_info"]
        )

    # ── 漲幅前100／成交量前100：實際抓資料與設定 stock_text_input 的邏輯，
    # 必須在 text_area 元件實例化「之前」執行，否則會觸發 StreamlitAPIException
    # （Streamlit 不允許在 widget 已經渲染後才設定該 widget key 對應的 session_state）。
    # 所以按鈕本身仍畫在 text_area 之後（視覺順序不變），按下後只設一個旗標並 rerun，
    # 實際抓資料的邏輯則挪到這裡、text_area 之前執行。
    if st.session_state.pop("_trigger_top100_gainers", False):
        if not api_token:
            st.session_state["top100_info"] = "❌ 請先輸入 FMP API Key 才能抓取今日漲幅排行"
        else:
            with st.spinner("抓取今日美股漲幅排行中…"):
                top100 = fetch_top100_gainers(api_token)
            if top100:
                st.session_state["stock_text_input"] = "\n".join(r["id"] for r in top100)
                st.session_state.active_list = "top100"
                st.session_state["top100_info"] = (
                    f"✅ 已取得今日漲幅前{len(top100)}"
                    f"（{top100[0]['id']} {top100[0]['name']} +{top100[0]['pct']:.2f}% 最高），正在自動開始批次分析…"
                )
                # 套入清單成功後，標記在下一次 rerun 時自動接著跑批次分析，不用使用者再按一次「批次分析」
                st.session_state["_run_after_top100"] = True
            else:
                st.session_state["top100_info"] = "❌ 未取得任何資料，請確認 API Key 是否有效"

    if st.session_state.pop("_trigger_volume100", False):
        if not api_token:
            st.session_state["top100_info"] = "❌ 請先輸入 FMP API Key 才能抓取今日成交量排行"
        else:
            with st.spinner("抓取今日美股成交量排行中…"):
                vol100 = fetch_top100_volume(api_token)
            if vol100:
                st.session_state["stock_text_input"] = "\n".join(r["id"] for r in vol100)
                st.session_state.active_list = "volume100"
                vol_text = f"量={int(vol100[0]['volume']):,}" if vol100[0]["volume"] is not None else "依FMP成交量排序"
                st.session_state["top100_info"] = (
                    f"✅ 已取得今日成交量前{len(vol100)}"
                    f"（{vol100[0]['id']} {vol100[0]['name']} {vol_text} 最高），正在自動開始批次分析…"
                )
                st.session_state["_run_after_top100"] = True
            else:
                st.session_state["top100_info"] = "❌ 未取得任何資料，請確認 API Key 是否有效"

    stock_text = st.text_area("每行一個，或逗號分隔（例：AAPL、MSFT、NVDA）",
                               height=180, key="stock_text_input")

    if st.button("💾 更新我的清單", use_container_width=True):
        stocks = parse_stock_tokens(stock_text)
        if stocks:
            st.session_state.custom_lists["my"] = stocks
            save_custom_lists(st.session_state.custom_lists)
            st.success(f"已更新我的清單（{len(stocks)}檔）")
        else:
            st.warning("批次股票代號目前是空的，沒有可儲存的內容")

    # 存為清單1／清單2／清單3：三個各自獨立的按鈕，直接存進指定槽位（不再是舊版
    # 「自動找空槽」的邏輯）。Streamlit沒有原生confirm彈窗，所以跟清單的×清除鈕一樣，
    # 按下就直接覆蓋，不會另外跳確認。
    save_cols = st.columns(3)
    for idx, key in enumerate(("my1", "my2", "my3")):
        if save_cols[idx].button(f"➕ 存為清單{idx+1}", use_container_width=True, key=f"save_{key}"):
            stocks = parse_stock_tokens(stock_text)
            if stocks:
                st.session_state.custom_lists[key] = stocks
                save_custom_lists(st.session_state.custom_lists)
                st.success(f"已存入{CUSTOM_LIST_LABELS[key]}（{len(stocks)}檔）")
            else:
                st.warning("批次股票代號目前是空的，沒有可儲存的內容")

    days = st.slider("分析天數", min_value=90, max_value=365, value=180, step=30)

    st.caption("以下每勾一項，批次分析都會為每檔股票多打1次API，股票數多時會明顯變慢：")
    show_pe = st.checkbox("📐 近3年P/E區間", value=False)
    show_rev = st.checkbox("📈 近3季營收YoY/QoQ", value=False)
    show_pxyoy = st.checkbox("💹 近3季均價YoY（可獨立勾選；若同時勾營收，季度會對齊營收那組）", value=False)

    run_clicked = st.button("🔍 批次分析", type="primary", use_container_width=True)

    with st.expander("🔬 歷史回測分析（事後驗證＋參數優化，美股／S&P500）"):
        months_back_bt = st.number_input("回測天數（月）", min_value=1, max_value=36, value=3, step=1, key="bt_months_back_us")
        st.caption(
            "回溯過去N個月，用S&P500清單重新計算每個交易日『當時』的DMI/多方力道評分"
            "（只用當天以前的資料，沒有偷看未來），對照5/10/20個交易日後的實際報酬，"
            "驗證現有評分公式準不準，並試算不同參數組合的效果。月數愈大，跑的時間愈長，"
            "拉到36個月時評估紀錄可能超過幾十萬筆，跑完可能要1小時以上，建議先從6-12"
            "個月試跑，確認可行再拉長。過程中請勿切換分頁或關閉視窗。分析結果會顯示在"
            "右側主畫面。"
        )
        st.caption("型態確認、回後買上漲不需要額外API，一律會記錄。以下這項要多抓一組資料，會拉長時間：")
        include_div_bt = st.checkbox("📈 近1季YoY乖離度（需要超過1年的股價歷史，明顯拉長抓取時間）", value=False, key="bt_include_div_us")
        st.caption("美股沒有三大法人買賣超這種資料，這裡跟台股版不同，沒有對應的選項。")
        min_liquidity_wan_bt = st.number_input(
            "最低近20日均成交金額（萬美元，0＝不篩選）", min_value=0, value=0, step=100, key="bt_min_liquidity_us"
        )
        st.caption(
            "排除成交量太小的股票：每個評估點各自檢查『當時』往前20天的平均成交金額"
            "（收盤價×成交量），低於門檻就跳過那個評估點——不是只看現在，避免用現在的"
            "流動性去篩過去的資料。"
        )
        backtest_clicked_us = st.button("🔬 執行歷史回測", use_container_width=True, key="btn_backtest_us")

    top100_clicked = st.button("🔥 漲幅前100分析", use_container_width=True)
    if top100_clicked:
        st.session_state["_trigger_top100_gainers"] = True
        st.rerun()

    volume100_clicked = st.button("📊 成交量前100分析", use_container_width=True)
    if volume100_clicked:
        st.session_state["_trigger_volume100"] = True
        st.rerun()

    st.divider()
    openai_key = st.text_input("OpenAI API Key（選填）", type="password", placeholder="sk-...")
    st.caption("用於「🤖 AI 智能綜合分析」，金鑰只會從您的本機直接呼叫 OpenAI，不會被儲存或上傳到任何伺服器。")
    openai_model = st.selectbox("AI 模型", ["gpt-4o-mini", "gpt-4o"],
                                 format_func=lambda x: {"gpt-4o-mini": "gpt-4o-mini（快速／經濟）",
                                                         "gpt-4o": "gpt-4o（進階／較貴）"}[x])

    st.divider()
    st.markdown("**評分維度各25分**")
    st.caption("📈 趨勢分析（轉折波）")
    st.caption("🕯️ K線型態分析")
    st.caption("📊 均線系統分析")
    st.caption("📦 成交量分析")
    st.markdown("**進場判斷**")
    st.caption("🟢 80+ 積極做多")
    st.caption("🔵 65-79 可考慮進場")
    st.caption("🟡 50-64 觀望")
    st.caption("🔴 <50 不適合進場")


# ────────────────────────────────────────────────────────────────
# 批次分析主流程
# ────────────────────────────────────────────────────────────────

def run_batch_analysis():
    if not api_token:
        st.error("請輸入 Financial Modeling Prep API Key")
        return

    stocks, seen = [], set()
    for tok in st.session_state["stock_text_input"].replace("，", ",").replace("、", ",").split():
        for s in tok.split(","):
            s = s.strip().upper()
            if s and s not in seen:
                seen.add(s)
                stocks.append(s)
    if not stocks:
        st.error("請輸入至少一個股票代號")
        return

    status = st.empty()
    progress_bar = st.progress(0.0)
    log_box = st.container()

    status.text("🔌 測試 API 連線中...")
    try:
        test = api_fetch(f"{FMP_BASE}/profile?symbol=AAPL&apikey={api_token}")
        if not isinstance(test, list) or not test:
            st.error("API Key 無效或額度已用完，請確認 FMP API Key 是否正確。")
            return
    except Exception as e:
        st.error(f"❌ 無法連線至 Financial Modeling Prep API\n\n錯誤：{e}\n\n請確認：\n1. API Key 是否正確\n2. 網路連線是否正常")
        return

    # 相對強弱要用的大盤(SPY)資料，整批只抓一次，不是每檔股票各抓一次
    global current_benchmark
    current_benchmark = None
    try:
        current_benchmark = fetch_benchmark_series(api_token, days)
    except Exception as e:
        current_benchmark = None
        st.warning(
            f"⚠️ 這次抓不到大盤(SPY)資料，「相對強弱」欄位這次不會出現（不影響其他"
            f"分析）。錯誤訊息：{e}　常見原因：FMP方案不支援ETF資料、API額度用完、"
            f"或SPY這個代號被擋。"
        )

    batch_results = []
    total = len(stocks)
    for i, sid in enumerate(stocks):
        status.text(f"📡 分析 {sid}… ({i + 1}/{total})")
        try:
            rows = fetch_price_data(sid, api_token, days)
            raw_data = sorted(
                [{"date": d["date"], "open": float(d["open"]), "high": float(d["high"]),
                  "low": float(d["low"]), "close": float(d["close"]), "volume": float(d.get("volume") or 0)}
                 for d in rows],
                key=lambda x: x["date"],
            )
            name = fetch_company_name(api_token, sid)
            pe_range, pe_range_err = None, None
            if show_pe:
                try:
                    pe_range = fetch_pe_range_us(api_token, sid, 3)
                except Exception as pe_err:
                    pe_range_err = str(pe_err)
            rev_range = None
            if show_rev:
                try:
                    rev_range = fetch_revenue_yoy_qoq_us(api_token, sid)
                except Exception:
                    pass  # 營收抓不到就顯示無資料，不影響其他分析
            price_yoy_range = None
            if show_pxyoy:
                try:
                    price_yoy_range = fetch_quarterly_avg_price_yoy_us(api_token, sid, rev_range)
                except Exception:
                    pass  # 均價YoY抓不到就顯示無資料，不影響其他分析
            data = enrich(raw_data)
            dmi = calc_dmi(data, 14)
            dm = score_dmi(dmi)
            pb = check_pullback_buy(data)
            pt = detect_patterns(data, pb)
            total_score = dm["score"]
            batch_results.append({"stockId": sid, "name": name, "data": data, "dm": dm,
                                   "pb": pb, "pt": pt, "total": total_score,
                                   "peRange": pe_range, "peRangeErr": pe_range_err,
                                   "revRange": rev_range, "priceYoYRange": price_yoy_range})
            with log_box:
                if pe_range_err:
                    st.caption(f"⚠️ {sid} P/E區間讀取失敗：{pe_range_err}")
                st.caption(f"✅ {sid} {name}　得分:{total_score}")
        except Exception as ex:
            with log_box:
                st.caption(f"❌ {sid} 失敗：{ex}")
        progress_bar.progress((i + 1) / total)
        if i < total - 1:
            time.sleep(0.3)

    progress_bar.empty()
    status.empty()

    if not batch_results:
        st.error("所有股票均無法取得資料")
        return

    batch_results.sort(key=lambda r: r["total"], reverse=True)
    st.session_state.batch_results = batch_results
    st.session_state.selected_stock_idx = 0


if run_clicked or st.session_state.pop("_run_after_top100", False):
    run_batch_analysis()

# ────────────────────────────────────────────────────────────────
# 歷史回測分析結果（美股／S&P500）——觸發鈕在側邊欄，實際執行跟結果顯示都
# 放在主畫面，表格才有足夠寬度顯示。
# ────────────────────────────────────────────────────────────────
if backtest_clicked_us:
    if not api_token:
        st.error("請輸入 Financial Modeling Prep API Key")
    else:
        run_historical_backtest(api_token, months_back=int(months_back_bt),
                                 include_div=include_div_bt,
                                 min_liquidity=float(min_liquidity_wan_bt) * 10000)

if "batch_results" not in st.session_state:
    st.info("📈 請在左側輸入 Financial Modeling Prep API Key 與美股代號，點擊「批次分析」即可開始。")
else:
    batch_results = st.session_state.batch_results

    st.markdown("### 📋 批次分析摘要")

    with st.expander(f"📖「指定組合命中」編號對照 — 高勝率 {len(PINNED_COMBOS)}組／高標股 {len(MOONSHOT_COMBOS)}組（點開查看完整條件）"):
        st.markdown("**⬥ 高勝率（#編號）**")
        if PINNED_COMBOS:
            for idx, combo in enumerate(PINNED_COMBOS):
                key = " ＋ ".join(combo)
                wr_txt = format_winrates(PINNED_COMBO_WINRATES.get(key))
                st.markdown(f"`#{idx+1}` {key}" + (f"　（歷史勝率：{wr_txt}）" if wr_txt else ""))
        else:
            st.caption("目前清單是空的。")
        st.markdown("**⬥ 高標股（M編號，容易命中大行情，不代表整體勝率高）**")
        if MOONSHOT_COMBOS:
            for idx, combo in enumerate(MOONSHOT_COMBOS):
                key = " ＋ ".join(combo)
                stats_txt = format_moonshot_stats(MOONSHOT_COMBO_STATS.get(key))
                st.markdown(f"`M{idx+1}` {key}" + (f"　（{stats_txt}）" if stats_txt else ""))
        else:
            st.caption("目前清單是空的。")

    fcol1, fcol2, fcol3, fcol4 = st.columns([1, 1, 1, 1.4])
    with fcol1:
        pb_filter = st.selectbox("進場條件", ["全部", "✅ 符合進場", "❌ 不符合"], key="pb_filter")
    with fcol2:
        score_filter = st.selectbox("評分", ["全部", "80+", "65-79", "50-64", "<50"], key="score_filter")
    with fcol3:
        pt_filter = st.selectbox("型態確認", ["全部", "✅ 已突破", "🔥 剛突破", "🕒 成形中", "－ 無"], key="pt_filter")
    with fcol4:
        kw = st.text_input("搜尋代號/名稱", key="kw_filter", placeholder="輸入代號或名稱關鍵字")

    def build_summary_row(i, r):
        score_lbl = "積極做多" if r["total"] >= 80 else "可考慮進場" if r["total"] >= 65 else "觀望" if r["total"] >= 50 else "不建議進場"
        pb_txt = "符合進場" if r["pb"]["allPass"] else f"{r['pb']['requiredPassed']}/{r['pb']['requiredTotal']} 通過"
        pt_names = [("🔥" if x["justBroke"] else "") + x["name"] for x in r["pt"]["results"]
                    if (x["breakout"] if r["pt"]["anyBreakout"] else x["formed"])]
        pt_txt = "、".join(pt_names) if pt_names else "無"
        pt_icon = "🔥" if r["pt"]["anyJustBroke"] else ("✅" if r["pt"]["anyBreakout"] else ("🕒" if r["pt"]["anyFormed"] else "－"))
        last = r["data"][-1]
        prev = r["data"][-2] if len(r["data"]) >= 2 else last
        chgp = (last["close"] - prev["close"]) / prev["close"] * 100 if prev["close"] else 0

        pe = r.get("peRange")
        pe_txt = "無資料"
        if pe:
            pe_txt = f"{pe['current']:.1f}（{pe['min']:.1f}~{pe['max']:.1f}）"
        elif r.get("peRangeErr"):
            pe_txt = f"⚠️ 讀取失敗：{r['peRangeErr']}"

        def fmt_q_pct(entries, field):
            if not entries:
                return "無資料"
            latest = entries[-1]
            v = latest.get(field)
            main = f"Q{latest['quarter']} {'+' if v is not None and v >= 0 else ''}{v:.1f}%" if v is not None else f"Q{latest['quarter']} N/A"
            prior = list(reversed(entries[:-1]))
            sub_parts = []
            for e in prior:
                ev = e.get(field)
                sub_parts.append(f"Q{e['quarter']} " + (f"{'+' if ev>=0 else ''}{ev:.1f}%" if ev is not None else "N/A"))
            return main + ("　" + "　".join(sub_parts) if sub_parts else "")

        rev = r.get("revRange")
        px_range = r.get("priceYoYRange")
        yoy_txt = fmt_q_pct(rev, "yoy")
        qoq_txt = fmt_q_pct(rev, "qoq")
        pxyoy_txt = fmt_q_pct(px_range, "yoy")

        # YoY乖離度＝營收YoY − 均價YoY，3季都算（不是只算最新季）。
        # 用(year,quarter)配對，不用陣列位置對應，避免兩邊季度萬一沒對齊時算錯。
        # 主要顯示改成「近3季加總乖離度」（3季各自的乖離度加總）——單一季度容易受
        # 單季雜訊干擾，加總後比較能看出持續性的乖離趨勢。3季各自的乖離度數字還是
        # 保留顯示（在加總值下面）。正值大＝營收成長比股價快；負值大＝股價漲幅超前
        # 營收成長。不用多打API，純算既有資料。
        div_txt = "需同時勾營收與均價YoY"
        if rev and px_range:
            px_by_key = {(p["year"], p["quarter"]): p for p in px_range}
            div_list = []
            for rv_e in rev:
                px_e = px_by_key.get((rv_e["year"], rv_e["quarter"]))
                d = (rv_e["yoy"] - px_e["yoy"]) if (rv_e.get("yoy") is not None and px_e and px_e.get("yoy") is not None) else None
                div_list.append({"quarter": rv_e["quarter"], "div": d})

            def fmt_div_q(d):
                return f"{'+' if d>=0 else ''}{d:.1f}pp" if d is not None else "N/A"

            valid_divs_q = [d["div"] for d in div_list if d["div"] is not None]
            if valid_divs_q:
                div_total_q = sum(valid_divs_q)
                lbl = ("💚 營收優於股價" if div_total_q > 45
                       else "⚠️ 股價超前營收" if div_total_q < -45 else "大致同步")
                main = f"近3季合計 {fmt_div_q(div_total_q)}　{lbl}"
            else:
                main = "當季資料不足"
            sub_parts = [f"Q{d['quarter']} {fmt_div_q(d['div'])}" for d in reversed(div_list)]
            div_txt = main + ("　" + "　".join(sub_parts) if sub_parts else "")

        dm = r["dm"]
        # 布林通道股價位置：跟個股詳細面板用同一套算法（不用多打API，last["bbU"]/
        # last["bbL"] 在enrich()時就已經算好了）。0%=貼著下軌，100%=貼著上軌，
        # 50%=通道中央。
        bb_pos_txt = "無資料"
        if last.get("bbU") is not None and last.get("bbL") is not None:
            bb_width = last["bbU"] - last["bbL"]
            bb_pos = ((last["close"] - last["bbL"]) / bb_width * 100) if bb_width else 50
            bb_width_pct = (bb_width / last["close"] * 100) if last["close"] else 0
            bb_pos_txt = f"{bb_pos:.0f}%（寬度{bb_width_pct:.1f}%）"

        # ── MACD狀態 ＋ 布林通道×MACD 情境判斷（強勢突破盤／跌深反彈盤）──
        # 強勢突破盤＝通道開口放大＋股價貼近上軌＋MACD零軸上紅柱持續增長；
        # 跌深反彈盤＝股價貼近下軌＋MACD低檔背離＋（近期）黃金交叉。
        # 低檔背離是真的背離偵測（跟型態辨識、圖表轉折波同一套 build_zigzag
        # 找最近兩個轉折低點比較），不是代理指標。
        macd_state_txt = "無資料"
        combo_tag = ""
        if (last.get("macd") is not None and last.get("macdSig") is not None
                and last.get("macdHist") is not None and prev.get("macdHist") is not None):
            above_zero = last["macd"] > 0
            hist_growing = last["macdHist"] > prev["macdHist"]
            just_golden_cross = (prev.get("macd") is not None and prev.get("macdSig") is not None
                                  and prev["macd"] <= prev["macdSig"] and last["macd"] > last["macdSig"])
            golden_cross_recent = False
            rdata = r["data"]
            for gci in range(max(1, len(rdata) - 3), len(rdata)):
                gc_cur, gc_prev = rdata[gci], rdata[gci - 1]
                if (gc_cur.get("macd") is not None and gc_cur.get("macdSig") is not None
                        and gc_prev.get("macd") is not None and gc_prev.get("macdSig") is not None
                        and gc_prev["macd"] <= gc_prev["macdSig"] and gc_cur["macd"] > gc_cur["macdSig"]):
                    golden_cross_recent = True
                    break

            if above_zero and last["macdHist"] > 0:
                macd_state_txt = "零軸上・紅柱增長" if hist_growing else "零軸上・紅柱縮短"
            elif not above_zero and last["macdHist"] < 0:
                macd_state_txt = "零軸下・綠柱縮短" if hist_growing else "零軸下・綠柱增長"
            else:
                macd_state_txt = "交叉轉換中"
            if just_golden_cross:
                macd_state_txt += "　⚡剛黃金交叉"

            width_expanding = False
            if last.get("bbU") is not None and last.get("bbL") is not None:
                width_now_pct = (last["bbU"] - last["bbL"]) / last["close"] * 100 if last["close"] else 0
                ref_idx = len(rdata) - 6
                ref_bar = rdata[ref_idx] if ref_idx >= 0 else None
                if ref_bar and ref_bar.get("bbU") is not None and ref_bar.get("bbL") is not None and ref_bar["close"]:
                    width_ref_pct = (ref_bar["bbU"] - ref_bar["bbL"]) / ref_bar["close"] * 100
                    width_expanding = width_now_pct > width_ref_pct
            bb_pos_for_combo = None
            if last.get("bbU") is not None and last.get("bbL") is not None and (last["bbU"] - last["bbL"]):
                bb_pos_for_combo = (last["close"] - last["bbL"]) / (last["bbU"] - last["bbL"]) * 100

            is_breakout = (bb_pos_for_combo is not None and bb_pos_for_combo >= 80 and width_expanding
                           and above_zero and last["macdHist"] > 0 and hist_growing)

            divergence_detected = False
            zz_for_div = build_zigzag(rdata)
            zz_lows = [p for p in zz_for_div if p["type"] == "L"]
            if len(zz_lows) >= 2:
                recent_low, prior_low = zz_lows[-1], zz_lows[-2]
                within_lookback = recent_low["idx"] >= len(rdata) - 1 - 60
                macd_at_recent = rdata[recent_low["idx"]].get("macd") if recent_low["idx"] < len(rdata) else None
                macd_at_prior = rdata[prior_low["idx"]].get("macd") if prior_low["idx"] < len(rdata) else None
                if within_lookback and macd_at_recent is not None and macd_at_prior is not None:
                    price_lower_low = recent_low["price"] < prior_low["price"]
                    macd_higher_low = macd_at_recent > macd_at_prior
                    divergence_detected = price_lower_low and macd_higher_low

            is_pullback_rebound = (bb_pos_for_combo is not None and bb_pos_for_combo <= 20
                                    and divergence_detected and golden_cross_recent)

            if is_breakout:
                combo_tag = "　🚀 強勢突破盤"
            elif is_pullback_rebound:
                combo_tag = "　🎯 跌深反彈盤"

        # 相對強弱(vs大盤SPY,20日) + 成交量確認(量比)
        rs20 = compute_relative_strength(r["data"], current_benchmark, 20) if current_benchmark else None
        rs_txt = f"RS{'+' if rs20 >= 0 else ''}{rs20:.1f}%" if rs20 is not None else "RS無資料"
        v_ratio = compute_vol_ratio(r["data"])
        vol_txt = f"量比{v_ratio:.1f}x" + (" 🔥" if v_ratio is not None and v_ratio >= 1.5 else "") if v_ratio is not None else ""
        rs_vol_txt = rs_txt + ("　" + vol_txt if vol_txt else "")

        row = {
            "_idx": i, "股票": f"{r['stockId']} {r['name']}", "多方力道": r["total"], "評等": score_lbl,
            "+DI": round(dm["plusDI"], 1) if dm["plusDI"] is not None else None,
            "-DI": round(dm["minusDI"], 1) if dm["minusDI"] is not None else None,
            "ADX": round(dm["adx"], 1) if dm["adx"] is not None else None,
            "ADXR": round(dm["adxr"], 1) if dm["adxr"] is not None else None,
            "布林通道位置": bb_pos_txt,
            "MACD狀態": macd_state_txt + combo_tag,
            "相對強弱/量比": rs_vol_txt,
            "指定組合命中": format_matched_combo_badges(r),
            "漲跌%": round(chgp, 2), "收盤": f"${last['close']:.2f}",
        }
        if show_pe:
            row["近3年P/E區間"] = pe_txt
        if show_rev:
            row["近3季營收YoY"] = yoy_txt
        if show_pxyoy:
            row["近3季均價YoY"] = pxyoy_txt
        if show_rev and show_pxyoy:
            row["近3季YoY乖離度"] = div_txt
        if show_rev:
            row["近3季營收QoQ"] = qoq_txt
        row["回後買進場"] = ("✅ " if r["pb"]["allPass"] else "❌ ") + pb_txt
        row["型態確認"] = f"{pt_icon} {pt_txt}"
        return row

    def row_passes_filter(r):
        ok_pb = pb_filter == "全部" or (pb_filter == "✅ 符合進場" and r["pb"]["allPass"]) or (pb_filter == "❌ 不符合" and not r["pb"]["allPass"])
        ok_score = (score_filter == "全部"
                    or (score_filter == "80+" and r["total"] >= 80)
                    or (score_filter == "65-79" and 65 <= r["total"] < 80)
                    or (score_filter == "50-64" and 50 <= r["total"] < 65)
                    or (score_filter == "<50" and r["total"] < 50))
        ok_kw = (not kw) or (kw in r["stockId"]) or (kw in r["name"])
        ok_pt = (pt_filter == "全部"
                 or (pt_filter == "✅ 已突破" and r["pt"]["anyBreakout"])
                 or (pt_filter == "🔥 剛突破" and r["pt"]["anyJustBroke"])
                 or (pt_filter == "🕒 成形中" and r["pt"]["anyFormed"] and not r["pt"]["anyBreakout"])
                 or (pt_filter == "－ 無" and not r["pt"]["anyFormed"]))
        return ok_pb and ok_score and ok_kw and ok_pt

    filtered_indices = [i for i, r in enumerate(batch_results) if row_passes_filter(r)]
    st.caption(f"顯示 {len(filtered_indices)} / {len(batch_results)} 檔")

    summary_rows = [build_summary_row(i, batch_results[i]) for i in filtered_indices]
    df_summary = pd.DataFrame(summary_rows)

    if df_summary.empty:
        st.info("沒有符合篩選條件的股票。")
    else:
        st.dataframe(
            df_summary.drop(columns=["_idx"]), use_container_width=True, hide_index=True, height=360,
        )

        # 匯出Excel：跟畫面上的表格一致——目前的篩選、排序後的資料都會反映在匯出結果裡。
        # 需要 openpyxl 套件（pandas寫.xlsx用的引擎），環境裡沒裝的話這裡會噴錯，
        # 跑 `pip install openpyxl` 補上即可。
        try:
            excel_buf = io.BytesIO()
            df_summary.drop(columns=["_idx"]).to_excel(excel_buf, index=False, engine="openpyxl", sheet_name="批次分析摘要")
            st.download_button(
                "📥 匯出 Excel",
                data=excel_buf.getvalue(),
                file_name=f"批次分析摘要_{datetime.today().strftime('%Y-%m-%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except ImportError:
            st.caption("⚠️ 匯出Excel需要 openpyxl 套件，請先執行 `pip install openpyxl`")

        # 選擇要看詳細分析的股票（取代原本 HTML 版的分頁 tab）
        options = [f"{r['stockId']} {r['name']}（{r['total']}分）" for r in batch_results]
        default_idx = st.session_state.get("selected_stock_idx", 0)
        selected_label = st.selectbox("選擇個股查看詳細分析", options, index=default_idx, key="stock_selector")
        sel_idx = options.index(selected_label)
        st.session_state.selected_stock_idx = sel_idx
        r = batch_results[sel_idx]

        # ── 個股詳細分析 ──
        total = r["total"]
        if total >= 80:
            vc, vt = "#00c864", "強力買進訊號"
        elif total >= 65:
            vc, vt = "#2196f3", "可考慮進場"
        elif total >= 50:
            vc, vt = "#f0a500", "觀望為主"
        else:
            vc, vt = "#ff3c3c", "不建議進場"

        last = r["data"][-1]
        prev = r["data"][-2] if len(r["data"]) >= 2 else last
        chg = last["close"] - prev["close"]
        chgp = (chg / prev["close"] * 100) if prev["close"] else 0

        st.markdown(f"## 🏷️ {r['stockId']} {r['name']}")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("最新收盤", f"${last['close']:.2f}", f"{chg:+.2f} ({chgp:+.2f}%)")
        m2.metric("當日成交量", f"{last['volume']:,.0f} 股")
        vr = (last["volume"] / last["vm20"]) if last["vm20"] else 1
        m3.metric("量比 vs MA20", f"{vr:.2f}x", "放量" if vr > 1.2 else ("縮量" if vr < 0.8 else "正常"))
        m4.metric("資料日期", last["date"])

        try:
            pinned_idx = matched_pinned_combos(r)
        except Exception:
            pinned_idx = []
        try:
            moonshot_idx = matched_moonshot_combos(r)
        except Exception:
            moonshot_idx = []
        if pinned_idx or moonshot_idx:
            st.markdown("##### 🎯 指定組合命中細節")
            for idx in pinned_idx:
                combo = PINNED_COMBOS[idx]
                key = " ＋ ".join(combo)
                wr_txt = format_winrates(PINNED_COMBO_WINRATES.get(key))
                st.success(f"**#{idx+1}** {key}" + (f"　（歷史勝率：{wr_txt}）" if wr_txt else ""))
            for idx in moonshot_idx:
                combo = MOONSHOT_COMBOS[idx]
                key = " ＋ ".join(combo)
                stats_txt = format_moonshot_stats(MOONSHOT_COMBO_STATS.get(key))
                st.warning(f"**M{idx+1}** {key}" + (f"　（{stats_txt}）" if stats_txt else ""))

        st.divider()
        st.markdown("### 📊 多方力道評分")
        sc1, sc2 = st.columns([1, 2])
        with sc1:
            st.markdown(
                f"<div style='text-align:center;background:linear-gradient(135deg,#1a1a2e,#16213e);"
                f"border-radius:16px;padding:28px 16px;border:1px solid rgba(255,255,255,.1)'>"
                f"<div style='font-size:64px;font-weight:700;color:{vc}'>{total}</div>"
                f"<div style='font-size:12px;color:#888;margin-top:6px'>多方力道 / 100</div>"
                f"<div style='margin-top:12px;display:inline-block;padding:7px 16px;border-radius:8px;"
                f"background:{vc}22;color:{vc};font-weight:700'>{vt}</div></div>",
                unsafe_allow_html=True,
            )
        with sc2:
            dm = r["dm"]
            dims = [("🧭 方向性 (+DI vs -DI)", dm["diPts"], 50, "+DI相對-DI的優勢程度"),
                    ("💪 趨勢強度 (ADX)", dm["adxPts"], 30, "ADX值，越高趨勢越明確"),
                    ("🚀 趨勢動能 (ADX vs ADXR)", dm["adxrPts"], 20, "ADX>ADXR代表趨勢正在轉強")]
            for t, score_val, max_val, d in dims:
                pct = score_val / max_val * 100 if max_val else 0
                col = "#00c864" if pct >= 70 else "#f0a500" if pct >= 40 else "#ff3c3c"
                st.markdown(
                    f"<div style='background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);"
                    f"border-radius:10px;padding:10px 14px;margin-bottom:6px'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                    f"<div><div style='font-size:12px;color:#888'>{t}</div><div style='font-size:11px;color:#666'>{d}</div></div>"
                    f"<div style='font-size:22px;font-weight:700;color:{col}'>{score_val:.1f}<span style='font-size:11px;color:#555'>/{max_val}</span></div>"
                    f"</div><div style='background:rgba(255,255,255,.05);border-radius:4px;height:5px;margin-top:6px'>"
                    f"<div style='background:{col};border-radius:4px;height:5px;width:{pct}%'></div></div></div>",
                    unsafe_allow_html=True,
                )
            if dm["plusDI"] is not None:
                st.caption(f"+DI={dm['plusDI']:.1f}　-DI={dm['minusDI']:.1f}　"
                           f"ADX={dm['adx']:.1f}　ADXR={dm['adxr']:.1f}" if dm["adx"] is not None and dm["adxr"] is not None
                           else f"+DI={dm['plusDI']:.1f}　-DI={dm['minusDI']:.1f}")

        st.divider()
        st.markdown("### 🔔 DMI訊號")
        dm_sigs = r["dm"]["sigs"]
        if dm_sigs:
            for label, kind in dm_sigs:
                color = "#00c864" if kind == "bull" else "#ff5555" if kind == "bear" else "#aaa"
                st.markdown(f"<span style='display:inline-block;padding:2px 8px;border-radius:12px;"
                            f"font-size:11px;color:{color};border:1px solid {color}55;margin:2px'>{label}</span>",
                            unsafe_allow_html=True)
        else:
            st.caption("無明顯訊號")

        st.divider()
        st.markdown("### 💡 操作建議")
        dm = r["dm"]
        if total >= 80:
            act, adv = "🟢 積極做多", [f"**{r['name']}** 多方力道評分 {total} 分，DMI顯示多方力道強勁，建議積極做多。"]
            if dm["tdir"] == "多頭":
                adv.append("+DI大於-DI且趨勢轉強，順勢操作，逢低分批佈局。")
            ma20v = last["ma20"] or last["close"]
            bb_up = last["bbU"] or last["close"] * 1.1
            sl = f"停損設於 **${ma20v * 0.97:.2f}**（20MA下方3%）"
            tgt = f"目標參考 **${last['close'] * ((bb_up - last['close']) / last['close'] + 1):.2f}**（布林上軌）"
        elif total >= 65:
            act, adv = "🔵 可考慮進場", [f"**{r['name']}** 評分 {total} 分，DMI偏多，可考慮分批進場。", "建議等待回測均線後再進場，降低風險。"]
            ma20v = last["ma20"] or last["close"]
            sl = f"停損建議 **${ma20v * 0.98:.2f}**（20MA下方2%）"
            tgt = f"短線目標 **${last['close'] * 1.08:.2f}**（+8%）"
        elif total >= 50:
            act, adv = "🟡 觀望為主", [f"**{r['name']}** 評分 {total} 分，DMI訊號混雜或趨勢不明確，建議觀望。", "等待ADX轉強或+DI/-DI方向明確後再行動。"]
            sl, tgt = "暫不建議進場", "等待更佳時機"
        else:
            act, adv = "🔴 不適合進場", [f"**{r['name']}** 評分 {total} 分，DMI偏空，不建議進場。"]
            if dm["tdir"] == "空頭":
                adv.append("目前-DI大於+DI，空方力道較強，切忌逆勢做多，等待趨勢反轉。")
            else:
                adv.append("DMI指標偏弱或資料不足，應持現金等待機會。")
            sl, tgt = "持倉者建議設停損出場", "等待多頭訊號出現"

        hints = [s[0] for s in dm["sigs"] if s[1] == "bull"][:5]
        st.markdown(f"**{act}**")
        for line in adv:
            st.markdown(line)
        st.markdown(f"🛑 **停損：**{sl}")
        st.markdown(f"🎯 **目標：**{tgt}")
        if hints:
            st.markdown("✅ **多頭訊號：**" + " · ".join(hints))

        if st.button("🤖 AI 智能綜合分析", key=f"ai_btn_{r['stockId']}"):
            if not openai_key:
                st.warning("請先在左側輸入 OpenAI API Key，才能使用 AI 智能綜合分析。")
            else:
                with st.spinner("正在請 AI 綜合研判技術面數據，請稍候…"):
                    ai_text = run_ai_analysis(r, openai_key, openai_model)
                st.markdown(ai_text)

        if st.button("🏦 載入分析師評等／目標價／內部人交易", key=f"fund_btn_{r['stockId']}"):
            if not api_token:
                st.warning("請先在左側輸入 FMP API Key。")
            else:
                with st.spinner("正在抓取分析師評等、目標價與內部人交易資料…"):
                    render_analyst_fundamentals(api_token, r["stockId"])

        st.divider()
        st.markdown("### 🎯 回後買上漲 · 進場條件核對")
        if r["pb"]["allPass"]:
            st.success(f"✅ 符合進場條件（必要條件全部通過）" + ("　+成交量增加分" if r["pb"]["bonusPassed"] else ""))
        else:
            st.error(f"❌ 不符合進場條件（必要條件 {r['pb']['requiredPassed']}/{r['pb']['requiredTotal']} 通過）")
        for cond in r["pb"]["results"]:
            icon = "✅" if cond["pass"] else ("❌" if cond["required"] else "—")
            tag = "" if cond["required"] else " `加分`"
            detail = f"　*(​{cond['detail']})*" if cond.get("detail") else ""
            st.markdown(f"{icon} {cond['label']}{tag}{detail}")

        st.divider()
        st.markdown("### 🔍 型態確認（15種進場型態）")
        if r["pt"]["anyJustBroke"]:
            st.warning("🔥 偵測到剛突破買點（較前一交易日新增）")
        elif r["pt"]["anyBreakout"]:
            st.success("✅ 偵測到型態突破買點")
        elif r["pt"]["anyFormed"]:
            st.info("🕒 型態成形中，尚未突破確認")
        else:
            st.caption("－ 目前未偵測到符合的進場型態")
        for p in r["pt"]["results"]:
            icon = "🔥" if p["justBroke"] else ("✅" if p["breakout"] else ("🕒" if p["formed"] else "－"))
            st.markdown(f"{icon} **{p['name']}**　_{p['desc']}_")
            if p.get("detail"):
                st.caption(p["detail"])

        st.divider()
        st.markdown("### 📐 指標對照")
        ic1, ic2, ic3 = st.columns(3)
        with ic1:
            st.write("**RSI 指標**")
            rv = last["rsi"]
            if rv is not None:
                st.metric("RSI", f"{rv:.1f}")
                st.caption("⚠️ 超買區（>70），注意回調" if rv > 70 else ("💚 超賣區（<30），留意反彈" if rv < 30 else "位於正常區間（30-70）"))
        with ic2:
            st.write("**KD 指標**")
            if last["kdK"] is not None and last["kdD"] is not None:
                kd_color = "🟢" if last["kdK"] > last["kdD"] else "🔴"
                st.markdown(f"K=**{last['kdK']:.1f}**　D=**{last['kdD']:.1f}** {kd_color}")
                st.caption("K>D 偏多" if last["kdK"] > last["kdD"] else "K<D 偏空")
            else:
                st.caption("資料不足")
        with ic3:
            st.write("**均線對照**")
            ma_rows = []
            for label, key in [("MA5", "ma5"), ("MA10", "ma10"), ("MA20", "ma20"), ("MA60", "ma60")]:
                if last[key] is not None:
                    diff = (last["close"] - last[key]) / last[key] * 100
                    ma_rows.append({"均線": label, "數值": round(last[key], 2),
                                     "股價偏離": f"{diff:+.2f}% ({'上方' if diff > 0 else '下方'})"})
            st.dataframe(pd.DataFrame(ma_rows), hide_index=True, use_container_width=True)

        ic4, ic5 = st.columns(2)
        with ic4:
            st.write("**MACD 指標**")
            if last.get("macd") is not None and last.get("macdSig") is not None:
                macd_bull = last["macd"] > last["macdSig"]
                cross = ""
                if prev.get("macd") is not None and prev.get("macdSig") is not None:
                    if prev["macd"] <= prev["macdSig"] and last["macd"] > last["macdSig"]:
                        cross = "　⚡ 黃金交叉"
                    elif prev["macd"] >= prev["macdSig"] and last["macd"] < last["macdSig"]:
                        cross = "　⚡ 死亡交叉"
                st.markdown(f"DIF=**{last['macd']:.2f}**　Signal=**{last['macdSig']:.2f}**　柱狀=**{last['macdHist']:.2f}**")
                st.caption(("DIF在Signal上方，偏多" if macd_bull else "DIF在Signal下方，偏空") + cross)
            else:
                st.caption("資料不足")
        with ic5:
            st.write("**布林通道**")
            if last.get("bbU") is not None and last.get("bbL") is not None:
                bb_width = last["bbU"] - last["bbL"]
                bb_pos = ((last["close"] - last["bbL"]) / bb_width * 100) if bb_width else 50
                bb_width_pct = (bb_width / last["close"] * 100) if last["close"] else 0
                st.markdown(f"上軌=**{last['bbU']:.2f}**　下軌=**{last['bbL']:.2f}**")
                bb_lbl = "⚠️ 貼近上軌，注意過熱回檔" if bb_pos > 80 else ("💚 貼近下軌，留意反彈" if bb_pos < 20 else "位於通道中段")
                if bb_width_pct < 8:
                    bb_lbl += "　🔸通道收窄，留意變盤"
                st.caption(f"股價位置：{bb_pos:.0f}%（通道寬度 {bb_width_pct:.1f}%）　{bb_lbl}")
            else:
                st.caption("資料不足")

        st.divider()
        st.markdown("### 📉 技術分析圖表")
        st.plotly_chart(draw_chart(r["data"], f"{r['stockId']} {r['name']}", r["pt"]), use_container_width=True)

        with st.expander("📋 原始資料（最近20筆）"):
            raw_rows = []
            for d in list(reversed(r["data"]))[:20]:
                raw_rows.append({
                    "日期": d["date"], "開盤": d["open"], "最高": d["high"], "最低": d["low"], "收盤": d["close"],
                    "成交量": d["volume"],
                    "MA5": round(d["ma5"], 2) if d["ma5"] is not None else None,
                    "MA20": round(d["ma20"], 2) if d["ma20"] is not None else None,
                    "MA60": round(d["ma60"], 2) if d["ma60"] is not None else None,
                    "RSI": round(d["rsi"], 2) if d["rsi"] is not None else None,
                    "KD-K": round(d["kdK"], 2) if d["kdK"] is not None else None,
                    "KD-D": round(d["kdD"], 2) if d["kdD"] is not None else None,
                    "MACD": round(d["macd"], 2) if d.get("macd") is not None else None,
                    "Signal": round(d["macdSig"], 2) if d.get("macdSig") is not None else None,
                    "BB上軌": round(d["bbU"], 2) if d.get("bbU") is not None else None,
                    "BB下軌": round(d["bbL"], 2) if d.get("bbL") is not None else None,
                })
            st.dataframe(pd.DataFrame(raw_rows), hide_index=True, use_container_width=True)


# ────────────────────────────────────────────────────────────────
# 歷史回測分析結果（放在批次分析結果後面，避免資料庫累積大量歷史紀錄後，
# 這一大段內容把批次分析摘要往下推，讓人誤以為批次分析「跑完跳轉不出來」）
# ────────────────────────────────────────────────────────────────

bt_df_us = load_backtest_df()
if bt_df_us is not None and not bt_df_us.empty:
    st.divider()
    with st.expander("🔬 歷史回測分析結果（點開查看，累積 " + f"{len(bt_df_us):,}" + " 筆評估紀錄）", expanded=backtest_clicked_us):
        st.markdown("## 🔬 歷史回測分析結果（美股／S&P500）")
        st.markdown(
            f"**目前累積 {len(bt_df_us):,} 筆評估紀錄**"
            f"（{bt_df_us['eval_date'].min()} ～ {bt_df_us['eval_date'].max()}）"
        )

        st.markdown("##### 📊 評分區間 vs 實際報酬")
        st.dataframe(analyze_score_buckets(bt_df_us), hide_index=True, use_container_width=True)

        st.markdown("##### 🚀 「強勢突破盤」標記 vs 實際報酬")
        st.dataframe(analyze_tag_hitrate(bt_df_us, "is_breakout", "強勢突破盤"), hide_index=True, use_container_width=True)

        st.markdown("##### 🎯 「跌深反彈盤」標記 vs 實際報酬")
        st.dataframe(analyze_tag_hitrate(bt_df_us, "is_pullback_rebound", "跌深反彈盤"), hide_index=True, use_container_width=True)

        if "golden_cross_recent" in bt_df_us.columns and bt_df_us["golden_cross_recent"].notna().any():
            st.markdown("##### ⚡ 「MACD近3日內黃金交叉」標記 vs 實際報酬")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "golden_cross_recent", "MACD黃金交叉"), hide_index=True, use_container_width=True)

        if "kdj_golden_cross_recent" in bt_df_us.columns and bt_df_us["kdj_golden_cross_recent"].notna().any():
            st.markdown("##### 🟢 「KDJ近3日內黃金交叉」標記 vs 實際報酬")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "kdj_golden_cross_recent", "KDJ黃金交叉"), hide_index=True, use_container_width=True)

        if "kdj_death_cross_recent" in bt_df_us.columns and bt_df_us["kdj_death_cross_recent"].notna().any():
            st.markdown("##### 🔴 「KDJ近3日內死亡交叉」標記 vs 實際報酬")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "kdj_death_cross_recent", "KDJ死亡交叉"), hide_index=True, use_container_width=True)

        if "rel_strength_20" in bt_df_us.columns and bt_df_us["rel_strength_20"].notna().any():
            bt_df_us["rel_strength_positive"] = (bt_df_us["rel_strength_20"] > 0).astype("Int64")
            bt_df_us.loc[bt_df_us["rel_strength_20"].isna(), "rel_strength_positive"] = pd.NA
            st.markdown("##### 💪 「相對強弱(vs大盤SPY，20日)」標記 vs 實際報酬")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "rel_strength_positive", "相對強弱為正"), hide_index=True, use_container_width=True)

        if "vol_ratio" in bt_df_us.columns and bt_df_us["vol_ratio"].notna().any():
            bt_df_us["vol_surge_15"] = (bt_df_us["vol_ratio"] >= 1.5).astype("Int64")
            bt_df_us.loc[bt_df_us["vol_ratio"].isna(), "vol_surge_15"] = pd.NA
            bt_df_us["vol_surge_2"] = (bt_df_us["vol_ratio"] >= 2).astype("Int64")
            bt_df_us.loc[bt_df_us["vol_ratio"].isna(), "vol_surge_2"] = pd.NA
            st.markdown("##### 📊 「爆量(≥1.5倍均量)」標記 vs 實際報酬")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "vol_surge_15", "爆量1.5倍"), hide_index=True, use_container_width=True)
            st.markdown("##### 📊 「爆量(≥2倍均量)」標記 vs 實際報酬")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "vol_surge_2", "爆量2倍"), hide_index=True, use_container_width=True)

        if "benchmark_above20" in bt_df_us.columns and bt_df_us["benchmark_above20"].notna().any():
            st.markdown("##### 🌐 「大盤站上20日均線」標記 vs 實際報酬（市場狀態濾網）")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "benchmark_above20", "大盤站上20日均線"), hide_index=True, use_container_width=True)
        if "benchmark_above60" in bt_df_us.columns and bt_df_us["benchmark_above60"].notna().any():
            st.markdown("##### 🌐 「大盤站上60日均線」標記 vs 實際報酬（市場狀態濾網）")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "benchmark_above60", "大盤站上60日均線"), hide_index=True, use_container_width=True)

        if "pattern_breakout" in bt_df_us.columns and bt_df_us["pattern_breakout"].notna().any():
            st.markdown("##### 🔍 「型態突破確認」標記 vs 實際報酬")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "pattern_breakout", "型態突破確認"), hide_index=True, use_container_width=True)

        if "pattern_just_broke" in bt_df_us.columns and bt_df_us["pattern_just_broke"].notna().any():
            st.markdown("##### 🔥 「型態剛形成(剛突破)」標記 vs 實際報酬")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "pattern_just_broke", "型態剛形成"), hide_index=True, use_container_width=True)

        if "pb_all_pass" in bt_df_us.columns and bt_df_us["pb_all_pass"].notna().any():
            st.markdown("##### ✅ 「回後買上漲全通過」標記 vs 實際報酬")
            st.dataframe(analyze_tag_hitrate(bt_df_us, "pb_all_pass", "回後買上漲全通過"), hide_index=True, use_container_width=True)

        pattern_hits_df_us = analyze_pattern_hits(bt_df_us)
        if not pattern_hits_df_us.empty:
            st.markdown("##### 📐 15種型態各自「剛形成」vs 實際報酬")
            st.caption("依樣本數由多到少排序，樣本數<10筆的型態不列出（資料太少沒有參考意義）。這是每種型態單獨、不跟其他條件混在一起的乾淨表現。")
            st.dataframe(pattern_hits_df_us, hide_index=True, use_container_width=True)

        st.markdown("##### 🎛️ 參數網格搜尋（單一評分公式的權重調整）")
        st.caption(
            "⚠️ 這是在已收集的歷史資料上找『表現較好』的參數組合，樣本數有限時容易"
            "過度適配——建議當作方向參考，人工確認合理後再手動調整正式評分公式，"
            "不要照單全收直接套用。"
        )
        horizon_choice_us = st.selectbox("優化目標天數", BACKTEST_HORIZONS, index=1, key="grid_horizon_us")
        grid_df_us = grid_search_params(bt_df_us, target_horizon=horizon_choice_us)
        if not grid_df_us.empty:
            st.dataframe(grid_df_us, hide_index=True, use_container_width=True)
        else:
            st.caption("資料量還不夠做網格搜尋分析（需要至少20筆訊號才會列入單一組合）。")

        st.markdown("##### 🧩 多因子複選搜尋（找出哪幾項欄位組合起來勝率最高）")
        st.caption(
            "窮舉1~3個條件旗標（分數門檻、強勢突破盤、跌深反彈盤、布林通道位置、"
            "型態確認、乖離度…）的AND組合，看哪個組合的勝率/平均報酬最好。美股沒有"
            "三大法人買賣超這種資料，旗標池跟台股版不同。⚠️ 測試的組合越多，純粹"
            "運氣好而表現突出的組合也會越多（多重比較問題），下面會顯示總共測了"
            "幾種組合——組合數越多，排在前面的結果就越需要保留懷疑，不代表真的"
            "有效，建議搭配樣本數一起看，樣本數太小（例如剛好卡在門檻附近）的組合"
            "更不可信。"
        )
        combo_horizon_us = st.selectbox("優化目標天數", BACKTEST_HORIZONS, index=1, key="combo_horizon_us")
        combo_min_samples_us = st.number_input("最小樣本數門檻", min_value=10, max_value=1000, value=50, step=10, key="combo_min_samples_us")
        combo_df_us, combos_tested_us = combo_search(bt_df_us, target_horizon=combo_horizon_us, min_samples=combo_min_samples_us)
        st.caption(f"共測試了 {combos_tested_us:,} 種條件組合")
        if not combo_df_us.empty:
            st.dataframe(combo_df_us, hide_index=True, use_container_width=True)
        else:
            st.caption("目前沒有任何組合的樣本數達到門檻，試著調低最小樣本數，或先累積更多回測資料。")

        st.markdown("##### 🎯 指定組合追蹤")
        st.caption(
            "22組（2026-09-21美股自己的回測結果，是真正驗證過的資料，不是移植台股"
            "的假設清單），不受上面的樣本數門檻或排名影響，一律顯示（含備註說明"
            "為什麼樣本數是0）。"
        )
        pinned_df_us = pinned_combo_stats(bt_df_us, PINNED_COMBOS, target_horizon=combo_horizon_us)
        st.dataframe(pinned_df_us, hide_index=True, use_container_width=True)

        st.markdown("##### 🚀 飆股搜尋（找出最容易出現大行情的組合）")
        st.caption(
            "這裡看的不是『平均勝率』，是『這個組合出現後，有多高比例會在N天內飆漲"
            "超過門檻%』——一個組合平均報酬普通，只要常常噴出大行情，一樣會排在前面。"
            "⚠️ 飆股本來就是稀有事件，樣本數少時『飆股比例』很容易被少數幾次極端行情"
            "撐出虛高的數字，務必搭配『飆股次數』一起看，次數只有個位數的不建議當真。"
            "從沒出現過飆股的組合不會列出來。"
        )
        ms_col1_us, ms_col2_us = st.columns(2)
        moonshot_horizon_us = ms_col1_us.selectbox("天數", BACKTEST_HORIZONS, index=1, key="moonshot_horizon_us")
        moonshot_threshold_us = ms_col2_us.number_input("漲幅門檻(%)", min_value=5, max_value=200, value=30, step=5, key="moonshot_threshold_us")
        moonshot_df_us, moonshot_combos_tested_us = find_moonshot_combos(
            bt_df_us, target_horizon=moonshot_horizon_us, threshold=moonshot_threshold_us
        )
        st.caption(f"共測試了 {moonshot_combos_tested_us:,} 種條件組合，其中有飆股紀錄的列在下面：")
        if not moonshot_df_us.empty:
            st.dataframe(moonshot_df_us, hide_index=True, use_container_width=True)
        else:
            st.caption("目前沒有任何組合出現過符合門檻的飆股，可以試著調低漲幅門檻，或先累積更多回測資料。")

        st.markdown(f"##### 🚀 高標股追蹤（指定{len(MOONSHOT_COMBOS)}組）")
        st.caption(
            "來源：2026-09-21美股飆股搜尋（10日/20日，漲幅門檻30%）的原始紀錄，固定"
            "顯示這23組（不隨你目前的回測資料重算）。每組視當初出現在哪張榜單（10/20日），"
            "列出樣本數、飆股次數、飆股比例%、飆股平均漲幅%。⚠️ 這是『命中大行情的比例』，"
            "不是整體勝率——跟上面的『🎯指定組合追蹤』(高勝率) 是不同的分類，飆股比例高"
            "不代表整體勝率高，兩者要分開看。多數組合樣本數只有20-80筆，飆股次數常常"
            "只有個位數，數字僅供參考方向。"
        )
        st.dataframe(moonshot_combo_static_stats(MOONSHOT_COMBOS, MOONSHOT_COMBO_STATS),
                     hide_index=True, use_container_width=True)


