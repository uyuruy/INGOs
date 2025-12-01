# scraper.py — upgraded job-card level scanner
# Behavior:
#  - Extract structured job items (title, url, location, posted/closing dates) using a mix of per-domain parsers and generic heuristics
#  - Match keywords only inside job cards
#  - Include undated jobs if config.scan.allow_undated_jobs == True (flagged)
#  - Apply recency filter only when a date exists
#  - Send structured email with results

import yaml
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from datetime import datetime, timedelta
import time
import re
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from tenacity import retry, wait_exponential, stop_after_attempt

# --- Config loader
def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

# --- HTTP fetch with retries and polite headers
HEADERS = {
    "User-Agent": "JobTrackerBot/1.0 (+usiaphreteheri@gmail.com) Python/requests",
    "Accept-Language": "en-US,en;q=0.9",
}

@retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(3))
def fetch(url, timeout=20):
    return requests.get(url, headers=HEADERS, timeout=timeout)

# --- Utilities
def parse_date(text):
    if not text or not isinstance(text, str):
        return None
    try:
        dt = dateparser.parse(text, fuzzy=True)
        return dt
    except Exception:
        return None

def clean_text(t):
    return re.sub(r"\s+", " ", (t or "").strip())

def contains_keyword(text, keywords):
    txt = (text or "").lower()
    for kw in keywords:
        if kw.lower() in txt:
            return True
    return False

# --- Generic job-card extractor (fallback)
def extract_job_cards_generic(soup):
    """
    Generic heuristic:
    - Look for <article> tags
    - Or <div> with class containing 'job', 'position', 'vacancy', 'opening'
    - Or list items with anchor and short text
    """
    cards = []
    # article tags
    for a in soup.find_all("article"):
        cards.append(a)
    # divs with job-like classes
    pattern = re.compile(r"(job|position|vacancy|opening|role|career|posting)", re.I)
    for d in soup.find_all("div", class_=pattern):
        cards.append(d)
    # li anchors
    for li in soup.find_all("li"):
        if li.find("a"):
            text = li.get_text(" ", strip=True)
            # only include li that are short and look like job entries
            if 5 < len(text.split()) < 60:
                cards.append(li)
    # dedupe by object id
    unique = []
    seen = set()
    for c in cards:
        key = str(c)[:200]
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique

# --- Per-domain parsers map
def parse_workday(soup):
    # Workday usually stores job data in JSON inside <script> tags or exposes job cards with 'data-automation' attrs
    # This is a simple heuristic; will iterate anchors with /job/ in href
    items = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/job/" in href or "wd3.myworkdaysite" in href:
            title = a.get_text(" ", strip=True)
            items.append({"title": clean_text(title), "url": requests.compat.urljoin("https://", href)})
    return items

def parse_icims(soup):
    # iCIMS usually has anchors with class 'iCIMS_Anchor'
    items = []
    for a in soup.select("a"):
        title = a.get_text(" ", strip=True)
        href = a.get("href")
        if href and ("icims.com" in href or "/jobs/" in href):
            if len(title.split()) < 12 and len(title) > 3:
                items.append({"title": clean_text(title), "url": requests.compat.urljoin("https://", href)})
    return items

# Add more per-domain functions as needed (Taleo, iCIMS detail, Workday JSON, etc.)

# --- Parse a job card element into structured info (fallback/generic)
def parse_job_card_element(el, base_url):
    title = ""
    url = None
    location = None
    posted = None
    closing = None
    # title heuristics
    title_tag = el.find(["h1","h2","h3","a","strong","span"])
    if title_tag:
        title = clean_text(title_tag.get_text(" ", strip=True))
        if title_tag.name == "a" and title_tag.get("href"):
            url = requests.compat.urljoin(base_url, title_tag.get("href"))
    # find anchors for url if not found
    if not url:
        a = el.find("a", href=True)
        if a:
            url = requests.compat.urljoin(base_url, a["href"])
    # location heuristics: look for 'location' words
    for lbl in el.find_all(text=re.compile(r"location|based in|city|country", re.I)):
        # attempt to parse next text
        parent = lbl.parent
        if parent:
            txt = parent.get_text(" ", strip=True)
            if "location" not in txt.lower():
                continue
            location = clean_text(txt)
            break
    # date heuristics
    # look for time tags
    time_tag = el.find("time")
    if time_tag and time_tag.get("datetime"):
        posted = parse_date(time_tag.get("datetime"))
    elif time_tag:
        posted = parse_date(time_tag.get_text(" ", strip=True))
    # try to find 'posted' or 'date' or 'closing' words
    text = el.get_text(" ", strip=True)
    # posted date
    m_post = re.search(r"(posted|publish(ed)?|date)[:\s]*([A-Za-z0-9\,\-\s]+)", text, re.I)
    if m_post:
        p = parse_date(m_post.group(3))
        if p:
            posted = p
    # closing date
    m_close = re.search(r"(closing date|deadline|apply by)[:\s]*([A-Za-z0-9\,\-\s]+)", text, re.I)
    if m_close:
        c = parse_date(m_close.group(2))
        if c:
            closing = c
    return {
        "title": title or clean_text(el.get_text(" ", strip=True)[:100]),
        "url": url,
        "location": location,
        "posted": posted.isoformat() if posted else None,
        "closing": closing.isoformat() if closing else None,
        "snippet": clean_text(text)[:400]
    }

