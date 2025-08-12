#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PubMed → (特定ジャーナルの新着) → Gemini(邦題+4点要約, 1コール/論文) → Gmail送信
- 毎日1回の実行想定（GitHub Actionsなど）
- 送信済みPMIDは sent_pmids.json で重複防止
- 要約は Google AI Studio の gemini-(1.5|2.5)-flash を想定
"""

import os, json, time, ssl, smtplib, requests, re
from string import Template
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET
from google import genai

# ========= 環境変数 =========
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # AI StudioのAPIキー
JOURNALS = [j.strip() for j in os.getenv("JOURNALS", "").split(",") if j.strip()]
RECIPIENT = os.getenv("RECIPIENT_EMAIL", os.getenv("GMAIL_ADDRESS", ""))
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
PUBMED_TOOL_EMAIL = os.getenv("PUBMED_TOOL_EMAIL", GMAIL_ADDRESS)  # eutils &emailに使用（推奨）
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "")  # 任意（レート上限UP）
SLEEP_BETWEEN_CALLS = float(os.getenv("SLEEP_BETWEEN_CALLS", "0.3"))  # 無料枠配慮

# ========= PubMed E-utilities =========
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TOOL_NAME = "pubmed-daily-digest"
HEADERS = {"User-Agent": TOOL_NAME}

# ========= 状態保存 =========
STATE_PATH = "sent_pmids.json"

def load_sent_pmids():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_sent_pmids(pmids):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(pmids)), f, ensure_ascii=False, indent=2)

# ========= PubMed検索 =========
def build_journal_query(journals):
    # PubMedジャーナルフィールド[ta]でOR結合（完全名 or 略称）
    parts = [f'("{j}"[ta])' for j in journals]
    return "(" + " OR ".join(parts) + ")"

def pubmed_esearch(term):
    # 直近2日(重複は自前で排除)をEDATで検索
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "datetype": "edat",
        "reldate": "2",
        "retmax": "200",
        "sort": "pub_date",
        "tool": TOOL_NAME,
        "email": PUBMED_TOOL_EMAIL,
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    r = requests.get(EUTILS_BASE + "esearch.fcgi", params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("esearchresult", {}).get("idlist", [])

def pubmed_efetch(pmids):
    if not pmids:
        return ""
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "abstract",
        "retmode": "xml",
        "tool": TOOL_NAME,
        "email": PUBMED_TOOL_EMAIL,
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    r = requests.get(EUTILS_BASE + "efetch.fcgi", params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.text

def _norm_ws(s: str) -> str:
    # 改行や連続空白を1スペースへ
    return re.sub(r"\s+", " ", (s or "")).strip()

def _itertext(elem) -> str:
    # 要素配下のテキスト（子要素含む）をすべて連結
    if elem is None:
        return ""
    return _norm_ws("".join(elem.itertext()))

def _prefer_abbrev(art) -> str:
    # ① ISO略称 → ② MedlineTA → ③ 正式名 の優先順
    for path in [".//Journal/ISOAbbreviation",
                 ".//MedlineJournalInfo/MedlineTA",
                 ".//Journal/Title"]:
        val = art.findtext(path)
        if val and val.strip():
            return _norm_ws(val)
    return ""

# --- 日付整形ヘルパー ---
_MONTH_ABBR = {
    "1":"Jan","01":"Jan","Jan":"Jan","January":"Jan",
    "2":"Feb","02":"Feb","Feb":"Feb","February":"Feb",
    "3":"Mar","03":"Mar","Mar":"Mar","March":"Mar",
    "4":"Apr","04":"Apr","Apr":"Apr","April":"Apr",
    "5":"May","05":"May","May":"May",
    "6":"Jun","06":"Jun","Jun":"Jun","June":"Jun",
    "7":"Jul","07":"Jul","Jul":"Jul","July":"Jul",
    "8":"Aug","08":"Aug","Aug":"Aug","August":"Aug",
    "9":"Sep","09":"Sep","Sep":"Sep","September":"Sep",
    "10":"Oct","Oct":"Oct","October":"Oct",
    "11":"Nov","Nov":"Nov","November":"Nov",
    "12":"Dec","Dec":"Dec","December":"Dec",
}

def _fmt_date(y, m, d):
    """YYYY Mon DD/ YYYY Mon / YYYY を返す（mは略称に統一）"""
    y = (y or "").strip()
    m = _MONTH_ABBR.get((m or "").strip(), (m or "").strip())
    d = (d or "").strip()
    if y and m and d:
        # 日は2桁にそろえる（1→01）
        if d.isdigit() and len(d) == 1:
            d = f"0{d}"
        return f"{y} {m} {d}"
    if y and m:
        return f"{y} {m}"
    return y or ""

def _extract_pubdate_display(art):
    """
    発行日の表示優先度：
    1) Article/ArticleDate[@DateType='Electronic']（EPub）
    2) Article/ArticleDate（最初のもの）
    3) JournalIssue/PubDate（Year/Month/Day または MedlineDate）
    4) History/PubMedPubDate[@PubStatus='pubmed' or 'entrez']（最後の手段）
    """
    # 1) Electronic
    for ad in art.findall(".//Article/ArticleDate"):
        dt = (ad.attrib or {}).get("DateType", "").lower()
        if dt == "electronic":
            return _fmt_date(ad.findtext("Year"), ad.findtext("Month"), ad.findtext("Day"))

    # 2) 何かしらのArticleDate
    ad = art.find(".//Article/ArticleDate")
    if ad is not None:
        s = _fmt_date(ad.findtext("Year"), ad.findtext("Month"), ad.findtext("Day"))
        if s:
            return s

    # 3) 号のPubDate
    y = art.findtext(".//JournalIssue/PubDate/Year")
    m = art.findtext(".//JournalIssue/PubDate/Month")
    d = art.findtext(".//JournalIssue/PubDate/Day")
    s = _fmt_date(y, m, d)
    if s:
        return s
    # MedlineDate（例: "2025 Sep-Oct" 等）はそのまま返す
    md = (art.findtext(".//JournalIssue/PubDate/MedlineDate") or "").strip()
    if md:
        return md

    # 4) PubMed履歴（入庫日など）
    for status in ("pubmed", "entrez", "medline"):
        ppd = art.find(f".//History/PubMedPubDate[@PubStatus='{status}']")
        if ppd is not None:
            return _fmt_date(ppd.findtext("Year"), ppd.findtext("Month"), ppd.findtext("Day"))

    return ""

def _extract_pubtypes(art):
    # PublicationType を重複なく順序保持で取得
    pts = []
    seen = set()
    for pt in art.findall(".//PublicationTypeList/PublicationType"):
        t = (pt.text or "").strip()
        if t and t not in seen:
            seen.add(t)
            pts.append(t)
    return pts

# 表示言語（既定: 英語 / 日本語にしたい場合は env: PT_DISPLAY_LANG=ja）
PT_JA_MAP = {
    "Randomized Controlled Trial":"無作為化比較試験",
    "Systematic Review":"システマティックレビュー",
    "Meta-Analysis":"メタアナリシス",
    "Clinical Trial":"臨床試験",
    "Clinical Trial, Phase II":"第II相臨床試験",
    "Clinical Trial, Phase III":"第III相臨床試験",
    "Review":"総説",
    "Guideline":"ガイドライン",
    "Practice Guideline":"診療ガイドライン",
    "Multicenter Study":"多施設研究",
    "Comparative Study":"比較研究",
    "Observational Study":"観察研究",
    "Case Reports":"症例報告",
    "Editorial":"編集者寄稿",
    "Letter":"レター",
}

def _format_pt_for_display(pts):
    lang = os.getenv("PT_DISPLAY_LANG", "en").lower()
    if lang == "ja":
        return ", ".join(PT_JA_MAP.get(p, p) for p in pts)
    return ", ".join(pts)

def parse_records(xml_text):
    """EFetch XMLから必要項目を抜き出す"""
    if not xml_text:
        return []
    root = ET.fromstring(xml_text)
    results = []
    for art in root.findall(".//PubmedArticle"):
        pmid = (art.findtext(".//PMID") or "").strip()
        
        # ★ タイトル：findtext -> itertext に変更
        title_elem = art.find(".//Article/ArticleTitle")
        title = _itertext(title_elem)

        if len(title) < 2:
            print("WARN: suspicious title for PMID", pmid, "->", repr(title))

        # ★ アブストラクト：子要素（<i>, <sup> 等）も含めて拾う
        texts = []
        for abs_elem in art.findall(".//Abstract/AbstractText"):
            label = abs_elem.attrib.get("Label") if abs_elem.attrib else None
            txt = _itertext(abs_elem)
            if not txt:
                continue
            texts.append(f"{label}: {txt}" if label else txt)
        abstract = "\n".join(texts)

        # --- 著者 ---
        authors = []
        for au in art.findall(".//AuthorList/Author"):
            last = au.findtext("LastName") or ""
            init = au.findtext("Initials") or ""
            if last or init:
                authors.append(f"{last} {init}".strip())
        authors_line = (", ".join(authors[:3]) + (", et al." if len(authors) > 3 else "")) if authors else ""

        # --- ジャーナル ---
        journal = _prefer_abbrev(art)

        # --- Publication Type（PT） ---
        pubtypes = _extract_pubtypes(art)

        # --- 発行日（EPub優先で堅牢に） ---
        pubdate = _extract_pubdate_display(art)

        # --- DOI ---
        doi = ""
        for aid in art.findall(".//ArticleIdList/ArticleId"):
            if (aid.attrib or {}).get("IdType", "").lower() == "doi":
                doi = (aid.text or "").strip()
                break

        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        results.append({
            "pmid": pmid,
            "title": title,
            "authors": authors_line,
            "journal": journal,
            "pubdate": pubdate,
            "doi": doi,
            "url": url,
            "abstract": abstract,
            "pt": pubtypes,  # ← 追加
        })
    return results

# ========= Gemini（1回で邦題＋4点要約） =========
def _resp_to_text(resp) -> str:
    # google-genaiの安全な文字列化
    if getattr(resp, "text", None):
        return resp.text
    parts = []
    try:
        for c in getattr(resp, "candidates", []) or []:
            for p in getattr(c.content, "parts", []) or []:
                if getattr(p, "text", None):
                    parts.append(p.text)
    except Exception:
        pass
    return "\n".join(parts)

def _force_json(text: str) -> dict:
    if not text:
        return {}
    m = re.search(r"\{[\s\S]*\}", text)
    raw = m.group(0) if m else text
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _numbers(s: str):
    # 12, 12.3, 95%, 84–95 などをざっくり抽出（全角→半角も軽く吸収）
    s = s.replace("％", "%").replace("．", ".")
    nums = set(re.findall(r"\d+(?:\.\d+)?%?", s))
    ranges = re.findall(r"\b\d+\s*[–-]\s*\d+\b", s)  # 84–95
    return nums.union(ranges)

def _terms(s: str):
    # FAPI-46 / FAPI-74 / Ga-FAPI / [68Ga] など“医用核種/トレーサー”っぽい語を抽出
    pats = [
        r"\[\d+\s*[A-Za-z]+\]",        # [68Ga], [18F]
        r"[A-Za-z]+-[A-Za-z]+-\d+",    # Ga-FAPI-46
        r"FAPI-\d+",                   # FAPI-46
        r"[A-Za-z]*FAPI-\d+",          # AlF-FAPI-74 など
    ]
    found = set()
    for p in pats:
        found.update(re.findall(p, s))
    return found

def _format_bullets(lines, target=4):
    xs = [str(x).strip() for x in (lines or []) if str(x).strip()]
    xs = [("・" + x.lstrip("・-•*・ 　")).strip() for x in xs]
    xs = xs[:target]
    while len(xs) < target:
        xs.append("・（要約が不足しています）")
    xs = [x if len(x) <= 150 else (x[:147] + "…") for x in xs]
    return xs

def _sanitize_against_abstract(bullets, abstract):
    abs_nums  = _numbers(abstract)
    abs_terms = _terms(abstract)
    out = []
    for b in bullets:
        # 数値の検証：本文に無い数値は削除
        for n in _numbers(b):
            if n not in abs_nums:
                b = b.replace(n, "（数値記載なし）")
        # 核種/薬剤名の検証：本文に無い表記は削除
        for t in _terms(b):
            if t not in abs_terms:
                b = b.replace(t, "")
        # 余分なスペースの整形
        b = re.sub(r"\s{2,}", " ", b).strip()
        out.append(b)
    return out


PROMPT_TEMPLATE = Template("""あなたは放射線腫瘍学の事実抽出専用サマライザーです。時間のない放射線治療医向けに最新論文を日本語要約をします。以下を厳守して、PubMedで見つかった論文の英語タイトルとアブストラクトから自然な日本語になるように要約をしてください。

