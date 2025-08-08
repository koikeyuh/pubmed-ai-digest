#!/usr/bin/env python3
import os, json, time, ssl, smtplib, requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET

# ==== 環境変数 ====
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")  # AI Studio 無料枠のFlash
JOURNALS = [j.strip() for j in os.getenv("JOURNALS", "").split(",") if j.strip()]
RECIPIENT = os.getenv("RECIPIENT_EMAIL", os.getenv("GMAIL_ADDRESS"))
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
PUBMED_TOOL_EMAIL = os.getenv("PUBMED_TOOL_EMAIL", GMAIL_ADDRESS)  # eutilsの&emailに使用
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "")  # 任意（無くても可）

# ==== PubMed E-utilities 基本 ====
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TOOL_NAME = "pubmed-daily-digest"
HEADERS = {"User-Agent": TOOL_NAME}

# ==== 送信済みPMID保存 ====
STATE_PATH = "sent_pmids.json"

def load_sent_pmids():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_sent_pmids(pmids):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(pmids)), f, ensure_ascii=False, indent=2)

def build_journal_query(journals):
    # PubMed推奨のジャーナルフィールド [ta] でOR結合
    parts = [f'("{j}"[ta])' for j in journals]
    return "(" + " OR ".join(parts) + ")"

def pubmed_esearch(term):
    # 直近2日 (バッファ) の追加日付 EDAT を対象（重複は別で除外）
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "datetype": "edat",
        "reldate": "2",
        "retmax": "200",
        "sort": "pub_date",
        "tool": TOOL_NAME,
        "email": PUBMED_TOOL_EMAIL
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY  # レート上限UP（任意） 
    r = requests.get(EUTILS_BASE + "esearch.fcgi", params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("esearchresult", {}).get("idlist", [])

def pubmed_efetch(pmids):
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "abstract",
        "retmode": "xml",
        "tool": TOOL_NAME,
        "email": PUBMED_TOOL_EMAIL
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    r = requests.get(EUTILS_BASE + "efetch.fcgi", params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.text

def parse_records(xml_text):
    """EFetch XMLを解析して必要項目を抜き出す"""
    root = ET.fromstring(xml_text)
    ns = {}  # PubMed XMLは名前空間なし
    results = []
    for art in root.findall(".//PubmedArticle", ns):
        pmid = (art.findtext(".//PMID") or "").strip()
        art_title = (art.findtext(".//Article/ArticleTitle") or "").strip()
        # アブストラクト
        ab_elems = art.findall(".//Abstract/AbstractText")
        abstract = "\n".join([(e.text or "").strip() for e in ab_elems if (e.text or "").strip()]) or ""
        # 著者（先頭3名 + et al.）
        authors = []
        for au in art.findall(".//AuthorList/Author"):
            last = au.findtext("LastName") or ""
            init = au.findtext("Initials") or ""
            if last or init:
                authors.append(f"{last} {init}".strip())
        if len(authors) > 3:
            authors_line = ", ".join(authors[:3]) + " ほか"
        else:
            authors_line = ", ".join(authors)
        # ジャーナル名
        journal = (art.findtext(".//Journal/Title") or art.findtext(".//Journal/ISOAbbreviation") or "").strip()
        # 発行日（PubDate → 年/月/日が無い場合があるので安全にまとめる）
        y = (art.findtext(".//JournalIssue/PubDate/Year") or "").strip()
        m = (art.findtext(".//JournalIssue/PubDate/Month") or "").strip()
        d = (art.findtext(".//JournalIssue/PubDate/Day") or "").strip()
        pubdate = " ".join([x for x in [y, m, d] if x])
        # DOI
        doi = ""
        for aid in art.findall(".//ArticleIdList/ArticleId"):
            if (aid.attrib or {}).get("IdType", "").lower() == "doi":
                doi = (aid.text or "").strip()
                break
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        results.append({
            "pmid": pmid, "title": art_title, "authors": authors_line, "journal": journal,
            "pubdate": pubdate, "doi": doi, "url": url, "abstract": abstract
        })
    return results

# ==== Gemini 要約 ====
def summarize_ja_bullets(text: str, title: str):
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')

    prompt = f"""
        以下の医学論文のアブストラクトを読んで、放射線腫瘍学の専門家向けに、重要なポイントを日本語で4つの箇条書きで日本語に要約してください。
        
        論文タイトル: {title}
        
        アブストラクト:
        {abstract}
        
        出力形式:
        ・[ポイント1]
        ・[ポイント2]
        ・[ポイント3]
        ・[ポイント4]
        """
    try:
        response = self.model.generate_content(prompt)
        # 箇条書きを抽出
        lines = response.text.strip().split('\n')
        points = [line.strip() for line in lines if line.strip().startswith('・')]
            
        if len(points) >= 4:
            return points[:4]
        else:
            # 不足分を補完
            return points + ["詳細はアブストラクトを参照してください"] * (4 - len(points))
    except Exception as e:
        print(f"要約エラー: {e}")
        return [
            "・AIによる要約生成に失敗しました",
            "・原文をご確認ください",
            "・一時的なエラーの可能性があります",
            "・後ほど再試行してください"
        ]

# ==== メール整形・送信 ====
def build_email_body(date_jst_str, items):
    lines = []
    lines.append("新着論文AI要約配信\n")
    lines.append("放射線腫瘍学\n\n")
    lines.append(f"本日の新着論文は{len(items)}件です。\n\n")
    for i, it in enumerate(items, 1):
        lines.append(f"[論文{i}]")
        lines.append(f"原題：{it['title']}")
        if it['authors']:
            lines.append(f"著者：{it['authors']}")
        lines.append(f"雑誌名：{it['journal']}")
        lines.append(f"発行日：{it['pubdate']}")
        lines.append(f"Pubmed：{it['url']}")
        lines.append(f"DOI：{it['doi'] or '-'}")
        lines.append("要約（AI生成）：")
        lines.append(it['summary'])
        lines.append("\n")
    return "\n".join(lines)

def send_via_gmail(subject, body):
    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [RECIPIENT], msg.as_string())

def main():
    if not JOURNALS:
        raise SystemExit("環境変数 JOURNALS が未設定です。カンマ区切りでジャーナル名を指定してください。")

    query = build_journal_query(JOURNALS)
    pmids = pubmed_esearch(query)

    sent = load_sent_pmids()
    new_pmids = [p for p in pmids if p not in sent]

    items = []
    if new_pmids:
        # PubMedへの礼儀として小休止（ポリシー: ≤3 rps / API keyで≤10 rps）
        # まとめてEFetchするので通常1リクエスト
        xml = pubmed_efetch(new_pmids)
        records = parse_records(xml)

        for rec in records:
            abstract = rec["abstract"]
            if abstract:
                # 要約（1レコード毎に1回呼ぶ）
                try:
                    rec["summary"] = summarize_ja_bullets(abstract, rec["title"])
                    time.sleep(0.2)  # 無料枠RPMに配慮
                except Exception as e:
                    rec["summary"] = "・要約生成に失敗しました"
            else:
                rec["summary"] = "・この論文にはPubMed上でアブストラクトが見つかりません"

            items.append(rec)

        # 送信済み更新
        sent.update([r["pmid"] for r in records])
        save_sent_pmids(sent)

    # JSTの本日日付で件名
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).strftime("%Y-%m-%d")
    subject = f"新着論文AI要約配信（放射線腫瘍学）{today}"
    body = build_email_body(today, items)

    # 件数0でも配信（ご希望フォーマットに合わせる）
    send_via_gmail(subject, body)

if __name__ == "__main__":
    main()
