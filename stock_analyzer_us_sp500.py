# -*- coding: utf-8 -*-
"""
US技術分析全攻略 · 美股評分分析系統 (Streamlit 版)
由 stock_analyzer_us.html 轉換而成，邏輯與原 HTML/JS 版本一致：
- 朱家泓四維度評分（趨勢／K線／均線／成交量，各25分，共100分），套用於美股個股分析
- 回後買上漲 8 條件核對
- 11 種進場型態確認（含「剛突破」：與前一交易日比較的新鮮突破訊號）
- 批次分析摘要表（可依進場條件／評分／型態確認篩選，關鍵字搜尋）
- Plotly K線＋均線＋布林通道＋成交量＋MACD 圖表
- OpenAI API「AI 智能綜合分析」
資料來源：Financial Modeling Prep（FMP）API /stable/ 端點
"""

import time
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

MY_LIST = ['AXTI', 'LITE', 'COHR', 'RCAT', 'LWLG', 'UMC', 'AMKR', 'AEHR', 'ON', 'SMR', 'IREN', 'HIMX', 'TSEM', 'CRDO', 'PLTR', 'BABA', 'HOOD', 'ALAB', 'NVDA', 'MU', 'FTNT', 'AEX', 'OXY', 'MRVL', 'QCOM', 'XYZ', 'RKLB', 'FN', 'ORCL', 'AVGO', 'BE', 'CRWV', 'AMD', 'SHOP', 'VZ', 'OWL', 'TER', 'GOOGL', 'SMCI', 'QBTS', 'VRT', 'TSM', 'SNDK', 'ONDS', 'RCAT', 'NBIS', 'POET', 'TSEM', 'GLW', 'DELL', 'SPCX', 'ON', 'SMTC']


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
# 技術指標計算、朱家泓四維度評分、回後買上漲、11種進場型態、Plotly 圖表
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


def enrich(data):
    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]
    m5, m10, m20, m60 = calc_ma(closes, 5), calc_ma(closes, 10), calc_ma(closes, 20), calc_ma(closes, 60)
    md = calc_macd_series(closes)
    rs = calc_rsi_series(closes)
    bbs = calc_bb_series(closes)
    vm5, vm20 = calc_volma(volumes, 5), calc_volma(volumes, 20)
    kd = calc_kd(data, 9)
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
# 回後買上漲 8 條件核對
# ────────────────────────────────────────────────────────────────

def check_pullback_buy(data):
    last = data[-1]
    prev = data[-2] if len(data) >= 2 else last

    results = []
    all_pass = True

    pv = pivots(data)
    c1 = False
    if len(pv["highs"]) >= 2 and len(pv["lows"]) >= 2:
        rh = [data[pv["highs"][-2]]["high"], data[pv["highs"][-1]]["high"]]
        rl = [data[pv["lows"][-2]]["low"], data[pv["lows"][-1]]["low"]]
        c1 = rh[1] > rh[0] and rl[1] > rl[0]
    results.append({"label": "①趨勢多頭（高高低低）", "pass": c1, "required": True, "detail": ""})
    if not c1:
        all_pass = False

    pullback_days = data[-6:-1]
    had_pullback = any(
        (d["close"] < (pullback_days[i - 1]["close"] if i > 0 else d["close"])) or (d["close"] < d["open"])
        for i, d in enumerate(pullback_days)
    )
    c2 = had_pullback and last["close"] > prev["close"]
    results.append({"label": "②位置回後上漲（近期有回檔，今轉上）", "pass": c2, "required": True, "detail": ""})
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


