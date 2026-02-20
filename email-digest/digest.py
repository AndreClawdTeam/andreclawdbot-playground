#!/usr/bin/env python3
"""
Email digest: queries unsummarized emails from SQLite, sends a Telegram summary,
marks them as summarized. Safe to call multiple times — never re-summarizes.
"""
import html
import sqlite3
import subprocess
import pathlib

DB_PATH = pathlib.Path(__file__).with_name("emails.db")


def run_digest() -> None:
    con = sqlite3.connect(DB_PATH)

    rows = con.execute(
        """SELECT id, uid, from_name, from_email, subject, body, received_at
           FROM emails
           WHERE is_summarized = 0
           ORDER BY uid ASC"""
    ).fetchall()

    if not rows:
        con.close()
        return  # Nothing new, stay silent

    MAX_MSG_LEN = 3800  # Telegram limit is 4096; leave headroom

    def send_message(text: str) -> bool:
        try:
            subprocess.run(
                [
                    "openclaw", "message", "send",
                    "--channel", "telegram",
                    "--target", "7965086167",
                    "--message", text,
                ],
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    # Build per-email blocks
    header = f"📬 *{len(rows)} novo(s) e-mail(s)*\n"
    email_blocks = []
    for row in rows:
        _, uid, from_name, from_email, subject, body, received_at = row
        sender = from_name if from_name else from_email
        clean_body = html.unescape(body or "").strip()
        preview = clean_body[:200] + ("…" if len(clean_body) > 200 else "")
        date = received_at[:10] if received_at else "?"
        block_lines = [
            "━━━━━━━━━━",
            f"📧 UID {uid} · {date}",
            f"De: {sender}",
            f"Assunto: {subject}",
        ]
        if preview:
            block_lines.append(f"Preview: {preview}")
        email_blocks.append((row[0], "\n".join(block_lines)))

    # Split into chunks that fit within Telegram's limit
    chunks: list[list[int]] = []  # list of (ids, text) per chunk
    current_ids: list[int] = []
    current_lines: list[str] = [header]
    current_len = len(header)

    for row_id, block in email_blocks:
        block_len = len(block) + 1  # +1 for newline
        if current_ids and current_len + block_len > MAX_MSG_LEN:
            chunks.append((current_ids[:], "\n".join(current_lines)))
            current_ids = []
            current_lines = []
            current_len = 0
        current_ids.append(row_id)
        current_lines.append(block)
        current_len += block_len

    if current_ids:
        chunks.append((current_ids, "\n".join(current_lines)))

    # Send all chunks; only mark summarized after all succeed
    all_ok = True
    for chunk_ids, chunk_text in chunks:
        if not send_message(chunk_text):
            all_ok = False
            break

    if all_ok:
        ids = [row[0] for row in rows]
        con.execute(
            f"UPDATE emails SET is_summarized=1 WHERE id IN ({','.join('?'*len(ids))})",
            ids,
        )
        con.commit()

    con.close()


if __name__ == "__main__":
    run_digest()
