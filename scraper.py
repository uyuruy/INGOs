import requests
from bs4 import BeautifulSoup
import yaml
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_jobs(url, keywords):
    """
    Fetches ALL text from the page and finds any lines matching keywords.
    This works even when sites change layout, because it does text scanning instead
    of HTML-structure parsing.
    """
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        return [f"[ERROR accessing {url}] {e}"]

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(separator="\n").lower()

    matched_lines = []

    for kw in keywords:
        if kw.lower() in text:
            matched_lines.append(f"Keyword found: '{kw}' at {url}")

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

    report = []
    total_hits = 0

    for org in config["scan"]["orgs"]:
        name = org["name"]
        url = org["url"]
        report.append(f"\n=== {name} ===\nURL: {url}")

        hits = fetch_jobs(url, keywords)

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
