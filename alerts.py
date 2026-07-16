"""Deal alert delivery: email (Gmail SMTP) and macOS notification banners."""

import logging
import smtplib
import subprocess
from email.message import EmailMessage

log = logging.getLogger(__name__)


def fmt_duration(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


def deal_line(d) -> str:
    """One-line plain-text summary of a deal dict."""
    stops = "nonstop" if d["stops"] == 0 else f"1 stop {d['stop_airport']} ({fmt_duration(d['layover_min'])})"
    line = (f"{d['origin']}->{d['destination']} {d['dep_date']} - {d['ret_date']}  "
            f"${d['price_pp']:,}/person RT  {', '.join(d['airlines'])} "
            f"[{d.get('cabin', 'business')}]  {stops}  "
            f"outbound {fmt_duration(d['total_duration_min'])}")
    if d.get("typical_low_pp"):
        line += f"  (typical ${d['typical_low_pp']:,}-${d['typical_high_pp']:,})"
    if d.get("note"):
        line += f"  [{d['note']}]"
    return line


def build_email(deals: list[dict], adults: int) -> tuple[str, str, str]:
    """Returns (subject, plain_text, html)."""
    best = min(deals, key=lambda d: d["price_pp"])
    subject = (f"✈️ {len(deals)} business-class deal{'s' if len(deals) > 1 else ''} "
               f"— {best['origin']}→{best['destination']} ${best['price_pp']:,}/person round trip")

    text_lines = [f"Found {len(deals)} qualifying business-class fare(s), priced for {adults} travelers:", ""]
    for d in sorted(deals, key=lambda x: x["price_pp"]):
        text_lines += [deal_line(d), d["url"]]
        if d.get("url_ret"):
            text_lines.append("return leg: " + d["url_ret"])
        text_lines.append("")
    text = "\n".join(text_lines)

    rows = []
    for d in sorted(deals, key=lambda x: x["price_pp"]):
        stops = "nonstop" if d["stops"] == 0 else f"1 stop in {d['stop_airport']}<br>({fmt_duration(d['layover_min'])} layover)"
        typical = (f"${d['typical_low_pp']:,} – ${d['typical_high_pp']:,}"
                   if d.get("typical_low_pp") else "—")
        note = f'<div style="color:#b45309;font-size:12px;margin-top:4px">{d["note"]}</div>' if d.get("note") else ""
        rows.append(f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;white-space:nowrap">
            <b>{d['origin']} → {d['destination']}</b><br>
            <span style="color:#6b7280;font-size:13px">{d['dep_date']} → {d['ret_date']}</span>{note}
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb">
            {', '.join(d['airlines'])} · {d.get('cabin', 'business')}<br>
            <span style="color:#6b7280;font-size:13px">{stops} · outbound {fmt_duration(d['total_duration_min'])}</span>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;white-space:nowrap">
            <b style="font-size:16px">${d['price_pp']:,}</b><span style="color:#6b7280">/person
            {'(2 one-way tickets)' if d.get('url_ret') else 'round trip'}</span><br>
            <span style="color:#6b7280;font-size:12px">Google shows ${d['price_pp'] * adults:,} = total for {adults}</span><br>
            <span style="color:#6b7280;font-size:12px">typical {typical}/person</span>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb">
            <a href="{d['url']}" style="color:#2563eb">Open in Google Flights</a>
            {f'<br><a href="{d["url_ret"]}" style="color:#2563eb;font-size:13px">Return leg</a>' if d.get('url_ret') else ''}
          </td>
        </tr>""")

    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px">
      <h2 style="margin:0 0 4px">✈️ Business-class deals found</h2>
      <p style="color:#6b7280;margin:0 0 16px">Round-trip, taxes included, priced for {adults} travelers
      (shown per person). Prices move fast — verify on Google Flights before booking.</p>
      <table style="border-collapse:collapse;width:100%">{''.join(rows)}</table>
      <p style="color:#9ca3af;font-size:12px;margin-top:16px">Sent by your Flight Deal Watcher.
      The Google Flights link opens the outbound date — pick any return around the shown date.</p>
    </div>"""
    return subject, text, html


def send_email(cfg: dict, subject: str, text: str, html: str) -> bool:
    email_cfg = cfg.get("email", {})
    password = (email_cfg.get("app_password") or "").replace(" ", "")
    if not email_cfg.get("enabled") or not password:
        log.warning("email not configured (set [email] app_password in config.toml) - skipping email")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg.get("username")
    msg["To"] = email_cfg.get("to")
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(email_cfg.get("smtp_host", "smtp.gmail.com"),
                      int(email_cfg.get("smtp_port", 587)), timeout=30) as s:
        s.starttls()
        s.login(email_cfg["username"], password)
        s.send_message(msg)
    log.info("alert email sent to %s", email_cfg.get("to"))
    return True


def send_sms(cfg: dict, message: str) -> bool:
    """Text message via AWS SNS. Never raises - SMS is one of several channels
    and a failure here must not block the others."""
    sms = cfg.get("sms", {})
    if not sms.get("enabled") or not sms.get("phone_number"):
        return False
    try:
        import boto3
        sns = boto3.Session(profile_name=sms.get("aws_profile") or None,
                            region_name=sms.get("aws_region", "us-west-2")).client("sns")
        sns.publish(
            PhoneNumber=sms["phone_number"],
            Message=message[:1600],
            MessageAttributes={"AWS.SNS.SMS.SMSType":
                               {"DataType": "String", "StringValue": "Transactional"}})
        log.info("SMS alert sent to %s", sms["phone_number"])
        return True
    except Exception as e:
        log.error("SMS failed: %s", e)
        return False


def send_imessage(cfg: dict, message: str) -> bool:
    """Text via Messages.app on this Mac (iMessage). Never raises."""
    im = cfg.get("imessage", {})
    if not im.get("enabled") or not im.get("phone_number"):
        return False
    try:
        script = [
            "on run argv",
            'tell application "Messages"',
            "set svc to 1st service whose service type = iMessage",
            "send (item 1 of argv) to buddy (item 2 of argv) of svc",
            "end tell",
            "end run",
        ]
        cmd = ["osascript"]
        for line in script:
            cmd += ["-e", line]
        cmd += [message[:1000], im["phone_number"]]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            log.info("iMessage alert sent to %s", im["phone_number"])
            return True
        log.error("iMessage failed: %s", (r.stderr or "").strip()[:200])
        return False
    except Exception as e:
        log.error("iMessage failed: %s", e)
        return False


def phone_ping(cfg: dict, message: str) -> bool:
    """Reach the phone by whatever text channel is enabled (SMS and/or iMessage).
    True if at least one got through."""
    ok = send_sms(cfg, message)
    return send_imessage(cfg, message) or ok


def phone_wanted(cfg: dict) -> bool:
    return bool(cfg.get("sms", {}).get("enabled") or cfg.get("imessage", {}).get("enabled"))


def send_plain(cfg: dict, subject: str, text: str) -> bool:
    """Plain-text operational email (degraded-mode / watchdog warnings)."""
    return send_email(cfg, subject, text, f"<pre>{text}</pre>")


def macos_notify(title: str, message: str):
    try:
        script = 'display notification "{}" with title "{}" sound name "Glass"'.format(
            message.replace("\\", "").replace('"', "'"), title.replace('"', "'"))
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True, timeout=10)
    except Exception as e:
        log.warning("macOS notification failed: %s", e)
