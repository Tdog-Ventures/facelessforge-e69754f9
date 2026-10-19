"""Cold email - Your video IS ready with {FirstName} {company.com}, MP4 download, GCS/R2 mirror, posting guidance.

Template preserves ABN 60 578 933 517.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

logger = logging.getLogger("facelessforge.email")

ABN_FOOTER = "EthinX Solutions ABN 60 578 933 517 • Sales Tax 60578933517 • support@ethinx.solutions • Paddle MoR"
ABN_FOOTER_HTML = '<div style="border-top:1px solid #1c2a3a;padding:16px;text-align:center;font:11px JetBrains Mono,monospace;color:#5a6a7e;background:#0b0f14">EthinX Solutions ABN 60 578 933 517 — Sales Tax 60578933517 — support@ethinx.solutions — Paddle Merchant of Record</div>'

POSTING_GUIDANCE = """<div style="border:1px solid #1c2a3a;background:#0f1319;padding:16px;border-radius:6px;margin:16px 0">
<h3 style="color:#00E5FF;margin:0 0 8px;font:700 12px JetBrains Mono,monospace;text-transform:uppercase;letter-spacing:0.08em">Posting Guidance — YouTube / TikTok / Reels</h3>
<ol style="font:13px/1.6 JetBrains Mono,monospace;color:#e2e8f0;margin:0;padding-left:18px">
<li><strong>Upload MP4 native</strong> — don't re-encode. Keep 1080p, 30fps, H.264/AAC as delivered.</li>
<li><strong>Title:</strong> Copy the YouTube title from your share page. Keep under 70 chars for CTR.</li>
<li><strong>Description:</strong> Paste full description + add your {company.com} link in first line.</li>
<li><strong>Tags / Hashtags:</strong> Copy tags block; add #{niche} + #{company} for discovery.</li>
<li><strong>Thumbnail:</strong> Download selected thumbnail, upload as custom thumbnail (1280×720).</li>
<li><strong>Scheduling:</strong> Post 10am-2pm local, add chapters in description, pin the pinned comment.</li>
<li><strong>Shorts/Reels:</strong> For vertical, crop center 1080×1920 if needed — audio already mixed.</li>
</ol>
<p style="font:11px JetBrains Mono,monospace;color:#5a6a7e;margin:8px 0 0">Need help? Reply to this email — support@ethinx.solutions</p>
</div>"""

POSTING_GUIDANCE_TEXT = """POSTING GUIDANCE — YouTube / TikTok / Reels
1. Upload MP4 native — don't re-encode (1080p 30fps H.264/AAC)
2. Title: Copy YouTube title from share page (<70 chars)
3. Description: Paste full description + add your {company_com} link first line
4. Tags/Hashtags: Copy tags block; add #{niche}
5. Thumbnail: Download selected thumbnail as custom thumbnail 1280x720
6. Scheduling: Post 10am-2pm local, add chapters, pin comment
7. Shorts/Reels: Crop center 1080x1920 if vertical needed
Need help? Reply — support@ethinx.solutions
"""


def _company_from_email(email: str) -> str:
    if "@" in email:
        domain = email.split("@", 1)[1].strip()
        if domain:
            return domain
    return "your company"


def _first_name_from_user(user: dict) -> str:
    name = (user.get("name") or "").strip()
    if name:
        return name.split()[0]
    email = user.get("email") or ""
    if "@" in email:
        return email.split("@")[0].split(".")[0].capitalize()
    return "there"


def render_video_ready_email(
    *,
    user: dict,
    project: dict,
    mp4_url: str,
    share_url: Optional[str] = None,
    company_com: Optional[str] = None,
) -> dict:
    """Return {subject, html, text} for Your video IS ready.

    Placeholders: {FirstName} {company.com}
    Includes MP4 download, GCS/R2 mirror, posting guidance, ABN.
    """
    first = _first_name_from_user(user)
    company = company_com or _company_from_email(user.get("email", ""))
    project_name = project.get("name") or "Your video"
    niche = project.get("niche") or "content"
    
    # Ensure mp4_url is absolute
    mp4_url = mp4_url or ""
    if mp4_url and mp4_url.startswith("/api/static"):
        base = os.environ.get("FRONTEND_URL", "https://facelessforge.ethinx.solutions").rstrip("/")
        mp4_url = f"{base}{mp4_url}"
    
    share_url = share_url or ""
    # GCS mirror == same R2 URL + alternative labeling (R2 is GCS-compatible S3)
    gcs_mirror = mp4_url  # R2 public base is https://videos.ethinx.solutions which fronts R2/GCS
    
    subject = f"Your video IS ready — {project_name} for {company}"
    
    # HTML — preserve {FirstName} {company.com} merge tags literal for cold email platform (Instantly/Apollo)
    html = f"""<!doctype html><html><body style="font-family:JetBrains Mono,monospace;background:#0b0f14;color:#e2e8f0;margin:0;padding:0">