def detect_patterns(data, pb, skip_just_broke=False):
    last = len(data) - 1
    last_close = data[last]["close"]
    last_vol = data[last]["volume"]
    vm20 = data[last]["vm20"]
    vol_confirm = (last_vol / vm20 >= 1.3) if vm20 else False

    # 型態辨識所用的高低點，改成直接沿用「轉折波」(build_zigzag) 算出來的同一組轉折點，
    # 不再用另一套獨立的分形視窗判斷法——這樣圖表上畫出來的轉折波，就是型態辨識實際依據的高低點，
    # 兩者完全一致，不會有「圖上看到的轉折」跟「型態判斷用的轉折」對不起來的狀況。
    zz = build_zigzag(data)
    # 只取近90根K棒內的轉折點，避免抓到太久遠、失去意義的型態
    floor = max(0, len(data) - 90)
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

    # (1) 頭肩底
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
            bo = breakout_check(neck)
            results.append({"id": id_, "name": name, "formed": True, "breakout": bo["confirmed"],
                             "detail": bo["detail"] or "型態成形，等待突破頸線", "desc": "左右肩低點相近，頭部最低，突破頸線為買點",
                             "line": {"i1": l3[0], "p1": neck, "slope": 0} if neck is not None else None})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "左右肩低點相近，頭部最低，突破頸線為買點"})

    # (2) 複式頭肩底
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
            bo = breakout_check(neck)
            results.append({"id": id_, "name": name, "formed": True, "breakout": bo["confirmed"],
                             "detail": bo["detail"] or "型態成形，等待突破頸線", "desc": "多重肩部低點環繞單一最低頭部，突破頸線為買點",
                             "line": {"i1": last_lows[0], "p1": neck, "slope": 0} if neck is not None else None})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "多重肩部低點環繞單一最低頭部，突破頸線為買點"})

    # (3) N字底
    id_, name = "nb", "N字底"
    added = False
    if len(lows) >= 2 and len(highs) >= 1:
        A, C = lows[-2], lows[-1]
        b_cands = [i for i in highs if A < i < C]
        if b_cands:
            B = b_cands[-1]
            low_a, low_c, high_b = data[A]["low"], data[C]["low"], data[B]["high"]
            higher_low = low_c > low_a
            if higher_low:
                bo = breakout_check(high_b)
                results.append({"id": id_, "name": name, "formed": True, "breakout": bo["confirmed"],
                                 "detail": bo["detail"] or "拉回未破前低，等待突破反彈高點", "desc": "低點反彈後拉回不破前低，再突破反彈高點為買點",
                                 "line": {"i1": B, "p1": high_b, "slope": 0}})
                added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "低點反彈後拉回不破前低，再突破反彈高點為買點"})

    # (4) 三重底
    id_, name = "tb", "三重底"
    added = False
    if len(lows) >= 3:
        l3 = lows[-3:]
        vals = [data[i]["low"] for i in l3]
        all_similar = tolerant(vals[0], vals[1], 0.08) and tolerant(vals[1], vals[2], 0.08) and tolerant(vals[0], vals[2], 0.08)
        if all_similar:
            h_between = [i for i in highs if l3[0] < i < l3[2]]
            res = max((data[i]["high"] for i in h_between), default=None)
            bo = breakout_check(res)
            results.append({"id": id_, "name": name, "formed": True, "breakout": bo["confirmed"],
                             "detail": bo["detail"] or "型態成形，等待突破壓力", "desc": "三個低點高度相近，突破期間高點為買點",
                             "line": {"i1": l3[0], "p1": res, "slope": 0} if res is not None else None})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "三個低點高度相近，突破期間高點為買點"})

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
            results.append({"id": id_, "name": name, "formed": True, "breakout": bo["confirmed"],
                             "detail": bo["detail"] or "弧形築底中，等待突破起跌壓力", "desc": "價格緩跌後緩升成U型，突破起跌點高點為買點",
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
            results.append({"id": id_, "name": name, "formed": True, "breakout": breakout,
                             "detail": f"整理區間高點＝{resistance:.2f}　現價＝{last_close:.2f}" + ("　(帶量突破)" if vol_confirm else "　(尚未帶量)"),
                             "desc": "均線糾結、價格窄幅整理（區間範圍約10%內），帶量突破整理區間為買點",
                             "line": {"i1": win_start_idx6, "p1": resistance, "slope": 0}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "均線糾結、價格窄幅整理（區間範圍約10%內），帶量突破整理區間為買點"})

    # (7) 突破ABC修正下降切線
    id_, name = "abc", "突破ABC修正下降切線"
    added = False
    # 只看「最近」的轉折高點，取最後兩個（A、C），避免抓到太久遠、已經沒有參考意義的舊高點
    recent_highs = [i for i in highs if i >= last - 40]
    if len(recent_highs) >= 2:
        h1, h2 = recent_highs[-2], recent_highs[-1]  # A：較早較高；C：較近較低
        y1, y2 = data[h1]["high"], data[h2]["high"]
        # A、C之間要有拉回的低點（確認是ABC三段式修正），C要比A低，且間隔/時效合理
        has_low_between = any(h1 < li < h2 for li in lows)
        if y2 < y1 and h2 > h1 and has_low_between and (h2 - h1) <= 20 and (last - h2) <= 20:
            slope_ = (y2 - y1) / (h2 - h1)
            line_at_last = y1 + slope_ * (last - h1)
            ma20up = _ma_slope_up(data, "ma20", 10, last)
            is_red = data[last]["close"] > data[last]["open"]
            breakout = last_close > line_at_last and ma20up and is_red
            results.append({"id": id_, "name": name, "formed": True, "breakout": breakout,
                             "detail": f"下降切線位置≈{line_at_last:.2f}　現價＝{last_close:.2f}" + ("　MA20上揚" if ma20up else "　MA20未上揚"),
                             "desc": "多頭回檔呈ABC下跌，反彈高點畫下降切線，MA20上揚下帶量紅K突破切線為買點",
                             "line": {"i1": h1, "p1": y1, "i2": h2, "p2": y2, "slope": slope_}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "多頭回檔呈ABC下跌，反彈高點畫下降切線，MA20上揚下帶量紅K突破切線為買點"})

    # (8) 突破上升軌道線
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
            offsets = [data[i]["high"] - (ly1 + slope2 * (i - l1)) for i in highs_in if i > l1]
            offset = max(offsets) if offsets else None
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

    # (10) K線橫盤的突破：三天(含首日)收盤未突破/跌破首日K線高低點，第四天(今日)帶量突破首日高點為買點
    id_, name = "kbp", "K線橫盤的突破"
    added = False
    if len(data) >= 4:
        anchor = data[last - 3]
        d2, d3 = data[last - 2], data[last - 1]

        def in_range(d):
            return anchor["low"] <= d["close"] <= anchor["high"]

        if in_range(d2) and in_range(d3):
            is_red_k = data[last]["close"] > data[last]["open"]
            breakout_k = last_close > anchor["high"] and is_red_k and vol_confirm
            results.append({"id": id_, "name": name, "formed": True, "breakout": breakout_k,
                             "detail": f"首日K線高點＝{anchor['high']:.2f}　現價＝{last_close:.2f}" + ("　(帶量)" if vol_confirm else "　(量未放大)"),
                             "desc": "三天(含首日)收盤未突破首日K線高低點，第四天帶量紅K突破首日高點為買點",
                             "line": {"i1": last - 3, "p1": anchor["high"], "slope": 0}})
            added = True
    if not added:
        results.append({"id": id_, "name": name, "formed": False, "breakout": False,
                         "detail": "尚未偵測到符合結構", "desc": "三天(含首日)收盤未突破首日K線高低點，第四天帶量紅K突破首日高點為買點"})

    # (11) 回後買上漲：沿用 checkPullbackBuy() 判斷結果
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
    raw_tail = data[-120:]
    tail_start_idx = len(data) - len(raw_tail)
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
    shapes, annotations = [], []
    last_idx = len(data) - 1
    if pt and pt.get("results"):
        for p in pt["results"]:
            if not p.get("formed"):
                continue
            style = PATTERN_LINE_STYLE.get(p["id"])
            if not style:
                continue
            # 上升軌道線另外多畫一條下緣支撐線（line2），其餘型態只有一條輔助線（line）
            for idx, ln in enumerate((p.get("line"), p.get("line2"))):
                if not ln or ln["i1"] < tail_start_idx:
                    continue
                line_end_price = ln["p1"] + ln["slope"] * (last_idx - ln["i1"])
                shapes.append(dict(
                    type="line", xref="x", yref="y",
                    x0=data[ln["i1"]]["date"], y0=ln["p1"],
                    x1=data[last_idx]["date"], y1=line_end_price,
                    line=dict(color=style["color"], width=2 if idx == 0 else 1.3,
                               dash="solid" if p["breakout"] else "dash"),
                ))
                if idx == 0:
                    annotations.append(dict(
                        x=data[ln["i1"]]["date"], y=ln["p1"], xref="x", yref="y",
                        text=style["label"], showarrow=True, arrowhead=2, arrowcolor=style["color"],
                        font=dict(color=style["color"], size=10), ax=-10, ay=-30,
                    ))
                    if p["breakout"]:
                        annotations.append(dict(
                            x=data[last_idx]["date"], y=data[last_idx]["close"], xref="x", yref="y",
                            text="★ 突破" + p["name"], showarrow=True, arrowhead=2, arrowcolor=style["color"],
                            font=dict(color=style["color"], size=11), ax=10, ay=-35,
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
    lines.append("")
    lines.append(f"【朱家泓四維度評分】總分 {r['total']}/100")
    lines.append(f"趨勢 {r['tr']['score']}/25、K線 {r['kl']['score']}/25、均線 {r['ma']['score']}/25、成交量 {r['vl']['score']}/25")
    all_sigs = r["tr"]["sigs"] + r["kl"]["sigs"] + r["ma"]["sigs"] + r["vl"]["sigs"]
    bull_sigs = [s[0] for s in all_sigs if s[1] == "bull"]
    bear_sigs = [s[0] for s in all_sigs if s[1] == "bear"]
    if bull_sigs:
        lines.append("多頭訊號：" + "、".join(bull_sigs))
    if bear_sigs:
        lines.append("空頭訊號：" + "、".join(bear_sigs))
    lines.append("")
    lines.append(f"【回後買上漲 8條件核對】必要條件通過 {r['pb']['requiredPassed']}/{r['pb']['requiredTotal']}" + ("（全數通過）" if r["pb"]["allPass"] else ""))
    lines.append("")
    lines.append("【型態確認，11種進場型態】")
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
                    {"role": "system", "content": "你是一位精通美股技術分析的資深操盤手，熟悉朱家泓《技術分析全攻略》方法論（趨勢轉折波、K線、均線、成交量四維度評分，以及回後買上漲、頭肩底等進場型態），並將此方法論套用於美國股市個股分析。請根據使用者提供的個股技術數據摘要，用繁體中文給出：1) 整體技術面研判（3-4句） 2) 進場時機與風險提示 3) 綜合建議（積極做多／可考慮／觀望／不建議）。語氣專業、精簡、避免空泛用詞，並提醒這僅為技術面參考，非投資建議，且未考慮美股盤前盤後交易、財報公布時程等因素。"},
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
# Streamlit UI
# ────────────────────────────────────────────────────────────────

if "stock_text_input" not in st.session_state:
    st.session_state["stock_text_input"] = "\n".join(SP500_LIST)
if "active_list" not in st.session_state:
    st.session_state.active_list = "sp500"

st.title("📊 US技術分析全攻略 · 美股評分分析系統")
st.caption("依據朱家泓《技術分析全攻略》課程方法論，從趨勢、K線、均線、成交量四大維度評分（美股版，資料來源：Financial Modeling Prep）")

with st.sidebar:
    st.header("📊 US技術分析全攻略")
    st.caption("朱家泓方法論 · 美股評分系統")

    api_token = st.text_input("Financial Modeling Prep API Key", type="password", placeholder="輸入您的 FMP API Key")
    st.caption("還沒有 Key？前往 [financialmodelingprep.com](https://site.financialmodelingprep.com/developer/docs) 免費註冊（Free 方案，250 次/天）")

    st.subheader("批次股票代號")
    c1, c2 = st.columns(2)
    if c1.button("S&P 500", use_container_width=True,
                  type="primary" if st.session_state.active_list == "sp500" else "secondary"):
        st.session_state["stock_text_input"] = "\n".join(SP500_LIST)
        st.session_state.active_list = "sp500"
        st.rerun()
    if c2.button("我的清單", use_container_width=True,
                  type="primary" if st.session_state.active_list == "my" else "secondary"):
        st.session_state["stock_text_input"] = "\n".join(MY_LIST)
        st.session_state.active_list = "my"
        st.rerun()

    stock_text = st.text_area("每行一個，或逗號分隔（例：AAPL、MSFT、NVDA）",
                               height=180, key="stock_text_input")

    days = st.slider("分析天數", min_value=90, max_value=365, value=180, step=30)

    run_clicked = st.button("🔍 批次分析", type="primary", use_container_width=True)

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
            data = enrich(raw_data)
            tr, kl, ma_, vl = score_trend(data), score_kline(data), score_ma(data), score_vol(data)
            pb = check_pullback_buy(data)
            pt = detect_patterns(data, pb)
            total_score = tr["score"] + kl["score"] + ma_["score"] + vl["score"]
            batch_results.append({"stockId": sid, "name": name, "data": data, "tr": tr, "kl": kl,
                                   "ma": ma_, "vl": vl, "pb": pb, "pt": pt, "total": total_score})
            with log_box:
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


if run_clicked:
    run_batch_analysis()
if "batch_results" not in st.session_state:
    st.info("📈 請在左側輸入 Financial Modeling Prep API Key 與美股代號，點擊「批次分析」即可開始。")
else:
    batch_results = st.session_state.batch_results

    st.markdown("### 📋 批次分析摘要")

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
        return {
            "_idx": i, "股票": f"{r['stockId']} {r['name']}", "總分": r["total"], "評等": score_lbl,
            "趨勢": r["tr"]["score"], "K線": r["kl"]["score"], "均線": r["ma"]["score"], "成交量": r["vl"]["score"],
            "漲跌%": round(chgp, 2), "收盤": f"${last['close']:.2f}",
            "回後買進場": ("✅ " if r["pb"]["allPass"] else "❌ ") + pb_txt,
            "型態確認": f"{pt_icon} {pt_txt}",
        }

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

        st.divider()
        st.markdown("### 📊 綜合評分")
        sc1, sc2 = st.columns([1, 2])
        with sc1:
            st.markdown(
                f"<div style='text-align:center;background:linear-gradient(135deg,#1a1a2e,#16213e);"
                f"border-radius:16px;padding:28px 16px;border:1px solid rgba(255,255,255,.1)'>"
                f"<div style='font-size:64px;font-weight:700;color:{vc}'>{total}</div>"
                f"<div style='font-size:12px;color:#888;margin-top:6px'>綜合評分 / 100</div>"
                f"<div style='margin-top:12px;display:inline-block;padding:7px 16px;border-radius:8px;"
                f"background:{vc}22;color:{vc};font-weight:700'>{vt}</div></div>",
                unsafe_allow_html=True,
            )
        with sc2:
            dims = [("📈 趨勢分析", r["tr"], "轉折波・多空頭辨別"), ("🕯️ K線型態", r["kl"], "K線組合・變盤訊號"),
                    ("📊 均線系統", r["ma"], "葛蘭畢・多空排列"), ("📦 成交量", r["vl"], "量價關係・量能確認")]
            for t, dr, d in dims:
                pct = dr["score"] / dr["max"] * 100
                col = "#00c864" if pct >= 70 else "#f0a500" if pct >= 40 else "#ff3c3c"
                st.markdown(
                    f"<div style='background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);"
                    f"border-radius:10px;padding:10px 14px;margin-bottom:6px'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                    f"<div><div style='font-size:12px;color:#888'>{t}</div><div style='font-size:11px;color:#666'>{d}</div></div>"
                    f"<div style='font-size:22px;font-weight:700;color:{col}'>{dr['score']}<span style='font-size:11px;color:#555'>/{dr['max']}</span></div>"
                    f"</div><div style='background:rgba(255,255,255,.05);border-radius:4px;height:5px;margin-top:6px'>"
                    f"<div style='background:{col};border-radius:4px;height:5px;width:{pct}%'></div></div></div>",
                    unsafe_allow_html=True,
                )

        st.divider()
        st.markdown("### 🔔 技術訊號")
        sig_cols = st.columns(4)
        for col, (t, sigs) in zip(sig_cols, [("趨勢訊號", r["tr"]["sigs"]), ("K線訊號", r["kl"]["sigs"]),
                                              ("均線訊號", r["ma"]["sigs"]), ("成交量訊號", r["vl"]["sigs"])]):
            with col:
                st.markdown(f"**{t}**")
                if sigs:
                    for label, kind in sigs:
                        color = "#00c864" if kind == "bull" else "#ff5555" if kind == "bear" else "#aaa"
                        st.markdown(f"<span style='display:inline-block;padding:2px 8px;border-radius:12px;"
                                    f"font-size:11px;color:{color};border:1px solid {color}55;margin:2px'>{label}</span>",
                                    unsafe_allow_html=True)
                else:
                    st.caption("無明顯訊號")

        st.divider()
        st.markdown("### 💡 操作建議")
        if total >= 80:
            act, adv = "🟢 積極做多", [f"**{r['name']}** 綜合評分 {total} 分，技術面強勢，建議積極做多。"]
            if r["tr"]["tdir"] == "多頭":
                adv.append("趨勢確立多頭，順勢操作，逢低分批佈局。")
            ma20v = last["ma20"] or last["close"]
            bb_up = last["bbU"] or last["close"] * 1.1
            sl = f"停損設於 **${ma20v * 0.97:.2f}**（20MA下方3%）"
            tgt = f"目標參考 **${last['close'] * ((bb_up - last['close']) / last['close'] + 1):.2f}**（布林上軌）"
        elif total >= 65:
            act, adv = "🔵 可考慮進場", [f"**{r['name']}** 評分 {total} 分，技術面偏多，可考慮分批進場。", "建議等待回測均線後再進場，降低風險。"]
            ma20v = last["ma20"] or last["close"]
            sl = f"停損建議 **${ma20v * 0.98:.2f}**（20MA下方2%）"
            tgt = f"短線目標 **${last['close'] * 1.08:.2f}**（+8%）"
        elif total >= 50:
            act, adv = "🟡 觀望為主", [f"**{r['name']}** 評分 {total} 分，技術面訊號混雜，建議觀望。", "等待均線整理完畢或趨勢明確後再行動。"]
            sl, tgt = "暫不建議進場", "等待更佳時機"
        else:
            act, adv = "🔴 不適合進場", [f"**{r['name']}** 評分 {total} 分，技術面偏空，不建議進場。"]
            if r["tr"]["tdir"] == "空頭":
                adv.append("目前空頭趨勢，切忌逆勢做多，等待趨勢反轉。")
            else:
                adv.append("技術指標偏弱，應持現金等待機會。")
            sl, tgt = "持倉者建議設停損出場", "等待多頭訊號出現"

        all_sigs = r["tr"]["sigs"] + r["kl"]["sigs"] + r["ma"]["sigs"] + r["vl"]["sigs"]
        hints = [s[0] for s in all_sigs if s[1] == "bull"][:5]
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
        st.markdown("### 🔍 型態確認（11種進場型態）")
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
                })
            st.dataframe(pd.DataFrame(raw_rows), hide_index=True, use_container_width=True)