# --- Main logic: scan an organization page
def scan_org(org, keywords, recency_days, allow_undated):
    results = []
    url = org.get("url")
    name = org.get("name")
    try:
        r = fetch(url)
        html = r.text
        soup = BeautifulSoup(html, "lxml")
    except Exception as e:
        return [{"org": name, "error": str(e), "url": url}]

    domain = requests.utils.urlparse(url).netloc.lower()

    # per-domain parsing attempts
    candidates = []
    if "myworkday" in domain or "workday" in domain:
        candidates = parse_workday(soup)
        # normalize
        candidates = [{"title": c["title"], "url": c["url"], "location": None, "posted": None, "closing": None, "snippet": ""} for c in candidates]
    elif "icims" in domain:
        candidates = parse_icims(soup)
        candidates = [{"title": c["title"], "url": c["url"], "location": None, "posted": None, "closing": None, "snippet": ""} for c in candidates]
    else:
        # generic job-card extraction
        card_elements = extract_job_cards_generic(soup)
        for el in card_elements:
            parsed = parse_job_card_element(el, url)
            candidates.append(parsed)

    # Now apply keyword logic — but only inside job title or snippet (job card fields)
    cutoff = datetime.utcnow() - timedelta(days=recency_days)
    for c in candidates:
        title = c.get("title") or ""
        snippet = c.get("snippet") or ""
        combined = f"{title} {snippet}".lower()
        matched_kw = [kw for kw in keywords if kw.lower() in combined]
        if not matched_kw:
            continue  # skip if no keyword in title/snippet

        # evaluate recency: if we have a posted date, apply cutoff filter
        posted_iso = c.get("posted")
        include = True
        recency_note = ""
        if posted_iso:
            try:
                posted_dt = dateparser.parse(posted_iso)
                if posted_dt < cutoff:
                    include = False
                else:
                    recency_note = f"posted {posted_dt.date()}"
            except Exception:
                # if parse fails, still include if allow_undated true
                if not allow_undated:
                    include = False
        else:
            # no posted date
            if allow_undated:
                recency_note = "no posted date (included for review)"
            else:
                include = False

        if include:
            results.append({
                "org": name,
                "org_url": url,
                "title": c.get("title"),
                "url": c.get("url"),
                "location": c.get("location"),
                "posted": c.get("posted"),
                "closing": c.get("closing"),
                "recency_note": recency_note
            })
    return results

# --- Email formatting and sending
def make_html_email(results):
    if not results:
        return "<p>No verified matches found.</p>"
    html = ["<h3>Job Tracker — new verified matches</h3>", "<table border='0' cellpadding='6' cellspacing='0'>"]
    html.append("<tr><th align='left'>Org</th><th align='left'>Title</th><th align='left'>Location</th><th align='left'>Posted</th><th align='left'>Notes</th></tr>")
    for r in results:
        title_html = r['title'] if r['title'] else "(no title)"
        if r.get('url'):
            title_html = f"<a href='{r['url']}' target='_blank' rel='noreferrer'>{title_html}</a>"
        posted = r.get('posted') or ""
        notes = r.get('recency_note') or ""
        location = r.get('location') or ""
        html.append(f"<tr><td>{r['org']}</td><td>{title_html}</td><td>{location}</td><td>{posted}</td><td>{notes}</td></tr>")
    html.append("</table>")
    return "\n".join(html)

def send_email_smtp(smtp_cfg, email_cfg, html_body, text_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{email_cfg.get('subject_prefix','[Job Alert]')} {datetime.utcnow().date()}"
    msg["From"] = email_cfg.get("from")
    msg["To"] = ", ".join(email_cfg.get("to", []))
    part1 = MIMEText(text_body, "plain")
    part2 = MIMEText(html_body, "html")
    msg.attach(part1)
    msg.attach(part2)
    s = smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"])
    s.starttls()
    s.login(smtp_cfg["username"], smtp_cfg["password"])
    s.sendmail(email_cfg.get("from"), email_cfg.get("to"), msg.as_string())
    s.quit()

# --- Main runner
def run():
    cfg = load_config()
    recency_days = cfg["scan"].get("recency_days", 14)
    allow_undated = cfg["scan"].get("allow_undated_jobs", True)
    keywords = cfg["scan"].get("keywords", [])
    all_results = []
    errors = []

    for org in cfg["scan"].get("orgs", []):
        try:
            items = scan_org(org, keywords, recency_days, allow_undated)
            if items:
                all_results.extend(items)
            time.sleep(1.2)  # polite pause between orgs
        except Exception as e:
            errors.append({"org": org.get("name"), "url": org.get("url"), "error": str(e)})

    # Prepare and send email if results exist
    if all_results:
        html = make_html_email(all_results)
        text = "\n".join([f"{r['org']} | {r['title']} | {r.get('url')} | posted: {r.get('posted')} | notes: {r.get('recency_note')}" for r in all_results])
        send_email_smtp(cfg["smtp"], cfg["email"], html, text)
        print(f"Sent email with {len(all_results)} matches.")
    else:
        print("No matches found for this run.")

    if errors:
        print("Errors encountered:", errors)


if __name__ == "__main__":
    run()