【出力】
- 日本語。厳格JSONのみを出力（前後の余計な文字やコードブロック禁止）
- 形式:
{
  "title_ja": "30〜45字の邦題（名詞止め・冗長な副題は圧縮）",
  "bullets": ["ポイント1","ポイント2","ポイント3","ポイント4"]
}

【重要ルール（厳守）】
- 事実抽出のみ：本文（タイトル/アブストラクト）に無い内容は書かない。外部知識の追加・言い換え・推測禁止
- 表記ポリシー：
  - 国際的略語・単位は原文のまま：OS, PFS, LC, HR, CI, CR/PR, ORR, CTCAE, SUVmax, Gy, fx, SBRT/IMRT/VMAT/SIB/PBT, RT/CRT
  - それ以外の一般語は**必ず日本語化**（patients→患者, toxicity→毒性, bleeding→出血, ulcer(s)→潰瘍, month(s)→か月 など）
  - 核種・トレーサー・薬剤名は原文表記（例：[18F], [68Ga], FAPI-46, nivolumab）
  - 数値・単位は原文どおり。本文に無い数値は書かない（無い場合は「数値記載なし」）
- スラッシュ表記は日本語で自然化（例：A/B→「AやB」または「A・B」）。「and/or」「vs」は「および／または」「対」に置換
- 線量分割は日本語標準で（例：5 × 7 Gy→「7 Gy × 5回」など原文が読みやすくなる範囲で整形可）

