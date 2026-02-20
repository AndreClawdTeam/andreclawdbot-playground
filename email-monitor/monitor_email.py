#!/usr/bin/env python3
"""IMAP poller for Proton via hydroxide — stores emails in SQLite."""
from __future__ import annotations

import email
import email.policy
import imaplib
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

HOST = "127.0.0.1"
PORT = 1143
USERNAME = "andreclawdbot"
PASSWORD = "puwE7aJWfp2KRkaqjlGAGR2WWUxFcTLiJnWgjgRiqCE="
TELEGRAM_CHANNEL = "telegram"
TELEGRAM_TARGET = "7965086167"

DB_PATH = pathlib.Path(__file__).with_name("emails.db")
LOG_FILE = pathlib.Path(__file__).with_name("inbox.log")
POLL_SECONDS = 30


def log(text: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"[{timestamp}] {text}\n")


def init_db(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS emails (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            uid         INTEGER UNIQUE,
            from_name   TEXT,
            from_email  TEXT,
            subject     TEXT,
            body        TEXT,
            received_at TEXT,
            is_read     INTEGER DEFAULT 0,
            is_notified INTEGER DEFAULT 0,
            is_replied  INTEGER DEFAULT 0
        )
    """)
    con.commit()


def strip_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-z]+;", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_body(msg) -> str:
    """Extract plain text body — prefers text/plain, falls back to text/html."""
    plain = ""
    html = ""

    parts = list(msg.walk()) if msg.is_multipart() else [msg]
    for part in parts:
        ct = part.get_content_type()
        disp = part.get("Content-Disposition", "")
        if disp and "attachment" in disp:
            continue
        try:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not plain:
                plain = text.strip()
            elif ct == "text/html" and not html:
                html = strip_html(text)
        except Exception:
            pass

    body = plain or html
    return body[:4000]


def parse_address(raw: str) -> tuple[str, str]:
    """Parse 'Name <email>' into (name, email)."""
    raw = raw or ""
    match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>', raw.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return "", raw.strip()


def notify(from_name: str, from_email: str, subject: str, uid: int) -> None:
    sender = from_name if from_name else from_email
    preview = f"📬 Novo e-mail (UID {uid})\nDe: {sender} <{from_email}>\nAssunto: {subject}"
    try:
        subprocess.run(
            ["openclaw", "message", "send",
             "--channel", TELEGRAM_CHANNEL,
             "--target", TELEGRAM_TARGET,
             "--message", preview],
            check=True, capture_output=True,
        )
        log(f"Notified UID {uid} from {from_email!r} subject {subject!r}")
    except subprocess.CalledProcessError as exc:
        log(f"Notify failed UID {uid}: code={exc.returncode} stderr={exc.stderr!r}")


def fetch_new(con: sqlite3.Connection) -> None:
    known_uids = {row[0] for row in con.execute("SELECT uid FROM emails")}
    try:
        with imaplib.IMAP4(HOST, PORT) as imap:
            imap.login(USERNAME, PASSWORD)
            imap.select("INBOX")
            result, data = imap.uid("search", None, "ALL")
            if result != "OK":
                log(f"Search failed: {result} {data}")
                return
            all_uids = [int(u) for u in data[0].split() if u]
            new_uids = [u for u in all_uids if u not in known_uids]
            for uid in new_uids:
                result, msg_data = imap.uid("fetch", str(uid), "(RFC822)")
                if result != "OK" or not msg_data or msg_data[0] is None:
                    log(f"Fetch failed UID {uid}")
                    continue
                raw_bytes = msg_data[0][1]
                msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
                subject = str(msg.get("Subject", "(sem assunto)"))
                from_raw = str(msg.get("From", ""))
                from_name, from_email = parse_address(from_raw)
                body = extract_body(msg)
                received_at = datetime.now(timezone.utc).isoformat()
                con.execute(
                    """INSERT OR IGNORE INTO emails
                       (uid, from_name, from_email, subject, body, received_at, is_notified)
                       VALUES (?, ?, ?, ?, ?, ?, 1)""",
                    (uid, from_name, from_email, subject, body, received_at),
                )
                con.commit()
                log(f"Saved UID {uid} from {from_email!r} subject {subject!r}")
    except Exception as exc:
        log(f"Exception: {exc}")


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    init_db(con)
    log("Starting IMAP monitor (SQLite mode)")
    while True:
        fetch_new(con)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Monitor stopped")
        sys.exit(0)
