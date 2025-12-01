import requests
from bs4 import BeautifulSoup
import yaml
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
import re


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_dates(text):
    """
    Extract date strings from text in common formats.
    Returns list of datetime objects.
    """
    patterns = [
        r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",   # 24 Nov 2025
        r"\b\d{4}-\d{2}-\d{2}\b",                 # 2025-11-24
        r"\b[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}\b"   # Dec 15, 2025
    ]
    dates = []
    for pat in patterns:
        for match in re.findall(pat, text):
            for fmt in ["%d %b %Y", "%Y-%m-%d", "%b %d, %Y"]:
                try:
                    dt = datetime.strptime(match, fmt)
                    dates.append(dt)
                    break
                except:
                    continue
    return dates


def fetch_jobs(url, keywords, cutoff_days):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36"
    }
    try:
        r = requests.get(url, headers=headers, timeout=30, verify=True)
        r.raise_for_status()
    except Exception as e:
        return [f"[ERROR accessing {url}] {e}"]

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(separator="\n").lower()

    matched_lines = []
    cutoff = datetime.now() - timedelta(days=cutoff_days)

    for kw in keywords:
        if kw.lower() in text:
            dates = parse_dates(text)
            if dates:
                recent = [d for d in dates if d >= cutoff]
                if recent:
                    matched_lines.append(f"Keyword '{kw}' with recent date at {url}")
                else:
                    matched_lines.append(f"Keyword '{kw}' but dates look old at {url}")
            else:
                matched_lines.append(f"Keyword '{kw}' (no date found) at {url}")

    return matched_lines


def send_email(smtp_cfg, email_cfg, body):
    msg = MIMEText(body)
    msg["Subject"] = email_cfg["subject_prefix"] + " New Matches Detected"
    msg["From"] = email_cfg["from"]
    msg["To"] = ", ".join(email_cfg["to"])

    server = smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"])
    server.starttls()
    server.login(smtp_cfg["username"], smtp_cfg["password"])
    server.sendmail(email_cfg["from"], email_cfg["to"], msg.as_string())
    server.quit()


def main():
    config = load_config()
    keywords = config["scan"]["keywords"]
    cutoff_days = config["scan"].get("recency_days", 30)

    report = []
    total_hits = 0

    for org in config["scan"]["orgs"]:
        name = org["name"]
        url = org["url"]
        report.append(f"\n=== {name} ===\nURL: {url}")

        hits = fetch_jobs(url, keywords, cutoff_days)
        if hits:
            total_hits += len(hits)
            for h in hits:
                report.append(" - " + h)
        else:
            report.append(" - No matches found")

    if total_hits > 0:
        send_email(config["smtp"], config["email"], "\n".join(report))
        print("Email sent with matches.")
    else:
        print("No matches found. No email sent.")


if __name__ == "__main__":
    main()