【放射線腫瘍医向けの優先観点（該当するもののみ）】
- 対象（がん種/病期/患者数/組み入れ）
- 介入/比較：線量(Gy)・分割(fx)・部位/照射野、モダリティ（IMRT/VMAT/SBRT/SRS/粒子線 等）、併用療法
- 主要評価：OS, PFS, LC, 反応率, HR/CI, p値, 追跡期間
- 安全性：CTCAEグレード、有害事象
- 画像/核医学：核種・トレーサー表記、診断能指標
- デザイン：相/無作為化/単群/多施設(タイトルにあれば確実にタイトルに入れる)

【出力の作り方】
- "bullets" は4 or 5点。各60〜120字。事実のみ、解釈や助言は書かない
- 各項目について、再帰的に文字数を厳密にカウントし、確実に120字以内に入るまで文字数チェックを繰り返し、120字以内に入っていれば最終的な出力とする。
- その際に120字を超えるようであれば、要約がメインであるため、p値やHR/CIを優先的に削除する。

英語タイトル:
$TITLE

アブストラクト:
$ABSTRACT
""")

def summarize_title_and_bullets(title: str, abstract: str) -> dict:
    client = genai.Client()  # GEMINI_API_KEY は環境変数から
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    prompt = PROMPT_TEMPLATE.substitute(
        TITLE=title,
        ABSTRACT=(abstract[:7000] if abstract else "")
    )

    try:
        resp = client.models.generate_content(model=model_name, contents=prompt)
        text = (_resp_to_text(resp) or "").strip()
        data = _force_json(text)
    except Exception:
        data = {}

    title_ja = str((data.get("title_ja") or "")).strip()
    bullets  = _format_bullets(data.get("bullets"))

    # ---- 生成後の整合チェック（抄録に無い数値/用語を除去）----
    #bullets = _sanitize_against_abstract(bullets, abstract or "")

    # 邦題の微整形（念のため）
    title_ja = title_ja.lstrip("・-•*[]() 　")
    if title_ja.endswith(("。","．",".")):
        title_ja = title_ja[:-1]
    if not title_ja:
        title_ja = "（邦題生成に失敗）"

    return {"title_ja": title_ja, "bullets": bullets}

# ========= メール整形・送信 =========
def build_email_body(date_jst_str, items):
    lines = []
    lines.append("新着論文AI要約配信\n")
    lines.append("放射線腫瘍学\n\n")
    lines.append(f"本日の新着論文は{len(items)}件です。\n\n")
    for i, it in enumerate(items, 1):
        lines.append(f"[論文{i}]")
        lines.append(f"原題：{str(it.get('title',''))}")
        lines.append(f"邦題（AI要約）：{str(it.get('title_ja',''))}")
        if it.get('authors'):
            lines.append(f"著者：{str(it.get('authors',''))}")
        lines.append(f"雑誌名：{str(it.get('journal',''))}")
        lines.append(f"発行日：{str(it.get('pubdate',''))}")
        # ★ 追加：Publication Type
        if it.get("pt"):
            lines.append(f"文献種別（PT）：{_format_pt_for_display(it.get('pt', []))}")
        lines.append(f"Pubmed：{str(it.get('url',''))}")
        lines.append(f"DOI：{str(it.get('doi','') or '-')}")
        lines.append("要約（AI生成）：")
        lines.append(str(it.get('summary','')))
        lines.append("\n")
    return "\n".join(lines)

def send_via_gmail(subject, body):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and RECIPIENT):
        raise RuntimeError("Gmail送信に必要な環境変数が不足しています。")
    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [RECIPIENT], msg.as_string())

# ========= メイン =========
def main():
    if not JOURNALS:
        raise SystemExit("環境変数 JOURNALS が未設定です。カンマ区切りでジャーナル名を指定してください。")

    print("=== PubMed論文収集開始 ===")

    # 1) PubMed検索
    query = build_journal_query(JOURNALS)
    pmids = pubmed_esearch(query)

    print(f"検索結果: {len(pmids)}件ヒット")

    # 2) 重複除去
    sent = load_sent_pmids()
    new_pmids = [p for p in pmids if p not in sent]

    print(f"見つかった論文数: {len(pmids)}件")
    print(f"既送信でスキップ: {len(pmids) - len(new_pmids)}件")
    print(f"新規論文数: {len(new_pmids)}件")

    items = []
    if new_pmids:
        # 3) まとめて取得→解析
        xml = pubmed_efetch(new_pmids)
        records = parse_records(xml)

        # 4) 各レコードを要約（1論文=1APIコール）
        print(f"\n{len(records)}件の新規論文を処理")
        for idx, rec in enumerate(records, 1):
            print("\n=== AI要約・翻訳生成開始 ===")
            print(f"要約中 ({idx}/{len(records)}): {rec['title'][:50]}...")
            data = summarize_title_and_bullets(rec["title"], rec["abstract"] or "")
            rec["title_ja"] = data["title_ja"]
            if rec["abstract"]:
                rec["summary"] = "\n".join(data["bullets"])
            else:
                rec["summary"] = "・この論文にはPubMed上でアブストラクトが見つかりません"
            time.sleep(SLEEP_BETWEEN_CALLS)  # 無料枠RPMに配慮
            items.append(rec)

        # 5) 送信済み更新
        sent.update([r["pmid"] for r in records])
        save_sent_pmids(sent)

    # 6) メール送信（0件でも通知する運用）
    print("\n=== メール送信 ===")
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).strftime("%Y-%m-%d")
    count = len(items)  # ← 追加：新着数
    subject = f"【PubMed論文AI要約配信：新着{count}本】放射線腫瘍学 {today}"
    body = build_email_body(today, items)
    send_via_gmail(subject, body)
    
    print("\n=== 処理完了 ===")

if __name__ == "__main__":
    main()