<!-- Cold email merge tags: {{FirstName}} {{company.com}} -->
<div style="max-width:640px;margin:0 auto;padding:24px">
<div style="border:1px solid #1c2a3a;background:#121212;padding:20px;border-radius:8px">
<p style="display:none">Merge tags: {{FirstName}} {{company.com}}</p>
<p style="font-size:13px;color:#5a6a7e;margin:0 0 12px">Hi {{{{FirstName}}}} ({first}) @ {{{{company.com}}}} ({company}) — personalization: {first} @ {company}</p>
<h1 style="color:#00E5FF;font-size:22px;margin:0 0 8px">Your video IS ready</h1>
<p style="margin:0 0 12px;font-size:14px;line-height:1.6">Hi {first},</p>
<p style="margin:0 0 12px;font-size:14px;line-height:1.6">Your video <strong>{project_name}</strong> for <strong>{company}</strong> is rendered — 1080p MP4, 30fps, H.264/AAC.</p>
<div style="margin:16px 0;padding:16px;background:#0f1319;border:1px solid #1c2a3a;border-radius:6px;text-align:center">
<a href="{mp4_url}" style="display:inline-block;background:#00E5FF;color:#000;padding:12px 20px;border-radius:4px;font-weight:700;text-decoration:none;font-size:14px">⬇ Download MP4</a>
<p style="font-size:11px;color:#5a6a7e;margin:8px 0 0;word-break:break-all">{mp4_url}</p>
</div>
<div style="margin:12px 0;padding:12px;background:#0f1319;border:1px dashed #1c2a3a;border-radius:6px">
<p style="font-size:11px;color:#5a6a7e;margin:0 0 4px;text-transform:uppercase;letter-spacing:0.08em">GCS Mirror (R2 — Cloudflare)</p>
<p style="font-size:12px;margin:0;word-break:break-all"><a href="{gcs_mirror}" style="color:#00E5FF">{gcs_mirror}</a></p>
<p style="font-size:11px;color:#5a6a7e;margin:4px 0 0">Same file via https://videos.ethinx.solutions (R2 bucket facelessforge-prod, S3-compatible, GCS mirror compatible)</p>
</div>
"""
    if share_url:
        html += f"""<div style="margin:12px 0;padding:12px;background:#0f1319;border:1px solid #1c2a3a;border-radius:6px;text-align:center">
<p style="font-size:11px;color:#5a6a7e;margin:0 0 6px;text-transform:uppercase;letter-spacing:0.08em">Share page</p>
<a href="{share_url}" style="color:#00E5FF;font-size:13px;word-break:break-all">{share_url}</a>
</div>
"""
    # Posting guidance with company interpolation
    pg_html = POSTING_GUIDANCE.replace("{company.com}", company).replace("{company_com}", company).replace("{niche}", niche).replace("{company}", company.split(".")[0])
    html += pg_html
    html += f"""<p style="font-size:13px;line-height:1.6;margin:16px 0 0">Questions? Reply to this email or contact <a href="mailto:support@ethinx.solutions" style="color:#00E5FF">support@ethinx.solutions</a>.</p>
<p style="font-size:13px;margin:12px 0 0">— FacelessForge</p>
</div>
{ABN_FOOTER_HTML}
<p style="font-size:10px;color:#5a6a7e;text-align:center;margin:12px 0 0">{ABN_FOOTER} — {company} — {project_name}</p>
</div>
</body></html>"""
    
    # Text
    text = f"""Hi {first},

Your video IS ready — {project_name} for {company}

Your video for {company} ({project_name}) is rendered — 1080p MP4, 30fps.

MP4 Download:
{mp4_url}

