"""Envoi explicite d'un PDF par Gmail SMTP avec mot de passe d'application."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def send_pdf(pdf: Path, *, date_label: str) -> None:
    sender = os.environ.get("GMAIL_SENDER", "").strip()
    recipient = os.environ.get("GMAIL_RECIPIENT", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not all((sender, recipient, password)):
        raise ValueError("GMAIL_SENDER, GMAIL_RECIPIENT et GMAIL_APP_PASSWORD sont requis")
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise FileNotFoundError(f"PDF absent ou vide : {pdf}")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"Signal Matin · {date_label}"
    message.set_content("Votre journal matinal est en pièce jointe. Les sources sont cliquables dans le PDF.")
    message.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)
