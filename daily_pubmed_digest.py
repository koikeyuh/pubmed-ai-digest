#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PubMed → (特定ジャーナルの新着) → Gemini(邦題+4点要約, 1コール/論文) → Gmail送信
- 毎日1回の実行想定（GitHub Actionsなど）
- 送信済みPMIDは sent_pmids.json で重複防止
- 要約は Google AI Studio の gemini-(1.5|2.5)-flash を想定
"""

import os, json, time, ssl, smtplib, requests, re
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET

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

def parse_records(xml_text):
    """EFetch XMLから必要項目を抜き出す"""
    if not xml_text:
        return []
    root = ET.fromstring(xml_text)
    results = []
    for art in root.findall(".//PubmedArticle"):
        pmid = (art.findtext(".//PMID") or "").strip()
        title = (art.findtext(".//Article/ArticleTitle") or "").strip()

        # --- Abstract（Label付きにも対応） ---
        texts = []
        for abs_elem in art.findall(".//Abstract/AbstractText"):
            label = abs_elem.attrib.get("Label") if abs_elem.attrib else None
            txt = (abs_elem.text or "").strip()
            if not txt:
                continue
            if label:
                texts.append(f"{label}: {txt}")
            else:
                texts.append(txt)
        abstract = "\n".join(texts)

        # --- 著者 ---
        authors = []
        for au in art.findall(".//AuthorList/Author"):
            last = au.findtext("LastName") or ""
            init = au.findtext("Initials") or ""
            if last or init:
                authors.append(f"{last} {init}".strip())
        authors_line = (", ".join(authors[:3]) + (" ほか" if len(authors) > 3 else "")) if authors else ""

        # --- ジャーナル ---
        journal = (art.findtext(".//Journal/Title") or art.findtext(".//Journal/ISOAbbreviation") or "").strip()

        # --- 発行日（欠損に強く） ---
        y = (art.findtext(".//JournalIssue/PubDate/Year") or "").strip()
        m = (art.findtext(".//JournalIssue/PubDate/Month") or "").strip()
        d = (art.findtext(".//JournalIssue/PubDate/Day") or "").strip()
        pubdate = " ".join([x for x in [y, m, d] if x])

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

def _format_bullets(lines, target=4):
    xs = [str(x).strip() for x in (lines or []) if str(x).strip()]
    xs = [("・" + x.lstrip("・-•*・ 　")).strip() for x in xs]
    xs = xs[:target]
    while len(xs) < target:
        xs.append("・（要約が不足しています）")
    xs = [x if len(x) <= 150 else (x[:147] + "…") for x in xs]
    return xs

def summarize_title_and_bullets(title: str, abstract: str) -> dict:
    """
    返り値: {"title_ja": str, "bullets": ["・...", "・...", "・...", "・..."]}
    """
    from google import genai
    # APIキーが渡されていれば明示指定（環境変数経由でもOK）
    client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()

    # 文字数/トークン対策：極端に長い抄録は先頭だけ
    abstract = (abstract or "").strip()
    if len(abstract) > 7000:
        abstract = abstract[:7000]

    prompt = f"""あなたは医学論文の要約編集者です。
放射線治療医向けに、英語タイトルとアブストラクトから、以下を**日本語**で出力してください。
1) "title_ja": タイトルの自然な邦題（30〜45字・1行・名詞止め・冗長な副題は圧縮）
2) "bullets": 重要ポイント**4点**（各60〜120字・事実ベース・過度な推測禁止・記号不要）

**出力はJSONのみ**。フォーマットは正確に次の通り：
{{
  "title_ja": "ここに邦題",
  "bullets": ["ポイント1", "ポイント2", "ポイント3", "ポイント4"]
}}

英語タイトル:
{title}

アブストラクト:
{abstract}
"""
    try:
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = (_resp_to_text(resp) or "").strip()
        data = _force_json(text)
    except Exception:
        data = {}

    title_ja = str((data.get("title_ja") or "")).strip()
    bullets = _format_bullets(data.get("bullets"))

    # 邦題の微整形
    title_ja = title_ja.lstrip("・-•*[]() 　")
    if title_ja.endswith(("。", "．", ".")):
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

    # 1) PubMed検索
    query = build_journal_query(JOURNALS)
    pmids = pubmed_esearch(query)

    # 2) 重複除去
    sent = load_sent_pmids()
    new_pmids = [p for p in pmids if p not in sent]

    items = []
    if new_pmids:
        # 3) まとめて取得→解析
        xml = pubmed_efetch(new_pmids)
        records = parse_records(xml)

        # 4) 各レコードを要約（1論文=1APIコール）
        for rec in records:
            abstract = rec["abstract"]
            if abstract:
                data = summarize_title_and_bullets(rec["title"], abstract)
                rec["title_ja"] = data["title_ja"]
                rec["summary"] = "\n".join(data["bullets"])
                time.sleep(SLEEP_BETWEEN_CALLS)  # 無料枠RPMに配慮
            else:
                rec["title_ja"] = "（邦題生成に失敗）"
                rec["summary"] = "・この論文にはPubMed上でアブストラクトが見つかりません"
            items.append(rec)

        # 5) 送信済み更新
        sent.update([r["pmid"] for r in records])
        save_sent_pmids(sent)

    # 6) メール送信（0件でも通知する運用）
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).strftime("%Y-%m-%d")
    subject = f"新着論文AI要約配信（放射線腫瘍学）{today}"
    body = build_email_body(today, items)
    send_via_gmail(subject, body)

if __name__ == "__main__":
    main()