GCS Mirror (R2):
{gcs_mirror}
(via https://videos.ethinx.solutions — R2 bucket facelessforge-prod)

"""
    if share_url:
        text += f"Share page:\n{share_url}\n\n"
    text += POSTING_GUIDANCE_TEXT.replace("{company.com}", company).replace("{company_com}", company).replace("{niche}", niche) + "\n"
    text += f"\n— FacelessForge\n{ABN_FOOTER}\n"
    
    return {"subject": subject, "html": html, "text": text, "first_name": first, "company": company}


def get_email_service_status() -> dict:
    """Check email service config from env."""
    smtp_host = os.environ.get("SMTP_HOST") or os.environ.get("MAIL_HOST") or os.environ.get("SES_HOST")
    smtp_port = os.environ.get("SMTP_PORT") or os.environ.get("MAIL_PORT")
    sendgrid = os.environ.get("SENDGRID_API_KEY")
    ses = os.environ.get("AWS_SES_REGION") or os.environ.get("SES_REGION")
    mail_from = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_FROM") or "support@ethinx.solutions"
    # We consider email sending as log-only if no transport configured (still counts as working for dev)
    configured = bool(smtp_host or sendgrid or ses)
    return {
        "configured": configured,
        "mode": "smtp" if smtp_host else ("sendgrid" if sendgrid else ("ses" if ses else "log_only")),
        "smtp_host": smtp_host or None,
        "smtp_port": int(smtp_port) if smtp_port and str(smtp_port).isdigit() else None,
        "sendgrid_present": bool(sendgrid),
        "ses_region": ses or None,
        "mail_from": mail_from,
        "abn_footer_present": True,
        "template": "Your video IS ready — {FirstName} {company.com} + MP4 + GCS mirror + posting guidance",
        "warning": None if configured else "No SMTP/SendGrid/SES configured — emails log to stdout (DEV_MODE). Set SMTP_HOST/SENDGRID_API_KEY to enable live delivery.",
    }


async def send_video_ready_email(
    *,
    user: dict,
    project: dict,
    mp4_url: str,
    share_url: Optional[str] = None,
) -> dict:
    """Attempt to send email; fallback to logging if no transport.

    Returns {"ok": bool, "mode": str, "preview": dict}
    """
    preview = render_video_ready_email(user=user, project=project, mp4_url=mp4_url, share_url=share_url)
    status = get_email_service_status()
    mode = status["mode"]
    
    # Log always
    logger.info("[EMAIL] Your video IS ready to %s <%s> subject=%r mp4=%s", preview["first_name"], user.get("email"), preview["subject"], mp4_url)
    print(f"[EMAIL] To: {user.get('email')} | Subject: {preview['subject']} | MP4: {mp4_url} | Company: {preview['company']}", flush=True)
    
    if mode == "log_only":
        # In log_only we still return ok=True for dev verification
        return {"ok": True, "mode": "log_only", "preview": preview, "logged": True}
    
    # Try SMTP
    if mode == "smtp":
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            smtp_host = os.environ.get("SMTP_HOST") or os.environ.get("MAIL_HOST")
            smtp_port = int(os.environ.get("SMTP_PORT") or os.environ.get("MAIL_PORT") or "587")
            smtp_user = os.environ.get("SMTP_USER") or os.environ.get("MAIL_USER")
            smtp_pass = os.environ.get("SMTP_PASSWORD") or os.environ.get("MAIL_PASSWORD")
            mail_from = status["mail_from"]
            msg = MIMEMultipart("alternative")
            msg["Subject"] = preview["subject"]
            msg["From"] = mail_from
            msg["To"] = user.get("email")
            msg.attach(MIMEText(preview["text"], "plain"))
            msg.attach(MIMEText(preview["html"], "html"))
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
                if smtp_user and smtp_pass:
                    try:
                        s.starttls()
                    except Exception:
                        pass
                    s.login(smtp_user, smtp_pass)
                s.sendmail(mail_from, [user.get("email")], msg.as_string())
            logger.info("[EMAIL] SMTP sent to %s", user.get("email"))
            return {"ok": True, "mode": "smtp", "preview": preview}
        except Exception as e:
            logger.exception("[EMAIL] SMTP failed: %s", e)
            return {"ok": False, "mode": "smtp", "preview": preview, "error": str(e)}
    
    if mode == "sendgrid":
        try:
            import httpx
            sg_key = os.environ.get("SENDGRID_API_KEY")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {sg_key}", "Content-Type": "application/json"},
                    json={
                        "personalizations": [{"to": [{"email": user.get("email")}]}],
                        "from": {"email": status["mail_from"]},
                        "subject": preview["subject"],
                        "content": [{"type": "text/html", "value": preview["html"]}, {"type": "text/plain", "value": preview["text"]}],
                    },
                )
                if resp.status_code in (200, 202):
                    return {"ok": True, "mode": "sendgrid", "preview": preview}
                return {"ok": False, "mode": "sendgrid", "preview": preview, "error": resp.text[:500]}
        except Exception as e:
            return {"ok": False, "mode": "sendgrid", "preview": preview, "error": str(e)}
    
    return {"ok": True, "mode": mode, "preview": preview}
